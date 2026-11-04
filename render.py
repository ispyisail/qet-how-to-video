# SPDX-License-Identifier: GPL-2.0-or-later
"""Post-production: raw recording + events.json (+ meta.json) -> the files a
video needs.

    python3 render.py <out_dir> <name>

Reads   <name>.raw.mkv  <name>.events.json  <name>.meta.json
Writes  <name>.mp4              1080p for YouTube, soft CC track, loudness -14 LUFS
        <name>.github.mp4       720p, under 10 MB, for a PR or discussion
        <name>.en.srt           captions
        <name>.description.txt  summary, chapters, links
        <name>.thumbnail.png    1280x720
        <name>.contact.png     one frame every few seconds, for review

What it does to the picture: a virtual camera eases in on the part of the
sheet being worked on and back out for menus (ffmpeg zoompan, the recording
is 1.5x oversize so the zoom stays sharp); click rings, key labels and
highlights are drawn after the zoom, moved through the same camera; an intro
card with the logo, an end card with the links.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import clip
import music
from clip import _docker_ffmpeg, _esc, _key_label, _circle, _ass_time, first_pts, FONT

OW, OH = 1920, 1080
FPS = 25
INTRO, OUTRO, XFADE = 3.6, 5.5, 0.6
CAM_MOVE = 0.9                    # seconds an ease takes
WIKI = "https://github.com/qelectrotech/qelectrotech-source-mirror/wiki/"
MENU_BAND = 0.13                  # top part of the screen: menu bar and toolbars


def _logo(info: dict, dest: Path):
    """QET's icon for the cards and the thumbnail, from the checkout the video
    was recorded from (story.py puts its path in meta.json), or
    $QET_VIDEO_LOGO. Without one, a transparent square: the cards still work."""
    import os
    src = Path(os.environ.get("QET_VIDEO_LOGO") or info.get("logo") or "")
    if src.is_file():
        shutil.copy(src, dest)
    else:
        from PIL import Image
        Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(dest)


# -- the camera ---------------------------------------------------------------
class Camera:
    """Targets (t, zoom, cx, cy) in recorded pixels; each is reached by an
    ease that starts at t. state(t) is what is on screen at t."""

    def __init__(self, size):
        self.W, self.H = size
        self.targets = [(0.0, 1.0, self.W / 2, self.H / 2)]

    def go(self, t, zoom, cx, cy):
        t = max(t, self.targets[-1][0] + CAM_MOVE + 0.2)   # one ease finishes before the next starts
        zoom = max(1.0, zoom)
        if zoom == 1.0:
            cx, cy = self.W / 2, self.H / 2
        self.targets.append((t, zoom, cx, cy))

    @staticmethod
    def _ss(u):
        u = max(0.0, min(1.0, u))
        return u * u * (3 - 2 * u)

    def _raw(self, t):
        prev, cur = self.targets[0], self.targets[0]
        for i, tg in enumerate(self.targets):
            if tg[0] <= t:
                cur, prev = tg, self.targets[i - 1] if i else tg
        u = self._ss((t - cur[0]) / CAM_MOVE)
        return tuple(p + (c - p) * u for p, c in zip(prev[1:], cur[1:]))

    def state(self, t):
        """(zoom, x0, y0): the visible window's top-left corner and zoom."""
        z, cx, cy = self._raw(t)
        x0 = max(0.0, min(self.W - self.W / z, cx - self.W / z / 2))
        y0 = max(0.0, min(self.H - self.H / z, cy - self.H / z / 2))
        return z, x0, y0

    def to_out(self, x, y, t):
        z, x0, y0 = self.state(t)
        s = z * OW / self.W
        return (x - x0) * s, (y - y0) * s, z

    def expressions(self):
        """ffmpeg expressions for zoompan, in terms of the input time 'it'."""
        def piece(idx, comp):
            tg, pv = self.targets[idx], self.targets[idx - 1]
            u = f"min(1,(it-{tg[0]:.3f})/{CAM_MOVE})"
            return f"({pv[comp]:.4f}+({tg[comp] - pv[comp]:.4f})*{u}*{u}*(3-2*{u}))"

        def build(comp):
            e = piece(len(self.targets) - 1, comp) if len(self.targets) > 1 else f"{self.targets[0][comp]:.4f}"
            for i in range(len(self.targets) - 2, 0, -1):
                e = f"if(lt(it,{self.targets[i + 1][0]:.3f}),{piece(i, comp)},{e})"
            if len(self.targets) > 1:
                e = f"if(lt(it,{self.targets[1][0]:.3f}),{self.targets[0][comp]:.4f},{e})"
            return e

        z, cx, cy = build(1), build(2), build(3)
        W, H = self.W, self.H
        return (f"z='{z}'",
                f"x='max(0,min({W}-{W}/zoom,({cx})-{W}/zoom/2))'",
                f"y='max(0,min({H}-{H}/zoom,({cy})-{H}/zoom/2))'")


