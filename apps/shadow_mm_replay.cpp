// shadow_mm_replay — drive the shadow MM engine over an NDJSON tape and print
// the Phase-1 acceptance deliverables: per-family net PnL (real fees, fail
// closed), fill rate, post-fill markout, and the speed -> PnL curve.
//
// Record-only by construction: this binary has no network code at all.
//
// Tape format (one JSON object per line, tape time ascending):
//   {"t":"trade","ts":<epoch_ns>,"tk":"TICKER","fam":"Sports","p":45,"c":10,"y":1}
//      p = yes-price cents, c = contracts, y = 1 taker bought YES / 0 bought NO
//   {"t":"settle","ts":<epoch_ns>,"tk":"TICKER","yes":1}
//
// Usage:
//   shadow_mm_replay <tape.ndjson> [--track strict|queue|optimistic]
//                    [--off N] [--size N] [--cap N] [--two-sided]
//                    [--fees config/fees.csv]
//                    [--speeds ms,ms,...]        (default 2,7,25,100)
//
// Fees: loaded from --fees if given; otherwise EMPTY (every market UNKNOWN =
// net PnL reported as excluded; gross still shown). No fee is ever guessed.

#include "trading/shadow/engine.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <map>
#include <string>
#include <vector>

using namespace trading::shadow;

namespace {

// Minimal field scanners for our own writer's NDJSON (cold path, replay only).
bool find_str(const std::string& s, const char* key, std::string& out) {
  const std::string pat = std::string("\"") + key + "\":\"";
  const auto i = s.find(pat);
  if (i == std::string::npos) return false;
  const auto j = s.find('"', i + pat.size());
  if (j == std::string::npos) return false;
  out = s.substr(i + pat.size(), j - i - pat.size());
  return true;
}
bool find_int(const std::string& s, const char* key, long long& out) {
  const std::string pat = std::string("\"") + key + "\":";
  const auto i = s.find(pat);
  if (i == std::string::npos) return false;
  out = std::atoll(s.c_str() + i + pat.size());
  return true;
}

struct Args {
  std::string tape;
  Track track = Track::Strict;
  int off = 2, size = 5, cap = 200;
  bool two_sided = false;
  std::string fees_path;
  std::vector<std::uint64_t> speeds_ns = {2'000'000, 7'000'000, 25'000'000,
                                          100'000'000};
};

Args parse(int argc, char** argv) {
  Args a;
  if (argc < 2) {
    std::fprintf(stderr, "usage: shadow_mm_replay <tape.ndjson> [options]\n");
    std::exit(2);
  }
  a.tape = argv[1];
  for (int i = 2; i < argc; ++i) {
    const std::string s = argv[i];
    auto next = [&]() -> const char* {
      if (i + 1 >= argc) { std::fprintf(stderr, "missing value for %s\n", s.c_str()); std::exit(2); }
      return argv[++i];
    };
    if (s == "--track") {
      const std::string t = next();
      a.track = t == "queue" ? Track::Queue
              : t == "optimistic" ? Track::Optimistic : Track::Strict;
    } else if (s == "--off") a.off = std::atoi(next());
    else if (s == "--size") a.size = std::atoi(next());
    else if (s == "--cap") a.cap = std::atoi(next());
    else if (s == "--two-sided") a.two_sided = true;
    else if (s == "--fees") a.fees_path = next();
    else if (s == "--speeds") {
      a.speeds_ns.clear();
      const char* v = next();
      for (const char* p = v; *p;) {
        a.speeds_ns.push_back(std::strtoull(p, nullptr, 10) * 1'000'000ULL);
        while (*p && *p != ',') ++p;
        if (*p == ',') ++p;
      }
    }
  }
  return a;
}

struct TapeLine {
  bool is_trade = false;
  std::string tk, fam;
  long long ts = 0, p = 0, c = 0, y = 0, yes = 0;
};

}  // namespace

