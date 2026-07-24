// Strict causal-latency probe for the PnL spine.
//
// Default invocation is a credential-free, socket-free dry run.  The only
// mutating mode is --execute-place-cancel and it requires both the normal
// environment safety gate and a current, externally SHA-pinned authority.
// The executable hashes its own loaded binary and root-pinned deployment
// controls; producer/environment/host hashes are never accepted as CLI facts.
//
// IOC_EXIT is intentionally NOT implemented as a write path.  The
// --ioc-exit-preflight mode performs authenticated GETs only and reports that
// a separate current live authority and a separately reviewed executor are
// required.  --execute-ioc-exit is refused before credentials are loaded.

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/wire.hpp"
#include "simdjson.h"

#include <curl/curl.h>
#include <openssl/evp.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <grp.h>
#include <iterator>
#include <limits>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <vector>

#if defined(__linux__)
#include <sys/prctl.h>
#endif

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#endif

using namespace kalshi;
using kalshi::daemon::steady_now_ns;

namespace {

constexpr std::string_view kAuthoritySchema =
    "pnl-spine-latency-probe-authority-v2";
constexpr std::string_view kPrivateTraceSchema =
    "pnl-spine-private-causal-latency-trace-v1";
constexpr std::string_view kProducerConfigSchema =
    "pnl-spine-latency-probe-config-v2";
constexpr std::string_view kEnvironmentReceiptSchema =
    "pnl-spine-execution-environment-receipt-v1";
constexpr std::string_view kExecutionHostReceiptSchema =
    "pnl-spine-execution-host-receipt-v1";
constexpr std::string_view kClockQualityReceiptSchema =
    "pnl-spine-clock-quality-receipt-v1";
constexpr std::string_view kEventOrderPath =
    "/portfolio/events/orders";
constexpr std::string_view kGetOrderPath = "/portfolio/orders";
constexpr std::int64_t kQuantityE4 = 10'000;
constexpr std::int64_t kPriceCapE4 = 100;
constexpr std::int64_t kMaximumCashLossE6 = 10'000;
constexpr std::int64_t kMaximumFeeE6 = 10'000;
constexpr std::int64_t kMaximumClockReceiptAgeMs = 5 * 60 * 1'000;
constexpr std::int64_t kMaximumClockFutureSkewMs = 5 * 1'000;
constexpr std::int64_t kMaximumClockErrorNs = 10 * 1'000'000;
constexpr std::string_view kProbeClockId =
    "STD_STEADY_CLOCK:probe-process";
// GetOrders has no direct client_order_id query, a bounded list query can time
// out or be eventually consistent, and this binary has neither an audited
// account-level trading block nor a position-flattening IOC transmitter.
// Therefore every possible ambiguous create outcome cannot yet be made safe.
// This compile-time release gate must remain false until all of those controls
// are implemented and independently audited.
constexpr bool kAmbiguousPlaceRecoveryProven = false;

struct Options {
  bool execute_place_cancel = false;
  bool ioc_exit_preflight = false;
  bool execute_ioc_exit = false;
  bool self_test_v2 = false;
  bool self_test_authority_deadline = false;
  bool self_test_network_environment = false;
  bool self_test_ambiguous_post_recovery = false;
  std::string ticker;
  std::string authority_file;
  std::string expected_authority_sha256;
  std::string producer_config_file;
  std::string environment_receipt_file;
  std::string execution_host_receipt_file;
  std::string clock_quality_receipt_file;
  std::string receipt_id;
  std::string output;
  std::string consumption_ledger;
  std::string terminal_consumption_receipt;
};

struct Authority {
  std::int64_t issued_at_unix_s = 0;
  std::int64_t expires_at_unix_s = 0;
  bool allow_place_cancel = false;
  bool allow_ioc_exit = false;
  std::string nonce_sha256;
  std::string transaction_id;
  std::uint64_t accepted_at_steady_ns = 0;
  std::uint64_t monotonic_deadline_ns = 0;
};

struct RuntimeFacts {
  std::string producer_code_sha256;
  std::string producer_config_sha256;
  std::string environment_fingerprint_sha256;
  std::string execution_host_fingerprint_sha256;
  std::string clock_quality_receipt_sha256;
  std::string hostname;
  std::string instance_id;
  std::string machine_id_sha256;
  std::string ca_bundle_path;
  std::string ca_bundle_sha256;
  uid_t execution_uid = 0;
  gid_t execution_gid = 0;
};

struct ProducerDeploymentConfig {
  std::string ca_bundle_path;
  std::string ca_bundle_sha256;
  uid_t execution_uid = 0;
  gid_t execution_gid = 0;
};

std::optional<std::filesystem::path> secure_new_output_path(
    const std::string& raw_path);

class OutputReservation {
 public:
  explicit OutputReservation(const std::string& raw_path) {
    const auto path = secure_new_output_path(raw_path);
    if (!path) return;
    path_ = *path;
    int directory_flags = O_RDONLY;
#ifdef O_DIRECTORY
    directory_flags |= O_DIRECTORY;
#endif
#ifdef O_NOFOLLOW
    directory_flags |= O_NOFOLLOW;
#endif
    directory_fd_ = ::open(path_.parent_path().c_str(), directory_flags);
    if (directory_fd_ < 0) return;
    int create_flags = O_WRONLY | O_CREAT | O_EXCL;
#ifdef O_NOFOLLOW
    create_flags |= O_NOFOLLOW;
#endif
    fd_ = ::openat(
        directory_fd_, path_.filename().c_str(), create_flags, 0400);
    if (fd_ < 0) return;
    struct stat info {};
    if (::fstat(fd_, &info) != 0 || !S_ISREG(info.st_mode) ||
        info.st_nlink != 1 || info.st_uid != 0) {
      ::close(fd_);
      fd_ = -1;
    }
  }

  OutputReservation(const OutputReservation&) = delete;
  OutputReservation& operator=(const OutputReservation&) = delete;

  ~OutputReservation() {
    if (fd_ >= 0) ::close(fd_);
    if (directory_fd_ >= 0) ::close(directory_fd_);
  }

  [[nodiscard]] bool valid() const noexcept { return fd_ >= 0; }

  bool finish(std::string_view bytes) {
    if (fd_ < 0) return false;
    std::size_t offset = 0;
    while (offset < bytes.size()) {
      const ssize_t written =
          ::write(fd_, bytes.data() + offset, bytes.size() - offset);
      if (written <= 0) return false;
      offset += static_cast<std::size_t>(written);
    }
    if (::fsync(fd_) != 0 || ::close(fd_) != 0) {
      fd_ = -1;
      return false;
    }
    fd_ = -1;
    if (::fsync(directory_fd_) != 0) return false;
    return true;
  }

