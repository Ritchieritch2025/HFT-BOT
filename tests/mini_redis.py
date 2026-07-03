#!/usr/bin/env python3
"""Minimal RESP2-compatible in-memory server for local pipeline tests.

Implements just what the pipeline uses: PING, LPUSH, RPUSH, BRPOP, LLEN,
LRANGE, LTRIM, DEL, FLUSHALL, PUBLISH, SUBSCRIBE. Binary-safe values.
This is test scaffolding — deployment uses a real Redis.

Usage: mini_redis.py <port>
"""
import socketserver
import sys
import threading
import time
from collections import defaultdict, deque

lists = defaultdict(deque)
lists_cv = threading.Condition()
subscribers = defaultdict(list)  # channel -> [(wfile, lock)]
subs_lock = threading.Lock()


def resp_bulk(b):
    return b"$%d\r\n%s\r\n" % (len(b), b)


def resp_array(items):
    return b"*%d\r\n%s" % (len(items), b"".join(items))


class Handler(socketserver.StreamRequestHandler):
    def read_command(self):
        header = self.rfile.readline()
        if not header:
            return None
        if not header.startswith(b"*"):
            raise ValueError("inline commands unsupported")
        n = int(header[1:].strip())
        args = []
        for _ in range(n):
            lenline = self.rfile.readline()
            if not lenline.startswith(b"$"):
                raise ValueError("expected bulk string")
            ln = int(lenline[1:].strip())
            data = self.rfile.read(ln + 2)[:-2]
            args.append(data)
        return args

    def handle(self):
        self.wlock = threading.Lock()
        my_channels = []
        try:
            while True:
                args = self.read_command()
                if args is None:
                    break
                cmd = args[0].upper().decode()
                if cmd == "PING":
                    self.reply(b"+PONG\r\n")
                elif cmd in ("LPUSH", "RPUSH"):
                    key = args[1]
                    with lists_cv:
                        for v in args[2:]:
                            if cmd == "LPUSH":
                                lists[key].appendleft(v)
                            else:
                                lists[key].append(v)
                        n = len(lists[key])
                        lists_cv.notify_all()
                    self.reply(b":%d\r\n" % n)
                elif cmd == "BRPOP":
                    key, timeout = args[1], float(args[2])
                    deadline = None if timeout == 0 else time.monotonic() + timeout
                    val = None
                    with lists_cv:
                        while not lists.get(key):
                            remaining = None if deadline is None else deadline - time.monotonic()
                            if remaining is not None and remaining <= 0:
                                break
                            lists_cv.wait(remaining)
                        if lists.get(key):
                            val = lists[key].pop()
                    if val is None:
                        self.reply(b"*-1\r\n")
                    else:
                        self.reply(resp_array([resp_bulk(key), resp_bulk(val)]))
                elif cmd == "LLEN":
                    with lists_cv:
                        n = len(lists.get(args[1], ()))
                    self.reply(b":%d\r\n" % n)
                elif cmd == "LRANGE":
                    start, stop = int(args[2]), int(args[3])
                    with lists_cv:
                        items = list(lists.get(args[1], ()))
                    if stop == -1:
                        stop = len(items) - 1
                    sel = items[start:stop + 1]
                    self.reply(resp_array([resp_bulk(v) for v in sel]))
                elif cmd == "LTRIM":
                    start, stop = int(args[2]), int(args[3])
                    with lists_cv:
                        items = list(lists.get(args[1], ()))
                        if stop == -1:
                            stop = len(items) - 1
                        lists[args[1]] = deque(items[start:stop + 1])
                    self.reply(b"+OK\r\n")
                elif cmd == "DEL":
                    n = 0
                    with lists_cv:
                        for k in args[1:]:
                            if k in lists:
                                del lists[k]
                                n += 1
                    self.reply(b":%d\r\n" % n)
                elif cmd == "FLUSHALL":
                    with lists_cv:
                        lists.clear()
                    self.reply(b"+OK\r\n")
                elif cmd == "PUBLISH":
                    chan, msg = args[1], args[2]
                    frame = resp_array([resp_bulk(b"message"), resp_bulk(chan),
                                        resp_bulk(msg)])
                    with subs_lock:
                        targets = list(subscribers.get(chan, ()))
                    delivered = 0
                    for wfile, lock in targets:
                        try:
                            with lock:
                                wfile.write(frame)
                                wfile.flush()
                            delivered += 1
                        except OSError:
                            pass
                    self.reply(b":%d\r\n" % delivered)
                elif cmd == "SUBSCRIBE":
                    for chan in args[1:]:
                        with subs_lock:
                            subscribers[chan].append((self.wfile, self.wlock))
                        my_channels.append(chan)
                        self.reply(resp_array([resp_bulk(b"subscribe"),
                                               resp_bulk(chan),
                                               b":%d\r\n" % len(my_channels)]))
                else:
                    self.reply(b"-ERR unknown command '%s'\r\n" % cmd.encode())
        except (ValueError, OSError):
            pass
        finally:
            with subs_lock:
                for chan in my_channels:
                    subscribers[chan] = [
                        (w, l) for (w, l) in subscribers.get(chan, [])
                        if w is not self.wfile
                    ]

    def reply(self, data):
        with self.wlock:
            self.wfile.write(data)
            self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1])
    server = Server(("127.0.0.1", port), Handler)
    print(f"mini-redis on 127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
