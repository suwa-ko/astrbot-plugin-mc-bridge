"""Unit tests for P2 upgrades (v2.2.0): Coach auto-trial/stats & Nether/End/modded-container safety."""
from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path

from test_base import m
from test_skills import FakePlugin

skills_mod = importlib.import_module(m.__name__ + ".skills")
coach_store_mod = importlib.import_module(m.__name__ + ".mc_bridge.coach.store")
coach_critic_mod = importlib.import_module(m.__name__ + ".mc_bridge.coach.critic")


class P2UpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_coach_trial_auto_promotes_on_strong_win_rate_and_retires_weak(self):
        with tempfile.TemporaryDirectory() as td:
            store = coach_store_mod.CoachStore(Path(td) / "coach.db")
            try:
                good_id = store.add_lesson(
                    scope="mine_ore",
                    condition="遇到水体",
                    rule="先绕开水体再下挖",
                    status="trial",
                    confidence=0.85,
                )
                bad_id = store.add_lesson(
                    scope="gather",
                    condition="无目标",
                    rule="胡乱原地重试",
                    status="trial",
                    confidence=0.80,
                )
                # Trial lesson is eligible for retrieval (up to 1 trial slot).
                picked = store.retrieve_lessons("mine_ore", limit=3)
                self.assertEqual([r["id"] for r in picked], [good_id])

                # 3/3 fulfilled auto-promotes trial -> active.
                for _ in range(3):
                    store.record_lesson_outcome([good_id], fulfilled=True)
                active_ids = [r["id"] for r in store.list_lessons(status="active")]
                self.assertIn(good_id, active_ids)

                # 0/2 fulfilled auto-retires trial -> retired.
                store.record_lesson_outcome([bad_id], fulfilled=False)
                retired = store.record_lesson_outcome([bad_id], fulfilled=False)
                self.assertIn(bad_id, retired)
            finally:
                store.close()

    async def test_coach_engine_auto_trial_and_stats_digest(self):
        with tempfile.TemporaryDirectory() as td:
            store = coach_store_mod.CoachStore(Path(td) / "coach.db")
            try:
                async def fake_llm(_sys, _usr):
                    return json.dumps({
                        "category": "planning_error",
                        "scope": "mine_ore",
                        "condition": "背包快满",
                        "rule": "提前清理杂石再继续深层掘进",
                        "confidence": 0.85,
                    }, ensure_ascii=False)

                engine = coach_critic_mod.CoachEngine(
                    store, llm_call=fake_llm, require_approval=True, auto_trial=True
                )
                res = await engine.on_task_finished(
                    opcode="mine_ore",
                    goal="挖钻石",
                    status="completed",
                    goal_outcome="partial",
                    confirmed=10,
                    requested=32,
                )
                self.assertEqual(res["action"], "lesson_trial")
                digest = engine.format_digest(
                    metrics_snapshot={
                        "overall": {"total": 100, "success_rate": 0.72, "goal_fulfilled_rate": 0.65},
                        "alerts": [{"message": "mine_ore 最近失败偏高"}],
                    }
                )
                self.assertIn("灰度试用", digest)
                self.assertIn("任务仪表盘：总计 100 次", digest)
                self.assertIn("mine_ore 最近失败偏高", digest)
            finally:
                store.close()

    async def test_sleep_refused_in_nether_and_end(self):
        for dim in ("minecraft:the_nether", "minecraft:the_end"):
            class DimPlugin(FakePlugin):
                async def _call(self, action, params=None, timeout=10):
                    data = await super()._call(action, params, timeout)
                    if action == "status":
                        data["dimension"] = dim
                    return data

            with self.assertRaisesRegex(skills_mod.SkillError, "床会爆炸"):
                await skills_mod.VanillaSkills(DimPlugin()).find_and_sleep()

    async def test_mine_ore_blocks_nether_overworld_ores_and_end_void(self):
        class NetherPlugin(FakePlugin):
            async def _call(self, action, params=None, timeout=10):
                data = await super()._call(action, params, timeout)
                if action == "status":
                    data["dimension"] = "minecraft:the_nether"
                    data["inventory_items"] = [
                        {"id": "minecraft:iron_pickaxe", "count": 1, "slot": 0, "damage": 0, "max_damage": 250}
                    ]
                return data

        with self.assertRaisesRegex(skills_mod.SkillError, "下界不生成 diamond"):
            await skills_mod.VanillaSkills(NetherPlugin()).mine_ore("diamond", 4)

        class EndPlugin(NetherPlugin):
            async def _call(self, action, params=None, timeout=10):
                data = await super()._call(action, params, timeout)
                if action == "status":
                    data["dimension"] = "minecraft:the_end"
                return data

        with self.assertRaisesRegex(skills_mod.SkillError, "末地虚空"):
            await skills_mod.VanillaSkills(EndPlugin()).mine_ore("gold", 4)

    async def test_nether_gold_armor_auto_equips_and_modded_containers_scanned(self):
        class NetherGearPlugin(FakePlugin):
            async def _call(self, action, params=None, timeout=10):
                data = await super()._call(action, params, timeout)
                if action == "status":
                    data["dimension"] = "minecraft:the_nether"
                    data["inventory_items"] = [
                        {"id": "minecraft:golden_boots", "count": 1, "slot": 4},
                    ]
                return data

        plugin = NetherGearPlugin()
        skills = skills_mod.VanillaSkills(plugin)
        self.assertTrue(await skills._ensure_nether_gold_armor())
        self.assertTrue(any(act == "equip" and p.get("item") == "minecraft:golden_boots" for act, p in plugin.runs))

        await skills._food_containers(radius=12)
        scan_calls = [p for act, p in plugin.calls if act == "scan_blocks"]
        self.assertTrue(scan_calls)
        self.assertIn("cabinet", scan_calls[-1].get("block", ""))
        self.assertIn("item_vault", scan_calls[-1].get("block", ""))


if __name__ == "__main__":
    unittest.main()
