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


class ProductionMixin:
    async def _recipe(self, item):
        if item in self._recipes:
            return self._recipes[item]
        data = await self._call("knowledge", {"query": item, "kind": "recipes", "limit": 8}, timeout=20)
        chosen = None
        for row in data.get("results") or []:
            output = row.get("output") or {}
            if str(output.get("id") or "") == item and row.get("bridge_crafting_supported") is True:
                chosen = row
                if row.get("unlocked") is True:
                    break
        self._recipes[item] = chosen
        return chosen

    async def craft_recipe(self, item, count=1, table_pos=None):
        """L1：材料已具备时，按服务器配方有界合成，不负责采集。"""
        item = str(item or "").strip()
        if not re.fullmatch(r"(?:[a-z0-9_]+:)?[a-z0-9_][a-z0-9_./-]*", item):
            raise SkillError("物品ID无效")
        item = item if ":" in item else "minecraft:" + item
        target = max(1, min(64, int(count)))
        recipe = await self._recipe(item)
        if not recipe:
            raise SkillError(f"没有可执行的服务器配方：{item}")
        output = recipe.get("output") or {}
        produced = max(1, int(output.get("count", 1) or 1))
        batches = max(1, math.ceil(target / produced))
        for group in (recipe.get("ingredients") or []):
            if not isinstance(group, dict):
                continue
            alternatives = [alt for alt in (group.get("alternatives") or []) if isinstance(alt, dict)]
            if not alternatives:
                continue
            enough = False
            for alt in alternatives:
                if await self._count(str(alt.get("id") or "")) >= max(1, int(alt.get("count", 1) or 1)) * batches:
                    enough = True
                    break
            if not enough:
                raise SkillError(f"材料不足，craft_recipe 不负责采集：{item}")
        width = int(recipe.get("width", 2) or 2)
        height = int(recipe.get("height", 2) or 2)
        opened = False
        try:
            if width > 2 or height > 2:
                status = await self.status()
                screen = str(status.get("screen") or "")
                if "Crafting" not in screen and "Workbench" not in screen:
                    if table_pos is None:
                        raise SkillError("该配方需要工作台坐标")
                    tx, ty, tz = _position(table_pos)
                    error = await self.plugin._approach_target(tx, ty, tz, seconds=10, reach=3.5)
                    if error:
                        raise SkillError("接近工作台失败：" + error)
                    data = await self._call("container", {"operation": "open", "x": tx, "y": ty, "z": tz}, timeout=10)
                    if str(data.get("screen") or "none") == "none":
                        raise SkillError("工作台没有确认打开")
                    opened = True
                    await asyncio.sleep(0.4)
            before = await self._count(item)
            crafted = 0
            while crafted < target:
                batch_before = await self._count(item)
                result = await self._run(
                    "craft",
                    {"item": item, "count": min(64, target - crafted)},
                    seconds=15,
                )
                actual = 0
                if isinstance(result, dict):
                    try:
                        actual = int(result.get("produced", 0) or 0)
                    except (TypeError, ValueError):
                        actual = 0
                after = await self._count(item)
                gained = max(actual, after - batch_before)
                if gained <= 0:
                    raise SkillError(f"合成没有确认入包：{item}")
                crafted += gained
                self._mark("craft", "crafting", crafted, target, {"item": item})
            total = await self._count(item)
            return {
                "skill": "craft_recipe",
                "item": item,
                "requested": target,
                "produced": max(crafted, total - before),
            }
        finally:
            if opened:
                try:
                    await self._call("container", {"operation": "close"}, timeout=5)
                except Exception:
                    pass

    async def _count(self, item):
        return self.item_count(await self.status(), item)

    async def _crafting_screen(self):
        status = await self.status()
        screen = str(status.get("screen") or "")
        return "Crafting" in screen or "Workbench" in screen

    async def _ensure_table(self, table_pos=None):
        if await self._crafting_screen():
            return False
        candidates = []
        seen = set()

        def add_candidate(row):
            if not isinstance(row, dict):
                return
            try:
                key = (int(row.get("x", 0)), int(row.get("y", 0)), int(row.get("z", 0)))
            except (TypeError, ValueError):
                return
            if key in seen:
                return
            seen.add(key)
            candidates.append({
                "x": key[0], "y": key[1], "z": key[2],
                "distance": _num(row.get("distance"), 999),
                "stands": row.get("stands") or [],
            })

        if table_pos is not None:
            try:
                tx, ty, tz = _position(table_pos)
                add_candidate({"x": tx, "y": ty, "z": tz, "distance": 0})
            except (TypeError, ValueError):
                pass
        scan = await self._call("scan", {"resource": "logs", "radius": 12}, timeout=15)
        tables = [row for row in (scan.get("workbenches") or []) if isinstance(row, dict)]
        for table in sorted(tables, key=lambda row: _num(row.get("distance"), 999))[:4]:
            add_candidate(table)
        if not tables:
            data = await self._call(
                "scan_blocks",
                {"radius": 16, "limit": 8, "block": "minecraft:crafting_table"},
                timeout=15,
            )
            for row in (data.get("blocks") or []):
                add_candidate(row)

        for table in candidates:
            stands = [row for row in (table.get("stands") or []) if isinstance(row, dict)]
            error = None
            if stands:
                stand = min(stands, key=lambda row: _num(row.get("distance"), 999))
                error = await self.plugin._approach_target(
                    stand["x"], stand.get("feet_y", stand["y"]), stand["z"],
                    seconds=20, reach=1.5,
                )
            if error or not stands:
                error = await self.plugin._approach_target(
                    table["x"], table["y"], table["z"], seconds=20, reach=3.5,
                )
            if error:
                continue
            opened = await self.plugin._call(
                "container",
                {"operation": "open", "x": table["x"], "y": table["y"], "z": table["z"]},
                timeout=10,
            )
            if self._err(opened):
                continue
            await asyncio.sleep(0.4)
            if await self._crafting_screen():
                return True
            try:
                await self.plugin._call("container", {"operation": "close"}, timeout=5)
            except Exception:
                pass
        if await self._count("minecraft:crafting_table") < 1:
            try:
                await self.ensure_item("minecraft:crafting_table", 1, depth=1)
            except SkillError as exc:
                raise SkillError(f"附近没有可打开的工作台，制作工作台也失败：{exc}") from exc
        scan = await self._call("scan", {"resource": "logs", "radius": 12}, timeout=15)
        placements = [row for row in (scan.get("placements") or []) if isinstance(row, dict)]
        placements = await self._without_player_body(placements)
        if not placements:
            ground = [row for row in (scan.get("ground") or []) if isinstance(row, dict) and 4 <= _num(row.get("distance"), 0) <= 12]
            if ground:
                point = min(ground, key=lambda row: _num(row.get("distance"), 999))
                error = await self.plugin._approach_target(point["x"], point.get("feet_y", point["y"]), point["z"], seconds=10, reach=1.5)
                if not error:
                    scan = await self._call("scan", {"resource": "logs", "radius": 12}, timeout=15)
                    placements = [row for row in (scan.get("placements") or []) if isinstance(row, dict)]
                    placements = await self._without_player_body(placements)
        if not placements:
            raise SkillError("附近没有可放置工作台的位置")
        site = min(placements, key=lambda row: _num(row.get("distance"), 999))
        error = await self.plugin._approach_target(
            int(site["x"]), site.get("feet_y", int(site["y"])), int(site["z"]), seconds=10, reach=3.5
        )
        if error:
            raise SkillError("接近工作台放置点失败：" + error)
        await self._run("workbench", {"operation": "place", "x": int(site["x"]), "y": int(site["y"]), "z": int(site["z"])}, seconds=12)
        opened = await self.plugin._call("container", {"operation": "open", "x": int(site["x"]), "y": int(site["y"]), "z": int(site["z"])}, timeout=10)
        err = self._err(opened)
        if err:
            raise SkillError("工作台已放置但打开失败：" + err)
        await asyncio.sleep(0.4)
        if not await self._crafting_screen():
            raise SkillError("工作台已放置但没有打开合成界面")
        return True

    async def ensure_item(self, item, need, depth=0, table_pos=None):
        need = max(0, int(need))
        if need <= 0:
            return 0
        have = await self._count(item)
        if have >= need:
            return have
        if depth > 4:
            raise SkillError(f"材料递归过深：{item}")
        if item.endswith(LOG_SUFFIX) or item == "logs":
            await self.gather("logs", need - have, allow_night=False)
            return await self._count(item)
        if item in PLANK_IDS or item.endswith("_planks"):
            total = 0
            for plank in PLANK_IDS:
                total += await self._count(plank)
            if total >= need:
                return total
            status = await self.status()
            logs = [str(row.get("id")) for row in self.inventory_items(status) if str(row.get("id", "")).endswith(LOG_SUFFIX)]
            if not logs:
                await self.gather("logs", max(1, need - total), allow_night=False)
                status = await self.status()
                logs = [str(row.get("id")) for row in self.inventory_items(status) if str(row.get("id", "")).endswith(LOG_SUFFIX)]
            if not logs:
                raise SkillError("没有原木，无法制作木板")
            log_id = logs[0]
            plank_id = log_id[:-4] + "_planks"
            if await self._count(log_id) <= 0:
                await self.gather("logs", 1, allow_night=False)
            await self.craft_recipe(plank_id, min(64, need - total))
            total = 0
            for plank in PLANK_IDS:
                total += await self._count(plank)
            return total
        if item in STONE_IDS or item == "stone":
            await self.gather("stone", need - have)
            return await self._count(item)
        if item in ORE_ITEM_IDS["iron"] or item == "iron":
            await self.gather("iron", need - have)
            return await self._count(item)
        if item == "minecraft:coal":
            await self.gather("coal", need - have)
            return await self._count(item)
        if item == "minecraft:wheat":
            await self.gather("wheat", need - have)
            return await self._count(item)
        recipe = await self._recipe(item)
        if not recipe:
            raise SkillError(f"没有可执行的服务器配方：{item}")
        output = recipe.get("output") or {}
        produced = max(1, int(output.get("count", 1) or 1))
        batches = max(1, math.ceil((need - have) / produced))
        groups = [group for group in (recipe.get("ingredients") or []) if isinstance(group, dict)]
        for group in groups:
            alternatives = [alt for alt in (group.get("alternatives") or []) if isinstance(alt, dict)]
            if not alternatives:
                continue
            chosen = None
            for alt in alternatives:
                if await self._count(str(alt.get("id") or "")) >= max(1, int(alt.get("count", 1) or 1)) * batches:
                    chosen = alt
                    break
            if chosen is None:
                chosen = alternatives[0]
            await self.ensure_item(
                str(chosen.get("id") or ""),
                max(1, int(chosen.get("count", 1) or 1)) * batches,
                depth + 1,
                table_pos=table_pos,
            )
        width = int(recipe.get("width", 2) or 2)
        height = int(recipe.get("height", 2) or 2)
        if width > 2 or height > 2:
            await self._ensure_table(table_pos)
        remaining = need - have
        crafted = 0
        while remaining > 0:
            outcome = await self.craft_recipe(item, min(64, remaining))
            produced = 0
            if isinstance(outcome, dict):
                try:
                    produced = int(outcome.get("produced", 0) or 0)
                except (TypeError, ValueError):
                    produced = 0
            if produced <= 0:
                after = await self._count(item)
                produced = max(0, after - (have + crafted))
            if produced <= 0:
                raise SkillError(f"合成没有确认入包：{item}")
            crafted += produced
            remaining = need - have - crafted
        return max(await self._count(item), have + crafted)

    async def craft(self, item, count=1, table_pos=None):
        item = str(item or "").strip()
        if not re.fullmatch(r"(?:[a-z0-9_]+:)?[a-z0-9_][a-z0-9_./-]*", item):
            raise SkillError("物品ID无效")
        item = item if ":" in item else "minecraft:" + item
        target = max(1, min(64, int(count)))
        if table_pos is not None:
            try:
                tx, ty, tz = _position(table_pos)
            except (TypeError, ValueError) as exc:
                raise SkillError("工作台坐标无效") from exc
            table_pos = [int(tx), int(ty), int(tz)]
        before = await self._count(item)
        try:
            ensured = await self.ensure_item(item, before + target, depth=0, table_pos=table_pos)
            after = await self._count(item)
            return {
                "skill": "craft",
                "item": item,
                "requested": target,
                "produced": max(0, max(after, int(ensured or 0)) - before),
            }
        finally:
            await self._ensure_no_screen()

    # ------------------------------------------------------------------ smelt
    async def _find_furnace(self, radius=12):
        scan = await self._call("scan", {"resource": "logs", "radius": radius}, timeout=15)
        stations = [
            row for row in (scan.get("stations") or [])
            if isinstance(row, dict) and str(row.get("item") or "") == "minecraft:furnace"
        ]
        if stations:
            return min(stations, key=lambda row: _num(row.get("distance"), 999))
        data = await self._call("scan_blocks", {"radius": radius, "limit": 8, "block": "minecraft:furnace"}, timeout=15)
        rows = [row for row in (data.get("blocks") or []) if isinstance(row, dict)]
        if rows:
            return min(rows, key=lambda row: _num(row.get("distance"), 999))
        return None

    async def _without_player_body(self, rows):
        status = await self.status()
        pos = status.get("position") or {}
        try:
            feet = (math.floor(float(pos.get("x"))), math.floor(float(pos.get("y"))), math.floor(float(pos.get("z"))))
        except (TypeError, ValueError):
            return rows
        body = {feet, (feet[0], feet[1] + 1, feet[2])}
        return [
            row for row in rows
            if (int(row.get("x", 0)), int(row.get("y", 0)), int(row.get("z", 0))) not in body
        ]

    async def _place_station(self, item):
        scan = await self._call("scan", {"resource": "logs", "radius": 12}, timeout=15)
        placements = [row for row in (scan.get("placements") or []) if isinstance(row, dict)]
        placements = await self._without_player_body(placements)
        if not placements:
            ground = [row for row in (scan.get("ground") or []) if isinstance(row, dict) and 4 <= _num(row.get("distance"), 0) <= 12]
            if ground:
                point = min(ground, key=lambda row: _num(row.get("distance"), 999))
                error = await self.plugin._approach_target(point["x"], point.get("feet_y", point["y"]), point["z"], seconds=10, reach=1.5)
                if not error:
                    scan = await self._call("scan", {"resource": "logs", "radius": 12}, timeout=15)
                    placements = [row for row in (scan.get("placements") or []) if isinstance(row, dict)]
                    placements = await self._without_player_body(placements)
        if not placements:
            raise SkillError(f"附近没有可放置{item}的位置")
        site = min(placements, key=lambda row: _num(row.get("distance"), 999))
        error = await self.plugin._approach_target(
            int(site["x"]), site.get("feet_y", int(site["y"])), int(site["z"]), seconds=10, reach=3.5
        )
        if error:
            raise SkillError(f"接近{item}放置点失败：" + error)
        placed = False
        try:
            await self._run(
                "station",
                {"operation": "place", "x": int(site["x"]), "y": int(site["y"]), "z": int(site["z"]), "item": item},
                seconds=12,
            )
            placed = True
        except SkillError:
            context = await self._call("block_context", {"x": int(site["x"]), "y": int(site["y"]), "z": int(site["z"])}, timeout=10)
            if item in str(context.get("block") or ""):
                placed = True
        if not placed:
            await self.plugin._run_operation(
                "place_at",
                {"x": int(site["x"]), "y": int(site["y"]), "z": int(site["z"]), "item": item},
                seconds=10,
            )
        context = await self._call("block_context", {"x": int(site["x"]), "y": int(site["y"]), "z": int(site["z"])}, timeout=10)
        if item not in str(context.get("block") or ""):
            raise SkillError(f"{item}放置后没有确认出现")
        return {"x": int(site["x"]), "y": int(site["y"]), "z": int(site["z"]), "distance": _num(site.get("distance"), 0)}

    async def smelt(self, item, count=1, collect_only=False):
        item = str(item or "").strip()
        if not re.fullmatch(r"[a-z0-9_]+:[a-z0-9_./-]+", item):
            raise SkillError("物品ID无效")
        target = max(1, min(32, int(count)))
        station = await self._find_furnace(12)
        if station is None:
            if collect_only:
                raise SkillError("附近没有普通熔炉")
            if await self._count("minecraft:furnace") < 1:
                await self.craft("minecraft:furnace", 1)
            if await self._count("minecraft:furnace") < 1:
                raise SkillError("没有熔炉，也没有成功合成")
            station = await self._place_station("minecraft:furnace")
        result = await self.smelt_batch(station, item, target, collect_only=collect_only)
        result["skill"] = "smelt"
        result["furnace"] = station
        return result

    async def _container_take(self, position, item, limit):
        """Take at most ``limit`` items from a container into the inventory."""
        limit = max(0, int(limit))
        if limit <= 0:
            return 0
        before = self.item_count(await self.status(), item)
        opened = False
        try:
            await self._call("container", {"operation": "open", **position}, timeout=10)
            opened = True
            await self._take_open_container(item, limit)
        finally:
            if opened:
                try:
                    await self._call("container", {"operation": "close"}, timeout=5)
                except Exception:
                    pass
        after = self.item_count(await self.status(), item)
        return max(0, after - before)

    async def _take_open_container(self, item, limit):
        limit = max(0, int(limit))
        for _ in range(6):
            if limit <= 0:
                break
            result = await self._call(
                "move_item",
                {"item": item, "count": min(64, limit), "direction": "from_container"},
                timeout=10,
            )
            moved = max(0, int(result.get("moved", 0) or 0))
            if moved <= 0:
                break
            limit -= moved

    async def _collect_nearby_smelt_output(self, item, limit, furnace_pos):
        limit = max(0, int(limit))
        if limit <= 0:
            return 0
        scan = await self._call(
            "scan_blocks",
            {"radius": 8, "limit": 8, "block": "minecraft:barrel,minecraft:chest,minecraft:hopper"},
            timeout=15,
        )
        total = 0
        fx, fy, fz = int(furnace_pos.get("x", 0)), int(furnace_pos.get("y", 0)), int(furnace_pos.get("z", 0))
        for row in scan.get("blocks") or []:
            if not isinstance(row, dict):
                continue
            pos = {"x": int(row.get("x", 0)), "y": int(row.get("y", 0)), "z": int(row.get("z", 0))}
            if pos == furnace_pos:
                continue
            # Only recover from containers immediately around the furnace
            # (hopper/barrel output).  Never drain unrelated storage chests.
            if abs(pos["x"] - fx) > 3 or abs(pos["y"] - fy) > 3 or abs(pos["z"] - fz) > 3:
                continue
            try:
                total += await self._container_take(pos, item, limit - total)
            except SkillError:
                continue
            if total >= limit:
                break
        return total

    async def smelt_batch(self, furnace_pos, item, count=1, collect_only=False):
        """L1：已知熔炉坐标，有界烧炼并核对产物。"""
        item = str(item or "").strip()
        if not re.fullmatch(r"[a-z0-9_]+:[a-z0-9_./-]+", item):
            raise SkillError("物品ID无效")
        target = max(1, min(32, int(count)))
        x, y, z = _position(furnace_pos)
        context = await self._call("block_context", {"x": x, "y": y, "z": z}, timeout=10)
        block = str(context.get("block") or "")
        if "furnace" not in block and "smoker" not in block:
            raise SkillError("目标方块不是熔炉：" + block)
        stands = [row for row in (context.get("stands") or []) if isinstance(row, dict)]
        # The player may already be within interaction reach.  Do not force a
        # pathfind to a stand that can be behind basement walls; the client
        # smelt action will open the furnace itself.
        status = await self.status()
        position = status.get("position") if isinstance(status.get("position"), dict) else {}
        vertical = 0.0
        if stands and position:
            stand = min(stands, key=lambda row: _num(row.get("distance"), 999))
            try:
                vertical = abs(float(position.get("y", 0)) - float(stand.get("feet_y", stand.get("y", 0))))
            except (TypeError, ValueError):
                vertical = 0.0
            if _num(context.get("distance"), 999) > 4.45 or vertical > 1.2:
                error = await self.plugin._approach_target(
                    stand["x"], stand.get("feet_y", stand["y"]), stand["z"],
                    seconds=15, reach=1.5, allow_break=vertical > 1.2,
                )
                if error:
                    raise SkillError("接近熔炉失败：" + error)
        before = await self._count(item)
        furnace_pos = {"x": x, "y": y, "z": z}
        try:
            await self._call("look_at", {"x": x, "y": y, "z": z}, timeout=5)
        except Exception:
            pass
        occupied = False
        read_ok = False
        for _attempt in range(4):
            opened = False
            try:
                await self._call("container", {"operation": "open", **furnace_pos}, timeout=10)
                opened = True
                read = await self._call("container", {"operation": "read"}, timeout=10)
                slots = {
                    int(row.get("slot", -1)): row
                    for row in (read.get("slots") or [])
                    if isinstance(row, dict) and not row.get("player_inventory")
                }
                output = slots.get(2) or {}
                input_slot = slots.get(0) or {}
                fuel_slot = slots.get(1) or {}
                occupied = any(
                    str(row.get("id") or "") not in {"", "minecraft:air"}
                    for row in (input_slot, fuel_slot, output)
                )
                if output.get("id") == item and int(output.get("count", 0) or 0) > 0:
                    await self._take_open_container(item, target)
                read_ok = True
                break
            except SkillError:
                await asyncio.sleep(0.35)
            finally:
                if opened:
                    try:
                        await self._call("container", {"operation": "close"}, timeout=5)
                    except Exception:
                        pass
        if not read_ok:
            occupied = False

        if occupied:
            existing = max(0, await self._count(item) - before)
            existing += await self._collect_nearby_smelt_output(item, target - existing, furnace_pos)
            after = await self._count(item)
            produced = max(0, after - before)
            if produced > 0:
                return {
                    "skill": "smelt_batch", "item": item, "requested": target,
                    "produced": produced, "furnace": furnace_pos,
                    "note": "collected existing furnace/hopper output",
                }
            raise SkillError(
                "熔炉里有未取出的材料或产物，请先清空或稍后再试: "
                + repr({k: (v.get("id"), v.get("count")) for k, v in slots.items() if v.get("id")})
            )

        remaining = target
        error = None
        try:
            while remaining > 0:
                batch = min(8, remaining)
                await self._run(
                    "smelt",
                    {"x": x, "y": y, "z": z, "item": item, "count": batch, "collect_only": bool(collect_only)},
                    seconds=170,
                )
                remaining -= batch
                produced = max(0, await self._count(item) - before)
                self._mark("smelt", "smelting", produced, target, {"item": item})
                if collect_only:
                    break
        except SkillError as exc:
            error = exc

        produced = max(0, await self._count(item) - before)
        if produced < target:
            produced += await self._collect_nearby_smelt_output(item, target - produced, furnace_pos)
        after = await self._count(item)
        produced = max(0, after - before)
        if error is not None and produced < target:
            raise error
        if produced < target:
            raise SkillError(f"烧炼产物不足：{produced}/{target}")
        return {
            "skill": "smelt_batch", "item": item, "requested": target,
            "produced": produced, "furnace": furnace_pos,
        }

    # --------------------------------------------------------------- transfer
    async def _resolve_item_name(self, item, direction):
        item = str(item or "").strip()
        if ":" in item:
            return item
        if not item:
            return item
        if direction == "to_container":
            status = await self.status()
            rows = self.inventory_items(status)
        else:
            data = await self._call("container", {"operation": "read"}, timeout=10)
            rows = [row for row in (data.get("slots") or []) if isinstance(row, dict) and not row.get("player_inventory")]
        matches = {str(row.get("id")): row for row in rows if row.get("id") and item in str(row.get("name") or "")}
        if len(matches) == 1:
            return next(iter(matches))
        if len(matches) > 1:
            raise SkillError("有多个匹配物品，请指定物品ID")
        raise SkillError("背包/容器里没有匹配物品：" + item)

    async def _open_transfer_container(self, x, y, z):
        opened = False
        if x or y or z:
            try:
                await self._call("container", {"operation": "open", "x": int(x), "y": int(y), "z": int(z)}, timeout=10)
            except Exception:
                error = await self.plugin._approach_target(int(x), int(y), int(z), seconds=10, reach=3.5)
                if error:
                    raise SkillError("打开容器失败：" + error)
                await self._call("container", {"operation": "open", "x": int(x), "y": int(y), "z": int(z)}, timeout=10)
            opened = True
            await asyncio.sleep(0.5)
            try:
                self.plugin._register_container(int(x), int(y), int(z))
            except Exception:
                pass
        return opened

    async def _move_one(self, identity, target, direction):
        before = await self._count(identity)
        moved = 0
        for _ in range(6):
            result = await self._call("move_item", {"item": identity, "count": min(64, target - moved), "direction": direction}, timeout=10)
            step = max(0, int(result.get("moved", 0) or 0))
            moved += step
            if step <= 0 or moved >= target:
                break
        after = await self._count(identity)
        return {"item": identity, "requested": target, "moved": moved, "inventory_delta": after - before}

    async def transfer(self, item="", count=1, direction="to_container", x=0, y=0, z=0, close=True, items=None):
        direction = str(direction or "to_container").strip().lower()
        if direction not in {"to_container", "from_container"}:
            raise SkillError("direction 只能是 to_container 或 from_container")
        if items is not None and str(items).strip():
            result = await self.transfer_many(items, direction=direction, x=x, y=y, z=z, close=close)
            result["skill"] = "transfer"
            return result
        target = max(1, min(256, int(count)))
        opened = await self._open_transfer_container(x, y, z)
        try:
            identity = await self._resolve_item_name(item, direction)
            if not identity:
                raise SkillError("请指定物品ID或名称")
            result = await self._move_one(identity, target, direction)
            self._mark("transfer", "transferring", result.get("moved", 0), target, {"item": identity, "direction": direction})
            result.update({"skill": "transfer", "direction": direction})
            return result
        finally:
            if opened and close:
                try:
                    await self._call("container", {"operation": "close"}, timeout=5)
                except Exception:
                    pass

    async def transfer_many(self, items, direction="to_container", x=0, y=0, z=0, close=True):
        direction = str(direction or "to_container").strip().lower()
        if direction not in {"to_container", "from_container"}:
            raise SkillError("direction 只能是 to_container 或 from_container")
        moves = parse_transfer_items(items)
        if not moves:
            raise SkillError("批量搬运清单为空或格式不正确")
        if len(moves) > 32:
            raise SkillError("单次批量搬运最多32种物品")
        opened = await self._open_transfer_container(x, y, z)
        results = []
        total_requested = 0
        total_moved = 0
        moved_by_item = {}
        try:
            for move in moves:
                requested = int(move["count"])
                total_requested += requested
                name = str(move["item"])
                try:
                    identity = await self._resolve_item_name(name, direction)
                    result = await self._move_one(identity, requested, direction)
                    result["ok"] = result["moved"] >= requested
                    results.append(result)
                    total_moved += result["moved"]
                    moved_by_item[identity] = moved_by_item.get(identity, 0) + result["moved"]
                    self._mark("transfer_many", "transferring", total_moved, total_requested,
                               {"moves": moved_by_item, "direction": direction})
                except Exception as exc:
                    results.append({"item": name, "requested": requested, "moved": 0, "ok": False, "error": str(exc)})
            return {
                "skill": "transfer_many",
                "direction": direction,
                "requested": total_requested,
                "moved": total_moved,
                "items": results,
                "opened_once": opened,
            }
        finally:
            if opened and close:
                try:
                    await self._call("container", {"operation": "close"}, timeout=5)
                except Exception:
                    pass

    async def transfer_item(self, container_pos, item, count=1, direction="to_container", close=True):
        """L1：已知容器坐标，有界搬运并核对背包变化。"""
        x, y, z = _position(container_pos)
        result = await self.transfer(item, count, direction=direction, x=x, y=y, z=z, close=close)
        result["skill"] = "transfer_item"
        return result

    # ------------------------------------------------------------------- build
