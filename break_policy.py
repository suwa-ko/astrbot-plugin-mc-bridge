"""Bounded destruction policy and protected-region registry.

The policy is deliberately plugin-side so every explicit break request and every
navigation fallback passes through the same check.  Regions are persisted next to
the plugin data and are never sent to the language model as raw world data.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_PER_ACTION = 8
DEFAULT_PER_TASK = 64
MAX_REGIONS = 64

PROTECTED_BLOCKS = {
    "chest", "trapped_chest", "barrel", "ender_chest", "furnace", "blast_furnace",
    "smoker", "hopper", "dispenser", "dropper", "crafting_table", "cartography_table",
    "fletching_table", "smithing_table", "loom", "stonecutter", "grindstone", "anvil",
    "chipped_anvil", "damaged_anvil", "enchanting_table", "brewing_stand", "beacon",
    "respawn_anchor", "bed", "door", "trapdoor", "fence_gate", "button", "lever",
    "pressure_plate", "sign", "hanging_sign", "torch", "lantern", "campfire",
    "soul_campfire", "beehive", "bee_nest", "spawner", "trial_spawner", "vault",
}



def _id(value: Any) -> str:
    return str(value or "").strip().lower()[:120]


def block_name(block_id: str) -> str:
    return _id(block_id).split(":", 1)[-1]


def block_category(block_id: str) -> str | None:
    name = block_name(block_id)
    if not name:
        return None
    if name in {"stone", "cobblestone", "deepslate", "tuff", "calcite", "andesite", "diorite", "granite"}:
        return "stone"
    if name.endswith(("_log", "_wood", "_stem", "_hyphae")):
        return "logs"
    if name.endswith(("_leaves", "_sapling")):
        return "plants"
    if name.endswith("_ore"):
        return "ore"
    if name in {"dirt", "grass_block", "podzol", "mycelium", "coarse_dirt", "rooted_dirt", "mud", "clay"}:
        return "soil"
    if name in {"sand", "gravel"}:
        return "sand"
    if name in {"short_grass", "fern", "wheat", "carrots", "potatoes", "beetroots", "sugar_cane", "bamboo", "cactus"}:
        return "plants"
    if name in {"snow", "ice", "packed_ice", "blue_ice"}:
        return "snow"
    if name in {"netherrack", "soul_sand", "soul_soil", "basalt", "blackstone"}:
        return "nether"
    if name == "end_stone":
        return "end"
    return None


class BreakPolicy:
    def __init__(self, path, *, per_action: int = DEFAULT_PER_ACTION, per_task: int = DEFAULT_PER_TASK,
                 allow_unnatural: bool = False):
        self.path = Path(path)
        self.per_action = max(1, int(per_action))
        self.per_task = max(self.per_action, int(per_task))
        self.allow_unnatural = bool(allow_unnatural)
        self.regions: list[dict[str, Any]] = []
        self._task_id = "direct"
        self._task_count = 0
        self._action_count = 0
        self.load()

    def load(self):
        self.regions = []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows = raw.get("regions") if isinstance(raw, dict) else raw
            if not isinstance(rows, list):
                return
            for row in rows[:MAX_REGIONS]:
                cleaned = self._clean_region(row)
                if cleaned is not None:
                    self.regions.append(cleaned)
        except FileNotFoundError:
            pass
        except Exception:
            self.regions = []

    def save(self):
        payload = {"version": 1, "regions": self.regions[-MAX_REGIONS:]}
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

    @staticmethod
    def _clean_region(row: Any) -> dict[str, Any] | None:
        if not isinstance(row, dict):
            return None
        name = str(row.get("name") or "").strip()[:80]
        if not name:
            return None
        try:
            mins = [int(row.get(key)) for key in ("min_x", "min_y", "min_z")]
            maxs = [int(row.get(key)) for key in ("max_x", "max_y", "max_z")]
        except (TypeError, ValueError):
            return None
        if any(lo > hi for lo, hi in zip(mins, maxs)):
            return None
        return {
            "name": name,
            "kind": str(row.get("kind") or "building")[:20],
            "dimension": str(row.get("dimension") or "")[:80],
            "min_x": mins[0], "min_y": mins[1], "min_z": mins[2],
            "max_x": maxs[0], "max_y": maxs[1], "max_z": maxs[2],
        }

    def add_box(self, name: str, minimum: tuple[int, int, int], maximum: tuple[int, int, int],
                *, kind: str = "building", dimension: str = "") -> dict[str, Any]:
        min_x, max_x = sorted((int(minimum[0]), int(maximum[0])))
        min_y, max_y = sorted((int(minimum[1]), int(maximum[1])))
        min_z, max_z = sorted((int(minimum[2]), int(maximum[2])))
        region = {
            "name": str(name or "protected")[:80],
            "kind": str(kind or "building")[:20],
            "dimension": str(dimension or "")[:80],
            "min_x": min_x, "min_y": min_y, "min_z": min_z,
            "max_x": max_x, "max_y": max_y, "max_z": max_z,
        }
        self.regions = [row for row in self.regions if row["name"] != region["name"]]
        self.regions.append(region)
        self.regions = self.regions[-MAX_REGIONS:]
        self.save()
        return dict(region)

    def add_around(self, name: str, x: int, y: int, z: int, radius: int = 3,
                   *, kind: str = "container", dimension: str = "") -> dict[str, Any]:
        radius = max(1, min(16, int(radius)))
        return self.add_box(
            name,
            (int(x) - radius, int(y) - radius, int(z) - radius),
            (int(x) + radius, int(y) + radius, int(z) + radius),
            kind=kind,
            dimension=dimension,
        )

    def remove(self, name: str) -> bool:
        name = str(name or "").strip()
        before = len(self.regions)
        self.regions = [row for row in self.regions if row["name"] != name]
        if len(self.regions) != before:
            self.save()
            return True
        return False

    def list(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.regions]

    def is_protected(self, x: int, y: int, z: int, dimension: str = "") -> dict[str, Any] | None:
        dimension = str(dimension or "")
        for row in self.regions:
            if row.get("dimension") and dimension and row["dimension"] != dimension:
                continue
            if (row["min_x"] <= int(x) <= row["max_x"]
                    and row["min_y"] <= int(y) <= row["max_y"]
                    and row["min_z"] <= int(z) <= row["max_z"]):
                return dict(row)
        return None

    def intersects(self, minimum: tuple[int, int, int], maximum: tuple[int, int, int],
                   dimension: str = "") -> dict[str, Any] | None:
        min_x, max_x = sorted((int(minimum[0]), int(maximum[0])))
        min_y, max_y = sorted((int(minimum[1]), int(maximum[1])))
        min_z, max_z = sorted((int(minimum[2]), int(maximum[2])))
        dimension = str(dimension or "")
        for row in self.regions:
            if row.get("dimension") and dimension and row["dimension"] != dimension:
                continue
            if (max_x < row["min_x"] or min_x > row["max_x"]
                    or max_y < row["min_y"] or min_y > row["max_y"]
                    or max_z < row["min_z"] or min_z > row["max_z"]):
                continue
            return dict(row)
        return None

    def begin_task(self, task_id: str) -> None:
        self._task_id = str(task_id or "direct")
        self._task_count = 0
        self._action_count = 0

    def preflight(self, x: int, y: int, z: int, block_id: str = "", *,
                  dimension: str = "") -> tuple[bool, str]:
        protected = self.is_protected(x, y, z, dimension)
        if protected is not None:
            return False, f"目标在保护区域「{protected['name']}」内，已拒绝破坏"
        name = block_name(block_id)
        if name in {"air", "cave_air", "void_air"}:
            return True, ""
        if name and any(name == item or name.endswith("_" + item) for item in PROTECTED_BLOCKS):
            return False, f"{block_id} 是容器/功能方块，已拒绝破坏"
        category = block_category(block_id)
        if category is None and not self.allow_unnatural:
            return False, f"方块 {block_id or '未知'} 不在可破坏类别内"
        return True, category or "unnatural"

    def check(self, x: int, y: int, z: int, block_id: str = "", *,
              count: int = 1, dimension: str = "", task_id: str = "") -> tuple[bool, str]:
        count = max(1, int(count))
        if task_id and task_id != self._task_id:
            self.begin_task(task_id)
        allowed, reason = self.preflight(x, y, z, block_id, dimension=dimension)
        if not allowed:
            return False, reason
        self._action_count = 0
        if count > self.per_action:
            return False, f"单次破坏预算不足：本次需要 {count}，上限 {self.per_action}"
        if self._task_count + count > self.per_task:
            return False, f"任务破坏预算不足：已用 {self._task_count}，本次 {count}，上限 {self.per_task}"
        self._action_count = count
        self._task_count += count
        return True, reason

    def ensure_task(self, task_id: str) -> None:
        if str(task_id or "direct") != self._task_id:
            self.begin_task(task_id)

    def remaining_task_budget(self) -> int:
        return max(0, self.per_task - self._task_count)

    def record_navigation_breaks(self, count: int) -> None:
        try:
            count = max(0, int(count))
        except (TypeError, ValueError):
            return
        self._task_count = min(self.per_task, self._task_count + count)

    def snapshot(self) -> dict[str, Any]:
        return {
            "regions": self.list(),
            "per_action": self.per_action,
            "per_task": self.per_task,
            "task_id": self._task_id,
            "task_used": self._task_count,
            "action_used": self._action_count,
            "allow_unnatural": self.allow_unnatural,
        }
