"""The app window: a tiny local web server (127.0.0.1 only) + the page in web/index.html.

Opens as its own window via Microsoft Edge's app mode on Windows (falls back to the browser).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from . import __version__, inventory
from .adb import Adb, AdbError
from .config import IS_WINDOWS, Config, load_config
from .devices import DeviceWatcher
from .session import Session
from .workflow import Workflow

DEFAULT_PORT = 8765
IDLE_EXIT_SECONDS = 120   # close the background server once the window has been gone this long


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = Session(__version__, str(cfg.data_home))
        self.adb: Adb | None = None
        if cfg.adb:
            self.adb = Adb(cfg.adb)
            try:
                self.adb.run("start-server", timeout=30)
            except AdbError as exc:
                self.session.add_log(f"adb could not start: {exc}", "error")
        self.watcher = DeviceWatcher(cfg, self.adb, lambda st: self.session.set_device(st.to_dict()))
        self.job_thread: threading.Thread | None = None
        self.tools = {"adb": bool(cfg.adb), "samloader": bool(cfg.samloader), "odin": bool(cfg.odin)}

    def busy(self) -> bool:
        return bool(self.job_thread and self.job_thread.is_alive())

    def start_job(self) -> bool:
        if self.busy():
            return False
        self.watcher.pause()

        def target():
            try:
                Workflow(self.cfg, self.session, self.adb).run()
            finally:
                self.watcher.resume()

        self.job_thread = threading.Thread(target=target, daemon=True)
        self.job_thread.start()
        return True

    def inventory_rows(self) -> list[dict]:
        return list(reversed(inventory.load(self.cfg.inventory_csv)))


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def make_handler(app: App):
    page = resources.files("galaxy_refurb").joinpath("web/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _json(self, obj, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # page reloaded mid-poll

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return {}

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)
            elif path == "/api/ping":
                self._json({"app": "galaxy-refurb", "version": __version__})
            elif path == "/api/state":
                since = -1
                for part in query.split("&"):
                    if part.startswith("v="):
                        try:
                            since = int(part[2:])
                        except ValueError:
                            pass
                snap = app.session.wait_for_change(since, 20.0)
                snap["tools"] = app.tools
                snap["busy"] = app.busy()
                self._json(snap)
            elif path == "/api/inventory":
                self._json({"rows": app.inventory_rows()})
            else:
                self.send_error(404)

        def do_POST(self):
            # Only accept requests from our own page.
            origin = self.headers.get("Origin", "")
            if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
                self.send_error(403)
                return
            body = self._body()
            if self.path == "/api/start":
                self._json({"ok": app.start_job()})
            elif self.path == "/api/answer":
                self._json({"ok": app.session.answer(int(body.get("id", 0)), body.get("value"))})
            elif self.path == "/api/cancel":
                app.session.request_cancel()
                self._json({"ok": True})
            elif self.path == "/api/next":
                if not app.busy():
                    app.session.clear_job()
                self._json({"ok": True})
            elif self.path == "/api/open":
                what = body.get("what")
                target = {"inventory": app.cfg.inventory_csv, "firmware": app.cfg.firmware_dir,
                          "logs": app.cfg.logs_dir, "data": app.cfg.data_home}.get(what)
                if target is None:
                    self._json({"ok": False}, 400)
                    return
                if target.is_file() and IS_WINDOWS:
                    os.startfile(str(target))  # type: ignore[attr-defined]
                else:
                    open_folder(target if target.suffix == "" else target.parent)
                self._json({"ok": True})
            elif self.path == "/api/open-url":
                url = str(body.get("url", ""))
                if url.startswith("https://"):
                    webbrowser.open(url)
                self._json({"ok": True})
            else:
                self.send_error(404)

    return Handler


def find_edge() -> str | None:
    if not IS_WINDOWS:
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return str(p)
    return shutil.which("msedge")


def open_window(url: str, app_home: Path) -> None:
    if os.environ.get("GALAXY_REFURB_NO_WINDOW"):
        return
    edge = find_edge()
    if edge:
        profile = app_home / "window-profile"
        subprocess.Popen([edge, f"--app={url}", "--window-size=1280,860", f"--user-data-dir={profile}",
                          "--no-first-run", "--disable-features=Translate"])
    else:
        webbrowser.open(url)


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == "galaxy-refurb"
    except Exception:
        return False


def main(port: int | None = None) -> int:
    cfg = load_config()
    port = port or int(os.environ.get("GALAXY_REFURB_PORT", DEFAULT_PORT))
    url = f"http://127.0.0.1:{port}/"
    if already_running(port):
        print("Galaxy Refurb is already running - opening its window.")
        open_window(url, cfg.app_home)
        return 0
    app = App(cfg)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/"
    server.daemon_threads = True
    app.watcher.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Galaxy Refurb {__version__} is running at {url}")
    print("Keep this window open while you work. It closes itself after you close the app window.")
    open_window(url, cfg.app_home)
    try:
        while True:
            time.sleep(5)
            idle = time.time() - app.session.last_seen
            if idle > IDLE_EXIT_SECONDS and not app.busy() and not os.environ.get("GALAXY_REFURB_STAY"):
                break
    except KeyboardInterrupt:
        pass
    server.shutdown()
    app.watcher.stop()
    return 0
