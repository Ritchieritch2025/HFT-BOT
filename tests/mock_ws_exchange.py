#!/usr/bin/env python3
"""Minimal RFC6455 WebSocket mock of the Kalshi WS v2 endpoint (plain ws://).

Usage: mock_ws_exchange.py <port>

Handshake: validates presence of KALSHI-ACCESS-KEY/SIGNATURE/TIMESTAMP headers,
completes the Sec-WebSocket-Accept upgrade. Then per connection:
  - on a `subscribe` command -> reply `subscribed`, then `orderbook_snapshot`
    (seq 1) and two `orderbook_delta`s (seq 2,3);
  - send a PING with body "heartbeat" and assert the client PONGs it back with
    the same payload (RFC echo) — prints "HEARTBEAT_OK";
  - accept a clean close.

This is test scaffolding; deployment talks to real Kalshi over wss://.
"""
import base64
import hashlib
import socket
import struct
import sys
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def recv_frame(conn):
    hdr = conn.recv(2)
    if len(hdr) < 2:
        return None, None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", conn.recv(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", conn.recv(8))[0]
    mask = conn.recv(4) if masked else b"\x00\x00\x00\x00"
    data = b""
    while len(data) < ln:
        chunk = conn.recv(ln - len(data))
        if not chunk:
            break
        data += chunk
    if masked:
        data = bytes(data[i] ^ mask[i % 4] for i in range(len(data)))
    return opcode, data


def send_frame(conn, opcode, payload=b""):
    b1 = 0x80 | opcode
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", b1, n)
    elif n < 65536:
        header = struct.pack("!BBH", b1, 126, n)
    else:
        header = struct.pack("!BBQ", b1, 127, n)
    conn.sendall(header + payload)  # server frames are unmasked


def send_text(conn, s):
    send_frame(conn, 0x1, s.encode())


def handle(conn):
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(1024)
            if not chunk:
                return
            req += chunk
        lines = req.decode("latin1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        # auth header presence check (I10)
        for h in ("kalshi-access-key", "kalshi-access-signature", "kalshi-access-timestamp"):
            if h not in headers:
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
                print(f"MISSING_AUTH_HEADER {h}", flush=True)
                return
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        conn.sendall(
            ("HTTP/1.1 101 Switching Protocols\r\n"
             "Upgrade: websocket\r\nConnection: Upgrade\r\n"
             f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        print("HANDSHAKE_OK", flush=True)

        subscribed = False
        while True:
            opcode, data = recv_frame(conn)
            if opcode is None or opcode == 0x8:  # closed
                break
            if opcode == 0xA:  # PONG
                if data == b"heartbeat":
                    print("HEARTBEAT_OK", flush=True)
                continue
            if opcode == 0x1:  # text (a command)
                text = data.decode()
                if '"cmd":"subscribe"' in text and not subscribed:
                    subscribed = True
                    send_text(conn, '{"id":1,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":7}}')
                    send_text(conn, '{"type":"orderbook_snapshot","sid":7,"seq":1,"msg":{"market_ticker":"MKT-A","yes_dollars_fp":[["0.4000","500.00"]],"no_dollars_fp":[["0.5500","100.00"]]}}')
                    send_text(conn, '{"type":"orderbook_delta","sid":7,"seq":2,"msg":{"market_ticker":"MKT-A","price_dollars":"0.4200","delta_fp":"300.00","side":"yes"}}')
                    send_text(conn, '{"type":"orderbook_delta","sid":7,"seq":3,"msg":{"market_ticker":"MKT-A","price_dollars":"0.5500","delta_fp":"-100.00","side":"no"}}')
                    print("SENT_BOOK", flush=True)
                    # heartbeat: server pings with "heartbeat" payload
                    send_frame(conn, 0x9, b"heartbeat")
    except OSError:
        pass
    finally:
        conn.close()


def main():
    port = int(sys.argv[1])
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    print(f"mock_ws_exchange on 127.0.0.1:{port}", flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
