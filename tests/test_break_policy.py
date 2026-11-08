import importlib
import tempfile
import unittest
from pathlib import Path

from test_base import m

break_mod = importlib.import_module(m.__name__ + ".break_policy")


class BreakPolicyTests(unittest.TestCase):
    def test_protected_region_blocks_natural_block(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = break_mod.BreakPolicy(Path(folder) / "regions.json", per_action=8, per_task=32)
            policy.add_box("home", (0, 60, 0), (10, 80, 10))
            ok, reason = policy.check(5, 64, 5, "minecraft:stone")
            self.assertFalse(ok)
            self.assertIn("home", reason)

    def test_protected_container_is_always_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = break_mod.BreakPolicy(Path(folder) / "regions.json")
            ok, reason = policy.check(99, 64, 99, "minecraft:chest")
            self.assertFalse(ok)
            self.assertIn("容器", reason)

    def test_unnatural_block_requires_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = break_mod.BreakPolicy(Path(folder) / "regions.json")
            ok, reason = policy.check(1, 64, 1, "minecraft:oak_planks")
            self.assertFalse(ok)
            self.assertIn("可破坏类别", reason)
            policy.allow_unnatural = True
            ok, _ = policy.check(1, 64, 1, "minecraft:oak_planks")
            self.assertTrue(ok)

    def test_per_action_and_task_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = break_mod.BreakPolicy(Path(folder) / "regions.json", per_action=2, per_task=3)
            self.assertTrue(policy.check(1, 64, 1, "minecraft:stone", count=2)[0])
            ok, reason = policy.check(2, 64, 2, "minecraft:stone", count=3)
            self.assertFalse(ok)
            self.assertIn("单次", reason)
            policy.begin_task("t1")
            self.assertTrue(policy.check(3, 64, 3, "minecraft:stone", count=2, task_id="t1")[0])
            ok, reason = policy.check(4, 64, 4, "minecraft:stone", count=2, task_id="t1")
            self.assertFalse(ok)
            self.assertIn("任务", reason)

    def test_regions_persist_and_remove(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "regions.json"
            policy = break_mod.BreakPolicy(path)
            policy.add_box("shed", (1, 2, 3), (4, 5, 6))
            again = break_mod.BreakPolicy(path)
            self.assertEqual(len(again.list()), 1)
            self.assertTrue(again.is_protected(2, 3, 4))
            self.assertTrue(again.remove("shed"))
            self.assertEqual(again.list(), [])

    def test_corridor_intersects_protected_region(self):
        with tempfile.TemporaryDirectory() as folder:
            policy = break_mod.BreakPolicy(Path(folder) / "regions.json")
            policy.add_box("home", (0, 60, 0), (10, 80, 10))
            self.assertIsNotNone(policy.intersects((-20, 60, -20), (20, 70, 20)))
            self.assertIsNone(policy.intersects((30, 60, 30), (40, 70, 40)))

    def test_category_mapping(self):
        self.assertEqual(break_mod.block_category("minecraft:oak_log"), "logs")
        self.assertEqual(break_mod.block_category("minecraft:iron_ore"), "ore")
        self.assertEqual(break_mod.block_category("minecraft:stone"), "stone")
        self.assertIsNone(break_mod.block_category("minecraft:oak_planks"))


if __name__ == "__main__":
    unittest.main()
