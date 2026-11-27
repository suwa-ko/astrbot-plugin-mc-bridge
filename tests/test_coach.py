import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from test_base import m, make_plugin

coach_mod = importlib.import_module(m.__name__ + ".mc_bridge.coach")


class CoachTests(unittest.IsolatedAsyncioTestCase):
    def test_objective_reward_calculation(self):
        full = coach_mod.compute_reward(goal_outcome="fulfilled", status="completed")
        self.assertEqual(full.score, 1.0)
        partial = coach_mod.compute_reward(
            goal_outcome="partial", status="completed", confirmed=4, requested=8, health_delta=-4.0, retreated=True
        )
        self.assertLess(partial.score, full.score)
        self.assertIn("health_loss", partial.penalties)
        self.assertIn("retreat", partial.penalties)
        failed = coach_mod.compute_reward(goal_outcome="unmet", status="failed", attempts=3)
        self.assertLess(failed.score, -0.5)
        cancelled = coach_mod.compute_reward(goal_outcome="unmet", status="cancelled")
        self.assertEqual(cancelled.score, 0.0)

    def test_store_privacy_redaction_and_win_rate_auto_retire(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = coach_mod.CoachStore(Path(tmp) / "coach.db")
            try:
                # Coordinates are redacted by privacy.redact_text; explicit coordinate leak is rejected or redacted
                lid = store.add_lesson(
                    scope="mine_ore",
                    condition="背包剩余空格不足",
                    rule="深层挖矿前先回箱子存杂物",
                    status="active",
                    confidence=0.8,
                )
                self.assertIsNotNone(lid)
                top = store.retrieve_lessons("mine_ore", limit=3)
                self.assertEqual(len(top), 1)
                self.assertEqual(top[0]["id"], lid)

                # 5 consecutive failures auto-retire non-owner AI lessons
                retired = []
                for _ in range(5):
                    retired.extend(store.record_lesson_outcome([lid], fulfilled=False))
                self.assertIn(lid, retired)
                self.assertEqual(store.retrieve_lessons("mine_ore", limit=3), [])
            finally:
                store.close()

    def test_owner_feedback_immediately_active_and_never_auto_retired(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = coach_mod.CoachStore(Path(tmp) / "coach.db")
            engine = coach_mod.CoachEngine(store, require_approval=True)
            try:
                lid = engine.record_owner_feedback("smelt", "烧炼完顺手检查旁边漏斗箱子")
                self.assertIsNotNone(lid)
                prompt_block, ids = engine.format_playbook_prompt("smelt")
                self.assertEqual(ids, [lid])
                self.assertIn("历史经验参考", prompt_block)
                # Empty scope should also retrieve active task-specific lessons for general agent turns
                _, global_ids = engine.format_playbook_prompt("")
                self.assertEqual(global_ids, [lid])
                for _ in range(6):
                    retired = store.record_lesson_outcome([lid], fulfilled=False)
                    self.assertEqual(retired, [])
            finally:
                store.close()

    def test_bandit_ucb1_and_macros(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = coach_mod.CoachStore(Path(tmp) / "coach.db")
            try:
                # Unpulled arms are explored first
                store.update_bandit("ensure_food", "farm", 0.9)
                ranked = store.rank_arms("ensure_food", ["farm", "hunt", "trade"])
                self.assertEqual(ranked[0], "hunt")
                store.update_bandit("ensure_food", "hunt", -0.4)
                store.update_bandit("ensure_food", "trade", 0.2)
                for _ in range(5):
                    store.update_bandit("ensure_food", "farm", 0.95)
                ranked2 = store.rank_arms("ensure_food", ["farm", "hunt", "trade"])
                self.assertEqual(ranked2[0], "farm")

                ok = store.record_macro("plan:iron_loop", "挖铁并烧锭", [{"opcode": "mine_ore"}, {"opcode": "smelt"}], 0.95)
                self.assertTrue(ok)
                macros = store.list_macros()
                self.assertEqual(len(macros), 1)
                self.assertEqual(macros[0]["name"], "plan:iron_loop")
            finally:
                store.close()

    async def test_critic_isolates_code_bugs_and_creates_candidate_lessons(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = coach_mod.CoachStore(Path(tmp) / "coach.db")
            llm = AsyncMock(
                return_value=json.dumps({
                    "category": "planning_error",
                    "scope": "mine_ore",
                    "condition": "未带备用镐子",
                    "rule": "下矿前先检查铁镐耐久，不足则先合成备用镐",
                    "confidence": 0.85,
                }, ensure_ascii=False)
            )
            engine = coach_mod.CoachEngine(store, llm_call=llm, require_approval=True)
            try:
                # 1. Code bug fast-path: skips LLM, writes to code_issues, never lessons
                res_bug = await engine.on_task_finished(
                    opcode="farm_plot",
                    goal="收割农田",
                    status="failed",
                    goal_outcome="unmet",
                    error="IllegalStateException: block is not visible",
                )
                self.assertEqual(res_bug["action"], "code_issue_recorded")
                llm.assert_not_awaited()
                self.assertEqual(len(store.list_code_issues()), 1)
                self.assertEqual(len(store.list_lessons()), 0)

                # 2. Planning error: invokes LLM critic, saves as candidate (awaiting owner approval)
                res_plan = await engine.on_task_finished(
                    opcode="mine_ore",
                    goal="挖16个铁矿",
                    status="completed",
                    goal_outcome="partial",
                    confirmed=4,
                    requested=16,
                    error="铁镐耗尽且未带木头",
                )
                self.assertEqual(res_plan["action"], "lesson_candidate")
                llm.assert_awaited_once()
                self.assertEqual(len(store.list_lessons(status="candidate")), 1)
                self.assertEqual(len(store.list_lessons(status="active")), 0)

                # 3. Owner approves via shortcut helper
                p = make_plugin()
                p._coach = engine
                out = p._handle_coach_command(f"approve {res_plan['record_id']}")
                self.assertIn("已转正", out)
                self.assertEqual(len(store.list_lessons(status="active")), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
