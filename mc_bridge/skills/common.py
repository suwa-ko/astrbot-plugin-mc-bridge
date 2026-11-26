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

from ...core import ActionError
from ...farm_memory import MAX_CELLS


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
    "沙子": "sand", "沙": "sand", "sand": "sand",
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
CROP_SPECS = {
    "wheat": {
        "block": "minecraft:wheat",
        "seed": "minecraft:wheat_seeds",
        "item": "minecraft:wheat",
        "age": 7,
    },
    "carrots": {
        "block": "minecraft:carrots",
        "seed": "minecraft:carrot",
        "item": "minecraft:carrot",
        "age": 7,
    },
    "potatoes": {
        "block": "minecraft:potatoes",
        "seed": "minecraft:potato",
        "item": "minecraft:potato",
        "age": 7,
    },
    "beetroots": {
        "block": "minecraft:beetroots",
        "seed": "minecraft:beetroot_seeds",
        "item": "minecraft:beetroot",
        "age": 3,
    },
}
CROP_BLOCKS = {spec["block"]: name for name, spec in CROP_SPECS.items()}
FOOD_NUTRITION = {
    "minecraft:bread": 5,
    "minecraft:baked_potato": 5,
    "minecraft:cooked_beef": 8,
    "minecraft:cooked_porkchop": 8,
    "minecraft:cooked_chicken": 6,
    "minecraft:cooked_mutton": 6,
    "minecraft:cooked_rabbit": 5,
    "minecraft:cooked_cod": 5,
    "minecraft:cooked_salmon": 6,
    "minecraft:carrot": 3,
    "minecraft:potato": 1,
    "minecraft:beetroot": 1,
    "minecraft:apple": 4,
    "minecraft:golden_carrot": 6,
    "minecraft:melon_slice": 2,
    "minecraft:sweet_berries": 2,
    "minecraft:pumpkin_pie": 8,
    "minecraft:cookie": 2,
    "genshin_nature:fowl": 2,
    "genshin_nature:cookedfowl": 6,
    "genshin_nature:rawmeat": 2,
    "genshin_nature:cookedmeat": 6,
    "genshin_nature:chilledmeat": 2,
    "genshin_nature:unagimeat": 2,
    "genshin_nature:cookedunagimeat": 6,
}
RAW_FOOD_COOKING = {
    "minecraft:beef": "minecraft:cooked_beef",
    "minecraft:porkchop": "minecraft:cooked_porkchop",
    "minecraft:chicken": "minecraft:cooked_chicken",
    "minecraft:mutton": "minecraft:cooked_mutton",
    "minecraft:rabbit": "minecraft:cooked_rabbit",
    "minecraft:potato": "minecraft:baked_potato",
    "minecraft:cod": "minecraft:cooked_cod",
    "minecraft:salmon": "minecraft:cooked_salmon",
    "genshin_nature:chilledmeat": "genshin_nature:rawmeat",
    "genshin_nature:rawmeat": "genshin_nature:cookedmeat",
    "genshin_nature:fowl": "genshin_nature:cookedfowl",
    "genshin_nature:unagimeat": "genshin_nature:cookedunagimeat",
}
HUNTABLE = {
    "minecraft:cow": "minecraft:beef",
    "minecraft:pig": "minecraft:porkchop",
    "minecraft:sheep": "minecraft:mutton",
    "minecraft:chicken": "minecraft:chicken",
    "minecraft:rabbit": "minecraft:rabbit",
    "genshin_nature:boar": "genshin_nature:rawmeat",
    "genshin_nature:shroomboar": "genshin_nature:rawmeat",
    "genshin_nature:snowboar": "genshin_nature:chilledmeat",
    "genshin_nature:azurecrane": "genshin_nature:fowl",
    "genshin_nature:scarletbeakduck": "genshin_nature:fowl",
    "genshin_nature:emeraldduck": "genshin_nature:fowl",
    "genshin_nature:violetibis": "genshin_nature:fowl",
    "genshin_nature:crow": "genshin_nature:fowl",
    "genshin_nature:squirrel": "genshin_nature:rawmeat",
    "genshin_nature:snowweasel": "genshin_nature:rawmeat",
    "genshin_nature:redtailedweasel": "genshin_nature:rawmeat",
    "genshin_nature:bootweasel": "genshin_nature:rawmeat",
    "genshin_nature:adornedunagi": "genshin_nature:unagimeat",
    "genshin_nature:redfinnedunagi": "genshin_nature:unagimeat",
    "genshin_nature:deepseaunagi": "genshin_nature:unagimeat",
}
HUNT_ALIASES = {
    "cow": "minecraft:cow", "pig": "minecraft:pig", "sheep": "minecraft:sheep",
    "chicken": "minecraft:chicken", "rabbit": "minecraft:rabbit",
    "boar": "genshin_nature:boar", "shroomboar": "genshin_nature:shroomboar",
    "snowboar": "genshin_nature:snowboar", "azurecrane": "genshin_nature:azurecrane",
    "crane": "genshin_nature:azurecrane",
    "duck": "genshin_nature:scarletbeakduck",
    "scarletbeakduck": "genshin_nature:scarletbeakduck",
    "emeraldduck": "genshin_nature:emeraldduck",
    "violetibis": "genshin_nature:violetibis", "ibis": "genshin_nature:violetibis",
    "crow": "genshin_nature:crow", "squirrel": "genshin_nature:squirrel",
    "snowweasel": "genshin_nature:snowweasel",
    "redtailedweasel": "genshin_nature:redtailedweasel",
    "bootweasel": "genshin_nature:bootweasel",
    "unagi": "genshin_nature:adornedunagi",
    "adornedunagi": "genshin_nature:adornedunagi",
    "redfinnedunagi": "genshin_nature:redfinnedunagi",
    "deepseaunagi": "genshin_nature:deepseaunagi",
}
DIGGABLE_GROUND = {
    "dirt", "grass_block", "podzol", "coarse_dirt", "rooted_dirt", "mud", "clay",
    "sand", "gravel", "stone", "cobblestone", "deepslate", "cobbled_deepslate",
    "tuff", "calcite", "andesite", "diorite", "granite", "netherrack",
    "soul_sand", "soul_soil", "basalt", "blackstone",
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
    if resource == "sand":
        return item_id in {"minecraft:sand", "minecraft:red_sand"}
    return item_id == resource


