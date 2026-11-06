# SPDX-License-Identifier: GPL-2.0-or-later
"""Run a how-to story: a YAML storyboard becomes a recorded video.

    python3 story.py stories/add-a-fillet.yaml \
        --binary <build>/qelectrotech --out <dir> [--qet-dir <checkout>]

Every target is found through the probe (probe.py), never measured: menus
by their command text, buttons by their command text, sheet clicks by sheet
coordinates. Steps wait on what QET reports (a dialog opened, an undo step
was pushed), not on fixed sleeps.

Steps (one main key each; extra keys are options):

  title: Add a fillet            sub: ...            hold: 4
  chapter: Draw two lines                            (caption + chapter mark)
  caption: "Pick it from {path:Add a fillet}"        hold: 1
      {path:CMD} -> "Edit ▸ Add ▸ CMD"   {tip:CMD} -> its status tip
  end_caption:
  menu: Add line                 walk the menu path to the command and click it
  toolbar: Add a fillet          click the command's toolbar button
  click: [x, y]                  a sheet point        button: right   double: true
  click: {widget: {class: QStatusBar}}   or  {toolbar: CMD}  or  {item: {class: .., n: 1, at: 0.5}}
         {widget: {class: QComboBox, text: Top-Left}}  a drop-down by what it shows
         {combo_item: Bottom-Right}     an entry of the drop-down list that is open
  drag: {from: [x, y], to: [x, y]}
  move: [x, y] | {widget: {...}} pointer only, no click
  key: ctrl+z                    shown on screen     press: ctrl+a  (not shown)
  type: "40"
  wait_for: {dialog: Fillet} | {dialog_closed: Fillet} | {undo: Add a fillet}
            | {status: ...} | {action: Add line}         timeout: 10
  highlight: {widget: {class: QStatusBar}}     secs: 3   left: 0.3 (fraction of the width)
  highlight: {rect: [x, y, w, h]}              an area of the sheet
  frame: {rect: [x, y, w, h], fill: 0.7}       zoom the sheet onto an area
  focus: {rect: [x, y, w, h]} | {widget: ..}   secs: 4   the camera frames it, clear
                                 of the caption; nothing is clicked or drawn
  hold: 2
  wheel: [x, y] | {widget: ..}     notches: 3   dir: up|down   hold_key: alt

`expect:` is checked against the saved project (and the undo steps seen):
  shapes: {type: Line, count: 2}      undo: Add a fillet      arcs_tangent: true
  aligned: [left, top]                every Rectangle shares those edges
  saved_text: 'cell_origin corner="bottom-right"'   a regex in the saved files
  view_zoom: 1.30                     the sheet's zoom at the end (130 %)
  (types: Line, Rectangle, Arc, Ellipse...; no count means at least one)
`--check-only` runs everything fast, records nothing, exits 1 if expect fails.

`setup:` steps run before recording starts, with the same vocabulary.

Thumbnail (optional): `thumbnail: {before: 2}` puts the work as it was when
chapter 2 starts (taken as the pointer reaches the menu bar) next to the
result at the end; `icon: <command>` picks the toolbar icon shown (default:
the last `menu:` command).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clip as clipmod  # noqa: E402
from clip import Clip, nap  # noqa: E402

PACE = 0.5          # seconds a viewer gets after every action
OPTIONS = {"sub", "hold", "button", "timeout", "secs", "fill", "show", "pace", "left",
           "notches", "dir", "hold_key", "gap", "double"}


class StoryError(RuntimeError):
    pass


def split(step: dict):
    """(kind, value, options) of one step."""
    kinds = list(step) if len(step) == 1 else [k for k in step if k not in OPTIONS]
    if len(kinds) != 1:
        raise StoryError(f"a step needs exactly one main key, got {list(step)}")
    return kinds[0], step[kinds[0]], {k: v for k, v in step.items() if k in OPTIONS}


class Runner:
    def __init__(self, clip: Clip):
        self.c = clip
        self.p = clip.probe
        self.mark = self.p.mark()

    # -- targets -----------------------------------------------------------
    def point(self, target):
        """Screen pixel for a story target."""
        if isinstance(target, (list, tuple)):
            return self.p.to_screen(*target)
        if "toolbar" in target:
            box = self.p.toolbar(target["toolbar"])
            if not box:
                raise StoryError(f"no toolbar button for {target['toolbar']!r}")
            return self.p.center(box)
        if "widget" in target:
            return self.p.center(self.p.wait_widget(**target["widget"])["box"])
        if "combo_item" in target:
            return self.p.center(self.p.combo_item(target["combo_item"]))
        if "item" in target:
            it = target["item"]
            items = self.p.items(it.get("class"))
            n = it.get("n", 1) - 1
            if n >= len(items):
                raise StoryError(f"item {it}: only {len(items)} on the sheet")
            o = items[n]
            at = it.get("at", 0.5)
            if "on" in o and "at" not in it:
                return self.p.to_screen(*o["on"])
            if "p1" not in o:
                x, y, w, h = o["scene_rect"]
                return self.p.to_screen(x + w / 2, y + h / 2)
            (x1, y1), (x2, y2) = o["p1"], o["p2"]
            return self.p.to_screen(x1 + (x2 - x1) * at, y1 + (y2 - y1) * at)
        raise StoryError(f"cannot find a point for {target!r}")

    def expand(self, text: str) -> str:
        def sub(m):
            what, cmd = m.group(1), m.group(2)
            if what == "path":
                return " ▸ ".join(self.p.menu_path(cmd))
            if what == "tip":
                return self.p.status_tip(cmd)
            raise StoryError(f"unknown {{{what}:…}} in caption")
        return re.sub(r"\{(path|tip):([^}]+)\}", sub, text)

    # -- steps -------------------------------------------------------------
    def run(self, steps, label="step"):
        for i, step in enumerate(steps, 1):
            kind, value, opt = split(step)
            try:
                self.step(kind, value, opt)
            except Exception as e:  # noqa: BLE001
                raise StoryError(f"{label} {i} ({kind}: {value!r}): {e}") from e

    def after(self, opt, extra=0.0):
        self.p.idle()                    # QET has handled the input before the next one arrives
        nap(opt.get("pace", PACE) + extra)

    def step(self, kind, value, opt):
        c, p = self.c, self.p
        acting = kind in ("menu", "toolbar", "click", "drag", "key", "press", "type")
        if acting:
            self.mark = p.mark()
        if kind == "title":
            c.title(self.expand(value), self.expand(opt.get("sub", "")), hold=opt.get("hold", 4))
        elif kind == "chapter":
            c.chapter(self.expand(value), hold=opt.get("hold", 1.0))
        elif kind == "caption":
            c.caption(self.expand(value), hold=opt.get("hold", 0.5))
        elif kind == "end_caption":
            c.end_caption()
        elif kind == "hold":
            c.hold(float(value))
        elif kind == "menu":
            self.menu(value)
            self.after(opt)
        elif kind == "toolbar":
            box = p.toolbar(value)
            if not box:
                raise StoryError(f"no toolbar button for {value!r}")
            c.click(*p.center(box), delay=0.3, ui=True)
            self.after(opt)
        elif kind == "click":
            x, y = self.point(value)
            c.click(x, y, button=3 if opt.get("button") == "right" else 1, delay=0.3,
                    double=bool(opt.get("double")), ui=isinstance(value, dict) and "item" not in value)
            self.after(opt)
        elif kind == "move":
            c.move(*self.point(value), delay=opt.get("pace", 0.3))
        elif kind == "drag":
            x1, y1 = self.point(value["from"])
            x2, y2 = self.point(value["to"])
            c.drag(x1, y1, x2, y2, delay=0.3)
            self.after(opt)
        elif kind == "key":
            c.key(value, delay=0.2, show=opt.get("show", True))
            self.after(opt)
        elif kind == "press":
            c.key(value, delay=0.2, show=False)
        elif kind == "type":
            c.type(str(value), delay=0.3)
            self.after(opt)
        elif kind == "wait_for":
            self.wait_for(value, opt.get("timeout", 10))
        elif kind == "highlight":
            if "rect" in value:                      # an area of the sheet, in sheet coordinates
                rx, ry, rw, rh = value["rect"]
                (x, y), (x2, y2) = p.to_screen(rx, ry), p.to_screen(rx + rw, ry + rh)
                c.highlight(x, y, x2 - x, y2 - y, secs=opt.get("secs", 3.0))
            else:
                x, y, w, h = p.wait_widget(**value["widget"])["box"]
                w = round(w * opt.get("left", 1.0))       # a part of a wide widget, from its left edge
                i = round(3 * c.scale)                     # keep the whole border on screen
                c.highlight(x + i, y + i, w - 2 * i, h - 2 * i, secs=opt.get("secs", 3.0))
        elif kind == "focus":
            if "rect" in value:                      # an area of the sheet, in sheet coordinates
                rx, ry, rw, rh = value["rect"]
                (x, y), (x2, y2) = p.to_screen(rx, ry), p.to_screen(rx + rw, ry + rh)
                c.focus(x, y, x2 - x, y2 - y, secs=opt.get("secs", 4.0))
            else:
                x, y, w, h = p.wait_widget(**value["widget"])["box"]
                c.focus(x, y, w, h, secs=opt.get("secs", 4.0))
        elif kind == "wheel":
            x, y = self.point(value)
            c.wheel(x, y, opt.get("notches", 1), up=opt.get("dir", "up") == "up",
                    keys=(opt["hold_key"],) if opt.get("hold_key") else (), gap=opt.get("gap", 0.5))
            self.after(opt)
        elif kind == "frame":
            p.frame(value["rect"], fill=value.get("fill", 0.7))
            nap(0.4)
        else:
            raise StoryError(f"unknown step {kind!r}")

    def menu(self, command):
        """Click the menu bar title, hover through submenus, click the command:
        the path and every box come from the probe."""
        path = self.p.menu_path(command)
        c, p = self.c, self.p
        for depth, label in enumerate(path):
            nap(0.25)
            box = p.open_entry(label, path[:depth + 1] if depth else None)
            x, y = p.center(box)
            # Move the way a person does. A diagonal from a menu bar title
            # crosses its neighbours on the bar, and hovering one switches
            # menus (Project -> Numbering): go straight down first. Into a
            # submenu, go across first, or the parent's entries are hovered
            # on the way and the submenu closes.
            if depth == 1:
                c.glide(c.cursor[0], y)
            elif depth >= 2:
                c.glide(x, c.cursor[1])
            if depth == len(path) - 1:
                c.click(x, y, delay=0.3, ui=True)
            elif depth == 0:
                c.click(x, y, delay=0.5, ui=True)
            else:
                c.glide(x, y)
                c.glide(x + 30 * c.scale, y)
                nap(0.8)                   # a submenu opens on hover

    def wait_for(self, what: dict, timeout):
        (kind, val), = what.items()
        event = {"dialog": "dialog_shown", "dialog_closed": "dialog_closed", "undo": "undo",
                 "status": "status", "action": "action_triggered"}[kind]
        field = "title" if kind.startswith("dialog") else "text"
        self.p.wait_event(event, since=self.mark, timeout=timeout, **{field: val})
        if kind == "dialog":
            # Log where the dialog is, for the camera. Asked now, not taken
            # from the event: the window manager may move it after it shows.
            time.sleep(0.15)
            hits = self.p.widget(**{"class": "QDialog", "title": val})
            if hits:
                x, y, w, h = hits[-1]["box"]
                self.c._log("dialog", title=val, x=x, y=y, w=w, h=h)


# -- checking the result -------------------------------------------------------
def save_project(c: Clip) -> Path:
    """Save the open project into the sandbox with Ctrl+S and return the file.
    Master saves .qetz (a zip) by default; a project opened from a .qet
    may be written as plain XML, which the checks read too."""
    p = c.probe
    m = p.mark()
    c.key("ctrl+shift+s", delay=0.3, show=False)     # Save as: a dialog even when the project has a file
    p.wait_event("dialog_shown", since=m, title="Save as")
    time.sleep(0.8)                      # the file name box takes focus a moment after the event
    target = c.dir / "check.qetz"
    c.type(str(target), delay=0.3)
    c.key("Return", delay=0.3, show=False)
    last = -1
    for _ in range(100):
        if target.exists():
            size = target.stat().st_size
            if size and size == last and (zipfile.is_zipfile(target) or target.read_bytes().rstrip().endswith(b">")):
                return target
            last = size
        time.sleep(0.1)
    raise StoryError("the project was not saved")


def saved_shapes(path: Path) -> list[dict]:
    """Every drawn shape of every folio of a saved .qetz."""
    out = []
    if not zipfile.is_zipfile(path):              # a plain .qet
        for sh in ET.parse(path).getroot().iter("shape"):
            out.append({**sh.attrib, "_children": {ch.tag: ch.attrib for ch in sh}})
        return out
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.startswith("folios/") and name.endswith(".xml"):
                for sh in ET.fromstring(z.read(name)).iter("shape"):
                    out.append({**sh.attrib, "_children": {ch.tag: ch.attrib for ch in sh}})
    return out


def kind_of(shape: dict) -> str:
    """The file stores an arc as an Ellipse with an <arc> child."""
    if shape.get("type") == "Ellipse" and "arc" in shape["_children"]:
        return "Arc"
    return shape.get("type", "?")


def arc_ends(sh: dict):
    """(centre, radius, [end1, end2]) of an arc in sheet coordinates. Qt angles
    run anticlockwise in degrees, the sheet's y axis points down."""
    import math
    x1, x2, y1, y2 = (float(sh[k]) for k in ("x1", "x2", "y1", "y2"))
    cx, cy, r = (x1 + x2) / 2, (y1 + y2) / 2, abs(x2 - x1) / 2
    a = sh["_children"]["arc"]
    ends = []
    for deg in (float(a["startAngle"]), float(a["startAngle"]) + float(a["spanAngle"])):
        ends.append((cx + r * math.cos(math.radians(deg)), cy - r * math.sin(math.radians(deg))))
    return (cx, cy), r, ends


