"""Deterministic, bounded intent classification for Agent task entry.

This is a fallback for common Chinese/English commands.  It never calls an LLM,
never searches the world and never starts a task; it only proposes an L1/L2
opcode and parameters for validation by `mc_task_start`.
"""
from __future__ import annotations

import re

RESOURCE_ALIASES = {
    "煤": "coal", "煤矿": "coal", "煤炭": "coal", "coal": "coal",
    "铁": "iron", "铁矿": "iron", "iron": "iron",
    "石头": "stone", "圆石": "stone", "石料": "stone", "stone": "stone",
    "木头": "logs", "原木": "logs", "树": "logs", "log": "logs", "wood": "logs",
}

ITEM_ALIASES = {
    "木棍": "minecraft:stick",
    "工作台": "minecraft:crafting_table",
    "木镐": "minecraft:wooden_pickaxe",
    "石镐": "minecraft:stone_pickaxe",
    "铁镐": "minecraft:iron_pickaxe",
    "钻石镐": "minecraft:diamond_pickaxe",
    "木剑": "minecraft:wooden_sword",
    "石剑": "minecraft:stone_sword",
    "铁剑": "minecraft:iron_sword",
    "钻石剑": "minecraft:diamond_sword",
    "火把": "minecraft:torch",
    "箱子": "minecraft:chest",
    "面包": "minecraft:bread",
    "铁锭": "minecraft:iron_ingot",
    "金锭": "minecraft:gold_ingot",
    "木板": "minecraft:oak_planks",
    "腐肉": "minecraft:rotten_flesh",
    "小麦": "minecraft:wheat",
    "种子": "minecraft:wheat_seeds",
    "骨粉": "minecraft:bone_meal",
}

COUNT = re.compile(r"(\d+)\s*(?:个|块|根|张|份|组|颗|把|只|次)?")
CHINESE_COUNT = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
CHINESE_COUNT_RE = re.compile(r"([一二两三四五六七八九十]+)\s*(?:个|块|根|张|份|组|颗|把|只|次)")


def _chinese_count(text):
    match = CHINESE_COUNT_RE.search(str(text or ""))
    if not match:
        return None
    value = match.group(1)
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = CHINESE_COUNT.get(left, 1) if left else 1
        ones = CHINESE_COUNT.get(right, 0) if right else 0
        return tens * 10 + ones
    return CHINESE_COUNT.get(value, 1)


def _count(text, default):
    match = COUNT.search(str(text or ""))
    if match:
        return max(1, min(256, int(match.group(1))))
    chinese = _chinese_count(text)
    if chinese is not None:
        return max(1, min(256, int(chinese)))
    return max(1, min(256, int(default)))


def _result(opcode, params, confidence, reason, text):
    return {
        "opcode": opcode,
        "params": dict(params or {}),
        "confidence": round(float(confidence), 2),
        "reason": reason,
        "text": text,
    }


def _none(text, reason="没有匹配到已知任务"):
    return _result("", {}, 0.0, reason, text)


def _item(text, exclude=()):
    value = str(text or "")
    match = re.search(r"minecraft:[a-z0-9_./-]+", value)
    if match:
        return match.group(0)
    excluded = set(exclude)
    for name in sorted(ITEM_ALIASES, key=len, reverse=True):
        if name in excluded:
            continue
        if name in value:
            return ITEM_ALIASES[name]
    return ""


def classify_task(text):
    original = str(text or "").strip()
    body = re.sub(r"^(?:mc|minecraft)\s*[:：]?\s*", "", original, flags=re.I).strip().lower()
    if not body:
        return _none(original, "空文本")

    if re.search(r"(回家|家床|家里).{0,8}(睡|睡觉)|(睡|睡觉).{0,8}(家床|家里)", body):
        return _result("find_and_sleep", {"source": "home"}, 0.94, "回家床睡觉", original)
    if re.search(r"睡|床", body):
        return _result("find_and_sleep", {"source": "nearby"}, 0.82, "附近找床睡觉", original)
    if re.search(r"回家|回基地|回营地|return\s+home", body):
        return _result("return_home", {}, 0.88, "回记录的家", original)
    if re.search(r"补给|备货|补充物资|resupply", body):
        return _result("resupply", {}, 0.9, "补给物资", original)
    if re.search(r"探索|探路|侦察|巡逻|explore", body):
        return _result("explore", {"goal": "generic"}, 0.86, "有界探索", original)
    if re.search(r"收割|收获|种地|农田|农场|farm", body):
        operation = "both" if "种" in body else "harvest"
        return _result("farm", {"operation": operation}, 0.82, "农业任务", original)

    for name in ("煤矿", "煤炭", "煤", "铁矿", "铁", "石头", "圆石", "石料"):
        if name in body and re.search(r"挖|采|矿|mine", body):
            resource = RESOURCE_ALIASES.get(name, "stone")
            params = {"resource": resource, "count": _count(body, 8)}
            if re.search(r"回来|回家|返回|带回|拿回", body):
                params["return_home"] = True
            return _result("mine_ore", params, 0.92, "挖矿", original)

    if re.search(r"砍|原木|木头|树|log|wood", body):
        return _result("gather", {"resource": "logs", "count": _count(body, 8)}, 0.9, "采木", original)
    if re.search(r"石头|圆石|石料|stone", body) and re.search(r"挖|采|collect|mine", body):
        return _result("mine_ore", {"resource": "stone", "count": _count(body, 32)}, 0.86, "采石", original)

    craft = re.search(r"(?:合成|制作|做|craft)\s*(?:一个|一把|一些|一组)?\s*([\u4e00-\u9fffA-Za-z0-9_:-]+)", body)
    if craft:
        item = _item(craft.group(1)) or _item(body)
        if item:
            return _result("craft_item", {"item": item, "count": _count(body, 1)}, 0.82, "合成物品", original)

    if re.search(r"烧|熔炼|smelt", body):
        if "铁" in body:
            item = "minecraft:iron_ingot"
        elif "金" in body:
            item = "minecraft:gold_ingot"
        else:
            item = _item(body)
        if item:
            return _result("smelt_item", {"item": item, "count": _count(body, 1)}, 0.82, "烧炼物品", original)

    if re.search(r"交易|买卖|买|卖|trade", body):
        item = _item(body)
        if item:
            return _result("trade_for", {"item": item, "count": _count(body, 1)}, 0.72, "村民交易", original)

    if re.search(r"打|杀|攻击|揍|combat|attack", body):
        target = re.sub(r"^(?:去|把|帮我|给我)?(?:打|杀|攻击|揍|combat|attack)\s*", "", body)
        target = re.sub(r"^(?:那只|这个|一个|一只)\s*", "", target).strip(" ，。,.!！?？")
        return _result("combat", {"target": target}, 0.75, "战斗目标", original)

    if re.search(r"放进|存入|取出|拿回|搬|容器|箱子|container", body):
        item = _item(body, exclude={"箱子", "容器"})
        if item:
            direction = "from_container" if re.search(r"取出|拿回|取", body) else "to_container"
            return _result("container_transfer", {
                "item": item, "count": _count(body, 1), "direction": direction,
            }, 0.76, "容器搬运", original)

    return _none(original)
