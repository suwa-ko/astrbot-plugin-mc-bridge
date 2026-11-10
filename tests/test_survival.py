import asyncio
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_base import m

survival_mod = importlib.import_module(m.__name__ + ".survival")


class FakeSkills:
    def __init__(self):
        self.eat_until_full = AsyncMock(return_value={"skill": "eat_until_full", "eaten": 2, "food": 15})
        self.combat = AsyncMock(return_value={"skill": "combat", "result": "killed", "enemy": "Zombie"})


class FakePlugin:
    def __init__(self, config=None):
        self.config = {
            "auto_survive": True,
            "auto_survive_mode": "defend",
            "auto_survive_interval": 2,
            "auto_flee_health": 4,
            "auto_fight_health": 14,
            "auto_hold_health": 6,
            "auto_eat_food": 8,
            "auto_eat_health_food": 18,
            "auto_attack_range": 6,
            "auto_fight_max_targets": 2,
            "auto_fight_leash_radius": 12,
            "auto_finish_low_target": True,
            "auto_survive_notify": True,
            "auto_survive_cooldown_seconds": 30,
            "auto_eat_cooldown_seconds": 15,
        }
        self.config.update(config or {})
        self.bridge = SimpleNamespace(connected=True, session_id="session", client_info={"capabilities": "survival_events"})
        self.skills = FakeSkills()
        self._task_current = None
        self._active_task = None
        self._closing = False
        self._auto_flee = AsyncMock(return_value="已确认到达撤离落脚点")
        self._stop_task_runtime = AsyncMock(return_value=None)
        self.notices = []
        self._task_chat = AsyncMock(side_effect=self._record_notice)
        self.status_after = None

    async def _record_notice(self, text, **kwargs):
        self.notices.append((text, kwargs))
        return True

    def _skills_for(self):
        return self.skills

    async def _call(self, action, params=None, timeout=10):
        if action == "status":
            if self.status_after is not None:
                return dict(self.status_after)
            return {"health": 20, "food": 20}
        return {}

    def _survival_notice_route(self):
        return SimpleNamespace(channel="qq", target="default:FriendMessage:1", connection_id="")


def status(**overrides):
    value = {
        "health": 20.0,
        "max_health": 20.0,
        "food": 20.0,
        "screen": "none",
        "survival_busy": False,
        "using_item": False,
        "sleeping": False,
        "on_fire": False,
        "in_water": False,
        "position": {"x": 0, "y": 64, "z": 0},
        "hostiles": [],
    }
    value.update(overrides)
    return value


class SurvivalTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_mode_does_nothing(self):
        plugin = FakePlugin({"auto_survive_mode": "off"})
        supervisor = survival_mod.SurvivalSupervisor(plugin)
        await supervisor.evaluate(status(health=2, hostiles=[{"type": "minecraft:zombie", "name": "Zombie", "distance": 2}]))
        plugin.skills.eat_until_full.assert_not_awaited()
        plugin.skills.combat.assert_not_awaited()
        plugin._auto_flee.assert_not_awaited()
        self.assertEqual(plugin.notices, [])

    async def test_notify_mode_reports_without_action(self):
        plugin = FakePlugin({"auto_survive_mode": "notify"})
        supervisor = survival_mod.SurvivalSupervisor(plugin)
        await supervisor.evaluate(status(health=10, hostiles=[{"type": "minecraft:zombie", "name": "Zombie", "distance": 3}]))
        plugin.skills.eat_until_full.assert_not_awaited()
        plugin.skills.combat.assert_not_awaited()
        plugin._auto_flee.assert_not_awaited()
        self.assertTrue(plugin.notices)
        self.assertIn("仅报告", plugin.notices[0][0])

    async def test_low_health_with_enemy_flees(self):
        plugin = FakePlugin()
        supervisor = survival_mod.SurvivalSupervisor(plugin)
        await supervisor.evaluate(status(health=3, hostiles=[{"type": "minecraft:zombie", "name": "Zombie", "distance": 3}]))
        plugin._auto_flee.assert_awaited_once()
        plugin.skills.combat.assert_not_awaited()
        plugin.skills.eat_until_full.assert_not_awaited()
        self.assertTrue(any("撤离" in text for text, _ in plugin.notices))

    async def test_healthy_enemy_uses_hold_policy(self):
        plugin = FakePlugin()
        supervisor = survival_mod.SurvivalSupervisor(plugin)
        await supervisor.evaluate(status(health=16, hostiles=[{"type": "minecraft:zombie", "name": "Zombie", "distance": 4}]))
        plugin.skills.combat.assert_awaited_once()
        kwargs = plugin.skills.combat.await_args.kwargs
        self.assertEqual(kwargs["retreat_health"], 4)
        self.assertEqual(kwargs["hold_health"], 6)
        self.assertTrue(kwargs["finish_low_target"])
        self.assertEqual(kwargs["max_targets"], 2)
        self.assertEqual(kwargs["leash_radius"], 12)
        plugin._auto_flee.assert_not_awaited()

    async def test_low_food_without_enemy_eats(self):
        plugin = FakePlugin()
        plugin.status_after = status(food=15)
        supervisor = survival_mod.SurvivalSupervisor(plugin)
        await supervisor.evaluate(status(food=5))
        plugin.skills.eat_until_full.assert_awaited_once()
        plugin.skills.combat.assert_not_awaited()
        self.assertTrue(any("自动进食" in text for text, _ in plugin.notices))

    async def test_eat_failure_notifies(self):
        plugin = FakePlugin()
        plugin.skills.eat_until_full = AsyncMock(side_effect=RuntimeError("no safe food"))
        supervisor = survival_mod.SurvivalSupervisor(plugin)
        await supervisor.evaluate(status(food=5))
        self.assertTrue(any("自动进食失败" in text for text, _ in plugin.notices))

    async def test_screen_open_skips(self):
        plugin = FakePlugin()
        supervisor = survival_mod.SurvivalSupervisor(plugin)
        await supervisor.evaluate(status(food=5, screen="Crafting"))
        plugin.skills.eat_until_full.assert_not_awaited()
        plugin.skills.combat.assert_not_awaited()

    async def test_owner_task_skips_noncritical_action(self):
        plugin = FakePlugin()
        plugin._task_current = object()
        supervisor = survival_mod.SurvivalSupervisor(plugin)
        await supervisor.evaluate(status(health=16, hostiles=[{"type": "minecraft:zombie", "name": "Zombie", "distance": 3}]))
        plugin.skills.combat.assert_not_awaited()
        plugin._auto_flee.assert_not_awaited()

    async def test_owner_task_critical_flees(self):
        plugin = FakePlugin()
        plugin._task_current = object()
        supervisor = survival_mod.SurvivalSupervisor(plugin)
        await supervisor.evaluate(status(health=3, hostiles=[{"type": "minecraft:zombie", "name": "Zombie", "distance": 3}]))
        plugin._stop_task_runtime.assert_awaited_once()
        plugin._auto_flee.assert_awaited_once()

    async def test_event_sequence_deduplicates(self):
        plugin = FakePlugin()
        supervisor = survival_mod.SurvivalSupervisor(plugin)
        await supervisor.on_event({"seq": 2, "kind": "damage"})
        await supervisor.on_event({"seq": 1, "kind": "damage"})
        self.assertEqual(supervisor.stats["events"], 1)
        self.assertEqual(supervisor.last_event_kind, "damage")


if __name__ == "__main__":
    unittest.main()