def plan_camera(events, t0, end, size, scale):
    """Shots from what the hands do: clicks on the sheet frame a zoom on them,
    menu clicks and highlights bring the whole screen back."""
    cam = Camera(size)
    W, H = size
    sheet = [(e["t"] - t0, e["x"], e["y"]) for e in events
             if e["kind"] in ("click",) and not e.get("ui") and e["y"] > MENU_BAND * H and 0 <= e["t"] - t0 <= end]
    menu = sorted([e["t"] - t0 for e in events
                   if (e["kind"] in ("click", "move") and e["y"] <= MENU_BAND * H) or (e["kind"] == "click" and e.get("ui"))]
                  + [e["t"] - t0 for e in events if e["kind"] == "box"])
    # group sheet clicks into shots: a gap of 6 s or a menu click in between starts a new one
    shots, cur = [], []
    for t, x, y in sheet:
        if cur and (t - cur[-1][0] > 6 or any(cur[-1][0] < m < t for m in menu)):
            shots.append(cur); cur = []
        cur.append((t, x, y))
    if cur:
        shots.append(cur)
    dialogs = [(e["t"] - t0, e) for e in events if e["kind"] == "dialog"]
    for sh in shots:
        start = max(0.0, sh[0][0] - 1.2)
        # leave the shot a bit after the last click, or early enough to be
        # back on the whole screen when the next menu action begins
        later = [m for m in menu if m > sh[-1][0]]
        stop = min(later[0] - CAM_MOVE - 0.3 if later else end, sh[-1][0] + 2.0)
        start = max(start, cam.targets[-1][0] + CAM_MOVE + 0.2)
        if stop - start < CAM_MOVE + 1.0:
            continue                  # too short to go in and come back out cleanly
        # frame the clicks and any dialog open during the shot, so the
        # dialog is in view without the camera moving
        pts = [(x, y) for _, x, y in sh]
        for t, d in dialogs:
            if start <= t <= stop:
                pts += [(d["x"], d["y"]), (d["x"] + d["w"], d["y"] + d["h"])]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        bw, bh = max(xs) - min(xs), max(ys) - min(ys)
        pw, ph = max(bw * 1.25 if len(pts) > len(sh) else bw * 1.7, W * 0.30), \
            max(bh * 1.25 if len(pts) > len(sh) else bh * 1.7, H * 0.30)
        z = min(1.7, W / pw, H / ph)
        if z < 1.18:
            continue
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        cam.go(start, z, cx, cy)
        cam.go(stop, 1.0, 0, 0)
    cam.ui_times = menu
    return cam


# -- subtitles drawn over the camera ---------------------------------------------
def make_ass(events, t0, end, cam, size, size_out=(OW, OH), title=None):
    w, h = size_out
    fs = round(h * 0.042)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},{fs},&H00FFFFFF,&H00FFFFFF,&H28202020,&H28202020,1,0,0,0,100,100,0,0,3,{round(fs*0.45)},0,2,80,80,{round(h*0.12)},1
