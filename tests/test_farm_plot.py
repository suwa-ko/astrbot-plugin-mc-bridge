import importlib
import tempfile
import unittest
from pathlib import Path

from test_base import m
from test_skills import FakePlugin

skills_mod = importlib.import_module(m.__name__ + ".skills")
farm_memory_mod = importlib.import_module(m.__name__ + ".farm_memory")


class FarmMemoryTests(unittest.TestCase):
    def test_irregular_cells_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "farm_plots.json"
            memory = farm_memory_mod.FarmMemory(path)
            plot = memory.upsert({
                "id": "farm-1",
                "name": "不规则菜地",
                "world_key": "world",
                "dimension": "minecraft:overworld",
                "cells": [
                    {"x": 0, "y": 63, "z": 0, "crop": "potatoes"},
                    {"x": 1, "y": 63, "z": 0, "crop": "potatoes"},
                    {"x": 0, "y": 63, "z": 1, "crop": "wheat"},
                ],
                "water_cells": [{"x": 2, "y": 63, "z": 1}],
            })
            self.assertEqual(plot["shape"], "irregular")
            self.assertEqual(len(plot["cells"]), 3)
            self.assertEqual(plot["bounds"]["max_x"], 1)
            reloaded = farm_memory_mod.FarmMemory(path).get("farm-1")
            self.assertEqual(len(reloaded["cells"]), 3)
            self.assertEqual(reloaded["shape"], "irregular")

    def test_duplicate_cells_are_collapsed(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = farm_memory_mod.FarmMemory(Path(folder) / "farm_plots.json")
            plot = memory.upsert({
                "id": "farm-1",
                "cells": [
                    {"x": 0, "y": 63, "z": 0, "crop": "wheat"},
                    {"x": 0, "y": 63, "z": 0, "crop": "carrots"},
                ],
            })
            self.assertEqual(len(plot["cells"]), 1)

    def test_plot_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = farm_memory_mod.FarmMemory(Path(folder) / "farm_plots.json")
            for index in range(farm_memory_mod.MAX_PLOTS):
                memory.upsert({
                    "id": f"farm-{index + 1}",
                    "cells": [{"x": index, "y": 63, "z": 0, "crop": "wheat"}],
                })
            with self.assertRaises(ValueError):
                memory.upsert({
                    "id": "farm-extra",
                    "cells": [{"x": 99, "y": 63, "z": 0, "crop": "wheat"}],
                })


class FarmPlotSkillTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, folder):
        plugin = FakePlugin()
        plugin._farm_memory_for = lambda: farm_memory_mod.FarmMemory(Path(folder) / "farm_plots.json")
        return plugin

    async def test_discover_groups_l_shaped_field_as_one_plot(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = self._plugin(folder)
            crop_rows = [
                {"x": 0, "y": 64, "z": 0, "block": "minecraft:potatoes"},
                {"x": 1, "y": 64, "z": 0, "block": "minecraft:potatoes"},
                {"x": 0, "y": 64, "z": 1, "block": "minecraft:wheat"},
            ]
            farmland_rows = [
                {"x": 0, "y": 63, "z": 0, "block": "minecraft:farmland"},
                {"x": 1, "y": 63, "z": 0, "block": "minecraft:farmland"},
                {"x": 0, "y": 63, "z": 1, "block": "minecraft:farmland"},
            ]

            async def call(action, params=None, timeout=10):
                if action == "scan_blocks":
                    query = str((params or {}).get("block") or "")
                    if "farmland" in query:
                        return {"blocks": farmland_rows}
                    if "water" in query:
                        return {"blocks": []}
                    return {"blocks": crop_rows}
                if action == "inspect_block":
                    return {"state": "Block{minecraft:potatoes}[age=7]"}
                return await FakePlugin._call(plugin, action, params, timeout)

            plugin._call = call
            result = await skills_mod.VanillaSkills(plugin).farm_plot("discover", radius=6)
            self.assertEqual(len(result["plots"]), 1)
            plot = result["plots"][0]
            self.assertEqual(plot["cells"], 3)
            self.assertEqual(plot["shape"], "irregular")
            self.assertEqual(plot["crops"].get("potatoes"), 2)

    async def test_harvest_irregular_plot_replants_correct_crops(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = self._plugin(folder)
            memory = plugin._farm_memory_for()
            memory.upsert({
                "id": "farm-1",
                "name": "L 形菜地",
                "cells": [
                    {"x": 0, "y": 63, "z": 0, "crop": "potatoes"},
                    {"x": 1, "y": 63, "z": 0, "crop": "potatoes"},
                    {"x": 0, "y": 63, "z": 1, "crop": "wheat"},
                ],
            })
            plugin.inventory = [
                {"id": "minecraft:potato", "count": 6, "slot": 0},
                {"id": "minecraft:wheat_seeds", "count": 6, "slot": 1},
            ]
            plugin.position = {"x": 1.5, "y": 64, "z": 1.5}

            async def inspect(action, params=None, timeout=10):
                if action == "inspect_block":
                    if (int(params.get("x", 0)), int(params.get("y", 0)), int(params.get("z", 0))) == (0, 64, 1):
                        return {"block": "minecraft:wheat", "state": "Block{minecraft:wheat}[age=7]"}
                    return {"block": "minecraft:potatoes", "state": "Block{minecraft:potatoes}[age=7]"}
                return await FakePlugin._call(plugin, action, params, timeout)

            async def run(action, params, seconds=10):
                plugin.runs.append((action, dict(params)))
                return {"phase": "completed", "success": True}

            plugin._call = inspect
            plugin._run_operation = run
            result = await skills_mod.VanillaSkills(plugin).farm_plot(
                "harvest", plot="farm-1", count=10, replant=True,
            )
            self.assertEqual(result["harvested"], 3)
            self.assertEqual(result["planted"], 3)
            self.assertTrue(result["partial"])
            self.assertEqual(result["requested"], 10)
            plant_items = [params.get("item") for action, params in plugin.runs if action == "plant"]
            self.assertIn("minecraft:potato", plant_items)
            self.assertIn("minecraft:wheat_seeds", plant_items)

    async def test_harvest_moves_into_plot_before_inspecting(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = self._plugin(folder)
            memory = plugin._farm_memory_for()
            memory.upsert({
                "id": "farm-1",
                "cells": [{"x": 0, "y": 63, "z": 0, "crop": "wheat"}],
            })
            plugin.inventory = [{"id": "minecraft:wheat_seeds", "count": 4, "slot": 0}]
            plugin.position = {"x": 20.0, "y": 64, "z": 0.5}
            plugin.bed_context = {
                "stands": [{"x": 0, "y": 64, "z": 1, "feet_y": 64, "distance": 2.0}],
            }

            async def call(action, params=None, timeout=10):
                if action == "inspect_block":
                    if abs(plugin.position["x"] - 0.5) > 16:
                        raise RuntimeError("OUT_OF_RANGE")
                    return {
                        "block": "minecraft:wheat",
                        "state": "Block{minecraft:wheat}[age=7]",
                    }
                return await FakePlugin._call(plugin, action, params, timeout)

            async def approach(x, y, z, seconds=20, reach=4.0, allow_break=False):
                plugin.approaches.append((x, y, z, reach))
                plugin.position = {"x": 1.5, "y": 64, "z": 1.5}
                return None

            async def run(action, params, seconds=10):
                plugin.runs.append((action, dict(params)))
                return {"phase": "completed", "success": True}

            plugin._call = call
            plugin._approach_target = approach
            plugin._run_operation = run
            result = await skills_mod.VanillaSkills(plugin).farm_plot(
                "harvest", plot="farm-1", count=1, replant=True,
            )
            self.assertEqual(result["harvested"], 1)
            self.assertTrue(plugin.approaches)
            self.assertEqual(plugin.approaches[0][:3], (0, 64, 1))

    async def test_plant_counts_crop_confirmed_after_action_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = self._plugin(folder)
            plugin.inventory = [{"id": "minecraft:wheat_seeds", "count": 4, "slot": 0}]
            plugin.inspect_extra[(0, 64, 0)] = {
                "block": "minecraft:wheat", "state": "Block{minecraft:wheat}[age=0]",
            }

            async def run(action, params, seconds=10):
                raise RuntimeError("plant space is occupied")

            plugin._run_operation = run
            ok = await skills_mod.VanillaSkills(plugin)._plant_crop(
                0, 63, 0, "minecraft:wheat_seeds",
            )
            self.assertTrue(ok)

    async def test_harvest_approaches_farmland_stand_at_air_level(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = self._plugin(folder)
            memory = plugin._farm_memory_for()
            memory.upsert({
                "id": "farm-1",
                "cells": [{"x": 0, "y": 63, "z": 0, "crop": "wheat"}],
            })
            plugin.inventory = [{"id": "minecraft:wheat_seeds", "count": 4, "slot": 0}]
            plugin.inspect_extra[(0, 64, 0)] = {
                "block": "minecraft:wheat", "state": "Block{minecraft:wheat}[age=7]",
            }
            plugin.position = {"x": 5.0, "y": 64, "z": 0.5}
            plugin.bed_context = {
                "stands": [{
                    "x": 0, "y": 63, "z": 1, "feet_y": 63.9375, "distance": 1.0,
                }],
            }

            async def approach(x, y, z, seconds=20, reach=4.0, allow_break=False):
                plugin.approaches.append((x, y, z, reach))
                plugin.position = {"x": 1.5, "y": 64, "z": 1.5}
                return None

            async def run(action, params, seconds=10):
                plugin.runs.append((action, dict(params)))
                return {"phase": "completed", "success": True}

            plugin._approach_target = approach
            plugin._run_operation = run
            result = await skills_mod.VanillaSkills(plugin).farm_plot(
                "harvest", plot="farm-1", count=1, replant=True,
            )
            self.assertEqual(result["harvested"], 1)
            self.assertEqual(plugin.approaches[0][1], 64)

    async def test_harvest_skips_navigation_when_already_in_reach(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = self._plugin(folder)
            memory = plugin._farm_memory_for()
            memory.upsert({
                "id": "farm-1",
                "cells": [{"x": 0, "y": 63, "z": 0, "crop": "wheat"}],
            })
            plugin.inventory = [{"id": "minecraft:wheat_seeds", "count": 4, "slot": 0}]
            plugin.inspect_extra[(0, 64, 0)] = {
                "block": "minecraft:wheat", "state": "Block{minecraft:wheat}[age=7]",
            }
            plugin.position = {"x": 1.5, "y": 64, "z": 0.5}

            async def run(action, params, seconds=10):
                plugin.runs.append((action, dict(params)))
                return {"phase": "completed", "success": True}

            plugin._run_operation = run
            result = await skills_mod.VanillaSkills(plugin).farm_plot(
                "harvest", plot="farm-1", count=1, replant=True,
            )
            self.assertEqual(result["harvested"], 1)
            self.assertEqual(result["planted"], 1)
            self.assertFalse(plugin.approaches)

    async def test_harvest_uses_partial_approach_when_navigation_reports_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = self._plugin(folder)
            memory = plugin._farm_memory_for()
            memory.upsert({
                "id": "farm-1",
                "cells": [{"x": 0, "y": 63, "z": 0, "crop": "wheat"}],
            })
            plugin.inventory = [{"id": "minecraft:wheat_seeds", "count": 4, "slot": 0}]
            plugin.inspect_extra[(0, 64, 0)] = {
                "block": "minecraft:wheat", "state": "Block{minecraft:wheat}[age=7]",
            }
            plugin.position = {"x": 5.0, "y": 64, "z": 0.5}
            plugin.bed_context = {
                "stands": [{"x": 0, "y": 64, "z": 1, "feet_y": 64, "distance": 1.0}],
            }

            async def approach(x, y, z, seconds=20, reach=4.0, allow_break=False):
                plugin.approaches.append((x, y, z, reach))
                # The client reports a navigation failure after getting close.
                plugin.position = {"x": 1.5, "y": 64, "z": 1.5}
                return "靠近目标无进展，已停止"

            async def run(action, params, seconds=10):
                plugin.runs.append((action, dict(params)))
                return {"phase": "completed", "success": True}

            plugin._approach_target = approach
            plugin._run_operation = run
            result = await skills_mod.VanillaSkills(plugin).farm_plot(
                "harvest", plot="farm-1", count=1, replant=True,
            )
            self.assertEqual(result["harvested"], 1)
            self.assertEqual(result["planted"], 1)
            self.assertTrue(plugin.approaches)
            self.assertGreater(plugin.approaches[0][3], 1.0)

    async def test_plant_reports_skipped_and_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = self._plugin(folder)
            memory = plugin._farm_memory_for()
            memory.upsert({
                "id": "farm-1",
                "cells": [
                    {"x": 0, "y": 63, "z": 0, "crop": "empty"},
                    {"x": 1, "y": 63, "z": 0, "crop": "wheat"},
                ],
            })
            plugin.inventory = [{"id": "minecraft:wheat_seeds", "count": 8, "slot": 0}]
            plugin.position = {"x": 1.6, "y": 64, "z": 1.6}

            async def inspect(action, params=None, timeout=10):
                if action == "inspect_block":
                    if int(params.get("x", 0)) == 1:
                        return {"block": "minecraft:wheat", "state": "Block{minecraft:wheat}[age=3]"}
                    return {"air": True}
                return await FakePlugin._call(plugin, action, params, timeout)

            async def run(action, params, seconds=10):
                plugin.runs.append((action, dict(params)))
                return {"phase": "completed", "success": True}

            plugin._call = inspect
            plugin._run_operation = run
            result = await skills_mod.VanillaSkills(plugin).farm_plot(
                "plant", plot="farm-1", crop="wheat", count=2,
            )
            self.assertEqual(result["planted"], 1)
            self.assertEqual(result["skipped"], 1)
            self.assertTrue(result["partial"])
            self.assertTrue(result["reasons"])

    async def test_create_mask_supports_irregular_shape(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = self._plugin(folder)

            async def call(action, params=None, timeout=10):
                if action == "inspect_block":
                    return {"block": "minecraft:dirt", "air": False, "replaceable": False}
                return await FakePlugin._call(plugin, action, params, timeout)

            plugin._call = call
            result = await skills_mod.VanillaSkills(plugin).farm_plot(
                "create", mask="##./#~./..#", origin=[0, 63, 0],
                crop="potatoes", dry_run=True,
            )
            self.assertEqual(result["cells"], 4)
            self.assertEqual(result["water_cells"], 1)
            self.assertEqual(result["shape"], "irregular")
            self.assertTrue(result["dry_run"])

    async def test_list_and_forget_plot(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = self._plugin(folder)
            memory = plugin._farm_memory_for()
            memory.upsert({
                "id": "farm-1",
                "cells": [{"x": 0, "y": 63, "z": 0, "crop": "wheat"}],
            })
            skills = skills_mod.VanillaSkills(plugin)
            listed = await skills.farm_plot("list")
            self.assertEqual(len(listed["plots"]), 1)
            forgotten = await skills.farm_plot("forget", plot="farm-1")
            self.assertTrue(forgotten["forget"])
            fresh = farm_memory_mod.FarmMemory(Path(folder) / "farm_plots.json")
            self.assertEqual(fresh.list(), [])


if __name__ == "__main__":
    unittest.main()
