# SPDX-License-Identifier: GPL-2.0-or-later
"""Record a short how-to video of a sandboxed QElectroTech.

    from clip import Clip
    with Clip(binary, out_dir, "fillet") as c:
        c.caption("Draw two lines that meet at a corner")
        c.menu("Edit", "Add", "Add line")
        c.click(600, 500); c.click(900, 500)
        c.hold(2)
    # -> out_dir/fillet.mp4 (and fillet.raw.mkv, fillet.ass)

A Clip is a shots.Session (own Xvfb, HOME, TMPDIR, binary copy) that
also records its display with ffmpeg's x11grab, moves the pointer smoothly
so a viewer can follow it, and logs captions and clicks with the time they
happened. At the end the log becomes an ASS subtitle track (captions in a
box, a ring where each click landed), burned into an H.264 mp4 small enough
to attach to a GitHub comment.

ffmpeg runs from the linuxserver/ffmpeg image, so nothing needs installing
on the host. x11grab stamps frames with the wall clock, and -copyts keeps
those stamps, so the first frame's time lines the log up with the video
exactly.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shots import Session  # noqa: E402
import probe as probe_mod  # noqa: E402

HERE = Path(__file__).resolve().parent
SLEEP_SCALE = 1.0      # story.py --check-only shrinks every pause; events still decide the pace


def nap(s):
    time.sleep(s * SLEEP_SCALE)


FFMPEG_IMAGE = os.environ.get("CLIP_FFMPEG_IMAGE", "linuxserver/ffmpeg")
FONTS = Path(os.environ.get("CLIP_FONTS", "/usr/share/fonts/truetype/noto"))
FONT = os.environ.get("CLIP_FONT", "Noto Sans")


def _docker_ffmpeg(args, out_dir: Path, stdin=None, x11=False, **kw):
    cmd = ["docker", "run", "--rm", "-i", "--network", "none",
           "-u", f"{os.getuid()}:{os.getgid()}",
           "-e", "XDG_CACHE_HOME=/tmp",
           "-v", f"{out_dir}:/out", "-v", f"{FONTS}:/fonts:ro"]
    if x11:
        cmd += ["-v", "/tmp/.X11-unix:/tmp/.X11-unix:ro"]
    # --entrypoint: the image's own start-up wrapper (s6) refuses -u.
    cmd += ["--entrypoint", "ffmpeg", FFMPEG_IMAGE, "-hide_banner", "-loglevel", "error", "-y"] + args
    return subprocess.Popen(cmd, stdin=stdin, **kw)


class Clip(Session):
    """size is the recorded screen in physical pixels; QT_SCALE_FACTOR makes
    QET lay out as it would at size/scale, so the picture keeps its familiar
    proportions while carrying 'scale' times the pixels, which is what lets
    the camera zoom in later without going soft."""

    def __init__(self, binary, out_dir, name, size=(2880, 1620), scale=1.5, fps=25, **kw):
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        # openbox's own Alt+wheel switches desktops; the videos show Alt+wheel
        # in QET, so run openbox without those mouse bindings
        kw.setdefault("openbox_rc", Path(__file__).resolve().parent / "assets" / "openbox-rc.xml")
        super().__init__(binary, out=out_dir, size=size, **kw)
        self.name = name
        self.fps = fps
        self.scale = scale
        self.events: list[dict] = []
        self.rec = None
        self._caption_open = None
        self.cursor = (size[0] // 2, size[1] // 2)
        self.sock = self.dir / "probe.sock"
        self.app_env = {**probe_mod.app_env(probe_mod.build(), self.sock),
                        "QT_SCALE_FACTOR": str(scale), "XCURSOR_SIZE": str(round(24 * scale))}
        self.probe = probe_mod.Probe(self.sock)

    def start(self, **kw):
        super().start(**kw)
        self.probe.connect()

    # -- recording --------------------------------------------------------
    @property
    def raw(self) -> Path:
        return self.out / f"{self.name}.raw.mkv"

    def record(self):
        """Start recording the display. Call once QET is set up, so the
        loading screens are not in the video."""
        if SLEEP_SCALE < 1:          # --check-only: nothing is recorded
            return
        self.raw.unlink(missing_ok=True)
        w, h = self.size
        self.rec = _docker_ffmpeg(
            ["-f", "x11grab", "-draw_mouse", "1", "-framerate", str(self.fps),
             "-video_size", f"{w}x{h}", "-i", f":{self.display}",
             "-copyts", "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
             f"/out/{self.raw.name}"],
            self.out, stdin=subprocess.PIPE, x11=True)
        for _ in range(100):
            if self.raw.exists() and self.raw.stat().st_size > 2000:
                break
            nap(0.1)
        else:
            raise RuntimeError("ffmpeg did not start recording")
        nap(0.5)

    def stop_recording(self):
        if not self.rec:
            return
        self._close_caption()
        self.rec.stdin.write(b"q")
        self.rec.stdin.close()
        self.rec.wait(30)
        self.rec = None
        (self.out / f"{self.name}.events.json").write_text(json.dumps(
            {"size": list(self.size), "scale": self.scale, "events": self.events}, indent=1))

    def stop(self):
        self.stop_recording()
        super().stop()

    def __exit__(self, *exc):
        self.stop()

    # -- log --------------------------------------------------------------
    def _log(self, kind, **data):
        self.events.append({"t": time.time(), "kind": kind, **data})

    def _close_caption(self):
        if self._caption_open is not None:
            self._log("caption_end")
            self._caption_open = None

    def caption(self, text, hold=0.0, where="bottom"):
        """Show @p text from now until the next caption (or end_caption).
        hold: seconds to wait after it appears, so it can be read first."""
        self._close_caption()
        self._log("caption", text=text, where=where)
        self._caption_open = text
        nap(hold)

    def chapter(self, text, hold=1.0):
        """A caption that is also a chapter marker for the YouTube description."""
        self._log("chapter", text=text)
        self.caption(text, hold=hold)

    def end_caption(self):
        self._close_caption()

    def title(self, text, sub="", hold=3.5):
        """A title over the screen for @p hold seconds."""
        self._close_caption()
        self._log("title", text=text, sub=sub)
        nap(hold)
        self._log("title_end")

    def highlight(self, x, y, w, h, secs=2.5):
        """An orange frame around (x, y, w, h) for @p secs, from now."""
        self._log("box", x=x, y=y, w=w, h=h, secs=secs)

    def hold(self, s):
        nap(s)

    # -- pointer that a viewer can follow --------------------------------
    def glide(self, x, y, speed=None, min_t=0.25, max_t=0.8):
        """Move the pointer to (x, y) along an eased path, ~speed px/s."""
        x0, y0 = self.cursor
        speed = (speed or 1400.0) * self.scale
        dist = math.hypot(x - x0, y - y0)
        dur = max(min_t, min(max_t, dist / speed)) if dist > 2 else 0
        if dur:
            self._log("move", x=x, y=y, dur=dur)
        steps = max(1, int(dur * 60))
        for i in range(1, steps + 1):
            f = i / steps
            f = f * f * (3 - 2 * f)          # smoothstep
            self.run("xdotool", "mousemove", int(x0 + (x - x0) * f), int(y0 + (y - y0) * f))
            nap(dur / steps)
        self.cursor = (x, y)

    def move(self, x, y, delay=0.2):
        self.glide(x, y)
        nap(delay)

    def click(self, x, y, button=1, delay=0.6, double=False, ring=True, ui=False):
        self.glide(x, y)
        nap(0.15)
        if ring:
            self._log("click", x=x, y=y, button=button, ui=ui)
        self.run("xdotool", "click", *(["--repeat", "2"] if double else []), button)
        nap(delay)

    def drag(self, x1, y1, x2, y2, button=1, steps=30, delay=0.6, keys=()):
        self.glide(x1, y1)
        for k in keys:
            self.run("xdotool", "keydown", k)
        self._log("click", x=x1, y=y1, button=button)
        self.run("xdotool", "mousedown", button)
        self.glide(x2, y2, speed=700, min_t=0.6, max_t=1.4)
        nap(0.15)
        self.run("xdotool", "mouseup", button)
        for k in keys:
            self.run("xdotool", "keyup", k)
        nap(delay)

    def wheel(self, x, y, notches=1, up=True, keys=(), delay=0.6, gap=0.5):
        """Turn the mouse wheel over (x, y), with @p keys held (alt, ctrl)."""
        self.glide(x, y)
        nap(0.2)
        for k in keys:
            self.run("xdotool", "keydown", k)
        for _ in range(notches):
            self._log("wheel", x=x, y=y, up=up, keys=list(keys))
            self.run("xdotool", "click", "4" if up else "5")
            nap(gap)
        for k in keys:
            self.run("xdotool", "keyup", k)
        nap(delay)

    def key(self, *keys, delay=0.5, show=True):
        """Press keys; with show, the key is shown on screen as it is pressed."""
        for k in keys:
            if show:
                self._log("key", key=k)
            self.run("xdotool", "key", "--clearmodifiers", k)
            nap(delay)

    def type(self, text, delay=0.5, per_char=0.12):
        self.run("xdotool", "type", "--delay", str(int(per_char * 1000)), text)
        nap(delay)


# -- rendering --------------------------------------------------------------
def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


KEY_NAMES = {"Return": "Enter", "Escape": "Esc", "ctrl": "Ctrl", "shift": "Shift", "alt": "Alt",
             "plus": "+", "minus": "−", "BackSpace": "Backspace", "Delete": "Delete"}


def _key_label(k: str) -> str:
    parts = [KEY_NAMES.get(p, p.upper() if len(p) == 1 else p) for p in k.split("+")]
    return " + ".join(parts)


def _circle(r: int) -> str:
    """An ASS drawing of a circle of radius r centred on the origin."""
    k = round(r * 0.5523)
    return (f"m 0 {-r} b {k} {-r} {r} {-k} {r} 0 b {r} {k} {k} {r} 0 {r} "
            f"b {-k} {r} {-r} {k} {-r} 0 b {-r} {-k} {-k} {-r} 0 {-r}")


def first_pts(raw: Path) -> float:
    r = subprocess.run(["docker", "run", "--rm", "-v", f"{raw.parent}:/out", "--entrypoint", "ffprobe",
                        FFMPEG_IMAGE, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=start_time:format=duration", "-of", "json", f"/out/{raw.name}"],
                       capture_output=True, text=True, check=True)
    j = json.loads(r.stdout)
    start, duration = float(j["streams"][0]["start_time"]), float(j["format"]["duration"])
    if duration > start > 1e9:          # -copyts: the duration counts from the epoch
        duration -= start
    return start, duration


