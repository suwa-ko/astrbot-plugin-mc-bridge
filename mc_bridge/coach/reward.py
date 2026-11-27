"""Deterministic objective reward calculation from verified task telemetry."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RewardBreakdown:
    score: float
    outcome_score: float
    penalties: dict[str, float] = field(default_factory=dict)
    summary: str = ""


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, round(float(value), 4)))


def compute_reward(
    *,
    goal_outcome: str = "",
    status: str = "",
    confirmed: int = 0,
    requested: int = 0,
    elapsed_seconds: float = 0.0,
    budget_seconds: float = 0.0,
    health_delta: float = 0.0,
    retreated: bool = False,
    attempts: int = 1,
    error: str = "",
) -> RewardBreakdown:
    """Compute a bounded objective reward in [-1.0, 1.0] from real outcomes."""
    outcome = str(goal_outcome or "").strip().lower()
    st = str(status or "").strip().lower()

    if st == "cancelled":
        return RewardBreakdown(score=0.0, outcome_score=0.0, penalties={}, summary="cancelled")

    if outcome == "fulfilled":
        base = 1.0
    elif outcome == "partial":
        if requested > 0 and confirmed > 0:
            ratio = max(0.0, min(1.0, float(confirmed) / float(requested)))
            base = 0.1 + 0.6 * ratio
        else:
            base = 0.3
    elif outcome in {"unmet", "failed"} or st == "failed":
        base = -0.5
    else:
        base = 0.2 if st == "completed" and not error else -0.2

    penalties: dict[str, float] = {}
    if budget_seconds > 0 and elapsed_seconds > budget_seconds:
        overtime_ratio = min(1.0, (elapsed_seconds - budget_seconds) / budget_seconds)
        penalties["overtime"] = round(0.2 * overtime_ratio, 4)
    if health_delta < 0:
        # Negative health_delta means HP lost (out of 20 max HP).
        penalties["health_loss"] = round(min(0.35, abs(health_delta) / 20.0 * 0.5), 4)
    if retreated:
        penalties["retreat"] = 0.15
    if attempts > 1:
        penalties["retries"] = round(min(0.25, 0.08 * (attempts - 1)), 4)

    total_penalty = sum(penalties.values())
    final = _clamp(base - total_penalty)
    parts = [f"outcome={outcome or st or 'unknown'}({base:+.2f})"]
    for k, v in penalties.items():
        parts.append(f"-{k}({v:.2f})")
    parts.append(f"=> {final:+.2f}")
    return RewardBreakdown(score=final, outcome_score=round(base, 4), penalties=penalties, summary=" ".join(parts))
