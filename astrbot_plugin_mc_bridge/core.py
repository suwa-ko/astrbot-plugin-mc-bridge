"""Shared primitive errors for the bottom-up Minecraft action layer.

The old autonomous survival/skill implementation is intentionally not imported
here.  This module contains only the error type used by primitive action tools.
"""
from __future__ import annotations


class ActionError(RuntimeError):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details


# Compatibility alias for existing primitive tool call sites and tests.  It does
# not imply that the removed autonomous survival module is still active.
SurvivalError = ActionError
