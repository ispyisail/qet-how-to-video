# SPDX-License-Identifier: GPL-2.0-or-later
"""Client for libqetdemoprobe, the library preloaded into QElectroTech that
says where things are on screen and what the program just did.

    lib = build()                                  # once; cached
    s = Session(binary, ..., app_env=app_env(lib, sock))
    p = Probe(sock); p.connect()
    x, y = p.center(p.toolbar("Add a fillet"))
    p.wait_event("dialog_shown", title="Fillet")

python3 probe.py --binary <qelectrotech>   runs the self-test.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get("QET_HOWTO_CACHE", Path.home() / ".cache" / "qet-how-to-video"))


def build() -> Path:
    """Compile the probe (once per source change) and return the .so."""
    # One build folder per copy of this tool: CMake refuses a build folder
    # made from another source folder (a second clone, a moved one).
    import hashlib
    tag = hashlib.sha1(str(HERE).encode()).hexdigest()[:10]
    out = CACHE / f"probe-build-{tag}"
    lib = out / "libqetdemoprobe.so"
    src = HERE / "probe" / "probe.cpp"
    if lib.exists() and lib.stat().st_mtime > src.stat().st_mtime:
        return lib
    out.mkdir(parents=True, exist_ok=True)
    for cmd in (["cmake", "-S", str(HERE / "probe"), "-B", str(out), "-GNinja"], ["ninja", "-C", str(out)]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise ProbeError(f"building the probe failed ({cmd[0]}); needs cmake, ninja and the "
                             f"Qt 6 development files:\n{(r.stdout + r.stderr)[-2000:]}")
    return lib


def app_env(lib: Path, sock: Path | str) -> dict:
    return {"LD_PRELOAD": str(lib), "QET_DEMO_PROBE_SOCKET": str(sock)}


class ProbeError(RuntimeError):
    pass


class Probe:
    def __init__(self, sock):
        self.sock = str(sock)
        self.events: list[dict] = []
        self._cv = threading.Condition()
        self._listener = None

    # -- connection -------------------------------------------------------
    def _open(self):
        c = socket.socket(socket.AF_UNIX)
        c.connect(self.sock)
        return c

    def connect(self, timeout=40):
        """Wait for QET to start the probe, then subscribe to its events."""
        end = time.time() + timeout
        while True:
            try:
                self.ask("ping")
                break
            except (OSError, ProbeError):
                if time.time() > end:
                    raise ProbeError(f"probe at {self.sock} did not answer in {timeout} s: "
                                     "is LD_PRELOAD set and is the Qt version the same?")
                time.sleep(0.3)
        self._listener = threading.Thread(target=self._listen, daemon=True)
        self._listener.start()
        for _ in range(50):                    # until the subscription is live
            with self._cv:
                if getattr(self, "_subscribed", False):
                    return
            time.sleep(0.05)

    def _listen(self):
        c = self._open()
        c.sendall(b'{"cmd":"subscribe"}\n')
        for line in c.makefile():
            j = json.loads(line)
            with self._cv:
                if "event" in j:
                    self.events.append(j)
                else:
                    self._subscribed = True
                self._cv.notify_all()

    def ask(self, cmd, **kw) -> dict:
        c = self._open()
        try:
            c.settimeout(10)
            c.sendall((json.dumps({"cmd": cmd, **kw}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = c.recv(65536)
                if not chunk:
                    raise ProbeError("probe closed the connection")
                buf += chunk
        finally:
            c.close()
        return json.loads(buf)

    def need(self, cmd, **kw) -> dict:
        a = self.ask(cmd, **kw)
        if not a.get("ok"):
            raise ProbeError(f"{cmd} {kw}: {a.get('error') or 'nothing found'}")
        return a

    # -- events -----------------------------------------------------------
    def mark(self) -> int:
        """A position in the event log; pass it as since= to ignore what came before."""
        with self._cv:
            return len(self.events)

    def wait_event(self, event, since=0, timeout=10, **match) -> dict:
        """The first event of this kind after @p since whose fields equal
        @p match (title="Fillet", text="Add a line"…). Raises on timeout,
        naming what did arrive."""
        end = time.time() + timeout
        with self._cv:
            while True:
                for e in self.events[since:]:
                    if e["event"] == event and all(e.get(k) == v for k, v in match.items()):
                        return e
                left = end - time.time()
                if left <= 0:
                    seen = [(e["event"], e.get("title") or e.get("text")) for e in self.events[since:]]
                    raise TimeoutError(f"no {event} {match} in {timeout} s; saw {seen}")
                self._cv.wait(left)

    # -- locating ---------------------------------------------------------
    @staticmethod
    def center(box) -> tuple[int, int]:
        x, y, w, h = box
        return x + w // 2, y + h // 2

    def locate(self, text=None, data=None, visible=False) -> list[dict]:
        kw = {"visible": visible}
        if text:
            kw["text"] = text
        if data:
            kw["data"] = data
        return self.ask("locate", **kw).get("hits", [])

    def toolbar(self, text) -> list[int] | None:
        for h in self.locate(text):
            if h["kind"] == "toolbar":
                return h["box"]
        return None

    def menu_path(self, text) -> list[str]:
        """Titles from the menu bar down to the command, e.g. Edit, Add, Add a fillet."""
        for h in self.locate(text):
            if h["kind"] == "menu" and h.get("path"):
                return h["path"]
        raise ProbeError(f"{text!r} is in no menu")

    def status_tip(self, text) -> str:
        for h in self.locate(text):
            if h.get("status_tip"):
                return h["status_tip"]
        return ""

    def open_entry(self, text, path=None) -> list[int]:
        """Box of an entry in a menu that is open right now (or on the menu
        bar). With @p path (titles from the menu bar down to the entry), only
        the entry at exactly that place counts: "Add" alone is not unique."""
        hits = [h for h in self.locate(text, visible=True) if h.get("box") and h["kind"] in ("menu", "menubar")]
        if path:
            hits = [h for h in hits if (h.get("path") or [h["text"]]) == list(path)]
        if not hits:
            raise ProbeError(f"{text!r} (path {path}) is not on screen in an open menu")
        return hits[-1]["box"]

    def combo_item(self, text, timeout=5) -> list[int]:
        """Box of an entry in the drop-down list open right now."""
        end = time.time() + timeout
        while time.time() < end:
            for h in self.ask("combo_items").get("hits", []):
                if h["text"] == text:
                    return h["box"]
            time.sleep(0.1)
        raise ProbeError(f"no open drop-down list with {text!r}")

    def widget(self, **kw) -> list[dict]:
        return self.ask("widget", **kw).get("hits", [])

    def wait_widget(self, timeout=10, **kw) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            hits = self.widget(**kw)
            if hits:
                return hits[-1]
            time.sleep(0.1)
        raise TimeoutError(f"no widget {kw}")

    # -- the sheet --------------------------------------------------------
    def view(self) -> dict:
        return self.need("view")

    def to_screen(self, x, y) -> tuple[int, int]:
        a = self.need("scene_to_screen", x=x, y=y)
        return a["x"], a["y"]

    def frame(self, rect, fill=0.7) -> dict:
        return self.need("frame", rect=list(rect), fill=fill)

    def items(self, cls=None) -> list[dict]:
        return self.need("scene_items", **({"class": cls} if cls else {}))["items"]

    def idle(self):
        self.ask("idle")


# -- self-test -------------------------------------------------------------
def selftest(binary, qet_dir=None):
    sys.path.insert(0, str(HERE))
    from shots import Session

    binary = Path(binary).resolve()
    qet = Path(qet_dir).resolve() if qet_dir else binary.parents[1]
    conf = {"elements-collections": {"common-collection-path": str(qet / "elements")}}
    lib = build()
    fails = []

    def check(name, ok, detail=""):
        print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail and not ok else ""))
        if not ok:
            fails.append(name)

    sock = None
    s = Session(binary, None, conf=conf, size=(1920, 1080), out=CACHE / "selftest")
    sock = s.dir / "probe.sock"
    s.app_env = app_env(lib, sock)
    try:
        s.start()
        p = Probe(sock)
        p.connect()
        s.key("ctrl+n", delay=2)

        tb = p.toolbar("Add a fillet")
        check("toolbar button found", bool(tb))
        check("menu path", p.menu_path("Add a fillet") == ["Edit", "Add", "Add a fillet"], p.menu_path("Add a fillet"))
        check("status tip", "Rounds the corner" in p.status_tip("Add a fillet"))

        # Walk the menu the way the recorder will: every step found, none measured.
        s.click(*p.center(p.open_entry("Edit")), delay=0.8)
        s.move(*p.center(p.open_entry("Add")), delay=1.2)
        entry = p.open_entry("Add a fillet")
        check("submenu entry visible after hover", entry[2] > 50, entry)
        s.key("Escape", "Escape", delay=0.4)

        # Draw a line between two sheet points and read it back.
        m = p.mark()
        s.click(*p.center(tb), delay=0.5)
        p.wait_event("action_triggered", since=m, text="Add a fillet")
        s.key("Escape", delay=0.3)
        s.key("ctrl+shift+m", delay=1); s.type("Add line", delay=1); s.key("Return", delay=1)
        a, b = p.to_screen(200, 200), p.to_screen(400, 200)
        m = p.mark()
        s.click(*a, delay=0.4); s.click(*b, delay=0.4)
        u = p.wait_event("undo", since=m, text="Add a line")
        s.key("Escape", "Escape", delay=0.4)
        lines = p.items("QetShapeItem")
        check("one line drawn", len(lines) == 1, lines)
        if lines:
            p1, p2 = lines[0]["p1"], lines[0]["p2"]
            ok = abs(p1[0] - 200) <= 5 and abs(p1[1] - 200) <= 5 and abs(p2[0] - 400) <= 5 and abs(p2[1] - 200) <= 5
            check("screen point lands on the requested sheet point (snap 10)", ok, (p1, p2))

        # Dialog events: a second line, then a fillet.
        s.key("ctrl+shift+m", delay=1); s.type("Add line", delay=1); s.key("Return", delay=1)
        s.click(*p.to_screen(400, 200), delay=0.4); s.click(*p.to_screen(400, 340), delay=0.4)
        s.key("Escape", "Escape", delay=0.4)
        s.click(*p.center(tb), delay=0.5)
        m = p.mark()
        s.click(*p.to_screen(300, 200), delay=0.4)
        s.click(*p.to_screen(400, 290), delay=1)
        d = p.wait_event("dialog_shown", since=m, title="Fillet")
        check("dialog_shown with a box", len(d["box"]) == 4 and d["box"][2] > 100, d)
        s.key("Return", delay=0.5)
        p.wait_event("dialog_closed", since=m, title="Fillet")
        p.wait_event("undo", since=m, text="Add a fillet")
        check("fillet pushed an undo step", True)
        arcs = [i for i in p.items("QetShapeItem")]
        check("fillet made a third shape", len(arcs) == 3, len(arcs))

        f = p.frame([200, 200, 200, 140], fill=0.5)
        check("frame zooms in", f["zoom"] > 1.5, f)
    except Exception as e:  # noqa: BLE001
        check("selftest ran to the end", False, repr(e))
    finally:
        s.stop()
    print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--qet-dir", help="the QElectroTech checkout (default: two levels above the binary)")
    a = ap.parse_args()
    sys.exit(selftest(a.binary, a.qet_dir))
