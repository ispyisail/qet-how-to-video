# SPDX-License-Identifier: GPL-2.0-or-later
"""Drive a sandboxed QElectroTech (for screenshots and the video recorder).

    from shots import Session
    with Session(binary, project, conf={"General": {"lang": "en"}}) as s:
        s.click(700, 400); s.key("s"); s.wait(1.5)
        s.shot("drawing-shortcut-bar", crop=(262, 300, 700, 600))

Each Session has its own Xvfb display (with openbox, which Qt menus need),
its own HOME/XDG dirs and a private TMPDIR, so it neither reads your
settings nor hands its launch to a QElectroTech already running
(SingleApplication keys on binary path + user + TMPDIR). The binary is
copied, so a rebuild cannot change what is being shot.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent


def _free_display() -> int:
    for n in range(95, 140):
        if not Path(f"/tmp/.X11-unix/X{n}").exists() and not Path(f"/tmp/.X{n}-lock").exists():
            return n
    raise RuntimeError("no free X display")


class Session:
    def __init__(self, binary, project=None, conf: dict | None = None, out=None,
                 size=(1400, 900), args=(), env: dict | None = None, setup=None,
                 binary_at=None, main_name="QElectroTech", app_env: dict | None = None,
                 openbox_rc=None):
        self.dir = Path(tempfile.mkdtemp(prefix="qs-", dir="/tmp"))
        self.home = self.dir / "h"
        self.cfg = self.home / "cfg" / "QElectroTech"
        self.data = self.home / "data"
        self.cfg.mkdir(parents=True)
        self.data.mkdir(parents=True)
        self.out = Path(out or os.environ.get("SHOTS_OUT", self.dir / "out"))
        self.out.mkdir(parents=True, exist_ok=True)
        # binary_at: where to put the copy, for a recipe that needs an install
        # layout around it (../share/qelectrotech/...); default inside the
        # session directory.
        self.binary = Path(binary_at) if binary_at else self.dir / "qet"
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(binary, self.binary)
        # The compiled translations, which QElectroTech looks for in a lang/
        # folder beside the binary: English needs qet_en.qm for its plurals
        # ("1 conductor"). Build them with the qelectrotech_lrelease target.
        qm = Path(binary).resolve().parent / "lang"
        if qm.is_dir() and not (self.binary.parent / "lang").exists():
            shutil.copytree(qm, self.binary.parent / "lang")
        self.project = None
        if project:
            self.project = self.dir / Path(project).name
            shutil.copy(project, self.project)
        conf = {"General": {"lang": "en"}, **(conf or {})}
        conf.setdefault("General", {}).setdefault("lang", "en")
        with open(self.cfg / "QElectroTech.conf", "w") as fh:
            for section, values in conf.items():
                fh.write(f"[{section}]\n")
                for k, v in values.items():
                    fh.write(f"{k}={v}\n")
                fh.write("\n")
        if setup:
            setup(self)
        self.size = size
        self.main_name = main_name     # the element editor's title has no "QElectroTech"
        self.args = list(args)
        self.extra_env = env or {}
        self.app_env = app_env or {}      # for the QET process only (LD_PRELOAD etc.)
        self.openbox_rc = openbox_rc      # a private openbox config, for recipes that need keys the default takes
        self.procs = []

    # -- lifecycle -------------------------------------------------------
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    @property
    def env(self):
        e = dict(os.environ)
        e.update(DISPLAY=f":{self.display}", HOME=str(self.home),
                 XDG_CONFIG_HOME=str(self.home / "cfg"), XDG_DATA_HOME=str(self.data),
                 TMPDIR=str(self.dir), LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8",
                 LANGUAGE="en")
        e.pop("WAYLAND_DISPLAY", None)
        # No session bus: unsetting the variable is not enough, Qt still
        # finds $XDG_RUNTIME_DIR/bus, so point
        # both at nothing private to this session.
        run = self.dir / "run"
        run.mkdir(mode=0o700, exist_ok=True)
        e.update(DBUS_SESSION_BUS_ADDRESS=f"unix:path={run}/no-session-bus", XDG_RUNTIME_DIR=str(run))
        e.update(self.extra_env)
        return e

    def start(self, wait_collection=True, dismiss=True):
        self.display = _free_display()
        w, h = self.size
        self.procs.append(subprocess.Popen(
            ["Xvfb", f":{self.display}", "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for _ in range(50):
            if Path(f"/tmp/.X11-unix/X{self.display}").exists():
                break
            time.sleep(0.1)
        cmd = ["openbox"] + (["--config-file", str(self.openbox_rc)] if self.openbox_rc else [])
        self.procs.append(subprocess.Popen(cmd, env=self.env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(0.5)
        self.log = open(self.dir / "qet.log", "w")
        cmd = [str(self.binary)] + self.args + ([str(self.project)] if self.project else [])
        self.qet = subprocess.Popen(cmd, env={**self.env, **self.app_env}, stdout=self.log, stderr=subprocess.STDOUT)
        self.procs.append(self.qet)
        self.main = self.wait_window(self.main_name, timeout=40)
        self.run("xdotool", "windowsize", self.main, str(w), str(h), "windowmove", self.main, "0", "0")
        time.sleep(1)
        if dismiss:
            self.dismiss_dialogs()
        if wait_collection:
            for _ in range(80):
                if "Count All Elements in collections" in (self.dir / "qet.log").read_text(errors="replace"):
                    break
                time.sleep(0.5)
            time.sleep(4)
            # A big project asks about a backup copy only after loading.
            if dismiss:
                self.dismiss_dialogs()

    def stop(self):
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs = []
        if self.out.resolve().is_relative_to(self.dir.resolve()) is False and not getattr(self, "keep", False):
            shutil.rmtree(self.dir, ignore_errors=True)   # the copy of QET and its sandbox

    # -- X helpers --------------------------------------------------------
    def run(self, *cmd, check=False) -> str:
        r = subprocess.run([str(c) for c in cmd], env=self.env, capture_output=True, text=True)
        if check and r.returncode:
            raise RuntimeError(f"{cmd}: {r.stderr}")
        return r.stdout.strip()

    def windows(self, name=".") -> list[tuple[str, str, tuple]]:
        """(id, title, (x, y, w, h)) of the visible windows whose title matches."""
        out = []
        for wid in self.run("xdotool", "search", "--onlyvisible", "--name", name).split():
            title = self.run("xdotool", "getwindowname", wid)
            geo = self.run("xdotool", "getwindowgeometry", "--shell", wid)
            g = dict(l.split("=") for l in geo.splitlines() if "=" in l)
            if not g:
                continue
            box = (int(g["X"]), int(g["Y"]), int(g["WIDTH"]), int(g["HEIGHT"]))
            if box[2] > 2 and box[3] > 2:
                out.append((wid, title, box))
        return out

    def wait_window(self, name, timeout=15) -> str:
        end = time.time() + timeout
        while time.time() < end:
            ws = [w for w in self.windows(name) if w[2][2] > 50]
            if ws:
                return max(ws, key=lambda w: w[2][2] * w[2][3])[0]
            time.sleep(0.3)
        raise TimeoutError(f"no window {name!r}; have {[w[1] for w in self.windows()]}")

    def geometry(self, wid) -> tuple:
        """(x, y, w, h) of a window's client area on the screen. xwininfo,
        not xdotool: inside an openbox frame xdotool reports a position off
        by the title bar."""
        info = self.run("xwininfo", "-id", wid)
        g = {k.strip(): int(v) for k, v in re.findall(r"(Absolute upper-left [XY]|Width|Height):\s+(-?\d+)", info)}
        return (g["Absolute upper-left X"], g["Absolute upper-left Y"], g["Width"], g["Height"])

    def activate(self, wid):
        self.run("xdotool", "windowactivate", "--sync", wid)
        time.sleep(0.2)

    def key(self, *keys, delay=0.25):
        for k in keys:
            self.run("xdotool", "key", "--clearmodifiers", k)
            time.sleep(delay)

    def type(self, text, delay=0.3):
        self.run("xdotool", "type", "--delay", "40", text)
        time.sleep(delay)

    def move(self, x, y, delay=0.2):
        self.run("xdotool", "mousemove", x, y)
        time.sleep(delay)

    def click(self, x, y, button=1, delay=0.4, double=False):
        self.run("xdotool", "mousemove", x, y)
        time.sleep(0.1)
        self.run("xdotool", "click", *(["--repeat", "2"] if double else []), button)
        time.sleep(delay)

    def drag(self, x1, y1, x2, y2, button=1, steps=12, delay=0.4, keys=()):
        for k in keys:
            self.run("xdotool", "keydown", k)
        self.run("xdotool", "mousemove", x1, y1)
        self.run("xdotool", "mousedown", button)
        for i in range(1, steps + 1):
            self.run("xdotool", "mousemove", int(x1 + (x2 - x1) * i / steps), int(y1 + (y2 - y1) * i / steps))
            time.sleep(0.03)
        time.sleep(0.2)
        self.run("xdotool", "mouseup", button)
        for k in keys:
            self.run("xdotool", "keyup", k)
        time.sleep(delay)

    def wait(self, s):
        time.sleep(s)

    def grab(self) -> Image.Image:
        """The whole screen, as Pillow RGB. scrot, not xwd: xwd shows dialogs
        black and its 24-bit output was read at three-quarters width."""
        png = self.dir / "screen.png"
        png.unlink(missing_ok=True)
        subprocess.run(["scrot", "--overwrite", str(png)], env=self.env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return Image.open(png).convert("RGB")

    def dismiss_dialogs(self, keep=None, tries=6):
        """Close every visible window but the main one (startup prompts)."""
        for _ in range(tries):
            keep_names = keep or (self.main_name,)
            extra = [w for w in self.windows(".") if w[0] != self.main and w[2][2] > 50
                     and not any(k in w[1] for k in keep_names)]
            if not extra:
                return
            self.activate(extra[0][0])
            self.key("Escape", delay=0.6)

    @staticmethod
    def diffbox(before: Image.Image, after: Image.Image, pad=0, region=None) -> tuple:
        """(x, y, w, h) of what changed between two grabs, optionally only
        inside region = (x, y, w, h). For popups, which have no title."""
        from PIL import ImageChops
        a, b = before, after
        if region:
            x, y, w, h = region
            a, b = a.crop((x, y, x + w, y + h)), b.crop((x, y, x + w, y + h))
        box = ImageChops.difference(a, b).getbbox()
        if not box:
            raise RuntimeError("nothing changed")
        ox, oy = (region[0], region[1]) if region else (0, 0)
        return (box[0] + ox - pad, box[1] + oy - pad, box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad)

    def popups(self) -> list[tuple]:
        """(x, y, w, h) of the mapped top-level popup windows: open menus,
        submenus, completers and tooltips, newest last."""
        out = []
        for line in self.run("xwininfo", "-root", "-children").splitlines():
            # Qt popups are override-redirect: direct children of the root,
            # titled like the app and with its class; the main window is
            # inside an openbox frame instead.
            m = re.match(r'\s*(0x[0-9a-f]+) "QElectroTech": \("qet" "QElectroTech"\)\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)', line)
            if not m:
                continue
            wid, w, h, x, y = m.group(1), *map(int, m.groups()[1:])
            if w < 20 or h < 10:
                continue
            state = self.run("xwininfo", "-id", wid)
            if "IsViewable" in state:
                out.append((x, y, w, h))
        return out

    @staticmethod
    def union(*boxes, pad=0) -> tuple:
        x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
        x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
        return (x0 - pad, y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad)

    def save_popups(self, name, boxes=None, bg="white") -> Path:
        """The open popups (menu and its submenus) on a plain background,
        cropped to their union: nothing from the window behind shows."""
        boxes = boxes or self.popups()
        screen = self.grab()
        x, y, w, h = self.union(*boxes)
        canvas = Image.new("RGB", (w, h), bg)
        for bx, by, bw, bh in boxes:
            canvas.paste(screen.crop((bx, by, bx + bw, by + bh)), (bx - x, by - y))
        p = self.out / f"{name}.png"
        canvas.save(p)
        return p

    def save(self, name, im: Image.Image, crop=None) -> Path:
        if crop:
            x, y, w, h = crop
            im = im.crop((x, y, x + w, y + h))
        p = self.out / f"{name}.png"
        im.save(p)
        return p

    def shot(self, name, crop=None, window=None, pad=0) -> Path:
        """Save the screen (or @p crop = (x, y, w, h), or a window's box) as
        <out>/<name>.png and return the path."""
        im = self.grab()
        if window is not None:
            x, y, w, h = self.geometry(window)
            crop = (x - pad, y - pad, w + 2 * pad, h + 2 * pad)
        if crop:
            x, y, w, h = crop
            im = im.crop((x, y, x + w, y + h))
        p = self.out / f"{name}.png"
        im.save(p)
        return p