int main(int argc, char** argv) {
  const Args args = parse(argc, argv);
  FeeSchedule fees = args.fees_path.empty()
                         ? FeeSchedule{}
                         : FeeSchedule::load_csv(args.fees_path);
  if (args.fees_path.empty())
    std::printf("# fees: NONE LOADED — every market fee-UNKNOWN, net PnL "
                "excluded (fail closed). Gross shown for diagnosis only.\n");

  // Pre-parse the tape once (shared across speed sweep runs).
  std::vector<TapeLine> tape;
  {
    std::ifstream in(args.tape);
    if (!in) { std::fprintf(stderr, "cannot open %s\n", args.tape.c_str()); return 2; }
    std::string line;
    while (std::getline(in, line)) {
      TapeLine t;
      std::string kind;
      if (!find_str(line, "t", kind)) continue;
      if (!find_str(line, "tk", t.tk)) continue;
      find_int(line, "ts", t.ts);
      if (kind == "trade") {
        t.is_trade = true;
        find_str(line, "fam", t.fam);
        find_int(line, "p", t.p);
        find_int(line, "c", t.c);
        find_int(line, "y", t.y);
      } else if (kind == "settle") {
        find_int(line, "yes", t.yes);
      } else {
        continue;
      }
      tape.push_back(std::move(t));
    }
  }
  std::printf("# tape: %zu events from %s\n", tape.size(), args.tape.c_str());

  std::printf("\n== speed -> PnL curve · track=%s off=%d size=%d cap=%d %s ==\n",
              to_string(args.track), args.off, args.size, args.cap,
              args.two_sided ? "two-sided" : "ask-only");
  std::printf("%10s %10s %8s %12s %12s %12s %10s %10s\n", "speed_ms", "orders",
              "fills", "fill_rate", "gross_c", "net_c", "markout_c", "excl_mkts");

  for (const std::uint64_t sp : args.speeds_ns) {
    ShadowEngine eng(fees, args.track, SpeedParams{sp, sp});
    SimpleQuoter quoter(eng, args.off, args.size, args.cap, args.two_sided);
    std::map<std::string, std::uint32_t> mkts;
    for (const TapeLine& t : tape) {
      auto it = mkts.find(t.tk);
      if (it == mkts.end())
        it = mkts.emplace(t.tk, eng.market(t.tk, t.fam.empty() ? "?" : t.fam))
                 .first;
      if (t.is_trade) {
        eng.on_trade(it->second, static_cast<std::int32_t>(t.p),
                     static_cast<std::int32_t>(t.c), t.y != 0,
                     static_cast<std::uint64_t>(t.ts));
        quoter.on_trade(it->second, static_cast<std::int32_t>(t.p),
                        static_cast<std::uint64_t>(t.ts));
      } else {
        eng.on_settle(it->second, t.yes != 0, static_cast<std::uint64_t>(t.ts));
      }
    }
    long long gross = 0, net = 0, excl = 0;
    double mko_sum = 0; long long mko_n = 0;
    const auto rep = eng.ledger().report();
    for (const auto& [fam, r] : rep) {
      gross += r.gross_pnl_cents;
      net += r.net_pnl_cents;
      excl += r.excluded_fee_unknown_markets;
      mko_sum += r.markout_sum_cents;
      mko_n += r.markout_n;
    }
    std::printf("%10.1f %10lld %8lld %11.2f%% %12lld %12lld %10.2f %10lld\n",
                sp / 1e6, static_cast<long long>(eng.ledger().orders()),
                static_cast<long long>(eng.ledger().fills()),
                eng.ledger().fill_rate() * 100.0, gross, net,
                mko_n ? mko_sum / static_cast<double>(mko_n) : 0.0, excl);
  }

  // Per-family table at the slowest (most pessimistic) speed.
  {
    const std::uint64_t sp = args.speeds_ns.back();
    ShadowEngine eng(fees, args.track, SpeedParams{sp, sp});
    SimpleQuoter quoter(eng, args.off, args.size, args.cap, args.two_sided);
    std::map<std::string, std::uint32_t> mkts;
    for (const TapeLine& t : tape) {
      auto it = mkts.find(t.tk);
      if (it == mkts.end())
        it = mkts.emplace(t.tk, eng.market(t.tk, t.fam.empty() ? "?" : t.fam))
                 .first;
      if (t.is_trade) {
        eng.on_trade(it->second, static_cast<std::int32_t>(t.p),
                     static_cast<std::int32_t>(t.c), t.y != 0,
                     static_cast<std::uint64_t>(t.ts));
        quoter.on_trade(it->second, static_cast<std::int32_t>(t.p),
                        static_cast<std::uint64_t>(t.ts));
      } else {
        eng.on_settle(it->second, t.yes != 0, static_cast<std::uint64_t>(t.ts));
      }
    }
    std::printf("\n== per-family @ speed %.1fms (settled markets only) ==\n",
                sp / 1e6);
    std::printf("%-24s %8s %8s %12s %12s %10s %10s\n", "family", "markets",
                "settled", "gross_c", "net_c", "markout_c", "excl_mkts");
    for (const auto& [fam, r] : eng.ledger().report()) {
      std::printf("%-24s %8lld %8lld %12lld %12lld %10.2f %10lld\n",
                  fam.c_str(), static_cast<long long>(r.markets),
                  static_cast<long long>(r.settled_markets),
                  static_cast<long long>(r.gross_pnl_cents),
                  static_cast<long long>(r.net_pnl_cents),
                  r.markout_n ? r.markout_sum_cents /
                                    static_cast<double>(r.markout_n)
                              : 0.0,
                  static_cast<long long>(r.excluded_fee_unknown_markets));
    }
  }
  std::printf("\nSHADOW REPLAY DONE\n");
  return 0;
}
