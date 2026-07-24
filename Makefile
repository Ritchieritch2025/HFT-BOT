CXX ?= clang++
BUILD := build
OPENSSL_DIR := third_party/openssl
SIMDJSON_DIR := third_party/simdjson

# On Linux deploy hosts, use the system OpenSSL: mixing the vendored static
# libcrypto with a distro libcurl (itself linked against distro OpenSSL)
# risks two OpenSSL copies clashing in one process. The vendored build is a
# macOS-arm64 dev convenience (this box has no Homebrew).
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Linux)
  OPENSSL_INC :=
  CRYPTO_LIBS := -lcrypto -lpthread -ldl
else
  OPENSSL_INC := -isystem $(OPENSSL_DIR)/include
  CRYPTO_LIBS := $(OPENSSL_DIR)/lib/libcrypto.a
endif

CXXFLAGS := -std=c++23 -O2 -Wall -Wextra -Wpedantic \
            -Iinclude -Iapps -I$(SIMDJSON_DIR) $(OPENSSL_INC)
LDLIBS := $(CRYPTO_LIBS) -lcurl

# Pure-C++ unit tests (no curl/OpenSSL) — fast to build, sanitizer-clean.
PURE_TESTS := $(BUILD)/test_ring $(BUILD)/test_fixedpoint $(BUILD)/test_ids \
              $(BUILD)/test_env_safety $(BUILD)/test_bus $(BUILD)/test_orderbook $(BUILD)/test_recovery \
              $(BUILD)/test_readability $(BUILD)/test_sid_stream $(BUILD)/test_token_bucket \
              $(BUILD)/test_backoff $(BUILD)/test_secret_redaction $(BUILD)/test_shadow_engine

BINS := $(BUILD)/kalshi_example $(BUILD)/test_signing $(BUILD)/test_integration \
        $(BUILD)/test_resp $(BUILD)/ingestd $(BUILD)/tradingd \
        $(BUILD)/preflight $(BUILD)/bench_rtt $(BUILD)/bench_order \
        $(BUILD)/test_rest_api $(BUILD)/bench_orderbook $(BUILD)/test_storage \
        $(BUILD)/test_shadow $(BUILD)/test_decode $(BUILD)/test_ws_client \
        $(BUILD)/test_recorder $(BUILD)/test_replay $(BUILD)/ws_smoke \
        $(BUILD)/ws_shadow $(BUILD)/bench_ws_decode $(BUILD)/bench_engine \
        $(BUILD)/test_account_limits $(BUILD)/test_endpoint_costs \
        $(BUILD)/test_request_spec $(BUILD)/test_request_executor \
        $(BUILD)/test_batch_cost $(BUILD)/probe_batch_cost $(BUILD)/shadow_mm_replay $(PURE_TESTS)

all: $(BINS)

$(BUILD):
	mkdir -p $(BUILD)

# Scratch dir for test output (raw logs, replays, rotation shards) — keeps the
# repo root clean. Tests that take a dir arg write here; the rest ignore it.
SCRATCH := $(BUILD)/scratch
$(SCRATCH): | $(BUILD)
	mkdir -p $(SCRATCH)

