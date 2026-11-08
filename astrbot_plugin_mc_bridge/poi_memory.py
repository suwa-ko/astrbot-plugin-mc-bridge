"""Small, bounded POI memory for L0 memory instructions.

Only coordinates and labels are stored.  No chat text, tokens or player data.
Writes are atomic; a corrupt file fails closed to an empty store.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

KINDS = {"home", "home_bed", "workbench", "furnace", "resource", "village", "danger", "chest"}
FIELDS = ("kind", "key", "name", "world_key", "dimension", "x", "y", "z", "updated_at")


class PoiMemory:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, dict] = {}
        self.load()

    @staticmethod
    def _id(kind, key):
        return f"{str(kind).strip()}:{str(key).strip()}"

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            records = raw.get("items") if isinstance(raw, dict) else None
            if not isinstance(records, list):
                return
            for row in records:
                if not isinstance(row, dict):
                    continue
                kind = str(row.get("kind") or "")
                key = str(row.get("key") or "")
                if kind not in KINDS or not key:
                    continue
                self.items[self._id(kind, key)] = {field: row.get(field) for field in FIELDS}
        except Exception:
            self.items = {}

    def save(self):
        payload = {"version": 1, "items": list(self.items.values())}
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def set(self, kind, key, name="", world_key="", dimension="", x=0, y=0, z=0):
        kind, key = str(kind).strip(), str(key).strip()
        if kind not in KINDS:
            raise ValueError("unsupported memory kind")
        if not key:
            raise ValueError("memory key is required")
        row = {
            "kind": kind, "key": key, "name": str(name or ""),
            "world_key": str(world_key or ""), "dimension": str(dimension or ""),
            "x": int(x), "y": int(y), "z": int(z), "updated_at": int(time.time()),
        }
        self.items[self._id(kind, key)] = row
        self.save()
        return dict(row)

    def get(self, kind, key):
        row = self.items.get(self._id(kind, key))
        return dict(row) if row else None

    def forget(self, kind, key):
        removed = self.items.pop(self._id(kind, key), None)
        if removed is not None:
            self.save()
            return True
        return False

    def list(self, kind=""):
        kind = str(kind or "").strip()
        rows = [dict(row) for row in self.items.values() if not kind or row.get("kind") == kind]
        rows.sort(key=lambda row: (str(row.get("kind")), str(row.get("key"))))
        return rows
