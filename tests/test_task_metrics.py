import json
import tempfile
import unittest
from pathlib import Path

from test_base import m
import importlib

metrics_mod = importlib.import_module(m.__name__ + ".task_metrics")


class TaskMetricsTests(unittest.TestCase):
    def test_summary_and_by_opcode(self):
        with tempfile.TemporaryDirectory() as folder:
            metrics = metrics_mod.TaskMetrics(Path(folder) / "metrics.jsonl")
            metrics.record("gather", "completed", 2.5, task_id="t1")
            metrics.record("gather", "failed", 4.0, task_id="t2", error="timeout")
            metrics.record("mine_ore", "cancelled", 1.0, task_id="t3")
            snapshot = metrics.snapshot()
            self.assertEqual(snapshot["overall"]["total"], 3)
            self.assertEqual(snapshot["overall"]["completed"], 1)
            self.assertEqual(snapshot["overall"]["failed"], 1)
            self.assertEqual(snapshot["overall"]["cancelled"], 1)
            self.assertEqual(snapshot["by_opcode"]["gather"]["total"], 2)
            self.assertEqual(snapshot["by_opcode"]["gather"]["failed"], 1)

    def test_metrics_survive_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "metrics.jsonl"
            first = metrics_mod.TaskMetrics(path)
            first.record("sleep_at", "completed", 3.25, task_id="task-1")
            second = metrics_mod.TaskMetrics(path)
            self.assertEqual(second.snapshot()["overall"]["completed"], 1)
            self.assertEqual(second.snapshot()["latest"][0]["opcode"], "sleep_at")

    def test_corrupt_lines_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "metrics.jsonl"
            path.write_text(
                '{"opcode":"gather","status":"completed","duration":1}\n'
                "{broken\n"
                '{"opcode":"","status":"completed"}\n',
                encoding="utf-8",
            )
            metrics = metrics_mod.TaskMetrics(path)
            self.assertEqual(metrics.snapshot()["overall"]["total"], 1)

    def test_store_is_bounded_and_compacted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "metrics.jsonl"
            metrics = metrics_mod.TaskMetrics(path, max_records=20)
            for index in range(25):
                metrics.record("gather", "completed", index, task_id=f"t{index}")
            self.assertEqual(len(metrics.snapshot(limit=100)["latest"]), 20)
            reloaded = metrics_mod.TaskMetrics(path, max_records=20)
            self.assertEqual(reloaded.snapshot()["overall"]["total"], 20)
            self.assertEqual(reloaded.snapshot()["latest"][-1]["task_id"], "t24")

    def test_low_success_rate_alert(self):
        with tempfile.TemporaryDirectory() as folder:
            metrics = metrics_mod.TaskMetrics(Path(folder) / "metrics.jsonl")
            for _ in range(5):
                metrics.record("gather", "failed", 1.0, error="timeout")
            alerts = metrics.snapshot()["alerts"]
            self.assertTrue(any(item["type"] == "low_success_rate" and item["opcode"] == "gather" for item in alerts))

    def test_alert_needs_at_least_five_samples(self):
        with tempfile.TemporaryDirectory() as folder:
            metrics = metrics_mod.TaskMetrics(Path(folder) / "metrics.jsonl")
            for _ in range(4):
                metrics.record("gather", "failed", 1.0)
            self.assertEqual(metrics.snapshot()["alerts"], [])

    def test_invalid_status_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            metrics = metrics_mod.TaskMetrics(Path(folder) / "metrics.jsonl")
            with self.assertRaises(ValueError):
                metrics.record("gather", "unknown", 1.0)


if __name__ == "__main__":
    unittest.main()
