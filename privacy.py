"""Small, dependency-free redaction helpers for the strict memory boundary.

The Playbook A08-M rule is intentionally narrower than "never show a coordinate
to the model": live gameplay tools still need coordinates for a current action.
What must never happen is that player coordinates, world identifiers or raw
private-chat text are written to LivingMemory, embedded, reranked, or returned
from a memory query.

This module only handles text redaction.  Structured coordinate storage stays in
``memory_store`` and is never serialised into a model-facing view.
"""
from __future__ import annotations

import re

# A coordinate triple such as ``-375 94 -154``, ``-375,94,-154`` or
# ``x=-375 y=94 z=-154``.  The look-around keeps ordinary decimal numbers and
# dates (``2026-11-09``) untouched.
_COORD_SEPARATED = re.compile(
    r"(?<![\w.])(?P<a>-?\d{1,7})\s*(?:[,/]\s*|\s+)(?P<b>-?\d{1,7})"
    r"\s*(?:[,/]\s*|\s+)(?P<c>-?\d{1,7})(?![\w.])"
)
_COORD_DICT = re.compile(
    r"(?i)[\{\(\[]?\s*['\"]?x['\"]?\s*[:=]\s*-?\d{1,7}\s*[,; ]+\s*"
    r"['\"]?y['\"]?\s*[:=]\s*-?\d{1,7}\s*[,; ]+\s*"
    r"['\"]?z['\"]?\s*[:=]\s*-?\d{1,7}\s*[\}\)\]]?"
)
_COORD_LABELLED = re.compile(
    r"(?i)(?:\bx\s*[:=]\s*-?\d{1,7}\s*[,; ]*\s*"
    r"\by\s*[:=]\s*-?\d{1,7}\s*[,; ]*\s*"
    r"\bz\s*[:=]\s*-?\d{1,7})"
)
_WORLD_FIELD = re.compile(
    r"(?i)\b(?:world[_-]?(?:key|id|instance|seed)|server[_-]?(?:id|address)|"
    r"seed)\s*[:=]\s*[^\s,;，；]+"
)
_LONG_HEX = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{16,}(?![0-9a-fA-F])")

_TOPICS = (
    ("移动", ("去", "过来", "前往", "goto", "坐标", "传送", "回家", "回来", "跟随")),
    ("采集", ("挖", "矿", "砍", "木", "采集", "收集", "收获", "钓鱼")),
    ("建造", ("建", "盖", "搭", "放", "基地", "房子", "家", "农田", "种植")),
    ("战斗", ("打", "攻击", "怪", "敌人", "战斗", "撤离", "逃跑", "危险")),
    ("存储", ("箱子", "仓库", "容器", "整理", "存取")),
    ("合成", ("合成", "工作台", "熔炉", "烧", "制作")),
    ("查看", ("状态", "位置", "看看", "查看", "检查", "坐标")),
)

# The canonical set of memory kinds.  Kept here so both legacy migration and
# the new store agree, without importing the old JSON module.
LEGACY_KINDS = {"home", "home_bed", "workbench", "furnace", "resource", "village", "danger", "chest"}


def redact_text(text: object, *, world_keys=()) -> str:
    """Return text with coordinates and world identifiers replaced.

    ``world_keys`` lets a caller remove exact world-key values that do not look
    like long hex strings (for example a local save name).
    """
    value = str(text or "")
    if not value:
        return ""
    for key in world_keys or ():
        key = str(key or "")
        if len(key) >= 4:
            value = re.sub(re.escape(key), "[世界]", value)
    value = _WORLD_FIELD.sub("[世界]", value)
    value = _LONG_HEX.sub("[世界]", value)
    value = _COORD_DICT.sub("[坐标]", value)
    value = _COORD_LABELLED.sub("[坐标]", value)
    value = _COORD_SEPARATED.sub("[坐标]", value)
    return value


def contains_coordinate(text: object) -> bool:
    """True when the text still looks like it contains a coordinate triple."""
    value = str(text or "")
    return bool(_COORD_DICT.search(value) or _COORD_LABELLED.search(value)
                or _COORD_SEPARATED.search(value))


def has_world_identifier(text: object) -> bool:
    value = str(text or "")
    return bool(_WORLD_FIELD.search(value) or _LONG_HEX.search(value))


def private_digest(text: object) -> str:
    """Turn private-chat text into a non-verbatim topic digest.

    Strict A08-M forbids the raw private message from reaching an embedding or
    generation request.  The digest keeps only coarse intent, never names,
    numbers, locations or quoted content.
    """
    value = str(text or "")
    topics: list[str] = []
    for label, words in _TOPICS:
        if any(word.lower() in value.lower() for word in words):
            topics.append(label)
    if not topics:
        topics.append("一般对话")
    return "、".join(topics[:4])


def memory_safe_text(text: object, *, source_kind: str = "", world_keys=()) -> str:
    """Make a LivingMemory result safe before it is placed in a model request."""
    value = redact_text(text, world_keys=world_keys)
    if str(source_kind or "").startswith("qq"):
        # Old QQ documents may contain verbatim private chat.  Keep only the
        # coarse topic so the promise holds for historical data too.
        return "旧QQ私聊MC记忆摘要：" + private_digest(value)
    return value
