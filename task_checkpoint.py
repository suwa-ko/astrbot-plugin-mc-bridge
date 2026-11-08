"""Checkpoint and resume helpers for interrupted Minecraft skill tasks.

The scheduler keeps task identity and remaining budget.  This module keeps the
small, bounded evidence needed to continue a long skill without repeating work
that was already confirmed by the client.
"""
from __future__ import annotations

import json
import time
from typing import Any

SKILL_ALIASES = {
    "mine_ore": "gather",
    "craft_item": "craft",
    "smelt_item": "smelt",
    "transfer_item": "transfer",
    "container_transfer": "transfer_many",
    "return_home": "return_to",
    "find_and_sleep": "sleep",
}

COUNT_SKILLS = {
    "gather", "mine_ore", "craft", "craft_item", "smelt", "smelt_item", "farm",
}
TRANSFER_SKILLS = {"transfer", "transfer_item", "container_transfer"}


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def inventory_counts(status: dict[str, Any] | None) -> dict[str, int]:
    """Return a compact item -> count map from a client status payload."""
    if not isinstance(status, dict):
        return {}
    rows = status.get("inventory_items")
    counts: dict[str, int] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                slot = int(row.get("slot", 0) or 0)
            except (TypeError, ValueError):
                slot = 0
            if slot >= 36:
                continue
            item = str(row.get("id") or "").strip()
            count = _to_int(row.get("count"), 0)
            if item and count > 0:
                counts[item] = counts.get(item, 0) + count
        return counts
    inventory = status.get("inventory")
    if isinstance(inventory, dict):
        for item, count in inventory.items():
            item = str(item or "").strip()
            count = _to_int(count, 0)
            if item and count > 0:
                counts[item] = count
    return counts


def baseline_from_status(status: dict[str, Any] | None) -> dict[str, Any]:
    """Capture the small baseline needed to compare a resumed task."""
    if not isinstance(status, dict):
        return {}
    position = status.get("position") if isinstance(status.get("position"), dict) else {}
    return {
        "position": {
            key: position.get(key)
            for key in ("x", "y", "z")
            if position.get(key) is not None
        },
        "dimension": str(status.get("dimension") or "")[:80],
        "world_key": str(status.get("world_key") or "")[:120],
        "health": status.get("health"),
        "food": status.get("food"),
        "inventory": inventory_counts(status),
        "captured_at": time.time(),
    }


