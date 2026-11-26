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


class BuildingMixin:
    async def build(self, pattern="line", material="minecraft:cobblestone", x=0, y=64, z=0, length=4, width=1, height=1, direction="east", skip_positions=None):
        pattern = str(pattern or "line").strip().lower()
        material = str(material or "").strip()
        if ":" not in material:
            material = "minecraft:" + material
        if pattern not in {"line", "wall", "floor", "pillar", "box"}:
            raise SkillError("pattern 只能是 line/wall/floor/pillar/box")
        direction = str(direction or "east").strip().lower()
        dx, dz = {"east": (1, 0), "west": (-1, 0), "south": (0, 1), "north": (0, -1)}.get(direction, (1, 0))
        length = max(1, min(16, int(length)))
        width = max(1, min(16, int(width)))
        height = max(1, min(8, int(height)))
        skip = set()
        for item in (skip_positions or []):
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                try:
                    skip.add((int(item[0]), int(item[1]), int(item[2])))
                except (TypeError, ValueError):
                    continue
        positions = []
        if pattern == "line":
            positions = [(int(x) + dx * i, int(y), int(z) + dz * i) for i in range(length)]
        elif pattern == "pillar":
            positions = [(int(x), int(y) + i, int(z)) for i in range(height)]
        elif pattern == "wall":
            positions = [(int(x) + dx * i, int(y) + j, int(z) + dz * i) for i in range(length) for j in range(height)]
        elif pattern == "floor":
            positions = [(int(x) + i, int(y), int(z) + j) for i in range(length) for j in range(width)]
        elif pattern == "box":
            for i in range(length):
                for j in range(height):
                    positions.extend([(int(x) + dx * i, int(y) + j, int(z) + dz * i)])
            for side in (-1, 1):
                for i in range(1, width - 1):
                    for j in range(height):
                        positions.append((int(x) + dx * i - dz * side, int(y) + j, int(z) + dz * i + dx * side))
        positions = list(dict.fromkeys(positions))
        total_positions = len(positions)
        positions = [position for position in positions if position not in skip]
        if len(positions) > 64:
            raise SkillError("单次建造最多64格")
        if await self._count(material) < len(positions):
            raise SkillError(f"材料不足：需要 {len(positions)} 个 {material}")
        dimension = ""
        try:
            dimension = str((await self.status()).get("dimension") or "")
        except Exception:
            pass
        placed, errors = 0, []
        placed_positions = [list(position) for position in skip]
        try:
            for px, py, pz in positions:
                try:
                    await self._run("place_at", {"x": px, "y": py, "z": pz, "item": material}, seconds=8)
                    placed += 1
                    placed_positions.append([px, py, pz])
                    self._mark("build", "building", len(placed_positions), total_positions,
                               {"positions": placed_positions[-64:]})
                except Exception as exc:
                    errors.append({"x": px, "y": py, "z": pz, "error": str(exc)})
                    if len(errors) >= 4:
                        break
        finally:
            if placed_positions:
                xs = [position[0] for position in placed_positions]
                ys = [position[1] for position in placed_positions]
                zs = [position[2] for position in placed_positions]
                try:
                    self.plugin._register_build_region(
                        f"build-{pattern}-{min(xs)}-{min(ys)}-{min(zs)}",
                        (min(xs), min(ys), min(zs)),
                        (max(xs), max(ys), max(zs)),
                        dimension=dimension,
                    )
                except Exception:
                    pass
        return {"skill": "build", "pattern": pattern, "material": material,
                "requested": total_positions, "already_placed": len(skip),
                "placed": placed, "errors": errors}

    # -------------------------------------------------------------------- farm
