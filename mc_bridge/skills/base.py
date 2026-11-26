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


class SkillsBaseMixin:
    def __init__(self, plugin):
        self.plugin = plugin
        self._recipes = {}
        self.checkpoint = None

    def _mark(self, skill, stage, completed=0, total=0, detail=None):
        callback = getattr(self, "checkpoint", None)
        if callback is None:
            return
        try:
            callback(str(skill), str(stage), completed=int(completed), total=int(total), detail=detail or {})
        except Exception:
            pass

    def handlers(self):
        return {
            "gather": self.gather,
            "craft": self.craft,
            "smelt": self.smelt,
            "transfer": self.transfer,
            "build": self.build,
            "farm": self.farm,
            "farm_plot": self.farm_plot,
            "sleep": self.sleep,
            "resupply": self.resupply,
            "explore": self.explore,
            "combat": self.combat,
            "hunt": self.hunt,
            "trade_for": self.trade_for,
            # L1 fixed combinations
            "sleep_at": self.sleep_at,
            "craft_recipe": self.craft_recipe,
            "smelt_batch": self.smelt_batch,
            "transfer_item": self.transfer_item,
            "attack_target": self.attack_target,
            "equip_best": self.equip_best,
            "eat_until_full": self.eat_until_full,
            "harvest_block": self.harvest_block,
            "plant_at": self.plant_at,
            "till_and_plant": self.till_and_plant,
            "open_and_read": self.open_and_read,
            "place_line": self.place_line,
            "return_to": self.return_to,
            # L2 search/planning aliases
            "mine_ore": self.mine_ore,
            "craft_item": self.craft_item,
            "smelt_item": self.smelt_item,
            "container_transfer": self.container_transfer,
            "find_and_sleep": self.find_and_sleep,
            "return_home": self.return_home,
            "tunnel_to": self.tunnel_to,
        }

    def validate_params(self, name, params=None):
        name = str(name or "")
        params = dict(params or {})
        handler = self.handlers().get(name)
        if handler is None:
            raise SkillError("未知技能：" + name)
        signature = inspect.signature(handler)
        allowed = {
            parameter.name for parameter in signature.parameters.values()
            if parameter.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
        }
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise SkillError("未知参数：" + ", ".join(unknown))
        missing = [
            parameter.name for parameter in signature.parameters.values()
            if parameter.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
            and parameter.default is inspect.Parameter.empty and parameter.name not in params
        ]
        if missing:
            raise SkillError("缺少参数：" + ", ".join(missing))
        normalized = dict(params)
        for parameter in signature.parameters.values():
            if parameter.name not in normalized:
                continue
            value = normalized[parameter.name]
            default = parameter.default
            if default is inspect.Parameter.empty or default is None:
                continue
            if isinstance(default, bool):
                if isinstance(value, bool):
                    normalized[parameter.name] = value
                elif isinstance(value, (int, float)) and value in (0, 1):
                    normalized[parameter.name] = bool(value)
                elif str(value).strip().lower() in {"true", "1", "yes", "y", "是", "开", "开启"}:
                    normalized[parameter.name] = True
                elif str(value).strip().lower() in {"false", "0", "no", "n", "否", "关", "关闭"}:
                    normalized[parameter.name] = False
                else:
                    raise SkillError(f"参数 {parameter.name} 必须是布尔值")
            elif isinstance(default, int):
                if isinstance(value, bool):
                    raise SkillError(f"参数 {parameter.name} 必须是整数")
                try:
                    normalized[parameter.name] = int(value)
                except (TypeError, ValueError) as exc:
                    raise SkillError(f"参数 {parameter.name} 必须是整数") from exc
            elif isinstance(default, float):
                try:
                    normalized[parameter.name] = float(value)
                except (TypeError, ValueError) as exc:
                    raise SkillError(f"参数 {parameter.name} 必须是数字") from exc
            elif isinstance(default, str):
                normalized[parameter.name] = str(value)
        if name == "gather":
            resource = RESOURCE_ALIASES.get(str(normalized.get("resource", "logs")).strip().lower(), str(normalized.get("resource", "logs")).strip().lower())
            if resource not in {"logs", "stone", "wheat", "sand"} and resource not in ORE_QUERIES:
                raise SkillError("resource 仅支持原木/石头/小麦/沙子/各种原矿")
            normalized["resource"] = resource
            if not 1 <= int(normalized.get("count", 8)) <= 256:
                raise SkillError("采集数量必须在1-256")
            if not 6 <= int(normalized.get("radius", 12)) <= 16:
                raise SkillError("扫描半径必须在6-16")
            if not 20 <= int(normalized.get("max_seconds", 180)) <= 600:
                raise SkillError("采集时限必须在20-600秒")
        if name in {"container_transfer", "transfer", "transfer_item"}:
            direction = str(normalized.get("direction", "to_container")).strip().lower()
            if direction not in {"to_container", "from_container"}:
                raise SkillError("direction 只能是 to_container 或 from_container")
            normalized["direction"] = direction
            raw_items = normalized.get("items")
            if raw_items is not None and str(raw_items).strip():
                moves = parse_transfer_items(raw_items)
                if not moves:
                    raise SkillError("批量搬运清单为空或格式不正确")
                if len(moves) > 32:
                    raise SkillError("单次批量搬运最多32种物品")
                if sum(move["count"] for move in moves) > 512:
                    raise SkillError("单次批量搬运总数不能超过512")
                normalized["items"] = moves
            else:
                if not str(normalized.get("item", "")).strip():
                    raise SkillError("请提供 item 或 items")
                if not 1 <= int(normalized.get("count", 1)) <= 256:
                    raise SkillError("搬运数量必须在1-256")
        if name == "smelt":
            item = str(normalized.get("item", "")).strip()
            if not re.fullmatch(r"[a-z0-9_]+:[a-z0-9_./-]+", item):
                raise SkillError("物品ID无效")
            if not 1 <= int(normalized.get("count", 1)) <= 32:
                raise SkillError("烧炼数量必须在1-32")
        if name == "craft":
            item = str(normalized.get("item", "")).strip()
            if not re.fullmatch(r"(?:[a-z0-9_]+:)?[a-z0-9_][a-z0-9_./-]*", item):
                raise SkillError("物品ID无效")
            if not 1 <= int(normalized.get("count", 1)) <= 64:
                raise SkillError("合成数量必须在1-64")
        if name == "farm":
            operation = str(normalized.get("operation", "harvest")).strip().lower()
            if operation not in {"harvest", "plant", "both"}:
                raise SkillError("operation 只能是 harvest/plant/both")
            crop = str(normalized.get("crop", "auto")).strip().lower()
            if crop not in {"auto", "wheat", "carrots", "potatoes", "beetroots"}:
                raise SkillError("crop 只能是 auto/wheat/carrots/potatoes/beetroots")
            normalized["operation"] = operation
            normalized["crop"] = crop
        if name == "farm_plot":
            operation = str(normalized.get("operation", "status")).strip().lower()
            if operation not in {"discover", "status", "harvest", "plant", "create", "list", "forget"}:
                raise SkillError("operation 只能是 discover/status/harvest/plant/create/list/forget")
            crop = str(normalized.get("crop", "auto")).strip().lower()
            if crop not in {"auto", "wheat", "carrots", "potatoes", "beetroots"}:
                raise SkillError("crop 只能是 auto/wheat/carrots/potatoes/beetroots")
            if not 2 <= int(normalized.get("radius", 12)) <= 16:
                raise SkillError("田块搜索半径必须在2-16")
            if not 1 <= int(normalized.get("count", 32)) <= 256:
                raise SkillError("田块处理数量必须在1-256")
            cells = normalized.get("cells")
            if cells is not None and not isinstance(cells, list):
                raise SkillError("cells 必须是数组")
            normalized["operation"] = operation
            normalized["crop"] = crop
        if name == "hunt":
            target = str(normalized.get("target", "auto")).strip().lower()
            if target != "auto":
                target = HUNT_ALIASES.get(target, target)
                if target not in HUNTABLE:
                    raise SkillError("target 只能是 auto 或具体猎物类型（cow/pig/sheep/chicken/rabbit/boar/azurecrane/duck/crow/squirrel/weasel/unagi 等）")
            if not 1 <= int(normalized.get("count", 2)) <= 20:
                raise SkillError("打猎数量必须在1-20")
            if not 4 <= int(normalized.get("radius", 24)) <= 32:
                raise SkillError("打猎搜索半径必须在4-32")
            if not 10 <= int(normalized.get("max_seconds", 180)) <= 600:
                raise SkillError("打猎时限必须在10-600秒")
            if not 0 <= int(normalized.get("meat_target", 0)) <= 64:
                raise SkillError("肉类目标必须在0-64")
            if not 0 <= int(normalized.get("search_rounds", 6)) <= 12:
                raise SkillError("搜索轮数必须在0-12")
            normalized["target"] = target
        if name == "build":
            if str(normalized.get("pattern", "line")).strip().lower() not in {"line", "wall", "floor", "pillar", "box"}:
                raise SkillError("pattern 只能是 line/wall/floor/pillar/box")
            if not 1 <= int(normalized.get("length", 4)) <= 16:
                raise SkillError("建造长度必须在1-16")
            if not 1 <= int(normalized.get("width", 1)) <= 16:
                raise SkillError("建造宽度必须在1-16")
            if not 1 <= int(normalized.get("height", 1)) <= 8:
                raise SkillError("建造高度必须在1-8")
        if name == "combat":
            if not 5 <= int(normalized.get("max_seconds", 30)) <= 120:
                raise SkillError("战斗时限必须在5-120秒")
            if not 1 <= int(normalized.get("retreat_health", 8)) <= 20:
                raise SkillError("撤退血量必须在1-20")
        return name, normalized

    async def run(self, name, params=None):
        name, params = self.validate_params(name, params)
        handler = self.handlers()[name]
        return await handler(**params)

    # ---------------------------------------------------------------- helpers
    def _err(self, data):
        return self.plugin._err(data)

    async def _call(self, action, params=None, timeout=15):
        data = await self.plugin._call(action, params or {}, timeout=timeout)
        err = self._err(data)
        if err:
            raise SkillError(err)
        return data

    async def _run(self, action, params, seconds=10):
        try:
            return await self.plugin._run_operation(action, params, seconds=seconds)
        except Exception as exc:
            raise SkillError(str(exc)) from exc

    async def status(self):
        return await self._call("status", timeout=10)

    @staticmethod
    def inventory_items(status):
        return [row for row in (status.get("inventory_items") or []) if isinstance(row, dict) and int(row.get("slot", 99)) < 36]

    def item_count(self, status, item):
        if item in {"logs", "stone", "wheat", "sand"} or item in ORE_ITEM_IDS:
            return sum(int(row.get("count", 0)) for row in self.inventory_items(status) if _matches_resource(str(row.get("id", "")), item))
        return sum(int(row.get("count", 0)) for row in self.inventory_items(status) if str(row.get("id", "")) == item)

    @staticmethod
    def _food_rows(status):
        rows = []
        for row in SkillsBaseMixin.inventory_items(status):
            if row.get("safe_food") is not True:
                continue
            item_id = str(row.get("id", ""))
            try:
                count = int(row.get("count", 0) or 0)
            except (TypeError, ValueError):
                count = 0
            try:
                nutrition = int(row.get("nutrition", 0) or 0)
            except (TypeError, ValueError):
                nutrition = 0
            if nutrition <= 0:
                nutrition = int(FOOD_NUTRITION.get(item_id, 1) or 1)
            if count > 0:
                rows.append({"id": item_id, "count": count, "nutrition": nutrition})
        return rows

    def food_points(self, status):
        return sum(row["count"] * row["nutrition"] for row in self._food_rows(status))

    def food_count(self, status):
        return sum(row["count"] for row in self._food_rows(status))

    async def _pickup_near(self, x, y, z, resource, radius=5):
        for attempt in range(3):
            data = await self._call(
                "scan_entities",
                {"radius": radius, "limit": 32, "type": "minecraft:item"},
                timeout=10,
            )
            rows = [row for row in (data.get("entities") or []) if isinstance(row.get("item"), dict)]
            rows = [
                row for row in rows
                if _matches_resource(str((row.get("item") or {}).get("id") or ""), resource)
            ]
            if not rows:
                if attempt < 2:
                    await asyncio.sleep(0.35)
                    continue
                return
            for row in rows:
                item_id = str((row.get("item") or {}).get("id") or "")
                try:
                    if _num(row.get("distance"), 99) > 2.2:
                        error = await self.plugin._approach_target(
                            row.get("x", x), row.get("y", y), row.get("z", z),
                            seconds=20, reach=1.8,
                        )
                        if error:
                            continue
                    try:
                        await self._run(
                            "pickup",
                            {"item": item_id, "entity_uuid": str(row.get("uuid") or "")},
                            seconds=4,
                        )
                    except Exception:
                        continue
                except Exception:
                    continue
            return

    async def _expose_block(self, node):
        """Break adjacent solid blocks so a hidden ore becomes reachable."""
        try:
            x, y, z = int(node["x"]), int(node["y"]), int(node["z"])
        except (KeyError, TypeError, ValueError):
            return False
        changed = False
        for dx, dy, dz in (
            (1, 0, 0), (-1, 0, 0), (0, 1, 0),
            (0, 0, 1), (0, 0, -1),
        ):
            px, py, pz = x + dx, y + dy, z + dz
            try:
                context = await self._call("block_context", {"x": px, "y": py, "z": pz}, timeout=6)
            except Exception:
                continue
            block = str(context.get("block") or "")
            if not block or block.endswith("air") or "water" in block or "lava" in block:
                continue
            try:
                await self._run("mine_at", {"x": px, "y": py, "z": pz}, seconds=8)
                changed = True
            except SkillError:
                continue
        return changed

    # ------------------------------------------------------------------- L1
    async def equip_best(self, tool_type="pickaxe"):
        aliases = {"镐": "pickaxe", "斧": "axe", "剑": "sword", "铲": "shovel", "锄": "hoe"}
        tool_type = aliases.get(str(tool_type or "").strip(), str(tool_type or "pickaxe").strip().lower())
        suffix = {
            "pickaxe": "_pickaxe", "axe": "_axe", "sword": "_sword",
            "shovel": "_shovel", "hoe": "_hoe",
        }.get(tool_type)
        if suffix is None:
            raise SkillError("tool_type 只能是 pickaxe/axe/sword/shovel/hoe")
        status = await self.status()
        rows = [row for row in self.inventory_items(status) if str(row.get("id", "")).endswith(suffix)]
        if not rows:
            raise SkillError(f"背包里没有 {tool_type}")
        best = max(rows, key=lambda row: (tool_rank(str(row.get("id", ""))), int(row.get("count", 0))))
        source = int(best["slot"])
        if source < 9:
            destination = source
        else:
            used = {int(row.get("slot", 99)) for row in self.inventory_items(status)}
            destination = next((i for i in range(9) if i not in used), int(status.get("selected_slot", 0) or 0))
            await self._run("inventory", {"operation": "swap", "slot": source, "hotbar": destination}, seconds=5)
        await self._call("select_slot", {"slot": destination}, timeout=10)
        status = await self.status()
        selected = {int(row.get("slot", 99)): row for row in self.inventory_items(status)}
        actual = str((selected.get(destination) or {}).get("id") or "")
        if actual != str(best.get("id")):
            raise SkillError("快捷栏没有确认切换到目标工具")
        return {"skill": "equip_best", "tool_type": tool_type, "item": actual, "slot": destination}

    async def eat_until_full(self):
        eaten = 0
        deadline = time.monotonic() + 25
        for _ in range(8):
            if time.monotonic() >= deadline:
                raise SkillError("进食超时，饱食度仍未满")
            status = await self.status()
            food = _num(status.get("food"), 0)
            if food >= 20:
                return {"skill": "eat_until_full", "eaten": eaten, "food": food}
            await self._run("inventory", {"operation": "food"}, seconds=5)
            result = await self._call("eat", timeout=10)
            error = self._err(result)
            if error:
                raise SkillError("进食失败：" + error)
            duration = max(2.6, min(11, _num(result.get("eat_ms"), 1600) / 1000 + 0.6))
            await asyncio.sleep(duration)
            after = await self.status()
            if _num(after.get("food"), 0) <= food:
                raise SkillError("进食后饱食度没有增加")
            eaten += 1
        raise SkillError("进食次数达到上限，饱食度仍未满")

    async def harvest_block(self, block_pos, item_filter=""):
        x, y, z = _position(block_pos)
        inspect = await self._call("block_context", {"x": x, "y": y, "z": z}, timeout=10)
        if inspect.get("air"):
            raise SkillError("目标位置是空气")
        distance = _num(inspect.get("distance"), 0)
        item_filter = str(item_filter or "").strip()
        before = await self._count(item_filter) if item_filter else 0
        excavated = False
        if distance > 4.5:
            stands = [row for row in (inspect.get("stands") or []) if isinstance(row, dict)]
            if stands:
                stand = min(stands, key=lambda row: _num(row.get("distance"), 999))
                error = await self.plugin._approach_target(
                    stand["x"], stand.get("feet_y", stand["y"]), stand["z"], seconds=10, reach=1.5
                )
            else:
                excavated = True
                error = await self.plugin._approach_target(
                    x, y, z, seconds=20, reach=2.5, allow_break=True
                )
            if error:
                raise SkillError("接近目标方块失败：" + error)
        if excavated:
            current = await self._call("block_context", {"x": x, "y": y, "z": z}, timeout=10)
            if current.get("air"):
                gained = 0
                if item_filter:
                    await self._pickup_near(x, y, z, item_filter)
                    gained = await self._count(item_filter) - before
                    if gained <= 0:
                        raise SkillError("目标方块已挖掉，但没有确认掉落物入包")
                return {
                    "skill": "harvest_block", "block": "air", "position": {"x": x, "y": y, "z": z},
                    "item": item_filter, "gained": gained, "excavated": True,
                }
        await self._run("mine_at", {"x": x, "y": y, "z": z}, seconds=15)
        gained = 0
        if item_filter:
            await self._pickup_near(x, y, z, item_filter)
            gained = await self._count(item_filter) - before
            if gained <= 0:
                raise SkillError("方块已挖掉，但没有确认掉落物入包")
        return {
            "skill": "harvest_block", "block": str(inspect.get("block") or ""),
            "position": {"x": x, "y": y, "z": z}, "item": item_filter, "gained": gained,
        }

    async def plant_at(self, farmland_pos, seed="minecraft:wheat_seeds"):
        x, y, z = _position(farmland_pos)
        farmland = await self._call("inspect_block", {"x": x, "y": y, "z": z}, timeout=10)
        if str(farmland.get("block") or "") != "minecraft:farmland":
            raise SkillError("目标方块不是耕地")
        above = await self._call("inspect_block", {"x": x, "y": y + 1, "z": z}, timeout=10)
        if not above.get("air") and not above.get("replaceable"):
            raise SkillError("耕地上方空间被占用")
        if await self._count(seed) <= 0:
            raise SkillError("背包里没有种子：" + seed)
        await self._run("plant", {"x": x, "y": y, "z": z, "item": seed}, seconds=6)
        after = await self._call("inspect_block", {"x": x, "y": y + 1, "z": z}, timeout=10)
        if after.get("air"):
            raise SkillError("种植后上方没有确认出现作物")
        return {"skill": "plant_at", "position": {"x": x, "y": y, "z": z}, "seed": seed, "crop": after.get("block")}

    async def till_and_plant(self, block_pos, seed="minecraft:wheat_seeds"):
        x, y, z = _position(block_pos)
        await self._run("till", {"x": x, "y": y, "z": z}, seconds=5)
        planted = await self.plant_at({"x": x, "y": y, "z": z}, seed)
        return {"skill": "till_and_plant", "position": {"x": x, "y": y, "z": z}, "planted": planted}

    async def open_and_read(self, container_pos):
        x, y, z = _position(container_pos)
        context = await self._call("block_context", {"x": x, "y": y, "z": z}, timeout=10)
        if not context.get("container"):
            raise SkillError("目标方块不是容器")
        stands = [row for row in (context.get("stands") or []) if isinstance(row, dict)]
        if stands:
            stand = min(stands, key=lambda row: _num(row.get("distance"), 999))
            error = await self.plugin._approach_target(stand["x"], stand.get("feet_y", stand["y"]), stand["z"], seconds=10, reach=1.5)
            if error:
                raise SkillError("接近容器失败：" + error)
        opened = False
        try:
            data = await self._call("container", {"operation": "open", "x": x, "y": y, "z": z}, timeout=10)
            if str(data.get("screen") or "none") == "none":
                raise SkillError("容器没有确认打开")
            opened = True
            await asyncio.sleep(0.4)
            read = await self._call("container", {"operation": "read"}, timeout=10)
            return {
                "skill": "open_and_read", "container": {"x": x, "y": y, "z": z},
                "title": read.get("title", ""), "slots": read.get("slots") or [],
            }
        finally:
            if opened:
                try:
                    await self._call("container", {"operation": "close"}, timeout=5)
                except Exception:
                    pass

    async def place_line(self, start, direction="east", length=4, material="minecraft:cobblestone"):
        x, y, z = _position(start)
        direction = str(direction or "east").strip().lower()
        dx, dz = {"east": (1, 0), "west": (-1, 0), "south": (0, 1), "north": (0, -1)}.get(direction, (None, None))
        if dx is None:
            raise SkillError("direction 只能是 east/west/south/north")
        length = max(1, min(64, int(length)))
        material = str(material or "").strip()
        if ":" not in material:
            material = "minecraft:" + material
        if await self._count(material) < length:
            raise SkillError(f"材料不足：需要 {length} 个 {material}")
        placed, errors = 0, []
        for i in range(length):
            px, pz = x + dx * i, z + dz * i
            try:
                await self._run("place_at", {"x": px, "y": y, "z": pz, "item": material}, seconds=8)
                placed += 1
            except Exception as exc:
                errors.append({"x": px, "y": y, "z": pz, "error": str(exc)})
                if len(errors) >= 4:
                    break
        return {"skill": "place_line", "placed": placed, "requested": length, "material": material, "errors": errors}

    async def return_to(self, target_pos, reach=2.0):
        x, y, z = _position(target_pos)
        reach = max(0.5, min(8.0, float(reach)))
        resolver = getattr(self.plugin, "_resolve_navigation_target", None)
        if callable(resolver):
            resolved, info = await resolver(x, y, z)
            if resolved is None:
                raise SkillError("返回目标不可站立：" + str(info))
            x, y, z = resolved
        mover = getattr(self.plugin, "_move_and_wait", None)
        moved = False
        if callable(mover):
            try:
                await mover(x, y, z, 30)
                moved = True
            except Exception:
                pass
        if moved:
            status = await self.status()
            position = status.get("position") or {}
            distance = math.sqrt(
                (float(position.get("x", 0)) - x) ** 2
                + (float(position.get("y", 0)) - y) ** 2
                + (float(position.get("z", 0)) - z) ** 2
            )
            if distance <= reach + 0.75:
                return {"skill": "return_to", "target": {"x": x, "y": y, "z": z}, "distance": distance, "arrived": True}
        error = await self.plugin._approach_target(x, y, z, seconds=30, reach=reach)
        if error:
            raise SkillError("返回目标失败：" + error)
        status = await self.status()
        position = status.get("position") or {}
        distance = math.sqrt(
            (float(position.get("x", 0)) - x) ** 2
            + (float(position.get("y", 0)) - y) ** 2
            + (float(position.get("z", 0)) - z) ** 2
        )
        if distance > reach + 0.75:
            raise SkillError(f"已停止但未到达目标，距离 {distance:.1f}")
        return {"skill": "return_to", "target": {"x": x, "y": y, "z": z}, "distance": distance, "arrived": True}

    # ----------------------------------------------------------------- gather
