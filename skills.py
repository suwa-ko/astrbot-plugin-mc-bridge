"""L1/L2 survival skills facade.

Domain implementations live under ``mc_bridge/skills/``. This module preserves
100% import compatibility for existing callers and tests.
"""
from __future__ import annotations

from .mc_bridge.skills import *  # noqa: F401,F403
from .mc_bridge.skills.common import (  # noqa: F401
    _matches_resource,
    _num,
    _position,
)
