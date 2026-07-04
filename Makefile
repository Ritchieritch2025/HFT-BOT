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
              $(BUILD)/test_env_safety $(BUILD)/test_bus $(BUILD)/test_orderbook \
              $(BUILD)/test_readability $(BUILD)/test_sid_stream

BINS := $(BUILD)/kalshi_example $(BUILD)/test_signing $(BUILD)/test_integration \
        $(BUILD)/test_resp $(BUILD)/ingestd $(BUILD)/tradingd \
        $(BUILD)/preflight $(BUILD)/bench_rtt $(BUILD)/bench_order \
        $(BUILD)/test_rest_api $(BUILD)/bench_orderbook $(BUILD)/test_storage \
        $(BUILD)/test_shadow $(PURE_TESTS)

all: $(BINS)

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/client.o: src/client.cpp include/kalshi/client.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/resp.o: src/resp.cpp include/kalshi/resp.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/env.o: src/env.cpp include/kalshi/env.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/rest_api.o: src/rest_api.cpp include/kalshi/rest_api.hpp include/kalshi/client.hpp include/kalshi/env.hpp include/trading/bus.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/storage.o: src/storage.cpp include/trading/storage.hpp include/trading/bus.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/gateway.o: src/gateway.cpp include/kalshi/gateway.hpp include/kalshi/env.hpp include/trading/storage.hpp include/trading/bus.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/strategies.o: src/strategies.cpp include/kalshi/strategy.hpp include/kalshi/wire.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

# simdjson is third-party; build it quietly.
$(BUILD)/simdjson.o: $(SIMDJSON_DIR)/simdjson.cpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -w -c $< -o $@

$(BUILD)/kalshi_example: examples/kalshi_example.cpp $(BUILD)/client.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

$(BUILD)/test_signing: tests/test_signing.cpp $(BUILD)/client.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

$(BUILD)/test_integration: tests/test_integration.cpp $(BUILD)/client.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

$(BUILD)/test_resp: tests/test_resp.cpp $(BUILD)/resp.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_ring: tests/test_ring.cpp include/kalshi/ring.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_ring.cpp -o $@

$(BUILD)/test_fixedpoint: tests/test_fixedpoint.cpp include/trading/fixedpoint.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_fixedpoint.cpp -o $@

$(BUILD)/test_ids: tests/test_ids.cpp include/trading/ids.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_ids.cpp -o $@

$(BUILD)/test_env_safety: tests/test_env_safety.cpp $(BUILD)/env.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_bus: tests/test_bus.cpp include/trading/bus.hpp include/trading/test_doubles.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_bus.cpp -o $@

$(BUILD)/test_rest_api: tests/test_rest_api.cpp $(BUILD)/rest_api.o $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

$(BUILD)/test_orderbook: tests/test_orderbook.cpp include/kalshi/orderbook.hpp include/kalshi/sid_stream.hpp include/trading/bus.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_orderbook.cpp -o $@

$(BUILD)/test_sid_stream: tests/test_sid_stream.cpp include/kalshi/sid_stream.hpp include/kalshi/orderbook.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_sid_stream.cpp -o $@

$(BUILD)/test_readability: tests/test_readability.cpp include/trading/format.hpp include/kalshi/format.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) tests/test_readability.cpp -o $@

$(BUILD)/bench_orderbook: apps/bench_orderbook.cpp include/kalshi/orderbook.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) apps/bench_orderbook.cpp -o $@

$(BUILD)/test_storage: tests/test_storage.cpp $(BUILD)/storage.o $(BUILD)/gateway.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/test_shadow: tests/test_shadow.cpp $(BUILD)/gateway.o $(BUILD)/storage.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@

# --- the trading engine (hot path) and the tape recorder (cold path) ---

$(BUILD)/tradingd: apps/tradingd.cpp apps/feed.hpp apps/daemon_util.hpp \
                   include/kalshi/ring.hpp $(BUILD)/client.o $(BUILD)/resp.o \
                   $(BUILD)/strategies.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/tradingd.cpp $(BUILD)/client.o $(BUILD)/resp.o \
	    $(BUILD)/strategies.o $(BUILD)/env.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/ingestd: apps/ingestd.cpp apps/feed.hpp $(BUILD)/client.o $(BUILD)/resp.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/ingestd.cpp $(BUILD)/client.o $(BUILD)/resp.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/preflight: apps/preflight.cpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/preflight.cpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/bench_rtt: apps/bench_rtt.cpp apps/feed.hpp $(BUILD)/client.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/bench_rtt.cpp $(BUILD)/client.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/bench_order: apps/bench_order.cpp include/kalshi/wire.hpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/bench_order.cpp $(BUILD)/client.o $(BUILD)/env.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

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

$(BUILD)/tradingd_tsan: apps/tradingd.cpp apps/feed.hpp src/client.cpp src/resp.cpp src/strategies.cpp src/env.cpp $(BUILD)/simdjson.o | $(BUILD)
	$(CXX) -std=c++23 -O1 -g -fsanitize=thread \
	    -Iinclude -Iapps -I$(SIMDJSON_DIR) $(OPENSSL_INC) \
	    apps/tradingd.cpp src/client.cpp src/resp.cpp src/strategies.cpp src/env.cpp \
	    $(BUILD)/simdjson.o -o $@ $(LDLIBS)

tsan: $(BUILD)/test_integration_tsan $(BUILD)/test_ring_tsan $(BUILD)/tradingd_tsan

# ASan+UBSan builds of the pure integer-heavy tests (fixedpoint parsers +
# orderbook delta math are where overflow/off-by-one hide). Pure C++ so fully
# instrumented. SAN_TESTS grows as phases land.
SAN_SRCS := tests/test_fixedpoint.cpp tests/test_ids.cpp tests/test_bus.cpp \
            tests/test_orderbook.cpp tests/test_sid_stream.cpp
SANFLAGS := -std=c++23 -O1 -g -fsanitize=address,undefined \
            -fno-omit-frame-pointer -Iinclude

san: | $(BUILD)
	@set -e; for src in $(SAN_SRCS); do \
	  bin=$(BUILD)/$$(basename $$src .cpp)_san; \
	  echo "  ASAN/UBSAN $$src"; \
	  $(CXX) $(SANFLAGS) $$src -o $$bin; \
	  ASAN_OPTIONS=detect_leaks=0 UBSAN_OPTIONS=halt_on_error=1:print_stacktrace=1 $$bin >/dev/null; \
	done; echo "  sanitizers clean"

# Build + run every pure unit test.
check: $(PURE_TESTS)
	@set -e; for t in $(PURE_TESTS); do echo "== $$t"; $$t | tail -1; done

test: $(BUILD)/test_signing
	$(BUILD)/test_signing

clean:
	rm -rf $(BUILD)

.PHONY: all tsan test clean
