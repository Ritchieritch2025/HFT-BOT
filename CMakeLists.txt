cmake_minimum_required(VERSION 3.20)
project(kalshi_client LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# std::expected needs GCC 12+ / Clang 16+ (enforced by a #error in client.hpp).

# The vendored OpenSSL is a macOS-arm64 dev build; only prefer it on Apple.
# Linux deploy hosts use system OpenSSL 3.x + libcurl dev packages.
if(APPLE AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/third_party/openssl"
   AND NOT DEFINED OPENSSL_ROOT_DIR)
  set(OPENSSL_ROOT_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party/openssl")
  set(OPENSSL_USE_STATIC_LIBS TRUE)
endif()
find_package(OpenSSL REQUIRED)
find_package(CURL REQUIRED)
find_package(Threads REQUIRED)

# Core library: Kalshi transport/gateway + the source-agnostic backbone
# (fixedpoint/timestamp/ids/bus/storage are header-only under include/trading/).
# NOTE: the WS transport (src/ix_transport.cpp, apps/ws_smoke.cpp, apps/ws_shadow.cpp)
# links the vendored ixwebsocket static lib and is built via the Makefile.
# src/ws_client.cpp is transport-independent (IWebSocketTransport) and lives in the
# core lib here.
add_library(kalshi STATIC
  src/client.cpp
  src/resp.cpp
  src/strategies.cpp
  src/env.cpp
  src/rest_api.cpp
  src/limits.cpp
  src/request_spec.cpp
  src/request_executor.cpp
  src/storage.cpp
  src/gateway.cpp
  src/ws_client.cpp
  third_party/simdjson/simdjson.cpp
)
target_include_directories(kalshi PUBLIC include third_party/simdjson)
target_link_libraries(kalshi PUBLIC OpenSSL::Crypto CURL::libcurl Threads::Threads)
target_compile_options(kalshi PRIVATE -Wall -Wextra)

add_executable(kalshi_example examples/kalshi_example.cpp)
target_link_libraries(kalshi_example PRIVATE kalshi)

# tradingd: the single-process trading engine. ingestd: cold-path tape recorder.
# bench_orderbook: pure microbenchmark.
foreach(daemon tradingd ingestd)
  add_executable(${daemon} apps/${daemon}.cpp)
  target_include_directories(${daemon} PRIVATE apps)
  target_link_libraries(${daemon} PRIVATE kalshi)
endforeach()
add_executable(bench_orderbook apps/bench_orderbook.cpp)
target_link_libraries(bench_orderbook PRIVATE kalshi)

# WS-path decode benchmark + JSON/NDJSON fuzzer (transport-independent — no
# ixwebsocket, so CMake builds them; the ixwebsocket-linked ws_smoke/ws_shadow
# stay Makefile-only per the note above).
add_executable(bench_ws_decode apps/bench_ws_decode.cpp)
target_link_libraries(bench_ws_decode PRIVATE kalshi)
add_executable(fuzz_decode tests/fuzz_decode.cpp)
target_link_libraries(fuzz_decode PRIVATE kalshi)

enable_testing()
# Tests that need scratch/mock args are registered but driven by the shell
# runners (run_rest_api.sh); the pure/self-contained ones run directly.
foreach(t test_signing test_integration test_resp test_ring test_fixedpoint
          test_ids test_env_safety test_bus test_orderbook test_sid_stream
          test_recovery test_storage test_recorder test_decode test_replay test_ws_client test_shadow
          test_readability test_rest_api test_account_limits test_endpoint_costs
          test_request_spec test_token_bucket test_backoff test_secret_redaction
          test_request_executor)
  add_executable(${t} tests/${t}.cpp)
  target_link_libraries(${t} PRIVATE kalshi)
endforeach()
# Fixture-driven (no network) — safe to run directly under CTest.
foreach(t signing ring fixedpoint ids env_safety bus orderbook sid_stream readability
          account_limits endpoint_costs request_spec token_bucket backoff secret_redaction)
  add_test(NAME ${t} COMMAND test_${t})
endforeach()
add_test(NAME storage COMMAND test_storage ${CMAKE_CURRENT_BINARY_DIR})
add_test(NAME shadow COMMAND test_shadow ${CMAKE_CURRENT_BINARY_DIR})
