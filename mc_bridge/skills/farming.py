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


class FarmingMixin:
    @staticmethod
    def _crop_name(value):
        value = str(value or "").strip().lower()
        aliases = {
            "carrot": "carrots", "potato": "potatoes", "beetroot": "beetroots",
            "carrots": "carrots", "potatoes": "potatoes", "beetroots": "beetroots",
            "wheat": "wheat",
        }
        return aliases.get(value, value)

    @staticmethod
    def _block_id(value):
        value = str(value or "").strip()
        return value if ":" in value else ("minecraft:" + value if value else "")

    @staticmethod
    def _crop_age(state):
        match = re.search(r"age=(\d+)", str(state or ""))
        try:
            return int(match.group(1)) if match else None
        except (TypeError, ValueError):
            return None

    async def _plant_crop(self, x, y, z, seed):
        if not seed or await self._count(seed) <= 0:
            self._last_plant_error = "没有可用种子"
            return False
        try:
            await self._run("plant", {"x": int(x), "y": int(y), "z": int(z), "item": seed}, seconds=6)
            self._last_plant_error = ""
            return True
        except Exception as exc:
            self._last_plant_error = str(exc).strip() or type(exc).__name__
            # The server may confirm the crop just after the action timeout.
            try:
                above = await self._call(
                    "inspect_block", {"x": int(x), "y": int(y) + 1, "z": int(z)}, timeout=6,
                )
                if CROP_BLOCKS.get(self._block_id(above.get("block"))):
                    self._last_plant_error = ""
                    return True
            except Exception:
                pass
            return False

    async def farm(self, operation="harvest", radius=8, count=8, replant=True, crop="auto"):
        operation = str(operation or "harvest").strip().lower()
        if operation not in {"harvest", "plant", "both"}:
            raise SkillError("operation 只能是 harvest/plant/both")
        crop = self._crop_name(crop or "auto")
        if crop not in set(CROP_SPECS) | {"auto"}:
            raise SkillError("crop 只能是 auto/wheat/carrots/potatoes/beetroots")
        radius = max(2, min(12, int(radius)))
        target = max(1, min(32, int(count)))
        harvested = {}
        planted = {}
        total_work = target * (2 if operation == "both" else 1)
        self._mark("farm", "farming", 0, total_work, {"operation": operation, "crop": crop})

        if operation in {"harvest", "both"}:
            query = CROP_SPECS[crop]["block"] if crop != "auto" else ",".join(
                spec["block"] for spec in CROP_SPECS.values()
            )
            data = await self._call("scan_blocks", {"radius": radius, "limit": 64, "block": query}, timeout=10)
            crops = [row for row in (data.get("blocks") or []) if isinstance(row, dict)]
            for row in crops:
                if sum(harvested.values()) >= target:
                    break
                block = self._block_id(row.get("block"))
                crop_name = CROP_BLOCKS.get(block)
                if not crop_name or (crop != "auto" and crop_name != crop):
                    continue
                spec = CROP_SPECS[crop_name]
                try:
                    inspect = await self._call(
                        "inspect_block",
                        {"x": int(row["x"]), "y": int(row["y"]), "z": int(row["z"])},
                        timeout=10,
                    )
                except Exception:
                    continue
                age = self._crop_age(inspect.get("state"))
                if age is None or age < int(spec["age"]):
                    continue
                try:
                    await self._run("mine_at", {"x": int(row["x"]), "y": int(row["y"]), "z": int(row["z"])}, seconds=10)
                    await self._pickup_near(int(row["x"]), int(row["y"]), int(row["z"]), spec["item"])
                    harvested[crop_name] = harvested.get(crop_name, 0) + 1
                    if replant:
                        used = await self._plant_crop(
                            int(row["x"]), int(row["y"]) - 1, int(row["z"]), spec["seed"],
                        )
                        if used:
                            planted[crop_name] = planted.get(crop_name, 0) + 1
                    self._mark(
                        "farm", "farming", sum(harvested.values()) + sum(planted.values()),
                        total_work, {"operation": operation, "crop": crop,
                                     "harvested": dict(harvested), "planted": dict(planted)},
                    )
                except Exception:
                    continue

        if operation in {"plant", "both"} and sum(planted.values()) < target:
            data = await self._call("scan_blocks", {"radius": radius, "limit": 64, "block": "minecraft:farmland"}, timeout=10)
            farms = [row for row in (data.get("blocks") or []) if isinstance(row, dict)]
            planted_cells = set()
            for row in farms:
                if sum(planted.values()) >= target:
                    break
                try:
                    x, y, z = int(row["x"]), int(row["y"]), int(row["z"])
                except (KeyError, TypeError, ValueError):
                    continue
                if (x, y, z) in planted_cells:
                    continue
                try:
                    above = await self._call("inspect_block", {"x": x, "y": y + 1, "z": z}, timeout=10)
                except Exception:
                    continue
                if not (above.get("air") or above.get("replaceable")):
                    continue
                if crop == "auto":
                    crop_name = None
                    for candidate in ("wheat", "carrots", "potatoes", "beetroots"):
                        if await self._count(CROP_SPECS[candidate]["seed"]) > 0:
                            crop_name = candidate
                            break
                else:
                    crop_name = crop
                if not crop_name:
                    break
                used = await self._plant_crop(x, y, z, CROP_SPECS[crop_name]["seed"])
                if used:
                    planted_cells.add((x, y, z))
                    planted[crop_name] = planted.get(crop_name, 0) + 1
                    self._mark(
                        "farm", "farming", sum(harvested.values()) + sum(planted.values()),
                        total_work, {"operation": operation, "crop": crop,
                                     "harvested": dict(harvested), "planted": dict(planted)},
                    )
        return {
            "skill": "farm",
            "operation": operation,
            "crop": crop,
            "requested": target,
            "harvested": sum(harvested.values()),
            "planted": sum(planted.values()),
            "collected": sum(harvested.values()) if operation in {"harvest", "both"} else sum(planted.values()),
            "partial": (sum(harvested.values()) if operation in {"harvest", "both"} else sum(planted.values())) < target,
            "crops": {"harvested": harvested, "planted": planted},
        }

    # ---------------------------------------------------------------- farm plot
    def _farm_memory(self):
        getter = getattr(self.plugin, "_farm_memory_for", None)
        return getter() if callable(getter) else None

    @staticmethod
    def _farm_shape(cells):
        if not cells:
            return "empty"
        xs = [int(cell["x"]) for cell in cells]
        zs = [int(cell["z"]) for cell in cells]
        area = (max(xs) - min(xs) + 1) * (max(zs) - min(zs) + 1)
        return "irregular" if len(cells) < area else "rectangle"

    @staticmethod
    def _group_farm_cells(cells):
        remaining = {(int(cell["x"]), int(cell["y"]), int(cell["z"])) for cell in cells}
        groups = []
        while remaining:
            seed = remaining.pop()
            group = [seed]
            queue = [seed]
            while queue and len(group) < MAX_CELLS:
                x, y, z = queue.pop()
                for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    key = (x + dx, y, z + dz)
                    if key in remaining:
                        remaining.remove(key)
                        group.append(key)
                        queue.append(key)
            groups.append(group)
        return groups

    @staticmethod
    def _farm_water_near(cells, water_rows):
        if not cells:
            return []
        xs = [int(cell["x"]) for cell in cells]
        zs = [int(cell["z"]) for cell in cells]
        ys = [int(cell["y"]) for cell in cells]
        min_x, max_x = min(xs) - 4, max(xs) + 4
        min_z, max_z = min(zs) - 4, max(zs) + 4
        min_y, max_y = min(ys) - 1, max(ys) + 1
        found = []
        for row in water_rows:
            try:
                x, y, z = int(row["x"]), int(row["y"]), int(row["z"])
            except (KeyError, TypeError, ValueError):
                continue
            if min_x <= x <= max_x and min_z <= z <= max_z and min_y <= y <= max_y:
                found.append({"x": x, "y": y, "z": z})
            if len(found) >= 64:
                break
        return found

    @staticmethod
    def _farm_stand_goal(row, default_y):
        try:
            return int(math.ceil(float(row.get("feet_y", row.get("y", default_y)))))
        except (TypeError, ValueError):
            return int(default_y)

    async def _approach_farm_cell(self, x, y, z, avoid_column=False):
        try:
            context = await self._call("block_context", {"x": int(x), "y": int(y), "z": int(z)}, timeout=8)
        except Exception:
            return False
        stands = [row for row in (context.get("stands") or []) if isinstance(row, dict)]
        if avoid_column:
            stands = [
                row for row in stands
                if (int(row.get("x", 0)), int(row.get("z", 0))) != (int(x), int(z))
            ]
            stands.sort(key=lambda row: (
                0 if abs(_num(row.get("feet_y"), y) - float(y)) <= 0.6 else 1,
                _num(row.get("distance"), 999),
            ))
            for stand in stands[:3]:
                error = await self.plugin._approach_target(
                    stand["x"], self._farm_stand_goal(stand, y), stand["z"],
                    seconds=10, reach=1.2,
                )
                if error:
                    continue
                try:
                    await self._run(
                        "align",
                        {"x": stand["x"], "y": stand.get("feet_y", stand.get("y", y)),
                         "z": stand["z"]},
                        seconds=4,
                    )
                except Exception:
                    pass
                status = await self.status()
                position = status.get("position") or {}
                try:
                    feet = (math.floor(float(position["x"])), math.floor(float(position["z"])))
                except (KeyError, TypeError, ValueError):
                    continue
                if feet == (int(x), int(z)):
                    continue
                if await self._farm_mine_ready(x, y, z, require_visible=True):
                    return True
            error = await self.plugin._approach_target(int(x), int(y), int(z), seconds=10, reach=2.5)
            if error:
                return False
            return await self._farm_mine_ready(x, y, z)
        if stands:
            stands.sort(key=lambda row: _num(row.get("distance"), 999))
            stand = stands[0]
            error = await self.plugin._approach_target(
                stand["x"], stand.get("feet_y", stand.get("y", y)), stand["z"],
                seconds=10, reach=1.5,
            )
            if error:
                return False
        elif _num(context.get("distance"), 0) <= 4.0:
            return True
        else:
            error = await self.plugin._approach_target(int(x), int(y), int(z), seconds=10, reach=2.5)
            if error:
                return False
        return True

    async def _farm_cell_state(self, cell):
        try:
            above = await self._call(
                "inspect_block",
                {"x": int(cell["x"]), "y": int(cell["y"]) + 1, "z": int(cell["z"])},
                timeout=10,
            )
        except Exception:
            return {"empty": False, "crop": "", "age": None, "mature": False}
        block = self._block_id(above.get("block"))
        if not block:
            match = re.search(r"Block\{([^}\[]+)", str(above.get("state") or ""))
            if match:
                block = self._block_id(match.group(1))
        crop = CROP_BLOCKS.get(block)
        if not crop:
            return {"empty": bool(above.get("air") or above.get("replaceable")), "crop": "", "age": None, "mature": False}
        age = self._crop_age(above.get("state"))
        mature = age is not None and age >= int(CROP_SPECS[crop]["age"])
        return {"empty": False, "crop": crop, "age": age, "mature": mature}

    async def _farm_mine_ready(self, x, y, z, reach=2.6, require_visible=False):
        """True when the block can be mined from the current feet position."""
        status = await self.status()
        position = status.get("position") or {}
        try:
            px, py, pz = float(position["x"]), float(position["y"]), float(position["z"])
        except (KeyError, TypeError, ValueError):
            return False
        if (math.floor(px), math.floor(pz)) == (int(x), int(z)):
            return False
        eye = (px, py + 1.62, pz)
        center = (float(x) + 0.5, float(y) + 0.5, float(z) + 0.5)
        if math.dist(eye, center) > reach:
            return False
        # Mirror the client's load-bearing rule: reject when the player's
        # bounding box overlaps the block's own column, not only its center.
        overlaps = (
            int(x) < px + 0.45 and int(x) + 1 > px - 0.45
            and int(z) < pz + 0.45 and int(z) + 1 > pz - 0.45
            and int(y) < py + 0.1 and int(y) + 1 > py - 0.25
        )
        if overlaps:
            return False
        if require_visible:
            try:
                probe = await self._call(
                    "inspect_block", {"x": int(x), "y": int(y), "z": int(z)}, timeout=6,
                )
                if probe.get("interactable") is False:
                    return False
            except Exception:
                return False
        return True

    async def _farm_plant_ready(self, x, y, z, reach=4.0):
        """True when the farmland top can be used from the current position."""
        status = await self.status()
        position = status.get("position") or {}
        try:
            px, py, pz = float(position["x"]), float(position["y"]), float(position["z"])
        except (KeyError, TypeError, ValueError):
            return False
        eye = (px, py + 1.62, pz)
        target = (float(x) + 0.5, float(y) + 1.0, float(z) + 0.5)
        if math.dist(eye, target) > reach:
            return False
        crop_y = int(y) + 1
        overlaps = (
            int(x) < px + 0.45 and int(x) + 1 > px - 0.45
            and int(z) < pz + 0.45 and int(z) + 1 > pz - 0.45
            and crop_y < py + 0.1 and crop_y + 1 > py - 0.25
        )
        return not overlaps

    async def _approach_farm_plant(self, x, y, z):
        try:
            context = await self._call("block_context", {"x": int(x), "y": int(y), "z": int(z)}, timeout=8)
        except Exception:
            return False
        stands = [row for row in (context.get("stands") or []) if isinstance(row, dict)]
        stands.sort(key=lambda row: (
            0 if abs(_num(row.get("feet_y"), y + 1) - float(y + 1)) <= 0.6 else 1,
            _num(row.get("distance"), 999),
        ))
        for stand in stands[:3]:
            error = await self.plugin._approach_target(
                stand["x"], self._farm_stand_goal(stand, y), stand["z"],
                seconds=10, reach=1.5,
            )
            if error:
                continue
            try:
                await self._run(
                    "align",
                    {"x": stand["x"], "y": stand.get("feet_y", stand.get("y", y)),
                     "z": stand["z"]},
                    seconds=4,
                )
            except Exception:
                pass
            if await self._farm_plant_ready(x, y, z):
                return True
        error = await self.plugin._approach_target(int(x), int(y), int(z), seconds=10, reach=2.5)
        if error:
            return False
        return await self._farm_plant_ready(x, y, z)

    async def _farm_visit_cells(self, cells, avoid_column=False, plant=False):
        """Move into the plot so cell inspection is within client reach."""
        if not cells:
            return False
        status = await self.status()
        position = status.get("position") or {}
        try:
            px, py, pz = float(position["x"]), float(position["y"]), float(position["z"])
        except (KeyError, TypeError, ValueError):
            px = py = pz = 0.0

        def distance(cell):
            try:
                return (
                    (float(cell["x"]) + 0.5 - px) ** 2
                    + (float(cell["y"]) + 0.5 - py) ** 2
                    + (float(cell["z"]) + 0.5 - pz) ** 2
                ) ** 0.5
            except (KeyError, TypeError, ValueError):
                return 999.0

        ordered = sorted(cells, key=distance)
        if ordered:
            first = ordered[0]
            try:
                fx, fy, fz = int(first["x"]), int(first["y"]), int(first["z"])
                if plant:
                    if await self._farm_plant_ready(fx, fy, fz):
                        return True
                elif await self._farm_mine_ready(fx, fy + 1, fz):
                    return True
            except (KeyError, TypeError, ValueError):
                pass
        for cell in ordered[:3]:
            x, y, z = int(cell["x"]), int(cell["y"]), int(cell["z"])
            if plant:
                if await self._approach_farm_plant(x, y, z):
                    return True
                continue
            target_y = y + 1 if avoid_column else y
            if await self._approach_farm_cell(x, target_y, z, avoid_column=avoid_column):
                return True
        return False

    async def _farm_harvest_cells(self, cells, count, replant=True):
        harvested = {}
        planted = {}
        reasons = []
        target = max(1, min(256, int(count)))
        await self._farm_visit_cells(cells, avoid_column=True)
        for cell in cells:
            if sum(harvested.values()) >= target:
                break
            state = await self._farm_cell_state(cell)
            crop = state.get("crop")
            label = "%s,%s,%s" % (cell.get("x"), cell.get("y"), cell.get("z"))
            if not crop:
                reasons.append(label + " 不是作物")
                continue
            if not state.get("mature"):
                reasons.append(label + " 未成熟")
                continue
            x, y, z = int(cell["x"]), int(cell["y"]), int(cell["z"])
            ready = await self._farm_mine_ready(x, y + 1, z, require_visible=True)
            if not ready:
                await self._approach_farm_cell(x, y + 1, z, avoid_column=True)
                # Navigation may fail in a dense field; harvest from the
                # current tile when it is already within interaction reach.
                ready = await self._farm_mine_ready(x, y + 1, z, require_visible=True)
            if not ready:
                reasons.append(label + " 无法站到旁边")
                continue
            spec = CROP_SPECS[crop]
            try:
                await self._run("mine_at", {"x": x, "y": y + 1, "z": z}, seconds=10)
                await self._pickup_near(x, y + 1, z, spec["item"])
                await self._pickup_near(x, y + 1, z, spec["seed"])
                harvested[crop] = harvested.get(crop, 0) + 1
                if replant:
                    # Replant needs a tighter, face-on position than mining.
                    if not await self._farm_plant_ready(x, y, z):
                        await self._approach_farm_plant(x, y, z)
                    replanted = False
                    for attempt in range(3):
                        if await self._plant_crop(x, y, z, spec["seed"]):
                            planted[crop] = planted.get(crop, 0) + 1
                            replanted = True
                            break
                        await asyncio.sleep(0.25)
                    if not replanted:
                        detail = str(getattr(self, "_last_plant_error", "") or "未确认").strip()
                        reasons.append(label + " 补种未确认：" + detail[:60])
                self._mark(
                    "farm_plot", "harvesting",
                    sum(harvested.values()) + sum(planted.values()),
                    target * 2, {"harvested": dict(harvested), "planted": dict(planted)},
                )
            except Exception as exc:
                detail = str(exc).strip() or type(exc).__name__
                reasons.append(label + " 收割失败：" + detail[:80])
                continue
        return harvested, planted, reasons

    async def _farm_plant_cells(self, cells, crop, count):
        chosen = self._crop_name(crop or "auto")
        planted = {}
        skipped = 0
        reasons = []
        target = max(1, min(256, int(count)))
        await self._farm_visit_cells(cells, plant=True)
        for cell in cells:
            if sum(planted.values()) >= target:
                break
            state = await self._farm_cell_state(cell)
            if not state.get("empty"):
                skipped += 1
                reasons.append("格子已被占用")
                continue
            if chosen == "auto":
                crop_name = None
                for candidate in ("wheat", "carrots", "potatoes", "beetroots"):
                    if await self._count(CROP_SPECS[candidate]["seed"]) > 0:
                        crop_name = candidate
                        break
            else:
                crop_name = chosen
            if not crop_name:
                skipped += 1
                reasons.append("没有可用种子")
                break
            x, y, z = int(cell["x"]), int(cell["y"]), int(cell["z"])
            if not await self._farm_plant_ready(x, y, z):
                if not await self._approach_farm_plant(x, y, z):
                    skipped += 1
                    reasons.append("无法接近格子")
                    continue
            planted_ok = False
            for attempt in range(3):
                if await self._plant_crop(x, y, z, CROP_SPECS[crop_name]["seed"]):
                    planted_ok = True
                    break
                await asyncio.sleep(0.25)
            if planted_ok:
                planted[crop_name] = planted.get(crop_name, 0) + 1
            else:
                skipped += 1
                detail = str(getattr(self, "_last_plant_error", "") or "未确认").strip()
                reasons.append("种植未确认：" + detail[:60])
        return planted, skipped, reasons

    async def _farm_discover(self, radius=12, plot_hint=""):
        status = await self.status()
        cells_by_key = {}
        query = ",".join(spec["block"] for spec in CROP_SPECS.values())
        crops = await self._call(
            "scan_blocks", {"radius": max(2, min(12, int(radius))), "limit": 64, "block": query}, timeout=10,
        )
        for row in (crops.get("blocks") or []):
            if not isinstance(row, dict):
                continue
            block = self._block_id(row.get("block"))
            crop = CROP_BLOCKS.get(block)
            if not crop:
                continue
            try:
                x, y, z = int(row["x"]), int(row["y"]), int(row["z"])
            except (KeyError, TypeError, ValueError):
                continue
            try:
                inspect = await self._call("inspect_block", {"x": x, "y": y, "z": z}, timeout=10)
                age = self._crop_age(inspect.get("state"))
            except Exception:
                age = None
            cells_by_key[(x, y - 1, z)] = {
                "x": x, "y": y - 1, "z": z, "crop": crop, "age": age,
            }
        farms = await self._call(
            "scan_blocks", {"radius": max(2, min(12, int(radius))), "limit": 64, "block": "minecraft:farmland"}, timeout=10,
        )
        for row in (farms.get("blocks") or []):
            if not isinstance(row, dict):
                continue
            try:
                x, y, z = int(row["x"]), int(row["y"]), int(row["z"])
            except (KeyError, TypeError, ValueError):
                continue
            key = (x, y, z)
            if key in cells_by_key:
                continue
            try:
                above = await self._call("inspect_block", {"x": x, "y": y + 1, "z": z}, timeout=10)
            except Exception:
                above = {}
            crop = CROP_BLOCKS.get(self._block_id(above.get("block")), "empty")
            cells_by_key[key] = {"x": x, "y": y, "z": z, "crop": crop or "empty", "age": self._crop_age(above.get("state"))}
        if not cells_by_key:
            return {"plots": [], "cells": 0, "_cells": []}
        water_rows = []
        try:
            water = await self._call(
                "scan_blocks", {"radius": max(2, min(16, int(radius) + 4)), "limit": 32, "block": "minecraft:water"}, timeout=10,
            )
            water_rows = [row for row in (water.get("blocks") or []) if isinstance(row, dict)]
        except Exception:
            pass
        memory = self._farm_memory()
        existing = {str(plot["id"]): plot for plot in (memory.list() if memory else [])}
        existing_by_cells = {
            frozenset((int(cell["x"]), int(cell["y"]), int(cell["z"])) for cell in plot["cells"]): plot
            for plot in existing.values()
        }
        plots = []
        used_ids = set(existing)
        for group in self._group_farm_cells(list(cells_by_key.values())):
            group_cells = [cells_by_key[key] for key in group]
            cell_key = frozenset(group)
            old = existing_by_cells.get(cell_key)
            if old is None:
                def near(candidate_plot):
                    keys = {
                        (int(cell["x"]), int(cell["y"]), int(cell["z"]))
                        for cell in (candidate_plot.get("cells") or [])
                    }
                    for x, y, z in group:
                        for dx, dz in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
                            if (x + dx, y, z + dz) in keys:
                                return True
                    return False

                for candidate in existing.values():
                    if near(candidate):
                        old = candidate
                        merged = {
                            (int(cell["x"]), int(cell["y"]), int(cell["z"])): cell
                            for cell in (candidate.get("cells") or [])
                        }
                        for cell in group_cells:
                            merged[(int(cell["x"]), int(cell["y"]), int(cell["z"]))] = cell
                        group_cells = list(merged.values())[:MAX_CELLS]
                        break
            if old is not None:
                plot_id = old["id"]
                name = old.get("name") or plot_id
            else:
                index = 1
                while f"farm-{index}" in used_ids:
                    index += 1
                plot_id = _text(plot_hint, 32) if plot_hint else f"farm-{index}"
                if plot_id in used_ids:
                    index += 1
                    while f"farm-{index}" in used_ids:
                        index += 1
                    plot_id = f"farm-{index}"
                name = f"菜地{index}"
            used_ids.add(plot_id)
            record = {
                "id": plot_id,
                "name": name,
                "world_key": str(status.get("world_key") or ""),
                "dimension": str(status.get("dimension") or ""),
                "cells": [{"x": cell["x"], "y": cell["y"], "z": cell["z"],
                           "crop": cell.get("crop") or "empty"} for cell in group_cells],
                "water_cells": self._farm_water_near(group_cells, water_rows),
            }
            if memory is not None:
                try:
                    record = memory.upsert(record)
                except ValueError:
                    continue
                existing[record["id"]] = record
                existing_by_cells[frozenset(
                    (int(cell["x"]), int(cell["y"]), int(cell["z"]))
                    for cell in record.get("cells") or []
                )] = record
            plots.append(self._farm_summary(record))
        return {"plots": plots, "cells": len(cells_by_key), "_cells": list(cells_by_key.values())}

    @staticmethod
    def _farm_summary(plot):
        cells = plot.get("cells") or []
        crops = {}
        for cell in cells:
            crop = str(cell.get("crop") or "empty")
            crops[crop] = crops.get(crop, 0) + 1
        return {
            "id": plot.get("id"),
            "name": plot.get("name"),
            "cells": len(cells),
            "shape": plot.get("shape") or "irregular",
            "crops": crops,
            "water_cells": len(plot.get("water_cells") or []),
            "bounds": plot.get("bounds"),
            "updated_at": plot.get("updated_at"),
        }

    async def _farm_status(self, plot_id=""):
        memory = self._farm_memory()
        if plot_id:
            plots = [memory.get(plot_id)] if memory else []
            plots = [plot for plot in plots if plot]
        else:
            if memory is None or not memory.list():
                await self._farm_discover()
            plots = memory.list() if memory else []
        result = []
        for plot in plots:
            counts = {"mature": 0, "immature": 0, "empty": 0}
            crops = {}
            for cell in plot.get("cells") or []:
                state = await self._farm_cell_state(cell)
                if state.get("empty"):
                    counts["empty"] += 1
                elif state.get("mature"):
                    counts["mature"] += 1
                    crop = state.get("crop") or "empty"
                    crops[crop] = crops.get(crop, 0) + 1
                else:
                    counts["immature"] += 1
            summary = self._farm_summary(plot)
            summary.update(counts)
            summary["mature_crops"] = crops
            result.append(summary)
        return result

    async def _farm_till_and_plant(self, cells, crop, dry_run=False):
        chosen = self._crop_name(crop or "auto")
        tilled = planted = 0
        shortages = []
        for cell in cells:
            x, y, z = int(cell["x"]), int(cell["y"]), int(cell["z"])
            try:
                below = await self._call("inspect_block", {"x": x, "y": y, "z": z}, timeout=10)
            except Exception:
                continue
            block = self._block_id(below.get("block"))
            if block not in {"minecraft:farmland", "minecraft:dirt", "minecraft:grass_block", "minecraft:dirt_path"}:
                continue
            if not await self._approach_farm_cell(x, y, z):
                continue
            if block != "minecraft:farmland":
                if dry_run:
                    tilled += 1
                else:
                    try:
                        await self._run("till", {"x": x, "y": y, "z": z}, seconds=6)
                        tilled += 1
                    except Exception as exc:
                        shortages.append(f"翻地 {x},{y},{z}: {exc}")
                        continue
            if dry_run:
                continue
            if chosen == "auto":
                crop_name = None
                for candidate in ("wheat", "carrots", "potatoes", "beetroots"):
                    if await self._count(CROP_SPECS[candidate]["seed"]) > 0:
                        crop_name = candidate
                        break
            else:
                crop_name = chosen
            if not crop_name:
                shortages.append("没有可用种子")
                break
            if await self._plant_crop(x, y, z, CROP_SPECS[crop_name]["seed"]):
                planted += 1
        return tilled, planted, shortages

    def _farm_cells_from_mask(self, mask, origin, crop):
        ox, oy, oz = _position(origin)
        rows = [row for row in re.split(r"[/\n]+", str(mask or "").strip()) if row.strip()]
        cells = []
        water = []
        crop_map = {"1": "wheat", "2": "carrots", "3": "potatoes", "4": "beetroots"}
        chosen = self._crop_name(crop or "auto")
        for dz, row in enumerate(rows):
            for dx, char in enumerate(row.strip()):
                if char in {"#", "X", "x", "1", "2", "3", "4"}:
                    cell_crop = crop_map.get(char)
                    if not cell_crop:
                        cell_crop = chosen if chosen in CROP_SPECS else "empty"
                    cells.append({"x": ox + dx, "y": oy, "z": oz + dz, "crop": cell_crop})
                elif char == "~":
                    water.append({"x": ox + dx, "y": oy, "z": oz + dz})
                if len(cells) > MAX_CELLS:
                    raise SkillError(f"田块最多 {MAX_CELLS} 格")
        return cells, water

    async def farm_plot(self, operation="status", plot="", radius=12, count=32, crop="auto",
                        cells=None, mask="", origin=None, replant=True, dry_run=False):
        operation = str(operation or "status").strip().lower()
        if operation not in {"discover", "status", "harvest", "plant", "create", "list", "forget"}:
            raise SkillError("operation 只能是 discover/status/harvest/plant/create/list/forget")
        chosen = self._crop_name(crop or "auto")
        if chosen not in set(CROP_SPECS) | {"auto"}:
            raise SkillError("crop 只能是 auto/wheat/carrots/potatoes/beetroots")
        await self._ensure_no_screen()
        memory = self._farm_memory()
        if operation == "list":
            return {"skill": "farm_plot", "plots": [self._farm_summary(row) for row in (memory.list() if memory else [])]}
        if operation == "forget":
            if not plot or memory is None:
                raise SkillError("forget 需要 plot")
            return {"skill": "farm_plot", "forget": bool(memory.forget(plot)), "plot": plot}
        if operation == "discover":
            result = await self._farm_discover(radius=radius, plot_hint=plot)
            result.pop("_cells", None)
            return {"skill": "farm_plot", "operation": "discover", **result}
        if operation == "status":
            plots = await self._farm_status(plot)
            return {"skill": "farm_plot", "operation": "status", "plots": plots}
        if operation == "create":
            water = []
            if mask:
                if origin is None:
                    status = await self.status()
                    position = status.get("position") or {}
                    origin = [int(position.get("x", 0)), int(position.get("y", 64)), int(position.get("z", 0))]
                planned, water = self._farm_cells_from_mask(mask, origin, chosen)
            elif cells:
                planned = []
                for row in cells:
                    if isinstance(row, dict):
                        parsed = {"x": row.get("x"), "y": row.get("y"), "z": row.get("z"),
                                  "crop": self._crop_name(row.get("crop") or chosen)}
                    elif isinstance(row, (list, tuple)) and len(row) >= 3:
                        parsed = {"x": row[0], "y": row[1], "z": row[2], "crop": chosen}
                    else:
                        continue
                    try:
                        parsed["x"], parsed["y"], parsed["z"] = int(parsed["x"]), int(parsed["y"]), int(parsed["z"])
                    except (TypeError, ValueError):
                        continue
                    if parsed["crop"] not in CROP_SPECS:
                        parsed["crop"] = "empty"
                    planned.append(parsed)
                if len(planned) > MAX_CELLS:
                    raise SkillError(f"田块最多 {MAX_CELLS} 格")
            else:
                status = await self.status()
                position = status.get("position") or {}
                ox, oy, oz = int(position.get("x", 0)), int(position.get("y", 64)), int(position.get("z", 0))
                data = await self._call(
                    "scan_blocks", {"radius": max(2, min(6, int(radius))), "limit": 32,
                                     "block": "minecraft:dirt,minecraft:grass_block,minecraft:dirt_path"},
                    timeout=10,
                )
                planned = []
                for row in (data.get("blocks") or []):
                    if not isinstance(row, dict):
                        continue
                    try:
                        x, y, z = int(row["x"]), int(row["y"]), int(row["z"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if abs(x - ox) + abs(z - oz) > 6:
                        continue
                    try:
                        above = await self._call("inspect_block", {"x": x, "y": y + 1, "z": z}, timeout=8)
                    except Exception:
                        continue
                    if not (above.get("air") or above.get("replaceable")):
                        continue
                    planned.append({"x": x, "y": y, "z": z, "crop": chosen if chosen in CROP_SPECS else "empty"})
                    if len(planned) >= max(1, min(256, int(count))):
                        break
                if not planned:
                    raise SkillError("附近没有可开垦的泥土/草方块")
            tilled, planted, shortages = await self._farm_till_and_plant(planned, chosen, dry_run=dry_run)
            shape = self._farm_shape(planned)
            plot_id = plot or ""
            if not dry_run and memory is not None and planned:
                status = await self.status()
                if not plot_id:
                    existing = {row["id"] for row in memory.list()}
                    index = 1
                    while f"farm-{index}" in existing:
                        index += 1
                    plot_id = f"farm-{index}"
                record = {
                    "id": plot_id,
                    "name": f"菜地{plot_id.split('-')[-1]}" if plot_id.startswith("farm-") else plot_id,
                    "world_key": str(status.get("world_key") or ""),
                    "dimension": str(status.get("dimension") or ""),
                    "cells": planned,
                    "water_cells": water,
                }
                try:
                    memory.upsert(record)
                except ValueError as exc:
                    shortages.append(str(exc))
            return {
                "skill": "farm_plot", "operation": "create", "plot": plot_id,
                "cells": len(planned), "shape": shape, "tilled": tilled,
                "planted": planted, "water_cells": len(water),
                "shortages": shortages, "dry_run": bool(dry_run),
            }
        if operation == "harvest":
            if plot and memory is not None:
                record = memory.get(plot)
                if not record:
                    raise SkillError("找不到田块：" + plot)
                target_cells = record.get("cells") or []
            else:
                discovered = await self._farm_discover(radius=radius)
                scanned_cells = discovered.pop("_cells", [])
                target_cells = []
                if memory is not None:
                    for row in memory.list():
                        target_cells.extend(row.get("cells") or [])
                if not target_cells:
                    target_cells = scanned_cells
            harvested, planted, harvest_reasons = await self._farm_harvest_cells(target_cells, count, replant=replant)
            harvested_total = sum(harvested.values())
            requested = max(1, min(256, int(count)))
            return {
                "skill": "farm_plot", "operation": "harvest",
                "requested": requested,
                "harvested": harvested_total,
                "collected": harvested_total,
                "partial": harvested_total < requested,
                "planted": sum(planted.values()),
                "crops": {"harvested": harvested, "planted": planted},
                "reasons": harvest_reasons[:10],
            }
        if operation == "plant":
            if plot and memory is not None:
                record = memory.get(plot)
                if not record:
                    raise SkillError("找不到田块：" + plot)
                target_cells = record.get("cells") or []
            else:
                discovered = await self._farm_discover(radius=radius)
                scanned_cells = discovered.pop("_cells", [])
                target_cells = []
                if memory is not None:
                    for row in memory.list():
                        target_cells.extend(row.get("cells") or [])
                if not target_cells:
                    target_cells = scanned_cells
            planted, skipped, reasons = await self._farm_plant_cells(target_cells, chosen, count)
            planted_total = sum(planted.values())
            requested = max(1, min(256, int(count)))
            return {
                "skill": "farm_plot", "operation": "plant",
                "requested": requested,
                "planted": planted_total,
                "collected": planted_total,
                "partial": planted_total < requested,
                "skipped": skipped,
                "reasons": reasons[:8],
                "crops": planted,
            }
        raise SkillError("未知田块操作")


    def _memory(self):
        getter = getattr(self.plugin, "_poi_memory_for", None)
        return getter() if callable(getter) else None

    # ------------------------------------------------------------------- sleep
    async def sleep_at(self, x, y, z):
        """L1: 查床上下文、走到站位、交互并确认进入睡眠。"""
        x, y, z = int(x), int(y), int(z)
        context = await self._call("block_context", {"x": x, "y": y, "z": z}, timeout=10)
        if context.get("bed_occupied"):
            raise SkillError("床已被占用")
        if not context.get("bed"):
            block = str(context.get("block") or "")
            if block.endswith("_bed"):
                raise SkillError("目标床当前不可用")
            raise SkillError("目标不是可用床")
        stands = [row for row in (context.get("stands") or []) if isinstance(row, dict)]
        if not stands:
            raise SkillError("床周围没有可站立位置")
        stand = min(stands, key=lambda row: _num(row.get("distance"), 999))
        error = await self.plugin._approach_target(
            stand["x"], stand.get("feet_y", stand["y"]), stand["z"], seconds=10, reach=1.5
        )
        if error:
            raise SkillError("接近床失败：" + error)
        status = await self.status()
        if status.get("can_sleep") is False:
            raise SkillError("现在不能睡觉：时间、天气或附近敌人不满足条件")
        await self._run("interact_block", {"x": x, "y": y, "z": z}, seconds=5)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = await self.status()
            if status.get("sleeping"):
                return {
                    "skill": "sleep_at",
                    "sleeping": True,
                    "bed": {"x": x, "y": y, "z": z},
                    "stand": {"x": stand["x"], "y": stand.get("feet_y", stand["y"]),
                              "z": stand["z"], "feet_y": stand.get("feet_y", stand["y"])},
                    "world_key": str(status.get("world_key") or ""),
                    "dimension": str(status.get("dimension") or ""),
                }
            await asyncio.sleep(0.4)
        raise SkillError("未确认进入睡眠")

    async def find_and_sleep(self, source="nearby", radius=16, remember=False, key="main"):
        """L2: 家床优先；找不到时扫描附近床，成功后可选写入家床记忆。"""
        pre_status = await self.status()
        pre_dim = str(pre_status.get("dimension") or "").lower()
        if "nether" in pre_dim or "the_end" in pre_dim:
            raise SkillError("下界和末地不能使用床（床会爆炸）")
        source = str(source or "nearby").strip().lower()
        memory = self._memory()
        if source in {"home", "home_bed", "家", "家床"}:
            status = await self.status()
            store = getattr(self.plugin, "_memory_store", None)
            row = None
            target = None
            if store is not None:
                scope = self.plugin._memory_scope_for(status)
                place = store.latest_place(**scope, kind="home_bed", key=str(key or "main"))
                if place:
                    local = store.local_view(place)
                    target = local.get("target")
                    row = place
            if target is None:
                legacy = memory.get("home_bed", str(key or "main")) if memory is not None else None
                if legacy:
                    if legacy.get("world_key") and legacy.get("world_key") != status.get("world_key"):
                        raise SkillError("家床记录属于另一个世界")
                    if legacy.get("dimension") and legacy.get("dimension") != status.get("dimension"):
                        raise SkillError("家床记录属于另一个维度")
                    target = legacy
            if not target:
                raise SkillError("没有记录家床")
            result = await self.sleep_at(target["x"], target["y"], target["z"])
            result.update({"source": "home_bed", "key": str(key or "main")})
            return result

        scan = await self._call(
            "scan_blocks",
            {"radius": max(4, min(32, int(radius))), "limit": 16, "block": "#minecraft:beds"},
            timeout=15,
        )
        beds = [row for row in (scan.get("blocks") or []) if isinstance(row, dict)]
        if not beds:
            raise SkillError("附近没有可用床")
        failures = []
        for bed in sorted(beds, key=lambda row: _num(row.get("distance"), 999))[:3]:
            try:
                result = await self.sleep_at(bed["x"], bed["y"], bed["z"])
            except SkillError as exc:
                failures.append(str(exc))
                continue
            remembered = False
            if remember and memory is not None:
                memory.set(
                    "home_bed", str(key or "main"), name="家床",
                    world_key=result.get("world_key", ""), dimension=result.get("dimension", ""),
                    x=result["bed"]["x"], y=result["bed"]["y"], z=result["bed"]["z"],
                )
                remembered = True
            helper = getattr(self.plugin, "_remember_home_bed", None)
            if remember and helper is not None:
                try:
                    place = helper(result, result.get("bed"), result.get("stand"),
                                   source="task_result", notes="skill find_and_sleep")
                    remembered = remembered or place is not None
                except Exception:
                    pass
            result.update({"source": "nearby", "remembered": remembered, "key": str(key or "main")})
            return result
        raise SkillError("附近没有可用床：" + "；".join(failures[:3]))

