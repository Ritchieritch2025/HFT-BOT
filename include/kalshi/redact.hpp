#pragma once
//
// Auth-header redaction (docs/PLAN_TOKEN_RULES.md hard rule 2). Used by the
// client's verbose debug callback so KALSHI-ACCESS-KEY / -SIGNATURE / -TIMESTAMP
// and Authorization VALUES never reach a log, and unit-tested directly.

#include <cstddef>
#include <string>
#include <string_view>

namespace kalshi {

// Replace the value of any sensitive header line with ***REDACTED***, leaving the
// header name and all non-sensitive lines intact. Operates on a header block
// (one or many CRLF/LF-terminated lines).
inline std::string redact_auth_headers(std::string_view in) {
  std::string s(in);
  static const char* const kSensitive[] = {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-SIGNATURE",
                                           "KALSHI-ACCESS-TIMESTAMP", "Authorization"};
  for (const char* key : kSensitive) {
    for (std::size_t p = s.find(key); p != std::string::npos; p = s.find(key, p + 1)) {
      const std::size_t colon = s.find(':', p);
      const std::size_t eol = s.find('\n', p);
      if (colon == std::string::npos || (eol != std::string::npos && colon > eol)) continue;
      std::size_t vend = (eol == std::string::npos) ? s.size() : eol;
      // Keep a trailing \r on the line intact.
      if (vend > colon + 1 && s[vend - 1] == '\r') --vend;
      s.replace(colon + 1, vend - (colon + 1), " ***REDACTED***");
    }
  }
  return s;
}

}  // namespace kalshi
