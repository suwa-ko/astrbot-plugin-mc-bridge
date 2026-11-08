import importlib
import json
import tempfile
import unittest
from pathlib import Path

from test_base import m

poi = importlib.import_module(m.__name__ + ".poi_memory")


class PoiMemoryTests(unittest.TestCase):
    def test_set_get_list_forget_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "poi.json"
            memory = poi.PoiMemory(path)
            memory.set("home_bed", "main", name="家床", world_key="w", dimension="d", x=1, y=64, z=2)
            row = memory.get("home_bed", "main")
            self.assertEqual((row["x"], row["y"], row["z"]), (1, 64, 2))
            self.assertEqual(len(memory.list("home_bed")), 1)
            self.assertTrue(memory.forget("home_bed", "main"))
            self.assertIsNone(memory.get("home_bed", "main"))
            restored = poi.PoiMemory(path)
            self.assertEqual(restored.list(), [])

    def test_invalid_kind_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = poi.PoiMemory(Path(folder) / "poi.json")
            with self.assertRaises(ValueError):
                memory.set("not_a_kind", "main", x=1, y=2, z=3)

    def test_corrupt_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "poi.json"
            path.write_text("{broken", encoding="utf-8")
            memory = poi.PoiMemory(path)
            self.assertEqual(memory.list(), [])


if __name__ == "__main__":
    unittest.main()
