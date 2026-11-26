import importlib
import tempfile
import unittest
from pathlib import Path

from test_base import m

ctx_mod = importlib.import_module(m.__name__ + ".mc_bridge.context")
rt_mod = importlib.import_module(m.__name__ + ".mc_bridge.runtime")
async_mem_mod = importlib.import_module(m.__name__ + ".mc_bridge.async_memory")
mem_mod = importlib.import_module(m.__name__ + ".memory_store")
skills_mod = importlib.import_module(m.__name__ + ".skills")


class ModularEngineTests(unittest.IsolatedAsyncioTestCase):
    def test_shared_context_vars_identity(self):
        self.assertIs(m.CONTROL_EPOCH, ctx_mod.CONTROL_EPOCH)
        self.assertIs(m.CONTROL_EPOCH, rt_mod.CONTROL_EPOCH)
        self.assertIs(m.CURRENT_EVENT, ctx_mod.CURRENT_EVENT)
        self.assertIs(m.CURRENT_EVENT, rt_mod.CURRENT_EVENT)

    def test_skills_facade_exports_and_composition(self):
        self.assertTrue(hasattr(skills_mod, "VanillaSkills"))
        self.assertTrue(hasattr(skills_mod, "HUNTABLE"))
        self.assertTrue(hasattr(skills_mod, "HUNT_ALIASES"))
        for method in ("gather", "craft", "smelt", "build", "farm", "farm_plot", "mine_ore", "hunt", "resupply"):
            self.assertTrue(callable(getattr(skills_mod.VanillaSkills, method, None)), method)

    async def test_async_memory_store_offloads_sqlite_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = mem_mod.MemoryStore(Path(tmp) / "memory.db")
            async_store = async_mem_mod.AsyncMemoryStore(store)
            try:
                world = await async_store.ensure_world("server:test")
                self.assertTrue(world)
                places = await async_store.find_places(world_key="server:test", world_instance=world)
                self.assertEqual(places, [])
                via_call = await async_store.call("find_places", world_key="server:test", world_instance=world)
                self.assertEqual(via_call, [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