$(BUILD)/client.o: src/client.cpp include/kalshi/client.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/resp.o: src/resp.cpp include/kalshi/resp.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/env.o: src/env.cpp include/kalshi/env.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/rest_api.o: src/rest_api.cpp include/kalshi/rest_api.hpp include/kalshi/client.hpp include/kalshi/env.hpp include/kalshi/limits.hpp include/trading/bus.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/limits.o: src/limits.cpp include/kalshi/limits.hpp include/kalshi/client.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/request_spec.o: src/request_spec.cpp include/kalshi/request_spec.hpp include/kalshi/limits.hpp include/kalshi/client.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/request_executor.o: src/request_executor.cpp include/kalshi/request_executor.hpp \
    include/kalshi/api_error.hpp include/kalshi/telemetry.hpp include/kalshi/token_bucket.hpp \
    include/kalshi/backoff.hpp include/kalshi/request_spec.hpp include/kalshi/limits.hpp \
    include/kalshi/client.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/storage.o: src/storage.cpp include/trading/storage.hpp include/trading/bus.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/gateway.o: src/gateway.cpp include/kalshi/gateway.hpp include/kalshi/env.hpp include/trading/storage.hpp include/trading/bus.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/ws_client.o: src/ws_client.cpp include/kalshi/ws_client.hpp include/kalshi/ws_transport.hpp include/kalshi/orderbook.hpp include/kalshi/sid_stream.hpp include/kalshi/gateway.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/ix_transport.o: src/ix_transport.cpp include/kalshi/ix_transport.hpp include/kalshi/ws_transport.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) $(IXWS_INC) -DIXWEBSOCKET_USE_TLS -DIXWEBSOCKET_USE_OPEN_SSL -c $< -o $@

# WS integration smoke: real ixwebsocket transport + client. Links the vendored
# ixwebsocket static lib + OpenSSL (libssl + libcrypto).
$(BUILD)/ws_smoke: apps/ws_smoke.cpp $(BUILD)/ix_transport.o $(BUILD)/ws_client.o \
                   $(BUILD)/gateway.o $(BUILD)/storage.o $(BUILD)/env.o \
                   $(BUILD)/simdjson.o $(BUILD)/ixwebsocket.a
	$(CXX) $(CXXFLAGS) apps/ws_smoke.cpp $(BUILD)/ix_transport.o $(BUILD)/ws_client.o \
	    $(BUILD)/gateway.o $(BUILD)/storage.o $(BUILD)/env.o $(BUILD)/simdjson.o \
	    $(BUILD)/ixwebsocket.a $(SSL_LIBS) $(CRYPTO_LIBS) -o $@

# Phase 8 read-only shadow smoke: full WS engine + recorder + read-only REST
# cross-check. Links ixwebsocket + OpenSSL + libcurl (REST client for auth signing
# and the batch-orderbook cross-check).
$(BUILD)/ws_shadow: apps/ws_shadow.cpp $(BUILD)/ix_transport.o $(BUILD)/ws_client.o \
                    $(BUILD)/gateway.o $(BUILD)/storage.o $(BUILD)/env.o \
                    $(BUILD)/rest_api.o $(BUILD)/request_executor.o $(BUILD)/request_spec.o \
                    $(BUILD)/limits.o $(BUILD)/client.o $(BUILD)/simdjson.o \
                    $(BUILD)/ixwebsocket.a
	$(CXX) $(CXXFLAGS) apps/ws_shadow.cpp $(BUILD)/ix_transport.o $(BUILD)/ws_client.o \
	    $(BUILD)/gateway.o $(BUILD)/storage.o $(BUILD)/env.o $(BUILD)/rest_api.o \
	    $(BUILD)/request_executor.o $(BUILD)/request_spec.o \
	    $(BUILD)/limits.o $(BUILD)/client.o $(BUILD)/simdjson.o $(BUILD)/ixwebsocket.a \
	    $(SSL_LIBS) $(CRYPTO_LIBS) -lcurl -o $@

$(BUILD)/strategies.o: src/strategies.cpp include/kalshi/strategy.hpp include/kalshi/wire.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

# simdjson is third-party; build it quietly.
$(BUILD)/simdjson.o: $(SIMDJSON_DIR)/simdjson.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -w -c $< -o $@

