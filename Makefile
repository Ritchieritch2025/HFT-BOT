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

BINS := $(BUILD)/kalshi_example $(BUILD)/test_signing $(BUILD)/test_integration \
        $(BUILD)/test_resp $(BUILD)/test_ring $(BUILD)/ingestd $(BUILD)/tradingd \
        $(BUILD)/preflight $(BUILD)/bench_rtt

all: $(BINS)

$(BUILD):
	mkdir -p $(BUILD)

$(BUILD)/client.o: src/client.cpp include/kalshi/client.hpp | $(BUILD)
	$(CXX) $(CXXFLAGS) -c $< -o $@

$(BUILD)/resp.o: src/resp.cpp include/kalshi/resp.hpp | $(BUILD)
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

# --- the trading engine (hot path) and the tape recorder (cold path) ---

$(BUILD)/tradingd: apps/tradingd.cpp apps/feed.hpp apps/daemon_util.hpp \
                   include/kalshi/ring.hpp $(BUILD)/client.o $(BUILD)/resp.o \
                   $(BUILD)/strategies.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/tradingd.cpp $(BUILD)/client.o $(BUILD)/resp.o \
	    $(BUILD)/strategies.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/ingestd: apps/ingestd.cpp apps/feed.hpp $(BUILD)/client.o $(BUILD)/resp.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/ingestd.cpp $(BUILD)/client.o $(BUILD)/resp.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/preflight: apps/preflight.cpp $(BUILD)/client.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/preflight.cpp $(BUILD)/client.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

$(BUILD)/bench_rtt: apps/bench_rtt.cpp apps/feed.hpp $(BUILD)/client.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) apps/bench_rtt.cpp $(BUILD)/client.o $(BUILD)/simdjson.o -o $@ $(LDLIBS)

# ThreadSanitizer builds. Note: libcrypto/libcurl are not TSan-instrumented,
# so these validate our ring/doorbell/pool call sites — not OpenSSL internals.
# test_ring_tsan is pure C++ and fully instrumented.
$(BUILD)/test_integration_tsan: tests/test_integration.cpp src/client.cpp | $(BUILD)
	$(CXX) -std=c++23 -O1 -g -fsanitize=thread \
	    -Iinclude $(OPENSSL_INC) \
	    tests/test_integration.cpp src/client.cpp -o $@ $(LDLIBS)

$(BUILD)/test_ring_tsan: tests/test_ring.cpp include/kalshi/ring.hpp | $(BUILD)
	$(CXX) -std=c++23 -O1 -g -fsanitize=thread -Iinclude tests/test_ring.cpp -o $@

$(BUILD)/tradingd_tsan: apps/tradingd.cpp apps/feed.hpp src/client.cpp src/resp.cpp src/strategies.cpp $(BUILD)/simdjson.o | $(BUILD)
	$(CXX) -std=c++23 -O1 -g -fsanitize=thread \
	    -Iinclude -Iapps -I$(SIMDJSON_DIR) $(OPENSSL_INC) \
	    apps/tradingd.cpp src/client.cpp src/resp.cpp src/strategies.cpp \
	    $(BUILD)/simdjson.o -o $@ $(LDLIBS)

tsan: $(BUILD)/test_integration_tsan $(BUILD)/test_ring_tsan $(BUILD)/tradingd_tsan

test: $(BUILD)/test_signing
	$(BUILD)/test_signing

clean:
	rm -rf $(BUILD)

.PHONY: all tsan test clean
