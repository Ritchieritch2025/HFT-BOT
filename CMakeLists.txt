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

add_library(kalshi STATIC
  src/client.cpp
  src/resp.cpp
  src/strategies.cpp
  third_party/simdjson/simdjson.cpp
)
target_include_directories(kalshi PUBLIC include third_party/simdjson)
target_link_libraries(kalshi PUBLIC OpenSSL::Crypto CURL::libcurl Threads::Threads)
target_compile_options(kalshi PRIVATE -Wall -Wextra)

add_executable(kalshi_example examples/kalshi_example.cpp)
target_link_libraries(kalshi_example PRIVATE kalshi)

# Pipeline daemons: Ingestion / Processing / Execution.
foreach(daemon ingestd stratd execd)
  add_executable(${daemon} apps/${daemon}.cpp)
  target_include_directories(${daemon} PRIVATE apps)
  target_link_libraries(${daemon} PRIVATE kalshi)
endforeach()

enable_testing()
foreach(t test_signing test_integration test_resp)
  add_executable(${t} tests/${t}.cpp)
  target_link_libraries(${t} PRIVATE kalshi)
endforeach()
add_test(NAME signing COMMAND test_signing)