# --- vendored ixwebsocket: OpenSSL TLS, permessage-deflate OFF at build ---
# (no IXWEBSOCKET_USE_ZLIB; AppleSSL/MbedTLS backends excluded). C++17, warnings
# silenced (third-party). Needs libssl in addition to libcrypto.
IXWS_DIR := third_party/ixwebsocket/ixwebsocket
IXWS_INC := -Ithird_party/ixwebsocket
IXWS_SRCS := $(filter-out $(IXWS_DIR)/IXSocketAppleSSL.cpp $(IXWS_DIR)/IXSocketMbedTLS.cpp,\
                          $(wildcard $(IXWS_DIR)/*.cpp))
IXWS_OBJS := $(patsubst $(IXWS_DIR)/%.cpp,$(BUILD)/ixws/%.o,$(IXWS_SRCS))
IXWS_FLAGS := -std=c++17 -O2 -w $(IXWS_INC) $(OPENSSL_INC) \
              -DIXWEBSOCKET_USE_TLS -DIXWEBSOCKET_USE_OPEN_SSL
ifeq ($(UNAME_S),Linux)
  SSL_LIBS := -lssl
else
  SSL_LIBS := $(OPENSSL_DIR)/lib/libssl.a
endif

$(BUILD)/ixws:
	mkdir -p $(BUILD)/ixws

$(BUILD)/ixws/%.o: $(IXWS_DIR)/%.cpp | $(BUILD)/ixws
	$(CXX) $(IXWS_FLAGS) -c $< -o $@

$(BUILD)/ixwebsocket.a: $(IXWS_OBJS)
	ar rcs $@ $^

$(BUILD)/kalshi_example: examples/kalshi_example.cpp $(BUILD)/client.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

$(BUILD)/test_signing: tests/test_signing.cpp $(BUILD)/client.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

$(BUILD)/bench_engine: apps/bench_engine.cpp apps/bench_engine_core.hpp apps/feed.hpp $(BUILD)/client.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/bench_engine.cpp $(BUILD)/client.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/test_engine_bench: tests/test_engine_bench.cpp apps/bench_engine_core.hpp apps/feed.hpp $(BUILD)/client.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) tests/test_engine_bench.cpp $(BUILD)/client.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/test_integration: tests/test_integration.cpp $(BUILD)/client.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

$(BUILD)/test_resp: tests/test_resp.cpp $(BUILD)/resp.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_ring: tests/test_ring.cpp include/kalshi/ring.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_ring.cpp -o $@

$(BUILD)/test_shadow_engine: tests/test_shadow_engine.cpp include/trading/shadow/engine.hpp include/trading/shadow/fill_sim.hpp include/trading/shadow/order_registry.hpp include/trading/shadow/ledger.hpp include/trading/shadow/fees.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_shadow_engine.cpp -o $@

$(BUILD)/shadow_mm_replay: apps/shadow_mm_replay.cpp include/trading/shadow/engine.hpp include/trading/shadow/fill_sim.hpp include/trading/shadow/order_registry.hpp include/trading/shadow/ledger.hpp include/trading/shadow/fees.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) apps/shadow_mm_replay.cpp -o $@

$(BUILD)/test_token_bucket: tests/test_token_bucket.cpp include/kalshi/token_bucket.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_token_bucket.cpp -o $@

$(BUILD)/test_backoff: tests/test_backoff.cpp include/kalshi/backoff.hpp include/kalshi/request_spec.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_backoff.cpp -o $@

$(BUILD)/test_secret_redaction: tests/test_secret_redaction.cpp include/kalshi/redact.hpp include/kalshi/telemetry.hpp include/kalshi/env.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_secret_redaction.cpp -o $@

$(BUILD)/test_request_executor: tests/test_request_executor.cpp $(BUILD)/request_executor.o $(BUILD)/request_spec.o $(BUILD)/limits.o $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

$(BUILD)/test_batch_cost: tests/test_batch_cost.cpp include/kalshi/request_spec.hpp $(BUILD)/limits.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) tests/test_batch_cost.cpp $(BUILD)/limits.o $(BUILD)/simdjson.o -o $@

# Read-only demo probe (T6/F8): batch-orderbook throughput to first throttle.
$(BUILD)/probe_batch_cost: apps/probe_batch_cost.cpp $(BUILD)/rest_api.o $(BUILD)/request_executor.o $(BUILD)/request_spec.o $(BUILD)/limits.o $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

# Read-only account tier inspector.
$(BUILD)/account_info: apps/account_info.cpp $(BUILD)/rest_api.o $(BUILD)/request_executor.o $(BUILD)/request_spec.o $(BUILD)/limits.o $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

# One-shot API usage-tier upgrade (30 Write tokens; the only write it performs).
$(BUILD)/account_upgrade: apps/account_upgrade.cpp $(BUILD)/rest_api.o $(BUILD)/request_executor.o $(BUILD)/request_spec.o $(BUILD)/limits.o $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

# Read-only rate-limit probe: raw calls (no local bucket) to observe server 429s.
$(BUILD)/rate_probe: apps/rate_probe.cpp $(BUILD)/rest_api.o $(BUILD)/request_executor.o $(BUILD)/request_spec.o $(BUILD)/limits.o $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

$(BUILD)/test_fixedpoint: tests/test_fixedpoint.cpp include/trading/fixedpoint.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_fixedpoint.cpp -o $@

$(BUILD)/test_ids: tests/test_ids.cpp include/trading/ids.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_ids.cpp -o $@

$(BUILD)/test_env_safety: tests/test_env_safety.cpp $(BUILD)/env.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_bus: tests/test_bus.cpp include/trading/bus.hpp include/trading/test_doubles.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_bus.cpp -o $@

$(BUILD)/test_rest_api: tests/test_rest_api.cpp $(BUILD)/rest_api.o $(BUILD)/request_executor.o $(BUILD)/request_spec.o $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/limits.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

# Limits/cost parsers are fixture-driven (no network): link only limits.o + simdjson.o.
$(BUILD)/test_account_limits: tests/test_account_limits.cpp $(BUILD)/limits.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@
$(BUILD)/test_endpoint_costs: tests/test_endpoint_costs.cpp $(BUILD)/limits.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@
$(BUILD)/test_request_spec: tests/test_request_spec.cpp $(BUILD)/request_spec.o $(BUILD)/limits.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_orderbook: tests/test_orderbook.cpp include/kalshi/orderbook.hpp include/kalshi/sid_stream.hpp include/trading/bus.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_orderbook.cpp -o $@

$(BUILD)/test_sid_stream: tests/test_sid_stream.cpp include/kalshi/sid_stream.hpp include/kalshi/orderbook.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_sid_stream.cpp -o $@

$(BUILD)/test_recovery: tests/test_recovery.cpp include/kalshi/recovery.hpp include/kalshi/orderbook.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_recovery.cpp -o $@

$(BUILD)/test_readability: tests/test_readability.cpp include/trading/format.hpp include/kalshi/format.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_readability.cpp -o $@

$(BUILD)/bench_orderbook: apps/bench_orderbook.cpp include/kalshi/orderbook.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) apps/bench_orderbook.cpp -o $@

$(BUILD)/bench_ws_decode: apps/bench_ws_decode.cpp $(BUILD)/gateway.o $(BUILD)/storage.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@

# Decoder/reader fuzzer under ASan+UBSan (OUR JSON parsing surface). simdjson is
# excluded from instrumentation via the ignore-list — it does deliberate
# low-level ops (SIMDJSON_ASSUME etc.) that UBSan flags but are safe by design.
$(BUILD)/fuzz_decode: tests/fuzz_decode.cpp src/gateway.cpp src/storage.cpp src/env.cpp src/limits.cpp src/request_spec.cpp $(BUILD)/simdjson.o tests/sanitizer_ignore.txt | $(BUILD)
	$(CXX) -std=c++23 -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer \
	    -fsanitize-ignorelist=tests/sanitizer_ignore.txt \
	    -Iinclude -I$(SIMDJSON_DIR) $(OPENSSL_INC) \
	    tests/fuzz_decode.cpp src/gateway.cpp src/storage.cpp src/env.cpp src/limits.cpp src/request_spec.cpp $(BUILD)/simdjson.o -o $@

# fuzz_decode takes [iters] [dir] positionally — pass BOTH, else a lone dir is
# parsed as atoi(dir)=0 iterations (silently fuzzing nothing) and the reader
# sweep litters the repo root.
fuzz: $(BUILD)/fuzz_decode | $(SCRATCH)
	ASAN_OPTIONS=detect_leaks=0 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 $(BUILD)/fuzz_decode 200000 $(SCRATCH)

$(BUILD)/test_storage: tests/test_storage.cpp $(BUILD)/storage.o $(BUILD)/gateway.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_decode: tests/test_decode.cpp $(BUILD)/gateway.o $(BUILD)/storage.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_replay: tests/test_replay.cpp $(BUILD)/gateway.o $(BUILD)/storage.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_ws_client: tests/test_ws_client.cpp $(BUILD)/ws_client.o $(BUILD)/gateway.o $(BUILD)/storage.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_recorder: tests/test_recorder.cpp include/kalshi/ws_recorder.hpp $(BUILD)/storage.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) tests/test_recorder.cpp $(BUILD)/storage.o $(BUILD)/simdjson.o -o $@

# TSan build of the 2-thread recorder (producer + writer thread; pure C++).
$(BUILD)/test_recorder_tsan: tests/test_recorder.cpp include/kalshi/ws_recorder.hpp src/storage.cpp $(BUILD)/simdjson.o | $(BUILD)
	$(CXX) -std=c++23 -O1 -g -fsanitize=thread -Iinclude -I$(SIMDJSON_DIR) $(OPENSSL_INC) \
	    tests/test_recorder.cpp src/storage.cpp $(BUILD)/simdjson.o -o $@

$(BUILD)/test_shadow: tests/test_shadow.cpp $(BUILD)/gateway.o $(BUILD)/storage.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@

# --- the trading engine (hot path) and the tape recorder (cold path) ---

$(BUILD)/tradingd: apps/tradingd.cpp apps/feed.hpp apps/daemon_util.hpp \
                   include/kalshi/ring.hpp $(BUILD)/client.o $(BUILD)/resp.o \
                   $(BUILD)/strategies.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/tradingd.cpp $(BUILD)/client.o $(BUILD)/resp.o \
	    $(BUILD)/strategies.o $(BUILD)/env.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/ingestd: apps/ingestd.cpp apps/feed.hpp $(BUILD)/client.o $(BUILD)/resp.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/ingestd.cpp $(BUILD)/client.o $(BUILD)/resp.o $(BUILD)/env.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/preflight: apps/preflight.cpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/preflight.cpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/bench_rtt: apps/bench_rtt.cpp apps/feed.hpp $(BUILD)/client.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/bench_rtt.cpp $(BUILD)/client.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/bench_order: apps/bench_order.cpp include/kalshi/wire.hpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/bench_order.cpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/pnl_latency_probe: apps/pnl_latency_probe.cpp include/kalshi/wire.hpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/pnl_latency_probe.cpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/fill_test: apps/fill_test.cpp include/kalshi/wire.hpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/fill_test.cpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

# ThreadSanitizer builds. Note: libcrypto/libcurl are not TSan-instrumented,
# so these validate our ring/doorbell/pool call sites — not OpenSSL internals.
# test_ring_tsan is pure C++ and fully instrumented.
$(BUILD)/test_integration_tsan: tests/test_integration.cpp src/client.cpp | $(BUILD)
	$(CXX) -std=c++23 -O1 -g -fsanitize=thread \
	    -Iinclude $(OPENSSL_INC) \
	    tests/test_integration.cpp src/client.cpp -o $@ $(LDLIBS)

$(BUILD)/test_ring_tsan: tests/test_ring.cpp include/kalshi/ring.hpp | $(BUILD)
	$(CXX) -std=c++23 -O1 -g -fsanitize=thread -Iinclude tests/test_ring.cpp -o $@

$(BUILD)/test_token_bucket_tsan: tests/test_token_bucket.cpp include/kalshi/token_bucket.hpp | $(BUILD)
	$(CXX) -std=c++23 -O1 -g -fsanitize=thread -Iinclude tests/test_token_bucket.cpp -o $@

$(BUILD)/tradingd_tsan: apps/tradingd.cpp apps/feed.hpp src/client.cpp src/resp.cpp src/strategies.cpp src/env.cpp $(BUILD)/simdjson.o | $(BUILD)
	$(CXX) -std=c++23 -O1 -g -fsanitize=thread \
	    -Iinclude -Iapps -I$(SIMDJSON_DIR) $(OPENSSL_INC) \
	    apps/tradingd.cpp src/client.cpp src/resp.cpp src/strategies.cpp src/env.cpp \
	    $(BUILD)/simdjson.o -o $@ $(LDLIBS)

tsan: $(BUILD)/test_integration_tsan $(BUILD)/test_ring_tsan $(BUILD)/tradingd_tsan $(BUILD)/test_recorder_tsan $(BUILD)/test_token_bucket_tsan

# ASan+UBSan builds of the pure integer-heavy tests (fixedpoint parsers +
# orderbook delta math are where overflow/off-by-one hide). Pure C++ so fully
# instrumented. SAN_TESTS grows as phases land.
SAN_SRCS := tests/test_fixedpoint.cpp tests/test_ids.cpp tests/test_bus.cpp \
            tests/test_orderbook.cpp tests/test_sid_stream.cpp tests/test_recovery.cpp \
            tests/test_token_bucket.cpp tests/test_backoff.cpp tests/test_secret_redaction.cpp
SANFLAGS := -std=c++23 -O1 -g -fsanitize=address,undefined \
            -fno-omit-frame-pointer -Iinclude

san: | $(BUILD)
	@set -e; for src in $(SAN_SRCS); do \
	  bin=$(BUILD)/$$(basename $$src .cpp)_san; \
	  echo "  ASAN/UBSAN $$src"; \
	  $(CXX) $(SANFLAGS) $$src -o $$bin; \
	  ASAN_OPTIONS=detect_leaks=0 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 $$bin >/dev/null; \
	done; echo "  sanitizers clean"

# Grep-based safety gates (forbidden patterns in live source). See tools/check_gates.sh.
gate:
	@./tools/check_gates.sh

# Fixture-driven tests: link simdjson but hit no network — safe to run in `check`.
OFFLINE_TESTS := $(BUILD)/test_account_limits $(BUILD)/test_endpoint_costs \
                 $(BUILD)/test_request_spec $(BUILD)/test_batch_cost \
                 $(BUILD)/test_engine_bench

# Build + run every pure + offline (fixture-driven) unit test, after the gates.
# Every test is passed $(SCRATCH) as argv[1]; file-writing tests use it, the rest
# ignore it — so no test litters the repo root.
check: gate $(PURE_TESTS) $(OFFLINE_TESTS) | $(SCRATCH)
	@set -e; for t in $(PURE_TESTS) $(OFFLINE_TESTS); do echo "== $$t"; $$t $(SCRATCH) | tail -1; done
	@python3 tests/test_experiment_registry.py | tail -1
	@python3 tools/experiment_registry.py --check

test: $(BUILD)/test_signing
	$(BUILD)/test_signing

clean:
	rm -rf $(BUILD)

# Ops console (localhost, read-only wrt trading). Add --allow-network to enable
# network_read tools; live_order tools are always refused.
dashboard: | $(BUILD)
	python3 dashboard_server.py --metrics work/metrics.ndjson \
	    --results work/test_results.ndjson --port 8765

.PHONY: all tsan test clean gate check dashboard
