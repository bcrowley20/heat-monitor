#!/usr/bin/env python3
"""Boiler pump monitor for a Raspberry Pi.

Watches three GPIO inputs, one per pump, and serves their state as JSON:

    GET /                              current state and the last 20 switches
    GET /events?after=N&boot=ID        switches numbered above N (what heatmon uses)

Each pump's 5 V USB adapter feeds its pin through a 10k/10k divider, so a running pump
reads high. A change counts only after it has held for SETTLE_S, and it is timed from
when it was first seen. Needs only Python 3 and gpiozero, which Raspberry Pi OS includes.
"""

import json
import os
import secrets
import threading
import time
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PUMPS = {"boiler": 22, "system": 23, "dhw": 24}  # name -> BCM GPIO number
PORT = 8081
SAMPLE_S = 0.02
SETTLE_S = 0.2
MAX_EVENTS = 10_000  # switches kept in memory; heatmon notices if it falls further behind
PAGE = 1000  # switches per /events reply


class Monitor:
    """Debounces the pump inputs and keeps a numbered log of switches.

    `read(name)` returns True when that pump is running. Times are from `clock`, a
    monotonic clock, so a wrong wall clock never shifts them; heatmon converts them.
    """

    def __init__(self, names, read, clock=time.monotonic, wall=time.time):
        self.read, self.clock, self.wall = read, clock, wall
        self.boot_id = secrets.token_hex(8)
        self.lock = threading.Lock()
        self.events = deque(maxlen=MAX_EVENTS)
        self.seq = 0
        self.state = {}  # name -> (on, since)
        self.pending = {}  # name -> (value, first seen)
        now = clock()
        for name in names:
            on = bool(read(name))
            self.state[name] = (on, now)
            self._add(name, on, now, start=True)

    def _add(self, name, on, at, start=False):
        self.seq += 1
        self.events.append({"seq": self.seq, "pump": name, "on": on, "mono": at, "start": start})

    def sample(self):
        now = self.clock()
        with self.lock:
            for name, (on, _) in self.state.items():
                v = bool(self.read(name))
                if v == on:
                    self.pending.pop(name, None)
                    continue
                seen = self.pending.get(name)
                if seen is None or seen[0] != v:
                    self.pending[name] = (v, now)
                elif now - seen[1] >= SETTLE_S:
                    self.state[name] = (v, seen[1])
                    self._add(name, v, seen[1])
                    del self.pending[name]

    def snapshot(self, after=None, boot=None):
        """State and switches as a JSON-ready dict. With `after`, switches numbered above it;
        if `boot` isn't this run's ID, the caller has missed a restart and gets all of them."""
        with self.lock:
            mono, wall = self.clock(), self.wall()

            def iso(m):
                return datetime.fromtimestamp(wall - (mono - m), UTC).isoformat(timespec="milliseconds")

            if after is None:
                events, more = list(self.events)[-20:], False
            else:
                if boot != self.boot_id:
                    after = 0
                newer = [e for e in self.events if e["seq"] > after]
                events, more = newer[:PAGE], len(newer) > PAGE
            return {
                "boot_id": self.boot_id,
                "mono_now": mono,
                "time": iso(mono),
                "pumps": {n: {"on": on, "since_mono": s, "since": iso(s), "for_s": round(mono - s, 1)}
                          for n, (on, s) in self.state.items()},
                "first_seq": self.events[0]["seq"] if self.events else 0,
                "last_seq": self.seq,
                "more": more,
                "events": [{**e, "time": iso(e["mono"])} for e in events],
            }


def serve(monitor, port=PORT):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path == "/":
                    body = monitor.snapshot()
                elif url.path == "/events":
                    body = monitor.snapshot(int(q.get("after", ["0"])[0]), q.get("boot", [None])[0])
                else:
                    return self.send_error(404)
            except ValueError:
                return self.send_error(400, "after must be a whole number")
            data = json.dumps(body, indent=1).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass  # a request every few seconds would fill the journal

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


def main():
    from gpiozero import DigitalInputDevice  # only on the Pi, so tests can import this file

    # pull_up=None: the divider's bottom resistor holds the pin low when a pump is off.
    pins = {n: DigitalInputDevice(p, pull_up=None, active_state=True) for n, p in PUMPS.items()}
    monitor = Monitor(PUMPS, lambda n: pins[n].value)

    def loop():
        try:
            while True:
                monitor.sample()
                time.sleep(SAMPLE_S)
        except Exception as e:  # never keep serving a frozen state: exit and let systemd restart us
            print(f"Reading the pins failed: {e!r}", flush=True)
            os._exit(1)

    threading.Thread(target=loop, daemon=True).start()
    print(f"Pump monitor on port {PORT}, pins {PUMPS}", flush=True)
    serve(monitor)


if __name__ == "__main__":
    main()
