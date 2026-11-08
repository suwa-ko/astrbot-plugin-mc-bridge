"""Durable task recovery snapshots with explicit confirmation.

A reload must not silently resume old Minecraft work.  This store keeps only the
metadata needed to show interrupted tasks and, after an explicit resume request,
re-enqueue them with a new task id.  It never starts anything by itself.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path


class TaskRecovery:
    def __init__(self, path, max_records=20):
        self.path = Path(path)
        self.max_records = max(1, int(max_records))
        self._lock = threading.Lock()
        self.items = []
        self.load()

    def _clean(self, row):
        if not isinstance(row, dict):
            return None
        recovery_id = str(row.get("recovery_id") or "").strip()
        opcode = str(row.get("opcode") or "").strip()
        if not recovery_id or not opcode:
            return None
        params = row.get("params")
        if not isinstance(params, dict):
            params = {}
        try:
            priority = max(0, min(40, int(row.get("priority", 20) or 20)))
        except (TypeError, ValueError):
            priority = 20
        try:
            remaining = max(1.0, min(3600.0, float(row.get("remaining_seconds", 60) or 60)))
        except (TypeError, ValueError):
            remaining = 60.0
        resources = row.get("resources")
        if not isinstance(resources, list):
            resources = []
        return {
            "recovery_id": recovery_id[:80],
            "opcode": opcode[:80],
            "params": params,
            "priority": priority,
            "resources": [str(item)[:40] for item in resources][:16],
            "goal": str(row.get("goal") or "")[:160],
            "remaining_seconds": round(remaining, 3),
            "saved_at": float(row.get("saved_at", 0) or time.time()),
        }

    def load(self):
        self.items = []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows = raw.get("items") if isinstance(raw, dict) else raw
            if not isinstance(rows, list):
                rows = []
            seen = set()
            for row in rows:
                cleaned = self._clean(row)
                if cleaned is None or cleaned["recovery_id"] in seen:
                    continue
                seen.add(cleaned["recovery_id"])
                self.items.append(cleaned)
        except FileNotFoundError:
            self.items = []
        except Exception:
            self.items = []
        if len(self.items) > self.max_records:
            self.items = self.items[-self.max_records:]

    def save(self):
        payload = {"version": 1, "items": self.items[-self.max_records:]}
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

    def merge(self, rows):
        with self._lock:
            current = {row["recovery_id"]: row for row in self.items}
            for row in rows:
                cleaned = self._clean(row)
                if cleaned is not None:
                    current[cleaned["recovery_id"]] = cleaned
            self.items = list(current.values())[-self.max_records:]
            self.save()
        return self.snapshot()

    def get(self, recovery_id):
        recovery_id = str(recovery_id or "")
        for row in self.items:
            if row["recovery_id"] == recovery_id:
                return dict(row)
        return None

    def discard(self, recovery_id):
        recovery_id = str(recovery_id or "")
        with self._lock:
            before = len(self.items)
            self.items = [row for row in self.items if row["recovery_id"] != recovery_id]
            changed = len(self.items) != before
            if changed:
                self.save()
        return changed

    def clear(self):
        with self._lock:
            self.items = []
            self.save()

    def snapshot(self):
        return {
            "count": len(self.items),
            "items": [dict(row) for row in self.items],
            "max_records": self.max_records,
        }
