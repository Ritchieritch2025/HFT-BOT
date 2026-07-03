#!/bin/bash
# Bootstrap a fresh Linux box (Ubuntu 22.04/24.04 or Amazon Linux 2023) to
# build and run tradingd. Run from the repo root: ./deploy/bootstrap.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== packages =="
if command -v apt-get >/dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y build-essential libcurl4-openssl-dev libssl-dev \
                          python3 chrony redis-server
elif command -v dnf >/dev/null; then
  sudo dnf install -y gcc-c++ make libcurl-devel openssl-devel python3 chrony
  sudo dnf install -y redis6 || sudo dnf install -y redis || true
else
  echo "unsupported distro (need apt or dnf)"; exit 1
fi

echo "== compiler (C++23 needs GCC 12+ / Clang 16+) =="
CXX_BIN=${CXX:-g++}
GCC_MAJOR=$($CXX_BIN -dumpversion | cut -d. -f1)
if [ "$GCC_MAJOR" -lt 12 ]; then
  echo "g++ $GCC_MAJOR is too old for std::expected; installing g++-12"
  if command -v apt-get >/dev/null; then
    sudo apt-get install -y g++-12 && CXX_BIN=g++-12
  else
    sudo dnf install -y gcc12-c++ && CXX_BIN=g++-12 || {
      echo "install a C++23 compiler manually"; exit 1; }
  fi
fi
echo "using $CXX_BIN ($($CXX_BIN -dumpversion))"

echo "== clock discipline (signature timestamps) =="
sudo systemctl enable --now chronyd 2>/dev/null || sudo systemctl enable --now chrony || true
sleep 1
chronyc tracking 2>/dev/null | grep -E "Reference|System time" || \
  echo "WARNING: chrony not reporting; check time sync before trading"

echo "== redis (cold-path telemetry; optional) =="
sudo systemctl enable --now redis-server 2>/dev/null || \
  sudo systemctl enable --now redis6 2>/dev/null || \
  sudo systemctl enable --now redis 2>/dev/null || \
  echo "note: no redis service — telemetry will drop (trading unaffected)"

echo "== build =="
make clean >/dev/null 2>&1 || true
make -j"$(nproc)" CXX="$CXX_BIN"

echo "== tests =="
./build/test_signing
./build/test_ring
./tests/run_pipeline.sh

echo "== placement check (warm RTT to Kalshi) =="
./build/bench_rtt 30

echo
echo "bootstrap complete. Next:"
echo "  1. put your key at /etc/kalshi/private_key.pem (chmod 600, root or service user)"
echo "  2. cp deploy/tradingd.env.example /etc/kalshi/tradingd.env  # and fill in"
echo "  3. sudo cp deploy/tradingd.service /etc/systemd/system/ && sudo systemctl daemon-reload"
echo "  4. KALSHI_API_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=... ./build/preflight   # gate"
echo "  5. sudo systemctl enable --now tradingd && journalctl -fu tradingd"
