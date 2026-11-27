import asyncio
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_base import m, make_plugin

ctx_mod = importlib.import_module(m.__name__ + ".mc_bridge.context")
rt_mod = importlib.import_module(m.__name__ + ".mc_bridge.runtime")
srv_mod = importlib.import_module(m.__name__ + ".mc_bridge.server")
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

    def test_parse_capabilities_supports_protocol_3_and_4(self):
        self.assertEqual(srv_mod.parse_capabilities("hunt, survival_events, operation"), {"hunt", "survival_events", "operation"})
        self.assertEqual(srv_mod.parse_capabilities(["hunt", "operation_events", ""]), {"hunt", "operation_events"})
        self.assertEqual(srv_mod.parse_capabilities(None), set())

    async def test_protocol4_operation_done_push_wakes_waiter(self):
        srv = srv_mod.BridgeServer("127.0.0.1", 8766, "/ws", "secret")
        srv.ws = SimpleNamespace(closed=False)

        async def emit():
            await asyncio.sleep(0.02)
            await srv._on_message(json.dumps({
                "type": "event",
                "event": "operation_done",
                "data": {"operation_id": "op-42", "phase": "completed", "success": True, "mined": 1},
            }))

        feeder = asyncio.create_task(emit())
        result = await srv.wait_operation("op-42", timeout=1.0)
        await feeder
        self.assertIsNotNone(result)
        self.assertEqual(result["operation_id"], "op-42")
        self.assertTrue(result["success"])

    async def test_protocol4_run_operation_uses_push_without_polling(self):
        p = make_plugin()
        srv = srv_mod.BridgeServer("127.0.0.1", 8766, "/ws", "secret")
        srv.ws = SimpleNamespace(closed=False)
        srv.client_info = {"protocol": 4, "capabilities": ["operation", "operation_events"]}
        p.bridge = srv
        p._call = AsyncMock(return_value={"operation_id": "op-99", "phase": "started"})
        # Simulate operation_done arriving immediately before wait_operation
        await srv._on_message(json.dumps({
            "type": "event",
            "event": "operation_done",
            "data": {"operation_id": "op-99", "phase": "completed", "success": True},
        }))
        out = await p._run_operation("mine_at", {"x": 1, "y": 64, "z": 1}, seconds=5)
        self.assertTrue(out["success"])
        # Only the initial start call happened; zero "operation" poll calls!
        self.assertEqual(p._call.await_count, 1)


if __name__ == "__main__":
    unittest.main()
