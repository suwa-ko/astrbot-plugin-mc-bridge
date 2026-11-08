"""Bounded, persistent short-term MC chat history.

Long-term facts still belong to `shared_memory`.  This store only keeps the
recent MC private/game conversation so a plugin reload does not erase the
immediately preceding context.  It is intentionally small, TTL-limited and
private to the plugin data directory.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path


class ShortHistoryStore:
    def __init__(self, path, *, ttl_seconds=86400, max_sessions=20, max_lines=20, max_chars=2000, clock=time.time):
        self.path = Path(path)
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_sessions = max(1, int(max_sessions))
        self.max_lines = max(1, int(max_lines))
        self.max_chars = max(80, int(max_chars))
        self.clock = clock
        self.sessions = {}
        self._lock = threading.Lock()
        self.load()

    def _clean_lines(self, lines):
        if not isinstance(lines, list):
            return []
        result = []
        for line in lines[-self.max_lines:]:
            if not isinstance(line, str):
                continue
            line = line.strip()
            if line:
                result.append(line[:self.max_chars])
        return result

    def _trim_sessions(self):
        if len(self.sessions) <= self.max_sessions:
            return
        ordered = sorted(self.sessions.items(), key=lambda item: float(item[1].get("updated_at", 0) or 0), reverse=True)
        self.sessions = dict(ordered[: self.max_sessions])

    def load(self):
        self.sessions = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows = raw.get("sessions") if isinstance(raw, dict) else None
            if not isinstance(rows, dict):
                return
            now = self.clock()
            for key, value in rows.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    continue
                updated = float(value.get("updated_at", 0) or 0)
                if self.ttl_seconds and now - updated > self.ttl_seconds:
                    continue
                lines = self._clean_lines(value.get("lines"))
                if lines:
                    self.sessions[key[:200]] = {"updated_at": updated, "lines": lines}
            self._trim_sessions()
        except FileNotFoundError:
            self.sessions = {}
        except Exception:
            self.sessions = {}

    def save(self):
        payload = {"version": 1, "sessions": self.sessions}
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def get(self, key):
        row = self.sessions.get(str(key or "")[:200])
        return list(row["lines"]) if row else []

    def items(self):
        return {key: list(row["lines"]) for key, row in self.sessions.items()}

    def set(self, key, lines):
        key = str(key or "").strip()[:200]
        if not key:
            return
        cleaned = self._clean_lines(list(lines or []))
        with self._lock:
            if cleaned:
                self.sessions[key] = {"updated_at": self.clock(), "lines": cleaned}
            else:
                self.sessions.pop(key, None)
            self._trim_sessions()
            self.save()

    def clear(self, key):
        key = str(key or "").strip()[:200]
        with self._lock:
            if self.sessions.pop(key, None) is not None:
                self.save()

    def snapshot(self):
        return {
            "sessions": len(self.sessions),
            "lines": sum(len(row["lines"]) for row in self.sessions.values()),
            "ttl_seconds": self.ttl_seconds,
            "max_sessions": self.max_sessions,
            "max_lines": self.max_lines,
        }
