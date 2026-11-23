import importlib
import unittest
from unittest.mock import AsyncMock

from test_base import m
from test_skills import FakePlugin

skills_mod = importlib.import_module(m.__name__ + ".skills")


class HuntTests(unittest.IsolatedAsyncioTestCase):
    async def test_hunt_kills_adult_cow_and_cooks_meat(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "minecraft:beef", "count": 2, "slot": 0}]
        calls = []

        async def call(action, params=None, timeout=10):
            calls.append((action, dict(params or {})))
            if action == "scan_entities":
                return {"entities": [{
                    "uuid": "cow-1", "type": "minecraft:cow", "name": "Cow",
                    "x": 1, "y": 64, "z": 0, "distance": 2.0,
                    "health": 10, "baby": False, "tamed": False, "passive": True,
                }]}
            if action == "hunt":
                return {"target": "Cow", "type": "minecraft:cow", "dead": True, "health": 0}
            return await FakePlugin._call(plugin, action, params, timeout)

        plugin._call = call
        skills = skills_mod.VanillaSkills(plugin)
        skills.return_home = AsyncMock(return_value={"arrived": True})
        skills.smelt = AsyncMock(return_value={"produced": 2})
        result = await skills.hunt("cow", count=1, radius=16, max_seconds=30)
        self.assertEqual(result["hunted"], 1)
        self.assertEqual(result["raw"].get("minecraft:beef"), 2)
        self.assertTrue(result["returned"])
        skills.smelt.assert_awaited()
        self.assertEqual(result["goal_outcome"], "fulfilled")

    async def test_hunt_skips_baby_and_tamed_animals(self):
        plugin = FakePlugin()

        async def call(action, params=None, timeout=10):
            if action == "scan_entities":
                return {"entities": [
                    {"uuid": "baby", "type": "minecraft:cow", "x": 1, "y": 64, "z": 0,
                     "distance": 1, "baby": True},
                    {"uuid": "pet", "type": "minecraft:cow", "x": 1, "y": 64, "z": 0,
                     "distance": 1, "tamed": True},
                ]}
            return await FakePlugin._call(plugin, action, params, timeout)

        plugin._call = call
        skills = skills_mod.VanillaSkills(plugin)
        skills.return_home = AsyncMock(return_value={"arrived": True})
        result = await skills.hunt("cow", count=1, radius=16, max_seconds=10)
        self.assertEqual(result["hunted"], 0)
        self.assertEqual(result["goal_outcome"], "unmet")
        self.assertTrue(any("没有可猎取" in error for error in result["errors"]))

    async def test_hunt_reports_no_targets(self):
        plugin = FakePlugin()
        skills = skills_mod.VanillaSkills(plugin)
        skills.return_home = AsyncMock(return_value={"arrived": True})
        result = await skills.hunt("auto", count=1, radius=16, max_seconds=10)
        self.assertEqual(result["hunted"], 0)
        self.assertEqual(result["goal_outcome"], "unmet")
        self.assertTrue(result["errors"])


if __name__ == "__main__":
    unittest.main()
