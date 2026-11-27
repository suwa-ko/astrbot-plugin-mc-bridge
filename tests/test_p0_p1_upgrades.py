"""Unit tests for P0 and P1 upgrades (v2.1.0)."""
from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from test_base import m
from test_skills import FakePlugin

skills_mod = importlib.import_module(m.__name__ + ".skills")
memory_mod = importlib.import_module(m.__name__ + ".memory_store")


class P0P1UpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_mining_torch_placed_when_dark_and_throttled(self):
        class DarkPlugin(FakePlugin):
            async def _call(self, action, params=None, timeout=10):
                data = await super()._call(action, params, timeout)
                if action == "status":
                    data["block_light"] = 2
                    data["inventory_items"] = [{"id": "minecraft:torch", "count": 8, "slot": 0}]
                return data

        plugin = DarkPlugin()
        skills = skills_mod.VanillaSkills(plugin)
        self.assertTrue(await skills._place_mine_torch_if_dark())
        self.assertFalse(await skills._place_mine_torch_if_dark())  # throttled
        self.assertTrue(any(act == "station" and p.get("item") == "minecraft:torch" for act, p in plugin.runs))

    async def test_mining_inventory_drops_excess_junk_keeping_cobble_reserve(self):
        class FullInvPlugin(FakePlugin):
            def __init__(self):
                super().__init__()
                self.dropped = []

            async def _call(self, action, params=None, timeout=10):
                if action == "drop_item":
                    self.dropped.append((params["item"], params["count"]))
                    return {"dropped": params["count"]}
                data = await super()._call(action, params, timeout)
                if action == "status":
                    data["empty_slots"] = 1
                    data["inventory_items"] = [
                        {"id": "minecraft:granite", "count": 64, "slot": 1},
                        {"id": "minecraft:cobblestone", "count": 64, "slot": 2},
                        {"id": "minecraft:cobblestone", "count": 32, "slot": 3},
                    ]
                return data

        plugin = FullInvPlugin()
        skills = skills_mod.VanillaSkills(plugin)
        dropped_stacks = await skills._manage_mining_inventory()
        self.assertEqual(dropped_stacks, 2)
        self.assertIn(("minecraft:granite", 64), plugin.dropped)
        self.assertIn(("minecraft:cobblestone", 32), plugin.dropped)

    async def test_combat_retreats_from_close_creeper_with_shield(self):
        class CreeperPlugin(FakePlugin):
            def __init__(self):
                super().__init__()
                self.fled = []

            async def _call(self, action, params=None, timeout=10):
                data = await super()._call(action, params, timeout)
                if action == "status":
                    data["hostiles"] = [
                        {"type": "minecraft:creeper", "name": "Creeper", "distance": 4.2, "position": {"x": 8, "y": 64, "z": 0}},
                    ]
                    data["inventory_items"] = [
                        {"id": "minecraft:shield", "count": 1, "slot": 40},
                    ]
                return data

            async def _auto_flee(self, status, hostile):
                self.fled.append(hostile["type"])
                return {"fled": True}

        plugin = CreeperPlugin()
        res = await skills_mod.VanillaSkills(plugin).combat(max_seconds=10)
        self.assertEqual(res["result"], "retreated_creeper")
        self.assertEqual(plugin.fled, ["minecraft:creeper"])
        self.assertTrue(any(act == "use_item" and p.get("hand") == "off_hand" for act, p in plugin.calls))

    async def test_emergency_fire_water_bucket_skips_nether(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "minecraft:water_bucket", "count": 1, "slot": 5}]
        skills = skills_mod.VanillaSkills(plugin)
        ok_nether = await skills._emergency_fire_water_bucket(
            {"on_fire": True, "dimension": "minecraft:the_nether", "inventory_items": plugin.inventory}
        )
        self.assertFalse(ok_nether)
        ok_overworld = await skills._emergency_fire_water_bucket(
            {"on_fire": True, "dimension": "minecraft:overworld", "inventory_items": plugin.inventory}
        )
        self.assertTrue(ok_overworld)
        self.assertTrue(any(act == "select_slot" and p.get("item") == "minecraft:water_bucket" for act, p in plugin.calls))

    async def test_build_blueprint_layers_check_and_place_bottom_up(self):
        plugin = FakePlugin()
        plugin.inventory = [
            {"id": "minecraft:cobblestone", "count": 8, "slot": 0},
            {"id": "minecraft:oak_planks", "count": 4, "slot": 1},
        ]
        bp = {
            "palette": {"#": "minecraft:cobblestone", "P": "minecraft:oak_planks", ".": ""},
            "layers": [
                ["##", "##"],
                ["PP", "PP"],
            ],
        }
        skills = skills_mod.VanillaSkills(plugin)
        check = await skills.build_blueprint(blueprint=bp, x=5, y=64, z=0, name="hut", check_only=True)
        self.assertTrue(check["ready"])
        self.assertEqual(check["materials_required"], {"minecraft:cobblestone": 4, "minecraft:oak_planks": 4})

        res = await skills.build_blueprint(blueprint=bp, x=5, y=64, z=0, name="hut")
        self.assertEqual(res["goal_outcome"], "fulfilled")
        self.assertEqual(res["placed"], 8)
        placed_ys = [p["y"] for act, p in plugin.runs if act == "place_at"]
        self.assertEqual(placed_ys, [64, 64, 64, 64, 65, 65, 65, 65])

    async def test_build_blueprint_rejects_missing_materials(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "minecraft:cobblestone", "count": 1, "slot": 0}]
        bp = {"blocks": [{"x": 0, "y": 0, "z": 0, "item": "cobblestone"}, {"x": 1, "y": 0, "z": 0, "item": "cobblestone"}]}
        with self.assertRaisesRegex(skills_mod.SkillError, "蓝图材料不足"):
            await skills_mod.VanillaSkills(plugin).build_blueprint(blueprint=bp, x=5, y=64, z=0)

    async def test_mine_shaft_memory_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            store = memory_mod.MemoryStore(Path(td) / "memory.db")
            try:
                plugin = FakePlugin()
                plugin.position = {"x": 10.0, "y": 70.0, "z": 10.0}
                plugin._memory_store_for = lambda: store
                plugin._memory_scope_for = lambda st: {
                    "world_key": st.get("world_key", "test-world"),
                    "world_instance": "default",
                    "dimension": st.get("dimension", "minecraft:overworld"),
                }
                skills = skills_mod.VanillaSkills(plugin)
                st = await skills.status()
                skills._remember_mine_shaft((20, 70, 10), st)
                found = skills._find_nearby_mine_shaft(st, max_distance=48.0)
                self.assertEqual(found, (20, 70, 10))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
