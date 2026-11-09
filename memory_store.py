"""Local structured memory for the MC bridge.

This is the L2 authority for coordinates and the local L1 task-terminal index.
It deliberately uses only the Python standard library and SQLite: location
queries must keep working when LivingMemory, embeddings or the network are
unavailable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .privacy import LEGACY_KINDS, contains_coordinate, redact_text

SCHEMA_VERSION = 1

KIND_LABELS = {
    "home": "家",
    "home_bed": "家床",
    "base": "基地",
    "portal": "传送门",
    "farm": "农田",
    "mine": "矿点",
    "storage": "仓库",
    "chest": "箱子",
    "workbench": "工作台",
    "furnace": "熔炉",
    "villager": "村庄",
    "danger": "危险区",
    "waypoint": "路径点",
    "resource": "资源点",
    "structure": "建筑",
}

TTL_DAYS = {
    "home": 90,
    "home_bed": 90,
    "base": 90,
    "portal": 30,
    "farm": 14,
    "mine": 14,
    "storage": 7,
    "chest": 7,
    "workbench": 30,
    "furnace": 30,
    "villager": 30,
    "danger": 7,
    "waypoint": 30,
    "resource": 7,
    "structure": 30,
}

ACTIVE_STATUSES = {"active", "candidate", "stale", "conflict"}


def _json(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bounded(value, limit=500) -> str:
    return str(value or "")[:limit]


def safe_label(kind: str, key: str = "", name: str = "") -> str:
    """A model/group-safe label; never a coordinate or world identifier."""
    raw = redact_text(name).strip()
    if raw and not contains_coordinate(raw):
        return raw[:40]
    base = KIND_LABELS.get(str(kind or ""), str(kind or "地点")[:20])
    key = redact_text(key).strip()
    if key and key not in {"main", "default"}:
        return f"{base}({key[:20]})"
    return base


class MemoryStore:
    """Synchronous SQLite store.  Operations are short and bounded."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._ensure_schema()
        self._secure_files()
        self._world_cache: dict[str, str] = {}

    def _secure_files(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(str(self.path) + suffix, 0o600)
            except OSError:
                pass

    # ------------------------------------------------------------------ schema
    def _ensure_schema(self):
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memory_migrations (
                    key TEXT PRIMARY KEY,
                    applied_at REAL NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS places (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_ref TEXT NOT NULL UNIQUE,
                    source_key TEXT UNIQUE,
                    world_key TEXT NOT NULL DEFAULT '',
                    world_instance TEXT NOT NULL DEFAULT '',
                    dimension TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    key TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    safe_label TEXT NOT NULL DEFAULT '',
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    target_x INTEGER,
                    target_y INTEGER,
                    target_z INTEGER,
                    stand_x REAL,
                    stand_y REAL,
                    stand_z REAL,
                    stand_feet_y REAL,
                    radius REAL NOT NULL DEFAULT 0,
                    precision TEXT NOT NULL DEFAULT 'exact',
                    yaw REAL,
                    pitch REAL,
                    target_status TEXT NOT NULL DEFAULT 'unknown',
                    stand_status TEXT NOT NULL DEFAULT 'unknown',
                    observation_status TEXT NOT NULL DEFAULT 'unloaded',
                    status TEXT NOT NULL DEFAULT 'candidate',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    source TEXT NOT NULL DEFAULT 'observed',
                    privacy TEXT NOT NULL DEFAULT 'owner_only',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_verified_at REAL,
                    verified_count INTEGER NOT NULL DEFAULT 0,
                    supersedes_id INTEGER,
                    pin INTEGER NOT NULL DEFAULT 0,
                    tombstone INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS idx_places_scope
                    ON places(world_key, world_instance, dimension, kind, status, tombstone);
                CREATE INDEX IF NOT EXISTS idx_places_kind_key
                    ON places(kind, key, tombstone);
                CREATE INDEX IF NOT EXISTS idx_places_updated
                    ON places(updated_at);
                CREATE TABLE IF NOT EXISTS place_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    place_id INTEGER NOT NULL,
                    changed_at REAL NOT NULL,
                    change_kind TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    snapshot_json TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_place_versions_place
                    ON place_versions(place_id, changed_at);
                CREATE TABLE IF NOT EXISTS place_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    place_id INTEGER NOT NULL,
                    episode_id TEXT NOT NULL DEFAULT '',
                    evidence_kind TEXT NOT NULL DEFAULT '',
                    digest TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_place_evidence_place
                    ON place_evidence(place_id, created_at);
                CREATE TABLE IF NOT EXISTS place_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_place_id INTEGER NOT NULL,
                    to_place_id INTEGER NOT NULL,
                    link_kind TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    UNIQUE(from_place_id, to_place_id, link_kind)
                );
                CREATE TABLE IF NOT EXISTS place_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    place_id INTEGER NOT NULL,
                    observed_at REAL NOT NULL,
                    target_status TEXT NOT NULL DEFAULT 'unknown',
                    stand_status TEXT NOT NULL DEFAULT 'unknown',
                    observation_status TEXT NOT NULL DEFAULT 'unknown',
                    digest TEXT NOT NULL DEFAULT '',
                    source_event_id TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_place_observations_place
                    ON place_observations(place_id, observed_at);
                CREATE TABLE IF NOT EXISTS world_bindings (
                    world_key TEXT NOT NULL,
                    world_instance TEXT NOT NULL,
                    alias TEXT NOT NULL DEFAULT '',
                    dimension_set TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    last_confirmed_at REAL NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(world_key, world_instance)
                );
                CREATE TABLE IF NOT EXISTS episodes_index (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    opcode TEXT NOT NULL DEFAULT '',
                    goal_digest TEXT NOT NULL DEFAULT '',
                    world_key TEXT NOT NULL DEFAULT '',
                    world_instance TEXT NOT NULL DEFAULT '',
                    dimension TEXT NOT NULL DEFAULT '',
                    position_json TEXT NOT NULL DEFAULT '{}',
                    target_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT '',
                    goal_outcome TEXT NOT NULL DEFAULT '',
                    confirmed_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_digest TEXT NOT NULL DEFAULT '',
                    result_summary TEXT NOT NULL DEFAULT '',
                    observation_scope TEXT NOT NULL DEFAULT '',
                    inventory_delta_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'task_worker',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    visibility TEXT NOT NULL DEFAULT 'owner_private',
                    created_at REAL NOT NULL,
                    livingmemory_doc_id INTEGER,
                    UNIQUE(task_id, run_id, event_type)
                );
                CREATE INDEX IF NOT EXISTS idx_episodes_created
                    ON episodes_index(created_at);
                CREATE TABLE IF NOT EXISTS episode_outbox (
                    event_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    shared_key TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_episode_outbox_state
                    ON episode_outbox(state, created_at);
                CREATE TABLE IF NOT EXISTS summary_index (
                    period_start REAL NOT NULL,
                    period_end REAL NOT NULL,
                    scope TEXT NOT NULL,
                    stats_json TEXT NOT NULL DEFAULT '{}',
                    narrative_doc_id INTEGER,
                    validated INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(period_start, period_end, scope)
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'owner',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS access_policies (
                    policy_key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS negative_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    world_key TEXT NOT NULL DEFAULT '',
                    world_instance TEXT NOT NULL DEFAULT '',
                    dimension TEXT NOT NULL DEFAULT '',
                    position_json TEXT NOT NULL DEFAULT '{}',
                    region_json TEXT NOT NULL DEFAULT '{}',
                    reason TEXT NOT NULL DEFAULT '',
                    observed_at REAL NOT NULL,
                    expires_at REAL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    source_event_id TEXT NOT NULL DEFAULT '',
                    privacy TEXT NOT NULL DEFAULT 'owner_only'
                );
                CREATE INDEX IF NOT EXISTS idx_negative_facts_scope
                    ON negative_facts(world_key, world_instance, dimension, kind, expires_at);
                CREATE TABLE IF NOT EXISTS retrieval_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL,
                    audience TEXT NOT NULL DEFAULT '',
                    query_digest TEXT NOT NULL DEFAULT '',
                    result_count INTEGER NOT NULL DEFAULT 0,
                    place_refs_json TEXT NOT NULL DEFAULT '[]',
                    warning_count INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self.db.execute(
                "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES(?,?)",
                (SCHEMA_VERSION, time.time()),
            )
            self.db.commit()

    def close(self):
        with self.lock:
            try:
                self.db.close()
            except Exception:
                pass

    # ------------------------------------------------------------- primitives
    def _now(self) -> float:
        return time.time()

    def _new_ref(self) -> str:
        return "p_" + secrets.token_hex(6)

    def _version(self, place_id: int, change_kind: str, actor: str = "", note: str = ""):
        row = self._get_raw(place_id)
        if row is None:
            return
        self.db.execute(
            "INSERT INTO place_versions(place_id, changed_at, change_kind, actor, snapshot_json, note) "
            "VALUES(?,?,?,?,?,?)",
            (place_id, self._now(), change_kind, actor, _json(dict(row)), note),
        )

    def _get_raw(self, place_id: int):
        cursor = self.db.execute("SELECT * FROM places WHERE id=?", (int(place_id),))
        return cursor.fetchone()

    def _row(self, row):
        return dict(row) if row is not None else None

    def has_migration(self, key: str) -> bool:
        with self.lock:
            cursor = self.db.execute("SELECT 1 FROM memory_migrations WHERE key=?", (str(key),))
            return cursor.fetchone() is not None

    def _mark_migration(self, key: str, detail: str = ""):
        self.db.execute(
            "INSERT OR REPLACE INTO memory_migrations(key, applied_at, detail) VALUES(?,?,?)",
            (str(key), self._now(), str(detail)[:1000]),
        )
        self.db.commit()

    # ---------------------------------------------------------------- worlds
    def ensure_world(self, world_key: str, alias: str = "") -> str:
        world_key = _bounded(world_key, 120)
        if not world_key:
            return ""
        with self.lock:
            cached = self._world_cache.get(world_key)
            if cached:
                self.db.execute(
                    "UPDATE world_bindings SET last_confirmed_at=? WHERE world_key=? AND world_instance=?",
                    (self._now(), world_key, cached),
                )
                self.db.commit()
                return cached
            cursor = self.db.execute(
                "SELECT world_instance FROM world_bindings WHERE world_key=? "
                "ORDER BY last_confirmed_at DESC, created_at DESC LIMIT 1",
                (world_key,),
            )
            row = cursor.fetchone()
            now = self._now()
            if row:
                instance = str(row["world_instance"])
                self.db.execute(
                    "UPDATE world_bindings SET last_confirmed_at=? WHERE world_key=? AND world_instance=?",
                    (now, world_key, instance),
                )
                self.db.commit()
                self._world_cache[world_key] = instance
                return instance
            instance = "w_" + uuid.uuid4().hex[:10]
            self.db.execute(
                "INSERT INTO world_bindings(world_key, world_instance, alias, created_at, last_confirmed_at, evidence) "
                "VALUES(?,?,?,?,?,?)",
                (world_key, instance, _bounded(alias, 80), now, now, "first_seen"),
            )
            self.db.commit()
            self._world_cache[world_key] = instance
            return instance

    def rebind_world(self, world_key: str, alias: str = "") -> str:
        world_key = _bounded(world_key, 120)
        if not world_key:
            raise ValueError("world_key is empty")
        instance = "w_" + uuid.uuid4().hex[:10]
        now = self._now()
        with self.lock:
            self.db.execute(
                "INSERT INTO world_bindings(world_key, world_instance, alias, created_at, last_confirmed_at, evidence) "
                "VALUES(?,?,?,?,?,?)",
                (world_key, instance, _bounded(alias, 80), now, now, "explicit_rebind"),
            )
            self.db.commit()
            self._world_cache[world_key] = instance
        return instance

    def world_bindings(self) -> list[dict]:
        with self.lock:
            cursor = self.db.execute(
                "SELECT * FROM world_bindings ORDER BY world_key, last_confirmed_at DESC"
            )
            return [dict(row) for row in cursor.fetchall()]

    # ---------------------------------------------------------------- places
    def _scope_rows(self, world_key: str, world_instance: str, dimension: str, kind: str = "", key: str = ""):
        sql = "SELECT * FROM places WHERE tombstone=0"
        args: list = []
        if world_key:
            sql += " AND world_key=?"
            args.append(str(world_key))
        if dimension:
            sql += " AND dimension=?"
            args.append(str(dimension))
        if world_instance:
            sql += " AND (world_instance=? OR world_instance='')"
            args.append(str(world_instance))
        if kind:
            sql += " AND kind=?"
            args.append(str(kind))
        if key:
            sql += " AND key=?"
            args.append(str(key))
        return sql, args

    def _place_candidates(self, *, world_key: str, world_instance: str, dimension: str, kind: str, key: str):
        sql, args = self._scope_rows(world_key, world_instance, dimension, kind, key)
        sql += " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END, updated_at DESC, id DESC"
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def latest_place(self, *, world_key: str, world_instance: str, dimension: str, kind: str, key: str = "main"):
        rows = self._place_candidates(
            world_key=world_key, world_instance=world_instance,
            dimension=dimension, kind=kind, key=key,
        )
        return rows[0] if rows else None

    def _insert_place(self, **values) -> int:
        now = self._now()
        ref = values.pop("public_ref", "") or self._new_ref()
        for _ in range(5):
            try:
                cursor = self.db.execute(
                    """
                    INSERT INTO places(
                        public_ref, source_key, world_key, world_instance, dimension, kind, key,
                        name, safe_label, aliases_json, target_x, target_y, target_z,
                        stand_x, stand_y, stand_z, stand_feet_y, radius, precision, yaw, pitch,
                        target_status, stand_status, observation_status, status, confidence,
                        source, privacy, created_at, updated_at, last_verified_at,
                        verified_count, supersedes_id, pin, tombstone, notes, tags_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        ref,
                        values.get("source_key"),
                        _bounded(values.get("world_key"), 120),
                        _bounded(values.get("world_instance"), 80),
                        _bounded(values.get("dimension"), 80),
                        _bounded(values.get("kind"), 40),
                        _bounded(values.get("key", "main"), 80),
                        _bounded(values.get("name"), 80),
                        safe_label(values.get("kind", ""), values.get("key", ""), values.get("name", "")),
                        _json(values.get("aliases") or []),
                        _to_int(values.get("target_x")),
                        _to_int(values.get("target_y")),
                        _to_int(values.get("target_z")),
                        _to_float(values.get("stand_x")),
                        _to_float(values.get("stand_y")),
                        _to_float(values.get("stand_z")),
                        _to_float(values.get("stand_feet_y")),
                        _to_float(values.get("radius"), 0.0) or 0.0,
                        _bounded(values.get("precision", "exact"), 20),
                        _to_float(values.get("yaw")),
                        _to_float(values.get("pitch")),
                        _bounded(values.get("target_status", "unknown"), 20),
                        _bounded(values.get("stand_status", "unknown"), 20),
                        _bounded(values.get("observation_status", "unloaded"), 20),
                        _bounded(values.get("status", "candidate"), 20),
                        _to_float(values.get("confidence"), 0.5) if values.get("confidence") is not None else 0.5,
                        _bounded(values.get("source", "observed"), 30),
                        _bounded(values.get("privacy", "owner_only"), 60),
                        _to_float(values.get("created_at"), now) or now,
                        _to_float(values.get("updated_at"), now) or now,
                        _to_float(values.get("last_verified_at")),
                        _to_int(values.get("verified_count"), 0) or 0,
                        _to_int(values.get("supersedes_id")),
                        1 if values.get("pin") else 0,
                        1 if values.get("tombstone") else 0,
                        _bounded(values.get("notes"), 500),
                        _json(values.get("tags") or []),
                    ),
                )
                return int(cursor.lastrowid)
            except sqlite3.IntegrityError:
                if values.get("source_key"):
                    # Another process/idempotent migration already inserted it.
                    row = self.db.execute(
                        "SELECT id FROM places WHERE source_key=?", (values["source_key"],)
                    ).fetchone()
                    if row:
                        return int(row["id"])
                ref = self._new_ref()
        raise sqlite3.IntegrityError("cannot allocate place reference")

    def remember_place(
        self,
        *,
        world_key: str,
        world_instance: str,
        dimension: str,
        kind: str,
        key: str = "main",
        name: str = "",
        target=None,
        stand=None,
        target_status: str = "unknown",
        stand_status: str = "unknown",
        observation_status: str = "loaded",
        status: str = "",
        confidence: float = 0.6,
        source: str = "observed",
        privacy: str = "owner_only",
        aliases=None,
        pin: bool = False,
        notes: str = "",
        tags=None,
        source_key: str | None = None,
        actor: str = "",
        created_at: float | None = None,
        updated_at: float | None = None,
        last_verified_at: float | None = None,
    ) -> dict:
        """Insert or update a place without ever silently overwriting a conflict."""
        kind = _bounded(kind, 40)
        key = _bounded(key or "main", 80)
        if not kind:
            raise ValueError("memory kind is required")
        if not key:
            raise ValueError("memory key is required")
        world_key = _bounded(world_key, 120)
        world_instance = _bounded(world_instance, 80)
        dimension = _bounded(dimension, 80)
        target_x, target_y, target_z = self._point(target)
        stand_x, stand_y, stand_z, stand_feet_y = self._stand(stand)
        now = self._now()
        if not status:
            verified = target_status == "valid" and stand_status == "valid" and observation_status == "loaded"
            status = "active" if verified else "candidate"
        if status not in ACTIVE_STATUSES and status not in {"deleted"}:
            status = "candidate"
        row_stamp = float(updated_at or now)
        verified_stamp = (
            float(last_verified_at) if last_verified_at is not None
            else (row_stamp if status == "active" else None)
        )
        with self.lock:
            old = None
            if source_key:
                row = self.db.execute("SELECT * FROM places WHERE source_key=?", (source_key,)).fetchone()
                old = self._row(row)
            if old is None:
                candidates = self._place_candidates(
                    world_key=world_key, world_instance=world_instance,
                    dimension=dimension, kind=kind, key=key,
                )
                old = next((item for item in candidates if item.get("status") == "active"), None)
                if old is None and candidates:
                    old = candidates[0]
            if old is not None:
                same_target = (
                    old.get("target_x") == target_x
                    and old.get("target_y") == target_y
                    and old.get("target_z") == target_z
                )
                old_verified = (
                    old.get("target_status") == "valid"
                    and old.get("stand_status") == "valid"
                    and old.get("observation_status") == "loaded"
                )
                new_verified = (
                    target_status == "valid"
                    and stand_status == "valid"
                    and observation_status == "loaded"
                )
                if same_target:
                    verified_now = new_verified and (target is not None)
                    self.db.execute(
                        """
                        UPDATE places SET name=?, safe_label=?, aliases_json=?, target_status=?,
                            stand_status=?, observation_status=?, status=?, confidence=?,
                            source=?, privacy=?, updated_at=?, last_verified_at=?,
                            verified_count=?, pin=?, notes=?, tags_json=?, world_instance=?
                        WHERE id=?
                        """,
                        (
                            _bounded(name or old.get("name"), 80),
                            safe_label(kind, key, name or old.get("name", "")),
                            _json(aliases if aliases is not None else json.loads(old.get("aliases_json") or "[]")),
                            target_status, stand_status, observation_status,
                            status if status != "candidate" or new_verified else old.get("status", "candidate"),
                            min(1.0, max(float(old.get("confidence") or 0.5), float(confidence))),
                            source, privacy, now,
                            now if verified_now else old.get("last_verified_at"),
                            int(old.get("verified_count") or 0) + (1 if verified_now else 0),
                            1 if pin or old.get("pin") else 0,
                            _bounded(notes or old.get("notes"), 500),
                            _json(tags if tags is not None else json.loads(old.get("tags_json") or "[]")),
                            world_instance or old.get("world_instance", ""),
                            int(old["id"]),
                        ),
                    )
                    self._version(int(old["id"]), "verify" if verified_now else "update", actor)
                    self.db.commit()
                    return self._row(self._get_raw(int(old["id"])))
                if new_verified and (old_verified or old.get("status") == "candidate"):
                    # A new verified target supersedes the old/candidate row,
                    # but the old row and its evidence stay available.
                    self.db.execute(
                        "UPDATE places SET status='stale', updated_at=? WHERE id=?",
                        (now, int(old["id"])),
                    )
                    self._version(int(old["id"]), "superseded", actor, "new verified target")
                    supersedes = int(old["id"])
                else:
                    supersedes = None
            else:
                supersedes = None
            place_id = self._insert_place(
                source_key=source_key,
                world_key=world_key, world_instance=world_instance, dimension=dimension,
                kind=kind, key=key, name=name, aliases=aliases or [],
                target_x=target_x, target_y=target_y, target_z=target_z,
                stand_x=stand_x, stand_y=stand_y, stand_z=stand_z, stand_feet_y=stand_feet_y,
                target_status=target_status, stand_status=stand_status,
                observation_status=observation_status, status=status,
                confidence=confidence, source=source, privacy=privacy,
                created_at=float(created_at or row_stamp), updated_at=row_stamp,
                last_verified_at=verified_stamp,
                verified_count=1 if status == "active" else 0,
                supersedes_id=supersedes, pin=pin, notes=notes, tags=tags or [],
            )
            self._version(place_id, "create", actor)
            self.db.commit()
            return self._row(self._get_raw(place_id))

    @staticmethod
    def _point(value):
        if isinstance(value, dict):
            return _to_int(value.get("x")), _to_int(value.get("y")), _to_int(value.get("z"))
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return _to_int(value[0]), _to_int(value[1]), _to_int(value[2])
        return None, None, None

    @staticmethod
    def _stand(value):
        if isinstance(value, dict):
            y = _to_float(value.get("y"))
            feet = _to_float(value.get("feet_y"), y)
            return (
                _to_float(value.get("x")), y, _to_float(value.get("z")),
                feet,
            )
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            y = _to_float(value[1])
            return _to_float(value[0]), y, _to_float(value[2]) if len(value) > 2 else None, y
        return None, None, None, None

    def get_place(self, place_ref: str):
        ref = str(place_ref or "").strip()
        if not ref:
            return None
        with self.lock:
            cursor = self.db.execute(
                "SELECT * FROM places WHERE public_ref=? AND tombstone=0", (ref,)
            )
            return self._row(cursor.fetchone())

    def verify_place(
        self,
        place_ref: str,
        *,
        target_status: str = "valid",
        stand_status: str = "valid",
        observation_status: str = "loaded",
        note: str = "",
        actor: str = "",
        world_instance: str = "",
    ) -> dict | None:
        row = self.get_place(place_ref)
        if row is None:
            return None
        now = self._now()
        if target_status == "valid" and stand_status == "valid" and observation_status == "loaded":
            status = "active"
        elif target_status == "invalid":
            status = "stale"
        else:
            status = "candidate"
        with self.lock:
            self.db.execute(
                "UPDATE places SET target_status=?, stand_status=?, observation_status=?, "
                "status=?, last_verified_at=?, verified_count=verified_count+1, "
                "confidence=MIN(1.0, confidence+0.05), updated_at=?, "
                "world_instance=CASE WHEN world_instance='' AND ?<>'' THEN ? ELSE world_instance END "
                "WHERE id=?",
                (target_status, stand_status, observation_status, status, now, now,
                 str(world_instance or ""), str(world_instance or ""), int(row["id"])),
            )
            self._version(int(row["id"]), "verify", actor, note)
            self.db.commit()
            return self._row(self._get_raw(int(row["id"])))

    def forget_place(self, place_ref: str, *, actor: str = "", note: str = "") -> bool:
        row = self.get_place(place_ref)
        if row is None:
            return False
        now = self._now()
        with self.lock:
            self.db.execute(
                "UPDATE places SET tombstone=1, status='deleted', updated_at=? WHERE id=?",
                (now, int(row["id"])),
            )
            self._version(int(row["id"]), "forget", actor, note)
            self.db.commit()
        return True

    def pin_place(self, place_ref: str, pinned: bool = True) -> bool:
        row = self.get_place(place_ref)
        if row is None:
            return False
        with self.lock:
            self.db.execute(
                "UPDATE places SET pin=?, updated_at=? WHERE id=?",
                (1 if pinned else 0, self._now(), int(row["id"])),
            )
            self._version(int(row["id"]), "pin" if pinned else "unpin")
            self.db.commit()
        return True

    def set_privacy(self, place_ref: str, privacy: str, *, actor: str = "") -> dict | None:
        row = self.get_place(place_ref)
        if row is None:
            return None
        privacy = _bounded(privacy, 60)
        if not (privacy in {"owner_only", "group_shared"} or privacy.startswith("group_only:")):
            raise ValueError("unsupported place privacy")
        with self.lock:
            self.db.execute(
                "UPDATE places SET privacy=?, updated_at=? WHERE id=?",
                (privacy, self._now(), int(row["id"])),
            )
            self._version(int(row["id"]), "share", actor, privacy)
            self.db.commit()
            return self._row(self._get_raw(int(row["id"])))

    def place_history(self, place_ref: str, limit: int = 20) -> list[dict]:
        ref = str(place_ref or "").strip()
        if not ref:
            return []
        with self.lock:
            row = self.db.execute("SELECT * FROM places WHERE public_ref=?", (ref,)).fetchone()
            if row is None:
                return []
            cursor = self.db.execute(
                "SELECT * FROM place_versions WHERE place_id=? ORDER BY changed_at DESC LIMIT ?",
                (int(row["id"]), max(1, min(50, int(limit)))),
            )
            return [dict(item) for item in cursor.fetchall()]

    def record_observation(self, place_ref: str, *, target_status="unknown", stand_status="unknown",
                           observation_status="unknown", digest="", source_event_id=""):
        row = self.get_place(place_ref)
        if row is None:
            return False
        with self.lock:
            self.db.execute(
                "INSERT INTO place_observations(place_id, observed_at, target_status, stand_status, "
                "observation_status, digest, source_event_id) VALUES(?,?,?,?,?,?,?)",
                (int(row["id"]), self._now(), target_status, stand_status, observation_status,
                 _bounded(digest, 500), source_event_id),
            )
            self.db.commit()
        return True

    # -------------------------------------------------------------- querying
    def find_places(
        self,
        *,
        world_key: str = "",
        world_instance: str = "",
        dimension: str = "",
        kind: str = "",
        key: str = "",
        query: str = "",
        audience: str = "owner_private",
        limit: int = 20,
    ) -> list[dict]:
        sql, args = self._scope_rows(world_key, world_instance, dimension, kind, key)
        if query:
            sql += " AND (kind LIKE ? OR key LIKE ? OR name LIKE ? OR safe_label LIKE ?)"
            like = f"%{str(query)[:80]}%"
            args.extend([like, like, like, like])
        sql += " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 "
        sql += "WHEN 'conflict' THEN 2 WHEN 'stale' THEN 3 ELSE 4 END, "
        sql += "confidence DESC, updated_at DESC LIMIT ?"
        args.append(max(1, min(100, int(limit))))
        with self.lock:
            rows = [dict(row) for row in self.db.execute(sql, args).fetchall()]
        return [row for row in rows if self._audience_allows(row, audience)]

    def _audience_allows(self, row: dict, audience: str) -> bool:
        audience = str(audience or "unknown")
        privacy = str(row.get("privacy") or "owner_only")
        if audience == "owner_private":
            return True
        if audience.startswith("group:"):
            gid = audience.split(":", 1)[1]
            return privacy == "group_shared" or privacy == f"group_only:{gid}"
        if audience == "public_mc":
            return privacy == "group_shared"
        return False

    def can_view(self, row: dict, audience: str) -> bool:
        return bool(row) and self._audience_allows(row, audience)

    # --------------------------------------------------------------- episodes
    def record_task_event(
        self,
        *,
        task_id: str,
        run_id: str,
        event_type: str,
        opcode: str = "",
        goal: str = "",
        status: str = "",
        goal_outcome: str = "",
        world_key: str = "",
        world_instance: str = "",
        dimension: str = "",
        position=None,
        target=None,
        confirmed_count: int = 0,
        error_code: str = "",
        error: str = "",
        result_summary: str = "",
        observation_scope: str = "",
        inventory_delta=None,
        source: str = "task_worker",
        visibility: str = "owner_private",
        created_at: float | None = None,
    ) -> tuple[str, bool]:
        task_id = _bounded(task_id, 120)
        run_id = _bounded(run_id or "0", 80)
        event_type = _bounded(event_type, 40)
        now = float(created_at or self._now())
        event_id = hashlib.sha256(
            f"{task_id}|{run_id}|{event_type}".encode("utf-8")
        ).hexdigest()[:32]
        goal_digest = redact_text(goal)[:300]
        error_digest = redact_text(error)[:300]
        summary = redact_text(result_summary)[:500]
        pos = position if isinstance(position, dict) else {}
        tgt = target if isinstance(target, dict) else {}
        with self.lock:
            cursor = self.db.execute(
                """
                INSERT OR IGNORE INTO episodes_index(
                    event_id, task_id, run_id, event_type, opcode, goal_digest,
                    world_key, world_instance, dimension, position_json, target_json,
                    status, goal_outcome, confirmed_count, error_code, error_digest,
                    result_summary, observation_scope, inventory_delta_json, source,
                    confidence, visibility, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id, task_id, run_id, event_type, _bounded(opcode, 40), goal_digest,
                    _bounded(world_key, 120), _bounded(world_instance, 80),
                    _bounded(dimension, 80), _json(pos), _json(tgt),
                    _bounded(status, 40), _bounded(goal_outcome, 20),
                    max(0, int(confirmed_count or 0)), _bounded(error_code, 80),
                    error_digest, summary, _bounded(observation_scope, 80),
                    _json(inventory_delta or {}), _bounded(source, 40),
                    1.0, _bounded(visibility, 60), now,
                ),
            )
            inserted = cursor.rowcount > 0
            if inserted:
                content = self._episode_narrative(
                    event_type=event_type, opcode=opcode, goal_outcome=goal_outcome,
                    summary=summary, error=error_digest, confirmed_count=confirmed_count,
                )
                metadata = {
                    "mc_shared": True,
                    "event_id": event_id,
                    "task_id": task_id,
                    "run_id": run_id,
                    "source_kind": "task_terminal",
                    "visibility": "owner_private",
                    "occurred_at": now,
                    "event_start": now,
                    "event_end": now,
                }
                self.db.execute(
                    "INSERT OR IGNORE INTO episode_outbox(event_id, content, metadata, created_at, state, updated_at) "
                    "VALUES(?,?,?,?, 'pending', ?)",
                    (event_id, content, _json(metadata), now, now),
                )
            self.db.commit()
        return event_id, inserted

    @staticmethod
    def _episode_narrative(*, event_type, opcode, goal_outcome, summary, error, confirmed_count) -> str:
        label = {
            "task_completed": "MC任务完成",
            "task_failed": "MC任务失败",
            "task_cancelled": "MC任务取消",
        }.get(str(event_type), "MC任务结束")
        parts = [f"{label}（{str(opcode or '任务')[:40]}）"]
        if goal_outcome:
            parts.append(f"目标结果：{str(goal_outcome)[:20]}")
        if confirmed_count:
            parts.append(f"确认数量：{int(confirmed_count)}")
        if summary:
            parts.append(f"摘要：{summary}")
        if error:
            parts.append(f"原因：{error}")
        return "；".join(parts)[:700]

    def pending_episodes(self, limit: int = 10) -> list[dict]:
        with self.lock:
            cursor = self.db.execute(
                "SELECT event_id, content, metadata, created_at FROM episode_outbox "
                "WHERE state='pending' ORDER BY created_at LIMIT ?",
                (max(1, min(50, int(limit))),),
            )
            return [dict(row) for row in cursor.fetchall()]

    def mark_episode_enqueued(self, event_id: str, shared_key: str = "") -> None:
        with self.lock:
            self.db.execute(
                "UPDATE episode_outbox SET state='enqueued', shared_key=?, updated_at=? WHERE event_id=?",
                (str(shared_key or "")[:120], self._now(), str(event_id)),
            )
            self.db.commit()

    def mark_episode_synced(self, event_id: str, memory_id: int) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE episode_outbox SET state='synced', updated_at=? WHERE event_id=?",
                (self._now(), str(event_id)),
            )
            self.db.execute(
                "UPDATE episodes_index SET livingmemory_doc_id=? WHERE event_id=?",
                (int(memory_id), str(event_id)),
            )
            self.db.commit()

    def episode_count(self) -> int:
        with self.lock:
            return int(self.db.execute("SELECT COUNT(*) FROM episodes_index").fetchone()[0])

    def recent_episodes(self, limit: int = 20, *, world_key: str = "") -> list[dict]:
        sql = "SELECT * FROM episodes_index"
        args: list = []
        if world_key:
            sql += " WHERE world_key=?"
            args.append(world_key)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(200, int(limit))))
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    # -------------------------------------------------------------- migration
    def migrate_legacy(self, rows, *, migration_key: str = "legacy_poi_v1") -> int:
        """Idempotently copy the old flat POI JSON into ``places``."""
        if self.has_migration(migration_key):
            return 0
        now = self._now()
        inserted = 0
        with self.lock:
            for row in rows or ():
                if not isinstance(row, dict):
                    continue
                kind = str(row.get("kind") or "").strip()
                key = str(row.get("key") or "").strip()
                if kind not in LEGACY_KINDS or not key:
                    continue
                source_key = f"legacy:{kind}:{key}"
                existing = self.db.execute(
                    "SELECT id FROM places WHERE source_key=?", (source_key,)
                ).fetchone()
                if existing:
                    continue
                updated = _to_float(row.get("updated_at"), now) or now
                age_days = max(0.0, (now - updated) / 86400.0)
                ttl = TTL_DAYS.get(kind, 30)
                status = "stale" if age_days > ttl else "candidate"
                try:
                    self._insert_place(
                        source_key=source_key,
                        world_key=_bounded(row.get("world_key"), 120),
                        world_instance="",
                        dimension=_bounded(row.get("dimension"), 80),
                        kind=kind, key=key,
                        name=_bounded(row.get("name"), 80),
                        target_x=_to_int(row.get("x")),
                        target_y=_to_int(row.get("y")),
                        target_z=_to_int(row.get("z")),
                        target_status="unknown",
                        stand_status="unknown",
                        observation_status="unloaded",
                        status=status,
                        confidence=0.4,
                        source="legacy",
                        privacy="owner_only",
                        created_at=updated,
                        updated_at=updated,
                        last_verified_at=None,
                        verified_count=0,
                        notes="legacy migration; verify before navigation",
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    continue
            self.db.execute(
                "INSERT OR REPLACE INTO memory_migrations(key, applied_at, detail) VALUES(?,?,?)",
                (migration_key, now, f"inserted={inserted}"),
            )
            self.db.commit()
        return inserted

    def migrate_legacy_file(self, path, *, migration_key: str = "legacy_poi_v1") -> int:
        source = Path(path)
        if self.has_migration(migration_key) or not source.exists():
            return 0
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except Exception:
            return 0
        rows = raw.get("items") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return 0
        return self.migrate_legacy(rows, migration_key=migration_key)

    # ------------------------------------------------------------------ views
    def _ttl_days(self, row: dict) -> int:
        return TTL_DAYS.get(str(row.get("kind") or ""), 30)

    def is_expired(self, row: dict, now: float | None = None) -> bool:
        if not row or row.get("pin"):
            return False
        now = float(now or self._now())
        stamp = _to_float(row.get("last_verified_at")) or _to_float(row.get("updated_at")) or 0.0
        if not stamp:
            return True
        return (now - stamp) / 86400.0 > self._ttl_days(row)

    def warnings(self, row: dict, now: float | None = None) -> list[str]:
        now = float(now or self._now())
        warnings: list[str] = []
        status = str(row.get("status") or "candidate")
        if status == "stale":
            warnings.append("已过期，使用前需要复核")
        elif status == "candidate":
            warnings.append("尚未验证，只能查看，不能自动导航")
        elif status == "conflict":
            warnings.append("存在冲突候选，需要主人确认")
        if self.is_expired(row, now) and status not in {"stale", "conflict"}:
            warnings.append("上次验证已超过有效期")
        if row.get("target_status") != "valid":
            warnings.append("目标方块/地点未验证")
        if row.get("stand_status") != "valid":
            warnings.append("没有已验证站位，导航前需要探测")
        if row.get("observation_status") != "loaded":
            warnings.append("区块未加载或尚未观察")
        if not row.get("world_instance"):
            warnings.append("旧记录未绑定世界实例，换档前不能自动执行")
        return warnings

    def safe_view(self, row: dict, *, audience: str = "owner_private") -> dict:
        if not row:
            return {}
        if audience != "owner_private" and not self._audience_allows(row, audience):
            return {}
        now = self._now()
        warnings = self.warnings(row, now)
        stamp = _to_float(row.get("last_verified_at"))
        view = {
            "place_ref": str(row.get("public_ref") or ""),
            "kind": str(row.get("kind") or ""),
            "key": str(row.get("key") or ""),
            "label": str(row.get("safe_label") or safe_label(row.get("kind", ""), row.get("key", ""), row.get("name", ""))),
            "name": str(row.get("safe_label") or safe_label(row.get("kind", ""), row.get("key", ""), row.get("name", ""))),
            "dimension": str(row.get("dimension") or ""),
            "status": str(row.get("status") or "candidate"),
            "confidence": round(float(row.get("confidence") or 0.0), 2),
            "source": str(row.get("source") or ""),
            "verified_count": int(row.get("verified_count") or 0),
            "last_verified_at": self._iso(stamp),
            "age_seconds": int(max(0.0, now - stamp)) if stamp else None,
            "target_status": str(row.get("target_status") or "unknown"),
            "stand_status": str(row.get("stand_status") or "unknown"),
            "observation_status": str(row.get("observation_status") or "unknown"),
            "has_target": row.get("target_x") is not None,
            "has_stand": row.get("stand_x") is not None,
            "pinned": bool(row.get("pin")),
            "warnings": warnings,
        }
        if audience != "owner_private":
            # Never expose private aliases/notes to a shared audience, even if a
            # future caller accidentally asks for the raw row.
            view.pop("key", None)
        return view

    def local_view(self, row: dict) -> dict:
        if not row:
            return {}
        view = self.safe_view(row, audience="owner_private")
        view.update({
            "world_key": str(row.get("world_key") or ""),
            "world_instance": str(row.get("world_instance") or ""),
            "target": {
                "x": row.get("target_x"), "y": row.get("target_y"), "z": row.get("target_z"),
            } if row.get("target_x") is not None else None,
            "stand": {
                "x": row.get("stand_x"), "y": row.get("stand_y"), "z": row.get("stand_z"),
                "feet_y": row.get("stand_feet_y"),
            } if row.get("stand_x") is not None else None,
            "aliases": json.loads(row.get("aliases_json") or "[]"),
            "notes": str(row.get("notes") or ""),
        })
        return view

    @staticmethod
    def _iso(stamp) -> str | None:
        stamp = _to_float(stamp)
        if not stamp:
            return None
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
        except Exception:
            return None

    def local_card(self, row: dict) -> str:
        if not row:
            return ""
        label = str(row.get("safe_label") or safe_label(row.get("kind", ""), row.get("key", ""), row.get("name", "")))
        bits = [f"[本地坐标卡片] {label}（{row.get('kind') or '地点'}）"]
        target = {k: row.get(f"target_{k}") for k in ("x", "y", "z")}
        stand = {k: row.get(f"stand_{k}") for k in ("x", "y", "z")}
        if target.get("x") is not None:
            bits.append(f"目标({target['x']}, {target['y']}, {target['z']})")
        if stand.get("x") is not None:
            bits.append(f"站位({stand['x']:g}, {stand['y']:g}, {stand['z']:g})")
        if row.get("dimension"):
            bits.append(f"维度 {row['dimension']}")
        bits.append(f"状态 {row.get('status') or 'candidate'}")
        if row.get("last_verified_at"):
            bits.append(f"上次验证 {self._iso(row['last_verified_at'])}")
        else:
            bits.append("尚未验证")
        return "；".join(bits)

    def resolve_for_navigation(
        self,
        place_ref: str,
        *,
        world_key: str,
        world_instance: str,
        dimension: str,
        audience: str = "owner_private",
    ) -> tuple[dict | None, str]:
        row = self.get_place(place_ref)
        if row is None:
            return None, "没有找到这个地点记录"
        if not self._audience_allows(row, audience):
            return None, "当前会话没有读取这个地点的权限"
        if world_key and str(row.get("world_key") or "") != str(world_key):
            return None, "这个地点属于另一个世界，未执行"
        if dimension and str(row.get("dimension") or "") and str(row.get("dimension")) != str(dimension):
            return None, "这个地点属于另一个维度，未执行"
        if not str(row.get("world_instance") or ""):
            return None, "旧记录未绑定世界实例，请先在当前世界复核后再自动执行"
        if world_instance and str(row.get("world_instance") or "") != str(world_instance):
            return None, "这个地点属于另一个世界实例，未执行"
        if str(row.get("status") or "") != "active":
            return None, "这个地点尚未验证或已过期，请先复核"
        if row.get("target_status") != "valid" or row.get("stand_status") != "valid":
            return None, "这个地点缺少已验证的目标或站位，不能自动导航"
        if row.get("observation_status") != "loaded":
            return None, "这个地点所在区块未加载，不能自动导航"
        if self.is_expired(row):
            return None, "这个地点已超过有效期，请先复核"
        return row, ""

    # ------------------------------------------------------------------ misc
    def record_retrieval(self, *, audience: str, query: str, rows: list[dict], warnings: int = 0):
        refs = [str(row.get("public_ref") or "") for row in rows[:20]]
        digest = hashlib.sha256(str(query or "").encode("utf-8")).hexdigest()[:16]
        with self.lock:
            self.db.execute(
                "INSERT INTO retrieval_log(at, audience, query_digest, result_count, place_refs_json, warning_count) "
                "VALUES(?,?,?,?,?,?)",
                (self._now(), _bounded(audience, 60), digest, len(rows), _json(refs), int(warnings)),
            )
            self.db.commit()

    def set_policy(self, key: str, value) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO access_policies(policy_key, value_json, updated_at) VALUES(?,?,?)",
                (str(key), _json(value), self._now()),
            )
            self.db.commit()

    def get_policy(self, key: str, default=None):
        with self.lock:
            row = self.db.execute(
                "SELECT value_json FROM access_policies WHERE policy_key=?", (str(key),)
            ).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value_json"])
            except Exception:
                return default

    def set_preference(self, key: str, value: str, source: str = "owner") -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR REPLACE INTO preferences(key, value, source, updated_at) VALUES(?,?,?,?)",
                (str(key), str(value)[:1000], str(source)[:40], self._now()),
            )
            self.db.commit()

    def count_places(self, *, world_key: str = "", include_deleted: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM places WHERE 1=1"
        args: list = []
        if world_key:
            sql += " AND world_key=?"
            args.append(world_key)
        if not include_deleted:
            sql += " AND tombstone=0"
        with self.lock:
            return int(self.db.execute(sql, args).fetchone()[0])
