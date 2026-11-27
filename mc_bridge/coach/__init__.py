"""Offline RL-inspired Coach subsystem (Reward + Critic + Playbook + Bandit).

Design principles:
  - Always asynchronous / offline: never blocks real-time Minecraft execution.
  - Privacy-enforced (A08-M): all stored text passes through ``privacy.redact_text``.
  - Bug isolation: ``code_or_env_bug`` goes to ``code_issues``, never ``lessons``.
  - Bounded context: at most Top-K active lessons injected per turn, with
    automatic win-rate decay and retirement for underperforming lessons.
"""
from __future__ import annotations

from .critic import CoachEngine
from .reward import RewardBreakdown, compute_reward
from .store import CoachStore

__all__ = ["CoachEngine", "CoachStore", "RewardBreakdown", "compute_reward"]
