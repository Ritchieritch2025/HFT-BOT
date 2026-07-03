// Signature round-trip test.
//
// Self-test mode (no args): generates a throwaway RSA-2048 key, signs the
// documented Kalshi example message, and verifies the signature in-process
// with OpenSSL's EVP_DigestVerify configured exactly per the Kalshi spec
// (PSS, SHA-256, MGF1-SHA256, salt length == digest length). Also checks
// that tampered messages fail and that PSS salting randomizes signatures.
//
// CLI mode (test_signing <key.pem> <message>): prints the base64 signature
// to stdout, so a shell harness can cross-verify with the openssl CLI.

#include "kalshi/client.hpp"

#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>

#include <iostream>
#include <string>
#include <vector>

namespace {

int g_failures = 0;

void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

std::vector<unsigned char> b64_decode(const std::string& b64) {
  std::vector<unsigned char> out(3 * (b64.size() / 4) + 3);
  const int n = EVP_DecodeBlock(
      out.data(), reinterpret_cast<const unsigned char*>(b64.data()),
      static_cast<int>(b64.size()));
  if (n < 0) throw std::runtime_error("b64_decode failed");
  size_t pad = 0;
  if (b64.size() >= 1 && b64[b64.size() - 1] == '=') ++pad;
  if (b64.size() >= 2 && b64[b64.size() - 2] == '=') ++pad;
  out.resize(static_cast<size_t>(n) - pad);
  return out;
}

bool verify_pss_digest_salt(EVP_PKEY* key, std::string_view msg,
                            const std::vector<unsigned char>& sig) {
  EVP_MD_CTX* ctx = EVP_MD_CTX_new();
  EVP_PKEY_CTX* pctx = nullptr;
  const bool ok =
      ctx != nullptr &&
      EVP_DigestVerifyInit(ctx, &pctx, EVP_sha256(), nullptr, key) == 1 &&
      EVP_PKEY_CTX_set_rsa_padding(pctx, RSA_PKCS1_PSS_PADDING) == 1 &&
      EVP_PKEY_CTX_set_rsa_pss_saltlen(pctx, RSA_PSS_SALTLEN_DIGEST) == 1 &&
      EVP_PKEY_CTX_set_rsa_mgf1_md(pctx, EVP_sha256()) == 1 &&
      EVP_DigestVerify(ctx, sig.data(), sig.size(),
                       reinterpret_cast<const unsigned char*>(msg.data()),
                       msg.size()) == 1;
  EVP_MD_CTX_free(ctx);
  ERR_clear_error();
  return ok;
}

std::string pkey_to_pem(EVP_PKEY* key) {
  BIO* bio = BIO_new(BIO_s_mem());
  if (!bio || PEM_write_bio_PrivateKey(bio, key, nullptr, nullptr, 0, nullptr,
                                       nullptr) != 1)
    throw std::runtime_error("PEM_write_bio_PrivateKey failed");
  char* data = nullptr;
  const long len = BIO_get_mem_data(bio, &data);
  std::string pem(data, static_cast<size_t>(len));
  BIO_free(bio);
  return pem;
}

kalshi::Config test_config(std::string pem) {
  kalshi::Config cfg;
  cfg.api_key_id = "00000000-0000-0000-0000-000000000000";
  cfg.private_key_pem = std::move(pem);
  cfg.pool_size = 1;
  return cfg;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc == 3) {
    kalshi::Config cfg = test_config(kalshi::read_file(argv[1]));
    kalshi::KalshiClient client(std::move(cfg));
    auto sig = client.sign(argv[2]);
    if (!sig) {
      std::cerr << "sign failed: " << sig.error().message << "\n";
      return 1;
    }
    std::cout << *sig << "\n";
    return 0;
  }

  EVP_PKEY* key = EVP_RSA_gen(2048);
  if (!key) {
    std::cerr << "RSA keygen failed\n";
    return 1;
  }

  kalshi::KalshiClient client(test_config(pkey_to_pem(key)));

  const std::string message = "1700000000000GET/trade-api/v2/portfolio/balance";
  auto sig_b64 = client.sign(message);
  check(sig_b64.has_value(), "sign() returns a signature");
  if (!sig_b64) return 1;

  const auto sig = b64_decode(*sig_b64);
  check(sig.size() == 256, "signature is 256 bytes for RSA-2048 (got " +
                               std::to_string(sig.size()) + ")");
  check(verify_pss_digest_salt(key, message, sig),
        "signature verifies as PSS/SHA-256 with salt == digest length");
  check(!verify_pss_digest_salt(key, "1700000000001GET/trade-api/v2/portfolio/balance", sig),
        "tampered timestamp fails verification");
  check(!verify_pss_digest_salt(key, "1700000000000POST/trade-api/v2/portfolio/balance", sig),
        "tampered method fails verification");

  auto sig2_b64 = client.sign(message);
  check(sig2_b64.has_value() && *sig2_b64 != *sig_b64,
        "PSS signatures are salted (two signatures of one message differ)");
  if (sig2_b64) {
    check(verify_pss_digest_salt(key, message, b64_decode(*sig2_b64)),
          "second signature also verifies");
  }

  EVP_PKEY_free(key);
  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
