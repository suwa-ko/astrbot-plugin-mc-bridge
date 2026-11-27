"""SQLite-backed Playbook, CodeIssueQueue, StrategyBandit, and MacroRegistry."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from ...privacy import contains_coordinate, redact_text

VALID_CATEGORIES = {"planning_error", "param_error", "precondition_gap", "owner_feedback"}
VALID_STATUSES = {"candidate", "trial", "active", "retired"}


def _clean_text(value: Any, limit: int = 240) -> str:
    text = redact_text(str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _fingerprint(scope: str, text: str) -> str:
    normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "#", _clean_text(text, 200).lower())
    raw = f"{scope.strip().lower()}::{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class CoachStore:
    """Thread-safe local SQLite store for Coach lessons, bugs, bandits, and macros."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()
        self._secure_files()

    def _secure_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(str(self.path) + suffix, 0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def _ensure_schema(self) -> None:
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL DEFAULT 'global',
                    condition TEXT NOT NULL DEFAULT '',
                    rule TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    category TEXT NOT NULL DEFAULT 'planning_error',
                    source TEXT NOT NULL DEFAULT 'coach_llm',
                    status TEXT NOT NULL DEFAULT 'candidate',
                    confidence REAL NOT NULL DEFAULT 0.7,
                    injected_count INTEGER NOT NULL DEFAULT 0,
                    success_after INTEGER NOT NULL DEFAULT 0,
                    fail_after INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_used_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_lessons_scope_status
                    ON lessons(scope, status);

                CREATE TABLE IF NOT EXISTS code_issues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL DEFAULT 'global',
                    error_summary TEXT NOT NULL,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'open',
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bandit_stats (
                    context_key TEXT NOT NULL,
                    arm TEXT NOT NULL,
                    pulls INTEGER NOT NULL DEFAULT 0,
                    reward_sum REAL NOT NULL DEFAULT 0.0,
                    last_pulled_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (context_key, arm)
                );

                CREATE TABLE IF NOT EXISTS macros (
                    name TEXT PRIMARY KEY,
                    goal_pattern TEXT NOT NULL,
                    steps_json TEXT NOT NULL,
                    uses INTEGER NOT NULL DEFAULT 1,
                    avg_reward REAL NOT NULL DEFAULT 1.0,
                    updated_at REAL NOT NULL
                );
                """
            )
            self.db.commit()

    # -------------------------------------------------------------- Playbook

    def add_lesson(
        self,
        *,
        scope: str,
        rule: str,
        condition: str = "",
        category: str = "planning_error",
        source: str = "coach_llm",
        status: str = "candidate",
        confidence: float = 0.7,
    ) -> int | None:
        clean_scope = _clean_text(scope or "global", 40).lower() or "global"
        clean_rule = _clean_text(rule, 220)
        clean_cond = _clean_text(condition, 140)
        if not clean_rule or contains_coordinate(clean_rule) or contains_coordinate(clean_cond):
            return None
        cat = category if category in VALID_CATEGORIES else "planning_error"
        st = status if status in VALID_STATUSES else "candidate"
        conf = max(0.0, min(1.0, float(confidence)))
        fp = _fingerprint(clean_scope, clean_rule)
        now = time.time()
        with self.lock:
            existing = self.db.execute(
                "SELECT id, status, confidence FROM lessons WHERE fingerprint = ?", (fp,)
            ).fetchone()
            if existing is not None:
                # Owner feedback promotes existing lesson directly to active.
                new_status = "active" if st == "active" else existing["status"]
                new_conf = max(float(existing["confidence"]), conf)
                self.db.execute(
                    "UPDATE lessons SET condition = CASE WHEN ? != '' THEN ? ELSE condition END, "
                    "status = ?, confidence = ?, updated_at = ? WHERE id = ?",
                    (clean_cond, clean_cond, new_status, new_conf, now, existing["id"]),
                )
                self.db.commit()
                return int(existing["id"])
            cur = self.db.execute(
                """
                INSERT INTO lessons(
                    scope, condition, rule, fingerprint, category, source,
                    status, confidence, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (clean_scope, clean_cond, clean_rule, fp, cat, source, st, conf, now, now),
            )
            self.db.commit()
            return int(cur.lastrowid)

    def approve_lesson(self, lesson_id: int) -> bool:
        with self.lock:
            cur = self.db.execute(
                "UPDATE lessons SET status = 'active', updated_at = ? WHERE id = ?",
                (time.time(), int(lesson_id)),
            )
            self.db.commit()
            return cur.rowcount > 0

    def start_trial(self, lesson_id: int) -> bool:
        with self.lock:
            cur = self.db.execute(
                "UPDATE lessons SET status = 'trial', updated_at = ? WHERE id = ?",
                (time.time(), int(lesson_id)),
            )
            self.db.commit()
            return cur.rowcount > 0

    def retire_lesson(self, lesson_id: int) -> bool:
        with self.lock:
            cur = self.db.execute(
                "UPDATE lessons SET status = 'retired', updated_at = ? WHERE id = ?",
                (time.time(), int(lesson_id)),
            )
            self.db.commit()
            return cur.rowcount > 0

    def list_lessons(self, *, status: str = "", scope: str = "", limit: int = 20) -> list[dict[str, Any]]:
        sql = "SELECT * FROM lessons WHERE 1=1"
        args: list[Any] = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if scope:
            sql += " AND scope = ?"
            args.append(scope.strip().lower())
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(max(1, min(100, int(limit))))
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def retrieve_lessons(
        self,
        scope: str = "",
        *,
        limit: int = 3,
        include_global: bool = True,
        include_trial: bool = True,
    ) -> list[dict[str, Any]]:
        """Retrieve Top-K active lessons (plus at most 1 grayscale trial lesson)."""
        limit = max(1, min(5, int(limit)))
        clean_scope = str(scope or "").strip().lower()
        statuses = ("active", "trial") if include_trial else ("active",)
        status_ph = ",".join("?" for _ in statuses)
        with self.lock:
            if not clean_scope:
                rows = self.db.execute(
                    f"SELECT * FROM lessons WHERE status IN ({status_ph})", statuses
                ).fetchall()
            else:
                scopes = [clean_scope]
                if include_global and "global" not in scopes:
                    scopes.append("global")
                scope_ph = ",".join("?" for _ in scopes)
                rows = self.db.execute(
                    f"SELECT * FROM lessons WHERE status IN ({status_ph}) AND scope IN ({scope_ph})",
                    (*statuses, *scopes),
                ).fetchall()
        active_scored = []
        trial_scored = []
        for r in rows:
            d = dict(r)
            trials = d["success_after"] + d["fail_after"]
            win_rate = (d["success_after"] + 1.5) / (trials + 3.0)
            scope_bonus = 0.15 if clean_scope and d["scope"] == clean_scope else 0.0
            owner_bonus = 0.1 if d["source"] == "owner" else 0.0
            rank_score = win_rate * 0.6 + float(d["confidence"]) * 0.4 + scope_bonus + owner_bonus
            entry = (rank_score, d["updated_at"], d)
            if d["status"] == "trial":
                trial_scored.append(entry)
            else:
                active_scored.append(entry)
        active_scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        trial_scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        if not trial_scored or not include_trial:
            return [item[2] for item in active_scored[:limit]]
        # Reserve at most 1 slot for a grayscale trial lesson.
        active_cap = max(0, limit - 1) if limit >= 2 else (0 if not active_scored else 1)
        chosen = [item[2] for item in active_scored[:active_cap]]
        if len(chosen) < limit:
            chosen.append(trial_scored[0][2])
        return chosen[:limit]

    def record_lesson_outcome(self, lesson_ids: Iterable[int], *, fulfilled: bool) -> list[int]:
        """Update post-injection win rates, auto-promote strong trials, and auto-retire weak lessons."""
        ids = [int(i) for i in lesson_ids if i]
        if not ids:
            return []
        now = time.time()
        retired: list[int] = []
        with self.lock:
            for lid in ids:
                if fulfilled:
                    self.db.execute(
                        "UPDATE lessons SET injected_count = injected_count + 1, "
                        "success_after = success_after + 1, last_used_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, lid),
                    )
                else:
                    self.db.execute(
                        "UPDATE lessons SET injected_count = injected_count + 1, "
                        "fail_after = fail_after + 1, last_used_at = ?, updated_at = ? WHERE id = ?",
                        (now, now, lid),
                    )
                row = self.db.execute(
                    "SELECT source, status, injected_count, success_after, fail_after FROM lessons WHERE id = ?",
                    (lid,),
                ).fetchone()
                if not row or row["source"] == "owner":
                    continue
                trials = int(row["success_after"]) + int(row["fail_after"])
                rate = (int(row["success_after"]) / trials) if trials > 0 else 0.0
                if row["status"] == "trial":
                    if trials >= 3 and rate >= 0.70:
                        self.db.execute(
                            "UPDATE lessons SET status = 'active', updated_at = ? WHERE id = ?",
                            (now, lid),
                        )
                    elif trials >= 2 and rate <= 0.35:
                        self.db.execute(
                            "UPDATE lessons SET status = 'retired', updated_at = ? WHERE id = ?",
                            (now, lid),
                        )
                        retired.append(lid)
                elif row["status"] == "active":
                    if trials >= 5 and rate < 0.35:
                        self.db.execute(
                            "UPDATE lessons SET status = 'retired', updated_at = ? WHERE id = ?",
                            (now, lid),
                        )
                        retired.append(lid)
            self.db.commit()
        return retired

    # ------------------------------------------------------- CodeIssueQueue

    def record_code_issue(self, *, scope: str, error_summary: str) -> int | None:
        clean_scope = _clean_text(scope or "global", 40).lower() or "global"
        clean_err = _clean_text(error_summary, 240)
        if not clean_err:
            return None
        fp = _fingerprint(clean_scope, clean_err)
        now = time.time()
        with self.lock:
            existing = self.db.execute(
                "SELECT id FROM code_issues WHERE fingerprint = ?", (fp,)
            ).fetchone()
            if existing is not None:
                self.db.execute(
                    "UPDATE code_issues SET occurrences = occurrences + 1, last_seen = ?, status = 'open' WHERE id = ?",
                    (now, existing["id"]),
                )
                self.db.commit()
                return int(existing["id"])
            cur = self.db.execute(
                """
                INSERT INTO code_issues(fingerprint, scope, error_summary, occurrences, status, first_seen, last_seen)
                VALUES (?, ?, ?, 1, 'open', ?, ?)
                """,
                (fp, clean_scope, clean_err, now, now),
            )
            self.db.commit()
            return int(cur.lastrowid)

    def list_code_issues(self, *, status: str = "open", limit: int = 10) -> list[dict[str, Any]]:
        sql = "SELECT * FROM code_issues"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY occurrences DESC, last_seen DESC LIMIT ?"
        args.append(max(1, min(50, int(limit))))
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------- StrategyBandit

    def update_bandit(self, context_key: str, arm: str, reward: float) -> None:
        ctx = _clean_text(context_key, 60).lower()
        arm_k = _clean_text(arm, 60).lower()
        if not ctx or not arm_k:
            return
        r = max(-1.0, min(1.0, float(reward)))
        now = time.time()
        with self.lock:
            self.db.execute(
                """
                INSERT INTO bandit_stats(context_key, arm, pulls, reward_sum, last_pulled_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(context_key, arm) DO UPDATE SET
                    pulls = pulls + 1,
                    reward_sum = reward_sum + excluded.reward_sum,
                    last_pulled_at = excluded.last_pulled_at
                """,
                (ctx, arm_k, r, now),
            )
            self.db.commit()

    def rank_arms(self, context_key: str, arms: Iterable[str], *, exploration_c: float = 0.8) -> list[str]:
        """Rank candidate strategy arms via UCB1 (unpulled arms explored first)."""
        ctx = _clean_text(context_key, 60).lower()
        candidates = [str(a).strip().lower() for a in arms if str(a).strip()]
        if not candidates:
            return []
        with self.lock:
            rows = self.db.execute(
                "SELECT arm, pulls, reward_sum FROM bandit_stats WHERE context_key = ?", (ctx,)
            ).fetchall()
        stats = {r["arm"]: (int(r["pulls"]), float(r["reward_sum"])) for r in rows}
        total_pulls = sum(p for p, _ in stats.values())
        scored = []
        for idx, arm in enumerate(candidates):
            pulls, rsum = stats.get(arm, (0, 0.0))
            if pulls <= 0:
                ucb = float("inf")
            else:
                mean = rsum / pulls
                ucb = mean + exploration_c * math.sqrt(math.log(max(1, total_pulls) + 1.0) / pulls)
            scored.append((ucb, -idx, arm))
        scored.sort(reverse=True)
        return [arm for _, _, arm in scored]

    # -------------------------------------------------------- MacroRegistry

    def record_macro(self, name: str, goal_pattern: str, steps: list[dict[str, Any]], reward: float) -> bool:
        clean_name = _clean_text(name, 60).lower()
        clean_goal = _clean_text(goal_pattern, 120)
        if not clean_name or not clean_goal or not isinstance(steps, list) or not steps:
            return False
        steps_json = redact_text(json.dumps(steps, ensure_ascii=False, separators=(",", ":")))
        if contains_coordinate(steps_json):
            return False
        now = time.time()
        with self.lock:
            row = self.db.execute("SELECT uses, avg_reward FROM macros WHERE name = ?", (clean_name,)).fetchone()
            if row is not None:
                uses = int(row["uses"]) + 1
                avg = (float(row["avg_reward"]) * int(row["uses"]) + float(reward)) / uses
                self.db.execute(
                    "UPDATE macros SET goal_pattern = ?, steps_json = ?, uses = ?, avg_reward = ?, updated_at = ? WHERE name = ?",
                    (clean_goal, steps_json, uses, round(avg, 4), now, clean_name),
                )
            else:
                self.db.execute(
                    "INSERT INTO macros(name, goal_pattern, steps_json, uses, avg_reward, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                    (clean_name, clean_goal, steps_json, round(float(reward), 4), now),
                )
            self.db.commit()
        return True

    def list_macros(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM macros ORDER BY avg_reward DESC, uses DESC LIMIT ?",
                (max(1, min(50, int(limit))),),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["steps"] = json.loads(d.get("steps_json") or "[]")
            except Exception:
                d["steps"] = []
            out.append(d)
        return out
