"""Modular skill mixin extracted from VanillaSkills."""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import time

from .common import (
    ActionError,
    CROP_BLOCKS,
    CROP_SPECS,
    DEEP_ORE_LAYERS,
    DIGGABLE_GROUND,
    FOOD_NUTRITION,
    HUNTABLE,
    HUNT_ALIASES,
    LOG_SUFFIX,
    MAX_CELLS,
    ORE_ITEM_IDS,
    ORE_QUERIES,
    PLANK_IDS,
    RAW_FOOD_COOKING,
    RESOURCE_ALIASES,
    STONE_IDS,
    SkillError,
    TOOL_SUFFIXES,
    TOOL_TIER_RANK,
    _matches_resource,
    _num,
    _position,
    parse_transfer_items,
    tool_rank,
)
from ...farm_memory import FarmMemory


class GatheringMixin:
    async def _gather_scan_fallback(self, resource, radius):
        """Raw ore/sand scan fallback when the normal exposed-resource scan is empty."""
        query = ORE_QUERIES.get(resource)
        if resource == "sand":
            query = "sand,red_sand"
        if not query:
            return []
        scan = await self._call("scan_blocks", {"radius": radius, "limit": 16, "block": query}, timeout=15)
        rows = [row for row in (scan.get("blocks") or []) if isinstance(row, dict)]
        found = []
        for row in rows[:12]:
            try:
                x, y, z = int(row["x"]), int(row["y"]), int(row["z"])
            except (KeyError, TypeError, ValueError):
                continue
            context = await self._call("block_context", {"x": x, "y": y, "z": z}, timeout=10)
            stands = [stand for stand in (context.get("stands") or []) if isinstance(stand, dict)]
            if stands:
                stand = min(stands, key=lambda item: _num(item.get("distance"), 999))
                found.append({
                    "x": x, "y": y, "z": z,
                    "distance": _num(row.get("distance"), 999),
                    "approaches": [stand],
                })
            else:
                found.append({
                    "x": x, "y": y, "z": z,
                    "distance": _num(row.get("distance"), 999),
                    "approaches": [],
                    "excavate": True,
                })
            if len(found) >= 5:
                break
        return found

    async def _nearest_raw_target(self, resource, radius):
        query = ORE_QUERIES.get(resource)
        if resource == "sand":
            query = "sand,red_sand"
        if not query:
            return None
        scan = await self._call("scan_blocks", {"radius": radius, "limit": 16, "block": query}, timeout=15)
        rows = [row for row in (scan.get("blocks") or []) if isinstance(row, dict)]
        return min(rows, key=lambda row: _num(row.get("distance"), 999)) if rows else None

    async def gather(self, resource="logs", count=8, radius=12, max_seconds=180, allow_night=False):
        resource = RESOURCE_ALIASES.get(str(resource).strip().lower(), str(resource).strip().lower())
        if resource not in {"logs", "stone", "wheat", "sand"} and resource not in ORE_QUERIES:
            raise SkillError("只支持原木、石头、小麦、沙子和各种原矿")
        await self._ensure_no_screen()
        target = max(1, min(256, int(count)))
        radius = max(6, min(16, int(radius)))
        status = await self.status()
        baseline = self.item_count(status, resource)
        deadline = time.monotonic() + max(20, min(600, int(max_seconds)))
        self._mark("gather", "collecting", 0, target, {"resource": resource})
        tried = set()
        failed = 0
        explored = 0
        while self.item_count(await self.status(), resource) - baseline < target:
            if time.monotonic() >= deadline:
                raise SkillError(f"采集超时，已获得 {self.item_count(await self.status(), resource) - baseline}/{target}")
            status = await self.status()
            if _num(status.get("health"), 20) <= 8:
                raise SkillError("血量过低，采集已停止")
            danger = [h for h in (status.get("hostiles") or []) if isinstance(h, dict) and _num(h.get("distance"), 999) <= 4 and h.get("line_of_sight", True) is not False]
            if danger:
                raise SkillError("附近有敌对生物，采集已停止")
            try:
                eat_threshold = int((getattr(self.plugin, "config", {}) or {}).get("auto_eat_food", 12) or 12)
            except (TypeError, ValueError):
                eat_threshold = 12
            if _num(status.get("food"), 20) <= eat_threshold or getattr(self.plugin, "_auto_eat_requested", False):
                await self._auto_eat_if_needed(eat_threshold)
            scan = await self._call("scan", {"resource": resource, "radius": radius}, timeout=15)
            resources = [node for node in (scan.get("resources") or []) if isinstance(node, dict)]
            resources = [node for node in resources if (int(node.get("x", 0)), int(node.get("y", 0)), int(node.get("z", 0))) not in tried]
            if not resources:
                resources = [
                    node for node in await self._gather_scan_fallback(resource, radius)
                    if (int(node.get("x", 0)), int(node.get("y", 0)), int(node.get("z", 0))) not in tried
                ]
            if not resources:
                if explored >= 2:
                    gained = self.item_count(await self.status(), resource) - baseline
                    if gained > 0:
                        return {
                            "skill": "gather",
                            "resource": resource,
                            "requested": target,
                            "collected": gained,
                            "targets_tried": len(tried),
                            "partial": True,
                            "reason": "附近没有更多可采集的目标",
                        }
                    raw = await self._nearest_raw_target(resource, radius)
                    if raw:
                        raise SkillError(
                            f"附近有{resource}矿，但没有可站立/可见的挖掘面；最近在 "
                            f"{int(raw.get('x', 0))} {int(raw.get('y', 0))} {int(raw.get('z', 0))}"
                        )
                    raise SkillError("附近没有可采集的目标")
                ground = [node for node in (scan.get("ground") or []) if isinstance(node, dict) and 4 <= _num(node.get("distance"), 0) <= 12]
                if not ground:
                    raise SkillError("附近没有可采集目标，也没有可探索落脚点")
                point = min(ground, key=lambda n: _num(n.get("distance"), 999))
                error = await self.plugin._approach_target(point["x"], point.get("feet_y", point["y"]), point["z"], seconds=10, reach=1.5)
                if error:
                    raise SkillError("探索移动失败：" + error)
                explored += 1
                continue
            def _node_score(n):
                rows = [row for row in (n.get("approaches") or []) if isinstance(row, dict)]
                if rows:
                    return min(_num(row.get("distance"), 999) for row in rows)
                return _num(n.get("distance"), 999) + 20

            node = min(resources, key=_node_score)
            key = (int(node.get("x", 0)), int(node.get("y", 0)), int(node.get("z", 0)))
            tried.add(key)
            approaches = [row for row in (node.get("approaches") or []) if isinstance(row, dict)]
            stand = min(approaches, key=lambda row: len(row.get("clear") or []) * 5 + _num(row.get("distance"), 999)) if approaches else node
            current = status.get("position") or {}
            try:
                distance = math.dist(
                    (float(current.get("x", 0)), float(current.get("y", 0)), float(current.get("z", 0))),
                    (float(stand.get("x", 0)), float(stand.get("y", 0)), float(stand.get("z", 0))),
                )
            except (TypeError, ValueError):
                distance = 99
            if distance > 1.6:
                excavate = bool(node.get("excavate")) or not approaches
                if excavate:
                    error = await self.plugin._approach_target(
                        node["x"], node["y"], node["z"], seconds=20, reach=2.5, allow_break=True
                    )
                else:
                    error = await self.plugin._approach_target(
                        stand.get("x", node["x"]), stand.get("feet_y", stand.get("y", node["y"])),
                        stand.get("z", node["z"]), seconds=12, reach=1.5
                    )
                    if error:
                        error = await self.plugin._approach_target(
                            node["x"], node["y"], node["z"], seconds=20, reach=2.5, allow_break=True
                        )
                if error:
                    if await self._expose_block(node):
                        try:
                            await self._run(
                                "mine_at",
                                {"x": int(node["x"]), "y": int(node["y"]), "z": int(node["z"])},
                                seconds=12,
                            )
                            await self._pickup_near(
                                int(node["x"]), int(node["y"]), int(node["z"]), resource,
                            )
                        except Exception:
                            pass
                        if self.item_count(await self.status(), resource) - baseline > 0:
                            continue
                    failed += 1
                    if failed >= 12:
                        raise SkillError("无法接近目标：" + error)
                    continue
            for clear in (stand.get("clear") or [])[:8]:
                if not isinstance(clear, dict):
                    continue
                try:
                    await self._run("mine_at", {"x": int(clear["x"]), "y": int(clear["y"]), "z": int(clear["z"])}, seconds=8)
                except Exception:
                    pass
            try:
                await self._run("mine_at", {"x": int(node["x"]), "y": int(node["y"]), "z": int(node["z"])}, seconds=12)
            except Exception as exc:
                await self._pickup_near(int(node["x"]), int(node["y"]), int(node["z"]), resource)
                if self.item_count(await self.status(), resource) - baseline > 0:
                    continue
                if await self._expose_block(node):
                    try:
                        await self._run(
                            "mine_at",
                            {"x": int(node["x"]), "y": int(node["y"]), "z": int(node["z"])},
                            seconds=12,
                        )
                        await self._pickup_near(
                            int(node["x"]), int(node["y"]), int(node["z"]), resource,
                        )
                    except Exception:
                        pass
                    if self.item_count(await self.status(), resource) - baseline > 0:
                        continue
                failed += 1
                if failed >= 12:
                    raise SkillError("挖掘连续失败：" + str(exc))
                continue
            await self._pickup_near(int(node["x"]), int(node["y"]), int(node["z"]), resource)
            await asyncio.sleep(0.3)
            await self._pickup_near(int(node["x"]), int(node["y"]), int(node["z"]), resource)
            gained = self.item_count(await self.status(), resource) - baseline
            if gained <= 0:
                failed += 1
                if failed >= 5:
                    raise SkillError("没有确认到掉落物入包")
            else:
                self._mark("gather", "collecting", gained, target, {"resource": resource})
        status = await self.status()
        return {
            "skill": "gather",
            "resource": resource,
            "requested": target,
            "collected": self.item_count(status, resource) - baseline,
            "targets_tried": len(tried),
        }

    # ------------------------------------------------------------------ craft