Style: Title,{FONT},{round(fs*1.6)},&H00FFFFFF,&H00FFFFFF,&H10582A0E,&H10582A0E,1,0,0,0,100,100,0,0,3,{round(fs*0.8)},0,5,80,80,0,1
Style: Sub,{FONT},{round(fs*0.9)},&H00FFFFFF,&H00FFFFFF,&H10582A0E,&H10582A0E,0,0,0,0,100,100,0,0,3,{round(fs*0.5)},0,5,80,80,0,1
Style: Key,{FONT},{round(fs*0.9)},&H00202020,&H00202020,&H10F0F0F0,&H10F0F0F0,1,0,0,0,100,100,0,0,3,{round(fs*0.35)},0,3,0,40,{round(h*0.12)},1
Style: Ring,{FONT},20,&HFF000000,&HFF000000,&H000A8CFF,&H00000000,0,0,0,0,100,100,0,0,1,4,0,7,0,0,0,1
Style: CardTitle,{FONT},{round(fs*1.9)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,5,80,80,0,1
Style: CardSub,{FONT},{round(fs*1.0)},&H00F0D8B8,&H00F0D8B8,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,80,80,0,1
Style: CardSmall,{FONT},{round(fs*0.62)},&H00D8C0A0,&H00D8C0A0,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines, cap, mid_title = [], None, None
    for e in events + [{"t": end + t0, "kind": "end"}]:
        t = e["t"] - t0
        if t < 0 and e["kind"] not in ("caption",):
            continue
        t = max(0.0, t)
        if e["kind"] in ("caption", "caption_end", "end") and cap is not None:
            lines.append(f"Dialogue: 1,{_ass_time(cap[0])},{_ass_time(min(t, end))},Cap,,0,0,0,,{{\\fad(150,150)}}{_esc(cap[1])}")
            cap = None
        if e["kind"] in ("title_end", "end") and mid_title is not None:
            lines.append(f"Dialogue: 2,{_ass_time(mid_title[0])},{_ass_time(min(t, end))},Title,,0,0,0,,{{\\fad(200,300)\\pos({w//2},{h//2})}}{_esc(mid_title[1])}")
            mid_title = None
        if e["kind"] == "caption":
            cap = (t, e["text"])
        elif e["kind"] == "title" and title == "overlay":
            mid_title = (t, e["text"])
        elif e["kind"] == "click":
            x, y, z = cam.to_out(e["x"], e["y"], e["t"] - t0)
            colour = "&H000A8CFF&" if e.get("button", 1) == 1 else "&H00E07B1F&"
            r = round(22 * z)
            lines.append(f"Dialogue: 3,{_ass_time(t)},{_ass_time(t + 0.55)},Ring,,0,0,0,,"
                         f"{{\\pos({x:.0f},{y:.0f})\\3c{colour}\\fscx40\\fscy40\\t(0,450,\\fscx100\\fscy100)\\fad(0,250)\\p1}}{_circle(r)}")
        elif e["kind"] == "box":
            x, y, _ = cam.to_out(e["x"], e["y"], e["t"] - t0)
            x2, y2, z = cam.to_out(e["x"] + e["w"], e["y"] + e["h"], e["t"] - t0)
            lines.append(f"Dialogue: 3,{_ass_time(t)},{_ass_time(t + e['secs'])},Ring,,0,0,0,,"
                         f"{{\\pos({x:.0f},{y:.0f})\\3c&H000A8CFF&\\fad(150,250)\\p1}}m 0 0 l {x2 - x:.0f} 0 {x2 - x:.0f} {y2 - y:.0f} 0 {y2 - y:.0f}")
        elif e["kind"] == "key":
            lines.append(f"Dialogue: 2,{_ass_time(t)},{_ass_time(t + 1.2)},Key,,0,0,0,,{{\\fad(80,200)}}{_esc(_key_label(e['key']))}")
    return head + "\n".join(lines) + "\n"


def card_ass(lines, size=(OW, OH)):
    """lines: [(style, text, y)] centred."""
    w, h = size
    base = make_ass([], 0, 0, Camera((w, h)), size)
    return base + "\n".join(
        f"Dialogue: 2,0:00:00.00,0:00:30.00,{style},,0,0,0,,{{\\pos({w//2},{y})}}{_esc(text)}"
        for style, text, y in lines) + "\n"


# -- helpers --------------------------------------------------------------------------
def _ffmpeg(args, out, label):
    p = _docker_ffmpeg(args, out)
    if p.wait():
        raise RuntimeError(f"ffmpeg failed: {label}")


def _srt_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d},{int(round((t % 1) * 1000)):03d}"