def _bounded_detail(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    detail: dict[str, Any] = {}
    for key, item in list(value.items())[:16]:
        key = str(key)[:40]
        if isinstance(item, (str, int, float, bool)) or item is None:
            detail[key] = item if not isinstance(item, str) else item[:240]
        elif isinstance(item, list):
            detail[key] = item[:64]
        elif isinstance(item, dict):
            detail[key] = {
                str(k)[:60]: v
                for k, v in list(item.items())[:64]
                if isinstance(v, (str, int, float, bool)) or v is None
            }
    try:
        if len(json.dumps(detail, ensure_ascii=False, separators=(",", ":"))) > 4096:
            return {}
    except (TypeError, ValueError):
        return {}
    return detail


def clean_checkpoint(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        "skill": str(value.get("skill") or "")[:40],
        "stage": str(value.get("stage") or "")[:80],
        "completed": max(0, _to_int(value.get("completed"), 0)),
        "total": max(0, _to_int(value.get("total"), 0)),
        "detail": _bounded_detail(value.get("detail")),
        "at": _to_float(value.get("at"), 0.0),
    }


def clean_baseline(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    position = value.get("position") if isinstance(value.get("position"), dict) else {}
    inventory = value.get("inventory") if isinstance(value.get("inventory"), dict) else {}
    return {
        "position": {
            key: position.get(key)
            for key in ("x", "y", "z")
            if position.get(key) is not None
        },
        "dimension": str(value.get("dimension") or "")[:80],
        "world_key": str(value.get("world_key") or "")[:120],
        "health": value.get("health"),
        "food": value.get("food"),
        "inventory": {
            str(item)[:120]: max(0, _to_int(count, 0))
            for item, count in list(inventory.items())[:80]
            if str(item or "").strip()
        },
        "captured_at": _to_float(value.get("captured_at"), 0.0),
    }


def checkpoint_matches(opcode: str, checkpoint: dict[str, Any] | None) -> bool:
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    expected = SKILL_ALIASES.get(str(opcode or ""), str(opcode or ""))
    actual = str(checkpoint.get("skill") or "")
    return bool(expected) and actual == expected


def _remaining_count(params: dict[str, Any], completed: int) -> int:
    target = max(1, _to_int(params.get("count"), 1))
    return max(0, target - min(target, max(0, completed)))


def resume_plan(opcode: str, params: dict[str, Any] | None, checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    """Return adjusted parameters and a resume mode for one interrupted task."""
    original = dict(params or {})
    checkpoint = clean_checkpoint(checkpoint)
    opcode = str(opcode or "")
    if not checkpoint or not checkpoint_matches(opcode, checkpoint):
        return {"params": original, "mode": "rerun", "checkpoint": checkpoint,
                "note": "没有匹配的阶段检查点，按原参数重新执行"}
    completed = max(0, int(checkpoint.get("completed", 0) or 0))
    total = max(0, int(checkpoint.get("total", 0) or 0))
    if completed <= 0:
        return {"params": original, "mode": "rerun", "checkpoint": checkpoint,
                "note": "检查点没有已确认进度，按原参数重新执行"}
    if opcode in COUNT_SKILLS:
        remaining = _remaining_count(original, completed)
        if remaining <= 0:
            return {"params": original, "mode": "complete", "checkpoint": checkpoint,
                    "note": f"检查点显示目标已完成（{completed}/{total or completed}）"}
        adjusted = dict(original)
        adjusted["count"] = remaining
        return {"params": adjusted, "mode": "checkpoint", "checkpoint": checkpoint,
                "note": f"从检查点继续：已完成 {completed}/{total or completed}，剩余 {remaining}"}
    if opcode in {"transfer", "transfer_item"}:
        remaining = _remaining_count(original, completed)
        if remaining <= 0:
            return {"params": original, "mode": "complete", "checkpoint": checkpoint,
                    "note": "检查点显示搬运目标已完成"}
        adjusted = dict(original)
        adjusted["count"] = remaining
        return {"params": adjusted, "mode": "checkpoint", "checkpoint": checkpoint,
                "note": f"从检查点继续：已搬运 {completed}/{total or completed}"}
    if opcode == "container_transfer":
        items = original.get("items")
        if isinstance(items, list):
            moves = checkpoint.get("detail", {}).get("moves")
            moves = moves if isinstance(moves, dict) else {}
            adjusted_items = []
            for move in items:
                if not isinstance(move, dict):
                    continue
                item = str(move.get("item") or "")
                count = max(0, _to_int(move.get("count"), 0))
                moved = max(0, _to_int(moves.get(item), 0))
                remaining = max(0, count - min(count, moved))
                if remaining > 0:
                    adjusted_items.append({"item": item, "count": remaining})
            if not adjusted_items:
                return {"params": original, "mode": "complete", "checkpoint": checkpoint,
                        "note": "检查点显示批量搬运目标已完成"}
            adjusted = dict(original)
            adjusted["items"] = adjusted_items
            return {"params": adjusted, "mode": "checkpoint", "checkpoint": checkpoint,
                    "note": "从检查点继续：已按物品扣除已搬运数量"}
        remaining = _remaining_count(original, completed)
        if remaining <= 0:
            return {"params": original, "mode": "complete", "checkpoint": checkpoint,
                    "note": "检查点显示搬运目标已完成"}
        adjusted = dict(original)
        adjusted["count"] = remaining
        return {"params": adjusted, "mode": "checkpoint", "checkpoint": checkpoint,
                "note": f"从检查点继续：已搬运 {completed}/{total or completed}"}
    if opcode == "build":
        positions = checkpoint.get("detail", {}).get("positions")
        if isinstance(positions, list) and positions:
            adjusted = dict(original)
            adjusted["skip_positions"] = positions
            return {"params": adjusted, "mode": "checkpoint", "checkpoint": checkpoint,
                    "note": f"从检查点继续：已放置 {completed}/{total or completed}"}
    return {"params": original, "mode": "rerun", "checkpoint": checkpoint,
            "note": "该技能没有安全的断点续作路径，按原参数重新执行"}
