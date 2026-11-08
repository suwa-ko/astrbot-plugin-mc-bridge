import json
import tempfile
import unittest
from pathlib import Path
import importlib

from test_base import m

journal_mod = importlib.import_module(m.__name__ + ".task_journal")


class TaskJournalTests(unittest.TestCase):
    def test_record_and_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "journal.jsonl"
            journal = journal_mod.TaskJournal(path)
            journal.record({"seq": 1, "event": "started", "task_id": "t1"})
            journal.record({"seq": 2, "event": "completed", "task_id": "t1"})
            again = journal_mod.TaskJournal(path)
            snapshot = again.snapshot()
            self.assertEqual(snapshot["total"], 2)
            self.assertEqual(snapshot["events"]["started"], 1)
            self.assertEqual(snapshot["last_seq"], 2)

    def test_corrupt_lines_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "journal.jsonl"
            path.write_text(
                '{"seq":1,"event":"started"}\n'
                "{broken\n"
                '{"seq":2,"event":"completed"}\n',
                encoding="utf-8",
            )
            journal = journal_mod.TaskJournal(path)
            self.assertEqual(journal.snapshot()["total"], 2)

    def test_journal_is_bounded_and_compacted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "journal.jsonl"
            journal = journal_mod.TaskJournal(path, max_records=20)
            for index in range(25):
                journal.record({"seq": index, "event": "started", "task_id": f"t{index}"})
            self.assertEqual(journal.snapshot(limit=100)["total"], 20)
            reloaded = journal_mod.TaskJournal(path, max_records=20)
            self.assertEqual(reloaded.snapshot(limit=100)["total"], 20)
            self.assertEqual(reloaded.snapshot()["last_seq"], 24)

    def test_record_many_skips_invalid_events(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = journal_mod.TaskJournal(Path(folder) / "journal.jsonl")
            journal.record_many([
                {"seq": 1, "event": "started"},
                {"seq": 2},
                "not-a-dict",
                {"seq": 3, "event": "failed"},
            ])
            self.assertEqual(journal.snapshot()["total"], 2)


if __name__ == "__main__":
    unittest.main()
