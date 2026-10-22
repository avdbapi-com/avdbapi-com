#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 AvdbAPI.com
"""Release pulse for the AvdbAPI.com profile README.

Reads the public releases of the repositories listed in config.json from the GitHub REST API and draws one SVG
per GitHub theme (pulse-dark.svg, pulse-light.svg): a 12-month timeline with one lane per repository, a dot per
release and the latest version on the right. No third-party packages: Python 3.8+ standard library only. Text is
drawn from glyphs.json (outlines exported from the SIL OFL fonts Space Grotesk and Bricolage Grotesque), because
SVG images on GitHub cannot load web fonts. The image carries no date of its own: it only changes (and the
workflow only publishes) when the release data changes or a new calendar month enters the window.

Usage:
  python3 pulse.py --owner avdbapi-com --config config.json --out DIR
                   [--atlas glyphs.json] [--data data.json] [--dump-data data.json] [--today YYYY-MM-DD]

  --data       draw from a saved data file instead of the API (tests, previews)
  --dump-data  also save the fetched data
Environment: GITHUB_TOKEN (optional, raises the API rate limit). It is sent only to api.github.com and never
printed or written.
Exit codes: 0 images written, 1 API or data error (nothing written), 2 usage error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

API = "https://api.github.com"
USER_AGENT = "avdbapi-com-release-pulse/1.0 (+https://github.com/avdbapi-com/avdbapi-com)"
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MAX_LANES = 8
MAX_RELEASE_PAGES = 5

# Colour tokens of the profile art (direction "Premiere").
THEMES = {
    "dark": {
        "bg0": "#100C30", "bg1": "#07071A", "border0": "#FFFFFF", "border0_op": 0.18, "border1": "#7A5CFF", "border1_op": 0.4,
        "ink": "#F5F3FF", "mid": "#C9C3EE", "low": "#918BBE", "track": "#FFFFFF", "track_op": 0.12, "grid": "#FFFFFF",
        "grid_op": 0.06, "today": "#6AF5C8", "glow_op": 0.22,
        "accents": {"violet": "#A891FF", "amber": "#FFB547", "cyan": "#55DEF8", "mint": "#6AF5C8", "rose": "#FF8FB8"},
        "aurora": ("#7A5CFF", 0.30, "#35D6F4", 0.16),
    },
    "light": {
        "bg0": "#FFFFFF", "bg1": "#F2EEFF", "border0": "#5B3DF5", "border0_op": 0.18, "border1": "#14B4DB", "border1_op": 0.35,
        "ink": "#15123A", "mid": "#433E6B", "low": "#686290", "track": "#5B3DF5", "track_op": 0.14, "grid": "#5B3DF5",
        "grid_op": 0.07, "today": "#097C5C", "glow_op": 0.18,
        "accents": {"violet": "#5B3DF5", "amber": "#A45704", "cyan": "#09769A", "mint": "#097C5C", "rose": "#C6326B"},
        "aurora": ("#7A5CFF", 0.16, "#14B4DB", 0.10),
    },
}


class PulseError(Exception):
    """API or data problem: nothing is written."""


# ------------------------------------------------------------------------------------------------
# GitHub API (read only)
# ------------------------------------------------------------------------------------------------
def http_json(url: str, token: str, timeout: float = 20.0, retries: int = 4):
    """GET a JSON document. Returns (data, link_header); data is None for 404. Retries 5xx and network errors."""
    if not url.startswith(API + "/"):
        raise PulseError("refusing a non-API URL")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    path = url[len(API):].split("?")[0]
    last = "no answer"
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise PulseError("answer too large for %s" % path)
                return json.loads(body.decode("utf-8")), resp.headers.get("Link", "") or ""
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, ""
            remaining = e.headers.get("X-RateLimit-Remaining") if e.headers else None
            if e.code in (403, 429) and remaining == "0":
                raise PulseError("GitHub API rate limit reached for %s (set GITHUB_TOKEN)" % path)
            if e.code >= 500 or e.code == 429:
                last = "HTTP %d" % e.code
            else:
                raise PulseError("GitHub API answered HTTP %d for %s" % (e.code, path))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = type(e).__name__
        except ValueError:
            raise PulseError("GitHub API sent invalid JSON for %s" % path)
        time.sleep(min(8, 1.5 * (2 ** attempt)))
    raise PulseError("GitHub API unreachable for %s (%s)" % (path, last))


def next_link(link: str) -> str:
    for part in link.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if m and m.group(1).startswith(API + "/"):
            return m.group(1)
    return ""


def parse_time(value) -> str:
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", value):
        return ""
    return value


def collect(owner: str, repos: list, token: str) -> dict:
    out = {"owner": owner, "repos": []}
    for item in repos:
        name = item["repo"]
        meta, _ = http_json("%s/repos/%s/%s" % (API, owner, name), token)
        if meta is None or not isinstance(meta, dict) or meta.get("private") or meta.get("visibility", "public") != "public":
            out["repos"].append({"repo": name, "missing": True})
            continue
        releases = []
        url = "%s/repos/%s/%s/releases?per_page=100" % (API, owner, name)
        for _page in range(MAX_RELEASE_PAGES):
            data, link = http_json(url, token)
            if data is None:
                break
            if not isinstance(data, list):
                raise PulseError("unexpected releases answer for %s" % name)
            for r in data:
                if not isinstance(r, dict) or r.get("draft"):
                    continue
                when = parse_time(r.get("published_at")) or parse_time(r.get("created_at"))
                tag = r.get("tag_name")
                if not when or not isinstance(tag, str) or not tag.strip():
                    continue
                releases.append({"tag": tag.strip()[:40], "date": when, "prerelease": bool(r.get("prerelease"))})
            url = next_link(link)
            if not url:
                break
        releases.sort(key=lambda r: r["date"], reverse=True)
        out["repos"].append({"repo": name, "missing": False, "archived": bool(meta.get("archived")),
                             "stars": int(meta.get("stargazers_count") or 0), "releases": releases})
    return out


# ------------------------------------------------------------------------------------------------
# Text from the glyph atlas
# ------------------------------------------------------------------------------------------------
class Atlas:
    def __init__(self, data: dict):
        if not isinstance(data, dict) or "styles" not in data or "em" not in data:
            raise PulseError("glyphs.json is not a glyph atlas")
        self.em = float(data["em"])
        self.styles = data["styles"]
        self.order = sorted(self.styles)
        self.used = {}

    def _glyph(self, style: str, ch: str) -> tuple:
        st = self.styles[style]
        if ch not in st["glyphs"]:
            ch = "?"
        return ch, st["glyphs"][ch]

    def width(self, text: str, style: str, size: float, tracking: float = 0.0) -> float:
        st = self.styles[style]
        units = 0.0
        prev = None
        n = 0
        for ch in text:
            ch, g = self._glyph(style, ch)
            if prev is not None:
                units += st["kern"].get(prev + ch, 0.0)
            units += g["a"]
            prev = ch
            n += 1
        return units * size / self.em + tracking * size * max(0, n - 1)

    def run(self, text: str, style: str, size: float, x: float, y: float, fill: str, anchor: str = "start",
            tracking: float = 0.0, opacity: float = 1.0) -> str:
        if style not in self.styles:
            raise PulseError("unknown text style %s" % style)
        w = self.width(text, style, size, tracking)
        if anchor == "middle":
            x -= w / 2.0
        elif anchor == "end":
            x -= w
        st = self.styles[style]
        si = self.order.index(style)
        track_units = tracking * self.em
        uses = []
        units = 0.0
        prev = None
        for ch in text:
            ch, g = self._glyph(style, ch)
            if prev is not None:
                units += st["kern"].get(prev + ch, 0.0)
            if g["d"]:
                gid = "g%dx%x" % (si, ord(ch))
                self.used[gid] = g["d"]
                uses.append('<use href="#%s" x="%s"/>' % (gid, fmt(units, 1)))
            units += g["a"] + track_units
            prev = ch
        op = "" if opacity >= 1 else ' opacity="%s"' % fmt(opacity, 3)
        return '<g transform="translate(%s %s) scale(%s)" fill="%s"%s>%s</g>' % (fmt(x, 2), fmt(y, 2), fmt(size / self.em, 5), fill, op, "".join(uses))

    def defs(self) -> str:
        return "".join('<path id="%s" d="%s"/>' % (k, self.used[k]) for k in sorted(self.used))


def fmt(v: float, prec: int = 2) -> str:
    s = ("%." + str(prec) + "f") % v
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ------------------------------------------------------------------------------------------------
# Drawing
# ------------------------------------------------------------------------------------------------
def month_start(d: dt.date, add: int = 0) -> dt.date:
    m = d.month - 1 + add
    return dt.date(d.year + m // 12, m % 12 + 1, 1)


def nice_tag(tag: str) -> str:
    m = re.match(r"^[Vv]\.?(\d.*)$", tag)
    return "v" + m.group(1) if m else tag


def nice_date(d: dt.date) -> str:
    return "%s %d, %d" % (MONTH_NAMES[d.month - 1], d.day, d.year)


def render(data: dict, config: dict, atlas: Atlas, theme: str, today: dt.date) -> str:
    """One lane per configured public repository. The window is the last `months` calendar months including the
    current one, so the image changes only when the release data changes or a new month starts."""
    T = THEMES[theme]
    months = int(config.get("months", 12))
    if not 3 <= months <= 24:
        raise PulseError("months must be 3 to 24")
    by_repo = {r["repo"]: r for r in data.get("repos", []) if isinstance(r, dict) and "repo" in r}
    lanes = []
    for item in config["repos"][:MAX_LANES]:
        r = by_repo.get(item["repo"])
        if not r or r.get("missing"):
            continue
        lanes.append((item, r))
    start = month_start(today, -(months - 1))
    end = month_start(today, 1)
    span = float((end - start).days)

    W = 1000
    top, lane_h = 124, 50
    n = max(1, len(lanes))
    H = top + n * lane_h + 52
    x_label, x0, x1, x_tag, x_right = 40, 286, 748, 778, 960
    title_txt = str(config.get("title", "Release pulse")).upper()[:32]
    defs = []
    body = []

    def xpos(d: dt.date) -> float:
        return x0 + (x1 - x0) * max(0.0, min(1.0, (d - start).days / span))

    a = T["aurora"]
    defs.append('<linearGradient id="bg" x1="0" y1="0" x2="0" y2="%d" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></linearGradient>' % (H, T["bg0"], T["bg1"]))
    defs.append('<radialGradient id="au" cx=".82" cy="0" r=".7"><stop offset="0" stop-color="%s" stop-opacity="%s"/><stop offset="1" stop-color="%s" stop-opacity="0"/></radialGradient>' % (a[0], fmt(a[1], 3), a[0]))
    defs.append('<radialGradient id="au2" cx=".05" cy="1" r=".6"><stop offset="0" stop-color="%s" stop-opacity="%s"/><stop offset="1" stop-color="%s" stop-opacity="0"/></radialGradient>' % (a[2], fmt(a[3], 3), a[2]))
    defs.append('<linearGradient id="bd" x1="0" y1="0" x2="%d" y2="%d" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="%s" stop-opacity="%s"/><stop offset="1" stop-color="%s" stop-opacity="%s"/></linearGradient>'
                % (W, H, T["border0"], fmt(T["border0_op"], 3), T["border1"], fmt(T["border1_op"], 3)))
    defs.append('<clipPath id="fr"><rect width="%d" height="%d" rx="24"/></clipPath>' % (W, H))
    body.append('<g clip-path="url(#fr)"><rect width="%d" height="%d" fill="url(#bg)"/><rect width="%d" height="%d" fill="url(#au)"/><rect width="%d" height="%d" fill="url(#au2)"/></g>' % (W, H, W, H, W, H))
    body.append('<rect x=".5" y=".5" width="%d" height="%d" rx="23.5" stroke="url(#bd)"/>' % (W - 1, H - 1))

    # header: title, window, honest count
    n_rel = 0
    for _i, r in lanes:
        for rel in r.get("releases", []):
            if start <= dt.date.fromisoformat(rel["date"][:10]) < end:
                n_rel += 1
    body.append('<circle cx="54" cy="56" r="6.5" fill="%s"/><circle cx="54" cy="56" r="13" fill="%s" fill-opacity="%s"/>' % (T["today"], T["today"], fmt(T["glow_op"], 3)))
    body.append(atlas.run(title_txt, "label-bold", 17, 78, 62, T["ink"], tracking=0.16))
    tw = atlas.width(title_txt, "label-bold", 17, 0.16)
    body.append(atlas.run("last %d months" % months, "data", 17, 78 + tw + 16, 62, T["low"]))
    summary = "%d release%s in %d repositor%s" % (n_rel, "" if n_rel == 1 else "s", len(lanes), "y" if len(lanes) == 1 else "ies")
    sw = atlas.width(summary, "label", 17, 0.02)
    if 78 + tw + 16 + atlas.width("last %d months" % months, "data", 17) + 24 > x_right - sw:
        raise PulseError("header text collides")
    body.append(atlas.run(summary, "label", 17, x_right, 62, T["mid"], anchor="end", tracking=0.02))
    body.append('<path d="M40 88.5H%d" stroke="%s" stroke-opacity="%s"/>' % (W - 40, T["grid"], fmt(T["grid_op"] * 1.6, 3)))

    # month grid + labels (the year replaces "Jan")
    grid = []
    m = start
    lane_bottom = top + n * lane_h - 16
    while m < end:
        x = xpos(m)
        grid.append("M%s %dV%d" % (fmt(x + .5, 1), top - 22, lane_bottom))
        nxt = month_start(m, 1)
        label = MONTH_NAMES[m.month - 1] if m.month != 1 else str(m.year)
        style = "label-bold" if m.month == 1 else "data"
        body.append(atlas.run(label, style, 13.5, (x + xpos(nxt)) / 2.0, lane_bottom + 30, T["low"], anchor="middle"))
        m = nxt
    grid.append("M%s %dV%d" % (fmt(x1 - .5, 1), top - 22, lane_bottom))
    body.append('<path d="%s" stroke="%s" stroke-opacity="%s"/>' % ("".join(grid), T["grid"], fmt(T["grid_op"], 3)))

    desc = []
    if not lanes:
        body.append(atlas.run("No public releases yet.", "label", 20, W / 2.0, top + 22, T["mid"], anchor="middle"))
    for i, (item, r) in enumerate(lanes):
        cy = top + i * lane_h + 2
        accent = T["accents"].get(item.get("accent", "violet"), T["accents"]["violet"])
        label = str(item.get("label") or item["repo"])[:28]
        lsize = 20.0
        while lsize > 13 and x_label + atlas.width(label, "title", lsize) > x0 - 16:
            lsize -= 0.5
        body.append(atlas.run(label, "title", lsize, x_label, cy + 7, T["ink"]))
        body.append('<rect x="%d" y="%s" width="%d" height="4" rx="2" fill="%s" fill-opacity="%s"/>' % (x0, fmt(cy - 2, 1), x1 - x0, T["track"], fmt(T["track_op"], 3)))
        rels = r.get("releases", [])
        in_window = []
        for rel in rels:
            d = dt.date.fromisoformat(rel["date"][:10])
            if start <= d < end:
                in_window.append((d, rel))
        in_window.sort(key=lambda t: t[0])
        if len(in_window) >= 2:
            xa, xb = xpos(in_window[0][0]), xpos(in_window[-1][0])
            body.append('<rect x="%s" y="%s" width="%s" height="4" rx="2" fill="%s" fill-opacity=".55"/>' % (fmt(xa, 1), fmt(cy - 2, 1), fmt(max(4.0, xb - xa), 1), accent))
        latest = rels[0] if rels else None
        for d, rel in in_window:
            x = xpos(d)
            is_latest = latest is not None and rel is latest
            if is_latest:
                body.append('<circle cx="%s" cy="%d" r="16" fill="%s" fill-opacity="%s"/>' % (fmt(x, 1), cy, accent, fmt(T["glow_op"], 3)))
            if rel.get("prerelease"):
                body.append('<circle cx="%s" cy="%d" r="6" fill="%s" stroke="%s" stroke-width="2.5"/>' % (fmt(x, 1), cy, T["bg1"], accent))
            else:
                body.append('<circle cx="%s" cy="%d" r="%s" fill="%s"/>' % (fmt(x, 1), cy, "8.5" if is_latest else "6.5", accent))
                body.append('<circle cx="%s" cy="%d" r="%s" fill="%s" fill-opacity=".9"/>' % (fmt(x, 1), cy, "3.2" if is_latest else "2.4", T["bg1"]))
        if latest:
            ld = dt.date.fromisoformat(latest["date"][:10])
            tag = nice_tag(latest["tag"])[:14]
            date_txt = nice_date(ld)
            body.append(atlas.run(tag, "label-bold", 17, x_tag, cy + 6.5, accent, tracking=0.02))
            if x_tag + atlas.width(tag, "label-bold", 17, 0.02) > x_right - atlas.width(date_txt, "data", 15) - 10:
                raise PulseError("lane text collides for %s" % item["repo"])
            body.append(atlas.run(date_txt, "data", 15, x_right, cy + 6, T["low"], anchor="end"))
            desc.append("%s %s on %s" % (label, tag, date_txt))
        else:
            body.append(atlas.run("no release yet", "data", 15, x_right, cy + 6, T["low"], anchor="end"))
            desc.append("%s: no release yet" % label)
    title = "Release pulse of AvdbAPI.com"
    description = "Releases of the last %d months. Latest: %s." % (months, "; ".join(desc) if desc else "none")
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" aria-labelledby="t d" fill="none">'
            '<title id="t">%s</title><desc id="d">%s</desc><defs>%s%s</defs>%s</svg>'
            % (W, H, W, H, esc(title), esc(description), "".join(defs), atlas.defs(), "".join(body)))


def validate_svg(svg: str) -> None:
    """Checks our own output before it is written. Every outside string in it (release tags, labels) was escaped
    by esc(); a DTD or entity declaration is refused before the stdlib parser sees the bytes (no XXE or entity
    expansion surface, and no third-party parser needed)."""
    if re.search(r"<!DOCTYPE|<!ENTITY", svg, re.I):
        raise PulseError("generated SVG contains a DTD or entity declaration")
    if re.search(r"<script|<foreignObject|<image[\s>]|https?://(?!www\.w3\.org/2000/svg)", svg):
        raise PulseError("generated SVG contains a forbidden element or URL")
    root = ET.fromstring(svg.encode("utf-8"))
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        raise PulseError("generated SVG has no svg root")
    ids = {el.get("id") for el in root.iter() if el.get("id")}
    for ref in re.findall(r'href="#([^"]+)"', svg) + re.findall(r"url\(#([^)]+)\)", svg):
        if ref not in ids:
            raise PulseError("generated SVG has an unresolved reference #%s" % ref)


def load_json(path: str, what: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        raise PulseError("cannot read %s %s: %s" % (what, path, type(e).__name__))


def check_config(cfg: dict) -> None:
    if not isinstance(cfg, dict) or not isinstance(cfg.get("repos"), list) or not cfg["repos"]:
        raise PulseError("config.json needs a non-empty repos list")
    for item in cfg["repos"]:
        if not isinstance(item, dict) or not REPO_RE.match(str(item.get("repo", ""))):
            raise PulseError("bad repo entry in config.json: %r" % (item,))
        if item.get("accent") and item["accent"] not in THEMES["dark"]["accents"]:
            raise PulseError("unknown accent %r" % item["accent"])


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Draw the release pulse SVGs.")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--config", default=os.path.join(here, "config.json"))
    ap.add_argument("--atlas", default=os.path.join(here, "glyphs.json"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--data")
    ap.add_argument("--dump-data")
    ap.add_argument("--today")
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not OWNER_RE.match(args.owner):
        print("pulse: bad --owner", file=sys.stderr)
        return 2
    try:
        today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(dt.timezone.utc).date()
    except ValueError:
        print("pulse: --today must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        cfg = load_json(args.config, "config")
        check_config(cfg)
        atlas_data = load_json(args.atlas, "glyph atlas")
        if args.data:
            data = load_json(args.data, "data")
        else:
            data = collect(args.owner, cfg["repos"], os.environ.get("GITHUB_TOKEN", "").strip())
        if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
            raise PulseError("data has no repos list")
        if args.dump_data:
            with open(args.dump_data, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, sort_keys=True)
        os.makedirs(args.out, exist_ok=True)
        rendered = {}
        for theme in ("dark", "light"):
            svg = render(data, cfg, Atlas(atlas_data), theme, today)
            validate_svg(svg)
            rendered[theme] = svg
        for theme, svg in rendered.items():
            path = os.path.join(args.out, "pulse-%s.svg" % theme)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(svg)
            os.replace(tmp, path)
            print("pulse: wrote %s (%d bytes)" % (path, len(svg.encode("utf-8"))))
        lanes = [r["repo"] for r in data["repos"] if not r.get("missing")]
        missing = [r["repo"] for r in data["repos"] if r.get("missing")]
        print("pulse: lanes %s%s" % (", ".join(lanes) or "none", ("; skipped (not public): " + ", ".join(missing)) if missing else ""))
        return 0
    except PulseError as e:
        print("pulse: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
