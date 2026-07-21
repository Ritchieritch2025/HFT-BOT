#!/usr/bin/env python3
"""WP-05 discovery probe (throwaway, sandbox-only).

Verifies private WS channel SUBSCRIBE semantics live on prod, per discovery
plan amendment A2: subscribing transmits nothing order-related. Connects to
wss://external-api-ws.kalshi.com/trade-api/ws/v2 with KALSHI-ACCESS-* auth,
subscribes to fill + user_orders + market_positions, records acks/errors and
server ping cadence for ~25s, then closes. Zero orders, zero mutations.

stdlib only (no `websockets`/`cryptography` on this machine); RSA-PSS signing
is delegated to the vendored OpenSSL 3.5.7 CLI. Never prints key material.

Usage: source ~/.kalshi/env.sh && python3 ws_private_probe.py
"""
import base64
import json
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import time

OSSL = "/Users/ritcardo/HFT BOT/third_party/openssl/bin/openssl"
HOST = "external-api-ws.kalshi.com"
PATH = "/trade-api/ws/v2"
RUN_SECONDS = 25


def sign(msg: str, key_path: str) -> str:
    digest = subprocess.run([OSSL, "dgst", "-sha256", "-binary"],
                            input=msg.encode(), capture_output=True, check=True).stdout
    sig = subprocess.run(
        [OSSL, "pkeyutl", "-sign", "-inkey", key_path,
         "-pkeyopt", "digest:sha256", "-pkeyopt", "rsa_padding_mode:pss",
         "-pkeyopt", "rsa_pss_saltlen:digest"],
        input=digest, capture_output=True, check=True).stdout
    return base64.b64encode(sig).decode()


def send_frame(sock, payload: bytes, opcode: int = 0x1) -> None:
    # Client frames must be masked (RFC 6455).
    mask = secrets.token_bytes(4)
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


def read_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def read_frame(sock):
    b1, b2 = read_exact(sock, 2)
    opcode = b1 & 0x0F
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", read_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", read_exact(sock, 8))[0]
    if b2 & 0x80:  # server frames should not be masked, but tolerate
        mask = read_exact(sock, 4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(read_exact(sock, n)))
    else:
        data = read_exact(sock, n)
    return opcode, data


def main() -> int:
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        print("export KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH first", file=sys.stderr)
        return 2

    ts = str(int(time.time() * 1000))
    sig = sign(ts + "GET" + PATH, key_path)

    ctx = ssl.create_default_context()
    raw = socket.create_connection((HOST, 443), timeout=15)
    sock = ctx.wrap_socket(raw, server_hostname=HOST)

    ws_key = base64.b64encode(secrets.token_bytes(16)).decode()
    req = (f"GET {PATH} HTTP/1.1\r\n"
           f"Host: {HOST}\r\n"
           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {ws_key}\r\nSec-WebSocket-Version: 13\r\n"
           f"KALSHI-ACCESS-KEY: {key_id}\r\n"
           f"KALSHI-ACCESS-SIGNATURE: {sig}\r\n"
           f"KALSHI-ACCESS-TIMESTAMP: {ts}\r\n\r\n")
    sock.sendall(req.encode())

    # Read HTTP upgrade response headers.
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += sock.recv(4096)
    head, _, rest = resp.partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0].decode()
    print(f"[handshake] {status}")
    if "101" not in status:
        print(head.decode(errors="replace"))
        return 1

    # Subscribe to the three private channels in one command.
    cmd = {"id": 1, "cmd": "subscribe",
           "params": {"channels": ["fill", "user_orders", "market_positions"]}}
    send_frame(sock, json.dumps(cmd).encode())
    print(f"[sent] {json.dumps(cmd)}")

    sock.settimeout(RUN_SECONDS + 5)
    deadline = time.time() + RUN_SECONDS
    pings = 0
    pending = rest
    # Re-inject any bytes that followed the handshake (rare).
    events = []
    buf_sock = sock
    if pending:
        print(f"[note] {len(pending)} bytes followed handshake (ignored: framing resync)")

    while time.time() < deadline:
        try:
            opcode, data = read_frame(buf_sock)
        except (socket.timeout, ConnectionError) as e:
            print(f"[end] {e}")
            break
        if opcode == 0x9:  # ping
            pings += 1
            send_frame(buf_sock, data, opcode=0xA)  # pong
            print(f"[ping #{pings}] {time.strftime('%H:%M:%S')} payload={data!r}")
        elif opcode == 0x8:
            print(f"[close] {data!r}")
            break
        elif opcode in (0x1, 0x2):
            txt = data.decode(errors="replace")
            events.append(txt)
            print(f"[msg] {txt[:400]}")

    send_frame(buf_sock, struct.pack(">H", 1000), opcode=0x8)
    print(f"[summary] messages={len(events)} server_pings={pings} over {RUN_SECONDS}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
