"""Asynchronous Coach Critic: offline reflection, bug attribution, and prompt injection."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

from ...privacy import redact_text
from .reward import RewardBreakdown, compute_reward
from .store import CoachStore

_CODE_BUG_PATTERNS = re.compile(
    r"Traceback|NameError|AttributeError|TypeError|KeyError|NullPointerException|"
    r"ClassNotFoundException|IllegalStateException|block is not visible|"
    r"plant space is occupied|close the current screen before digging|"
    r"below-feet|InvalidStateError|AssertionError",
    re.I,
)

_COACH_SYSTEM_PROMPT = (
    "你是 Minecraft 生存 Agent 的离线教练（Critic）。"
    "只根据脱敏后的任务摘要做归因，输出严格 JSON（不要 Markdown 代码块）："
    '{"category":"planning_error|param_error|precondition_gap|code_or_env_bug",'
    '"scope":"任务类型或global","condition":"触发条件（简短）","rule":"下次可执行的具体改进规则（<=80字）","confidence":0.7}。'
    "如果是底层插件或 Mod 报错、异常或协议问题，必须归为 code_or_env_bug。"
    "严禁输出任何具体坐标、世界标识或私聊原文。"
)


def _extract_json(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


class CoachEngine:
    """Coordinates objective rewards, offline LLM reflection, owner feedback, and Playbook injection."""

    def __init__(
        self,
        store: CoachStore,
        *,
        llm_call: Callable[[str, str], Awaitable[str]] | None = None,
        require_approval: bool = True,
        max_injected: int = 3,
    ):
        self.store = store
        self.llm_call = llm_call
        self.require_approval = bool(require_approval)
        self.max_injected = max(1, min(5, int(max_injected)))

    @staticmethod
    def looks_like_code_bug(error: str) -> bool:
        return bool(error and _CODE_BUG_PATTERNS.search(str(error)))

    async def on_task_finished(
        self,
        *,
        opcode: str,
        goal: str,
        status: str,
        goal_outcome: str,
        confirmed: int = 0,
        requested: int = 0,
        elapsed_seconds: float = 0.0,
        budget_seconds: float = 0.0,
        health_delta: float = 0.0,
        retreated: bool = False,
        attempts: int = 1,
        error: str = "",
        result_summary: str = "",
        injected_lesson_ids: list[int] | None = None,
        bandit_context: str = "",
        bandit_arm: str = "",
        plan_steps: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Offline post-task hook: score, update win rates/bandit, isolate bugs, optionally reflect."""
        reward: RewardBreakdown = compute_reward(
            goal_outcome=goal_outcome,
            status=status,
            confirmed=confirmed,
            requested=requested,
            elapsed_seconds=elapsed_seconds,
            budget_seconds=budget_seconds,
            health_delta=health_delta,
            retreated=retreated,
            attempts=attempts,
            error=error,
        )
        scope = str(opcode or "global").strip().lower() or "global"
        fulfilled = str(goal_outcome or "").strip().lower() == "fulfilled" and str(status or "").lower() == "completed"

        retired_ids: list[int] = []
        if injected_lesson_ids and str(status or "").lower() != "cancelled":
            retired_ids = await asyncio.to_thread(
                self.store.record_lesson_outcome, injected_lesson_ids, fulfilled=fulfilled
            )

        if bandit_context and bandit_arm and str(status or "").lower() != "cancelled":
            await asyncio.to_thread(self.store.update_bandit, bandit_context, bandit_arm, reward.score)

        # Promote consistently high-reward multi-step plans into reusable macros.
        if scope == "plan" and fulfilled and reward.score >= 0.85 and plan_steps:
            macro_name = f"plan:{re.sub(r'[^a-z0-9_]+', '_', redact_text(goal).lower()).strip('_')[:32] or 'default'}"
            await asyncio.to_thread(self.store.record_macro, macro_name, goal, plan_steps, reward.score)

        action_taken = "scored_only"
        record_id: int | None = None

        # Fast-path deterministic bug isolation (zero LLM cost, never pollutes Playbook).
        if error and self.looks_like_code_bug(error):
            record_id = await asyncio.to_thread(
                self.store.record_code_issue, scope=scope, error_summary=error
            )
            return {
                "reward": reward.score,
                "reward_summary": reward.summary,
                "action": "code_issue_recorded",
                "record_id": record_id,
                "retired_lesson_ids": retired_ids,
            }

        # Invoke LLM Critic only on non-trivial failures / partial results.
        if self.llm_call is not None and str(status or "").lower() != "cancelled" and reward.score < 0.75:
            user_prompt = redact_text(
                f"任务类型: {scope}\n"
                f"目标: {goal[:120]}\n"
                f"状态: {status} / 达成度: {goal_outcome}\n"
                f"奖励: {reward.summary}\n"
                f"结果摘要: {result_summary[:200]}\n"
                f"错误: {error[:200]}"
            )
            try:
                raw = await self.llm_call(_COACH_SYSTEM_PROMPT, user_prompt)
                parsed = _extract_json(raw)
            except Exception:
                parsed = None
            if parsed:
                category = str(parsed.get("category") or "planning_error").strip().lower()
                lesson_scope = str(parsed.get("scope") or scope).strip().lower() or scope
                condition = str(parsed.get("condition") or "").strip()
                rule = str(parsed.get("rule") or "").strip()
                confidence = float(parsed.get("confidence", 0.7) or 0.7)
                if category == "code_or_env_bug":
                    record_id = await asyncio.to_thread(
                        self.store.record_code_issue,
                        scope=lesson_scope,
                        error_summary=rule or error or result_summary,
                    )
                    action_taken = "code_issue_recorded"
                elif rule:
                    lesson_status = "candidate" if self.require_approval else "active"
                    record_id = await asyncio.to_thread(
                        self.store.add_lesson,
                        scope=lesson_scope,
                        condition=condition,
                        rule=rule,
                        category=category,
                        source="coach_llm",
                        status=lesson_status,
                        confidence=confidence,
                    )
                    action_taken = f"lesson_{lesson_status}"

        return {
            "reward": reward.score,
            "reward_summary": reward.summary,
            "action": action_taken,
            "record_id": record_id,
            "retired_lesson_ids": retired_ids,
        }

    def record_owner_feedback(self, scope: str, rule: str, condition: str = "") -> int | None:
        """Direct human-coach correction: immediately active with top trust."""
        return self.store.add_lesson(
            scope=scope or "global",
            condition=condition,
            rule=rule,
            category="owner_feedback",
            source="owner",
            status="active",
            confidence=1.0,
        )

    def format_playbook_prompt(self, scope: str = "", *, limit: int | None = None) -> tuple[str, list[int]]:
        """Format Top-K active lessons as a compact system-prompt appendix."""
        k = self.max_injected if limit is None else max(1, min(5, int(limit)))
        lessons = self.store.retrieve_lessons(scope, limit=k)
        if not lessons:
            return "", []
        lines = ["\n\n[历史经验参考（最多 3 条，须服从安全护栏）]"]
        ids: list[int] = []
        for row in lessons:
            ids.append(int(row["id"]))
            cond = f"当{row['condition']}时：" if row.get("condition") else ""
            lines.append(f"- ({row['scope']}) {cond}{row['rule']}")
        return "\n".join(lines), ids

    def format_digest(self) -> str:
        """Human-readable summary for the owner's `mccoach` shortcut."""
        candidates = self.store.list_lessons(status="candidate", limit=5)
        active = self.store.list_lessons(status="active", limit=5)
        bugs = self.store.list_code_issues(status="open", limit=5)
        lines = [
            f"Coach 状态：生效经验 {len(active)} 条 · 待审经验 {len(candidates)} 条 · 隔离代码问题 {len(bugs)} 条"
        ]
        if candidates:
            lines.append("待审经验（发 mccoach approve <id> 转正 / retire <id> 废弃）：")
            for row in candidates:
                lines.append(f"  #{row['id']} [{row['scope']}] {row['rule']}")
        if active:
            lines.append("生效经验（最近）：")
            for row in active[:3]:
                trials = row["success_after"] + row["fail_after"]
                rate = f" ({row['success_after']}/{trials})" if trials else ""
                lines.append(f"  #{row['id']} [{row['scope']}]{rate} {row['rule']}")
        if bugs:
            lines.append("代码/环境问题队列（未写入经验库）：")
            for b in bugs[:3]:
                lines.append(f"  #{b['id']} [{b['scope']} x{b['occurrences']}] {b['error_summary'][:100]}")
        lines.append("快捷命令：mccoach teach <scope> <规则> | mccoach approve <id> | mccoach retire <id>")
        return "\n".join(lines)