def tangent_failures(shapes, tol=0.5) -> list[str]:
    """Each end of each arc must sit on the end of a line that touches the
    arc there without crossing it (line direction at right angles to the radius)."""
    bad = []
    lines = [s for s in shapes if kind_of(s) == "Line"]
    arcs = [s for s in shapes if kind_of(s) == "Arc"]
    if not arcs:
        return ["no arc to check for tangency"]
    for arc in arcs:
        (cx, cy), r, ends = arc_ends(arc)
        for ex, ey in ends:
            ok = False
            for ln in lines:
                p = [(float(ln["x1"]), float(ln["y1"])), (float(ln["x2"]), float(ln["y2"]))]
                for (px, py), (qx, qy) in (p, p[::-1]):
                    if abs(px - ex) <= tol and abs(py - ey) <= tol:
                        dx, dy = qx - px, qy - py
                        norm = (dx * dx + dy * dy) ** 0.5 or 1
                        # direction . radius, both unit length: 0 when tangent
                        if abs((dx * (ex - cx) + dy * (ey - cy)) / (norm * r)) < 0.02:
                            ok = True
            if not ok:
                bad.append(f"arc end ({ex:.1f}, {ey:.1f}) is not tangent to any line end")
    return bad


def aligned_failures(shapes, edges) -> list[str]:
    """Every Rectangle (a story that aligns items draws only those) shares each
    named edge: left, right, top, bottom."""
    keys = {"left": "x1", "right": "x2", "top": "y1", "bottom": "y2"}
    rects = [s for s in shapes if kind_of(s) == "Rectangle"]
    if len(rects) < 2:
        return ["aligned: fewer than two rectangles to compare"]
    bad = []
    for e in edges:
        off = "posX" if e in ("left", "right") else "posY"      # a moved shape keeps x1.. and stores its move
        vals = {round(float(r[keys[e]]) + float(r.get(off, 0)), 1) for r in rects}
        if len(vals) != 1:
            bad.append(f"edge {e} is not shared: {sorted(vals)}")
    return bad


