#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plot.py — render the three report figures as standalone SVG.

Standard library only. No pandas / matplotlib / numpy.

  python scripts/plot.py --csv data/summary.csv --after data/before_after.csv --out figures

Figures:
  figures/latency-vs-concurrency.svg   mean latency vs concurrency, four endpoints
  figures/throughput-and-errors.svg    throughput panel + error-rate panel at c=30
  figures/before-after.svg             rps panel + p95 panel, before/after at c=30

Rules baked in (these are exactly what broke in the earlier version):
  * axis maxima are derived from the data, never hardcoded;
  * a value label is never allowed to leave the canvas (y is clamped);
  * labels sharing a column are pushed apart so they cannot overlap;
  * legends live in empty bands outside the plot area;
  * nothing is drawn on top of an axis tick label.
"""

import argparse
import csv
import html
import math
import os

ENDPOINTS = ["E1_login", "E2_dashboard", "E3_assetlist", "E4_assetdetail"]
SHORT = {
    "E1_login": "Login",
    "E2_dashboard": "Dashboard",
    "E3_assetlist": "Asset list",
    "E4_assetdetail": "Asset detail",
}
COLOR = {
    "E1_login": "#1f77b4",
    "E2_dashboard": "#d62728",
    "E3_assetlist": "#2ca02c",
    "E4_assetdetail": "#9467bd",
    "p95": "#883333",
}
FONT = "Segoe UI, Arial, sans-serif"
INK = "#333333"
MUTED = "#555555"
GRID = "#e6e6e6"


# ---------------------------------------------------------------- numbers

def nice_step(value, intervals=4):
    if value <= 0:
        return 1.0
    raw = value / float(intervals)
    exp = math.floor(math.log10(raw))
    base = 10.0 ** exp
    for m in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        step = m * base
        if step >= raw:
            if value >= 5 and step < 1:
                step = 1.0
            return float(step)
    return float(10 * base)


def axis_for(max_value, intervals=4):
    """Return (axis_max, step) covering max_value with a little headroom."""
    if max_value <= 0:
        return 1.0, 0.2
    step = nice_step(max_value * 1.1, intervals)
    top = math.ceil(max_value / step) * step
    while top < max_value * 1.06:
        top += step
    return float(top), step


def ticks_for(axis_max, step):
    out, v = [], 0.0
    while v <= axis_max + step * 1e-9:
        out.append(round(v, 10))
        v += step
    return out


def fmt(value):
    if value is None or value == "":
        return ""
    f = float(value)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return ("%.2f" % f).rstrip("0").rstrip(".")


def esc(text_value):
    return html.escape(str(text_value), quote=True)


# ---------------------------------------------------------------- svg bits

def text(x, y, s, size=11, color=INK, anchor="middle", weight="normal", rotate=None):
    rot = ' transform="rotate(%s, %.1f, %.1f)"' % (rotate, x, y) if rotate is not None else ""
    return ('<text x="%.1f" y="%.1f" font-family="%s" font-size="%d" fill="%s" '
            'text-anchor="%s" font-weight="%s"%s>%s</text>'
            % (x, y, FONT, size, color, anchor, weight, rot, esc(s)))


def svg_open(width, height, title):
    return [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d">' % (width, height, width, height),
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        text(width / 2.0, 30, title, size=15, weight="bold"),
    ]


def polyline(pts, color, width=2.2, dash=None):
    if not pts:
        return ""
    d = " ".join("%.2f %.2f" % (p[0], p[1]) for p in pts)
    dash_attr = ' stroke-dasharray="%s"' % dash if dash else ""
    return ('<path d="M %s" fill="none" stroke="%s" stroke-width="%.1f"%s/>'
            % (d, color, width, dash_attr))


def hline(x1, x2, y, color=GRID, width=1, dash=None):
    dash_attr = ' stroke-dasharray="%s"' % dash if dash else ""
    return ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
            'stroke-width="%s"%s/>' % (x1, y, x2, y, color, width, dash_attr))


def vline(x, y1, y2, color=GRID, width=1):
    return ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="%s"/>'
            % (x, y1, x, y2, color, width))


def dot(x, y, color, r=4, hollow=False):
    if hollow:
        return ('<circle cx="%.2f" cy="%.2f" r="%.1f" fill="#ffffff" stroke="%s" '
                'stroke-width="1.6"/>' % (x, y, r, color))
    return ('<circle cx="%.2f" cy="%.2f" r="%.1f" fill="%s" stroke="#ffffff" '
            'stroke-width="1.2"/>' % (x, y, r, color))


def rect(x, y, w, h, fill, stroke=None, opacity=None):
    extra = ""
    if stroke:
        extra += ' stroke="%s" stroke-width="1"' % stroke
    if opacity is not None:
        extra += ' opacity="%.2f"' % opacity
    return '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="%s"%s/>' % (x, y, w, h, fill, extra)


class Labels:
    """Collects point labels; pushes apart any that share an x-column."""

    def __init__(self, top_limit, bottom_limit, direction="up"):
        self.top_limit = top_limit
        self.bottom_limit = bottom_limit
        self.direction = direction
        self.columns = {}

    def add(self, x, y, s, color, size=10, anchor="middle", weight="normal"):
        self.columns.setdefault(round(x, 1), []).append(
            {"x": x, "y": y, "s": s, "color": color, "size": size,
             "anchor": anchor, "weight": weight}
        )

    def render(self):
        out = []
        gap = 12.0
        for key in sorted(self.columns):
            items = sorted(self.columns[key], key=lambda d: d["y"])
            placed = [item["y"] for item in items]
            # settle the column: pushing one label away can re-collide with its neighbour
            for _ in range(8):
                changed = False
                for i in range(len(placed)):
                    placed[i] = max(self.top_limit,
                                    min(self.bottom_limit, placed[i]))
                for i in range(1, len(placed)):
                    for j in range(i - 1, -1, -1):
                        if abs(placed[i] - placed[j]) < gap:
                            if self.direction == "up":
                                placed[i] = placed[j] - gap
                            else:
                                placed[i] = placed[j] + gap
                            changed = True
                if not changed:
                    break
            for item, y in zip(items, placed):
                s = item["s"]
                size = item["size"]
                w = len(s) * size * 0.56 + 4
                if item["anchor"] == "middle":
                    box_x = item["x"] - w / 2.0
                elif item["anchor"] == "end":
                    box_x = item["x"] - w + 2
                else:
                    box_x = item["x"] - 2
                out.append(rect(box_x, y - size * 0.82, w, size * 1.12, "#ffffff"))
                out.append(text(item["x"], y, s, size=size,
                                color=item["color"], anchor=item["anchor"],
                                weight=item["weight"]))
        return out


def load(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def write(path, body):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(body))
        fh.write("\n")


def y_axis(body, left, right, top, bottom, axis_max, step, title):
    for t in ticks_for(axis_max, step):
        yy = bottom - (t / axis_max) * (bottom - top)
        body.append(hline(left, right, yy))
        body.append(text(left - 8, yy + 4, fmt(t), size=11, color=MUTED, anchor="end"))
    body.append(hline(left, right, bottom, color="#333", width=1.2))
    body.append(vline(left, top, bottom, color="#333", width=1.2))
    body.append(text(left - 54, (top + bottom) / 2.0, title, size=12, color=INK, rotate=-90))


# ---------------------------------------------------------------- figure 1

def figure_latency(rows, out_path):
    width, height = 960, 560
    left, right, top, bottom = 90, 830, 70, 460

    axis_max, step = axis_for(max(float(r["mean_latency_ms"]) for r in rows))

    def y_of(v):
        return bottom - (float(v) / axis_max) * (bottom - top)

    xs = [1, 5, 10, 20, 30]
    x_positions = {c: left + (right - left) * i / (len(xs) - 1) for i, c in enumerate(xs)}

    body = svg_open(width, height,
                    "Mean response time by concurrency \u2014 Snipe-IT 8.8.3, php-cgi on :9002")
    y_axis(body, left, right, top, bottom, axis_max, step, "Mean latency (ms)")

    for c in xs:
        body.append(text(x_positions[c], bottom + 20, str(c), size=11, color=MUTED))
    body.append(text((left + right) / 2.0, bottom + 42, "Concurrency", size=12, color=INK))

    body.append(hline(left, right, y_of(300), color="#cc3333", width=1.2, dash="5,4"))
    body.append(text(right - 6, y_of(300) + 16, "300 ms budget", size=11,
                     color="#cc3333", anchor="end"))

    labels = Labels(top + 12, bottom - 8)
    gutter = Labels(top + 12, bottom - 8, direction="down")
    last = xs[-1]
    for idx, ep in enumerate(ENDPOINTS):
        series = sorted([r for r in rows if r["endpoint"] == ep],
                        key=lambda r: int(r["concurrency"]))
        pts = [(x_positions[int(r["concurrency"])], y_of(r["mean_latency_ms"]))
               for r in series]
        body.append(polyline(pts, COLOR[ep]))
        # labels alternate left/right of the marker so four series at c=1 do not stack,
        # and the asset-list series labels sit below its marker
        side = "start" if idx % 2 == 0 else "end"
        drop = 16 if idx == 2 else -11
        for r, (px, py) in zip(series, pts):
            body.append(dot(px, py, COLOR[ep]))
            if int(r["concurrency"]) == last:
                gutter.add(right + 10, py - 3, fmt(r["mean_latency_ms"]), COLOR[ep],
                           size=10, anchor="start")
                continue
            lx = px + 11 if side == "start" else px - 11
            anchor = side
            if anchor == "start" and lx > right - 60:
                anchor, lx = "end", px - 11
            elif anchor == "end" and lx < left + 30:
                anchor, lx = "start", px + 11
            labels.add(lx, py + drop, fmt(r["mean_latency_ms"]), COLOR[ep], anchor=anchor)
    body.extend(labels.render())
    body.extend(gutter.render())

    lx, ly = left + 16, top + 16
    for i, ep in enumerate(ENDPOINTS):
        yy = ly + i * 22
        body.append(hline(lx, lx + 22, yy, color=COLOR[ep], width=2.2))
        body.append(dot(lx + 11, yy, COLOR[ep]))
        body.append(text(lx + 30, yy + 4, SHORT[ep], size=11, color=INK, anchor="start"))

    body.append("</svg>")
    write(out_path, body)


# ---------------------------------------------------------------- figure 2

def figure_throughput(rows, out_path):
    width, height = 960, 680
    left, right = 90, 830
    top1, bot1 = 66, 300
    top2, bot2 = 360, 570

    xs = [1, 5, 10, 20, 30]
    x_positions = {c: left + (right - left) * i / (len(xs) - 1) for i, c in enumerate(xs)}

    rps_axis, rps_step = axis_for(max(float(r["throughput_rps"]) for r in rows))
    err_axis, err_step = axis_for(max(float(r["error_rate_pct"]) for r in rows))

    def y1(v):
        return bot1 - (float(v) / rps_axis) * (bot1 - top1)

    def y2(v):
        return bot2 - (float(v) / err_axis) * (bot2 - top2)

    body = svg_open(width, height,
                    "Throughput ceiling and the 502 wall \u2014 12\u201315 rps, 10\u201314% errors at c=30")

    y_axis(body, left, right, top1, bot1, rps_axis, rps_step, "Throughput (rps)")
    for c in xs:
        body.append(text(x_positions[c], bot1 + 18, str(c), size=11, color=MUTED))
    body.append(text((left + right) / 2.0, bot1 + 38, "Concurrency", size=11, color=MUTED))

    labels = Labels(top1 + 12, bot1 - 8)
    gutter = Labels(top1 + 12, bot1 - 8, direction="down")
    last = xs[-1]
    for idx, ep in enumerate(ENDPOINTS):
        series = sorted([r for r in rows if r["endpoint"] == ep],
                        key=lambda r: int(r["concurrency"]))
        pts = [(x_positions[int(r["concurrency"])], y1(r["throughput_rps"])) for r in series]
        body.append(polyline(pts, COLOR[ep]))
        side = "start" if idx % 2 == 0 else "end"
        for r, (px, py) in zip(series, pts):
            body.append(dot(px, py, COLOR[ep]))
            if int(r["concurrency"]) == last:
                gutter.add(right + 10, py - 3, fmt(r["throughput_rps"]), COLOR[ep],
                           size=10, anchor="start")
                continue
            lx = px + 11 if side == "start" else px - 11
            anchor = side
            if anchor == "start" and lx > right - 60:
                anchor, lx = "end", px - 11
            elif anchor == "end" and lx < left + 30:
                anchor, lx = "start", px + 11
            labels.add(lx, py - 11, fmt(r["throughput_rps"]), COLOR[ep], anchor=anchor)
    body.extend(labels.render())
    body.extend(gutter.render())

    body.append(hline(left, right, y1(12), color="#888888", width=1.2, dash="6,4"))
    body.append(text(left + 8, y1(12) - 7, "observed ceiling \u2248 12 rps", size=11,
                     color="#666666", anchor="start"))

    y_axis(body, left, right, top2, bot2, err_axis, err_step, "Error rate (%)")

    slot = (right - left) / float(len(ENDPOINTS))
    bar_w = 64.0
    bar_labels = Labels(top2 + 12, bot2 - 30)
    for i, ep in enumerate(ENDPOINTS):
        cell = left + slot * (i + 0.5)
        row = [r for r in rows if r["endpoint"] == ep and int(r["concurrency"]) == 30]
        if not row:
            continue
        v = float(row[0]["error_rate_pct"])
        top_y = y2(v)
        body.append(rect(cell - bar_w / 2.0, top_y, bar_w, bot2 - top_y,
                         COLOR[ep], stroke="#333333", opacity=0.75))
        body.append(text(cell, bot2 + 18, SHORT[ep], size=11, color=INK))
        bar_labels.add(cell, top_y - 9, fmt(v), COLOR[ep], size=11, weight="bold")
    body.extend(bar_labels.render())
    body.append(text((left + right) / 2.0, bot2 + 40, "Endpoints at concurrency 30",
                     size=11, color=MUTED))

    lx, ly = left, height - 32
    for i, ep in enumerate(ENDPOINTS):
        xx = lx + i * 150
        body.append(hline(xx, xx + 22, ly, color=COLOR[ep], width=2.2))
        body.append(dot(xx + 11, ly, COLOR[ep]))
        body.append(text(xx + 30, ly + 4, SHORT[ep], size=11, color=INK, anchor="start"))

    body.append("</svg>")
    write(out_path, body)


# ---------------------------------------------------------------- figure 3

def figure_before_after(before_after, out_path):
    width, height = 960, 760
    left, right = 90, 900
    top1, bot1 = 66, 310
    top2, bot2 = 400, 630

    rows30 = {r["endpoint"]: r for r in before_after if r["c"] == "30"}

    rps_axis, rps_step = axis_for(max(max(float(r["rps_before"]), float(r["rps_after"]))
                                      for r in before_after))
    p95_axis, p95_step = axis_for(max(max(float(r["p95_before"]), float(r["p95_after"]))
                                      for r in before_after))

    def y_rps(v):
        return bot1 - (float(v) / rps_axis) * (bot1 - top1)

    def y_p95(v):
        return bot2 - (float(v) / p95_axis) * (bot2 - top2)

    body = svg_open(width, height,
                    "One ini directive: OPcache off vs on \u2014 throughput and p95 at concurrency 30")

    y_axis(body, left, right, top1, bot1, rps_axis, rps_step, "Throughput (rps)")
    y_axis(body, left, right, top2, bot2, p95_axis, p95_step, "p95 latency (ms)")

    slot = (right - left) / float(len(ENDPOINTS))
    bar_w, gap = 46.0, 10.0

    rps_labels = Labels(top1 + 12, bot1 - 8)
    p95_labels = Labels(top2 + 12, bot2 - 8)

    for i, ep in enumerate(ENDPOINTS):
        cell = left + slot * (i + 0.5)
        r = rows30[ep]

        for j, (key, fill, stroke, lbl_color, weight) in enumerate((
                ("rps_before", "#999999", "#666666", "#333333", "normal"),
                ("rps_after", "#1f77b4", "#0d4e8c", "#1f77b4", "bold"))):
            xx = cell - (bar_w + gap) / 2.0 + j * (bar_w + gap)
            v = float(r[key])
            top_y = y_rps(v)
            body.append(rect(xx, top_y, bar_w, bot1 - top_y, fill, stroke=stroke))
            rps_labels.add(xx + bar_w / 2.0, top_y - 9, fmt(v), lbl_color,
                           size=10, weight=weight)

        for j, (key, fill, stroke, lbl_color, weight) in enumerate((
                ("p95_before", "#bbbbbb", "#888888", "#777777", "normal"),
                ("p95_after", COLOR["p95"], "#5e2323", COLOR["p95"], "bold"))):
            xx = cell - (bar_w + gap) / 2.0 + j * (bar_w + gap)
            v = float(r[key])
            top_y = y_p95(v)
            body.append(rect(xx, top_y, bar_w, bot2 - top_y, fill, stroke=stroke))
            p95_labels.add(xx + bar_w / 2.0, top_y - 9, fmt(v), lbl_color,
                           size=10, weight=weight)

        body.append(text(cell, bot1 + 18, SHORT[ep], size=11, color=INK))
        body.append(text(cell, bot2 + 18, SHORT[ep], size=11, color=INK))

    body.extend(rps_labels.render())
    body.extend(p95_labels.render())

    body.append(text((left + right) / 2.0, bot1 + 40, "Throughput, endpoints at concurrency 30",
                     size=11, color=MUTED))
    body.append(text((left + right) / 2.0, bot2 + 40, "p95 latency, endpoints at concurrency 30",
                     size=11, color=MUTED))

    lx, ly = left, height - 46
    body.append(rect(lx - 6, ly - 26, 620, 54, "#ffffff", stroke="#cccccc"))
    body.append(rect(lx, ly - 16, 14, 12, "#999999", stroke="#666666"))
    body.append(text(lx + 22, ly - 6, "before (OPcache off)", size=11,
                     color="#333333", anchor="start"))
    body.append(rect(lx + 190, ly - 16, 14, 12, "#1f77b4", stroke="#0d4e8c"))
    body.append(text(lx + 212, ly - 6, "after (OPcache on)", size=11,
                     color="#1f77b4", anchor="start"))
    body.append(rect(lx, ly + 6, 14, 12, "#bbbbbb", stroke="#888888"))
    body.append(text(lx + 22, ly + 16, "p95 before", size=11, color="#777777", anchor="start"))
    body.append(rect(lx + 190, ly + 6, 14, 12, COLOR["p95"], stroke="#5e2323"))
    body.append(text(lx + 212, ly + 16, "p95 after", size=11, color=COLOR["p95"], anchor="start"))

    body.append("</svg>")
    write(out_path, body)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Render the three report figures as SVG.")
    ap.add_argument("--csv", required=True, help="summary.csv (baseline medians)")
    ap.add_argument("--after", required=True, help="before_after.csv (OPcache off vs on)")
    ap.add_argument("--out", default="figures", help="output directory")
    args = ap.parse_args()

    rows = load(args.csv)
    before_after = load(args.after)

    if not rows:
        raise SystemExit("summary.csv is empty")
    if not before_after:
        raise SystemExit("before_after.csv is empty")
    missing = [ep for ep in ENDPOINTS if not any(r["endpoint"] == ep for r in rows)]
    if missing:
        raise SystemExit("summary.csv is missing endpoints: %s" % ", ".join(missing))
    missing = [ep for ep in ENDPOINTS if ep not in {r["endpoint"] for r in before_after}]
    if missing:
        raise SystemExit("before_after.csv is missing endpoints: %s" % ", ".join(missing))

    figure_latency(rows, os.path.join(args.out, "latency-vs-concurrency.svg"))
    figure_throughput(rows, os.path.join(args.out, "throughput-and-errors.svg"))
    figure_before_after(before_after, os.path.join(args.out, "before-after.svg"))

    for name in ("latency-vs-concurrency.svg", "throughput-and-errors.svg",
                 "before-after.svg"):
        p = os.path.join(args.out, name)
        print("%-32s %6d bytes" % (name, os.path.getsize(p)))


if __name__ == "__main__":
    main()
