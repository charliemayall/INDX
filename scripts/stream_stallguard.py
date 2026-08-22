#!/usr/bin/env python3
"""
# INDX - stream TMC StallGuard / CoolStep current from the printer
#
# Copyright (C) 2026 Charlie Mayall
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""

# Subscribe to Klipper's existing tmc/stallguard_dump over Moonraker's
# klippysocket bridge. Writes CSV locally. No extra on the printer.
#
# Use M83 (relative E). G91 alone does not make E relative - default is M82.
# F300 (5 mm/s) is too slow for TMC2240 StallGuard2; expect sg_result=0 for
# the whole move. Prefer F1200+ (20 mm/s+) for a live reading.
#
#   python3 scripts/stream_stallguard.py --self-check
#   python3 scripts/stream_stallguard.py --arm --distance 40 --feed 1200 \
#     --out ./sg-loaded.csv
#
# Nozzle must already be hot enough to extrude (or test empty gears cold
# on purpose). Yank mid-move for runout; Ctrl-C also stops cleanly.

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time
from collections import deque
from typing import Any, Callable
from urllib.parse import urlencode

DEFAULT_HOST = "bigtreetech-cb1.local"
DEFAULT_PORT = 7125
TCOOLTHRS_ON = 0xFFFFF
CSV_HEADER = "time,sg_result,cs_actual"
# NB: below ~10 mm/s SG2 on the DX often reads 0 for the whole move.
MIN_FEED_WARN = 600.0
DEFAULT_FEED = 1200.0
DEFAULT_DISTANCE = 40.0


class Acc:
    __slots__ = ("n", "total", "lo", "hi")

    def __init__(self) -> None:
        self.n = 0
        self.total = 0.0
        self.lo: float | None = None
        self.hi: float | None = None

    def add(self, value: float) -> None:
        self.n += 1
        self.total += value
        if self.lo is None or value < self.lo:
            self.lo = value
        if self.hi is None or value > self.hi:
            self.hi = value

    def mean(self) -> float | None:
        if not self.n:
            return None
        return self.total / self.n


def format_acc(acc: Acc) -> str:
    if not acc.n or acc.lo is None or acc.hi is None:
        return "n=0"
    mean = acc.mean()
    assert mean is not None
    return "n=%d min=%.0f max=%.0f mean=%.1f" % (acc.n, acc.lo, acc.hi, mean)


def rows_from_dump(msg: dict[str, Any]) -> list[tuple[float, int, int]]:
    params = msg.get("params")
    if not isinstance(params, dict):
        return []
    data = params.get("data") or []
    rows = []
    for item in data:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        rows.append((float(item[0]), int(item[1]), int(item[2])))
    return rows


def build_extrude_script(distance: float, feed: float) -> str:
    # M83: relative E. G91 alone leaves E absolute (M82).
    # M400: wait until the move finishes so the script response brackets
    # the motion. M82: restore absolute E for the rest of the printer.
    return "M83\nG1 E%.3f F%.1f\nM400\nM82\n" % (distance, feed)


class WebSocket:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buf = bytearray()

    def send_json(self, obj: dict[str, Any]) -> None:
        payload = json.dumps(obj, separators=(",", ":")).encode()
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", n))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", n))
        self.sock.sendall(header + mask + masked)

    def recv_json(self) -> dict[str, Any] | None:
        while True:
            opcode, payload = self._recv_frame()
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send_pong(payload)
                continue
            if opcode == 0xA:
                continue
            if opcode != 0x1 and opcode != 0x2:
                continue
            return json.loads(payload.decode())

    def close(self) -> None:
        try:
            self.sock.sendall(b"\x88\x80\x00\x00\x00\x00")
        except OSError:
            pass
        self.sock.close()

    def _send_pong(self, payload: bytes) -> None:
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        if n >= 126:
            return
        self.sock.sendall(bytes([0x8A, 0x80 | n]) + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        hdr = self._read_exact(2)
        opcode = hdr[0] & 0x0F
        masked = bool(hdr[1] & 0x80)
        n = hdr[1] & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(n)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket closed")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


def ws_handshake(
    host: str, port: int, path: str, api_key: str | None
) -> WebSocket:
    sock = socket.create_connection((host, port), timeout=10)
    sock.settimeout(30)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [
        "GET %s HTTP/1.1" % path,
        "Host: %s:%d" % (host, port),
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Key: %s" % key,
        "Sec-WebSocket-Version: 13",
    ]
    if api_key:
        lines.append("X-Api-Key: %s" % api_key)
    lines.extend(["", ""])
    sock.sendall("\r\n".join(lines).encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("no websocket handshake")
        resp += chunk
    head = resp.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", "replace")
    if " 101 " not in head.split("\r\n", 1)[0]:
        sock.close()
        raise ConnectionError(
            "websocket handshake failed: %s" % head.split("\r\n", 1)[0]
        )
    expect = base64.b64encode(
        hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()
    ).decode()
    accept = ""
    for line in head.split("\r\n")[1:]:
        name, _, value = line.partition(":")
        if name.lower() == "sec-websocket-accept":
            accept = value.strip()
    if accept != expect:
        sock.close()
        raise ConnectionError("bad Sec-WebSocket-Accept")
    leftover = resp.split(b"\r\n\r\n", 1)[1]
    ws = WebSocket(sock)
    ws._buf.extend(leftover)
    return ws


def connect_klippy(host: str, port: int, api_key: str | None) -> WebSocket:
    path = "/klippysocket"
    if api_key:
        path += "?" + urlencode({"token": api_key})
    return ws_handshake(host, port, path, api_key)


def send_gcode(ws: WebSocket, script: str, req_id: int) -> None:
    ws.send_json(
        {"id": req_id, "method": "gcode/script", "params": {"script": script}}
    )


def wait_id(
    ws: WebSocket,
    req_id: int,
    on_other: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    while True:
        msg = ws.recv_json()
        if msg is None:
            raise ConnectionError("socket closed waiting for id %s" % req_id)
        if msg.get("id") == req_id:
            if "error" in msg:
                err = msg["error"]
                text = err.get("message", err) if isinstance(err, dict) else err
                raise RuntimeError(str(text))
            return msg
        if on_other is not None:
            on_other(msg)


class SampleWriter:
    def __init__(self, out) -> None:
        self.out = out
        self.sg_acc = Acc()
        self.cs_acc = Acc()
        self.recent: deque[tuple[float, int, int]] = deque()
        self.n_rows = 0
        self.last_report = time.time()

    def ingest(self, msg: dict[str, Any]) -> None:
        rows = rows_from_dump(msg)
        for t, sg, cs in rows:
            self.n_rows += 1
            if sg >= 0:
                self.sg_acc.add(sg)
            if cs >= 0:
                self.cs_acc.add(cs)
            self.recent.append((t, sg, cs))
            self.out.write("%.4f,%d,%d\n" % (t, sg, cs))
        while self.recent and self.recent[-1][0] - self.recent[0][0] > 2.0:
            self.recent.popleft()
        now = time.time()
        if now - self.last_report >= 0.5:
            self.last_report = now
            win = Acc()
            for _, sg, _ in self.recent:
                if sg >= 0:
                    win.add(sg)
            sys.stderr.write(
                "\r%d samples  sg %s  window %s  cs %s"
                % (
                    self.n_rows,
                    format_acc(self.sg_acc),
                    format_acc(win),
                    format_acc(self.cs_acc),
                )
            )
            sys.stderr.flush()

    def finish(self) -> None:
        sys.stderr.write(
            "\nsg %s  cs %s\n"
            % (format_acc(self.sg_acc), format_acc(self.cs_acc))
        )


def self_check() -> None:
    rows = rows_from_dump(
        {"params": {"data": [[1.0, 400, 16], [1.01, 80, 16], ["bad"], [1.02]]}}
    )
    assert rows == [(1.0, 400, 16), (1.01, 80, 16)], rows
    acc = Acc()
    for _, sg, _ in rows:
        acc.add(sg)
    assert acc.n == 2 and acc.lo == 80 and acc.hi == 400
    assert "mean=240.0" in format_acc(acc)
    script = build_extrude_script(40.0, 1200.0)
    assert "M83" in script and "M82" in script and "M400" in script
    assert "G1 E40.000 F1200.0" in script
    print("self-check ok")


def resolve_gcode(args: argparse.Namespace) -> tuple[str, float]:
    """Return (script, suggested_seconds). suggested_seconds is 0 if unknown."""
    if args.gcode:
        return args.gcode, 0.0
    if args.distance is not None or args.feed is not None:
        distance = (
            args.distance if args.distance is not None else DEFAULT_DISTANCE
        )
        feed = args.feed if args.feed is not None else DEFAULT_FEED
        if feed < MIN_FEED_WARN:
            sys.stderr.write(
                "warning: F%.0f is %.1f mm/s - StallGuard2 often reads 0 "
                "this slow; try --feed %.0f or higher\n"
                % (feed, feed / 60.0, DEFAULT_FEED)
            )
        move_s = abs(distance) / (feed / 60.0)
        return build_extrude_script(distance, feed), move_s + 3.0
    return "", 0.0


def stream(args: argparse.Namespace) -> int:
    api_key = args.api_key or os.environ.get("MOONRAKER_API_KEY")
    script, suggest_s = resolve_gcode(args)
    seconds = args.seconds
    if seconds <= 0 and suggest_s > 0:
        seconds = suggest_s
        sys.stderr.write(
            "recording for %.1fs (move + margin); override with --seconds\n"
            % seconds
        )

    ws = connect_klippy(args.host, args.port, api_key)
    armed = False
    out = sys.stdout if args.out == "-" else open(args.out, "w", buffering=1)
    writer = SampleWriter(out)
    try:
        if args.arm:
            send_gcode(
                ws,
                "SET_TMC_FIELD STEPPER=%s FIELD=tcoolthrs VALUE=%d"
                % (args.stepper, TCOOLTHRS_ON),
                1,
            )
            wait_id(ws, 1)
            armed = True
            sys.stderr.write(
                "armed TCOOLTHRS=%d on %s (restored to 0 on exit)\n"
                % (TCOOLTHRS_ON, args.stepper)
            )
        else:
            sys.stderr.write(
                "warning: without --arm, TCOOLTHRS stays 0 and SG is blind\n"
            )

        ws.send_json(
            {
                "id": 2,
                "method": "tmc/stallguard_dump",
                "params": {
                    "name": args.stepper,
                    "response_template": {"key": "sg"},
                },
            }
        )
        hello = wait_id(ws, 2, on_other=writer.ingest)
        header = hello.get("result", {}).get("header")
        sys.stderr.write("subscribed %s header=%s\n" % (args.stepper, header))

        out.write(CSV_HEADER + "\n")
        if out is sys.stdout:
            out.flush()

        t0 = time.time()
        deadline = None if seconds <= 0 else t0 + seconds

        if script:
            sys.stderr.write("gcode:\n%s" % script)
            send_gcode(ws, script, 3)
            # Ingest dump frames while M400 waits - that is the move window.
            wait_id(ws, 3, on_other=writer.ingest)
            sys.stderr.write("gcode finished\n")

        while True:
            now = time.time()
            if deadline is not None and now >= deadline:
                break
            remain = 1.0
            if deadline is not None:
                remain = max(0.05, deadline - now)
            ws.sock.settimeout(remain)
            try:
                msg = ws.recv_json()
            except socket.timeout:
                continue
            if msg is None:
                break
            if msg.get("id") == 3 and "error" in msg:
                raise RuntimeError(msg["error"])
            writer.ingest(msg)

        writer.finish()
        if writer.n_rows and writer.sg_acc.n and writer.sg_acc.hi == 0:
            sys.stderr.write(
                "note: every sg_result was 0 - speed too low, TCOOLTHRS off, "
                "or motor not stepping\n"
            )
        return 0
    finally:
        if armed:
            try:
                send_gcode(
                    ws,
                    "SET_TMC_FIELD STEPPER=%s FIELD=tcoolthrs VALUE=0"
                    % args.stepper,
                    99,
                )
                wait_id(ws, 99)
                sys.stderr.write("restored TCOOLTHRS=0\n")
            except Exception as exc:
                sys.stderr.write("failed to restore TCOOLTHRS: %s\n" % exc)
        if out is not sys.stdout:
            out.close()
        ws.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stream TMC stallguard (sg_result, cs_actual) to this machine. "
            "Prefer --arm --distance/--feed over raw --gcode."
        )
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--stepper", default="extruder")
    parser.add_argument("--out", default="-", help="CSV path, or - for stdout")
    parser.add_argument(
        "--seconds",
        type=float,
        default=0,
        help="record length; 0 = until Ctrl-C, or move+margin with --distance",
    )
    parser.add_argument(
        "--arm",
        action="store_true",
        help="SET_TMC_FIELD tcoolthrs so StallGuard can update while moving",
    )
    parser.add_argument(
        "--distance",
        type=float,
        default=None,
        help="relative E mm (implies M83 / G1 / M400 / M82)",
    )
    parser.add_argument(
        "--feed",
        type=float,
        default=None,
        help="F in mm/min (default %.0f = %.0f mm/s)"
        % (DEFAULT_FEED, DEFAULT_FEED / 60.0),
    )
    parser.add_argument(
        "--gcode",
        default="",
        help="raw script instead of --distance/--feed (must include M83)",
    )
    parser.add_argument("--api-key", default="")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0
    if args.gcode and (args.distance is not None or args.feed is not None):
        parser.error("use either --gcode or --distance/--feed, not both")
    try:
        return stream(args)
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
