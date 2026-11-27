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


class MiningAndNavMixin:
    async def sleep(self, source="nearby", remember=False):
        return await self.find_and_sleep(source=source, remember=bool(remember))

    # ------------------------------------------------------------------- L2
    async def _ensure_no_screen(self):
        """Close stray inventory/crafting screens before mining or walking."""
        try:
            status = await self.status()
            screen = str(status.get("screen") or "none").strip().lower()
            if screen and screen not in {"none", "null"}:
                await self._call("container", {"operation": "close"}, timeout=5)
        except Exception:
            pass

    async def _auto_eat_if_needed(self, threshold=None):
        """Eat at a safe point inside a long task when food is low."""
        requested = bool(getattr(self.plugin, "_auto_eat_requested", False))
        config = getattr(self.plugin, "config", {}) or {}
        try:
            threshold = int(
                threshold if threshold is not None
                else config.get("auto_eat_food", 12) or 12
            )
        except (TypeError, ValueError):
            threshold = 12
        status = await self.status()
        food = _num(status.get("food"), 20)
        if not requested and food > threshold:
            return False
        hostiles = [
            row for row in (status.get("hostiles") or [])
            if isinstance(row, dict)
            and _num(row.get("distance"), 999) <= 6
            and row.get("line_of_sight", True) is not False
        ]
        if hostiles:
            return False
        try:
            await self.eat_until_full()
            setattr(self.plugin, "_auto_eat_requested", False)
            return True
        except SkillError:
            setattr(self.plugin, "_auto_eat_requested", False)
            return False

    async def _place_underfoot(self, item):
        """Try to pillar-place one block under the player while jumping."""
        item = str(item or "").strip()
        if not item:
            return False
        for _ in range(6):
            status = await self.status()
            current = self._coords(status)
            if current is None:
                return False
            x = int(math.floor(current[0]))
            y = int(math.floor(current[1]))
            z = int(math.floor(current[2]))
            try:
                await self._run("jump", {}, seconds=3)
                await self._run(
                    "place_at",
                    {"x": x, "y": y, "z": z, "item": item},
                    seconds=4,
                )
            except SkillError:
                await asyncio.sleep(0.4)
                continue
            await asyncio.sleep(0.4)
            after = await self.status()
            after_position = self._coords(after)
            if after_position is not None and after_position[1] > current[1] + 0.5:
                return True
        return False

    async def _ensure_pickaxe(self, min_remaining=40):
        """Return True when a pickaxe with enough durability is available."""
        status = await self.status()
        for row in self.inventory_items(status):
            item_id = str(row.get("id", "") or "")
            if not item_id.endswith("_pickaxe"):
                continue
            try:
                remaining = int(row.get("max_damage", 0) or 0) - int(row.get("damage", 0) or 0)
            except (TypeError, ValueError):
                remaining = 0
            if remaining >= int(min_remaining):
                return True
        if (
            self.item_count(status, "minecraft:iron_ingot") >= 3
            and self.item_count(status, "minecraft:stick") >= 2
        ):
            try:
                await self.craft("minecraft:iron_pickaxe", 1)
                return True
            except SkillError:
                # A 1x2 shaft has no room to place a crafting table normally.
                # Pillar-place one under the player, then retry.
                if self.item_count(await self.status(), "minecraft:crafting_table") < 1:
                    try:
                        await self.craft("minecraft:crafting_table", 1)
                    except SkillError:
                        pass
                if await self._place_underfoot("minecraft:crafting_table"):
                    try:
                        await self.craft("minecraft:iron_pickaxe", 1)
                        return True
                    except SkillError:
                        return False
                return False
        return False

    def _diggable_block(self, block):
        name = str(block or "").split(":")[-1].strip()
        if not name or name in {"air", "cave_air", "void_air", "water", "lava"}:
            return False
        if name in DIGGABLE_GROUND:
            return True
        config = getattr(self.plugin, "config", {}) or {}
        if bool(config.get("break_allow_unnatural", False)):
            return not any(
                word in name for word in (
                    "chest", "barrel", "furnace", "hopper", "bed",
                    "door", "trapdoor", "sign", "torch", "lantern",
                )
            )
        return False

    async def _can_dig_at(self, x, y, z):
        try:
            below = await self._call("block_context", {"x": int(x), "y": int(y) - 1, "z": int(z)}, timeout=8)
            under = await self._call("block_context", {"x": int(x), "y": int(y) - 2, "z": int(z)}, timeout=8)
        except Exception:
            return False
        if not self._diggable_block(below.get("block")):
            return False
        under_block = str(under.get("block") or "")
        if not under_block or under_block.endswith("air") or "water" in under_block or "lava" in under_block:
            return False
        return self._diggable_block(under_block) or bool(under.get("standable"))

    async def _can_dig_here(self):
        status = await self.status()
        if str(status.get("screen") or "none").strip().lower() not in {"none", "null"}:
            await self._ensure_no_screen()
            status = await self.status()
        if status.get("on_ground") is False:
            return False
        current = self._coords(status)
        if current is None:
            return False
        return await self._can_dig_at(
            int(math.floor(current[0])),
            int(math.floor(current[1])),
            int(math.floor(current[2])),
        )

    async def _move_to_dig_site(self):
        try:
            scan = await self._call("scan", {"resource": "logs", "radius": 12}, timeout=15)
        except Exception:
            return False
        ground = [row for row in (scan.get("ground") or []) if isinstance(row, dict)]
        for row in sorted(ground, key=lambda item: _num(item.get("distance"), 999))[:12]:
            try:
                x = int(row["x"])
                y = int(round(float(row.get("feet_y", row.get("y", 0)))))
                z = int(row["z"])
            except (KeyError, TypeError, ValueError):
                continue
            if not await self._can_dig_at(x, y, z):
                continue
            error = await self.plugin._approach_target(x, y, z, seconds=20, reach=1.2)
            if not error:
                return True
        return False

    async def _prepare_dig_site(self):
        if await self._can_dig_here():
            return True
        return await self._move_to_dig_site()

    async def _relocate_shaft(self, x: int, y: int, z: int) -> bool:
        """Tunnel 2-3 blocks laterally to a safe shaft column when a void/cave is directly below."""
        for dist in (2, 3):
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, nz = x + dx * dist, z + dz * dist
                if not await self._can_dig_at(nx, y, nz):
                    continue
                try:
                    moved = await self.tunnel_to([nx, y, nz], max_distance=6, reach=1.2)
                except Exception:
                    continue
                if isinstance(moved, dict) and moved.get("arrived") and await self._can_dig_here():
                    return True
        return False

    async def _dig_to_layer(self, target_y, deadline, step=8):
        """Dig a safe 1x1 shaft down toward a target Y, relocating laterally around voids."""
        digs = 0
        relocations = 0
        target_y = int(target_y)
        while True:
            if time.monotonic() >= deadline:
                raise SkillError("向下挖掘到矿层超时")
            status = await self.status()
            position = status.get("position") or {}
            try:
                x = int(math.floor(float(position.get("x", 0))))
                y = int(math.floor(float(position.get("y", 0))))
                z = int(math.floor(float(position.get("z", 0))))
            except (TypeError, ValueError):
                raise SkillError("当前位置不可用，无法向下挖掘")
            if y <= target_y + 2:
                return digs
            depth = max(1, min(int(step), y - target_y))
            try:
                await self._run("dig_down", {"depth": depth}, seconds=80)
            except SkillError as exc:
                text = str(exc)
                if "预算" in text:
                    raise
                if "screen" in text.lower():
                    await self._ensure_no_screen()
                    try:
                        await self._run("dig_down", {"depth": depth}, seconds=80)
                    except SkillError as retry_exc:
                        text = str(retry_exc)
                    else:
                        text = ""
                if text and ("no safe floor" in text or "falling" in text) and relocations < 3:
                    relocations += 1
                    if await self._relocate_shaft(x, y, z):
                        continue
                if text and any(
                    word in text for word in (
                        "不在可破坏类别", "not standing safely",
                        "no safe floor", "falling", "not visible",
                    )
                ):
                    raise SkillError("向下挖掘受阻：" + text)
                # A cave or fluid stops the shaft; scan from this depth.
                return digs
            after = await self.status()
            after_position = after.get("position") or {}
            try:
                after_y = int(math.floor(float(after_position.get("y", y))))
            except (TypeError, ValueError):
                after_y = y
            if after_y >= y:
                raise SkillError("向下挖掘没有进展")
            digs += y - after_y

    async def mine_ore(self, resource="coal", count=8, radius=12, max_seconds=1200, allow_night=False, return_home=False):
        resource = RESOURCE_ALIASES.get(str(resource or "").strip().lower(), str(resource or "").strip().lower())
        if resource not in ORE_QUERIES and resource != "stone":
            raise SkillError("mine_ore 只支持石头和各种原矿")
        await self._ensure_no_screen()
        if not await self._ensure_pickaxe(80):
            raise SkillError("没有耐久足够的镐，无法深层采矿；请先合成铁镐")
        await self._auto_eat_if_needed()
        target = max(1, min(32, int(count)))
        radius = max(6, min(16, int(radius)))
        deadline = time.monotonic() + max(30, min(1200, int(max_seconds)))
        result = None
        error = None
        layer = DEEP_ORE_LAYERS.get(resource)
        status = await self.status()
        position = status.get("position") or {}
        try:
            current_y = int(math.floor(float(position.get("y", 0))))
        except (TypeError, ValueError):
            current_y = 0

        if layer is not None and current_y > layer + 2:
            # Deep ores: descend at the current mining site before scanning.
            # Letting gather() explore first can walk back into the base, where
            # the floor may be a slab and cannot be dug.
            if not await self._prepare_dig_site():
                raise SkillError("附近没有可向下挖的自然地面；请先走到泥土/石头地面再挖矿")
            self._mark("mine_ore", "descending", 0, target, {"resource": resource, "target_y": layer})
            await self._dig_to_layer(layer, deadline)
            await self._auto_eat_if_needed()
            remaining = int(max(20, deadline - time.monotonic()))
            try:
                result = await self.gather(
                    resource, target, radius=radius,
                    max_seconds=remaining, allow_night=allow_night,
                )
            except SkillError as exc:
                error = exc
        else:
            try:
                result = await self.gather(
                    resource, target, radius=radius,
                    max_seconds=min(120, max_seconds), allow_night=allow_night,
                )
            except SkillError as exc:
                error = exc
                if layer is None or not any(word in str(exc) for word in ("没有可采集", "不可采集")):
                    raise
                if current_y <= layer + 2:
                    raise
                if not await self._prepare_dig_site():
                    raise SkillError("附近没有可向下挖的自然地面；请先走到泥土/石头地面再挖矿")
                self._mark("mine_ore", "descending", 0, target, {"resource": resource, "target_y": layer})
                try:
                    await self._dig_to_layer(layer, deadline)
                except SkillError as dig_exc:
                    error = dig_exc
                remaining = int(max(20, deadline - time.monotonic()))
                try:
                    result = await self.gather(
                        resource, target, radius=radius,
                        max_seconds=remaining, allow_night=allow_night,
                    )
                    error = None
                except SkillError as gather_exc:
                    error = gather_exc

        if result is None:
            # Do not silently walk home on an ordinary mining failure; that
            # hides the failure.  Only return when staying would be dangerous.
            status = await self.status()
            health = _num(status.get("health"), 20)
            danger = any(
                isinstance(row, dict) and _num(row.get("distance"), 999) <= 8
                for row in (status.get("hostiles") or [])
            )
            if return_home and (health <= 8 or danger):
                try:
                    await self.return_home()
                except SkillError:
                    pass
            raise error or SkillError("没有采集到目标矿物")
        result["skill"] = "mine_ore"
        if return_home:
            self._mark("mine_ore", "returning", 0, target, {"resource": resource})
            try:
                result["return_home"] = await self.return_home()
                result["returned"] = True
            except SkillError as exc:
                result["return_home_error"] = str(exc)
                result["returned"] = False
                result["goal_outcome"] = "partial"
        return result

    async def craft_item(self, item, count=1, table_pos=None):
        result = await self.craft(item, count, table_pos=table_pos)
        result["skill"] = "craft_item"
        return result

    async def smelt_item(self, item, count=1, collect_only=False):
        result = await self.smelt(item, count, collect_only=collect_only)
        result["skill"] = "smelt_item"
        return result

    async def container_transfer(self, item="", count=1, direction="to_container", x=0, y=0, z=0, close=True, container_pos=None, items=None):
        position = container_pos if container_pos is not None else {"x": int(x), "y": int(y), "z": int(z)}
        if items is not None and str(items).strip():
            result = await self.transfer_many(
                items,
                direction=direction,
                x=int(position.get("x", 0)),
                y=int(position.get("y", 0)),
                z=int(position.get("z", 0)),
                close=close,
            )
        else:
            result = await self.transfer_item(position, item, count, direction=direction, close=close)
        result["skill"] = "container_transfer"
        return result

    @staticmethod
    def _coords(status):
        position = status.get("position") if isinstance(status, dict) else None
        if not isinstance(position, dict):
            return None
        try:
            return (
                float(position.get("x", 0)),
                float(position.get("y", 0)),
                float(position.get("z", 0)),
            )
        except (TypeError, ValueError):
            return None

    async def tunnel_to(self, target_pos, max_distance=32, reach=2.0):
        """L2: bounded tunnel toward a target stand.

        A single client navigation call carries a small break budget, so the
        skill restarts navigation against the same resolved target until it
        arrives, the task budget is exhausted, or it stops making progress.
        """
        x, y, z = _position(target_pos)
        limit = max(1, min(32, int(max_distance)))
        reach = max(0.5, min(4.0, float(reach)))
        await self._ensure_no_screen()
        start = self._coords(await self.status())
        if start is None:
            raise SkillError("当前位置不可用，无法挖隧道")
        if math.dist(start, (float(x), float(y), float(z))) > limit + 1.5:
            raise SkillError(f"隧道目标超出 {limit} 格安全范围")
        if abs(start[1] - float(y)) > 12:
            raise SkillError("隧道目标垂直差超过 12 格，请分段接近或改用 return_home")
        resolved, info = await self.plugin._resolve_navigation_target(x, y, z)
        if resolved is None:
            raise SkillError("隧道目标不可站立：" + str(info))
        point = tuple(float(value) for value in resolved)
        if abs(point[1] - start[1]) > 12:
            raise SkillError("隧道目标垂直差超过 12 格，请分段接近或改用 return_home")

        last_error = None
        best = math.dist(start, point)
        stalled = 0
        for _ in range(8):
            current = self._coords(await self.status())
            if current is not None:
                distance = math.dist(current, point)
                if distance <= reach + 0.75:
                    return {
                        "skill": "tunnel_to",
                        "target": {"x": int(resolved[0]), "y": int(resolved[1]), "z": int(resolved[2])},
                        "distance": distance,
                        "arrived": True,
                    }
                if distance < best - 0.5:
                    best = distance
                    stalled = 0
                else:
                    stalled += 1
            error = await self.plugin._approach_target(
                resolved[0], resolved[1], resolved[2],
                seconds=30,
                reach=reach,
                allow_break=True,
            )
            if error:
                last_error = error
                if "预算" in error or "无路" in error:
                    raise SkillError("隧道接近失败：" + error)
            if stalled >= 2:
                break

        current = self._coords(await self.status())
        distance = 999.0 if current is None else math.dist(current, point)
        if distance <= reach + 0.75:
            return {
                "skill": "tunnel_to",
                "target": {"x": int(resolved[0]), "y": int(resolved[1]), "z": int(resolved[2])},
                "distance": distance,
                "arrived": True,
            }
        detail = f"：{last_error}" if last_error else ""
        raise SkillError(f"隧道停止后仍未到达目标，距离 {distance:.1f}{detail}")

    def _home_target(self, status):
        getter = getattr(self.plugin, "_memory_store_for", None)
        if callable(getter):
            try:
                store = getter()
                scope = self.plugin._memory_scope_for(status)
                row = (store.latest_place(**scope, kind="home_bed", key="main")
                       or store.latest_place(**scope, kind="home", key="main"))
                if row:
                    view = store.local_view(row)
                    stand = view.get("stand") or view.get("target")
                    if stand and stand.get("x") is not None:
                        return {
                            "x": int(round(float(stand["x"]))),
                            "y": int(round(float(stand.get("feet_y", stand["y"])))),
                            "z": int(round(float(stand["z"]))),
                            "world_key": view.get("world_key", ""),
                            "dimension": view.get("dimension", ""),
                        }
            except Exception:
                pass
        memory = self._memory()
        row = (memory.get("home", "main") or memory.get("home_bed", "main")) if memory is not None else None
        if row:
            return {
                "x": int(row["x"]), "y": int(row["y"]), "z": int(row["z"]),
                "world_key": row.get("world_key", ""), "dimension": row.get("dimension", ""),
            }
        return None

    async def _ascend_to_surface(self, min_y=None, max_steps=192):
        """Climb out by mining a one-block-up staircase, then stepping onto it.

        Baritone cannot reliably path to a solid cell inside rock, so ascent is
        done deterministically: clear the two blocks above the next step, then
        walk up onto the exposed floor.  This works in a 1x1 shaft as well as in
        a room.  When ``min_y`` is given, climb to that level instead of relying
        on ``sky_visible`` (an open shaft can see the sky while still being deep).
        """
        min_y = None if min_y is None else int(min_y)
        directions = ((1, 0), (-1, 0), (0, 1), (0, -1))
        last_error = None
        for step in range(max_steps):
            status = await self.status()
            current = self._coords(status)
            if current is None:
                raise SkillError("当前位置不可用，无法向上挖掘")
            if min_y is not None:
                if current[1] >= min_y - 1:
                    return True
            elif status.get("sky_visible", True) is not False:
                return True
            x = int(math.floor(current[0]))
            y = int(math.floor(current[1]))
            z = int(math.floor(current[2]))
            if step % 8 == 0:
                await self._auto_eat_if_needed()
            moved = False
            step_errors = []
            for offset in range(4):
                dx, dz = directions[(step + offset) % 4]
                tx, ty, tz = x + dx, y + 1, z + dz
                try:
                    base = await self._call(
                        "block_context", {"x": tx, "y": y, "z": tz}, timeout=8,
                    )
                except Exception:
                    base = {}
                base_block = str(base.get("block") or "")
                if (
                    "water" in base_block
                    or "lava" in base_block
                    or base.get("fluid")
                ):
                    continue
                if base_block.endswith("air"):
                    # In a cave there may be no adjacent step base.  Try to
                    # place a filler block; the client will only accept it if a
                    # visible sturdy support face exists.
                    filler = next(
                        (
                            item for item in (
                                "minecraft:cobblestone", "minecraft:dirt",
                                "minecraft:stone", "minecraft:deepslate",
                            )
                            if self.item_count(status, item) > 0
                        ),
                        "",
                    )
                    if not filler:
                        continue
                    try:
                        await self._run(
                            "place_at",
                            {"x": tx, "y": y, "z": tz, "item": filler},
                            seconds=8,
                        )
                    except SkillError:
                        continue
                if base_block and any(
                    word in base_block for word in ("chest", "barrel", "furnace", "hopper", "bed")
                ):
                    continue
                # A 1x2 step has a solid ceiling directly above the player.
                # Clear it first, otherwise Baritone cannot jump onto the step.
                try:
                    await self._run(
                        "mine_at",
                        {"x": x, "y": y + 2, "z": z},
                        seconds=8,
                    )
                except SkillError as exc:
                    pass
                for dy in (1, 2):
                    try:
                        await self._run(
                            "mine_at",
                            {"x": tx, "y": y + dy, "z": tz},
                            seconds=8,
                        )
                    except SkillError as exc:
                        # Air is fine; a protected block will make the move
                        # fail and the next direction will be tried.
                        pass
                error = await self.plugin._approach_target(
                    tx, ty, tz, seconds=12, reach=1.2, allow_break=False,
                )
                after = await self.status()
                after_position = self._coords(after)
                if after_position is not None and after_position[1] > current[1] + 0.5:
                    moved = True
                    break
                if error:
                    step_errors.append(error)
                    last_error = error
            if not moved:
                detail = f"：{last_error}" if last_error else ""
                raise SkillError("向上挖掘没有进展" + detail)
        raise SkillError("未能到达地表")

    async def _hop_toward_home(self, target, max_steps=8):
        """Move upward through existing standable cells before digging.

        The client limits one navigation request to 12 blocks vertically, so a
        home 13+ blocks above cannot be a single goto.  Probe standable cells
        between the current position and the bed (stairs/rooms), and only fall
        back to mining if no such route exists.
        """
        target_x = int(target["x"])
        target_y = int(target["y"])
        target_z = int(target["z"])
        resolver = getattr(self.plugin, "_resolve_navigation_target", None)
        if not callable(resolver):
            return False
        for _ in range(max_steps):
            status = await self.status()
            current = self._coords(status)
            if current is None:
                return False
            if target_y - current[1] <= 8:
                return True
            moved = False
            for step in (8, 4, 2):
                step_y = min(target_y, int(current[1]) + step)
                if step_y <= current[1] + 1:
                    continue
                resolved, _info = await self.plugin._resolve_navigation_target(
                    target_x, step_y, target_z,
                )
                if resolved is None:
                    continue
                if resolved[1] <= current[1] + 1:
                    continue
                error = await self.plugin._approach_target(
                    resolved[0], resolved[1], resolved[2],
                    seconds=30, reach=2.0,
                )
                after = await self.status()
                after_position = self._coords(after)
                if after_position is not None and after_position[1] > current[1] + 0.5:
                    moved = True
                    break
                if error:
                    continue
            if not moved:
                return False
        return False

    async def _unprotect_bed_region(self, target):
        """A bed accidentally registered as a container must not block entry."""
        getter = getattr(self.plugin, "_break_policy_for", None)
        if not callable(getter):
            return
        try:
            policy = getter()
            rows = policy.list()
        except Exception:
            return
        for row in rows:
            if str(row.get("kind") or "") != "container":
                continue
            try:
                cx = (int(row["min_x"]) + int(row["max_x"])) // 2
                cy = (int(row["min_y"]) + int(row["max_y"])) // 2
                cz = (int(row["min_z"]) + int(row["max_z"])) // 2
            except (KeyError, TypeError, ValueError):
                continue
            if abs(cx - int(target["x"])) > 4 or abs(cz - int(target["z"])) > 4:
                continue
            try:
                context = await self._call(
                    "block_context", {"x": cx, "y": cy, "z": cz}, timeout=8,
                )
            except Exception:
                continue
            if "bed" in str(context.get("block") or ""):
                try:
                    policy.remove(str(row.get("name") or ""))
                except Exception:
                    pass

    async def return_home(self, reach=2.0):
        await self._ensure_no_screen()
        status = await self.status()
        target = self._home_target(status)
        if not target:
            raise SkillError("没有记录家床")
        if target.get("world_key") and target["world_key"] != status.get("world_key"):
            raise SkillError("家床记录属于另一个世界")
        if target.get("dimension") and target["dimension"] != status.get("dimension"):
            raise SkillError("家床记录属于另一个维度")
        reach = max(0.5, min(8.0, float(reach)))
        await self._unprotect_bed_region(target)
        await self._auto_eat_if_needed()
        current = self._coords(status)
        if current is None:
            raise SkillError("当前位置不可用，无法回家")
        target_point = (float(target["x"]), float(target["y"]), float(target["z"]))
        distance = math.dist(current, target_point)
        above = target_point[1] - current[1]
        direct_error = None
        ascended = False

        # 1) Normal route first when the bed is nearby or we are already outside.
        # This avoids digging up through a base when the stairs are usable.
        if status.get("sky_visible", True) is not False or above <= 12:
            try:
                result = await self.return_to(target, reach=reach)
                result["skill"] = "return_home"
                result["ascended"] = False
                result["hopped"] = False
                result["tunneled"] = False
                return result
            except SkillError as exc:
                direct_error = exc

        tunnel_error = None
        hopped = False

        # 2) Use existing stairs/rooms before digging through player blocks.
        if above > 2:
            try:
                if await self._hop_toward_home(target):
                    hopped = True
                    try:
                        result = await self.return_to(target, reach=reach)
                        result["skill"] = "return_home"
                        result["ascended"] = False
                        result["hopped"] = True
                        result["tunneled"] = False
                        return result
                    except SkillError as exc:
                        if direct_error is None:
                            direct_error = exc
            except SkillError as exc:
                if direct_error is None:
                    direct_error = exc

        # 3) Short tunnel for a nearby bed behind walls/terrain.
        if distance <= 32 and abs(above) <= 12:
            try:
                await self.tunnel_to(target, max_distance=32, reach=reach)
                return {
                    "skill": "return_home",
                    "target": {"x": int(target["x"]), "y": int(target["y"]), "z": int(target["z"])},
                    "distance": distance,
                    "arrived": True,
                    "ascended": False,
                    "hopped": hopped,
                    "tunneled": True,
                }
            except SkillError as exc:
                tunnel_error = exc

        # 4) Direct and short-tunnel routes failed.  If the bed is above us,
        # climb a staircase to the bed's level; do not trust sky_visible, because
        # an open shaft can see the sky while still being deep underground.
        if above > 2:
            try:
                await self._ascend_to_surface(min_y=int(round(target_point[1])))
                ascended = True
                result = await self.return_to(target, reach=reach)
                result["skill"] = "return_home"
                result["ascended"] = True
                result["hopped"] = hopped
                result["tunneled"] = False
                return result
            except SkillError as exc:
                if direct_error is None:
                    direct_error = exc
                # Recompute distance from the new position; a short tunnel may
                # now be enough to reach the bed.
                status = await self.status()
                current = self._coords(status)
                if current is not None:
                    updated_distance = math.dist(current, target_point)
                    if updated_distance <= 32 and abs(target_point[1] - current[1]) <= 12:
                        try:
                            await self.tunnel_to(target, max_distance=32, reach=reach)
                            return {
                                "skill": "return_home",
                                "target": {"x": int(target["x"]), "y": int(target["y"]), "z": int(target["z"])},
                                "distance": updated_distance,
                                "arrived": True,
                                "ascended": True,
                                "hopped": hopped,
                                "tunneled": True,
                            }
                        except SkillError as tunnel_exc:
                            if tunnel_error is None:
                                tunnel_error = tunnel_exc
                # The staircase may have reached a basement/room that has an
                # existing path.  Re-probe before reporting failure.
                try:
                    if await self._hop_toward_home(target):
                        result = await self.return_to(target, reach=reach)
                        result["skill"] = "return_home"
                        result["ascended"] = True
                        result["hopped"] = True
                        result["tunneled"] = False
                        return result
                except SkillError as hop_exc:
                    if direct_error is None:
                        direct_error = hop_exc

        if direct_error is not None and tunnel_error is not None:
            raise SkillError(f"回家失败：{direct_error}；隧道失败：{tunnel_error}") from tunnel_error
        if direct_error is not None:
            raise direct_error
        if tunnel_error is not None:
            raise tunnel_error
        raise SkillError("回家失败：目标太远且无法直接到达")

    # ---------------------------------------------------------------- resupply