def _mmss(t):
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _measure(path, out):
    r = subprocess.run(["docker", "run", "--rm", "-v", f"{out}:/out", "--entrypoint", "ffmpeg", clip.FFMPEG_IMAGE,
                        "-hide_banner", "-nostats", "-i", f"/out/{path.name}", "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True)
    tail = r.stderr.split("Summary:")[-1]
    lufs = peak = None
    for ln in tail.splitlines():
        ln = ln.strip()
        if ln.startswith("I:"):
            lufs = float(ln.split()[1])
        if ln.startswith("Peak:"):
            peak = float(ln.split()[1])
    return lufs, peak


# -- thumbnail ------------------------------------------------------------------------
TW, TH = 1280, 720


def _wrap(text, fs, width, max_lines):
    """Greedy word wrap with an estimated bold glyph width; None if it does not fit."""
    lines, cur = [], ""
    for w in text.split():
        cand = (cur + " " + w).strip()
        if len(cand) * fs * 0.56 <= width or not cur:
            cur = cand
        else:
            lines.append(cur); cur = w
    lines.append(cur)
    if len(lines) > max_lines or any(len(l) * fs * 0.56 > width for l in lines):
        return None
    return lines


def thumbnail_ass(title, summary):
    """Left column of the thumbnail: brand, a HOW-TO tag, the title as large
    as fits in two lines, the summary."""
    fits = [(f, n) for n in (2, 3) for f in (118, 106, 96, 86, 78, 70, 64, 58) if _wrap(title, f, 520, n)]
    fs, n = fits[0] if fits else (52, 4)
    tlines = _wrap(title, fs, 520, n) or _wrap(title, fs, 520, 9) or [title]
    sfs = 36
    slines = _wrap(summary, sfs * 0.9, 520, 3) or ([summary] if summary else [])
    y_title = 330
    y_title = 330 if len(tlines) <= 2 else 290
    y_sum = y_title + len(tlines) * fs * 1.08 + 24
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {TW}
PlayResY: {TH}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TTitle,{FONT},{fs},&H00FFFFFF,&H00FFFFFF,&H00301A08,&H80000000,1,0,0,0,100,100,0,0,1,2,4,7,0,0,0,1
Style: TSub,{FONT},{sfs},&H00F5DDB8,&H00F5DDB8,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,0,2,7,0,0,0,1
Style: TBrand,{FONT},30,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,4,0,0,0,1
Style: TTag,{FONT},30,&H00FFFFFF,&H00FFFFFF,&H00137EF5,&H00137EF5,1,0,0,0,100,100,1,0,3,10,0,4,0,0,0,1
Style: TArrow,{FONT},20,&H00FFFFFF,&H00FFFFFF,&H00301A08,&H80000000,0,0,0,0,100,100,0,0,1,3,3,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = [f"Dialogue: 1,0:00:00.00,0:00:10.00,TBrand,,0,0,0,,{{\\pos(118,68)}}QElectroTech",
          f"Dialogue: 1,0:00:00.00,0:00:10.00,TTag,,0,0,0,,{{\\pos(232,205)}}HOW-TO",
          f"Dialogue: 1,0:00:00.00,0:00:10.00,TTitle,,0,0,0,,{{\\pos(50,{y_title})}}" + "\\N".join(map(_esc, tlines))]
    if slines:
        ev.append(f"Dialogue: 1,0:00:00.00,0:00:10.00,TSub,,0,0,0,,{{\\pos(52,{y_sum:.0f})}}" + "\\N".join(map(_esc, slines)))
    return head, ev


def thumbnail(out, name, raw, lead, body, t0, main_len, size, info, title):
    """<name>.thumbnail.png: title, toolbar icon, and the work before and
    after (or after only), taken from the raw recording so no caption or
    click ring is on it."""
    W, H = size
    work = out / "work"
    spec = info.get("thumbnail") or {}
    chapters = [e["t"] - t0 for e in body if e["kind"] == "chapter"]
    after = info.get("thumbnail_at", main_len - 0.5)
    before = None
    if "before" in spec and 1 <= spec["before"] <= len(chapters):
        c0 = chapters[spec["before"] - 1]
        c1 = chapters[spec["before"]] if spec["before"] < len(chapters) else main_len
        # as the pointer reaches the menu bar: the work as it was, no menu open yet
        to_bar = [e["t"] - t0 + e.get("dur", 0) + 0.1 for e in body
                  if e["kind"] == "move" and e["y"] <= MENU_BAND * H and c0 <= e["t"] - t0 < c1]
        before = to_bar[0] if to_bar else c0 + 0.3

    # one crop, 4:3, round the clicks on the sheet, for both pictures;
    # `crop: [x0, y0, x1, y1]` (fractions of the screen) when there are none
    pts = [(e["x"], e["y"]) for e in body if e["kind"] == "click" and not e.get("ui") and e["y"] > MENU_BAND * H]
    if pts:
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        cw = max((max(xs) - min(xs)) * 1.35, (max(ys) - min(ys)) * 1.35 * 4 / 3, W * 0.2)
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    else:
        cw, cx, cy = W * 0.5, W / 2, H / 2
    if "crop" in spec:
        x0, y0, x1, y1 = spec["crop"]
        cw = max((x1 - x0) * W, (y1 - y0) * H * 4 / 3)
        cx, cy = (x0 + x1) / 2 * W, (y0 + y1) / 2 * H
    cw = min(cw, W, H * 4 / 3); ch = cw * 3 / 4
    cx = max(cw / 2, min(W - cw / 2, cx)); cy = max(ch / 2, min(H - ch / 2, cy))
    crop = f"crop={cw:.0f}:{ch:.0f}:{cx - cw / 2:.0f}:{cy - ch / 2:.0f}"

    head, ev = thumbnail_ass(title, info.get("summary", ""))
    inputs = ["-f", "lavfi", "-i", f"gradients=s={TW}x{TH}:c0=0x0a1f47:c1=0x2a6fb8:x0=0:y0=0:x1={TW}:y1={TH}:speed=0:d=1",
              "-i", "/out/work/logo.png",
              "-ss", f"{lead + after:.2f}", "-i", f"/out/{raw.name}"]
    # the icon when the tool can be used but the pointer is not on its
    # button: from the "before" picture, else half way through
    inputs += ["-ss", f"{lead + (before if before is not None else main_len / 2):.2f}", "-i", f"/out/{raw.name}"]
    if before is not None:
        inputs += ["-ss", f"{lead + before:.2f}", "-i", f"/out/{raw.name}"]
        cards = [(4, 380, 285, 585, 40), (2, 500, 375, 755, 315)]      # (input, w, h, x, y): before, after
        # an arrow from the before card to the after card
        ev.append("Dialogue: 2,0:00:00.00,0:00:10.00,TArrow,,0,0,0,,{\\pos(655,385)\\p1}"
                  "m 0 0 l 0 70 l 70 70 l 70 96 l 118 52 l 70 8 l 70 34 l 26 34 l 26 0")
    else:
        cards = [(2, 620, 465, 620, 128)]
    (work / "thumb.ass").write_text(head + "\n".join(ev) + "\n", encoding="utf-8")
    _logo(info, work / "logo.png")

    # one frame of each input, all at time 0: overlay pairs frames by time
    one = "trim=end_frame=1,setpts=0"
    fc = [f"[0:v]{one}[bg]", f"[1:v]{one},scale=60:60[logo]"]
    after_in = bool(spec.get("icon", True) and info.get("icon_box"))
    if after_in:
        ix, iy, iw, ih = info["icon_box"]
        fc.append(f"[3:v]{one},crop={iw}:{ih}:{ix}:{iy},scale=112:112:flags=lanczos,pad=148:148:18:18:white[icon]")
    else:
        fc.append(f"[3:v]{one},nullsink")
    srcs = {2: f"[2:v]{one},", 4: f"[4:v]{one},"}
    for i, (inp, w, h, _, _) in enumerate(cards):
        fc.append(f"{srcs[inp]}{crop},scale={w}:{h}:flags=lanczos,pad={w + 12}:{h + 12}:6:6:white[c{i}]")
        fc.append(f"color=c=black@0.0:s={w + 92}x{h + 92},format=rgba[sh{i}b];"
                  f"color=c=black@0.55:s={w + 12}x{h + 12},format=rgba[sh{i}c];"
                  f"[sh{i}b][sh{i}c]overlay=40:40,boxblur=16:2[sh{i}]")
    cur = "[bg]"
    for i, (_, w, h, x, y) in enumerate(cards):
        fc.append(f"{cur}[sh{i}]overlay={x - 40 + 10}:{y - 40 + 14}[s{i}];[s{i}][c{i}]overlay={x}:{y}[v{i}]")
        cur = f"[v{i}]"
    fc.append(f"{cur}[logo]overlay=48:38[vl]")
    cur = "[vl]"
    if after_in:
        fc.append(f"{cur}[icon]overlay=50:130[vi]")
        cur = "[vi]"
    else:
        ev_fix = (work / "thumb.ass").read_text().replace("\\pos(232,205)", "\\pos(52,205)")
        (work / "thumb.ass").write_text(ev_fix, encoding="utf-8")
    fc.append(f"{cur}subtitles=/out/work/thumb.ass:fontsdir=/fonts,format=rgb24[out]")
    _ffmpeg(inputs + ["-filter_complex", ";".join(fc), "-map", "[out]", "-frames:v", "1",
                      f"/out/{name}.thumbnail.png"], out, "thumbnail")


# -- the render -------------------------------------------------------------------------
def render(out: Path, name: str, crf=19):
    out = Path(out).resolve()
    work = out / "work"
    work.mkdir(exist_ok=True)
    raw = out / f"{name}.raw.mkv"
    meta_file = out / f"{name}.events.json"
    data = json.loads(meta_file.read_text())
    events, size, scale = data["events"], tuple(data["size"]), data["scale"]
    info = {}
    if (out / f"{name}.meta.json").exists():
        info = json.loads((out / f"{name}.meta.json").read_text())

    start, duration = first_pts(raw)
    if start < 1e9:
        raise RuntimeError(f"first frame stamped {start}: not wall-clock, cannot line up the log")

    # intro/outro cards come from the first and last title steps
    titles = [e for e in events if e["kind"] == "title"]
    ends = [e for e in events if e["kind"] == "title_end"]
    intro_ev = titles[0] if titles else None
    outro_ev = titles[-1] if len(titles) > 1 and titles[-1]["t"] > events[-1]["t"] - 12 else None
    main_start = (ends[0]["t"] + 0.1) if intro_ev else min(e["t"] for e in events) - 0.4
    main_end = (outro_ev["t"] - 0.1) if outro_ev else start + duration - 0.3
    main_len = main_end - main_start
    lead = main_start - start
    t0 = main_start
    # events in the title steps are not part of the lesson
    body = [e for e in events if not (e["kind"] in ("title", "title_end") and e is not None)]
    body = [e for e in body if main_start - 1 <= e["t"] <= main_end + 1]

    cam = plan_camera(body, t0, main_len, size, scale)
    # every menu or toolbar action must happen on the whole, still screen
    late = [round(m, 1) for m in cam.ui_times if 0 <= m <= main_len and cam.state(m)[0] > 1.001]
    if late:
        print(f"warning: camera still zoomed at menu actions (s): {late}")
    zexpr, xexpr, yexpr = cam.expressions()
    (work / "main.ass").write_text(make_ass(body, t0, main_len, cam, size), encoding="utf-8")
    vf = (f"fps={FPS},zoompan={zexpr}:{xexpr}:{yexpr}:d=1:s={OW}x{OH}:fps={FPS},"
          f"subtitles=/out/work/main.ass:fontsdir=/fonts,format=yuv420p")
    (work / "main.vf").write_text(vf)
    print(f"camera: {len(cam.targets) - 1} moves; main {main_len:.1f} s")
    _ffmpeg(["-ss", f"{lead:.3f}", "-i", f"/out/{raw.name}", "-t", f"{main_len:.2f}",
             "-/filter:v", "/out/work/main.vf", "-c:v", "libx264", "-preset", "medium", "-crf", "14",
             "-r", str(FPS), "/out/work/main.mp4"], out, "main")

    # cards
    _logo(info, work / "logo.png")
    title = info.get("title") or (intro_ev or {}).get("text", name)
    sub = (intro_ev or {}).get("sub") or info.get("summary", "")
    page = info.get("wiki")
    commit = info.get("commit", "")
    intro_lines = [("CardTitle", title, 640), ("CardSub", sub, 760),
                   ("CardSmall", "QElectroTech how-to", 900)]
    outro_lines = [("CardTitle", (outro_ev or {}).get("text") or title, 520),
                   ("CardSub", (outro_ev or {}).get("sub") or "", 640)]
    if page:
        outro_lines.append(("CardSmall", f"Wiki: {WIKI}{page}", 770))
    if info.get("pr"):
        outro_lines.append(("CardSmall", f"Pull request #{info['pr']}", 830))
    outro_lines.append(("CardSmall", "qelectrotech.org" + (f"   ·   built from {commit}" if commit else ""), 920))
    for card, lines, secs in (("intro", intro_lines, INTRO), ("outro", outro_lines, OUTRO)):
        (work / f"{card}.ass").write_text(card_ass(lines), encoding="utf-8")
        _ffmpeg(["-f", "lavfi", "-i", f"gradients=s={OW}x{OH}:c0=0x0e2a58:c1=0x2a6fb8:x0=0:y0=0:x1={OW}:y1={OH}:d={secs}:r={FPS}",
                 "-i", "/out/work/logo.png",
                 "-filter_complex",
                 f"[1:v]scale=240:-1[l];[0:v][l]overlay=(W-w)/2:{'200' if card == 'intro' else '70'},"
                 f"subtitles=/out/work/{card}.ass:fontsdir=/fonts,fade=in:0:12,fade=out:st={secs - 0.5}:d=0.5,format=yuv420p",
                 "-t", str(secs), "-c:v", "libx264", "-preset", "medium", "-crf", "14", "-r", str(FPS),
                 f"/out/work/{card}.mp4"], out, card)

    has_outro = True
    total = INTRO + main_len + OUTRO - 2 * XFADE
    offset = INTRO - XFADE            # main starts here on the final timeline

    def fin(t):                       # a time in the main clip -> final timeline
        return offset + t

    # audio: bed + effects on the final timeline
    music.bed(work / "bed.wav", total + 0.5)
    clicks = [fin(e["t"] - t0) for e in body if e["kind"] == "click"]
    keys = [fin(e["t"] - t0) for e in body if e["kind"] == "key"]
    music.effects(work / "fx.wav", total + 0.5, clicks, keys, chimes=[total - OUTRO + XFADE + 0.3])

    # captions, chapters
    caps, cur = [], None
    for e in body + [{"t": t0 + main_len, "kind": "end"}]:
        if e["kind"] in ("caption", "caption_end", "end") and cur:
            caps.append((cur[0], e["t"] - t0, cur[1])); cur = None
        if e["kind"] == "caption":
            cur = (e["t"] - t0, e["text"])
    srt = "".join(f"{i}\n{_srt_time(fin(a))} --> {_srt_time(fin(b))}\n{text}\n\n"
                  for i, (a, b, text) in enumerate(caps, 1))
    (out / f"{name}.en.srt").write_text(srt, encoding="utf-8")
    (work / "cc.srt").write_text(srt, encoding="utf-8")

    # final: cards crossfaded round the lesson, music, effects, soft CC
    mp4 = out / f"{name}.mp4"
    fc = (f"[0:v][1:v]xfade=transition=fade:duration={XFADE}:offset={INTRO - XFADE:.3f}[a];"
          f"[a][2:v]xfade=transition=fade:duration={XFADE}:offset={INTRO + main_len - 2 * XFADE:.3f}[v];"
          f"[3:a]aresample=48000,volume=0.8[m];[4:a]aresample=48000,volume=1.0[s];"
          f"[m][s]amix=inputs=2:normalize=0:duration=longest,loudnorm=I=-14:TP=-1.5:LRA=7,"
          f"atrim=0:{total:.2f},afade=t=out:st={total - 2.5:.2f}:d=2.5[aout]")
    _ffmpeg(["-i", "/out/work/intro.mp4", "-i", "/out/work/main.mp4", "-i", "/out/work/outro.mp4",
             "-i", "/out/work/bed.wav", "-i", "/out/work/fx.wav", "-i", "/out/work/cc.srt",
             "-filter_complex", fc, "-map", "[v]", "-map", "[aout]", "-map", "5:s",
             "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-pix_fmt", "yuv420p",
             "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
             "-c:s", "mov_text", "-metadata:s:s:0", "language=eng", "-metadata", f"title={title}",
             "-movflags", "+faststart", f"/out/{mp4.name}"], out, "final")

    # a copy that fits GitHub's 10 MB
    gh = out / f"{name}.github.mp4"
    for gcrf in (28, 31, 34, 37):
        _ffmpeg(["-i", f"/out/{mp4.name}", "-vf", "scale=1280:720:flags=lanczos", "-c:v", "libx264",
                 "-preset", "slow", "-crf", str(gcrf), "-c:a", "aac", "-b:a", "96k", "-c:s", "mov_text",
                 "-movflags", "+faststart", f"/out/{gh.name}"], out, "github")
        if gh.stat().st_size < 9.5e6:
            break

    # description with chapters (YouTube wants 0:00 first, three or more, 10 s each)
    chapters = [(fin(e["t"] - t0), e["text"]) for e in body if e["kind"] == "chapter"]
    chap = []                         # first at 0:00, each at least 10 s from the last
    for t, text in chapters:
        if not chap:
            chap.append((0.0, text))
        elif t - chap[-1][0] >= 10 and total - t >= 10:
            chap.append((t, text))
    if len(chap) < 3:
        print("note: fewer than 3 chapters of 10 s or more: YouTube will not show chapters")
    desc = [f"{title} — QElectroTech", "", info.get("summary", ""), ""]
    desc += [f"{_mmss(t)} {text}" for t, text in chap]
    desc += [""]
    if page:
        desc.append(f"Wiki: {WIKI}{page}")
    if info.get("pr"):
        desc.append(f"Pull request: https://github.com/qelectrotech/qelectrotech-source-mirror/pull/{info['pr']}")
    desc += ["Download QElectroTech: https://qelectrotech.org", "",
             f"Recorded from QElectroTech {commit}. Captions: English (CC)." if commit else "Captions: English (CC).",
             "Music: generated for this video, no copyright claim."]
    (out / f"{name}.description.txt").write_text("\n".join(desc) + "\n", encoding="utf-8")

    thumbnail(out, name, raw, lead, body, t0, main_len, size, info, title)
    _ffmpeg(["-i", f"/out/{mp4.name}", "-vf", "fps=1/5,scale=480:-1,tile=4x4", "-frames:v", "1",
             f"/out/{name}.contact.png"], out, "contact")

    lufs, peak = _measure(mp4, out)
    print(f"{mp4}: {mp4.stat().st_size / 1e6:.1f} MB, {total:.0f} s, {lufs} LUFS, peak {peak} dBTP")
    print(f"{gh}: {gh.stat().st_size / 1e6:.1f} MB")
    return mp4


def render_thumbnail(out: Path, name: str):
    """Only the thumbnail, from a kept recording."""
    out = Path(out).resolve()
    (out / "work").mkdir(exist_ok=True)
    raw = out / f"{name}.raw.mkv"
    data = json.loads((out / f"{name}.events.json").read_text())
    events, size = data["events"], tuple(data["size"])
    info = json.loads((out / f"{name}.meta.json").read_text()) if (out / f"{name}.meta.json").exists() else {}
    start, duration = first_pts(raw)
    titles = [e for e in events if e["kind"] == "title"]
    ends = [e for e in events if e["kind"] == "title_end"]
    outro = titles[-1] if len(titles) > 1 and titles[-1]["t"] > events[-1]["t"] - 12 else None
    main_start = (ends[0]["t"] + 0.1) if titles else min(e["t"] for e in events) - 0.4
    main_end = (outro["t"] - 0.1) if outro else start + duration - 0.3
    body = [e for e in events if e["kind"] not in ("title", "title_end") and main_start - 1 <= e["t"] <= main_end + 1]
    title = info.get("title") or (titles[0]["text"] if titles else name)
    thumbnail(out, name, raw, main_start - start, body, main_start, main_end - main_start, size, info, title)
    print(out / f"{name}.thumbnail.png")


if __name__ == "__main__":
    if "--thumbnail" in sys.argv:
        render_thumbnail(Path(sys.argv[1]), sys.argv[2])
    else:
        render(Path(sys.argv[1]), sys.argv[2])
