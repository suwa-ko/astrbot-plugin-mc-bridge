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

    @staticmethod
    def _parse_blueprint_spec(spec, origin_x: int, origin_y: int, origin_z: int) -> list[tuple[int, int, int, str]]:
        """Parse either {blocks:[{x,y,z,item}]} or {palette:{}, layers:[[...]]} into absolute (x,y,z,item)."""
        if isinstance(spec, str):
            text = spec.strip()
            if not text:
                raise SkillError("蓝图内容为空")
            try:
                spec = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SkillError("蓝图 JSON 格式无效：" + str(exc)) from exc
        if not isinstance(spec, dict):
            raise SkillError("蓝图必须是 JSON 对象")
        cells: dict[tuple[int, int, int], str] = {}
        if isinstance(spec.get("blocks"), list):
            for row in spec["blocks"]:
                if not isinstance(row, dict):
                    continue
                item = str(row.get("item") or row.get("block") or "").strip()
                if not item or item in {"air", "minecraft:air"}:
                    continue
                if ":" not in item:
                    item = "minecraft:" + item
                try:
                    bx = origin_x + int(row.get("x", 0))
                    by = origin_y + int(row.get("y", 0))
                    bz = origin_z + int(row.get("z", 0))
                except (TypeError, ValueError):
                    continue
                cells[(bx, by, bz)] = item
        elif isinstance(spec.get("layers"), list):
            palette = spec.get("palette") or {}
            if not isinstance(palette, dict):
                raise SkillError("蓝图 palette 必须是字典")
            for dy, layer in enumerate(spec["layers"]):
                if not isinstance(layer, list):
                    continue
                for dz, row_str in enumerate(layer):
                    if not isinstance(row_str, str):
                        continue
                    for dx, ch in enumerate(row_str):
                        item = str(palette.get(ch) or "").strip()
                        if not item or item in {"air", "minecraft:air"}:
                            continue
                        if ":" not in item:
                            item = "minecraft:" + item
                        cells[(origin_x + dx, origin_y + dy, origin_z + dz)] = item
        else:
            raise SkillError("蓝图必须包含 blocks 或 layers 字段")
        if not cells:
            raise SkillError("蓝图不包含任何可放置方块")
        if len(cells) > 256:
            raise SkillError(f"单次蓝图建造最多 256 格（当前 {len(cells)} 格）")
        # Sort bottom-up by Y, then Z, then X for stable layer-by-layer building.
        return [
            (px, py, pz, item)
            for (px, py, pz), item in sorted(cells.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0]))
        ]

    async def build_blueprint(
        self,
        blueprint=None,
        x=0,
        y=64,
        z=0,
        name="",
        check_only=False,
        skip_positions=None,
    ):
        """L2: Layer-by-layer structured blueprint builder with pre-flight material check."""
        spec = blueprint
        bp_name = re.sub(r"[^a-zA-Z0-9_-]", "", str(name or "").strip())[:40]
        if spec is None and bp_name:
            from pathlib import Path
            data_dir = None
            getter = getattr(self.plugin, "_plugin_data_dir", None)
            if callable(getter):
                data_dir = Path(getter())
            if data_dir is not None:
                bp_file = data_dir / "blueprints" / f"{bp_name}.json"
                if bp_file.is_file():
                    spec = bp_file.read_text(encoding="utf-8")
        if spec is None:
            raise SkillError("build_blueprint 需要提供 blueprint JSON 或已存在的蓝图 name")

        blocks = self._parse_blueprint_spec(spec, int(x), int(y), int(z))
        skip = set()
        for item in (skip_positions or []):
            if isinstance(item, (list, tuple)) and len(item) >= 3:
                try:
                    skip.add((int(item[0]), int(item[1]), int(item[2])))
                except (TypeError, ValueError):
                    continue
        total_blocks = len(blocks)
        todo = [row for row in blocks if (row[0], row[1], row[2]) not in skip]
        required: dict[str, int] = {}
        for _px, _py, _pz, item in todo:
            required[item] = required.get(item, 0) + 1
        status = await self.status()
        missing: dict[str, int] = {}
        for item, need in required.items():
            have = self.item_count(status, item)
            if have < need:
                missing[item] = need - have
        if check_only:
            return {
                "skill": "build_blueprint",
                "name": bp_name or "inline",
                "check_only": True,
                "total_blocks": total_blocks,
                "remaining_blocks": len(todo),
                "materials_required": required,
                "materials_missing": missing,
                "ready": not missing,
            }
        if missing:
            detail = "、".join(f"{k} 缺 {v}" for k, v in missing.items())
            raise SkillError("蓝图材料不足：" + detail)

        dimension = str(status.get("dimension") or "")
        placed = 0
        errors = []
        placed_positions = [list(pos) for pos in skip]
        try:
            for px, py, pz, item in todo:
                cur = self._coords(await self.status())
                if cur is not None and math.dist(cur, (float(px) + 0.5, float(py) + 0.5, float(pz) + 0.5)) > 4.2:
                    await self.plugin._approach_target(px, py, pz, seconds=12, reach=3.5, allow_break=False)
                try:
                    await self._run("place_at", {"x": px, "y": py, "z": pz, "item": item}, seconds=8)
                    placed += 1
                    placed_positions.append([px, py, pz])
                    self._mark(
                        "build_blueprint",
                        "building",
                        len(placed_positions),
                        total_blocks,
                        {"positions": placed_positions[-64:], "name": bp_name or "inline"},
                    )
                except Exception as exc:
                    errors.append({"x": px, "y": py, "z": pz, "item": item, "error": str(exc)})
                    if len(errors) >= 6:
                        break
        finally:
            if placed_positions:
                xs = [p[0] for p in placed_positions]
                ys = [p[1] for p in placed_positions]
                zs = [p[2] for p in placed_positions]
                try:
                    self.plugin._register_build_region(
                        f"blueprint-{bp_name or 'inline'}-{min(xs)}-{min(ys)}-{min(zs)}",
                        (min(xs), min(ys), min(zs)),
                        (max(xs), max(ys), max(zs)),
                        dimension=dimension,
                    )
                except Exception:
                    pass
        return {
            "skill": "build_blueprint",
            "name": bp_name or "inline",
            "requested": total_blocks,
            "already_placed": len(skip),
            "placed": placed,
            "errors": errors,
            "goal_outcome": "fulfilled" if placed == len(todo) and not errors else ("partial" if placed > 0 else "unmet"),
        }

    # -------------------------------------------------------------------- farm