def saved_text_of(path: Path) -> str:
    """Every text file of a saved .qetz (or the .qet itself), for `saved_text`."""
    if not zipfile.is_zipfile(path):
        return Path(path).read_text(encoding="utf-8", errors="replace")
    with zipfile.ZipFile(path) as z:
        # project.sqlite holds the engineering data (wire numbers...): its
        # text is stored as plain UTF-8, so a pattern finds it in the bytes
        return "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist()
                         if n.endswith((".xml", ".qet", ".json", ".txt", ".sqlite")))


def check_expect(expect, shapes, events, saved_text="", view_zoom=1.0) -> list[str]:
    """Failures, as sentences; empty when the saved project and the events
    match what the story promises."""
    bad = []
    for item in expect:
        (kind, v), = item.items()
        if kind == "shapes":
            n = [s for s in shapes if "type" not in v or kind_of(s) == v["type"]]
            if "count" in v:
                if len(n) != v["count"]:
                    bad.append(f"shapes {v}: found {len(n)}")
            elif not n:
                bad.append(f"shapes {v}: none found")
        elif kind == "undo":
            if not any(e.get("event") == "undo" and e.get("text") == v for e in events):
                bad.append(f"no undo step named {v!r}")
        elif kind == "aligned":
            bad += aligned_failures(shapes, v)
        elif kind == "saved_text":
            # a regular expression found in the saved project's own files
            if not re.search(v, saved_text):
                bad.append(f"saved project has no {v!r}")
        elif kind == "view_zoom":
            # the zoom of the sheet at the end, as a fraction (1.30 = 130 %)
            if abs(view_zoom - v) > 0.0015:
                bad.append(f"the sheet ends at {view_zoom * 100:.2f} %, expected {v * 100:.2f} %")
        elif kind == "arcs_tangent":
            bad += tangent_failures(shapes)
        else:
            bad.append(f"unknown expect {kind!r}")
    return bad


