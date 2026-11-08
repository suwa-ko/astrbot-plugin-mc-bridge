"""Durable, bounded task outcome metrics for the Minecraft agent.

The scheduler keeps an in-memory audit for live inspection.  This module adds a
small JSONL store so success rate, duration and failure reasons survive plugin
reloads.  It has no AstrBot, aiohttp or Minecraft imports and is safe to unit
test with a temporary directory.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path

STATUSES = {"completed", "failed", "cancelled"}


def _result_summary(value, limit=1000):
    if value is None:
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text[:max(0, int(limit))]


def _aggregate(rows):
    total = len(rows)
    completed = sum(1 for row in rows if row.get("status") == "completed")
    failed = sum(1 for row in rows if row.get("status") == "failed")
    cancelled = sum(1 for row in rows if row.get("status") == "cancelled")
    goal_fulfilled = sum(1 for row in rows if row.get("goal_outcome") == "fulfilled")
    goal_partial = sum(1 for row in rows if row.get("goal_outcome") == "partial")
    goal_unmet = sum(1 for row in rows if row.get("goal_outcome") == "unmet")
    goal_unknown = sum(1 for row in rows if row.get("goal_outcome") not in {"fulfilled", "partial", "unmet"})
    durations = sorted(float(row.get("duration", 0) or 0) for row in rows)
    average = round(sum(durations) / total, 3) if total else 0.0
    p95 = durations[min(total - 1, max(0, math.ceil(total * 0.95) - 1))] if total else 0.0
    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "cancelled": cancelled,
        "goal_fulfilled": goal_fulfilled,
        "goal_partial": goal_partial,
        "goal_unmet": goal_unmet,
        "goal_unknown": goal_unknown,
        "goal_fulfilled_rate": round(goal_fulfilled / total, 4) if total else 0.0,
        "success_rate": round(completed / total, 4) if total else 0.0,
        "avg_seconds": average,
        "p95_seconds": round(float(p95), 3),
    }


class TaskMetrics:
    def __init__(self, path, max_records=1000):
        self.path = Path(path)
        self.max_records = max(20, int(max_records))
        self.records = []
        self._lock = threading.Lock()
        self._appended = 0
        self.load()

    def _clean(self, row):
        if not isinstance(row, dict):
            return None
        opcode = str(row.get("opcode") or "").strip()
        status = str(row.get("status") or "").strip().lower()
        if not opcode or status not in STATUSES:
            return None
        try:
            duration = max(0.0, float(row.get("duration", 0) or 0))
        except (TypeError, ValueError):
            duration = 0.0
        return {
            "opcode": opcode[:80],
            "status": status,
            "duration": round(duration, 3),
            "task_id": str(row.get("task_id") or "")[:80],
            "priority": int(row.get("priority", 20) or 20),
            "error": str(row.get("error") or "")[:500],
            "result": str(row.get("result") or "")[:1000],
            "goal_outcome": str(row.get("goal_outcome") or "").strip().lower()[:20],
            "recorded_at": float(row.get("recorded_at", 0) or time.time()),
        }

    def load(self):
        self.records = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = self._clean(json.loads(line))
                    except Exception:
                        continue
                    if row is not None:
                        self.records.append(row)
        except FileNotFoundError:
            self.records = []
        except Exception:
            self.records = []
        if len(self.records) > self.max_records:
            self.records = self.records[-self.max_records:]
            self.compact()

    def record(self, opcode, status, duration, *, task_id="", priority=20, error="", result=None, goal_outcome=""):
        row = self._clean({
            "opcode": opcode,
            "status": status,
            "duration": duration,
            "task_id": task_id,
            "priority": priority,
            "error": error,
            "result": _result_summary(result),
            "goal_outcome": goal_outcome,
            "recorded_at": time.time(),
        })
        if row is None:
            raise ValueError("invalid task metric")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.records.append(row)
            if len(self.records) > self.max_records:
                self.records = self.records[-self.max_records:]
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self._appended += 1
                if self._appended > self.max_records:
                    self.compact()
            except Exception:
                pass
        return dict(row)

    def compact(self):
        payload = "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
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
        by_opcode = {}
        for opcode in sorted({row["opcode"] for row in rows}):
            by_opcode[opcode] = _aggregate([row for row in rows if row["opcode"] == opcode])
        overall = _aggregate(rows)
        alerts = []
        for opcode, stats in by_opcode.items():
            total = stats["total"]
            if total >= 5 and stats["failed"] / total >= 0.6:
                alerts.append({
                    "type": "low_success_rate",
                    "opcode": opcode,
                    "samples": total,
                    "failed": stats["failed"],
                    "success_rate": stats["success_rate"],
                    "message": f"{opcode} 最近 {total} 次失败 {stats['failed']} 次",
                })
            if total >= 5 and stats["cancelled"] / total >= 0.5:
                alerts.append({
                    "type": "high_cancel_rate",
                    "opcode": opcode,
                    "samples": total,
                    "cancelled": stats["cancelled"],
                    "message": f"{opcode} 最近 {total} 次取消 {stats['cancelled']} 次",
                })
        if overall["total"] >= 10 and overall["success_rate"] < 0.5:
            alerts.append({
                "type": "overall_low_success_rate",
                "samples": overall["total"],
                "success_rate": overall["success_rate"],
                "message": f"整体任务成功率偏低：{overall['success_rate']:.0%}",
            })
        alerts.sort(key=lambda item: (item.get("type", ""), item.get("opcode", "")))
        return {
            "overall": overall,
            "by_opcode": by_opcode,
            "alerts": alerts,
            "latest": [dict(row) for row in rows[-max(1, int(limit)):]],
        }
