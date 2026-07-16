// W-LAT-BENCH-01 Tier 1 — local engine-path benchmark. Times ExecPayload
// build, order_json serialization, and RSA-PSS sign_request (n>=10,000 each)
// and appends one ndjson line per metric to the output file. Pure CPU: no
// socket is ever opened, whatever credentials are in the environment.
//
//   bench_engine [n=10000] [--out work/latency_baseline/engine_bench.ndjson]
//
// Uses the real key from KALSHI_PRIVATE_KEY_PATH when set (RSA-PSS cost
// depends on the key), else a throwaway 2048-bit key; the ndjson records
// which (key_source/key_bits) so numbers are never silently mixed.

#include "bench_engine_core.hpp"
#include "feed.hpp"

#include <openssl/evp.h>
#include <openssl/pem.h>

#include <cstdio>
#include <cstring>
#include <ctime>
#include <string>
#include <unistd.h>

using namespace kalshi;

namespace {

std::string utc_now() {
  char buf[32];
  const std::time_t t = std::time(nullptr);
  std::tm tm{};
  gmtime_r(&t, &tm);
  std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tm);
  return buf;
}

int key_bits(const std::string& pem) {
  BIO* bio = BIO_new_mem_buf(pem.data(), static_cast<int>(pem.size()));
  if (!bio) return 0;
  EVP_PKEY* k = PEM_read_bio_PrivateKey(bio, nullptr, nullptr, nullptr);
  BIO_free(bio);
  if (!k) return 0;
  const int bits = EVP_PKEY_get_bits(k);
  EVP_PKEY_free(k);
  return bits;
}

}  // namespace

int main(int argc, char** argv) {
  int n = 10000;
  std::string out = "work/latency_baseline/engine_bench.ndjson";
  for (int i = 1; i < argc; ++i) {
    if (std::strcmp(argv[i], "--out") == 0 && i + 1 < argc) out = argv[++i];
    else if (std::atoi(argv[i]) > 0) n = std::atoi(argv[i]);
  }

  Config cfg;
  cfg.pool_size = 1;  // no connection is ever used
  const char* key_path = std::getenv("KALSHI_PRIVATE_KEY_PATH");
  const char* key_id = std::getenv("KALSHI_API_KEY_ID");
  std::string key_source;
  if (key_path && key_id) {
    cfg.api_key_id = key_id;
    cfg.private_key_pem = read_file(key_path);
    key_source = "env";
  } else {
    cfg.api_key_id = "00000000-0000-0000-0000-000000000000";
    cfg.private_key_pem = feed::throwaway_key_pem();
    key_source = "throwaway";
  }
  const int bits = key_bits(cfg.private_key_pem);
  KalshiClient client(std::move(cfg));

  std::printf("bench_engine: n=%d key=%s(%d bits)\n", n, key_source.c_str(), bits);
  const auto dists = bench::run_engine_bench(client, n);
  if (dists.empty()) {
    std::fprintf(stderr, "bench_engine: signing failed\n");
    return 1;
  }

  char host[128] = "unknown";
  gethostname(host, sizeof(host) - 1);
  std::FILE* f = std::fopen(out.c_str(), "a");
  if (!f) {
    std::fprintf(stderr, "bench_engine: cannot open %s\n", out.c_str());
    return 1;
  }
  const std::string ts = utc_now();
  for (const auto& d : dists) {
    std::fprintf(f,
                 "{\"ts_utc\":\"%s\",\"host\":\"%s\",\"metric\":\"%s\",\"n\":%zu,"
                 "\"min_us\":%.3f,\"p50_us\":%.3f,\"p90_us\":%.3f,\"p99_us\":%.3f,"
                 "\"max_us\":%.3f,\"key_source\":\"%s\",\"key_bits\":%d}\n",
                 ts.c_str(), host, d.metric.c_str(), d.n, d.min_us, d.p50_us,
                 d.p90_us, d.p99_us, d.max_us, key_source.c_str(), bits);
    std::printf("%-22s n=%zu min=%.1fus p50=%.1fus p90=%.1fus p99=%.1fus max=%.1fus\n",
                d.metric.c_str(), d.n, d.min_us, d.p50_us, d.p90_us, d.p99_us,
                d.max_us);
  }
  std::fclose(f);
  std::printf("appended %zu metrics to %s\n", dists.size(), out.c_str());
  return 0;
}
