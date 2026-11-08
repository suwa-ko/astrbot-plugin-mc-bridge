import tempfile
import unittest
from pathlib import Path
import importlib

from test_base import m

recovery_mod = importlib.import_module(m.__name__ + ".task_recovery")


def row(recovery_id, opcode="gather", params=None, priority=20):
    return {
        "recovery_id": recovery_id,
        "opcode": opcode,
        "params": params or {"resource": "logs", "count": 4},
        "priority": priority,
        "resources": ["actor", "navigation"],
        "goal": opcode,
        "remaining_seconds": 60,
    }


class TaskRecoveryTests(unittest.TestCase):
    def test_merge_get_and_discard(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recovery.json"
            store = recovery_mod.TaskRecovery(path)
            store.merge([row("old-1"), row("old-2", opcode="mine_ore")])
            self.assertEqual(store.get("old-1")["opcode"], "gather")
            self.assertEqual(store.snapshot()["count"], 2)
            self.assertTrue(store.discard("old-1"))
            self.assertIsNone(store.get("old-1"))

    def test_survives_reload_and_merges(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recovery.json"
            first = recovery_mod.TaskRecovery(path)
            first.merge([row("old-1")])
            second = recovery_mod.TaskRecovery(path)
            second.merge([row("old-2")])
            self.assertEqual(second.snapshot()["count"], 2)
            third = recovery_mod.TaskRecovery(path)
            self.assertEqual(third.snapshot()["count"], 2)

    def test_corrupt_file_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recovery.json"
            path.write_text("{broken", encoding="utf-8")
            store = recovery_mod.TaskRecovery(path)
            self.assertEqual(store.snapshot()["count"], 0)

    def test_invalid_rows_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recovery.json"
            path.write_text(
                '{"items":[{"recovery_id":"","opcode":"gather"},{"recovery_id":"ok","opcode":"gather","params":{}}]}',
                encoding="utf-8",
            )
            store = recovery_mod.TaskRecovery(path)
            self.assertEqual(store.snapshot()["count"], 1)
            self.assertEqual(store.snapshot()["items"][0]["recovery_id"], "ok")

    def test_store_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            store = recovery_mod.TaskRecovery(Path(folder) / "recovery.json", max_records=2)
            store.merge([row("a"), row("b"), row("c")])
            self.assertEqual(store.snapshot()["count"], 2)
            self.assertIsNone(store.get("a"))


if __name__ == "__main__":
    unittest.main()
