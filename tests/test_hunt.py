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
        self.assertEqual(result["goal_outcome"], "partial")

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
        result = await skills.hunt("cow", count=1, radius=16, max_seconds=10, search=False)
        self.assertEqual(result["hunted"], 0)
        self.assertEqual(result["goal_outcome"], "unmet")
        self.assertTrue(any("没有可猎取" in error for error in result["errors"]))

    async def test_hunt_reports_no_targets(self):
        plugin = FakePlugin()
        skills = skills_mod.VanillaSkills(plugin)
        skills.return_home = AsyncMock(return_value={"arrived": True})
        result = await skills.hunt("auto", count=1, radius=16, max_seconds=10, search=False)
        self.assertEqual(result["hunted"], 0)
        self.assertEqual(result["goal_outcome"], "unmet")
        self.assertTrue(result["errors"])

    async def test_hunt_without_drop_is_partial(self):
        plugin = FakePlugin()

        async def call(action, params=None, timeout=10):
            if action == "scan_entities":
                return {"entities": [{
                    "uuid": "cow-1", "type": "minecraft:cow", "x": 1, "y": 64, "z": 0,
                    "distance": 2.0, "baby": False, "tamed": False,
                }]}
            if action == "hunt":
                return {"target": "Cow", "type": "minecraft:cow", "dead": True, "health": 0}
            return await FakePlugin._call(plugin, action, params, timeout)

        plugin._call = call
        skills = skills_mod.VanillaSkills(plugin)
        skills.return_home = AsyncMock(return_value={"arrived": True})
        result = await skills.hunt("cow", count=1, radius=16, max_seconds=20, cook=False)
        self.assertEqual(result["hunted"], 1)
        self.assertEqual(result["goal_outcome"], "partial")
        self.assertTrue(any("没有拾取到肉" in error for error in result["errors"]))

    async def test_hunt_uses_return_to_for_far_targets(self):
        plugin = FakePlugin()
        scans = [
            {"entities": [{"uuid": "pig-1", "type": "minecraft:pig", "x": 20, "y": 64, "z": 0,
                           "distance": 20.0, "baby": False, "tamed": False}]},
            {"entities": [{"uuid": "pig-1", "type": "minecraft:pig", "x": 1, "y": 64, "z": 0,
                           "distance": 2.0, "baby": False, "tamed": False}]},
        ]

        async def call(action, params=None, timeout=10):
            if action == "scan_entities":
                return scans.pop(0) if scans else {"entities": []}
            if action == "hunt":
                return {"target": "Pig", "type": "minecraft:pig", "dead": True, "health": 0}
            return await FakePlugin._call(plugin, action, params, timeout)

        plugin._call = call
        skills = skills_mod.VanillaSkills(plugin)
        skills.return_to = AsyncMock(return_value={"arrived": True})
        skills.return_home = AsyncMock(return_value={"arrived": True})
        result = await skills.hunt("pig", count=1, radius=32, max_seconds=30)
        skills.return_to.assert_awaited()
        self.assertEqual(result["hunted"], 1)

    async def test_hunt_skips_targets_beyond_safe_navigation(self):
        plugin = FakePlugin()

        async def call(action, params=None, timeout=10):
            if action == "scan_entities":
                return {"entities": [{
                    "uuid": "far-cow", "type": "minecraft:cow", "x": 40, "y": 64, "z": 0,
                    "distance": 34.3, "baby": False, "tamed": False,
                }]}
            return await FakePlugin._call(plugin, action, params, timeout)

        plugin._call = call
        skills = skills_mod.VanillaSkills(plugin)
        skills.return_home = AsyncMock(return_value={"arrived": True})
        result = await skills.hunt("cow", count=1, radius=32, max_seconds=10, search=False)
        self.assertEqual(result["hunted"], 0)
        self.assertEqual(result["goal_outcome"], "unmet")
        self.assertFalse(any("safe navigation" in error for error in result["errors"]))


    async def test_hunt_azurecrane_drops_fowl(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "genshin_nature:fowl", "count": 3, "slot": 0}]

        async def call(action, params=None, timeout=10):
            if action == "scan_entities":
                return {"entities": [{
                    "uuid": "crane-1", "type": "genshin_nature:azurecrane", "x": 1, "y": 64, "z": 0,
                    "distance": 2.0, "baby": False, "tamed": False,
                }]}
            if action == "hunt":
                return {"target": "Azure crane", "type": "genshin_nature:azurecrane", "dead": True, "health": 0}
            return await FakePlugin._call(plugin, action, params, timeout)

        plugin._call = call
        skills = skills_mod.VanillaSkills(plugin)
        skills.return_home = AsyncMock(return_value={"arrived": True})
        skills.smelt = AsyncMock(return_value={"produced": 3})
        result = await skills.hunt("azurecrane", count=1, radius=16, max_seconds=20)
        self.assertEqual(result["hunted"], 1)
        self.assertEqual(result["raw"].get("genshin_nature:fowl"), 3)
        skills.smelt.assert_awaited()

    def test_hunt_target_alias_resolves_modded_animal(self):
        skills = skills_mod.VanillaSkills(FakePlugin())
        _, params = skills.validate_params("hunt", {"target": "azurecrane", "count": 1})
        self.assertEqual(params["target"], "genshin_nature:azurecrane")

    async def test_hunt_scan_filters_by_concrete_type(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "genshin_nature:fowl", "count": 1, "slot": 0}]
        scans = []

        async def call(action, params=None, timeout=10):
            if action == "scan_entities":
                if params and params.get("type") in skills_mod.HUNTABLE:
                    scans.append(params["type"])
                return {"entities": [{
                    "uuid": "crane-1", "type": "genshin_nature:azurecrane", "x": 1, "y": 64, "z": 0,
                    "distance": 2.0, "baby": False, "tamed": False,
                }]}
            if action == "hunt":
                return {"target": "Azure crane", "type": "genshin_nature:azurecrane", "dead": True, "health": 0}
            return await FakePlugin._call(plugin, action, params, timeout)

        plugin._call = call
        skills = skills_mod.VanillaSkills(plugin)
        skills.return_home = AsyncMock(return_value={"arrived": True})
        skills.smelt = AsyncMock(return_value={"produced": 1})
        result = await skills.hunt("azurecrane", count=1, radius=32, max_seconds=20, cook=False)
        self.assertEqual(result["hunted"], 1)
        self.assertEqual(scans, ["genshin_nature:azurecrane"])

    async def test_hunt_auto_scans_each_whitelisted_type(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "minecraft:beef", "count": 1, "slot": 0}]
        scans = []

        async def call(action, params=None, timeout=10):
            if action == "scan_entities":
                if params and params.get("type") in skills_mod.HUNTABLE:
                    scans.append(params["type"])
                if params.get("type") == "minecraft:cow":
                    return {"entities": [{
                        "uuid": "cow-1", "type": "minecraft:cow", "x": 1, "y": 64, "z": 0,
                        "distance": 2.0, "baby": False, "tamed": False,
                    }]}
                return {"entities": []}
            if action == "hunt":
                return {"target": "Cow", "type": "minecraft:cow", "dead": True, "health": 0}
            return await FakePlugin._call(plugin, action, params, timeout)

        plugin._call = call
        skills = skills_mod.VanillaSkills(plugin)
        skills.return_home = AsyncMock(return_value={"arrived": True})
        result = await skills.hunt("auto", count=1, radius=32, max_seconds=20, cook=False)
        self.assertEqual(result["hunted"], 1)
        self.assertIn("minecraft:cow", scans)
        self.assertIn("genshin_nature:azurecrane", scans)
        self.assertEqual(scans, list(skills_mod.HUNTABLE))

    async def test_hunt_stops_at_meat_target(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "minecraft:beef", "count": 10, "slot": 0}]

        async def call(action, params=None, timeout=10):
            if action == "scan_entities":
                return {"entities": [{
                    "uuid": "cow-1", "type": "minecraft:cow", "x": 1, "y": 64, "z": 0,
                    "distance": 2.0, "baby": False, "tamed": False,
                }]}
            if action == "hunt":
                return {"target": "Cow", "type": "minecraft:cow", "dead": True, "health": 0}
            return await FakePlugin._call(plugin, action, params, timeout)

        plugin._call = call
        skills = skills_mod.VanillaSkills(plugin)
        skills.return_home = AsyncMock(return_value={"arrived": True})
        result = await skills.hunt(
            "cow", count=20, meat_target=10, max_seconds=20, cook=False,
        )
        self.assertEqual(result["hunted"], 1)
        self.assertEqual(result["raw_total"], 10)
        self.assertEqual(result["goal_outcome"], "fulfilled")

    async def test_hunt_explores_when_no_prey(self):
        plugin = FakePlugin()
        skills = skills_mod.VanillaSkills(plugin)
        skills.explore = AsyncMock(return_value={"stopped": "no_frontier"})
        skills.return_home = AsyncMock(return_value={"arrived": True})
        result = await skills.hunt(
            "auto", count=1, max_seconds=20, search=True, search_rounds=1,
        )
        skills.explore.assert_awaited_once()
        self.assertEqual(result["hunted"], 0)
        self.assertEqual(result["goal_outcome"], "unmet")


if __name__ == "__main__":
    unittest.main()
