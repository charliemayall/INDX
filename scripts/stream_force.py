#!/usr/bin/env python3
"""
# INDX - stream load-cell force from the printer
#
# Copyright (C) 2026 Charlie Mayall
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""

# Subscribe to Klipper's load_cell/dump_force over Moonraker's klippysocket
# bridge. Writes CSV locally. No extra on the printer.
#
# Use M83 (relative E). G91 alone does not make E relative - default is M82.
# Continuous detection should be off. Do not home or probe between loaded
# and empty passes (Z probe re-tares the cell).
#
#   python3 scripts/stream_force.py --self-check
#   python3 scripts/stream_force.py --ladder --state loaded --out-dir ./force-traces \
#     --tool 2 --material PLA --temp 210
#   python3 scripts/stream_force.py --state empty --speed 4 --distance 40
#   python3 scripts/stream_force.py --listen --out ./force-traces/print.csv
#
# Nozzle must already be hot enough to extrude (or test empty gears on
# purpose). Over the purge bucket. Ctrl-C stops cleanly.

from __future__ import annotations

import argparse
import csv
import math
import os
import socket
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from typing import Any, Sequence, TextIO

from stream_stallguard import (
    Acc,
    connect_klippy,
    send_gcode,
    wait_id,
)

DEFAULT_HOST = "bigtreetech-cb1.local"
DEFAULT_PORT = 7125
DEFAULT_SENSOR = "load_cell_probe"
DEFAULT_DISTANCE = 40.0
DEFAULT_SPEEDS = (0.5, 1.0, 2.0, 4.0, 7.0, 10.0)
DEFAULT_PAUSE_S = 0.5
DETECTION_LENGTH_MM = 4.0
SKIP_MM = 8.0
CSV_HEADER = "time,force_g,counts,tare_counts"
MANIFEST_FIELDS = (
    "file",
    "tool",
    "material",
    "temp_c",
    "e_mm_s",
    "state",
    "distance_mm",
    "notes",
)
STATES = ("loaded", "empty")


def rows_from_dump(msg: dict[str, Any]) -> list[tuple[float, float, int, int]]:
    params = msg.get("params")
    if not isinstance(params, dict):
        return []
    data = params.get("data") or []
    rows = []
    for item in data:
        if not isinstance(item, (list, tuple)) or len(item) < 4:
            continue
        try:
            rows.append(
                (float(item[0]), float(item[1]), int(item[2]), int(item[3]))
            )
        except (TypeError, ValueError):
            continue
    return rows


def build_extrude_script(distance: float, speed_mm_s: float) -> str:
    feed = speed_mm_s * 60.0
    return "M83\nG1 E%.3f F%.1f\nM400\nM82\n" % (distance, feed)


def force_csv_name(state: str, speed_mm_s: float) -> str:
    return "force-%s-%gmms.csv" % (state, speed_mm_s)


def parse_speeds(text: str) -> tuple[float, ...]:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise ValueError("need at least one speed")
    speeds = tuple(float(p) for p in parts)
    if any(s <= 0.0 for s in speeds):
        raise ValueError("speeds must be positive")
    return speeds


def format_force_acc(acc: Acc) -> str:
    if not acc.n or acc.lo is None or acc.hi is None:
        return "n=0"
    mean = acc.mean()
    assert mean is not None
    return "n=%d min=%.1f max=%.1f mean=%.1f" % (acc.n, acc.lo, acc.hi, mean)


def pctl(vals: Sequence[float], p: float) -> float | None:
    if not vals:
        return None
    ordered = sorted(vals)
    idx = int(round((p / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, idx))]


def window_means(
    times: Sequence[float],
    forces: Sequence[float],
    speed_mm_s: float,
    *,
    ref: float = 0.0,
    detection_length: float = DETECTION_LENGTH_MM,
    skip_mm: float = SKIP_MM,
) -> list[float]:
    """Mean |F - ref| per detection_length mm of commanded E.

    Matches filament_force window level (grams of pull, not signed counts).
    The cell often sits at ~-2400 g tare; empty vs loaded is the delta.

    NB: E(t) = speed * (t - t0) assumes samples are only the constant-speed
    move. Accel at the start is the ceiling; skip_mm drops it.
    """
    if speed_mm_s <= 0.0 or detection_length <= 0.0:
        return []
    if len(times) < 2 or len(times) != len(forces):
        return []
    t0 = times[0]
    buckets: dict[int, list[float]] = {}
    for t, force in zip(times, forces):
        e_mm = (t - t0) * speed_mm_s
        if e_mm < skip_mm:
            continue
        idx = int((e_mm - skip_mm) / detection_length)
        buckets.setdefault(idx, []).append(abs(force - ref))
    return [statistics.fmean(vals) for _, vals in sorted(buckets.items()) if vals]


def fisher_snr(loaded: Sequence[float], empty: Sequence[float]) -> float | None:
    if len(loaded) < 2 or len(empty) < 2:
        return None
    mu_l = statistics.fmean(loaded)
    mu_e = statistics.fmean(empty)
    var_l = statistics.pvariance(loaded)
    var_e = statistics.pvariance(empty)
    denom = math.sqrt(var_l + var_e)
    if denom < 1e-9:
        return None if abs(mu_l - mu_e) < 1e-9 else float("inf")
    return (mu_l - mu_e) / denom


def append_manifest(path: str, row: dict[str, object]) -> None:
    exists = os.path.isfile(path)
    with open(path, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANIFEST_FIELDS))
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in MANIFEST_FIELDS})


class SampleWriter:
    def __init__(self, out: TextIO) -> None:
        self.out = out
        self.force_acc = Acc()
        self.n_rows = 0
        self.last_report = time.time()

    def ingest(self, msg: dict[str, Any]) -> None:
        rows = rows_from_dump(msg)
        for t, force, counts, tare in rows:
            self.n_rows += 1
            self.force_acc.add(force)
            self.out.write("%.4f,%.3f,%d,%d\n" % (t, force, counts, tare))
        now = time.time()
        if now - self.last_report >= 0.5:
            self.last_report = now
            sys.stderr.write(
                "\r%d samples  force %s"
                % (self.n_rows, format_force_acc(self.force_acc))
            )
            sys.stderr.flush()

    def finish(self) -> None:
        sys.stderr.write("\nforce %s\n" % format_force_acc(self.force_acc))


def discard(_msg: dict[str, Any]) -> None:
    return None


def record_stream(ws: Any, writer: SampleWriter, seconds: float) -> None:
    deadline = None if seconds <= 0 else time.time() + seconds
    while True:
        now = time.time()
        if deadline is not None and now >= deadline:
            break
        remain = 1.0 if deadline is None else max(0.05, deadline - now)
        ws.sock.settimeout(remain)
        try:
            msg = ws.recv_json()
        except socket.timeout:
            continue
        if msg is None:
            break
        writer.ingest(msg)
    writer.finish()


class ReqIds:
    def __init__(self, start: int = 10) -> None:
        self.n = start

    def next(self) -> int:
        self.n += 1
        return self.n


def subscribe_force(ws: Any, sensor: str, ids: ReqIds, ingest) -> Any:
    req_id = ids.next()
    ws.send_json(
        {
            "id": req_id,
            "method": "load_cell/dump_force",
            "params": {
                "load_cell": sensor,
                "response_template": {"key": "force"},
            },
        }
    )
    hello = wait_id(ws, req_id, on_other=ingest)
    header = hello.get("result", {}).get("header")
    sys.stderr.write("subscribed %s header=%s\n" % (sensor, header))
    return hello


def record_move(
    ws: Any,
    ids: ReqIds,
    out_path: str,
    script: str,
) -> SampleWriter:
    out = open(out_path, "w", buffering=1)
    writer = SampleWriter(out)
    try:
        out.write(CSV_HEADER + "\n")
        sys.stderr.write("gcode:\n%s" % script)
        req_id = ids.next()
        send_gcode(ws, script, req_id)
        wait_id(ws, req_id, on_other=writer.ingest)
        sys.stderr.write("gcode finished\n")
        writer.finish()
        return writer
    finally:
        out.close()


def dwell(ws: Any, ids: ReqIds, pause_s: float) -> None:
    if pause_s <= 0.0:
        return
    req_id = ids.next()
    send_gcode(ws, "G4 P%.0f\n" % (pause_s * 1000.0), req_id)
    wait_id(ws, req_id, on_other=discard)


def meta_from_args(
    args: argparse.Namespace, *, speed: float | None, out_path: str
) -> dict[str, object]:
    return {
        "file": os.path.basename(out_path),
        "tool": "" if args.tool is None else args.tool,
        "material": args.material,
        "temp_c": "" if args.temp is None else args.temp,
        "e_mm_s": "" if speed is None else "%.4g" % speed,
        "state": args.state or "",
        "distance_mm": "" if args.distance is None else "%.3g" % args.distance,
        "notes": args.notes,
    }


def stream(args: argparse.Namespace) -> int:
    api_key = args.api_key or os.environ.get("MOONRAKER_API_KEY")
    ws = connect_klippy(args.host, args.port, api_key)
    ids = ReqIds()
    try:
        subscribe_force(ws, args.sensor, ids, discard)
        if args.ladder:
            out_dir = args.out_dir
            os.makedirs(out_dir, exist_ok=True)
            manifest = args.manifest or os.path.join(out_dir, "runs.csv")
            for i, speed in enumerate(args.speeds):
                if i:
                    dwell(ws, ids, args.pause_s)
                name = force_csv_name(args.state, speed)
                path = os.path.join(out_dir, name)
                script = build_extrude_script(args.distance, speed)
                record_move(ws, ids, path, script)
                append_manifest(manifest, meta_from_args(args, speed=speed, out_path=path))
                sys.stderr.write("wrote %s\n" % path)
            sys.stderr.write("manifest %s\n" % manifest)
            return 0

        if args.listen:
            out_path = args.out or "force-print.csv"
            sys.stderr.write(
                "listening (Ctrl-C to stop)%s\n"
                % (
                    ""
                    if args.seconds <= 0
                    else " for %.1fs" % args.seconds
                )
            )
            if out_path == "-":
                writer = SampleWriter(sys.stdout)
                sys.stdout.write(CSV_HEADER + "\n")
                sys.stdout.flush()
                record_stream(ws, writer, args.seconds)
                return 0
            out = open(out_path, "w", buffering=1)
            writer = SampleWriter(out)
            try:
                out.write(CSV_HEADER + "\n")
                record_stream(ws, writer, args.seconds)
            finally:
                out.close()
            sys.stderr.write("wrote %s\n" % out_path)
            return 0

        speed = args.speed
        if args.gcode:
            script = args.gcode if args.gcode.endswith("\n") else args.gcode + "\n"
        else:
            if speed is None:
                raise SystemExit("need --speed (mm/s), --ladder, or --gcode")
            script = build_extrude_script(args.distance, speed)

        out_path = args.out
        if not out_path:
            if not args.state or speed is None:
                out_path = "-"
            else:
                out_path = force_csv_name(args.state, speed)

        if out_path == "-":
            writer = SampleWriter(sys.stdout)
            sys.stdout.write(CSV_HEADER + "\n")
            sys.stdout.flush()
            sys.stderr.write("gcode:\n%s" % script)
            req_id = ids.next()
            send_gcode(ws, script, req_id)
            wait_id(ws, req_id, on_other=writer.ingest)
            sys.stderr.write("gcode finished\n")
            if args.gcode and args.seconds > 0:
                deadline = time.time() + args.seconds
                while time.time() < deadline:
                    remain = max(0.05, deadline - time.time())
                    ws.sock.settimeout(remain)
                    try:
                        msg = ws.recv_json()
                    except socket.timeout:
                        continue
                    if msg is None:
                        break
                    writer.ingest(msg)
            writer.finish()
            return 0

        record_move(ws, ids, out_path, script)
        if args.manifest or args.state:
            manifest = args.manifest or os.path.join(
                os.path.dirname(os.path.abspath(out_path)) or ".", "runs.csv"
            )
            append_manifest(
                manifest, meta_from_args(args, speed=speed, out_path=out_path)
            )
            sys.stderr.write("wrote %s  manifest %s\n" % (out_path, manifest))
        else:
            sys.stderr.write("wrote %s\n" % out_path)
        return 0
    finally:
        ws.close()


def load_csv(path: str) -> tuple[list[float], list[float]]:
    times: list[float] = []
    forces: list[float] = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            try:
                times.append(float(row["time"]))
                forces.append(float(row["force_g"]))
            except (KeyError, TypeError, ValueError):
                continue
    return times, forces


def parse_name_meta(filename: str) -> tuple[str, float] | None:
    base = os.path.basename(filename)
    if not base.startswith("force-") or not base.endswith("mms.csv"):
        return None
    body = base[len("force-") : -len("mms.csv")]
    state, sep, speed_s = body.rpartition("-")
    if not sep or state not in STATES:
        return None
    try:
        return state, float(speed_s)
    except ValueError:
        return None


def summarise(dir_path: str) -> int:
    manifest_path = os.path.join(dir_path, "runs.csv")
    rows: list[dict[str, str]] = []
    if os.path.isfile(manifest_path):
        with open(manifest_path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    if not rows:
        for name in sorted(os.listdir(dir_path)):
            parsed = parse_name_meta(name)
            if parsed is None:
                continue
            state, speed = parsed
            rows.append(
                {
                    "file": name,
                    "state": state,
                    "e_mm_s": "%.4g" % speed,
                }
            )
    if not rows:
        sys.stderr.write("no force-*.csv or runs.csv in %s\n" % dir_path)
        return 1

    traces: list[tuple[str, float, list[float], list[float]]] = []
    empty_samples: dict[float, list[float]] = defaultdict(list)
    for row in rows:
        state = (row.get("state") or "").strip()
        try:
            speed = float(row.get("e_mm_s") or "")
        except ValueError:
            continue
        if state not in STATES or speed <= 0.0:
            continue
        path = os.path.join(dir_path, row["file"])
        if not os.path.isfile(path):
            sys.stderr.write("missing %s\n" % path)
            continue
        times, forces = load_csv(path)
        traces.append((state, speed, times, forces))
        if state == "empty" and forces:
            empty_samples[speed].extend(forces)

    refs: dict[float, float] = {
        speed: statistics.fmean(vals)
        for speed, vals in empty_samples.items()
        if vals
    }
    grouped: dict[tuple[str, float], list[float]] = defaultdict(list)
    for state, speed, times, forces in traces:
        ref = refs.get(speed)
        if ref is None and times:
            # No empty pass: lock ref on the first 0.15 s (filament_force baseline_time).
            t_cut = times[0] + 0.15
            head = [f for t, f in zip(times, forces) if t <= t_cut] or forces[:1]
            ref = statistics.fmean(head)
            refs[speed] = ref
        grouped[(state, speed)].extend(
            window_means(times, forces, speed, ref=0.0 if ref is None else ref)
        )

    speeds = sorted({speed for _, speed in grouped})
    print(
        "speed  ref_g  n_L  n_E  μ_L  σ_L  μ_E  σ_E  Fisher  empty_p95/μ_L  empty_p95  pass"
    )
    any_cruise_fail = False
    for speed in speeds:
        loaded = grouped.get(("loaded", speed), [])
        empty = grouped.get(("empty", speed), [])
        mu_l = statistics.fmean(loaded) if loaded else None
        mu_e = statistics.fmean(empty) if empty else None
        sd_l = statistics.pstdev(loaded) if len(loaded) > 1 else None
        sd_e = statistics.pstdev(empty) if len(empty) > 1 else None
        snr = fisher_snr(loaded, empty)
        p95 = pctl(empty, 95.0)
        ratio = (
            (p95 / mu_l)
            if p95 is not None and mu_l is not None and mu_l > 1e-9
            else None
        )
        cruise = speed >= 2.0
        passed = None
        if mu_l is not None and p95 is not None and snr is not None:
            passed = (
                mu_l > 0.0
                and snr >= 3.0
                and p95 <= min(0.35 * mu_l, 80.0)
            )
            if cruise and not passed:
                any_cruise_fail = True

        def fmt(v: float | None, spec: str) -> str:
            if v is None:
                return "-"
            if math.isinf(v):
                return "inf"
            return format(v, spec)

        print(
            "%5g  %7s  %3d  %3d  %s  %s  %s  %s  %s  %s  %s  %s"
            % (
                speed,
                fmt(refs.get(speed), ".1f"),
                len(loaded),
                len(empty),
                fmt(mu_l, ".1f"),
                fmt(sd_l, ".1f"),
                fmt(mu_e, ".1f"),
                fmt(sd_e, ".1f"),
                fmt(snr, ".2f"),
                fmt(ratio, ".2f"),
                fmt(p95, ".1f"),
                "-" if passed is None else ("yes" if passed else "NO"),
            )
        )
    fit_x = []
    fit_y = []
    for speed in speeds:
        loaded = grouped.get(("loaded", speed), [])
        if loaded:
            fit_x.append(speed)
            fit_y.append(statistics.fmean(loaded))
    if len(fit_x) >= 2:
        mx = statistics.fmean(fit_x)
        my = statistics.fmean(fit_y)
        den = sum((x - mx) ** 2 for x in fit_x)
        if den > 1e-9:
            b = sum((x - mx) * (y - my) for x, y in zip(fit_x, fit_y)) / den
            a = my - b * mx
            sys.stderr.write(
                "option B: |F-ref| ≈ %.1f + %.1f * e_speed g  (loaded windows)\n"
                % (a, b)
            )
    if any_cruise_fail:
        sys.stderr.write(
            "fail at cruise (>=2 mm/s): sensing / gating, not option B\n"
        )
        return 2
    return 0


def self_check() -> None:
    rows = rows_from_dump(
        {
            "params": {
                "data": [
                    [3292.4329, 40.65, 562534, -234467],
                    [3292.44, 12.5, 1, 0, 99],
                    ["bad"],
                    [1.0, 2.0],
                ]
            }
        }
    )
    assert rows == [
        (3292.4329, 40.65, 562534, -234467),
        (3292.44, 12.5, 1, 0),
    ], rows
    script = build_extrude_script(40.0, 4.0)
    assert "M83" in script and "M82" in script and "M400" in script
    assert "G1 E40.000 F240.0" in script
    assert force_csv_name("loaded", 4.0) == "force-loaded-4mms.csv"
    assert force_csv_name("empty", 0.5) == "force-empty-0.5mms.csv"
    assert parse_speeds("0.5,1,2") == (0.5, 1.0, 2.0)
    assert parse_name_meta("force-loaded-4mms.csv") == ("loaded", 4.0)

    t0 = 10.0
    speed = 4.0
    times = [t0 + i * 0.05 for i in range(80)]
    forces = [100.0] * 40 + [20.0] * 40
    means = window_means(times, forces, speed, skip_mm=0.0, detection_length=4.0)
    assert len(means) == 4, means
    times_skip = [t0 + i * 0.25 for i in range(40)]
    forces_skip = [50.0] * 40
    skipped = window_means(times_skip, forces_skip, speed)
    assert skipped, skipped
    e_end = (times_skip[-1] - times_skip[0]) * speed
    assert e_end - SKIP_MM >= DETECTION_LENGTH_MM

    loaded = [200.0, 210.0, 190.0, 205.0]
    empty = [10.0, 12.0, 8.0, 11.0]
    snr = fisher_snr(loaded, empty)
    assert snr is not None and snr > 10.0, snr
    assert pctl(empty, 95.0) == 12.0

    tdir = tempfile.mkdtemp()
    t0 = 0.0
    speed = 4.0
    n = 80
    dt = 40.0 / speed / n
    loaded_path = os.path.join(tdir, force_csv_name("loaded", speed))
    empty_path = os.path.join(tdir, force_csv_name("empty", speed))
    with open(loaded_path, "w") as fh:
        fh.write(CSV_HEADER + "\n")
        for i in range(n):
            fh.write("%.4f,200.0,0,0\n" % (t0 + i * dt))
    with open(empty_path, "w") as fh:
        fh.write(CSV_HEADER + "\n")
        for i in range(n):
            fh.write("%.4f,10.0,0,0\n" % (t0 + i * dt))
    rc = summarise(tdir)
    assert rc == 0, rc

    # Signed cell like INDX: empty floor ~-2400, loaded more negative.
    ntdir = tempfile.mkdtemp()
    loaded_path = os.path.join(ntdir, force_csv_name("loaded", speed))
    empty_path = os.path.join(ntdir, force_csv_name("empty", speed))
    with open(loaded_path, "w") as fh:
        fh.write(CSV_HEADER + "\n")
        for i in range(n):
            fh.write("%.4f,-2800.0,0,0\n" % (t0 + i * dt))
    with open(empty_path, "w") as fh:
        fh.write(CSV_HEADER + "\n")
        for i in range(n):
            fh.write("%.4f,-2400.0,0,0\n" % (t0 + i * dt))
    rc = summarise(ntdir)
    assert rc == 0, rc
    print("self-check ok")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stream load-cell force to CSV. Prefer --ladder for the SNR / "
            "option B capture."
        )
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--sensor", default=DEFAULT_SENSOR)
    parser.add_argument(
        "--out",
        default="",
        help="CSV path, or - for stdout; default from --state/--speed",
    )
    parser.add_argument(
        "--out-dir",
        default=".",
        help="directory for --ladder CSVs and runs.csv",
    )
    parser.add_argument(
        "--manifest",
        default="",
        help="runs.csv path (default: next to the CSV / out-dir)",
    )
    parser.add_argument("--seconds", type=float, default=0)
    parser.add_argument(
        "--distance",
        type=float,
        default=DEFAULT_DISTANCE,
        help="relative E mm (default %.0f)" % DEFAULT_DISTANCE,
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=None,
        help="e_speed mm/s for a single rung",
    )
    parser.add_argument(
        "--listen",
        action="store_true",
        help="record dump_force only (no E moves). Ctrl-C or --seconds",
    )
    parser.add_argument(
        "--ladder",
        action="store_true",
        help="run the default speed ladder into --out-dir",
    )
    parser.add_argument(
        "--speeds",
        default=",".join(str(s) for s in DEFAULT_SPEEDS),
        help="comma-separated mm/s for --ladder",
    )
    parser.add_argument(
        "--pause-s",
        type=float,
        default=DEFAULT_PAUSE_S,
        help="idle between ladder rungs (default %.1f)" % DEFAULT_PAUSE_S,
    )
    parser.add_argument(
        "--state",
        choices=STATES,
        default="",
        help="loaded or empty (required for --ladder)",
    )
    parser.add_argument("--tool", type=int, default=None)
    parser.add_argument("--material", default="")
    parser.add_argument("--temp", type=float, default=None)
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--gcode",
        default="",
        help="raw script instead of --distance/--speed (must include M83)",
    )
    parser.add_argument("--api-key", default="")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument(
        "--summarise",
        "--summarize",
        dest="summarise",
        default="",
        help="offline SNR table from a directory of force CSVs + runs.csv",
    )
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return 0
    if args.summarise:
        return summarise(args.summarise)
    try:
        args.speeds = parse_speeds(args.speeds)
    except ValueError as exc:
        parser.error(str(exc))
    if args.ladder:
        if not args.state:
            parser.error("--ladder needs --state loaded|empty")
        if args.gcode or args.speed is not None or args.out == "-" or args.listen:
            parser.error("--ladder cannot mix with --gcode / --speed / --listen / --out -")
    if args.listen:
        if args.gcode or args.speed is not None:
            parser.error("--listen cannot mix with --gcode / --speed")
    if args.gcode and args.speed is not None:
        parser.error("use either --gcode or --speed, not both")
    try:
        return stream(args)
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
