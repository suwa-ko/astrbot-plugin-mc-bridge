"""Shared execution context variables and constants for mc_bridge."""
from __future__ import annotations

from contextvars import ContextVar

PLUGIN_NAME = "astrbot_plugin_mc_bridge"
CONTROL_EPOCH = ContextVar("mc_control_epoch", default=None)
CURRENT_EVENT = ContextVar("mc_current_event", default=None)
RESOURCE_MENTION = {
    "coal": ("煤", "coal"),
    "iron": ("铁", "iron"),
    "copper": ("铜", "copper"),
    "gold": ("金", "gold"),
    "redstone": ("红石", "redstone"),
    "lapis": ("青金石", "lapis"),
    "diamond": ("钻石", "diamond"),
    "emerald": ("绿宝石", "emerald"),
    "stone": ("石头", "圆石", "石料", "stone"),
    "logs": ("木", "树", "原木", "log", "wood"),
    "wheat": ("小麦", "wheat"),
    "sand": ("沙", "sand"),
}
