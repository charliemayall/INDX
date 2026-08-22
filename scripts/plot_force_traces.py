#!/usr/bin/env python3
"""
# INDX - plot loaded vs empty load-cell force traces
#
# Copyright (C) 2026 Charlie Mayall
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""

# Reads force-{loaded,empty}-*mms.csv and writes one HTML (Plotly CDN).
# No Python plotly package.
#
#   python3 scripts/plot_force_traces.py ./force-traces

from __future__ import annotations

import csv
import json
import os
import re
import sys

NAME_RE = re.compile(r"^force-(loaded|empty)-([0-9.]+)mms\.csv$")
MAX_POINTS = 2500
EMPTY_COLOUR = "#6b7280"
LOADED_COLOUR = "#2563eb"


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


def downsample(xs: list[float], ys: list[float], n: int = MAX_POINTS):
    if len(xs) <= n:
        return xs, ys
    step = max(1, len(xs) // n)
    return xs[::step], ys[::step]


def mean(vals: list[float]) -> float | None:
    if not vals:
        return None
    return sum(vals) / len(vals)


def collect(dir_path: str) -> dict[float, dict[str, tuple[list[float], list[float]]]]:
    by_speed: dict[float, dict[str, tuple[list[float], list[float]]]] = {}
    for name in sorted(os.listdir(dir_path)):
        match = NAME_RE.match(name)
        if not match:
            continue
        state, speed_s = match.group(1), match.group(2)
        speed = float(speed_s)
        times, forces = load_csv(os.path.join(dir_path, name))
        by_speed.setdefault(speed, {})[state] = (times, forces)
    return by_speed


def traces_and_layout(by_speed: dict[float, dict[str, tuple[list[float], list[float]]]]):
    speeds = sorted(by_speed)
    traces = []
    annotations = []
    n = len(speeds)
    cols = 2
    rows = (n + cols - 1) // cols
    for i, speed in enumerate(speeds):
        row, col = divmod(i, cols)
        axis_i = i + 1
        xaxis = "x" if axis_i == 1 else "x%d" % axis_i
        yaxis = "y" if axis_i == 1 else "y%d" % axis_i
        pair = by_speed[speed]
        for state, colour in (("empty", EMPTY_COLOUR), ("loaded", LOADED_COLOUR)):
            if state not in pair:
                continue
            times, forces = pair[state]
            if not times:
                continue
            t0 = times[0]
            xs, ys = downsample([(t - t0) * speed for t in times], forces)
            traces.append(
                {
                    "type": "scattergl",
                    "mode": "lines",
                    "name": state,
                    "legendgroup": state,
                    "showlegend": i == 0,
                    "x": xs,
                    "y": ys,
                    "line": {"color": colour, "width": 1},
                    "xaxis": xaxis,
                    "yaxis": yaxis,
                    "hovertemplate": "E=%{x:.1f} mm<br>F=%{y:.1f} g<extra>"
                    + state
                    + "</extra>",
                }
            )
        x_dom = [col / cols + 0.03, (col + 1) / cols - 0.04]
        y_top = 1 - row / rows
        y_bot = 1 - (row + 1) / rows
        y_dom = [y_bot + 0.06, y_top - 0.04]
        annotations.append(
            {
                "text": "%g mm/s" % speed,
                "xref": "paper",
                "yref": "paper",
                "x": (x_dom[0] + x_dom[1]) / 2,
                "y": y_top - 0.01,
                "showarrow": False,
                "font": {"size": 13},
            }
        )
    layout = {
        "title": {
            "text": "Loaded vs empty load-cell force (T1 PLA 230 C, 40 mm rungs)"
        },
        "height": 280 * rows,
        "template": "plotly_white",
        "legend": {"orientation": "h", "y": 1.06},
        "margin": {"t": 80, "l": 60, "r": 20, "b": 50},
        "annotations": annotations,
        "hovermode": "closest",
    }
    for i, speed in enumerate(speeds):
        row, col = divmod(i, cols)
        axis_i = i + 1
        xkey = "xaxis" if axis_i == 1 else "xaxis%d" % axis_i
        ykey = "yaxis" if axis_i == 1 else "yaxis%d" % axis_i
        x_dom = [col / cols + 0.05, (col + 1) / cols - 0.03]
        y_top = 1 - row / rows
        y_bot = 1 - (row + 1) / rows
        y_dom = [y_bot + 0.08, y_top - 0.06]
        layout[xkey] = {
            "domain": x_dom,
            "anchor": "y" if axis_i == 1 else "y%d" % axis_i,
            "title": {"text": "E (mm)"},
        }
        layout[ykey] = {
            "domain": y_dom,
            "anchor": "x" if axis_i == 1 else "x%d" % axis_i,
            "title": {"text": "Force (g)"},
        }
    return traces, layout, speeds


def summary_traces(by_speed: dict[float, dict[str, tuple[list[float], list[float]]]]):
    speeds = sorted(by_speed)
    loaded_y = []
    empty_y = []
    for speed in speeds:
        pair = by_speed[speed]
        empty = pair.get("empty")
        ref = mean(empty[1]) if empty and empty[1] else 0.0
        if empty and empty[1]:
            empty_y.append(mean([abs(f - ref) for f in empty[1]]))
        else:
            empty_y.append(None)
        loaded = pair.get("loaded")
        if loaded and loaded[1]:
            loaded_y.append(mean([abs(f - ref) for f in loaded[1]]))
        else:
            loaded_y.append(None)
    return [
        {
            "type": "scatter",
            "mode": "lines+markers",
            "name": "loaded |F-ref|",
            "x": speeds,
            "y": loaded_y,
            "line": {"color": LOADED_COLOUR, "width": 2},
            "marker": {"size": 8},
        },
        {
            "type": "scatter",
            "mode": "lines+markers",
            "name": "empty |F-ref|",
            "x": speeds,
            "y": empty_y,
            "line": {"color": EMPTY_COLOUR, "width": 2},
            "marker": {"size": 8},
        },
    ]


def write_html(
    path: str,
    wave_traces,
    wave_layout,
    sum_traces,
) -> None:
    payload = {
        "wave": {"data": wave_traces, "layout": wave_layout},
        "sum": {
            "data": sum_traces,
            "layout": {
                "title": {"text": "Mean |F-ref| vs e-speed (empty floor as ref)"},
                "height": 380,
                "template": "plotly_white",
                "xaxis": {"title": {"text": "e-speed (mm/s)"}},
                "yaxis": {"title": {"text": "|F-ref| (g)"}},
                "legend": {"orientation": "h", "y": 1.12},
                "margin": {"t": 80, "l": 60, "r": 20, "b": 50},
            },
        },
    }
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Loaded vs empty force</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    body { font-family: sans-serif; margin: 24px; color: #111; }
    h1 { font-size: 20px; font-weight: 600; margin: 0 0 8px; }
    p { color: #555; margin: 0 0 16px; font-size: 14px; }
  </style>
</head>
<body>
  <h1>Loaded vs empty load-cell force</h1>
  <p>T1 PLA 230 C. 40 mm rungs. Traces downsampled to %d points. Source: force-traces.</p>
  <div id="wave"></div>
  <div id="sum"></div>
  <script>
    const spec = %s;
    Plotly.newPlot("wave", spec.wave.data, spec.wave.layout, {responsive: true});
    Plotly.newPlot("sum", spec.sum.data, spec.sum.layout, {responsive: true});
  </script>
</body>
</html>
""" % (
        MAX_POINTS,
        json.dumps(payload, separators=(",", ":")),
    )
    with open(path, "w") as fh:
        fh.write(html)


def main() -> int:
    dir_path = sys.argv[1] if len(sys.argv) > 1 else "./force-traces"
    out_path = (
        sys.argv[2]
        if len(sys.argv) > 2
        else os.path.join(dir_path, "loaded-vs-empty.html")
    )
    by_speed = collect(dir_path)
    if not by_speed:
        sys.stderr.write("no force-{loaded,empty}-*mms.csv in %s\n" % dir_path)
        return 1
    wave_traces, wave_layout, _speeds = traces_and_layout(by_speed)
    write_html(out_path, wave_traces, wave_layout, summary_traces(by_speed))
    sys.stderr.write("wrote %s\n" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
