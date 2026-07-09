#!/usr/bin/env bash
# W-A1 bring-up: turn a bare Ubuntu 24.04 arm64 box into a locked-down,
# reproducible pipeline host — WITHOUT starting capture (that is W-A4).
# Idempotent: safe to re-run. Run ON THE BOX from the repo root:
#     cd ~/hft-bot && bash deploy/bringup_ec2.sh
#
# Prereq: the repo is already on the box (pushed from the Mac to the bare
# repo ~/kalshi.git and cloned to ~/hft-bot — see deploy/README_EC2.md;
# no GitHub credentials ever live on the box, S4).
#
# What it does:  apt build/runtime deps · UTC · chrony→Amazon Time Sync ·
#                16 GB swap + swappiness (one-box guardrail) · python venv
#                (duckdb pinned to the Mac's version) · C++ build (make -j2) ·
#                systemd units installed DRY (pipeline NOT enabled/started;
#                oom-guard timer enabled — it is a no-op until capture runs)
# What it NEVER does: touch ~/.kalshi/env.sh (operator hand-creates, 600, S4);
#                start capture; open firewall holes.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

echo "== [1/8] apt packages =="
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential clang make git \
  libssl-dev libcurl4-openssl-dev zlib1g-dev \
  python3-venv python3-pip \
  chrony zstd util-linux

echo "== [2/8] UTC timezone =="
sudo timedatectl set-timezone UTC

echo "== [3/8] chrony -> Amazon Time Sync (link-local, survives egress lockdown) =="
sudo mkdir -p /etc/chrony/sources.d
echo "server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4" \
  | sudo tee /etc/chrony/sources.d/aws-time-sync.sources >/dev/null
sudo systemctl restart chrony
sleep 3
chronyc tracking | sed -n '1,5p'

echo "== [4/8] 16 GB swap (one-box guardrail: build spills, capture survives) =="
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 16G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
# prefer RAM; swap is a cliff-edge backstop for the nightly build, not a cache
sudo sysctl -w vm.swappiness=10 >/dev/null
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-kalshi.conf >/dev/null
swapon --show

echo "== [5/8] python venv (>=3.12 required; Ubuntu 24.04 ships 3.12) =="
python3 --version
if [ ! -d .venv ]; then python3 -m venv .venv; fi
./.venv/bin/pip install --quiet --upgrade pip
# duckdb pinned to the Mac production version (warehouse file compatibility).
# pyyaml: LAZY-imported inside function bodies (mm_research.load_fee_facts,
# build_classification) — invisible to top-level import scans; W-A2 caught it.
./.venv/bin/pip install --quiet duckdb==1.4.5 numpy pandas pytest pyyaml
./.venv/bin/python -c "import duckdb; print('duckdb', duckdb.__version__)"

# NOTE (measured, W-A1): the build lands on g++ 13.3.0, NOT clang — GNU make
# has a built-in default CXX=g++, so the Makefile's `CXX ?= clang++` never
# fires on Linux. Deliberately NOT overridden: the W-A1/W-A2 validation ran
# against g++-built binaries; changing compilers would invalidate it. clang is
# installed as a fallback only.
echo "== [6/8] C++ build (g++ per make default, system OpenSSL per Makefile Linux branch) =="
make -j"$(nproc)" >/dev/null
ls -la build/ws_shadow build/ws_smoke build/preflight

echo "== [7/8] systemd units — installed DRY (capture NOT started; W-A4 owns that) =="
# AUDIT FIX (W-A1 audit B1): work/ is gitignored so a fresh clone has no
# work/live/ — but the unit's StandardOutput=append: opens the log file
# BEFORE ExecStart and systemd does NOT create parent dirs (empirically
# verified: missing dir ⇒ status=209/STDOUT crash-loop). The supervisor's own
# mkdir never gets the chance to run. Create it here; W-A4 go/no-go re-checks.
mkdir -p "$REPO/work/live"
sudo cp "$REPO/deploy/kalshi-pipeline.service"  /etc/systemd/system/
sudo cp "$REPO/deploy/kalshi-oom-guard.service" /etc/systemd/system/
sudo cp "$REPO/deploy/kalshi-oom-guard.timer"   /etc/systemd/system/
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/kalshi-pipeline.service \
                       /etc/systemd/system/kalshi-oom-guard.service \
                       /etc/systemd/system/kalshi-oom-guard.timer
# the guard timer is safe to run now (no-op until pipeline processes exist)
sudo systemctl enable --now kalshi-oom-guard.timer
systemctl status kalshi-oom-guard.timer --no-pager | head -5
echo "kalshi-pipeline.service: installed, NOT enabled (deliberate — W-A4)"

echo "== [8/8] unattended security updates (security pocket only) =="
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y unattended-upgrades
sudo dpkg-reconfigure -f noninteractive unattended-upgrades || true

echo
echo "BRING-UP COMPLETE. Remaining (operator, S4): create ~/.kalshi/env.sh"
echo "(chmod 600) + private key; then the auth smoke:  make sure NOTHING is"
echo "started until W-A4. Security checklist: deploy/SECURITY_CHECKLIST_EC2.md"