def load(path) -> dict:
    story = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for key in ("title", "steps"):
        if key not in story:
            raise StoryError(f"{path}: missing '{key}'")
    story["_dir"] = str(Path(path).resolve().parent)
    return story


def write_meta(story, qet, out, name, probe=None):
    """What render.py puts on the cards, the thumbnail and the description."""
    if clipmod.SLEEP_SCALE < 1:
        return
    try:
        commit = subprocess.run(["git", "-C", str(qet), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = ""
    meta = {k: story[k] for k in ("title", "summary", "wiki", "pr", "thumbnail_at", "thumbnail") if k in story}
    # The thumbnail shows the feature's toolbar icon: `icon:` names the
    # command, or the last command the story picks from a menu.
    icon = story.get("icon") or next((s["menu"] for s in reversed(story["steps"]) if "menu" in s), None)
    if probe and icon:
        box = probe.toolbar(icon)
        if box:
            meta["icon_box"] = box
    # QET's own icon for the cards, read from the checkout: it is CC-BY-ND
    # (see ico/copyright there), so this repository does not carry a copy.
    logo = Path(qet) / "ico" / "256x256" / "qelectrotech.png"
    if logo.exists():
        meta["logo"] = str(logo)
    (Path(out) / f"{name}.meta.json").write_text(json.dumps({**meta, "commit": commit}, indent=1))


def record(story: dict, binary, out, name, qet_dir=None) -> Clip:
    binary = Path(binary).resolve()
    # the QET checkout: elements, fixtures, the commit and the logo come from it
    qet = Path(qet_dir).resolve() if qet_dir else binary.parents[1]
    conf = {"elements-collections": {"common-collection-path": str(qet / "elements")}}
    fixture = story.get("fixture")
    if fixture and not Path(fixture).is_absolute():
        beside = Path(story.get("_dir", "")) / fixture
        # beside the story (a fixture made for the video), else relative to
        # the QET checkout the binary was built from
        fixture = str(beside if story.get("_dir") and beside.is_file() else qet / fixture)
    with Clip(binary, out, name, conf=conf, project=fixture) as c:
        r = Runner(c)
        if not fixture:
            c.key("ctrl+n", delay=2, show=False)
        try:
            view = c.probe.wait_widget(timeout=10, **{"class": "QGraphicsView"})
        except TimeoutError:
            # A loaded machine (several stories at once) can still be starting
            # QET when Ctrl+N is sent, and the key is lost: ask once more.
            if not fixture:
                c.key("ctrl+n", delay=2, show=False)
            view = c.probe.wait_widget(timeout=30, **{"class": "QGraphicsView"})
        c.click(*c.probe.center(view["box"]), ring=False)
        r.run(story.get("setup", []), "setup")
        c.record()
        r.run(story["steps"])
        c.hold(0.5)
        end_zoom = c.probe.view().get("zoom", 1.0)
        c.stop_recording()
        write_meta(story, qet, out, name, c.probe)
        saved = save_project(c)
        # a copy beside the outputs: the sandbox goes, and a failed check
        # is answered by reading what was actually saved
        import shutil
        shutil.copy(saved, Path(out) / f"{name}.saved{saved.suffix}")
        shapes = saved_shapes(saved)
        saved_text = saved_text_of(saved)
        events = list(c.probe.events)
    c.shapes = shapes
    if os.environ.get("STORY_DEBUG"):
        for sh in shapes:
            print("shape", sh)
    bad = check_expect(story.get("expect", []), shapes, events, saved_text, end_zoom)
    if bad:
        raise StoryError("the story does not do what it says:\n  " + "\n  ".join(bad))
    return c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("story")
    ap.add_argument("--binary", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--qet-dir", help="the QElectroTech checkout the binary was built from "
                    "(default: two levels above the binary, as in <checkout>/build/qelectrotech)")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--check-only", action="store_true",
                    help="run fast without recording and check 'expect'; exit 1 if the story no longer works")
    ap.add_argument("--speed", type=float, default=0.5,
                    help="with --check-only: fraction of the normal pauses to keep (default 0.5)")
    a = ap.parse_args()
    story = load(a.story)
    name = Path(a.story).stem
    if a.check_only:
        clipmod.SLEEP_SCALE = min(a.speed, 0.99)
        try:
            record(story, a.binary, a.out, name, a.qet_dir)
        except (StoryError, TimeoutError, RuntimeError) as e:
            print(f"FAIL {a.story}: {e}")
            return 1
        print(f"PASS {a.story}")
        return 0
    record(story, a.binary, a.out, name, a.qet_dir)
    if not a.no_render:
        from render import render
        render(Path(a.out).resolve(), name)


if __name__ == "__main__":
    sys.exit(main() or 0)
