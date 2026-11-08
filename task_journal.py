"""Durable, bounded scheduler audit journal.

Scheduler audit is intentionally in-memory so the policy layer stays pure.  This
module lets the plugin persist new audit events to JSONL without coupling the
scheduler to AstrBot or the filesystem.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path


class TaskJournal:
    def __init__(self, path, max_records=2000):
        self.path = Path(path)
        self.max_records = max(20, int(max_records))
        self.records = []
        self._lock = threading.Lock()
        self._appended = 0
        self.load()

    def load(self):
        self.records = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(row, dict) and row.get("event"):
                        self.records.append(row)
        except FileNotFoundError:
            self.records = []
        except Exception:
            self.records = []
        if len(self.records) > self.max_records:
            self.records = self.records[-self.max_records:]
            self.compact()

    def record(self, event):
        if not isinstance(event, dict) or not event.get("event"):
            raise ValueError("journal event is required")
        row = dict(event)
        row.setdefault("at", time.time())
        row["journal_at"] = time.time()
        with self._lock:
            self.records.append(row)
            if len(self.records) > self.max_records:
                self.records = self.records[-self.max_records:]
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self._appended += 1
                if self._appended > self.max_records:
                    self.compact()
            except Exception:
                pass
        return dict(row)

    def record_many(self, events):
        for event in events:
            try:
                self.record(event)
            except Exception:
                continue

    def compact(self):
        payload = "".join(
            json.dumps(row, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
            for row in self.records[-self.max_records:]
        )
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._appended = 0
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def snapshot(self, limit=20):
        rows = list(self.records)
        counts = {}
        for row in rows:
            event = str(row.get("event") or "unknown")
            counts[event] = counts.get(event, 0) + 1
        return {
            "total": len(rows),
            "events": dict(sorted(counts.items())),
            "latest": [dict(row) for row in rows[-max(1, int(limit)):]],
            "last_seq": int(rows[-1].get("seq", 0) or 0) if rows else 0,
        }
