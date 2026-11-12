import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_base import m, make_plugin


class PlanHarness:
    """Small deterministic platform for plan-continuity scenarios."""

    def __init__(self, outcomes=None):
        self.plugin = make_plugin()
        self.plugin._task_chat = AsyncMock()
        self.plugin._skills_for = lambda: SimpleNamespace(run=self._run_skill)
        self.calls = []
        self.outcomes = outcomes or {}

    async def _run_skill(self, opcode, params):
        self.calls.append((str(opcode), dict(params or {})))
        outcome = self.outcomes.get(str(opcode))
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "fail":
            raise m.SkillError(f"{opcode} failed")
        return {"skill": opcode, "ok": True}

    async def run_plan(self, steps):
        item = m.QueuedTask(
            kind="plan", goal="test plan", target="plan",
            params={"steps": steps}, task_id="plan-test",
        )
        result = await self.plugin._run_plan_task(item)
        return item, result


class PlanContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_runs_steps_in_order(self):
        harness = PlanHarness()
        item, result = await harness.run_plan([
            {"opcode": "mine_ore", "params": {"resource": "lapis", "count": 1}},
            {"opcode": "return_home", "params": {}},
        ])
        self.assertEqual([call[0] for call in harness.calls], ["mine_ore", "return_home"])
        self.assertEqual(result["goal_outcome"], "fulfilled")
        self.assertEqual(result["completed"], 2)
        self.assertEqual(item.checkpoint["plan"]["next"], 2)
        harness.plugin._task_chat.assert_awaited_once()

    async def test_required_step_failure_stops_following_steps(self):
        harness = PlanHarness({"mine_ore": "fail"})
        item, result = await harness.run_plan([
            {"opcode": "mine_ore", "params": {"resource": "lapis", "count": 1}},
            {"opcode": "return_home", "params": {}},
        ])
        self.assertEqual([call[0] for call in harness.calls], ["mine_ore"])
        self.assertEqual(result["goal_outcome"], "unmet")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(item.checkpoint["plan"]["next"], 0)

    async def test_optional_failure_continues_to_return_home(self):
        harness = PlanHarness({"mine_ore": "fail"})
        item, result = await harness.run_plan([
            {
                "opcode": "mine_ore",
                "params": {"resource": "lapis", "count": 1},
                "continue_on_failure": True,
            },
            {"opcode": "return_home", "params": {}},
        ])
        self.assertEqual([call[0] for call in harness.calls], ["mine_ore", "return_home"])
        self.assertEqual(result["goal_outcome"], "partial")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(item.checkpoint["plan"]["next"], 2)
        harness.plugin._task_chat.assert_awaited_once()

    async def test_empty_plan_is_rejected(self):
        harness = PlanHarness()
        with self.assertRaises(m.SkillError):
            await harness.run_plan([])

    async def test_client_reconnect_resumes_remaining_plan_steps(self):
        plugin = make_plugin()
        plugin._interrupted_task = {
            "opcode": "plan",
            "params": {
                "steps": [
                    {"opcode": "mine_ore", "params": {"resource": "lapis", "count": 1}},
                    {"opcode": "return_home", "params": {}},
                ],
                "break_budget": 1280,
            },
            "priority": 10,
            "route": None,
            "checkpoint": {"plan": {"next": 1, "results": []}},
            "at": time.time(),
        }
        plugin._resume_attempts = 0
        plugin._call = AsyncMock(return_value={"health": 20, "hostiles": []})
        plugin._enqueue_plan = Mock(return_value="plan-resume")
        plugin._task_chat = AsyncMock()
        await plugin._client_ready({})
        plugin._enqueue_plan.assert_called_once()
        steps = plugin._enqueue_plan.call_args.args[0]
        self.assertEqual([step["opcode"] for step in steps], ["return_home"])


if __name__ == "__main__":
    unittest.main()
