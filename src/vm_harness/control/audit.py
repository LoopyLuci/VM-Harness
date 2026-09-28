"""The audit log: every operation that changed something, who asked for it, and how it ended.

One JSON object per line in ``<home>/audit/audit.jsonl``. Each entry carries the SHA-256 of the one before it, so an
entry that is edited or removed afterwards breaks the chain, and ``verify`` says where. Argument values that look like
secrets (passwords, tokens, keys, file contents) are never written.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

SECRET_KEY = re.compile(r"pass|secret|token|key|credential|content", re.I)


def redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "..."
    if isinstance(value, dict):
        return {k: ("***" if SECRET_KEY.search(str(k)) and v not in (None, "") else redact(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + f"... ({len(value)} chars)"
    return value


class AuditLog:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last = self._tail_hash()

    def _tail_hash(self) -> str:
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 65536))
                lines = [l for l in f.read().splitlines() if l.strip()]
            return json.loads(lines[-1])["hash"] if lines else ""
        except (OSError, ValueError, KeyError, IndexError):
            return ""

    def record(self, operation: str, args: dict, *, client: str, ok: bool, error: str = "",
               duration_s: float = 0.0) -> dict:
        with self._lock:
            entry = {"ts": time.time(), "operation": operation, "args": redact(args), "client": client, "ok": ok,
                     "error": error[:500], "duration_s": round(duration_s, 3), "prev": self._last}
            entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            self._last = entry["hash"]
            return entry

    def query(self, limit: int = 100, operation: str = "", since: float = 0.0) -> list[dict]:
        out: list[dict] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return out
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if since and e.get("ts", 0) < since:
                break
            if operation and not str(e.get("operation", "")).startswith(operation):
                continue
            out.append(e)
            if len(out) >= limit:
                break
        return out

    def verify(self) -> tuple[bool, str]:
        prev = ""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return True, "empty"
        for n, line in enumerate(lines, 1):
            try:
                e = json.loads(line)
            except ValueError:
                return False, f"line {n} is not JSON"
            h = e.pop("hash", "")
            if e.get("prev") != prev:
                return False, f"line {n}: chain broken (an entry before it was changed or removed)"
            if hashlib.sha256(json.dumps(e, sort_keys=True).encode()).hexdigest() != h:
                return False, f"line {n}: entry was changed"
            prev = h
        return True, f"{len(lines)} entries, chain intact"