 private:
  std::filesystem::path path_;
  int fd_ = -1;
  int directory_fd_ = -1;
};

struct PositionSnapshot {
  std::int64_t position_e4 = 0;
  std::string response_sha256;
};

struct OrderSnapshot {
  std::int64_t requested_e4 = 0;
  std::int64_t filled_e4 = 0;
  std::int64_t remaining_e4 = 0;
  std::string response_sha256;
};

struct CreateAck {
  std::string order_id;
  std::int64_t fill_count_e4 = 0;
  std::int64_t remaining_count_e4 = 0;
  std::int64_t matching_engine_ts_ms = 0;
  std::optional<std::int64_t> average_fee_paid_e6;
  std::optional<std::int64_t> average_fill_price_e4;
};

struct CancelAck {
  std::string order_id;
  std::int64_t reduced_by_e4 = 0;
  std::int64_t matching_engine_ts_ms = 0;
};

struct CausalSample {
  std::string path;
  std::string action_semantics;
  std::uint64_t decision_ns = 0;
  std::uint64_t sent_ns = 0;
  std::uint64_t acknowledged_ns = 0;
  std::uint64_t effective_ns = 0;
  std::string order_ref_sha256;
  std::string request_sha256;
  std::string response_sha256;
  std::string source_event_sha256;
  long http_status = 0;
  std::string live_authority_sha256;
  std::int64_t matching_engine_ts_ms = 0;
  std::optional<std::int64_t> average_fee_paid_e6;
  std::optional<std::int64_t> average_fill_price_e4;
  std::string book_side;
  std::string clock_quality_receipt_sha256;
  std::string environment_fingerprint_sha256;
  std::string execution_host_fingerprint_sha256;
  bool request_reduce_only = false;
  std::int64_t requested_price_e4 = 0;
  std::int64_t subaccount = 0;
  std::string ticker_sha256;
  std::string time_in_force;
  std::int64_t requested_e4 = 0;
  std::int64_t filled_e4 = 0;
  std::int64_t canceled_e4 = 0;
  std::int64_t remaining_e4 = 0;
  std::int64_t position_before_e4 = 0;
  std::int64_t position_after_e4 = 0;
  std::string order_status;
};

bool is_sha256(std::string_view value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](unsigned char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}

std::string sha256_hex(std::string_view value) {
  std::array<unsigned char, EVP_MAX_MD_SIZE> digest{};
  unsigned int length = 0;
  EVP_MD_CTX* context = EVP_MD_CTX_new();
  if (!context ||
      EVP_DigestInit_ex(context, EVP_sha256(), nullptr) != 1 ||
      EVP_DigestUpdate(context, value.data(), value.size()) != 1 ||
      EVP_DigestFinal_ex(context, digest.data(), &length) != 1) {
    if (context) EVP_MD_CTX_free(context);
    throw std::runtime_error("SHA-256 computation failed");
  }
  EVP_MD_CTX_free(context);
  static constexpr char hex[] = "0123456789abcdef";
  std::string result;
  result.resize(length * 2);
  for (unsigned int index = 0; index < length; ++index) {
    result[index * 2] = hex[digest[index] >> 4];
    result[index * 2 + 1] = hex[digest[index] & 0x0f];
  }
  return result;
}

std::optional<std::filesystem::path> strict_absolute_path(
    const std::string& raw_path) {
  if (raw_path.empty() || raw_path.find('\0') != std::string::npos)
    return std::nullopt;
  const std::filesystem::path path(raw_path);
  if (!path.is_absolute() || path.filename().empty() ||
      path != path.lexically_normal())
    return std::nullopt;
  return path;
}

bool process_in_group(gid_t group) {
  if (::getegid() == group) return true;
  const int count = ::getgroups(0, nullptr);
  if (count <= 0) return false;
  std::vector<gid_t> groups(static_cast<std::size_t>(count));
  if (::getgroups(count, groups.data()) != count) return false;
  return std::find(groups.begin(), groups.end(), group) != groups.end();
}

bool root_owned_components(
    const std::filesystem::path& path, bool include_leaf,
    bool allow_direct_parent_group_write) {
  std::filesystem::path current(path.root_path());
  const auto end_offset = include_leaf ? 0U : 1U;
  const auto components =
      static_cast<std::size_t>(std::distance(path.begin(), path.end()));
  std::size_t index = 0;
  for (const auto& component : path) {
    ++index;
    if (component == path.root_path()) continue;
    if (index > components - end_offset) break;
    current /= component;
    struct stat info {};
    if (::lstat(current.c_str(), &info) != 0 ||
        S_ISLNK(info.st_mode) || info.st_uid != 0)
      return false;
    const bool direct_parent =
        !include_leaf && index == components - end_offset;
    if (direct_parent && allow_direct_parent_group_write) {
      if (!S_ISDIR(info.st_mode) || (info.st_mode & S_IWOTH) != 0 ||
          !process_in_group(info.st_gid) ||
          (info.st_mode & S_ISVTX) == 0 ||
          (info.st_mode & (S_IWGRP | S_IXGRP)) !=
              (S_IWGRP | S_IXGRP))
        return false;
    } else if (S_ISDIR(info.st_mode)) {
      if ((info.st_mode & (S_IWGRP | S_IWOTH)) != 0) return false;
    } else if (index != components) {
      return false;
    }
  }
  return true;
}

std::optional<std::filesystem::path> secure_new_output_path(
    const std::string& raw_path) {
  const auto path = strict_absolute_path(raw_path);
  if (!path || !root_owned_components(
                   *path, false, false)) {
    return std::nullopt;
  }
  struct stat existing {};
  if (::lstat(path->c_str(), &existing) == 0 || errno != ENOENT)
    return std::nullopt;
  return path;
}

std::optional<std::string> read_root_control_file(
    const std::string& raw_path, std::size_t maximum_bytes = 64 * 1024) {
  const auto path = strict_absolute_path(raw_path);
  if (!path || !root_owned_components(*path, true, false))
    return std::nullopt;
  int flags = O_RDONLY;
#ifdef O_NOFOLLOW
  flags |= O_NOFOLLOW;
#endif
  const int fd = ::open(path->c_str(), flags);
  if (fd < 0) return std::nullopt;
  struct stat before {};
  if (::fstat(fd, &before) != 0 || !S_ISREG(before.st_mode) ||
      before.st_uid != 0 || (before.st_mode & 0222) != 0 ||
      before.st_size <= 0 ||
      static_cast<std::uint64_t>(before.st_size) > maximum_bytes) {
    ::close(fd);
    return std::nullopt;
  }
  std::string bytes;
  bytes.reserve(static_cast<std::size_t>(before.st_size));
  std::array<char, 16 * 1024> buffer{};
  while (true) {
    const ssize_t size = ::read(fd, buffer.data(), buffer.size());
    if (size < 0) {
      ::close(fd);
      return std::nullopt;
    }
    if (size == 0) break;
    bytes.append(buffer.data(), static_cast<std::size_t>(size));
    if (bytes.size() > maximum_bytes) {
      ::close(fd);
      return std::nullopt;
    }
  }
  struct stat after {};
  const bool stable =
      ::fstat(fd, &after) == 0 && before.st_dev == after.st_dev &&
      before.st_ino == after.st_ino && before.st_size == after.st_size &&
      before.st_mtime == after.st_mtime;
  ::close(fd);
  if (!stable || bytes.empty()) return std::nullopt;
  return bytes;
}

std::optional<std::string> hash_root_read_only_file(
    const std::filesystem::path& path) {
  if (!path.is_absolute() || path != path.lexically_normal() ||
      !root_owned_components(path, true, false))
    return std::nullopt;
  int flags = O_RDONLY;
#ifdef O_NOFOLLOW
  flags |= O_NOFOLLOW;
#endif
  const int fd = ::open(path.c_str(), flags);
  if (fd < 0) return std::nullopt;
  struct stat before {};
  if (::fstat(fd, &before) != 0 || !S_ISREG(before.st_mode) ||
      before.st_uid != 0 || (before.st_mode & 0222) != 0) {
    ::close(fd);
    return std::nullopt;
  }
  EVP_MD_CTX* context = EVP_MD_CTX_new();
  if (!context ||
      EVP_DigestInit_ex(context, EVP_sha256(), nullptr) != 1) {
    if (context) EVP_MD_CTX_free(context);
    ::close(fd);
    return std::nullopt;
  }
  std::array<unsigned char, 64 * 1024> buffer{};
  while (true) {
    const ssize_t size = ::read(fd, buffer.data(), buffer.size());
    if (size < 0 ||
        (size > 0 &&
         EVP_DigestUpdate(
             context, buffer.data(), static_cast<std::size_t>(size)) != 1)) {
      EVP_MD_CTX_free(context);
      ::close(fd);
      return std::nullopt;
    }
    if (size == 0) break;
  }
  struct stat after {};
  std::array<unsigned char, EVP_MAX_MD_SIZE> digest{};
  unsigned int digest_size = 0;
  const bool ok =
      ::fstat(fd, &after) == 0 && before.st_dev == after.st_dev &&
      before.st_ino == after.st_ino && before.st_size == after.st_size &&
      before.st_mtime == after.st_mtime &&
      EVP_DigestFinal_ex(context, digest.data(), &digest_size) == 1;
  EVP_MD_CTX_free(context);
  ::close(fd);
  if (!ok) return std::nullopt;
  static constexpr char hex[] = "0123456789abcdef";
  std::string result(digest_size * 2, '0');
  for (unsigned int index = 0; index < digest_size; ++index) {
    result[index * 2] = hex[digest[index] >> 4];
    result[index * 2 + 1] = hex[digest[index] & 0x0f];
  }
  return result;
}

std::optional<std::filesystem::path> running_executable_path() {
#if defined(__linux__)
  std::array<char, 4096> buffer{};
  const ssize_t size =
      ::readlink("/proc/self/exe", buffer.data(), buffer.size() - 1);
  if (size <= 0 || static_cast<std::size_t>(size) >= buffer.size() - 1)
    return std::nullopt;
  buffer[static_cast<std::size_t>(size)] = '\0';
  const std::filesystem::path path(buffer.data());
#elif defined(__APPLE__)
  std::uint32_t size = 0;
  (void)::_NSGetExecutablePath(nullptr, &size);
  if (size == 0 || size > 64 * 1024) return std::nullopt;
  std::vector<char> buffer(size);
  if (::_NSGetExecutablePath(buffer.data(), &size) != 0)
    return std::nullopt;
  std::array<char, 64 * 1024> resolved{};
  if (!::realpath(buffer.data(), resolved.data())) return std::nullopt;
  const std::filesystem::path path(resolved.data());
#else
  return std::nullopt;
#endif
  if (!path.is_absolute()) return std::nullopt;
  return path.lexically_normal();
}

std::string trim_ascii(std::string value) {
  while (!value.empty() &&
         (value.back() == '\0' ||
          std::isspace(static_cast<unsigned char>(value.back()))))
    value.pop_back();
  std::size_t first = 0;
  while (first < value.size() &&
         (value[first] == '\0' ||
          std::isspace(static_cast<unsigned char>(value[first]))))
    ++first;
  return value.substr(first);
}

std::optional<std::int64_t> decimal_e4(simdjson::dom::element element) {
  std::int64_t integer = 0;
  if (element.get(integer) == simdjson::SUCCESS) {
    if (integer > std::numeric_limits<std::int64_t>::max() / 10'000 ||
        integer < std::numeric_limits<std::int64_t>::min() / 10'000)
      return std::nullopt;
    return integer * 10'000;
  }
  std::uint64_t unsigned_integer = 0;
  if (element.get(unsigned_integer) == simdjson::SUCCESS) {
    if (unsigned_integer >
        static_cast<std::uint64_t>(
            std::numeric_limits<std::int64_t>::max() / 10'000))
      return std::nullopt;
    return static_cast<std::int64_t>(unsigned_integer * 10'000);
  }
  std::string_view text;
  if (element.get(text) != simdjson::SUCCESS || text.empty())
    return std::nullopt;
  bool negative = false;
  std::size_t index = 0;
  if (text[index] == '-') {
    negative = true;
    ++index;
  }
  if (index == text.size()) return std::nullopt;
  std::int64_t whole = 0;
  bool saw_digit = false;
  while (index < text.size() && std::isdigit(
      static_cast<unsigned char>(text[index]))) {
    saw_digit = true;
    if (whole > (std::numeric_limits<std::int64_t>::max() - 9) / 10)
      return std::nullopt;
    whole = whole * 10 + (text[index++] - '0');
  }
  if (!saw_digit) return std::nullopt;
  std::int64_t fraction = 0;
  int fraction_digits = 0;
  if (index < text.size() && text[index] == '.') {
    ++index;
    while (index < text.size() && std::isdigit(
        static_cast<unsigned char>(text[index]))) {
      if (fraction_digits >= 4) return std::nullopt;
      fraction = fraction * 10 + (text[index++] - '0');
      ++fraction_digits;
    }
  }
  if (index != text.size()) return std::nullopt;
  while (fraction_digits++ < 4) fraction *= 10;
  if (whole > (std::numeric_limits<std::int64_t>::max() - fraction) /
                  10'000)
    return std::nullopt;
  const std::int64_t result = whole * 10'000 + fraction;
  return negative ? -result : result;
}

std::optional<std::int64_t> decimal_e6(simdjson::dom::element element) {
  std::string_view text;
  if (element.get(text) != simdjson::SUCCESS || text.empty())
    return std::nullopt;
  bool negative = false;
  std::size_t index = 0;
  if (text[index] == '-') {
    negative = true;
    ++index;
  }
  if (index == text.size()) return std::nullopt;
  std::int64_t whole = 0;
  bool saw_digit = false;
  while (index < text.size() && std::isdigit(
      static_cast<unsigned char>(text[index]))) {
    saw_digit = true;
    if (whole > (std::numeric_limits<std::int64_t>::max() - 9) / 10)
      return std::nullopt;
    whole = whole * 10 + (text[index++] - '0');
  }
  if (!saw_digit) return std::nullopt;
  std::int64_t fraction = 0;
  int fraction_digits = 0;
  if (index < text.size() && text[index] == '.') {
    ++index;
    while (index < text.size() && std::isdigit(
        static_cast<unsigned char>(text[index]))) {
      if (fraction_digits >= 6) return std::nullopt;
      fraction = fraction * 10 + (text[index++] - '0');
      ++fraction_digits;
    }
  }
  if (index != text.size()) return std::nullopt;
  while (fraction_digits++ < 6) fraction *= 10;
  if (whole > (std::numeric_limits<std::int64_t>::max() - fraction) /
                  1'000'000)
    return std::nullopt;
  const std::int64_t result = whole * 1'000'000 + fraction;
  return negative ? -result : result;
}

simdjson::dom::element order_object(simdjson::dom::element root) {
  simdjson::dom::element nested;
  if (root["order"].get(nested) == simdjson::SUCCESS) return nested;
  return root;
}

std::optional<std::string> string_field(
    simdjson::dom::element object,
    std::initializer_list<const char*> names) {
  for (const char* name : names) {
    std::string_view value;
    if (object[name].get(value) == simdjson::SUCCESS && !value.empty())
      return std::string(value);
  }
  return std::nullopt;
}

std::optional<std::int64_t> count_field(
    simdjson::dom::element object,
    std::initializer_list<const char*> fixed_names,
    std::initializer_list<const char*> integer_names) {
  for (const char* name : fixed_names) {
    simdjson::dom::element value;
    if (object[name].get(value) == simdjson::SUCCESS) {
      auto parsed = decimal_e4(value);
      if (parsed) return parsed;
    }
  }
  for (const char* name : integer_names) {
    simdjson::dom::element value;
    if (object[name].get(value) == simdjson::SUCCESS) {
      auto parsed = decimal_e4(value);
      if (parsed) return parsed;
    }
  }
  return std::nullopt;
}

bool has_exact_keys(
    simdjson::dom::element root,
    const std::set<std::string>& expected) {
  simdjson::dom::object object;
  if (root.get(object) != simdjson::SUCCESS) return false;
  std::set<std::string> actual;
  std::size_t count = 0;
  for (auto field : object) {
    ++count;
    actual.emplace(std::string(field.key));
  }
  return actual == expected && count == actual.size();
}

std::optional<std::string> local_hostname() {
  std::array<char, 256> buffer{};
  if (::gethostname(buffer.data(), buffer.size() - 1) != 0)
    return std::nullopt;
  buffer.back() = '\0';
  const std::string hostname(buffer.data());
  if (hostname.empty() || hostname.size() > 253 ||
      !std::all_of(
          hostname.begin(), hostname.end(), [](unsigned char character) {
            return std::isalnum(character) || character == '-' ||
                   character == '.';
          }))
    return std::nullopt;
  return hostname;
}

std::optional<std::string> local_machine_id_sha256() {
  const auto bytes = read_root_control_file("/etc/machine-id", 1024);
  if (!bytes) return std::nullopt;
  const std::string machine_id = trim_ascii(*bytes);
  if (machine_id.size() < 16 || machine_id.size() > 128 ||
      !std::all_of(
          machine_id.begin(), machine_id.end(), [](unsigned char character) {
            return std::isxdigit(character) || character == '-';
          }))
    return std::nullopt;
  return sha256_hex(machine_id);
}

std::optional<std::string> local_instance_id() {
  constexpr std::array<std::string_view, 2> identity_paths = {
      "/sys/devices/virtual/dmi/id/board_asset_tag",
      "/sys/firmware/devicetree/base/serial-number",
  };
  for (const auto path : identity_paths) {
    const auto bytes = read_root_control_file(std::string(path), 1024);
    if (!bytes) continue;
    const std::string instance_id = trim_ascii(*bytes);
    if (instance_id.size() < 10 || instance_id.size() > 64 ||
        instance_id.rfind("i-", 0) != 0 ||
        !std::all_of(
            instance_id.begin() + 2, instance_id.end(),
            [](unsigned char character) {
              return std::isdigit(character) ||
                     (character >= 'a' && character <= 'f');
            }))
      continue;
    return instance_id;
  }
  return std::nullopt;
}

std::optional<std::string> default_curl_ca_path() {
  CURL* handle = ::curl_easy_init();
  if (!handle) return std::nullopt;
  char* ca_path = nullptr;
  const CURLcode result =
      ::curl_easy_getinfo(handle, CURLINFO_CAINFO, &ca_path);
  std::optional<std::string> value;
  if (result == CURLE_OK && ca_path && *ca_path)
    value = std::string(ca_path);
  ::curl_easy_cleanup(handle);
  return value;
}

std::optional<ProducerDeploymentConfig> validate_producer_config(
    std::string_view bytes) {
  try {
    simdjson::dom::parser parser;
    auto result = parser.parse(bytes);
    if (result.error()) return std::nullopt;
    auto root = result.value();
    const std::set<std::string> expected = {
        "ca_bundle_path",
        "ca_bundle_sha256",
        "execution_gid",
        "execution_uid",
        "max_cash_loss_e6",
        "max_fee_e6",
        "place_post_only",
        "place_side",
        "place_time_in_force",
        "price_cap_e4",
        "quantity_e4",
        "schema_version",
    };
    if (!has_exact_keys(root, expected)) return std::nullopt;
    const auto schema = string_field(root, {"schema_version"});
    const auto ca_bundle_path = string_field(root, {"ca_bundle_path"});
    const auto ca_bundle_sha =
        string_field(root, {"ca_bundle_sha256"});
    const auto place_side = string_field(root, {"place_side"});
    const auto time_in_force =
        string_field(root, {"place_time_in_force"});
    std::int64_t quantity = 0;
    std::int64_t price_cap = 0;
    std::int64_t maximum_loss = 0;
    std::int64_t maximum_fee = 0;
    std::int64_t execution_uid = 0;
    std::int64_t execution_gid = 0;
    bool post_only = false;
    if (!schema || *schema != kProducerConfigSchema || !ca_bundle_path ||
        !strict_absolute_path(*ca_bundle_path) || !ca_bundle_sha ||
        !is_sha256(*ca_bundle_sha) || !place_side ||
        *place_side != "BID" || !time_in_force ||
        *time_in_force != "GOOD_TILL_CANCELED" ||
        root["quantity_e4"].get(quantity) != simdjson::SUCCESS ||
        quantity != kQuantityE4 ||
        root["price_cap_e4"].get(price_cap) != simdjson::SUCCESS ||
        price_cap != kPriceCapE4 ||
        root["max_cash_loss_e6"].get(maximum_loss) !=
            simdjson::SUCCESS ||
        maximum_loss != kMaximumCashLossE6 ||
        root["max_fee_e6"].get(maximum_fee) != simdjson::SUCCESS ||
        maximum_fee != kMaximumFeeE6 ||
        root["execution_uid"].get(execution_uid) != simdjson::SUCCESS ||
        root["execution_gid"].get(execution_gid) != simdjson::SUCCESS ||
        execution_uid <= 0 ||
        execution_uid > std::numeric_limits<uid_t>::max() ||
        execution_gid <= 0 ||
        execution_gid > std::numeric_limits<gid_t>::max() ||
        root["place_post_only"].get(post_only) != simdjson::SUCCESS ||
        !post_only)
      return std::nullopt;
    return ProducerDeploymentConfig{
        *ca_bundle_path,
        *ca_bundle_sha,
        static_cast<uid_t>(execution_uid),
        static_cast<gid_t>(execution_gid),
    };
  } catch (...) {
    return std::nullopt;
  }
}

bool validate_environment_receipt(
    std::string_view bytes, const RuntimeFacts& facts) {
  try {
    simdjson::dom::parser parser;
    auto result = parser.parse(bytes);
    if (result.error()) return false;
    auto root = result.value();
    const std::set<std::string> expected = {
        "hostname",
        "instance_id",
        "kalshi_environment",
        "kalshi_mode",
        "ca_bundle_sha256",
        "machine_id_sha256",
        "producer_code_sha256",
        "producer_config_sha256",
        "schema_version",
    };
    if (!has_exact_keys(root, expected)) return false;
    const auto schema = string_field(root, {"schema_version"});
    const auto hostname = string_field(root, {"hostname"});
    const auto instance_id = string_field(root, {"instance_id"});
    const auto machine_id = string_field(root, {"machine_id_sha256"});
    const auto code = string_field(root, {"producer_code_sha256"});
    const auto config = string_field(root, {"producer_config_sha256"});
    const auto environment = string_field(root, {"kalshi_environment"});
    const auto mode = string_field(root, {"kalshi_mode"});
    const auto ca_bundle = string_field(root, {"ca_bundle_sha256"});
    return schema && *schema == kEnvironmentReceiptSchema && hostname &&
           *hostname == facts.hostname && instance_id &&
           *instance_id == facts.instance_id && machine_id &&
           *machine_id == facts.machine_id_sha256 && code &&
           *code == facts.producer_code_sha256 && config &&
           *config == facts.producer_config_sha256 && environment &&
           *environment == "prod" && mode && *mode == "live" &&
           ca_bundle && *ca_bundle == facts.ca_bundle_sha256;
  } catch (...) {
    return false;
  }
}

bool validate_execution_host_receipt(
    std::string_view bytes, const RuntimeFacts& facts,
    std::string_view environment_sha256) {
  try {
    simdjson::dom::parser parser;
    auto result = parser.parse(bytes);
    if (result.error()) return false;
    auto root = result.value();
    const std::set<std::string> expected = {
        "captured_at_wall_utc_ms",
        "environment_fingerprint_sha256",
        "hostname",
        "instance_id",
        "machine_id_sha256",
        "schema_version",
    };
    if (!has_exact_keys(root, expected)) return false;
    const auto schema = string_field(root, {"schema_version"});
    const auto hostname = string_field(root, {"hostname"});
    const auto instance_id = string_field(root, {"instance_id"});
    const auto machine_id = string_field(root, {"machine_id_sha256"});
    const auto environment =
        string_field(root, {"environment_fingerprint_sha256"});
    std::int64_t captured_at = 0;
    const auto now_ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch())
            .count();
    return schema && *schema == kExecutionHostReceiptSchema && hostname &&
           *hostname == facts.hostname && instance_id &&
           *instance_id == facts.instance_id && machine_id &&
           *machine_id == facts.machine_id_sha256 && environment &&
           *environment == environment_sha256 &&
           root["captured_at_wall_utc_ms"].get(captured_at) ==
               simdjson::SUCCESS &&
           captured_at > 0 &&
           captured_at <= now_ms + kMaximumClockFutureSkewMs;
  } catch (...) {
    return false;
  }
}

bool validate_clock_quality_receipt(
    std::string_view bytes, const RuntimeFacts& facts) {
  try {
    simdjson::dom::parser parser;
    auto result = parser.parse(bytes);
    if (result.error()) return false;
    auto root = result.value();
    const std::set<std::string> expected = {
        "captured_at_wall_utc_ms",
        "clock_id",
        "hostname",
        "instance_id",
        "machine_id_sha256",
        "max_error_ns",
        "schema_version",
        "source",
        "synchronized",
    };
    if (!has_exact_keys(root, expected)) return false;
    const auto schema = string_field(root, {"schema_version"});
    const auto clock_id = string_field(root, {"clock_id"});
    const auto hostname = string_field(root, {"hostname"});
    const auto instance_id = string_field(root, {"instance_id"});
    const auto machine_id = string_field(root, {"machine_id_sha256"});
    const auto source = string_field(root, {"source"});
    std::int64_t captured_at = 0;
    std::int64_t maximum_error_ns = 0;
    bool synchronized = false;
    const auto now_ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch())
            .count();
    if (!schema || *schema != kClockQualityReceiptSchema || !clock_id ||
        *clock_id != kProbeClockId || !hostname ||
        *hostname != facts.hostname || !instance_id ||
        *instance_id != facts.instance_id || !machine_id ||
        *machine_id != facts.machine_id_sha256 || !source ||
        source->size() > 128 ||
        root["captured_at_wall_utc_ms"].get(captured_at) !=
            simdjson::SUCCESS ||
        root["max_error_ns"].get(maximum_error_ns) !=
            simdjson::SUCCESS ||
        root["synchronized"].get(synchronized) != simdjson::SUCCESS)
      return false;
    return synchronized && maximum_error_ns >= 0 &&
           maximum_error_ns <= kMaximumClockErrorNs &&
           captured_at <= now_ms + kMaximumClockFutureSkewMs &&
           now_ms - captured_at <= kMaximumClockReceiptAgeMs;
  } catch (...) {
    return false;
  }
}

std::optional<RuntimeFacts> collect_runtime_facts(
    const Options& options) {
  if (::geteuid() != 0 || ::getuid() != 0) return std::nullopt;
  const auto executable = running_executable_path();
  if (!executable) return std::nullopt;
  const auto code_sha = hash_root_read_only_file(*executable);
  const auto hostname = local_hostname();
  const auto machine_id_sha = local_machine_id_sha256();
  const auto instance_id = local_instance_id();
  if (!code_sha || !hostname || !machine_id_sha || !instance_id)
    return std::nullopt;

  const auto config =
      read_root_control_file(options.producer_config_file);
  if (!config) return std::nullopt;
  const auto deployment_config = validate_producer_config(*config);
  if (!deployment_config) return std::nullopt;
  const auto default_ca = default_curl_ca_path();
  if (!default_ca ||
      std::filesystem::path(*default_ca).lexically_normal() !=
          std::filesystem::path(deployment_config->ca_bundle_path))
    return std::nullopt;
  const auto ca_bundle_sha = hash_root_read_only_file(
      std::filesystem::path(deployment_config->ca_bundle_path));
  if (!ca_bundle_sha ||
      *ca_bundle_sha != deployment_config->ca_bundle_sha256)
    return std::nullopt;
  RuntimeFacts facts;
  facts.producer_code_sha256 = *code_sha;
  facts.producer_config_sha256 = sha256_hex(*config);
  facts.hostname = *hostname;
  facts.machine_id_sha256 = *machine_id_sha;
  facts.instance_id = *instance_id;
  facts.ca_bundle_path = deployment_config->ca_bundle_path;
  facts.ca_bundle_sha256 = *ca_bundle_sha;
  facts.execution_uid = deployment_config->execution_uid;
  facts.execution_gid = deployment_config->execution_gid;

  const auto environment =
      read_root_control_file(options.environment_receipt_file);
  if (!environment || !validate_environment_receipt(*environment, facts))
    return std::nullopt;
  facts.environment_fingerprint_sha256 = sha256_hex(*environment);

  const auto host =
      read_root_control_file(options.execution_host_receipt_file);
  if (!host ||
      !validate_execution_host_receipt(
          *host, facts, facts.environment_fingerprint_sha256))
    return std::nullopt;
  facts.execution_host_fingerprint_sha256 = sha256_hex(*host);

  const auto clock =
      read_root_control_file(options.clock_quality_receipt_file);
  if (!clock || !validate_clock_quality_receipt(*clock, facts))
    return std::nullopt;
  facts.clock_quality_receipt_sha256 = sha256_hex(*clock);
  return facts;
}

std::optional<OrderSnapshot> parse_order_snapshot(
    std::string_view body) {
  try {
    simdjson::dom::parser parser;
    auto root_result = parser.parse(body);
    if (root_result.error()) return std::nullopt;
    auto order = order_object(root_result.value());
    auto requested = count_field(
        order, {"initial_count_fp", "count_fp"}, {"initial_count", "count"});
    auto filled = count_field(
        order, {"fill_count_fp", "filled_count_fp"},
        {"fill_count", "filled_count"});
    auto remaining = count_field(
        order, {"remaining_count_fp"}, {"remaining_count"});
    if (!requested || !filled || !remaining)
      return std::nullopt;
    return OrderSnapshot{
        *requested, *filled, *remaining, sha256_hex(body)};
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<std::string> parse_order_id(std::string_view body) {
  try {
    simdjson::dom::parser parser;
    auto root_result = parser.parse(body);
    if (root_result.error()) return std::nullopt;
    auto order = order_object(root_result.value());
    return string_field(order, {"order_id"});
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<CreateAck> parse_create_ack(std::string_view body) {
  try {
    simdjson::dom::parser parser;
    auto root_result = parser.parse(body);
    if (root_result.error()) return std::nullopt;
    auto root = root_result.value();
    auto order_id = string_field(root, {"order_id"});
    auto filled = count_field(root, {"fill_count"}, {});
    auto remaining = count_field(root, {"remaining_count"}, {});
    std::int64_t ts_ms = 0;
    if (!order_id || !filled || !remaining ||
        root["ts_ms"].get(ts_ms) != simdjson::SUCCESS || ts_ms <= 0)
      return std::nullopt;
    std::optional<std::int64_t> average_fee;
    simdjson::dom::element average_fee_element;
    if (root["average_fee_paid"].get(average_fee_element) ==
        simdjson::SUCCESS) {
      average_fee = decimal_e6(average_fee_element);
      if (!average_fee || *average_fee < 0) return std::nullopt;
    }
    std::optional<std::int64_t> average_fill_price;
    simdjson::dom::element average_fill_price_element;
    if (root["average_fill_price"].get(average_fill_price_element) ==
        simdjson::SUCCESS) {
      average_fill_price = decimal_e4(average_fill_price_element);
      if (!average_fill_price || *average_fill_price <= 0 ||
          *average_fill_price > 10'000)
        return std::nullopt;
    }
    return CreateAck{
        *order_id, *filled, *remaining, ts_ms, average_fee,
        average_fill_price};
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<CancelAck> parse_cancel_ack(std::string_view body) {
  try {
    simdjson::dom::parser parser;
    auto root_result = parser.parse(body);
    if (root_result.error()) return std::nullopt;
    auto root = root_result.value();
    auto order_id = string_field(root, {"order_id"});
    auto reduced_by = count_field(root, {"reduced_by"}, {});
    std::int64_t ts_ms = 0;
    if (!order_id || !reduced_by ||
        root["ts_ms"].get(ts_ms) != simdjson::SUCCESS || ts_ms <= 0)
      return std::nullopt;
    return CancelAck{*order_id, *reduced_by, ts_ms};
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<PositionSnapshot> parse_position_snapshot(
    std::string_view body, std::string_view ticker) {
  try {
    simdjson::dom::parser parser;
    auto root_result = parser.parse(body);
    if (root_result.error()) return std::nullopt;
    simdjson::dom::array positions;
    if (root_result.value()["market_positions"].get(positions) !=
        simdjson::SUCCESS)
      return std::nullopt;
    for (auto position : positions) {
      auto found_ticker =
          string_field(position, {"ticker", "market_ticker"});
      if (!found_ticker || *found_ticker != ticker) continue;
      auto value = count_field(
          position, {"position_fp"}, {"position"});
      if (!value) return std::nullopt;
      return PositionSnapshot{*value, sha256_hex(body)};
    }
    // The portfolio endpoint omits zero-position markets.
    return PositionSnapshot{0, sha256_hex(body)};
  } catch (...) {
    return std::nullopt;
  }
}

std::optional<Authority> parse_authority(
    const Options& options, const RuntimeFacts& facts,
    std::string& authority_sha256) {
  const auto bytes = read_root_control_file(options.authority_file);
  if (!bytes) return std::nullopt;
  authority_sha256 = sha256_hex(*bytes);
  if (!is_sha256(options.expected_authority_sha256) ||
      authority_sha256 != options.expected_authority_sha256)
    return std::nullopt;
  try {
    simdjson::dom::parser parser;
    auto root_result = parser.parse(*bytes);
    if (root_result.error()) return std::nullopt;
    auto root = root_result.value();
    const std::set<std::string> expected_keys = {
        "allow_ioc_exit",
        "allow_place_cancel",
        "ca_bundle_sha256",
        "clock_quality_receipt_sha256",
        "consumption_ledger_path_sha256",
        "environment_fingerprint_sha256",
        "execution_host_fingerprint_sha256",
        "expires_at_unix_s",
        "issued_at_unix_s",
        "max_attempts",
        "max_cancel_orders",
        "max_cash_loss_e6",
        "max_fee_e6",
        "max_place_orders",
        "measured_on",
        "nonce_sha256",
        "producer_code_sha256",
        "producer_config_sha256",
        "receipt_id",
        "schema_version",
        "single_use",
        "ticker",
        "terminal_consumption_receipt_path_sha256",
        "trace_output_path_sha256",
        "price_cap_e4",
        "quantity_e4",
    };
    if (!has_exact_keys(root, expected_keys)) return std::nullopt;
    auto schema = string_field(root, {"schema_version"});
    auto ticker = string_field(root, {"ticker"});
    auto code_sha = string_field(root, {"producer_code_sha256"});
    auto config_sha = string_field(root, {"producer_config_sha256"});
    auto environment_sha =
        string_field(root, {"environment_fingerprint_sha256"});
    auto execution_host_sha =
        string_field(root, {"execution_host_fingerprint_sha256"});
    auto clock_quality_sha =
        string_field(root, {"clock_quality_receipt_sha256"});
    auto ca_bundle_sha =
        string_field(root, {"ca_bundle_sha256"});
    auto receipt_id = string_field(root, {"receipt_id"});
    auto measured_on = string_field(root, {"measured_on"});
    auto output_path_sha =
        string_field(root, {"trace_output_path_sha256"});
    auto ledger_path_sha =
        string_field(root, {"consumption_ledger_path_sha256"});
    auto terminal_path_sha = string_field(
        root, {"terminal_consumption_receipt_path_sha256"});
    auto nonce = string_field(root, {"nonce_sha256"});
    std::int64_t issued = 0;
    std::int64_t expires = 0;
    std::int64_t max_place_orders = 0;
    std::int64_t max_cancel_orders = 0;
    std::int64_t max_attempts = 0;
    std::int64_t quantity_e4 = 0;
    std::int64_t price_cap_e4 = 0;
    std::int64_t max_cash_loss_e6 = 0;
    std::int64_t max_fee_e6 = 0;
    bool allow_place_cancel = false;
    bool allow_ioc_exit = false;
    bool single_use = false;
    const auto output_path = strict_absolute_path(options.output);
    const auto ledger_path =
        strict_absolute_path(options.consumption_ledger);
    const auto terminal_path =
        strict_absolute_path(options.terminal_consumption_receipt);
    if (!output_path || !ledger_path || !terminal_path ||
        output_path == ledger_path || output_path == terminal_path ||
        ledger_path == terminal_path)
      return std::nullopt;
    if (!schema || *schema != kAuthoritySchema || !ticker ||
        *ticker != options.ticker || !code_sha ||
        *code_sha != facts.producer_code_sha256 || !config_sha ||
        *config_sha != facts.producer_config_sha256 ||
        !environment_sha ||
        *environment_sha != facts.environment_fingerprint_sha256 ||
        !execution_host_sha ||
        *execution_host_sha != facts.execution_host_fingerprint_sha256 ||
        !clock_quality_sha ||
        *clock_quality_sha != facts.clock_quality_receipt_sha256 ||
        !ca_bundle_sha || *ca_bundle_sha != facts.ca_bundle_sha256 ||
        !receipt_id || *receipt_id != options.receipt_id ||
        !measured_on || *measured_on != facts.hostname ||
        !output_path_sha ||
        *output_path_sha != sha256_hex(output_path->string()) ||
        !ledger_path_sha ||
        *ledger_path_sha != sha256_hex(ledger_path->string()) ||
        !terminal_path_sha ||
        *terminal_path_sha != sha256_hex(terminal_path->string()) ||
        !nonce || !is_sha256(*nonce) ||
        root["issued_at_unix_s"].get(issued) != simdjson::SUCCESS ||
        root["expires_at_unix_s"].get(expires) != simdjson::SUCCESS ||
        root["max_place_orders"].get(max_place_orders) !=
            simdjson::SUCCESS ||
        root["max_cancel_orders"].get(max_cancel_orders) !=
            simdjson::SUCCESS ||
        root["max_attempts"].get(max_attempts) != simdjson::SUCCESS ||
        root["quantity_e4"].get(quantity_e4) != simdjson::SUCCESS ||
        root["price_cap_e4"].get(price_cap_e4) != simdjson::SUCCESS ||
        root["max_cash_loss_e6"].get(max_cash_loss_e6) !=
            simdjson::SUCCESS ||
        root["max_fee_e6"].get(max_fee_e6) != simdjson::SUCCESS ||
        root["allow_place_cancel"].get(allow_place_cancel) !=
            simdjson::SUCCESS ||
        root["allow_ioc_exit"].get(allow_ioc_exit) != simdjson::SUCCESS)
      return std::nullopt;
    if (root["single_use"].get(single_use) != simdjson::SUCCESS)
      return std::nullopt;
    const auto now = std::chrono::system_clock::to_time_t(
        std::chrono::system_clock::now());
    if (issued > now || expires <= now || expires <= issued ||
        expires - issued > 15 * 60 || !allow_place_cancel ||
        allow_ioc_exit || !single_use || max_place_orders != 1 ||
        max_cancel_orders != 1 || max_attempts != 1 ||
        quantity_e4 != kQuantityE4 || price_cap_e4 != kPriceCapE4 ||
        max_cash_loss_e6 != kMaximumCashLossE6 ||
        max_fee_e6 != kMaximumFeeE6)
      return std::nullopt;
    const auto accepted_at_steady_ns = steady_now_ns();
    const auto remaining_seconds =
        static_cast<std::uint64_t>(expires - now);
    if (remaining_seconds >
        (std::numeric_limits<std::uint64_t>::max() -
         accepted_at_steady_ns) /
            1'000'000'000ULL)
      return std::nullopt;
    const std::string transaction_id = sha256_hex(
        authority_sha256 + *nonce + options.receipt_id);
    return Authority{
        issued,
        expires,
        allow_place_cancel,
        allow_ioc_exit,
        *nonce,
        transaction_id,
        accepted_at_steady_ns,
        accepted_at_steady_ns + remaining_seconds * 1'000'000'000ULL,
    };
  } catch (...) {
    return std::nullopt;
  }
}

bool authority_allows_new_risk(const Authority& authority) {
  const auto wall_now = std::chrono::system_clock::to_time_t(
      std::chrono::system_clock::now());
  const auto steady_now = steady_now_ns();
  return wall_now >= authority.issued_at_unix_s &&
         wall_now < authority.expires_at_unix_s &&
         steady_now >= authority.accepted_at_steady_ns &&
         steady_now < authority.monotonic_deadline_ns;
}

constexpr std::array<std::string_view, 18> kAmbientNetworkVariables = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "CURL_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "AWS_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "GIT_PROXY_COMMAND",
    "SOCKS_PROXY",
    "socks_proxy",
    "CURL_HOME",
};

bool scrub_ambient_network_environment() {
  for (const auto name : kAmbientNetworkVariables) {
    if (::unsetenv(std::string(name).c_str()) != 0) return false;
  }
  for (const auto name : kAmbientNetworkVariables) {
    if (::getenv(std::string(name).c_str()) != nullptr) return false;
  }
  return true;
}

bool network_boundary_still_valid(const RuntimeFacts& facts) {
  for (const auto name : kAmbientNetworkVariables) {
    if (::getenv(std::string(name).c_str()) != nullptr) return false;
  }
  const auto default_ca = default_curl_ca_path();
  if (!default_ca ||
      std::filesystem::path(*default_ca).lexically_normal() !=
          std::filesystem::path(facts.ca_bundle_path))
    return false;
  const auto ca_sha = hash_root_read_only_file(
      std::filesystem::path(facts.ca_bundle_path));
  return ca_sha && *ca_sha == facts.ca_bundle_sha256;
}

bool drop_execution_privileges(const RuntimeFacts& facts) {
  if (::geteuid() != 0 || facts.execution_uid == 0 ||
      facts.execution_gid == 0)
    return false;
  if (::setgroups(0, nullptr) != 0 ||
      ::setgid(facts.execution_gid) != 0 ||
      ::setuid(facts.execution_uid) != 0)
    return false;
#if defined(__linux__)
  if (::prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) return false;
#endif
  return ::getuid() == facts.execution_uid &&
         ::geteuid() == facts.execution_uid &&
         ::getgid() == facts.execution_gid &&
         ::getegid() == facts.execution_gid;
}

bool valid_ticker(std::string_view ticker) {
  return !ticker.empty() && ticker.size() <= 64 &&
         std::all_of(ticker.begin(), ticker.end(), [](unsigned char c) {
           return std::isalnum(c) || c == '-' || c == '_';
         });
}

bool valid_ascii_label(std::string_view value) {
  return !value.empty() && value.size() <= 160 &&
         std::all_of(value.begin(), value.end(), [](unsigned char c) {
           return c >= 0x21 && c <= 0x7e;
         });
}

std::string json_escape(std::string_view value) {
  std::string result;
  result.reserve(value.size() + 8);
  for (unsigned char c : value) {
    switch (c) {
      case '"': result += "\\\""; break;
      case '\\': result += "\\\\"; break;
      case '\b': result += "\\b"; break;
      case '\f': result += "\\f"; break;
      case '\n': result += "\\n"; break;
      case '\r': result += "\\r"; break;
      case '\t': result += "\\t"; break;
      default:
        if (c < 0x20) {
          char buffer[7];
          std::snprintf(buffer, sizeof(buffer), "\\u%04x", c);
          result += buffer;
        } else {
          result.push_back(static_cast<char>(c));
        }
    }
  }
  return result;
}

std::string sample_json(const CausalSample& sample) {
  std::ostringstream output;
  output
      << "{\"acknowledged_ns\":" << sample.acknowledged_ns
      << ",\"action_semantics\":\"" << sample.action_semantics
      << "\",\"average_fee_paid_e6\":";
  if (sample.average_fee_paid_e6) {
    output << *sample.average_fee_paid_e6;
  } else {
    output << "null";
  }
  output << ",\"average_fill_price_e4\":";
  if (sample.average_fill_price_e4) {
    output << *sample.average_fill_price_e4;
  } else {
    output << "null";
  }
  output
      << ",\"book_side\":\"" << sample.book_side
      << "\",\"clock_quality_receipt_sha256\":\""
      << sample.clock_quality_receipt_sha256
      << "\",\"decision_ns\":" << sample.decision_ns
      << ",\"effective_ns\":" << sample.effective_ns
      << ",\"environment_fingerprint_sha256\":\""
      << sample.environment_fingerprint_sha256
      << "\",\"execution_host_fingerprint_sha256\":\""
      << sample.execution_host_fingerprint_sha256
      << "\",\"fill_reconciliation\":{"
      << "\"canceled_quantity_e4\":" << sample.canceled_e4
      << ",\"filled_quantity_e4\":" << sample.filled_e4
      << ",\"order_status\":\"" << sample.order_status
      << "\",\"position_after_e4\":" << sample.position_after_e4
      << ",\"position_before_e4\":" << sample.position_before_e4
      << ",\"reconciled\":true,\"reduce_only\":"
      << (sample.request_reduce_only ? "true" : "false")
      << ",\"remaining_quantity_e4\":" << sample.remaining_e4
      << ",\"requested_quantity_e4\":" << sample.requested_e4
      << "},\"http_status\":" << sample.http_status
      << ",\"live_authority_sha256\":\""
      << sample.live_authority_sha256
      << "\",\"matching_engine_ts_ms\":"
      << sample.matching_engine_ts_ms
      << ",\"order_ref_sha256\":\"" << sample.order_ref_sha256
      << "\",\"path\":\"" << sample.path
      << "\",\"request_reduce_only\":"
      << (sample.request_reduce_only ? "true" : "false")
      << ",\"request_sha256\":\"" << sample.request_sha256
      << "\",\"requested_price_e4\":" << sample.requested_price_e4
      << ",\"response_sha256\":\"" << sample.response_sha256
      << "\",\"sent_ns\":" << sample.sent_ns
      << ",\"source_event_sha256\":\"" << sample.source_event_sha256
      << "\",\"subaccount\":" << sample.subaccount
      << ",\"ticker_sha256\":\"" << sample.ticker_sha256
      << "\",\"time_in_force\":\"" << sample.time_in_force
      << "\"}";
  return output.str();
}

struct MutationAccounting {
  std::int64_t place_post_attempts = 0;
  std::int64_t cancel_delete_attempts = 0;
  std::string client_order_id_sha256;
  bool cancel_risk_reduction_after_expiry = false;
};

enum class AmbiguousLookupOutcome {
  ExactClientOrderFound,
  NoMatchingOrder,
  QueryTimeout,
};

enum class AmbiguousCancelOutcome {
  Confirmed,
  TimeoutOrUnknown,
  NotAttempted,
};

struct AmbiguousRecoveryEvidence {
  AmbiguousLookupOutcome lookup =
      AmbiguousLookupOutcome::QueryTimeout;
  AmbiguousCancelOutcome cancel =
      AmbiguousCancelOutcome::NotAttempted;
  bool terminal_readback_complete = false;
  std::int64_t requested_before_e4 = 0;
  std::int64_t filled_before_e4 = 0;
  std::int64_t remaining_before_e4 = 0;
  std::int64_t requested_after_e4 = 0;
  std::int64_t filled_after_e4 = 0;
  std::int64_t remaining_after_e4 = 0;
  std::int64_t position_before_e4 = 0;
  std::int64_t position_after_e4 = 0;
};

// A missing list result is not proof of non-existence: the create may have
// committed but not yet appeared in a bounded/eventually-consistent read.
// Likewise, a cancel timeout or any fill requires an external account block
// and position-flattening path which this binary deliberately does not have.
bool ambiguous_recovery_proves_no_residual_risk(
    const AmbiguousRecoveryEvidence& evidence) {
  return evidence.lookup ==
             AmbiguousLookupOutcome::ExactClientOrderFound &&
         evidence.cancel == AmbiguousCancelOutcome::Confirmed &&
         evidence.terminal_readback_complete &&
         evidence.requested_before_e4 == kQuantityE4 &&
         evidence.filled_before_e4 == 0 &&
         evidence.remaining_before_e4 == kQuantityE4 &&
         evidence.requested_after_e4 == kQuantityE4 &&
         evidence.filled_after_e4 == 0 &&
         evidence.remaining_after_e4 == 0 &&
         evidence.position_after_e4 == evidence.position_before_e4;
}

bool publish_terminal_consumption(
    OutputReservation& reservation, const Authority& authority,
    std::string_view authority_sha256,
    const MutationAccounting& accounting, std::string_view terminal_state,
    const std::optional<std::string>& trace_source_sha256) {
  if (!valid_ascii_label(terminal_state) ||
      accounting.place_post_attempts < 0 ||
      accounting.place_post_attempts > 1 ||
      accounting.cancel_delete_attempts < 0 ||
      accounting.cancel_delete_attempts > 1 ||
      (!accounting.client_order_id_sha256.empty() &&
       !is_sha256(accounting.client_order_id_sha256)) ||
      (trace_source_sha256 && !is_sha256(*trace_source_sha256)))
    return false;
  const auto finished_at_wall_utc_ms =
      std::chrono::duration_cast<std::chrono::milliseconds>(
          std::chrono::system_clock::now().time_since_epoch())
          .count();
  std::ostringstream document;
  document
      << "{\"authority_sha256\":\"" << authority_sha256
      << "\",\"cancel_delete_attempts\":"
      << accounting.cancel_delete_attempts
      << ",\"cancel_risk_reduction_after_expiry\":"
      << (accounting.cancel_risk_reduction_after_expiry ? "true" : "false")
      << ",\"client_order_id_sha256\":";
  if (accounting.client_order_id_sha256.empty()) {
    document << "null";
  } else {
    document << "\"" << accounting.client_order_id_sha256 << "\"";
  }
  document
      << ",\"finished_at_wall_utc_ms\":" << finished_at_wall_utc_ms
      << ",\"place_post_attempts\":" << accounting.place_post_attempts
      << ",\"schema_version\":\"pnl-spine-latency-authority-terminal-v2\""
      << ",\"terminal_state\":\"" << terminal_state
      << "\",\"trace_source_sha256\":";
  if (trace_source_sha256) {
    document << "\"" << *trace_source_sha256 << "\"";
  } else {
    document << "null";
  }
  document << ",\"transaction_id\":\"" << authority.transaction_id
           << "\"}";
  return reservation.finish(document.str());
}

std::optional<PositionSnapshot> fetch_position(
    KalshiClient& client, const std::string& ticker) {
  auto response = client.request(
      Method::Get, "/portfolio/positions?ticker=" + ticker);
  if (!response || !response->ok()) return std::nullopt;
  return parse_position_snapshot(response->body, ticker);
}

std::optional<OrderSnapshot> fetch_order(
    KalshiClient& client, const std::string& order_id) {
  auto response = client.request(
      Method::Get, std::string(kGetOrderPath) + "/" + order_id);
  if (!response || !response->ok()) return std::nullopt;
  return parse_order_snapshot(response->body);
}

bool best_effort_cancel(
    KalshiClient& client, KalshiClient::Lane& lane,
    const std::string& order_id, const RuntimeFacts& facts,
    const Authority& authority, MutationAccounting& accounting) {
  if (accounting.cancel_delete_attempts != 0 ||
      !network_boundary_still_valid(facts))
    return false;
  const bool live_before_sign = authority_allows_new_risk(authority);
  const std::string path =
      std::string(kEventOrderPath) + "/" + order_id;
  auto request = client.sign_request(Method::Delete, path);
  if (!request || !network_boundary_still_valid(facts)) return false;
  accounting.cancel_risk_reduction_after_expiry =
      !live_before_sign || !authority_allows_new_risk(authority);
  ++accounting.cancel_delete_attempts;
  auto response = lane.send(*request);
  return response && response->ok();
}

bool require_common_options(const Options& options) {
  return valid_ticker(options.ticker) &&
         is_sha256(options.expected_authority_sha256) &&
         !options.producer_config_file.empty() &&
         !options.environment_receipt_file.empty() &&
         !options.execution_host_receipt_file.empty() &&
         !options.clock_quality_receipt_file.empty() &&
         valid_ascii_label(options.receipt_id);
}

std::optional<std::string> next_value(
    int& index, int argc, char** argv) {
  if (index + 1 >= argc) return std::nullopt;
  return std::string(argv[++index]);
}

std::optional<Options> parse_options(int argc, char** argv) {
  Options options;
  for (int index = 1; index < argc; ++index) {
    const std::string argument(argv[index]);
    if (argument == "--execute-place-cancel") {
      options.execute_place_cancel = true;
    } else if (argument == "--ioc-exit-preflight") {
      options.ioc_exit_preflight = true;
    } else if (argument == "--execute-ioc-exit") {
      options.execute_ioc_exit = true;
    } else if (argument == "--self-test-v2") {
      options.self_test_v2 = true;
    } else if (argument == "--self-test-authority-deadline") {
      options.self_test_authority_deadline = true;
    } else if (argument == "--self-test-network-environment") {
      options.self_test_network_environment = true;
    } else if (argument == "--self-test-ambiguous-post-recovery") {
      options.self_test_ambiguous_post_recovery = true;
    } else {
      auto value = next_value(index, argc, argv);
      if (!value) return std::nullopt;
      if (argument == "--ticker") options.ticker = *value;
      else if (argument == "--authority-file") options.authority_file = *value;
      else if (argument == "--expected-authority-sha256")
        options.expected_authority_sha256 = *value;
      else if (argument == "--producer-config-file")
        options.producer_config_file = *value;
      else if (argument == "--environment-receipt-file")
        options.environment_receipt_file = *value;
      else if (argument == "--execution-host-receipt-file")
        options.execution_host_receipt_file = *value;
      else if (argument == "--clock-quality-receipt-file")
        options.clock_quality_receipt_file = *value;
      else if (argument == "--receipt-id") options.receipt_id = *value;
      else if (argument == "--out") options.output = *value;
      else if (argument == "--consumption-ledger")
        options.consumption_ledger = *value;
      else if (argument == "--terminal-consumption-receipt")
        options.terminal_consumption_receipt = *value;
      else return std::nullopt;
    }
  }
  return options;
}

int dry_run() {
  std::puts(
      "{\"default_mode\":\"DRY_RUN\",\"network_io\":false,"
      "\"order_transmitted\":false,\"place_cancel_gate\":"
      "\"FAIL_CLOSED_AMBIGUOUS_POST_RECOVERY_UNPROVEN\","
      "\"ioc_exit_gate\":\"FAIL_CLOSED_EXECUTOR_NOT_IMPLEMENTED\","
      "\"three_path_latency_ready\":false,"
      "\"schema_version\":\"pnl-spine-causal-latency-probe-plan-v1\"}");
  return 0;
}

int self_test_v2_contract() {
  constexpr std::string_view create_response =
      "{\"average_fee_paid\":\"0.000123\","
      "\"average_fill_price\":\"0.5600\",\"client_order_id\":\"c\","
      "\"fill_count\":\"1.00\",\"order_id\":\"o\","
      "\"remaining_count\":\"0.00\",\"ts_ms\":1800000000000}";
  constexpr std::string_view cancel_response =
      "{\"client_order_id\":\"c\",\"order_id\":\"o\","
      "\"reduced_by\":\"1.00\",\"ts_ms\":1800000000001}";
  constexpr std::string_view order_response =
      "{\"order\":{\"fill_count_fp\":\"0.00\","
      "\"initial_count_fp\":\"1.00\","
      "\"order_id\":\"o\",\"remaining_count_fp\":\"1.00\"}}";
  constexpr std::string_view position_response =
      "{\"market_positions\":[{\"market_ticker\":\"T\","
      "\"position_fp\":\"-2.00\"}]}";
  const auto create = parse_create_ack(create_response);
  const auto cancel = parse_cancel_ack(cancel_response);
  const auto order = parse_order_snapshot(order_response);
  const auto position = parse_position_snapshot(position_response, "T");
  const bool ok =
      create && create->order_id == "o" &&
      create->fill_count_e4 == 10'000 &&
      create->remaining_count_e4 == 0 &&
      create->matching_engine_ts_ms == 1'800'000'000'000LL &&
      create->average_fee_paid_e6 == 123 &&
      create->average_fill_price_e4 == 5'600 &&
      cancel && cancel->order_id == "o" &&
      cancel->reduced_by_e4 == 10'000 &&
      cancel->matching_engine_ts_ms == 1'800'000'000'001LL &&
      order && order->requested_e4 == 10'000 && order->filled_e4 == 0 &&
      order->remaining_e4 == 10'000 && position &&
      position->position_e4 == -20'000;
  std::puts(ok ? "V2 CONTRACT SELF-TEST PASS"
               : "V2 CONTRACT SELF-TEST FAIL");
  return ok ? 0 : 1;
}

int self_test_authority_deadline_contract() {
  const auto steady = steady_now_ns();
  const auto wall = std::chrono::system_clock::to_time_t(
      std::chrono::system_clock::now());
  Authority active{
      wall - 1,
      wall + 60,
      true,
      false,
      std::string(64, '1'),
      std::string(64, '2'),
      steady - 1,
      steady + 60'000'000'000ULL,
  };
  Authority expired_wall = active;
  expired_wall.expires_at_unix_s = wall;
  Authority expired_monotonic = active;
  expired_monotonic.monotonic_deadline_ns = steady;
  const bool ok =
      authority_allows_new_risk(active) &&
      !authority_allows_new_risk(expired_wall) &&
      !authority_allows_new_risk(expired_monotonic);
  std::puts(ok ? "AUTHORITY DEADLINE SELF-TEST PASS"
               : "AUTHORITY DEADLINE SELF-TEST FAIL");
  return ok ? 0 : 1;
}

int self_test_network_environment_contract() {
  const bool ok = scrub_ambient_network_environment() &&
                  std::all_of(
                      kAmbientNetworkVariables.begin(),
                      kAmbientNetworkVariables.end(),
                      [](std::string_view name) {
                        return ::getenv(std::string(name).c_str()) == nullptr;
                      });
  std::puts(ok ? "NETWORK ENVIRONMENT SELF-TEST PASS"
               : "NETWORK ENVIRONMENT SELF-TEST FAIL");
  return ok ? 0 : 1;
}

int self_test_ambiguous_post_recovery_contract() {
  const AmbiguousRecoveryEvidence order_exists_cleanly_canceled{
      AmbiguousLookupOutcome::ExactClientOrderFound,
      AmbiguousCancelOutcome::Confirmed,
      true,
      kQuantityE4,
      0,
      kQuantityE4,
      kQuantityE4,
      0,
      0,
      0,
      0,
  };
  AmbiguousRecoveryEvidence order_not_found =
      order_exists_cleanly_canceled;
  order_not_found.lookup = AmbiguousLookupOutcome::NoMatchingOrder;
  order_not_found.cancel = AmbiguousCancelOutcome::NotAttempted;
  order_not_found.terminal_readback_complete = false;
  AmbiguousRecoveryEvidence query_timeout = order_not_found;
  query_timeout.lookup = AmbiguousLookupOutcome::QueryTimeout;
  AmbiguousRecoveryEvidence cancel_timeout =
      order_exists_cleanly_canceled;
  cancel_timeout.cancel = AmbiguousCancelOutcome::TimeoutOrUnknown;
  cancel_timeout.terminal_readback_complete = false;
  AmbiguousRecoveryEvidence partial_fill =
      order_exists_cleanly_canceled;
  partial_fill.filled_before_e4 = 1;
  partial_fill.remaining_before_e4 = kQuantityE4 - 1;
  partial_fill.filled_after_e4 = 1;
  partial_fill.position_after_e4 = 1;

  const bool ok =
      ambiguous_recovery_proves_no_residual_risk(
          order_exists_cleanly_canceled) &&
      !ambiguous_recovery_proves_no_residual_risk(order_not_found) &&
      !ambiguous_recovery_proves_no_residual_risk(query_timeout) &&
      !ambiguous_recovery_proves_no_residual_risk(cancel_timeout) &&
      !ambiguous_recovery_proves_no_residual_risk(partial_fill) &&
      !kAmbiguousPlaceRecoveryProven;
  std::puts(ok ? "AMBIGUOUS POST RECOVERY SELF-TEST PASS"
               : "AMBIGUOUS POST RECOVERY SELF-TEST FAIL");
  return ok ? 0 : 1;
}

int ioc_preflight(const Options& options) {
  if (!valid_ticker(options.ticker)) {
    std::fprintf(stderr, "IOC_EXIT preflight requires --ticker\n");
    return 2;
  }
  Runtime runtime;
  Config config;
  try {
    runtime = resolve_runtime();
  } catch (const SafetyViolation& error) {
    std::fprintf(stderr, "IOC_EXIT preflight refused: %s\n", error.what());
    return 2;
  }
  const char* key_id = std::getenv("KALSHI_API_KEY_ID");
  const char* key_path = std::getenv("KALSHI_PRIVATE_KEY_PATH");
  if (!key_id || !*key_id || !key_path || !*key_path) {
    std::fprintf(stderr, "IOC_EXIT preflight requires read credentials\n");
    return 2;
  }
  config.api_key_id = key_id;
  config.private_key_pem = read_file(key_path);
  config.base_url = runtime.rest_base_url;
  config.pool_size = 1;
  KalshiClient client(std::move(config));
  auto position = fetch_position(client, options.ticker);
  auto book = client.request(
      Method::Get, "/markets/" + options.ticker + "/orderbook?depth=1");
  if (!position || !book || !book->ok()) {
    std::fprintf(stderr, "IOC_EXIT read-only preflight incomplete\n");
    return 3;
  }
  const char* required_book_side =
      position->position_e4 > 0
          ? "ASK"
          : position->position_e4 < 0 ? "BID" : "NO_POSITION";
  std::printf(
      "{\"checked_at_ns\":%llu,\"market_snapshot_sha256\":\"%s\","
      "\"order_transmitted\":false,\"position_e4\":%lld,"
      "\"required_book_side\":\"%s\","
      "\"required_create_endpoint\":\"/portfolio/events/orders\","
      "\"required_authority_action\":\"IOC_EXIT\","
      "\"required_reduce_only\":true,"
      "\"schema_version\":\"pnl-spine-ioc-exit-preflight-v1\","
      "\"state\":\"REQUIRES_SEPARATE_CURRENT_LIVE_AUTHORITY_AND_"
      "REVIEWED_EXECUTOR\",\"ticker\":\"%s\","
      "\"required_time_in_force\":\"immediate_or_cancel\"}\n",
      static_cast<unsigned long long>(steady_now_ns()),
      sha256_hex(book->body).c_str(),
      static_cast<long long>(position->position_e4),
      required_book_side,
      json_escape(options.ticker).c_str());
  return 4;
}

int execute_place_cancel(const Options& options) {
  if constexpr (!kAmbiguousPlaceRecoveryProven) {
    (void)options;
    std::fprintf(
        stderr,
        "PLACE/CANCEL LIVE TRANSMISSION IS DISABLED AND FAILS CLOSED: "
        "ambiguous create recovery cannot yet prove no resting order or "
        "residual position; no authority, credential, network, or output "
        "artifact was touched\n");
    return 2;
  }
  if (!require_common_options(options) || options.authority_file.empty() ||
      options.output.empty() || options.consumption_ledger.empty() ||
      options.terminal_consumption_receipt.empty() ||
      options.clock_quality_receipt_file.empty()) {
    std::fprintf(stderr, "PLACE/CANCEL probe options incomplete\n");
    return 2;
  }
  const auto facts = collect_runtime_facts(options);
  if (!facts) {
    std::fprintf(
        stderr,
        "PLACE/CANCEL immutable code/config/environment/host/clock "
        "boundary refused\n");
    return 2;
  }
  std::string authority_sha256;
  auto authority = parse_authority(options, *facts, authority_sha256);
  if (!authority) {
    std::fprintf(stderr, "PLACE/CANCEL authority refused\n");
    return 2;
  }
  // This root preamble is the single-use broker.  The durable nonce record is
  // created in a root-only directory before the execution uid can obtain
  // credentials.  The root-owned output descriptors remain usable after
  // privilege drop, but the execution uid cannot unlink or reopen them.
  OutputReservation ledger_reservation(options.consumption_ledger);
  if (!ledger_reservation.valid()) {
    std::fprintf(stderr, "PLACE/CANCEL authority already consumed\n");
    return 2;
  }
  OutputReservation output_reservation(options.output);
  if (!output_reservation.valid()) {
    std::fprintf(
        stderr,
        "PLACE/CANCEL output reservation refused; authority remains "
        "consumed\n");
    return 2;
  }
  OutputReservation terminal_reservation(
      options.terminal_consumption_receipt);
  if (!terminal_reservation.valid()) {
    std::fprintf(
        stderr,
        "PLACE/CANCEL terminal reservation refused; authority remains "
        "consumed\n");
    return 2;
  }
  const auto consumed_at_wall_utc_ms =
      std::chrono::duration_cast<std::chrono::milliseconds>(
          std::chrono::system_clock::now().time_since_epoch())
          .count();
  std::ostringstream consumption;
  consumption
      << "{\"authority_sha256\":\"" << authority_sha256
      << "\",\"ca_bundle_sha256\":\"" << facts->ca_bundle_sha256
      << "\",\"clock_quality_receipt_sha256\":\""
      << facts->clock_quality_receipt_sha256
      << "\",\"consumed_at_wall_utc_ms\":" << consumed_at_wall_utc_ms
      << ",\"environment_fingerprint_sha256\":\""
      << facts->environment_fingerprint_sha256
      << "\",\"execution_host_fingerprint_sha256\":\""
      << facts->execution_host_fingerprint_sha256
      << "\",\"max_attempts\":1,\"max_cancel_delete_attempts\":1"
      << ",\"max_place_post_attempts\":1,\"nonce_sha256\":\""
      << authority->nonce_sha256 << "\",\"producer_code_sha256\":\""
      << facts->producer_code_sha256
      << "\",\"producer_config_sha256\":\""
      << facts->producer_config_sha256
      << "\",\"receipt_id\":\"" << json_escape(options.receipt_id)
      << "\",\"schema_version\":\"pnl-spine-latency-authority-"
      << "consumption-v2\","
      << "\"terminal_consumption_receipt_path_sha256\":\""
      << sha256_hex(
             strict_absolute_path(options.terminal_consumption_receipt)
                 ->string())
      << "\",\"trace_output_path_sha256\":\""
      << sha256_hex(strict_absolute_path(options.output)->string())
      << "\",\"transaction_id\":\"" << authority->transaction_id
      << "\"}";
  if (!ledger_reservation.finish(consumption.str())) {
    std::fprintf(stderr, "PLACE/CANCEL authority consumption failed\n");
    return 2;
  }
  if (!scrub_ambient_network_environment() ||
      !network_boundary_still_valid(*facts)) {
    std::fprintf(stderr, "PLACE/CANCEL proxy/CA boundary refused\n");
    return 2;
  }
  if (!drop_execution_privileges(*facts)) {
    std::fprintf(stderr, "PLACE/CANCEL privilege drop refused\n");
    return 2;
  }

  MutationAccounting accounting;
  const auto terminate = [&](std::string_view state, int code) {
    (void)publish_terminal_consumption(
        terminal_reservation, *authority, authority_sha256, accounting,
        state, std::nullopt);
    return code;
  };

  Runtime runtime;
  try {
    runtime = resolve_runtime();
    require_orders_allowed(runtime);
  } catch (const SafetyViolation& error) {
    std::fprintf(stderr, "PLACE/CANCEL probe refused: %s\n", error.what());
    return terminate("BLOCKED_RUNTIME_SAFETY_GATE", 2);
  }
  if (!network_boundary_still_valid(*facts))
    return terminate("BLOCKED_PROXY_OR_CA_DRIFT", 2);
  const char* key_id = std::getenv("KALSHI_API_KEY_ID");
  const char* key_path = std::getenv("KALSHI_PRIVATE_KEY_PATH");
  if (!key_id || !*key_id || !key_path || !*key_path) {
    std::fprintf(stderr, "PLACE/CANCEL probe requires credentials\n");
    return terminate("BLOCKED_CREDENTIALS_UNAVAILABLE", 2);
  }
  Config config;
  config.api_key_id = key_id;
  try {
    config.private_key_pem = read_file(key_path);
  } catch (...) {
    std::fprintf(stderr, "PLACE/CANCEL private key read refused\n");
    return terminate("BLOCKED_PRIVATE_KEY_READ", 2);
  }
  config.base_url = runtime.rest_base_url;
  config.pool_size = 1;
  std::optional<KalshiClient> client_storage;
  try {
    client_storage.emplace(std::move(config));
  } catch (...) {
    std::fprintf(stderr, "PLACE/CANCEL client construction refused\n");
    return terminate("BLOCKED_CLIENT_CONSTRUCTION", 2);
  }
  KalshiClient& client = *client_storage;
  auto lane = client.make_lane();
  auto warm = lane.ping();
  if (!warm || !warm->ok()) {
    std::fprintf(stderr, "PLACE/CANCEL warm lane failed\n");
    return terminate("BLOCKED_WARMUP_FAILED", 3);
  }
  auto position_before = fetch_position(client, options.ticker);
  if (!position_before) {
    std::fprintf(stderr, "PLACE/CANCEL baseline position unavailable\n");
    return terminate("BLOCKED_BASELINE_POSITION", 3);
  }

  wire::ExecPayload payload;
  payload.action = wire::kActionBuy;
  payload.side = wire::kSideYes;
  payload.order_type = wire::kTypeLimit;
  payload.count = 1;
  payload.price_cents = 1;
  payload.strategy_id = 0;
  payload.seq = 1;
  payload.ts_ns = daemon::now_ns();
  payload.set_ticker(options.ticker);
  const std::string client_order_id = wire::client_order_id(payload);
  accounting.client_order_id_sha256 = sha256_hex(client_order_id);
  const std::string body =
      "{\"cancel_order_on_pause\":true,\"client_order_id\":\"" +
      client_order_id +
      "\",\"count\":\"1.00\",\"exchange_index\":0,\"post_only\":true,"
      "\"price\":\"0.0100\",\"reduce_only\":false,"
      "\"self_trade_prevention_type\":\"taker_at_cross\","
      "\"side\":\"bid\",\"subaccount\":0,\"ticker\":\"" +
      options.ticker +
      "\",\"time_in_force\":\"good_till_canceled\"}";
  const std::string place_material =
      std::string("POST\n") + std::string(kEventOrderPath) + "\n" + body;

  CausalSample place;
  place.path = "PLACE";
  place.action_semantics = "NEW_ORDER_PLACE";
  place.decision_ns = steady_now_ns();
  if (!authority_allows_new_risk(*authority))
    return terminate("BLOCKED_AUTHORITY_EXPIRED_BEFORE_PLACE_SIGN", 2);
  auto place_request =
      client.sign_request(Method::Post, kEventOrderPath, body);
  if (!place_request) {
    std::fprintf(stderr, "PLACE signing failed\n");
    return terminate("BLOCKED_PLACE_SIGNING", 3);
  }
  if (!authority_allows_new_risk(*authority) ||
      !network_boundary_still_valid(*facts))
    return terminate(
        "BLOCKED_AUTHORITY_OR_NETWORK_BOUNDARY_BEFORE_PLACE_SEND", 2);
  place.sent_ns = steady_now_ns();
  ++accounting.place_post_attempts;
  auto place_response = lane.send(*place_request);
  place.acknowledged_ns = steady_now_ns();
  if (!place_response || place_response->status != 201) {
    std::fprintf(
        stderr,
        "PLACE outcome ambiguous or rejected; authority consumed, no retry, "
        "manual reconciliation by deterministic client_order_id required\n");
    return terminate("BLOCKED_AMBIGUOUS_PLACE_OUTCOME_NO_RETRY", 6);
  }
  auto create_ack = parse_create_ack(place_response->body);
  if (!create_ack || create_ack->fill_count_e4 != 0 ||
      create_ack->remaining_count_e4 != kQuantityE4 ||
      create_ack->average_fee_paid_e6.has_value() ||
      create_ack->average_fill_price_e4.has_value()) {
    auto cleanup_order_id = parse_order_id(place_response->body);
    if (cleanup_order_id)
      (void)best_effort_cancel(
          client, lane, *cleanup_order_id, *facts, *authority,
          accounting);
    std::fprintf(
        stderr,
        "PLACE V2 ack failed zero-fill/remaining/engine-ts validation\n");
    return terminate("BLOCKED_PLACE_ACK_INVALID_CLEANUP_ATTEMPTED", 5);
  }
  const std::string& order_id = create_ack->order_id;
  auto resting = fetch_order(client, order_id);
  auto position_after_place = fetch_position(client, options.ticker);
  place.effective_ns = steady_now_ns();
  if (!resting || !position_after_place ||
      resting->requested_e4 != kQuantityE4 ||
      resting->filled_e4 != 0 ||
      resting->remaining_e4 != kQuantityE4 ||
      position_after_place->position_e4 != position_before->position_e4) {
    // Never intentionally leave the probe's remaining quantity resting after
    // a failed reconciliation.  This cleanup cannot turn the sample into a
    // success; the process still returns a hard failure.
    (void)best_effort_cancel(
        client, lane, order_id, *facts, *authority, accounting);
    std::fprintf(
        stderr,
        "PLACE reconciliation failed; operator must inspect/cancel account "
        "orders manually\n");
    return terminate(
        "BLOCKED_PLACE_READBACK_INVALID_CLEANUP_ATTEMPTED", 5);
  }
  place.order_ref_sha256 = sha256_hex(order_id);
  place.request_sha256 = sha256_hex(place_material);
  place.response_sha256 = sha256_hex(
      place_response->body + "\n" + resting->response_sha256 + "\n" +
      position_before->response_sha256 + "\n" +
      position_after_place->response_sha256);
  place.source_event_sha256 = sha256_hex(
      authority_sha256 + place.request_sha256 + place.response_sha256 +
      std::to_string(place.decision_ns) +
      std::to_string(place.effective_ns));
  place.http_status = place_response->status;
  place.live_authority_sha256 = authority_sha256;
  place.matching_engine_ts_ms = create_ack->matching_engine_ts_ms;
  place.average_fee_paid_e6 = create_ack->average_fee_paid_e6;
  place.average_fill_price_e4 = create_ack->average_fill_price_e4;
  place.book_side = "BID";
  place.clock_quality_receipt_sha256 =
      facts->clock_quality_receipt_sha256;
  place.environment_fingerprint_sha256 =
      facts->environment_fingerprint_sha256;
  place.execution_host_fingerprint_sha256 =
      facts->execution_host_fingerprint_sha256;
  place.request_reduce_only = false;
  place.requested_price_e4 = kPriceCapE4;
  place.subaccount = 0;
  place.ticker_sha256 = sha256_hex(options.ticker);
  place.time_in_force = "GOOD_TILL_CANCELED";
  place.requested_e4 = kQuantityE4;
  place.filled_e4 = 0;
  place.canceled_e4 = 0;
  place.remaining_e4 = kQuantityE4;
  place.position_before_e4 = position_before->position_e4;
  place.position_after_e4 = position_after_place->position_e4;
  place.order_status = "RESTING";

  const std::string cancel_path =
      std::string(kEventOrderPath) + "/" + order_id;
  const std::string cancel_material = "DELETE\n" + cancel_path + "\n";
  CausalSample cancel;
  cancel.path = "CANCEL";
  cancel.action_semantics = "RESTING_ORDER_CANCEL";
  cancel.decision_ns = steady_now_ns();
  const bool cancel_authority_live_before_sign =
      authority_allows_new_risk(*authority);
  if (!network_boundary_still_valid(*facts))
    return terminate("BLOCKED_NETWORK_BOUNDARY_BEFORE_CANCEL_SIGN", 5);
  auto cancel_request =
      client.sign_request(Method::Delete, cancel_path);
  if (!cancel_request) {
    std::fprintf(
        stderr, "CANCEL signing failed; manual cancellation required\n");
    return terminate("BLOCKED_CANCEL_SIGNING_MANUAL_RECONCILIATION", 5);
  }
  // Expiry is re-evaluated at every mutation boundary.  Once PLACE is
  // acknowledged, this exact DELETE is permitted after expiry solely as a
  // bounded risk-reduction cleanup; it cannot create or increase exposure.
  const bool cancel_after_expiry =
      !cancel_authority_live_before_sign ||
      !authority_allows_new_risk(*authority);
  accounting.cancel_risk_reduction_after_expiry = cancel_after_expiry;
  if (!network_boundary_still_valid(*facts))
    return terminate("BLOCKED_NETWORK_BOUNDARY_BEFORE_CANCEL", 5);
  if (accounting.cancel_delete_attempts != 0)
    return terminate("BLOCKED_CANCEL_CARDINALITY", 5);
  cancel.sent_ns = steady_now_ns();
  ++accounting.cancel_delete_attempts;
  auto cancel_response = lane.send(*cancel_request);
  cancel.acknowledged_ns = steady_now_ns();
  if (!cancel_response || cancel_response->status != 200) {
    std::fprintf(
        stderr, "CANCEL mutation failed; manual cancellation required\n");
    return terminate("BLOCKED_AMBIGUOUS_CANCEL_OUTCOME", 5);
  }
  auto cancel_ack = parse_cancel_ack(cancel_response->body);
  if (!cancel_ack || cancel_ack->order_id != order_id ||
      cancel_ack->reduced_by_e4 != kQuantityE4) {
    std::fprintf(
        stderr, "CANCEL V2 ack failed reduced_by/engine-ts validation\n");
    return terminate("BLOCKED_CANCEL_ACK_INVALID", 5);
  }
  auto canceled = fetch_order(client, order_id);
  auto position_after_cancel = fetch_position(client, options.ticker);
  cancel.effective_ns = steady_now_ns();
  if (!canceled || !position_after_cancel ||
      canceled->requested_e4 != kQuantityE4 ||
      canceled->filled_e4 != 0 || canceled->remaining_e4 != 0 ||
      position_after_cancel->position_e4 !=
          position_before->position_e4) {
    std::fprintf(
        stderr,
        "CANCEL reconciliation failed; sample is not successful and "
        "operator inspection is required\n");
    return terminate("BLOCKED_CANCEL_READBACK_INVALID", 5);
  }
  cancel.order_ref_sha256 = place.order_ref_sha256;
  cancel.request_sha256 = sha256_hex(cancel_material);
  cancel.response_sha256 = sha256_hex(
      cancel_response->body + "\n" + canceled->response_sha256 + "\n" +
      position_after_place->response_sha256 + "\n" +
      position_after_cancel->response_sha256);
  cancel.source_event_sha256 = sha256_hex(
      authority_sha256 + cancel.request_sha256 + cancel.response_sha256 +
      std::to_string(cancel.decision_ns) +
      std::to_string(cancel.effective_ns));
  cancel.http_status = cancel_response->status;
  cancel.live_authority_sha256 = authority_sha256;
  cancel.matching_engine_ts_ms = cancel_ack->matching_engine_ts_ms;
  cancel.average_fee_paid_e6 = std::nullopt;
  cancel.average_fill_price_e4 = std::nullopt;
  cancel.book_side = place.book_side;
  cancel.clock_quality_receipt_sha256 =
      place.clock_quality_receipt_sha256;
  cancel.environment_fingerprint_sha256 =
      place.environment_fingerprint_sha256;
  cancel.execution_host_fingerprint_sha256 =
      place.execution_host_fingerprint_sha256;
  cancel.request_reduce_only = false;
  cancel.requested_price_e4 = place.requested_price_e4;
  cancel.subaccount = place.subaccount;
  cancel.ticker_sha256 = place.ticker_sha256;
  cancel.time_in_force = place.time_in_force;
  cancel.requested_e4 = kQuantityE4;
  cancel.filled_e4 = 0;
  cancel.canceled_e4 = kQuantityE4;
  cancel.remaining_e4 = 0;
  cancel.position_before_e4 = position_after_place->position_e4;
  cancel.position_after_e4 = position_after_cancel->position_e4;
  cancel.order_status = "CANCELED";

  const std::uint64_t created_at_ns = steady_now_ns();
  const auto created_at_wall_utc_ms =
      std::chrono::duration_cast<std::chrono::milliseconds>(
          std::chrono::system_clock::now().time_since_epoch())
          .count();
  std::ostringstream trace;
  trace << "{\"clock_id\":\"" << kProbeClockId << "\","
        << "\"clock_quality_receipt_sha256\":\""
        << facts->clock_quality_receipt_sha256 << "\","
        << "\"created_at_ns\":" << created_at_ns
        << ",\"created_at_wall_utc_ms\":" << created_at_wall_utc_ms
        << ",\"environment_fingerprint_sha256\":\""
        << facts->environment_fingerprint_sha256
        << "\",\"execution_host_fingerprint_sha256\":\""
        << facts->execution_host_fingerprint_sha256
        << "\",\"measured_on\":\"" << json_escape(facts->hostname)
        << "\",\"measurement_mode\":\"REAL_ORDER_MEASURED\","
        << "\"producer_code_sha256\":\"" << facts->producer_code_sha256
        << "\",\"producer_config_sha256\":\""
        << facts->producer_config_sha256
        << "\",\"receipt_id\":\"" << json_escape(options.receipt_id)
        << "\",\"samples\":[" << sample_json(place) << ","
        << sample_json(cancel) << "],\"schema_version\":\""
        << kPrivateTraceSchema << "\"}";
  if (!output_reservation.finish(trace.str())) {
    std::fprintf(
        stderr, "private trace output write failed: %s\n",
        std::strerror(errno));
    return terminate("BLOCKED_PRIVATE_TRACE_WRITE", 3);
  }
  const std::string trace_sha256 = sha256_hex(trace.str());
  if (!publish_terminal_consumption(
          terminal_reservation, *authority, authority_sha256, accounting,
          "PLACE_CANCEL_RECONCILED", trace_sha256)) {
    std::fprintf(stderr, "terminal consumption receipt write failed\n");
    return 3;
  }
  std::printf(
      "{\"ioc_exit_state\":\"FAIL_CLOSED_EXECUTOR_NOT_IMPLEMENTED\","
      "\"order_transmitted\":true,"
      "\"place_cancel_trace_sha256\":\"%s\","
      "\"private_trace_path\":\"%s\","
      "\"state\":\"PLACE_CANCEL_RECONCILED_IOC_EXIT_MISSING\"}\n",
      trace_sha256.c_str(), json_escape(options.output).c_str());
  return 4;
}

}  // namespace

int main(int argc, char** argv) {
  const auto parsed = parse_options(argc, argv);
  if (!parsed) {
    std::fprintf(
        stderr,
        "usage: pnl_latency_probe [--ioc-exit-preflight --ticker T] | "
        "[--execute-place-cancel --ticker T --authority-file P "
        "--expected-authority-sha256 H --producer-config-file P "
        "--environment-receipt-file P --execution-host-receipt-file P "
        "--clock-quality-receipt-file P --receipt-id ID "
        "--out P --consumption-ledger P "
        "--terminal-consumption-receipt P]\n");
    return 2;
  }
  const Options& options = *parsed;
  if (options.execute_ioc_exit) {
    std::fprintf(
        stderr,
        "IOC_EXIT TRANSMISSION IS NOT IMPLEMENTED AND FAILS CLOSED: "
        "read-only preflight cannot produce IOC_EXIT evidence\n");
    return 2;
  }
  if (options.self_test_v2) return self_test_v2_contract();
  if (options.self_test_authority_deadline)
    return self_test_authority_deadline_contract();
  if (options.self_test_network_environment)
    return self_test_network_environment_contract();
  if (options.self_test_ambiguous_post_recovery)
    return self_test_ambiguous_post_recovery_contract();
  if (options.execute_place_cancel && options.ioc_exit_preflight) {
    std::fprintf(stderr, "choose exactly one probe mode\n");
    return 2;
  }
  if (options.execute_place_cancel) return execute_place_cancel(options);
  if (options.ioc_exit_preflight) return ioc_preflight(options);
  return dry_run();
}
