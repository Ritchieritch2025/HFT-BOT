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
        $(BUILD)/test_resp $(BUILD)/ingestd $(BUILD)/stratd $(BUILD)/execd

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

# --- pipeline daemons: Ingestion / Processing / Execution ---

$(BUILD)/ingestd: apps/ingestd.cpp $(BUILD)/client.o $(BUILD)/resp.o $(BUILD)/simdjson.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

$(BUILD)/stratd: apps/stratd.cpp $(BUILD)/resp.o $(BUILD)/strategies.o
	$(CXX) $(CXXFLAGS) $^ -o $@

$(BUILD)/execd: apps/execd.cpp $(BUILD)/client.o $(BUILD)/resp.o
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDLIBS)

# ThreadSanitizer build of the integration test. Note: libcrypto/libcurl are
# not TSan-instrumented, so this validates our pool/signing call sites and
# anything crossing them — not OpenSSL's internals.
$(BUILD)/test_integration_tsan: tests/test_integration.cpp src/client.cpp | $(BUILD)
	$(CXX) -std=c++23 -O1 -g -fsanitize=thread \
	    -Iinclude $(OPENSSL_INC) \
	    tests/test_integration.cpp src/client.cpp -o $@ $(LDLIBS)

tsan: $(BUILD)/test_integration_tsan

test: $(BUILD)/test_signing
	$(BUILD)/test_signing

clean:
	rm -rf $(BUILD)

.PHONY: all tsan test clean
