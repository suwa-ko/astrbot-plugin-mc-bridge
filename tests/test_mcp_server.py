import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_base import m

mcp_mod = importlib.import_module(m.__name__ + ".mc_bridge.mcp_server")
async_mem_mod = importlib.import_module(m.__name__ + ".mc_bridge.async_memory")
mem_mod = importlib.import_module(m.__name__ + ".memory_store")
coach_mod = importlib.import_module(m.__name__ + ".mc_bridge.coach")
sched_mod = importlib.import_module(m.__name__ + ".scheduler")


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_resources_and_tools_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = mem_mod.MemoryStore(Path(tmp) / "memory.db")
            async_mem = async_mem_mod.AsyncMemoryStore(store)
            coach_store = coach_mod.CoachStore(Path(tmp) / "coach.db")
            coach = coach_mod.CoachEngine(coach_store)
            scheduler = sched_mod.UnifiedScheduler()
            scheduler.register(kind="mine_ore", goal="挖铁矿")

            bridge = SimpleNamespace(
                connected=True,
                client_info={"protocol": 4, "version": "1.0.0", "capabilities": ["operation", "operation_events", "hunt"]},
                call=AsyncMock(side_effect=lambda action, params=None, timeout=10: (
                    {
                        "player": "Bot",
                        "dimension": "minecraft:overworld",
                        "biome": "minecraft:plains",
                        "health": 20.0,
                        "food": 18,
                        "survival_busy": False,
                        "position": {"x": 123.4, "y": 64.0, "z": -456.7},
                    }
                    if action == "status"
                    else {"items": [{"id": "minecraft:bread", "count": 8}]}
                    if action == "inventory"
                    else {"ok": True, "action": action}
                )),
            )
            adapter = mcp_mod.McBridgeMcpAdapter(
                bridge=bridge,
                memory=async_mem,
                coach=coach,
                scheduler=scheduler,
            )
            try:
                # 1. mc://status returns Protocol 4 capabilities and hides raw coordinates
                status_raw = await adapter.read_resource("mc://status")
                status_obj = json.loads(status_raw)
                self.assertTrue(status_obj["connected"])
                self.assertEqual(status_obj["protocol"], 4)
                self.assertIn("operation_events", status_obj["capabilities"])
                self.assertNotIn("position", status_obj)

                # 2. mc://inventory & mc://tasks/active
                inv_obj = json.loads(await adapter.read_resource("mc://inventory"))
                self.assertEqual(inv_obj["items"][0]["id"], "minecraft:bread")
                tasks_obj = json.loads(await adapter.read_resource("mc://tasks/active"))
                self.assertEqual(len(tasks_obj["pending"]), 1)

                # 3. Teach via MCP tool and verify mc://coach/playbook resource
                teach_obj = json.loads(await adapter.call_tool("mc_coach_teach", {"scope": "mine_ore", "rule": "挖矿前检查镐子耐久"}))
                self.assertTrue(teach_obj["ok"])
                pb_obj = json.loads(await adapter.read_resource("mc://coach/playbook"))
                self.assertEqual(len(pb_obj["active_lessons"]), 1)
                self.assertIn("挖矿前检查镐子耐久", pb_obj["active_lessons"][0]["rule"])

                # 4. mc_stop & mc://memory/places
                stop_obj = json.loads(await adapter.call_tool("mc_stop"))
                self.assertTrue(stop_obj["stopped"])
                places_obj = json.loads(await adapter.read_resource("mc://memory/places"))
                self.assertEqual(places_obj["places"], [])
            finally:
                store.close()
                coach_store.close()


if __name__ == "__main__":
    unittest.main()
