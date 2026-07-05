// Typed REST tests against tests/mock_rest.py: pagination, the 429/Retry-After
// matrix, Kalshi error-body parsing, dollar-string + cents orderbook decode,
// and exact V2 order-JSON construction. No real network, no real orders.
//
// usage: test_rest_api <base_url>   (e.g. http://127.0.0.1:PORT)

#include "kalshi/env.hpp"
#include "kalshi/rest_api.hpp"

#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>

#include <chrono>
#include <iostream>
#include <string>

using namespace kalshi;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

std::string throwaway_key() {
  EVP_PKEY* k = EVP_RSA_gen(2048);
  BIO* bio = BIO_new(BIO_s_mem());
  PEM_write_bio_PrivateKey(bio, k, nullptr, nullptr, 0, nullptr, nullptr);
  char* data = nullptr;
  long len = BIO_get_mem_data(bio, &data);
  std::string pem(data, static_cast<size_t>(len));
  BIO_free(bio);
  EVP_PKEY_free(k);
  return pem;
}

long ms_since(std::chrono::steady_clock::time_point t0) {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::steady_clock::now() - t0).count();
}
}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "usage: test_rest_api <base_url>\n";
    return 2;
  }
  Config cfg;
  cfg.api_key_id = "test";
  cfg.private_key_pem = throwaway_key();
  cfg.base_url = argv[1];
  cfg.pool_size = 2;
  KalshiClient client(std::move(cfg));

  Runtime rt;  // local_mock defaults are fine; base_url comes from the client
  rt.rest_base_url = argv[1];
  // Fast policy + high rate so timing assertions aren't dominated by rate limit.
  RetryPolicy policy;
  policy.base_ms = 40;
  policy.max_attempts = 4;
  policy.retry_after_cap_ms = 120;
  RestApi api(client, rt, policy);  // read/write buckets start at conservative basic tier

  // 1. exchange status
  {
    auto s = api.exchange_status();
    check(s && s->exchange_active && s->trading_active, "exchange_status active");
  }

  // 2. pagination: walk cursor until empty, aggregate
  {
    std::vector<MarketSummary> all;
    std::string cursor;
    int pages = 0;
    for (;;) {
      MarketsQuery q;
      q.series_ticker = "PAGED";
      q.cursor = cursor;
      auto page = api.markets(q);
      if (!page) { check(false, "pagination page fetch"); break; }
      ++pages;
      for (auto& m : page->markets) all.push_back(m);
      if (page->next_cursor.empty()) break;
      cursor = page->next_cursor;
      if (pages > 5) break;  // safety
    }
    check(pages == 2 && all.size() == 3, "pagination: 2 pages, 3 markets aggregated");
    check(all.size() == 3 && all[0].yes_bid == 4200,
          "market yes_bid decoded to PriceE4 (0.4200 -> 4200)");
  }

  // 3. 429 with no Retry-After -> exponential backoff, then success
  {
    auto t0 = std::chrono::steady_clock::now();
    MarketsQuery q; q.series_ticker = "RETRY_NONE";
    auto r = api.markets(q);
    long el = ms_since(t0);
    check(r.has_value(), "429/none eventually succeeds after retries");
    check(el >= 60, "429/none backed off (>=60ms for two backoffs)");
  }

  // 4. 429 with huge Retry-After -> IGNORED (F9: telemetry-only), backoff used.
  {
    auto t0 = std::chrono::steady_clock::now();
    MarketsQuery q; q.series_ticker = "RETRY_DELTA";
    auto r = api.markets(q);
    long el = ms_since(t0);
    check(r.has_value(), "429/delta succeeds via backoff");
    check(el <= 2000, "429/delta did NOT honor Retry-After:3600 (no stall) — telemetry only");
  }

  // 5. 429 with garbage Retry-After -> irrelevant now (Retry-After never honored).
  {
    MarketsQuery q; q.series_ticker = "RETRY_GARBAGE";
    auto r = api.markets(q);
    check(r.has_value(), "429/garbage succeeds (Retry-After ignored regardless)");
  }

  // 6. Kalshi error body -> structured ApiError
  {
    MarketsQuery q; q.series_ticker = "ERR_AUTH";
    auto r = api.markets(q);
    check(!r.has_value(), "auth error surfaces as ApiError");
    check(!r && r.error().kind == ApiError::Kind::Kalshi, "error kind == Kalshi");
    check(!r && r.error().kalshi_code == "authentication_error",
          "error kalshi_code parsed from body");
  }

  // 7. orderbook, dollar strings -> fixed point, sorted ascending
  {
    auto ob = api.orderbook("SOME-MKT");
    check(ob.has_value(), "orderbook fetch");
    check(ob && ob->yes.size() == 2 && ob->yes[0].price == 800 && ob->yes[1].price == 4200,
          "yes levels decoded + sorted (0.0800,0.4200)");
    check(ob && ob->yes[1].size == 10000, "level size 100.00 -> 10000 CountFp");
  }

  // 8. orderbook, integer cents -> cents_to_e4
  {
    auto ob = api.orderbook("CENTS-MKT");
    check(ob.has_value(), "cents orderbook fetch");
    check(ob && ob->yes.size() == 2 && ob->yes[0].price == 800 && ob->yes[1].price == 4200,
          "cents 8,42 -> PriceE4 800,4200");
  }

  // 9. typed V2 order JSON (construction only)
  {
    OrderSpec spec;
    spec.ticker = "KX-TEST";
    spec.buy = true;
    spec.side = trading::Side::Yes;
    spec.price = 5500;  // $0.5500
    spec.count = 100;   // 1 contract
    spec.client_order_id = "cid-1";
    const std::string j = RestApi::build_order_json(spec);
    const std::string want =
        R"({"ticker":"KX-TEST","side":"bid","count":"1.00","price":"0.5500",)"
        R"("time_in_force":"good_till_canceled","self_trade_prevention_type":"taker_at_cross",)"
        R"("client_order_id":"cid-1"})";
    check(j == want, std::string("order JSON exact match") + (j == want ? "" : "\n    got: " + j));
    // sell-YES normalizes to an ask at the same price
    OrderSpec sell = spec; sell.buy = false;
    check(RestApi::build_order_json(sell).find(R"("side":"ask")") != std::string::npos,
          "sell-YES -> ask");
    // buy-NO normalizes to a bid at complementary price (1.0 - price)
    OrderSpec no = spec; no.side = trading::Side::No; no.price = 3000;
    check(RestApi::build_order_json(no).find(R"("price":"0.7000")") != std::string::npos,
          "buy-NO at 0.30 -> yes-price 0.70");
  }

  // 10. KalshiDataSource: REST orderbook -> source-agnostic BookSnapshot event
  {
    struct Sink : trading::EventSink {
      int snapshots = 0;
      trading::SourceId src = trading::SourceId::Unknown;
      trading::PriceE4 best_yes = 0;
      bool trace_ok = true;
      void on_event(const trading::NormalizedEvent& e) override {
        if (e.trace_id.v == 0) trace_ok = false;
        src = e.source;
        if (e.kind() == trading::Kind::BookSnapshot) {
          ++snapshots;
          const auto& bs = std::get<trading::BookSnapshot>(e.payload);
          for (const auto& l : bs.yes) best_yes = std::max(best_yes, l.price);
        }
      }
    } sink;
    trading::EntityRegistry reg;
    KalshiDataSource ds(api, {"SOME-MKT"}, reg);
    ds.set_sink(&sink);
    const int n = ds.poll_once();
    check(n == 1 && sink.snapshots == 1, "KalshiDataSource emits a BookSnapshot");
    check(sink.src == trading::SourceId::Kalshi, "event source is Kalshi");
    check(sink.trace_ok, "event carries a trace_id");
    check(sink.best_yes == 4200, "best yes level decoded to PriceE4 (0.4200)");
  }

  // 11. batch orderbook (form-explode) — REST bootstrap/cross-check
  {
    auto obs = api.batch_orderbook({"MKT-A", "MKT-B"});
    check(obs.has_value() && obs->size() == 2, "batch_orderbook returns 2 books");
    check(obs && (*obs)[0].ticker == "MKT-A" && (*obs)[0].yes.size() == 1 &&
              (*obs)[0].yes[0].price == 4200,
          "batch book decoded to PriceE4");
    auto too_many = std::vector<std::string>(101, "X");
    check(!api.batch_orderbook(too_many).has_value(), "batch size >100 rejected");
  }

  // 12. account limits + endpoint costs self-config (nested v3.23.0 schema).
  {
    auto lim = api.account_limits();
    check(lim && lim->from_server && lim->usage_tier == "advanced" &&
              lim->read.refill_rate == 300 && lim->read.bucket_capacity == 300 &&
              lim->write.refill_rate == 300 && lim->grants.size() == 2,
          "account_limits: nested advanced tier + 2 grants pulled from server");
    check(lim && lim->grants[0].source == "volume" && !lim->grants[0].expires_ts &&
              lim->grants[1].expires_ts.has_value(),
          "account_limits: permanent + expiring grant variants parsed");

    auto costs = api.endpoint_costs();
    check(costs && costs->from_server && costs->default_cost == 10,
          "endpoint_costs: default_cost 10 from server");
    check(costs && costs->cost_for(Method::Post, "/trade-api/v2/portfolio/orders") == 10 &&
              costs->cost_for(Method::Delete, "/trade-api/v2/portfolio/orders/abc-123") == 2 &&
              costs->cost_for(Method::Get, "/trade-api/v2/exchange/status") == 10,
          "endpoint_costs: exact + template match + default fallback");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
