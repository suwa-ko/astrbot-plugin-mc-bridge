import tempfile
import unittest
from pathlib import Path
import importlib

from test_base import m

history_mod = importlib.import_module(m.__name__ + ".short_history")


class ShortHistoryTests(unittest.TestCase):
    def test_set_get_and_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            store = history_mod.ShortHistoryStore(path)
            store.set("default:FriendMessage:1", ["主人：mc红床", "你：记下了"])
            self.assertEqual(store.get("default:FriendMessage:1")[0], "主人：mc红床")
            again = history_mod.ShortHistoryStore(path)
            self.assertEqual(again.get("default:FriendMessage:1")[1], "你：记下了")

    def test_ttl_prunes_old_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            store = history_mod.ShortHistoryStore(path, ttl_seconds=100, clock=lambda: 1000)
            store.set("old", ["主人：旧消息", "你：旧回复"])
            fresh = history_mod.ShortHistoryStore(path, ttl_seconds=100, clock=lambda: 1200)
            self.assertEqual(fresh.get("old"), [])

    def test_store_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            now = [1000]
            store = history_mod.ShortHistoryStore(path, max_sessions=2, max_lines=3, clock=lambda: now[0])
            store.set("a", ["1", "2", "3", "4"])
            now[0] += 1
            store.set("b", ["b"])
            now[0] += 1
            store.set("c", ["c"])
            snapshot = store.snapshot()
            self.assertEqual(snapshot["sessions"], 2)
            self.assertLessEqual(snapshot["lines"], 6)
            self.assertIsNone(store.sessions.get("a"))

    def test_corrupt_file_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            path.write_text("{broken", encoding="utf-8")
            store = history_mod.ShortHistoryStore(path)
            self.assertEqual(store.items(), {})


if __name__ == "__main__":
    unittest.main()
