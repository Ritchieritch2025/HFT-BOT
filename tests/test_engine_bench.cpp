// Smoke test for the W-LAT-BENCH-01 Tier 1 measurement core (`make check`
// hook). Runs the engine-path benchmark with a throwaway key at small n and
// asserts the distributions are sane — nonempty, ordered percentiles,
// signing visibly nonzero. No network, no credentials, writes only to the
// scratch dir passed as argv[1].

#include "bench_engine_core.hpp"
#include "feed.hpp"

#include <cstdio>
#include <string>

using namespace kalshi;

static int g_failures = 0;
static void check(bool ok, const char* what) {
  if (!ok) {
    ++g_failures;
    std::printf("FAIL: %s\n", what);
  }
}

int main(int argc, char** argv) {
  Config cfg;
  cfg.pool_size = 1;
  cfg.api_key_id = "00000000-0000-0000-0000-000000000000";
  cfg.private_key_pem = feed::throwaway_key_pem();
  KalshiClient client(std::move(cfg));

  const int n = 64;
  const auto dists = bench::run_engine_bench(client, n);
  check(dists.size() == 4, "bench: four metrics produced");
  for (const auto& d : dists) {
    check(d.n == static_cast<std::size_t>(n), "bench: full sample count");
    check(d.min_us <= d.p50_us && d.p50_us <= d.p90_us && d.p90_us <= d.p99_us &&
              d.p99_us <= d.max_us,
          "bench: percentiles ordered");
  }
  if (dists.size() == 4) {
    check(dists[2].metric == "sign_request", "bench: metric order stable");
    check(dists[2].p50_us > 1.0, "bench: RSA-PSS sign is not free (>1us)");
    check(dists[2].p50_us < 100000.0, "bench: sign p50 under 100ms sanity bound");
    // chain and sign are independently sampled; the chain runs cache-warm in
    // the same iteration, so demand only that a signing-scale cost is inside.
    check(dists[3].p50_us >= dists[2].p50_us * 0.5,
          "bench: chain includes signing cost");
  }

  // Ring-handoff metric (gate-10 decision→handoff ruler): same sanity bar.
  const auto h = bench::run_ring_handoff_bench(n, /*warmup=*/16);
  check(h.metric == "ring_handoff", "handoff: metric name stable");
  check(h.n == static_cast<std::size_t>(n), "handoff: full sample count");
  check(h.min_us <= h.p50_us && h.p50_us <= h.p90_us && h.p90_us <= h.p99_us &&
            h.p99_us <= h.max_us,
        "handoff: percentiles ordered");
  check(h.p50_us < 1000.0, "handoff p50 under 1ms sanity bound");

  // The scratch-dir write path the real tool uses must work.
  if (argc > 1) {
    const std::string out = std::string(argv[1]) + "/engine_bench_smoke.ndjson";
    std::FILE* f = std::fopen(out.c_str(), "w");
    check(f != nullptr, "bench: scratch output writable");
    if (f) {
      std::fprintf(f, "{\"metric\":\"%s\",\"p50_us\":%.3f}\n",
                   dists.empty() ? "none" : dists[0].metric.c_str(),
                   dists.empty() ? 0.0 : dists[0].p50_us);
      std::fclose(f);
    }
  }

  std::printf("%s\n", g_failures == 0 ? "ALL PASS" : "FAILURES");
  return g_failures == 0 ? 0 : 1;
}
