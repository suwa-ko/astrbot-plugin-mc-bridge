import importlib
import tempfile
import unittest
from pathlib import Path

from test_base import m

checkpoint_mod = importlib.import_module(m.__name__ + ".task_checkpoint")
recovery_mod = importlib.import_module(m.__name__ + ".task_recovery")


class TaskCheckpointTests(unittest.TestCase):
    def test_inventory_and_baseline(self):
        status = {
            "position": {"x": 1.5, "y": 64, "z": -2.25},
            "dimension": "minecraft:overworld",
            "world_key": "world",
            "health": 18,
            "food": 15,
            "inventory_items": [
                {"id": "minecraft:oak_log", "count": 3, "slot": 0},
                {"id": "minecraft:oak_log", "count": 2, "slot": 1},
                {"id": "minecraft:bread", "count": 4, "slot": 40},
            ],
        }
        self.assertEqual(checkpoint_mod.inventory_counts(status), {"minecraft:oak_log": 5})
        baseline = checkpoint_mod.baseline_from_status(status)
        self.assertEqual(baseline["position"]["x"], 1.5)
        self.assertEqual(baseline["dimension"], "minecraft:overworld")
        self.assertEqual(baseline["inventory"], {"minecraft:oak_log": 5})

    def test_checkpoint_is_bounded(self):
        cleaned = checkpoint_mod.clean_checkpoint({
            "skill": "gather",
            "stage": "x" * 200,
            "completed": -4,
            "total": 8,
            "detail": {"note": "y" * 5000},
        })
        self.assertEqual(cleaned["completed"], 0)
        self.assertLessEqual(len(cleaned["stage"]), 80)
        self.assertLessEqual(len(cleaned["detail"].get("note", "")), 240)

    def test_gather_resumes_with_remaining_count(self):
        plan = checkpoint_mod.resume_plan(
            "gather",
            {"resource": "logs", "count": 8},
            {"skill": "gather", "stage": "collecting", "completed": 5, "total": 8},
        )
        self.assertEqual(plan["mode"], "checkpoint")
        self.assertEqual(plan["params"]["count"], 3)
        self.assertIn("5/8", plan["note"])

    def test_gather_complete(self):
        plan = checkpoint_mod.resume_plan(
            "mine_ore",
            {"resource": "iron", "count": 4},
            {"skill": "gather", "stage": "collecting", "completed": 4, "total": 4},
        )
        self.assertEqual(plan["mode"], "complete")

    def test_transfer_many_subtracts_per_item(self):
        plan = checkpoint_mod.resume_plan(
            "container_transfer",
            {"items": [{"item": "minecraft:cobblestone", "count": 10}, {"item": "minecraft:stick", "count": 4}]},
            {
                "skill": "transfer_many",
                "stage": "transferring",
                "completed": 8,
                "total": 14,
                "detail": {"moves": {"minecraft:cobblestone": 8}},
            },
        )
        self.assertEqual(plan["mode"], "checkpoint")
        self.assertEqual(plan["params"]["items"], [
            {"item": "minecraft:cobblestone", "count": 2},
            {"item": "minecraft:stick", "count": 4},
        ])

    def test_build_resumes_with_skip_positions(self):
        plan = checkpoint_mod.resume_plan(
            "build",
            {"pattern": "line", "length": 4, "x": 1, "y": 64, "z": 2},
            {
                "skill": "build",
                "stage": "building",
                "completed": 2,
                "total": 4,
                "detail": {"positions": [[1, 64, 2], [2, 64, 2]]},
            },
        )
        self.assertEqual(plan["mode"], "checkpoint")
        self.assertEqual(plan["params"]["skip_positions"], [[1, 64, 2], [2, 64, 2]])

    def test_farm_both_resumes_only_unfinished_side(self):
        plan = checkpoint_mod.resume_plan(
            "farm",
            {"operation": "both", "count": 4},
            {
                "skill": "farm",
                "stage": "farming",
                "completed": 6,
                "total": 8,
                "detail": {"operation": "both", "harvested": 4, "planted": 2},
            },
        )
        self.assertEqual(plan["mode"], "checkpoint")
        self.assertEqual(plan["params"]["operation"], "plant")
        self.assertEqual(plan["params"]["count"], 2)

    def test_unmatched_checkpoint_reruns(self):
        plan = checkpoint_mod.resume_plan(
            "craft",
            {"item": "minecraft:stick", "count": 4},
            {"skill": "gather", "stage": "collecting", "completed": 2, "total": 4},
        )
        self.assertEqual(plan["mode"], "rerun")
        self.assertEqual(plan["params"], {"item": "minecraft:stick", "count": 4})

    def test_checkpoint_for_another_item_reruns(self):
        plan = checkpoint_mod.resume_plan(
            "craft",
            {"item": "minecraft:stone_pickaxe", "count": 1},
            {"skill": "craft", "completed": 8, "total": 8, "detail": {"item": "minecraft:stick"}},
        )
        self.assertEqual(plan["mode"], "rerun")
        self.assertEqual(plan["params"]["count"], 1)

    def test_checkpoint_for_another_resource_reruns(self):
        plan = checkpoint_mod.resume_plan(
            "gather",
            {"resource": "iron", "count": 4},
            {"skill": "gather", "completed": 2, "total": 4, "detail": {"resource": "logs"}},
        )
        self.assertEqual(plan["mode"], "rerun")

    def test_checkpoint_for_another_transfer_direction_reruns(self):
        plan = checkpoint_mod.resume_plan(
            "transfer",
            {"item": "minecraft:cobblestone", "count": 8, "direction": "from_container"},
            {"skill": "transfer", "completed": 2, "total": 8,
             "detail": {"item": "minecraft:cobblestone", "direction": "to_container"}},
        )
        self.assertEqual(plan["mode"], "rerun")

    def test_recovery_preserves_checkpoint_group_and_baseline(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recovery.json"
            store = recovery_mod.TaskRecovery(path)
            store.merge([{
                "recovery_id": "t1",
                "group_id": "g1",
                "opcode": "gather",
                "params": {"resource": "logs", "count": 8},
                "dependencies": ["t0"],
                "checkpoint": {"skill": "gather", "stage": "collecting", "completed": 3, "total": 8},
                "baseline": {"inventory": {"minecraft:oak_log": 2}},
            }])
            row = store.get("t1")
            self.assertEqual(row["group_id"], "g1")
            self.assertEqual(row["dependencies"], ["t0"])
            self.assertEqual(row["checkpoint"]["completed"], 3)
            self.assertEqual(store.group("g1")[0]["recovery_id"], "t1")
            self.assertTrue(store.discard_group("g1"))
            self.assertIsNone(store.get("t1"))


if __name__ == "__main__":
    unittest.main()
