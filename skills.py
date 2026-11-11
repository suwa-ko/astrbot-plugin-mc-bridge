"""Vanilla skill layer built on top of primitive Minecraft actions.

The old autonomous survival implementation is intentionally gone.  Each method
here is an explicit, cancellable goal that composes primitives, verifies the
result from real inventory/block state, and reports partial progress instead of
claiming success.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import time

from .core import ActionError


class SkillError(ActionError):
    """A skill could not reach its verified goal."""


def parse_transfer_items(raw):
    """Parse a batch transfer list into [{'item': str, 'count': int}]."""
    if raw is None:
        return []
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = [{"item": key, "count": value} for key, value in raw.items()]
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("[") or text.startswith("{"):
            try:
                return parse_transfer_items(json.loads(text))
            except json.JSONDecodeError:
                pass
        rows = []
        for part in re.split(r"[,，;；\n]+", text):
            part = part.strip()
            if not part:
                continue
            match = re.match(r"^(.*?)(?:\s*[xX×*]\s*|\s*[:：]\s*|\s+)(\d+)\s*$", part)
            if match and match.group(1).strip():
                rows.append({"item": match.group(1).strip(), "count": int(match.group(2))})
            else:
                rows.append({"item": part, "count": 1})
    else:
        return []
    moves = []
    for row in rows:
        if isinstance(row, str):
            item, count = row.strip(), 1
        elif isinstance(row, dict):
            item = str(row.get("item") or row.get("id") or "").strip()
            try:
                count = int(row.get("count", 1) or 1)
            except (TypeError, ValueError):
                count = 1
        else:
            continue
        if not item:
            continue
        moves.append({"item": item, "count": max(1, min(256, count))})
    return moves


RESOURCE_ALIASES = {
    "木头": "logs", "原木": "logs", "树": "logs", "log": "logs",
    "石头": "stone", "圆石": "stone", "石料": "stone", "stone": "stone",
    "煤": "coal", "煤炭": "coal", "煤矿": "coal", "coal": "coal",
    "铁": "iron", "铁矿": "iron", "iron": "iron",
    "铜": "copper", "铜矿": "copper", "copper": "copper",
    "金": "gold", "金矿": "gold", "gold": "gold",
    "红石": "redstone", "红石矿": "redstone", "redstone": "redstone",
    "青金石": "lapis", "青金石矿": "lapis", "lapis": "lapis",
    "钻石": "diamond", "钻石矿": "diamond", "diamond": "diamond",
    "绿宝石": "emerald", "绿宝石矿": "emerald", "emerald": "emerald",
    "小麦": "wheat", "麦子": "wheat", "wheat": "wheat",
}
LOG_SUFFIX = "_log"
STONE_IDS = {"minecraft:stone", "minecraft:cobblestone", "minecraft:deepslate", "minecraft:cobbled_deepslate"}
ORE_ITEM_IDS = {
    "coal": {"minecraft:coal"},
    "iron": {"minecraft:raw_iron", "minecraft:iron_ore", "minecraft:deepslate_iron_ore"},
    "copper": {"minecraft:raw_copper", "minecraft:copper_ore", "minecraft:deepslate_copper_ore"},
    "gold": {"minecraft:raw_gold", "minecraft:gold_ore", "minecraft:deepslate_gold_ore", "minecraft:nether_gold_ore"},
    "redstone": {"minecraft:redstone"},
    "lapis": {"minecraft:lapis_lazuli"},
    "diamond": {"minecraft:diamond"},
    "emerald": {"minecraft:emerald"},
}
ORE_QUERIES = {
    "coal": "coal_ore,deepslate_coal_ore",
    "iron": "iron_ore,deepslate_iron_ore",
    "copper": "copper_ore,deepslate_copper_ore",
    "gold": "gold_ore,deepslate_gold_ore,nether_gold_ore",
    "redstone": "redstone_ore,deepslate_redstone_ore",
    "lapis": "lapis_ore,deepslate_lapis_ore",
    "diamond": "diamond_ore,deepslate_diamond_ore",
    "emerald": "emerald_ore,deepslate_emerald_ore",
}
DEEP_ORE_LAYERS = {
    "coal": 96,
    "iron": 32,
    "copper": 48,
    "gold": 16,
    "redstone": 12,
    "lapis": 0,
    "diamond": -54,
    "emerald": 32,
}
PLANK_IDS = {
    "minecraft:oak_planks", "minecraft:spruce_planks", "minecraft:birch_planks",
    "minecraft:jungle_planks", "minecraft:acacia_planks", "minecraft:dark_oak_planks",
    "minecraft:mangrove_planks", "minecraft:cherry_planks", "minecraft:bamboo_planks",
    "minecraft:crimson_planks", "minecraft:warped_planks",
}
TOOL_SUFFIXES = ("_pickaxe", "_axe", "_sword", "_shovel", "_hoe")
TOOL_TIER_RANK = {"netherite": 5, "diamond": 4, "iron": 3, "stone": 2, "golden": 1, "wooden": 0}


def tool_rank(item_id):
    item_id = str(item_id)
    for suffix in TOOL_SUFFIXES:
        if item_id.endswith(suffix):
            for tier, rank in TOOL_TIER_RANK.items():
                if item_id.startswith(f"minecraft:{tier}_"):
                    return rank
            return 0
    return -1


def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _position(value):
    if isinstance(value, dict):
        return int(value.get("x", 0)), int(value.get("y", 0)), int(value.get("z", 0))
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return int(value[0]), int(value[1]), int(value[2])
    if isinstance(value, str):
        parts = value.replace(",", " ").split()
        if len(parts) >= 3:
            return int(parts[0]), int(parts[1]), int(parts[2])
    raise SkillError("坐标必须是 {x,y,z}、[x,y,z] 或 'x y z'")


def _matches_resource(item_id, resource):
    if resource == "logs":
        return item_id.endswith(LOG_SUFFIX)
    if resource == "stone":
        return item_id in STONE_IDS
    if resource in ORE_ITEM_IDS:
        return item_id in ORE_ITEM_IDS[resource]
    if resource == "wheat":
        return item_id == "minecraft:wheat"
    return item_id == resource


class VanillaSkills:
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
            "sleep": self.sleep,
            "resupply": self.resupply,
            "explore": self.explore,
            "combat": self.combat,
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
            if resource not in {"logs", "stone", "wheat"} and resource not in ORE_QUERIES:
                raise SkillError("resource 仅支持原木/石头/小麦和各种原矿")
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
        if item in {"logs", "stone", "wheat"} or item in ORE_ITEM_IDS:
            return sum(int(row.get("count", 0)) for row in self.inventory_items(status) if _matches_resource(str(row.get("id", "")), item))
        return sum(int(row.get("count", 0)) for row in self.inventory_items(status) if str(row.get("id", "")) == item)

    async def _pickup_near(self, x, y, z, resource, radius=5):
        data = await self._call("scan_entities", {"radius": radius, "limit": 32}, timeout=10)
        rows = [row for row in (data.get("entities") or []) if isinstance(row.get("item"), dict)]
        for row in rows:
            item_id = str((row.get("item") or {}).get("id") or "")
            if not _matches_resource(item_id, resource):
                continue
            try:
                if _num(row.get("distance"), 99) > 2.2:
                    error = await self.plugin._approach_target(row.get("x", x), row.get("y", y), row.get("z", z), seconds=20, reach=1.8)
                    if error:
                        continue
                try:
                    await self._run("pickup", {"item": item_id, "entity_uuid": str(row.get("uuid") or "")}, seconds=4)
                except Exception:
                    continue
            except Exception:
                continue

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
    async def _gather_scan_fallback(self, resource, radius):
        """Raw ore scan fallback when the normal exposed-resource scan is empty."""
        query = ORE_QUERIES.get(resource)
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
        if not query:
            return None
        scan = await self._call("scan_blocks", {"radius": radius, "limit": 16, "block": query}, timeout=15)
        rows = [row for row in (scan.get("blocks") or []) if isinstance(row, dict)]
        return min(rows, key=lambda row: _num(row.get("distance"), 999)) if rows else None

    async def gather(self, resource="logs", count=8, radius=12, max_seconds=180, allow_night=False):
        resource = RESOURCE_ALIASES.get(str(resource).strip().lower(), str(resource).strip().lower())
        if resource not in {"logs", "stone", "wheat"} and resource not in ORE_QUERIES:
            raise SkillError("只支持原木、石头、小麦和各种原矿")
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
                    failed += 1
                    if failed >= 5:
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
                failed += 1
                if failed >= 5:
                    raise SkillError("挖掘连续失败：" + str(exc))
                continue
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
            remaining = target
            while remaining > 0:
                batch_before = await self._count(item)
                await self._run("craft", {"item": item, "count": min(64, remaining)}, seconds=15)
                after = await self._count(item)
                gained = after - batch_before
                if gained <= 0:
                    raise SkillError(f"合成没有确认入包：{item}")
                remaining = target - (after - before)
                self._mark("craft", "crafting", after - before, target, {"item": item})
            return {"skill": "craft_recipe", "item": item, "requested": target, "produced": await self._count(item) - before}
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

    async def _ensure_table(self):
        if await self._crafting_screen():
            return False
        scan = await self._call("scan", {"resource": "logs", "radius": 12}, timeout=15)
        tables = [row for row in (scan.get("workbenches") or []) if isinstance(row, dict)]
        for table in sorted(tables, key=lambda row: _num(row.get("distance"), 999))[:2]:
            stands = [row for row in (table.get("stands") or []) if isinstance(row, dict)]
            if stands:
                stand = min(stands, key=lambda row: _num(row.get("distance"), 999))
                error = await self.plugin._approach_target(stand["x"], stand.get("feet_y", stand["y"]), stand["z"], seconds=20, reach=1.5)
                if error:
                    continue
            opened = await self.plugin._call("container", {"operation": "open", "x": table["x"], "y": table["y"], "z": table["z"]}, timeout=10)
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
            await self.ensure_item("minecraft:crafting_table", 1, depth=1)
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

    async def ensure_item(self, item, need, depth=0):
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
            await self.ensure_item(str(chosen.get("id") or ""), max(1, int(chosen.get("count", 1) or 1)) * batches, depth + 1)
        width = int(recipe.get("width", 2) or 2)
        height = int(recipe.get("height", 2) or 2)
        if width > 2 or height > 2:
            await self._ensure_table()
        remaining = need - have
        while remaining > 0:
            before = await self._count(item)
            await self.craft_recipe(item, min(64, remaining))
            after = await self._count(item)
            if after <= before:
                raise SkillError(f"合成没有确认入包：{item}")
            remaining = need - after
        return await self._count(item)

    async def craft(self, item, count=1):
        item = str(item or "").strip()
        if not re.fullmatch(r"(?:[a-z0-9_]+:)?[a-z0-9_][a-z0-9_./-]*", item):
            raise SkillError("物品ID无效")
        item = item if ":" in item else "minecraft:" + item
        target = max(1, min(64, int(count)))
        before = await self._count(item)
        await self.ensure_item(item, before + target, depth=0)
        after = await self._count(item)
        return {"skill": "craft", "item": item, "requested": target, "produced": max(0, after - before)}

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
        occupied = False
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
            occupied = bool(input_slot.get("id") or fuel_slot.get("id") or output.get("id"))
            if output.get("id") == item and int(output.get("count", 0) or 0) > 0:
                await self._take_open_container(item, target)
        except SkillError:
            occupied = bool(opened)
        finally:
            if opened:
                try:
                    await self._call("container", {"operation": "close"}, timeout=5)
                except Exception:
                    pass

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
            raise SkillError("熔炉里有未取出的材料或产物，请先清空或稍后再试")

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
    async def farm(self, operation="harvest", radius=8, count=8, replant=True):
        operation = str(operation or "harvest").strip().lower()
        if operation not in {"harvest", "plant", "both"}:
            raise SkillError("operation 只能是 harvest/plant/both")
        radius = max(2, min(12, int(radius)))
        target = max(1, min(32, int(count)))
        harvested = planted = 0
        total_work = target * (2 if operation == "both" else 1)
        self._mark("farm", "farming", 0, total_work, {"operation": operation, "harvested": 0, "planted": 0})
        if operation in {"harvest", "both"}:
            data = await self._call("scan_blocks", {"radius": radius, "limit": 32, "block": "minecraft:wheat"}, timeout=10)
            crops = [row for row in (data.get("blocks") or []) if isinstance(row, dict)]
            for row in crops:
                if harvested >= target:
                    break
                inspect = await self._call("inspect_block", {"x": row["x"], "y": row["y"], "z": row["z"]}, timeout=10)
                if "age=7" not in str(inspect.get("state") or ""):
                    continue
                try:
                    await self._run("mine_at", {"x": row["x"], "y": row["y"], "z": row["z"]}, seconds=10)
                    harvested += 1
                    await self._pickup_near(row["x"], row["y"], row["z"], "wheat")
                    if replant and await self._count("minecraft:wheat_seeds") > 0:
                        try:
                            await self._run("plant", {"x": row["x"], "y": int(row["y"]) - 1, "z": row["z"]}, seconds=6)
                            planted += 1
                        except Exception:
                            pass
                    self._mark("farm", "farming", harvested + planted, total_work,
                               {"operation": operation, "harvested": harvested, "planted": planted})
                except Exception:
                    continue
        if operation in {"plant", "both"} and planted == 0:
            data = await self._call("scan_blocks", {"radius": radius, "limit": 32, "block": "minecraft:farmland"}, timeout=10)
            farms = [row for row in (data.get("blocks") or []) if isinstance(row, dict)]
            for row in farms:
                if planted >= target:
                    break
                if await self._count("minecraft:wheat_seeds") <= 0:
                    break
                try:
                    await self._run("plant", {"x": row["x"], "y": row["y"], "z": row["z"]}, seconds=6)
                    planted += 1
                    self._mark("farm", "farming", harvested + planted, total_work,
                               {"operation": operation, "harvested": harvested, "planted": planted})
                except Exception:
                    continue
        return {"skill": "farm", "operation": operation, "harvested": harvested, "planted": planted}

    # ------------------------------------------------------------------- sleep
    def _memory(self):
        getter = getattr(self.plugin, "_poi_memory_for", None)
        return getter() if callable(getter) else None

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

    async def sleep(self, source="nearby", remember=False):
        return await self.find_and_sleep(source=source, remember=bool(remember))

    # ------------------------------------------------------------------- L2
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
                return False
        return False

    async def _dig_to_layer(self, target_y, deadline, step=8):
        """Dig a safe 1x1 shaft down toward a target Y, returning progress."""
        digs = 0
        target_y = int(target_y)
        while True:
            if time.monotonic() >= deadline:
                raise SkillError("向下挖掘到矿层超时")
            status = await self.status()
            position = status.get("position") or {}
            try:
                y = int(math.floor(float(position.get("y", 0))))
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
                # A cave, fluid, slab/stairs, or protected block stops the
                # shaft.  Scan from the current depth instead of forcing it.
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

    async def craft_item(self, item, count=1):
        result = await self.craft(item, count)
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

    async def return_home(self, reach=2.0):
        status = await self.status()
        target = self._home_target(status)
        if not target:
            raise SkillError("没有记录家床")
        if target.get("world_key") and target["world_key"] != status.get("world_key"):
            raise SkillError("家床记录属于另一个世界")
        if target.get("dimension") and target["dimension"] != status.get("dimension"):
            raise SkillError("家床记录属于另一个维度")
        reach = max(0.5, min(8.0, float(reach)))
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

        if direct_error is not None and tunnel_error is not None:
            raise SkillError(f"回家失败：{direct_error}；隧道失败：{tunnel_error}") from tunnel_error
        if direct_error is not None:
            raise direct_error
        if tunnel_error is not None:
            raise tunnel_error
        raise SkillError("回家失败：目标太远且无法直接到达")

    # ---------------------------------------------------------------- resupply
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

    async def resupply(self, food=8, torches=8, blocks=32, tools=True, max_seconds=300):
        food_target = max(0, min(64, int(food)))
        torch_target = max(0, min(64, int(torches)))
        block_target = max(0, min(256, int(blocks)))
        deadline = time.monotonic() + max(30, min(900, int(max_seconds)))
        actions, shortages = [], []

        async def snapshot():
            status = await self.status()
            items = self.inventory_items(status)
            safe_food = sum(int(row.get("count", 0)) for row in items if row.get("safe_food") is True)
            torch_count = self.item_count(status, "minecraft:torch")
            block_count = self.item_count(status, "minecraft:cobblestone")
            tool_ids = [str(row.get("id", "")) for row in items]
            tool_ok = all(any(tool_rank(item_id) >= 3 and item_id.endswith(suffix) for item_id in tool_ids) for suffix in TOOL_SUFFIXES)
            return {"food": safe_food, "torches": torch_count, "cobblestone": block_count, "tools_ok": tool_ok}

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
            safe_food = sum(int(row.get("count", 0)) for row in self.inventory_items(status) if row.get("safe_food") is True)
            missing = max(0, food_target - safe_food)
            if missing:
                try:
                    await self.craft("minecraft:bread", missing)
                    actions.append("craft:bread")
                except SkillError as exc:
                    shortages.append(f"食物: {exc}")
        after = await snapshot()
        complete = (
            after["food"] >= food_target
            and after["torches"] >= torch_target
            and after["cobblestone"] >= block_target
            and (not tools or after["tools_ok"])
        )
        return {
            "skill": "resupply",
            "before": before,
            "after": after,
            "targets": {"food": food_target, "torches": torch_target, "cobblestone": block_target, "tools": bool(tools)},
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
            if goal in ORE_QUERIES:
                for row in await self._gather_scan_fallback(goal, 12):
                    add_found(str(row.get("block") or goal), row, str(row.get("block") or goal))
            if goal in {"generic", "village"}:
                entities = await self._call("scan_entities", {"radius": 16, "limit": 32}, timeout=10)
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
        while time.monotonic() < deadline:
            status = await self.status()
            health = _num(status.get("health"), 20)
            position = status.get("position") if isinstance(status.get("position"), dict) else {}
            if anchor is None and position:
                anchor = dict(position)
            hostiles = [row for row in (status.get("hostiles") or []) if isinstance(row, dict)]
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
            if _num(hostile.get("distance"), 99) > 3.2:
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
