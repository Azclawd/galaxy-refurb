"""Shared state between the workflow thread and the web page.

The page long-polls /api/state and renders whatever is here. When the workflow needs the
person at the desk, it puts a `prompt` here and waits for the page to post an answer.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque

from .diagnose import Diagnosis
from .log import log


class Cancelled(Exception):
    pass


class Session:
    def __init__(self, app_version: str, data_home: str):
        self._cond = threading.Condition()
        self.version = 0
        self.app = {"version": app_version, "data_home": data_home}
        self.device: dict = {"state": "none", "label": "Plug in a phone", "hint": "", "model": "", "serial": ""}
        self.job: dict | None = None
        self.prompt: dict | None = None
        self._answers: dict[int, object] = {}
        self._ids = itertools.count(1)
        self.logs: deque[dict] = deque(maxlen=400)
        self.cancel_requested = False
        self.last_seen = time.time()

    # ------------------------------------------------------------------ plumbing
    def _bump(self) -> None:
        self.version += 1
        self._cond.notify_all()

    def snapshot(self) -> dict:
        with self._cond:
            return {
                "version": self.version,
                "app": self.app,
                "device": self.device,
                "job": self.job,
                "prompt": self.prompt,
                "log": list(self.logs)[-150:],
            }

    def wait_for_change(self, since: int, timeout: float = 20.0) -> dict:
        self.last_seen = time.time()
        with self._cond:
            if self.version <= since:
                self._cond.wait(timeout)
        return self.snapshot()

    # ------------------------------------------------------------------ device + log
    def set_device(self, status: dict) -> None:
        with self._cond:
            self.device = status
            self._bump()

    def add_log(self, msg: str, level: str = "info") -> None:
        log(f"[{level}] {msg}")
        with self._cond:
            self.logs.append({"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
            self._bump()

    # ------------------------------------------------------------------ job + steps
    def new_job(self, steps: list[tuple[str, str]]) -> None:
        with self._cond:
            self.cancel_requested = False
            self.logs.clear()
            self.job = {
                "running": True,
                "started": time.time(),
                "phone": {},
                "result": None,
                "steps": [{"key": k, "title": t, "status": "pending", "detail": "", "checks": []} for k, t in steps],
            }
            self._bump()

    def _step(self, key: str) -> dict:
        assert self.job is not None
        for s in self.job["steps"]:
            if s["key"] == key:
                return s
        raise KeyError(key)

    def set_step(self, key: str, status: str | None = None, detail: str | None = None) -> None:
        with self._cond:
            s = self._step(key)
            if status is not None:
                s["status"] = status
            if detail is not None:
                s["detail"] = detail
            self._bump()

    def add_check(self, key: str, title: str, status: str, detail: str = "") -> None:
        """A sub-check under a step, e.g. 'No store apps' ✓."""
        with self._cond:
            self._step(key)["checks"].append({"title": title, "status": status, "detail": detail})
            self._bump()

    def set_phone(self, **fields: str) -> None:
        with self._cond:
            if self.job is not None:
                self.job["phone"].update({k: v for k, v in fields.items() if v not in (None, "")})
                self._bump()

    def finish(self, status: str, title: str, detail: str = "", diagnosis: Diagnosis | None = None) -> None:
        with self._cond:
            if self.job is not None:
                self.job["running"] = False
                self.job["result"] = {"status": status, "title": title, "detail": detail,
                                      "diagnosis": diagnosis.to_dict() if diagnosis else None}
            self.prompt = None
            self._bump()

    def clear_job(self) -> None:
        with self._cond:
            self.job = None
            self.prompt = None
            self._bump()

    # ------------------------------------------------------------------ prompts
    def show_prompt(self, kind: str, title: str, text: str = "", steps: list[str] | None = None,
                    options: list[dict] | None = None, placeholder: str = "", link: dict | None = None,
                    diagnosis: Diagnosis | None = None, waiting: str = "", error: str = "") -> int:
        """Show a card to the user. kind: steps | choice | text. Returns the prompt id."""
        with self._cond:
            pid = next(self._ids)
            self.prompt = {"id": pid, "kind": kind, "title": title, "text": text, "steps": steps or [],
                           "options": options or [], "placeholder": placeholder, "link": link,
                           "diagnosis": diagnosis.to_dict() if diagnosis else None,
                           "waiting": waiting, "error": error}
            self._bump()
            return pid

    def answer(self, pid: int, value: object) -> bool:
        with self._cond:
            if not self.prompt or self.prompt["id"] != pid:
                return False
            self._answers[pid] = value
            self.prompt = None
            self._bump()
            return True

    def poll_answer(self, pid: int, timeout: float) -> tuple[bool, object]:
        """Wait up to timeout for an answer to prompt pid. Returns (answered, value)."""
        end = time.time() + timeout
        with self._cond:
            while pid not in self._answers:
                if self.cancel_requested:
                    self.prompt = None
                    self._bump()
                    raise Cancelled()
                left = end - time.time()
                if left <= 0:
                    return False, None
                self._cond.wait(left)
            return True, self._answers.pop(pid)

    def close_prompt(self, pid: int) -> None:
        with self._cond:
            if self.prompt and self.prompt["id"] == pid:
                self.prompt = None
                self._bump()

    def ask(self, kind: str, title: str, **kw) -> object:
        pid = self.show_prompt(kind, title, **kw)
        while True:
            answered, value = self.poll_answer(pid, 1.0)
            if answered:
                return value

    def request_cancel(self) -> None:
        with self._cond:
            self.cancel_requested = True
            self._bump()

    def check_cancel(self) -> None:
        if self.cancel_requested:
            raise Cancelled()
