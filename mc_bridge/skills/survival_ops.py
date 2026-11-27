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
    CONTAINER_BLOCKS_QUERY,
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


class SurvivalOpsMixin:
    async def _ensure_iron_ingots(self, need):
        need = max(1, int(need))
        status = await self.status()
        have = self.item_count(status, "minecraft:iron_ingot")
        if have >= need:
            return have
        raw = self.item_count(status, "minecraft:raw_iron")
        raw += self.item_count(status, "minecraft:iron_ore")
        raw += self.item_count(status, "minecraft:deepslate_iron_ore")
        missing = need - have
        if raw < missing:
            await self.gather("iron", missing - raw)
        await self.smelt("minecraft:iron_ingot", missing)
        return self.item_count(await self.status(), "minecraft:iron_ingot")

    async def _food_containers(self, radius=16, limit=8):
        data = await self._call(
            "scan_blocks",
            {"radius": max(2, min(16, int(radius))), "limit": max(1, min(16, int(limit))),
             "block": CONTAINER_BLOCKS_QUERY},
            timeout=10,
        )
        rows = [row for row in (data.get("blocks") or []) if isinstance(row, dict)]
        rows.sort(key=lambda row: _num(row.get("distance"), 999))
        return rows

    async def _withdraw_food(self, points_needed, deadline, radius=16):
        if points_needed <= 0:
            return {"withdrawn": 0, "items": [], "containers": 0}
        withdrawn = 0
        items = []
        containers = 0
        for row in await self._food_containers(radius=radius):
            if time.monotonic() >= deadline or withdrawn >= points_needed:
                break
            containers += 1
            try:
                read = await self.open_and_read({"x": int(row["x"]), "y": int(row["y"]), "z": int(row["z"])})
            except SkillError:
                continue
            moves = []
            for slot in (read.get("slots") or []):
                if not isinstance(slot, dict) or slot.get("safe_food") is not True:
                    continue
                item_id = str(slot.get("id") or "")
                try:
                    count = int(slot.get("count", 0) or 0)
                    nutrition = int(slot.get("nutrition", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if not item_id or count <= 0:
                    continue
                if nutrition <= 0:
                    nutrition = int(FOOD_NUTRITION.get(item_id, 1) or 1)
                reserve = 1 if count > 1 else 0
                take = min(count - reserve, max(0, int(math.ceil((points_needed - withdrawn) / max(1, nutrition)))))
                if take > 0:
                    moves.append({"item": item_id, "count": take})
                    withdrawn += take * nutrition
            if not moves:
                continue
            try:
                result = await self.transfer_many(
                    moves, direction="from_container",
                    x=int(row["x"]), y=int(row["y"]), z=int(row["z"]), close=True,
                )
                moved = int(result.get("moved", 0) or 0)
                if moved:
                    items.extend(result.get("items") or [])
            except SkillError:
                continue
        return {"withdrawn": withdrawn, "items": items, "containers": containers}

    async def _cook_and_craft_food(self, points_needed):
        actions = []
        shortages = []
        if points_needed <= 0:
            return actions, shortages
        status = await self.status()
        for raw_id, cooked_id in RAW_FOOD_COOKING.items():
            try:
                count = self.item_count(status, raw_id)
            except Exception:
                count = 0
            if count <= 0:
                continue
            try:
                await self.smelt(cooked_id, min(32, count))
                actions.append(f"smelt:{cooked_id}")
                status = await self.status()
            except SkillError as exc:
                shortages.append(f"烹饪 {cooked_id}: {exc}")
        wheat = self.item_count(await self.status(), "minecraft:wheat")
        if wheat >= 3:
            bread_needed = min(16, max(1, int(math.ceil(points_needed / 5))))
            if wheat >= bread_needed * 3:
                try:
                    await self.craft("minecraft:bread", bread_needed)
                    actions.append(f"craft:bread x{bread_needed}")
                except SkillError as exc:
                    shortages.append(f"面包: {exc}")
        return actions, shortages

    async def ensure_food(self, points=40, max_seconds=600, withdraw=True, harvest=True,
                          cook=True, trade=True):
        target = max(0, min(256, int(points)))
        deadline = time.monotonic() + max(30, min(900, int(max_seconds)))
        status = await self.status()
        before_points = self.food_points(status)
        before_count = self.food_count(status)
        actions, shortages = [], []
        if target <= before_points:
            return {
                "skill": "ensure_food", "target_points": target,
                "before_points": before_points, "after_points": before_points,
                "before_items": before_count, "after_items": before_count,
                "actions": actions, "shortages": shortages, "complete": True,
            }
        if withdraw:
            try:
                result = await self._withdraw_food(target - before_points, deadline)
                if result.get("withdrawn"):
                    actions.append("container_food")
            except SkillError as exc:
                shortages.append(f"食物箱: {exc}")
        if harvest and self.food_points(await self.status()) < target:
            memory = self._farm_memory()
            if memory is not None and memory.list():
                try:
                    result = await self.farm_plot("harvest", count=64, replant=True)
                    if result.get("harvested"):
                        actions.append(f"farm_plot:harvest x{result['harvested']}")
                except SkillError as exc:
                    shortages.append(f"收田块: {exc}")
            for crop in ("potatoes", "carrots", "wheat", "beetroots"):
                if self.food_points(await self.status()) >= target or time.monotonic() >= deadline:
                    break
                try:
                    result = await self.farm("harvest", radius=12, count=16, replant=True, crop=crop)
                    if result.get("harvested"):
                        actions.append(f"harvest:{crop} x{result['harvested']}")
                except SkillError as exc:
                    shortages.append(f"收{crop}: {exc}")
        if cook and self.food_points(await self.status()) < target:
            cooked_actions, cooked_shortages = await self._cook_and_craft_food(
                max(0, target - self.food_points(await self.status())),
            )
            actions.extend(cooked_actions)
            shortages.extend(cooked_shortages)
        if trade and self.food_points(await self.status()) < target and time.monotonic() < deadline:
            missing = max(1, int(math.ceil((target - self.food_points(await self.status())) / 5)))
            for item in ("minecraft:bread", "minecraft:baked_potato"):
                try:
                    await self.trade_for(item, min(8, missing))
                    actions.append(f"trade:{item}")
                    break
                except SkillError as exc:
                    shortages.append(f"交易 {item}: {exc}")
        after_status = await self.status()
        after_points = self.food_points(after_status)
        after_count = self.food_count(after_status)
        return {
            "skill": "ensure_food", "target_points": target,
            "before_points": before_points, "after_points": after_points,
            "before_items": before_count, "after_items": after_count,
            "actions": actions, "shortages": shortages,
            "complete": after_points >= target,
        }

    async def resupply(self, food=8, torches=8, blocks=32, tools=True, max_seconds=300):
        food_target = max(0, min(64, int(food)))
        torch_target = max(0, min(64, int(torches)))
        block_target = max(0, min(256, int(blocks)))
        deadline = time.monotonic() + max(30, min(900, int(max_seconds)))
        actions, shortages = [], []

        async def snapshot():
            status = await self.status()
            items = self.inventory_items(status)
            safe_food = self.food_count(status)
            food_points = self.food_points(status)
            torch_count = self.item_count(status, "minecraft:torch")
            block_count = self.item_count(status, "minecraft:cobblestone")
            tool_ids = [str(row.get("id", "")) for row in items]
            tool_ok = all(any(tool_rank(item_id) >= 3 and item_id.endswith(suffix) for item_id in tool_ids) for suffix in TOOL_SUFFIXES)
            return {
                "food": safe_food, "food_points": food_points,
                "torches": torch_count, "cobblestone": block_count, "tools_ok": tool_ok,
            }

        before = await snapshot()
        if tools and not before["tools_ok"]:
            for suffix, item, ingots in (
                ("_pickaxe", "minecraft:iron_pickaxe", 3),
                ("_axe", "minecraft:iron_axe", 3),
                ("_sword", "minecraft:iron_sword", 2),
            ):
                if time.monotonic() >= deadline:
                    shortages.append("补给超时，工具未补齐")
                    break
                status = await self.status()
                if any(tool_rank(str(row.get("id", ""))) >= 3 and str(row.get("id", "")).endswith(suffix) for row in self.inventory_items(status)):
                    continue
                try:
                    await self._ensure_iron_ingots(ingots)
                    await self.craft(item, 1)
                    actions.append("craft:" + item)
                except SkillError as exc:
                    shortages.append(f"工具 {item}: {exc}")
        if time.monotonic() >= deadline:
            shortages.append("补给超时，剩余项目未执行")
        else:
            status = await self.status()
            current_torches = self.item_count(status, "minecraft:torch")
            missing = max(0, torch_target - current_torches)
            if missing:
                need_craft = math.ceil(missing / 4)
                need_coal = need_craft
                try:
                    coal_have = self.item_count(status, "minecraft:coal")
                    if coal_have < need_coal:
                        await self.gather("coal", need_coal - coal_have)
                        actions.append("gather:coal")
                except SkillError:
                    try:
                        await self.smelt("minecraft:charcoal", need_coal)
                        actions.append("smelt:charcoal")
                    except SkillError as exc:
                        shortages.append(f"火把缺少燃料: {exc}")
                try:
                    await self.craft("minecraft:torch", missing)
                    actions.append("craft:torch")
                except SkillError as exc:
                    shortages.append(f"火把: {exc}")
        if time.monotonic() >= deadline:
            shortages.append("补给超时，剩余项目未执行")
        else:
            status = await self.status()
            current_blocks = self.item_count(status, "minecraft:cobblestone")
            missing = max(0, block_target - current_blocks)
            if missing:
                try:
                    await self.gather("stone", missing)
                    actions.append("gather:stone")
                except SkillError as exc:
                    shortages.append(f"方块: {exc}")
        if time.monotonic() >= deadline:
            shortages.append("补给超时，剩余项目未执行")
        else:
            status = await self.status()
            food_target_points = food_target * 5
            current_points = self.food_points(status)
            if current_points < food_target_points:
                try:
                    result = await self.ensure_food(
                        food_target_points,
                        max_seconds=max(30, int(deadline - time.monotonic())),
                    )
                    actions.extend(result.get("actions") or [])
                    shortages.extend(result.get("shortages") or [])
                    if not result.get("complete"):
                        shortages.append(
                            f"食物不足：{result.get('after_points', 0)}/{food_target_points} 点"
                        )
                except SkillError as exc:
                    shortages.append(f"食物: {exc}")
        after = await snapshot()
        complete = (
            after["food_points"] >= food_target * 5
            and after["torches"] >= torch_target
            and after["cobblestone"] >= block_target
            and (not tools or after["tools_ok"])
        )
        return {
            "skill": "resupply",
            "before": before,
            "after": after,
            "targets": {
                "food": food_target, "food_points": food_target * 5,
                "torches": torch_target, "cobblestone": block_target, "tools": bool(tools),
            },
            "actions": actions,
            "shortages": shortages,
            "complete": complete,
        }

    # ---------------------------------------------------------------- explore
    async def explore(self, goal="generic", max_seconds=120, max_distance=64, return_home=False, allow_night=False):
        goal = str(goal or "generic").strip().lower()
        if goal not in {"generic", "logs", "stone", "wheat", "village"} and goal not in ORE_QUERIES:
            raise SkillError("goal 只能是 generic/logs/stone/wheat/village 或各种原矿")
        deadline = time.monotonic() + max(10, min(300, int(max_seconds)))
        max_distance = max(16, min(128, int(max_distance)))
        status = await self.status()
        origin = status.get("position") or {}
        try:
            ox, oy, oz = float(origin["x"]), float(origin["y"]), float(origin["z"])
        except (KeyError, TypeError, ValueError):
            raise SkillError("起点位置数据不完整")
        origin_scope = {"world_key": status.get("world_key"), "dimension": status.get("dimension")}
        visited = {(int(ox), int(oy), int(oz))}
        found, seen = [], set()
        visits = 0
        failures = 0
        stopped = "no_frontier"
        scan_resource = goal if goal in {"logs", "stone", "wheat"} else "logs"

        def add_found(kind, row, name=""):
            try:
                key = (kind, int(row["x"]), int(row["y"]), int(row["z"]))
            except (KeyError, TypeError, ValueError):
                return
            if key in seen or len(found) >= 32:
                return
            seen.add(key)
            found.append({"type": kind, "name": name, "x": key[1], "y": key[2], "z": key[3]})

        while True:
            if time.monotonic() >= deadline:
                stopped = "timeout"
                break
            status = await self.status()
            if origin_scope["world_key"] and (status.get("world_key") != origin_scope["world_key"] or status.get("dimension") != origin_scope["dimension"]):
                stopped = "world_changed"
                break
            if _num(status.get("health"), 20) <= 8:
                stopped = "low_health"
                break
            hostiles = [row for row in (status.get("hostiles") or []) if isinstance(row, dict) and _num(row.get("distance"), 999) <= 5 and row.get("line_of_sight", True) is not False]
            if hostiles:
                stopped = "danger"
                break
            position = status.get("position") or {}
            try:
                px, py, pz = float(position["x"]), float(position["y"]), float(position["z"])
            except (KeyError, TypeError, ValueError):
                stopped = "position_unknown"
                break
            if math.hypot(px - ox, pz - oz) >= max_distance:
                stopped = "max_distance"
                break
            scan = await self._call("scan", {"resource": scan_resource, "radius": 12}, timeout=15)
            for row in (scan.get("resources") or []):
                if isinstance(row, dict):
                    block = str(row.get("block") or "")
                    add_found(block or (goal if goal in {"logs", "stone", "wheat"} else "resource"), row, block)
            chests = await self._call(
                "scan_blocks",
                {"radius": 12, "limit": 16,
                 "block": "minecraft:chest,minecraft:trapped_chest,minecraft:barrel"},
                timeout=10,
            )
            for row in (chests.get("blocks") or []):
                if isinstance(row, dict):
                    add_found("container", row, str(row.get("block") or ""))
            if goal in ORE_QUERIES:
                for row in await self._gather_scan_fallback(goal, 12):
                    add_found(str(row.get("block") or goal), row, str(row.get("block") or goal))
            if goal in {"generic", "village"}:
                entities = await self._call("scan_entities", {"radius": 16, "limit": 32, "type": "minecraft:villager"}, timeout=10)
                for row in (entities.get("entities") or []):
                    if not isinstance(row, dict):
                        continue
                    kind = str(row.get("type") or "")
                    if "villager" in kind:
                        add_found("village", row, str(row.get("name") or kind))
            if goal == "generic":
                for row in (scan.get("workbenches") or []):
                    if isinstance(row, dict):
                        add_found("workbench", row)
                for row in (scan.get("beds") or []):
                    if isinstance(row, dict):
                        add_found("bed", row)
                for row in (scan.get("stations") or []):
                    if isinstance(row, dict):
                        add_found("station", row, str(row.get("item") or ""))
            ground = []
            for row in (scan.get("ground") or []):
                if not isinstance(row, dict):
                    continue
                key = (int(row.get("x", 0)), int(row.get("y", 0)), int(row.get("z", 0)))
                if key in visited:
                    continue
                distance = _num(row.get("distance"), 0)
                if not 6 <= distance <= 24:
                    continue
                try:
                    if math.hypot(float(row["x"]) - ox, float(row["z"]) - oz) > max_distance:
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                ground.append(row)
            if not ground:
                stopped = "no_frontier"
                break
            point = max(ground, key=lambda row: _num(row.get("distance"), 0))
            key = (int(point.get("x", 0)), int(point.get("y", 0)), int(point.get("z", 0)))
            visited.add(key)
            error = await self.plugin._approach_target(
                point.get("x", 0), point.get("feet_y", point.get("y", 0)), point.get("z", 0),
                seconds=15, reach=1.8,
            )
            if error:
                failures += 1
                if failures >= 3:
                    stopped = "unreachable"
                    break
                continue
            visits += 1
            failures = 0
        returned = None
        if return_home:
            try:
                await self.plugin._move_and_wait(ox, oy, oz, seconds=120)
                final = await self.status()
                position = final.get("position") or {}
                returned = math.hypot(float(position.get("x", 0)) - ox, float(position.get("z", 0)) - oz) <= 2.0
            except Exception:
                returned = False
        return {
            "skill": "explore",
            "goal": goal,
            "visited": visits,
            "found": found,
            "stopped": stopped,
            "returned": returned,
        }

    # ------------------------------------------------------------------ combat
    async def attack_target(self, target, max_seconds=20, retreat_health=8):
        """L1：已知目标，换武器、接近、攻击并核对死亡/撤退。"""
        if isinstance(target, dict):
            target_text = str(target.get("uuid") or target.get("type") or target.get("name") or "").strip()
        else:
            target_text = str(target or "").strip()
        if not target_text:
            raise SkillError("attack_target 需要目标名称、类型或UUID")
        retreat_health = max(1, min(20, int(retreat_health)))
        deadline = time.monotonic() + max(3, min(60, int(max_seconds)))
        selector = getattr(self.plugin, "_select_best_weapon", None)
        if selector is not None:
            try:
                await selector()
            except Exception:
                pass
        attacked = 0
        while time.monotonic() < deadline:
            status = await self.status()
            hostiles = [row for row in (status.get("hostiles") or []) if isinstance(row, dict)]
            matches = [
                row for row in hostiles
                if target_text.lower() in str(row.get("name", "")).lower()
                or target_text.lower() in str(row.get("type", "")).lower()
                or target_text == str(row.get("uuid") or "")
            ]
            hostile = min(matches, key=lambda row: _num(row.get("distance"), 999)) if matches else None
            if _num(status.get("health"), 20) <= retreat_health:
                outcome = None
                if hostile is not None and hasattr(self.plugin, "_auto_flee"):
                    try:
                        outcome = await self.plugin._auto_flee(status, hostile)
                    except Exception:
                        outcome = None
                return {"skill": "attack_target", "target": target_text, "attacked": attacked, "result": "retreated", "detail": outcome}
            if hostile is None:
                return {"skill": "attack_target", "target": target_text, "attacked": attacked, "result": "no_target"}
            if _num(hostile.get("distance"), 99) > 3.2:
                position = hostile.get("position") or {}
                error = await self.plugin._approach_target(position.get("x", 0), position.get("y", 64), position.get("z", 0), seconds=6, reach=2.4)
                if error:
                    return {"skill": "attack_target", "target": target_text, "attacked": attacked, "result": "unreachable", "detail": error}
                continue
            data = await self._call("attack", {"target": str(hostile.get("type") or hostile.get("name") or target_text)}, timeout=10)
            attacked += 1
            if data.get("dead"):
                return {"skill": "attack_target", "target": target_text, "attacked": attacked, "result": "killed", "enemy": data.get("target")}
            await asyncio.sleep(0.65)
        return {"skill": "attack_target", "target": target_text, "attacked": attacked, "result": "timeout"}

    async def _scan_hunt_targets(self, radius, wanted_type=""):
        """按具体类型扫描猎物，避免通用实体上限被仙灵等无关实体占满。"""
        types = [wanted_type] if wanted_type else list(HUNTABLE)
        rows = []
        seen = set()
        for type_id in types:
            data = await self._call(
                "scan_entities", {"radius": radius, "limit": 32, "type": type_id}, timeout=10,
            )
            for row in (data.get("entities") or []):
                if not isinstance(row, dict):
                    continue
                uuid = str(row.get("uuid") or "")
                if uuid and uuid in seen:
                    continue
                if uuid:
                    seen.add(uuid)
                rows.append(row)
        return rows

    async def hunt(self, target="auto", count=2, radius=24, max_seconds=180,
                   return_home=True, cook=True, meat_target=0, search=True, search_rounds=6):
        target = str(target or "auto").strip().lower()
        if target != "auto":
            target = HUNT_ALIASES.get(target, target)
            if target not in HUNTABLE:
                raise SkillError("hunt target 只能是 auto 或具体猎物类型（cow/pig/sheep/chicken/rabbit/boar/azurecrane/duck/crow/squirrel/weasel/unagi 等）")
        await self._ensure_no_screen()
        requested = max(1, min(20, int(count)))
        meat_target = max(0, min(64, int(meat_target)))
        search_rounds = max(0, min(12, int(search_rounds)))
        radius = max(4, min(32, int(radius)))
        deadline = time.monotonic() + max(10, min(600, int(max_seconds)))
        wanted_type = "" if target == "auto" else target
        max_approach = min(28.0, float(radius))
        failed_targets = set()
        search_index = 0
        search_directions = (
            (0, 1), (1, 0), (0, -1), (-1, 0),
            (1, 1), (-1, 1), (1, -1), (-1, -1),
        )
        selector = getattr(self.plugin, "_select_best_weapon", None)
        if callable(selector):
            try:
                await selector()
            except Exception:
                pass
        hunted = 0
        errors = []
        collected = {}
        raw_total = 0
        while (
            hunted < requested
            and time.monotonic() < deadline
            and (meat_target <= 0 or raw_total < meat_target)
        ):
            await self._auto_eat_if_needed()
            status = await self.status()
            if _num(status.get("health"), 20) <= 8:
                errors.append("血量过低，停止打猎")
                break
            danger = [
                row for row in (status.get("hostiles") or [])
                if isinstance(row, dict) and _num(row.get("distance"), 999) <= 5
            ]
            if danger:
                errors.append("附近有敌人，停止打猎")
                break
            data = await self._scan_hunt_targets(radius, wanted_type)
            candidates = []
            for row in data:
                if not isinstance(row, dict):
                    continue
                row_type = str(row.get("type") or "")
                if row_type not in HUNTABLE:
                    continue
                if row.get("baby") is True or row.get("tamed") is True:
                    continue
                if wanted_type and row_type != wanted_type:
                    continue
                if _num(row.get("distance"), 999) > max_approach:
                    continue
                if str(row.get("uuid") or "") in failed_targets:
                    continue
                candidates.append(row)
            if not candidates:
                if search and search_rounds > 0 and time.monotonic() < deadline:
                    search_rounds -= 1
                    dx, dz = search_directions[search_index % len(search_directions)]
                    search_index += 1
                    status = await self.status()
                    position = status.get("position") or {}
                    try:
                        x = int(float(position.get("x", 0)))
                        y = int(float(position.get("y", 64)))
                        z = int(float(position.get("z", 0)))
                        await self.return_to([x + dx * 48, y, z + dz * 48], reach=4)
                        errors.append("附近没有猎物，已向新区域搜索")
                    except SkillError as exc:
                        errors.append(f"搜索移动失败：{exc}")
                        try:
                            await self.explore(
                                "generic", max_seconds=30, max_distance=64,
                                return_home=False, allow_night=True,
                            )
                            errors.append("附近没有猎物，继续搜索")
                        except SkillError as explore_exc:
                            errors.append(f"搜索失败：{explore_exc}")
                            break
                    continue
                if not errors:
                    errors.append("附近没有可猎取的被动动物")
                break
            row = min(candidates, key=lambda item: _num(item.get("distance"), 999))
            row_type = str(row.get("type") or "")
            raw_item = HUNTABLE[row_type]
            try:
                x, y, z = int(row["x"]), int(row["y"]), int(row["z"])
            except (KeyError, TypeError, ValueError):
                continue
            if _num(row.get("distance"), 99) > 12:
                try:
                    await self.return_to([x, y, z], reach=3.0)
                except SkillError as exc:
                    errors.append(f"接近猎物失败：{exc}")
                    failed_targets.add(str(row.get("uuid") or ""))
                continue
            if _num(row.get("distance"), 99) > 3.3:
                error = await self.plugin._approach_target(x, y, z, seconds=20, reach=2.2)
                if error:
                    errors.append(f"接近猎物失败：{error}")
                    failed_targets.add(str(row.get("uuid") or ""))
                    continue
            try:
                result = await self._call(
                    "hunt", {"uuid": str(row.get("uuid") or ""), "type": row_type}, timeout=10,
                )
            except SkillError as exc:
                errors.append(f"攻击猎物失败：{exc}")
                failed_targets.add(str(row.get("uuid") or ""))
                await asyncio.sleep(0.3)
                continue
            if result.get("dead"):
                hunted += 1
                await asyncio.sleep(0.5)
                await self._pickup_near(x, y, z, raw_item)
                await asyncio.sleep(0.3)
                await self._pickup_near(x, y, z, raw_item)
                status_after = await self.status()
                collected = {
                    item: self.item_count(status_after, item)
                    for item in set(HUNTABLE.values())
                    if self.item_count(status_after, item) > 0
                }
                raw_total = sum(collected.values())
                self._mark("hunt", "hunting", hunted, requested, {"raw": dict(collected)})
                if meat_target > 0 and raw_total >= meat_target:
                    break
            else:
                await asyncio.sleep(0.45)
        returned = False
        if return_home:
            try:
                await self.return_home()
                returned = True
            except SkillError as exc:
                errors.append(f"回家失败：{exc}")
        cooked = {}
        if cook:
            for raw_item in HUNTABLE.values():
                count_have = self.item_count(await self.status(), raw_item)
                cooked_item = RAW_FOOD_COOKING.get(raw_item)
                if count_have <= 0 or not cooked_item:
                    continue
                try:
                    await self.smelt(cooked_item, min(32, count_have))
                    cooked[cooked_item] = self.item_count(await self.status(), cooked_item)
                except SkillError as exc:
                    errors.append(f"烹饪 {cooked_item} 失败：{exc}")
        final_status = await self.status()
        collected = {
            raw_item: self.item_count(final_status, raw_item)
            for raw_item in set(HUNTABLE.values())
            if self.item_count(final_status, raw_item) > 0
        }
        raw_total = sum(collected.values())
        cooked_total = sum(cooked.values())
        if hunted > 0 and raw_total == 0 and cooked_total == 0:
            errors.append("没有拾取到肉类掉落")
        target_reached = (
            (meat_target > 0 and raw_total >= meat_target)
            or (meat_target <= 0 and hunted >= requested)
        )
        if target_reached and (raw_total + cooked_total) > 0:
            outcome = "partial" if cook and cooked_total == 0 else "fulfilled"
        elif hunted > 0 or (raw_total + cooked_total) > 0:
            outcome = "partial"
        else:
            outcome = "unmet"
        return {
            "skill": "hunt",
            "target": target,
            "requested": requested,
            "meat_target": meat_target,
            "hunted": hunted,
            "raw": collected,
            "cooked": cooked,
            "raw_total": raw_total,
            "cooked_total": cooked_total,
            "returned": returned,
            "errors": errors,
            "goal_outcome": outcome,
        }

    async def _ensure_nether_gold_armor(self, status=None):
        """When in the Nether, equip a golden armor piece from inventory if not already wearing gold."""
        status = status or await self.status()
        if "nether" not in str(status.get("dimension") or "").lower():
            return True
        all_items = [r for r in (status.get("inventory_items") or []) if isinstance(r, dict)]
        armor_slots = {36, 37, 38, 39}
        if any(
            int(r.get("slot", -1)) in armor_slots and "golden_" in str(r.get("id") or "").lower()
            for r in all_items
        ):
            return True
        for row in self.inventory_items(status):
            iid = str(row.get("id") or "").lower()
            if "golden_" in iid and any(iid.endswith(s) for s in ("_boots", "_helmet", "_leggings", "_chestplate")):
                try:
                    await self._run("equip", {"item": iid}, seconds=4)
                    return True
                except Exception:
                    continue
        return False

    async def _ensure_offhand_shield(self, status=None):
        """Best-effort equip a shield to offhand if carried in inventory."""
        status = status or await self.status()
        all_items = [r for r in (status.get("inventory_items") or []) if isinstance(r, dict)]
        if any(str(r.get("id") or "") == "minecraft:shield" and int(r.get("slot", -1)) == 40 for r in all_items):
            return True
        if self.item_count(status, "minecraft:shield") < 1:
            return False
        try:
            await self._run("equip", {"item": "minecraft:shield", "slot": "offhand"}, seconds=4)
            return True
        except Exception:
            return False

    async def _emergency_fire_water_bucket(self, status=None):
        """Extinguish fire or cool lava underfoot using a water bucket outside the Nether."""
        status = status or await self.status()
        if not (status.get("on_fire") or status.get("in_lava")):
            return False
        if "nether" in str(status.get("dimension") or "").lower():
            return False
        if self.item_count(status, "minecraft:water_bucket") < 1:
            return False
        try:
            await self._call("select_slot", {"item": "minecraft:water_bucket"}, timeout=5)
            await self._call("look", {"pitch": 88.0}, timeout=5)
            await self._call("use_item", {}, timeout=5)
            await asyncio.sleep(0.5)
            await self._call("use_item", {}, timeout=5)
            return True
        except Exception:
            return False

    async def _raise_shield_briefly(self, duration=0.7):
        """Raise offhand shield briefly to absorb incoming projectile/explosion."""
        try:
            await self._call("use_item", {"hand": "off_hand"}, timeout=5)
            await asyncio.sleep(max(0.2, min(1.5, float(duration))))
            await self._call("release_use", {}, timeout=5)
            return True
        except Exception:
            return False

    async def _try_ranged_shot(self, status, hostile):
        """Fire one bow shot when carrying bow+arrows against a mid-range hostile."""
        if self.item_count(status, "minecraft:bow") < 1 or self.item_count(status, "minecraft:arrow") < 1:
            return False
        pos = hostile.get("position") or {}
        if not pos:
            return False
        try:
            await self._call("select_slot", {"item": "minecraft:bow"}, timeout=5)
            await self._call("look_at", {"x": pos.get("x", 0), "y": float(pos.get("y", 64)) + 1.0, "z": pos.get("z", 0)}, timeout=5)
            await self._call("use_item", {}, timeout=5)
            await asyncio.sleep(0.95)
            await self._call("release_use", {}, timeout=5)
            return True
        except Exception:
            return False

    async def combat(self, target="", max_seconds=30, retreat_health=8,
                     hold_health=None, finish_low_target=False, max_targets=0,
                     leash_radius=0, blacklist=()):
        target_text = str(target or "").strip().lower()
        deadline = time.monotonic() + max(5, min(120, int(max_seconds)))
        retreat_health = max(1, min(20, int(retreat_health)))
        hold = (max(retreat_health, min(20, int(hold_health)))
                if hold_health is not None else None)
        max_targets = max(0, min(8, int(max_targets or 0)))
        leash = max(0.0, min(48.0, float(leash_radius or 0)))
        blocked = tuple(str(token).strip().lower() for token in (blacklist or ()) if str(token).strip())
        attacked = 0
        anchor = None
        selector = getattr(self.plugin, "_select_best_weapon", None)
        if selector is not None:
            try:
                await selector()
            except Exception:
                pass
        has_shield = await self._ensure_offhand_shield()
        await self._ensure_nether_gold_armor()
        while time.monotonic() < deadline:
            status = await self.status()
            if status.get("in_lava") or status.get("on_fire"):
                await self._emergency_fire_water_bucket(status)
            health = _num(status.get("health"), 20)
            position = status.get("position") if isinstance(status.get("position"), dict) else {}
            if anchor is None and position:
                anchor = dict(position)
            raw_hostiles = [row for row in (status.get("hostiles") or []) if isinstance(row, dict)]
            hostiles = list(raw_hostiles)
            if target_text:
                hostiles = [
                    row for row in hostiles
                    if target_text in str(row.get("name", "")).lower()
                    or target_text in str(row.get("type", "")).lower()
                ]
            hostiles = [
                row for row in hostiles
                if not any(token in (str(row.get("name", "")) + " " + str(row.get("type", ""))).lower()
                           for token in blocked)
            ]
            if not hostiles:
                return {"skill": "combat", "target": target, "attacked": attacked, "result": "no_target"}
            # Creeper blast-range guard: back off from close creepers before engaging.
            close_creeper = next(
                (
                    row for row in raw_hostiles
                    if "creeper" in (str(row.get("name", "")) + " " + str(row.get("type", ""))).lower()
                    and _num(row.get("distance"), 999) <= 5.5
                ),
                None,
            )
            if close_creeper is not None:
                if has_shield:
                    await self._raise_shield_briefly(0.5)
                outcome = await self.plugin._auto_flee(status, close_creeper)
                return {"skill": "combat", "target": target, "attacked": attacked,
                        "result": "retreated_creeper", "detail": outcome}
            hostile = min(hostiles, key=lambda row: _num(row.get("distance"), 999))
            if health <= retreat_health:
                outcome = await self.plugin._auto_flee(status, hostile)
                return {"skill": "combat", "target": target, "attacked": attacked,
                        "result": "retreated", "detail": outcome}
            if hold is not None and health <= hold:
                target_health = _num(hostile.get("health"), 20)
                target_distance = _num(hostile.get("distance"), 999)
                finish = (finish_low_target and len(hostiles) == 1
                          and target_health <= 2 and target_distance <= 3.2)
                if not finish:
                    outcome = await self.plugin._auto_flee(status, hostile)
                    return {"skill": "combat", "target": target, "attacked": attacked,
                            "result": "retreated", "detail": outcome}
            if max_targets and len(hostiles) > max_targets:
                outcome = await self.plugin._auto_flee(status, hostile)
                return {"skill": "combat", "target": target, "attacked": attacked,
                        "result": "retreated", "detail": outcome}
            if leash and anchor:
                position = hostile.get("position") or {}
                try:
                    distance = math.hypot(
                        float(position.get("x", 0)) - float(anchor.get("x", 0)),
                        float(position.get("z", 0)) - float(anchor.get("z", 0)),
                    )
                except (TypeError, ValueError):
                    distance = 0.0
                if distance > leash:
                    return {"skill": "combat", "target": target, "attacked": attacked,
                            "result": "leash_exceeded", "detail": "目标超出战斗半径"}
            dist = _num(hostile.get("distance"), 99)
            hostile_label = (str(hostile.get("name", "")) + " " + str(hostile.get("type", ""))).lower()
            if 5.5 <= dist <= 16.0 and await self._try_ranged_shot(status, hostile):
                attacked += 1
                continue
            if dist > 3.2:
                if has_shield and any(r in hostile_label for r in ("skeleton", "stray", "pillager", "bogged")):
                    await self._raise_shield_briefly(0.5)
                position = hostile.get("position") or {}
                error = await self.plugin._approach_target(
                    position.get("x", 0), position.get("y", 64), position.get("z", 0),
                    seconds=6, reach=2.4,
                )
                if error:
                    return {"skill": "combat", "target": target, "attacked": attacked,
                            "result": "unreachable", "detail": error}
                continue
            data = await self._call(
                "attack",
                {"target": str(hostile.get("type") or hostile.get("name") or "")},
                timeout=10,
            )
            attacked += 1
            if data.get("dead"):
                return {"skill": "combat", "target": target, "attacked": attacked,
                        "result": "killed", "enemy": data.get("target")}
            await asyncio.sleep(0.65)
        return {"skill": "combat", "target": target, "attacked": attacked, "result": "timeout"}

    # --------------------------------------------------------------- trade_for
    async def trade_for(self, item, count=1, radius=16):
        item = str(item or "").strip()
        target = max(1, min(8, int(count)))
        data = await self._call("scan_entities", {"radius": max(4, min(32, int(radius))), "limit": 32}, timeout=10)
        villagers = [row for row in (data.get("entities") or []) if isinstance(row, dict) and "villager" in str(row.get("type", ""))]
        if not villagers:
            raise SkillError("附近没有村民")
        villager = min(villagers, key=lambda row: _num(row.get("distance"), 999))
        error = await self.plugin._approach_target(villager["x"], villager["y"], villager["z"], seconds=12, reach=3.0)
        if error:
            raise SkillError("接近村民失败：" + error)
        await self._call("use_item", {"target": str(villager.get("uuid") or villager.get("name") or "")}, timeout=10)
        await asyncio.sleep(0.6)
        offers = await self._call("trade", {"operation": "read"}, timeout=10)
        rows = [row for row in (offers.get("offers") or []) if isinstance(row, dict)]
        matches = [row for row in rows if item in str((row.get("result") or {}).get("id", "")) or item in str((row.get("result") or {}).get("name", ""))]
        matches = [row for row in matches if not row.get("out_of_stock")]
        if not matches:
            raise SkillError("村民没有匹配的交易")
        offer = matches[0]
        traded = 0
        try:
            for _ in range(target):
                await self._call("trade", {"operation": "trade", "index": int(offer["index"])}, timeout=10)
                traded += 1
                await asyncio.sleep(0.35)
        finally:
            try:
                await self._call("container", {"operation": "close"}, timeout=5)
            except Exception:
                pass
        return {"skill": "trade_for", "item": item, "traded": traded, "offer": offer}
