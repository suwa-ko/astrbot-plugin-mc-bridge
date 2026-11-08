import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_base import Event, make_plugin, m


class P02Fixes(unittest.IsolatedAsyncioTestCase):
    async def test_mine_retries_once_after_auto_approach(self):
        plugin = make_plugin()
        calls = []

        async def operation(action, params, seconds=0, timeout=10):
            calls.append((action, dict(params)))
            if len(calls) == 1:
                raise m.SurvivalError("target block is outside interaction reach")
            return {"phase": "completed", "success": True}

        plugin._run_operation = operation
        plugin._approach_target = AsyncMock(return_value=None)
        result = await plugin._operation_near("mine_at", {"x": 1, "y": 2, "z": 3}, 12)
        self.assertTrue(result["success"])
        plugin._approach_target.assert_awaited_once_with(1, 2, 3)
        self.assertEqual(len(calls), 2)

    async def test_mine_stops_when_approach_fails(self):
        plugin = make_plugin()
        plugin._run_operation = AsyncMock(
            side_effect=m.SurvivalError("target block is outside interaction reach")
        )
        plugin._approach_target = AsyncMock(return_value="靠近目标无进展，已停止")
        with self.assertRaises(m.SurvivalError) as ctx:
            await plugin._operation_near("mine_at", {"x": 1, "y": 2, "z": 3}, 12)
        self.assertIn("靠近目标无进展", str(ctx.exception))
        plugin._run_operation.assert_awaited_once()

    async def test_non_reach_errors_do_not_trigger_approach(self):
        plugin = make_plugin()
        plugin._run_operation = AsyncMock(side_effect=m.SurvivalError("target is protected"))
        plugin._approach_target = AsyncMock(return_value=None)
        with self.assertRaises(m.SurvivalError):
            await plugin._operation_near("mine_at", {"x": 1, "y": 2, "z": 3}, 12)
        plugin._approach_target.assert_not_awaited()

    async def test_inventory_sort_uses_one_client_call(self):
        plugin = make_plugin()
        plugin.bridge.call = AsyncMock(return_value={"sorted": True, "moves": []})
        result = await plugin.mc_inventory(Event("mc整理背包"), operation="sort")
        plugin.bridge.call.assert_awaited_once_with("sort_inventory", {}, 10)
        self.assertIn('"sorted"', result)

    async def test_approach_stops_when_control_epoch_changes(self):
        plugin = make_plugin()
        plugin.bridge.client_info = {"protocol": 3}
        plugin.bridge.call = AsyncMock(return_value={"position": {"x": 0, "y": 64, "z": 0}})

        async def fake_call(action, params=None, timeout=20.0):
            if action == "status":
                plugin._control_epoch += 1
                return {"position": {"x": 0, "y": 64, "z": 0}}
            if action == "navigate":
                return {"ok": True}
            if action == "baritone":
                return {"ok": True}
            return {}

        plugin._call = fake_call
        token = m.CONTROL_EPOCH.set(0)
        try:
            result = await plugin._approach_target(10, 64, 0, seconds=5, reach=1.0)
        finally:
            m.CONTROL_EPOCH.reset(token)
        self.assertIn("停止", result)

    async def test_readonly_inspect_does_not_wait_for_action_lock(self):
        plugin = make_plugin()
        plugin._action_lock = asyncio.Lock()
        await plugin._action_lock.acquire()
        plugin.bridge.call = AsyncMock(return_value={"block": "minecraft:air"})
        result = await asyncio.wait_for(plugin._call("inspect_block", {"x": 1, "y": 2, "z": 3}), timeout=1)
        self.assertEqual(result["block"], "minecraft:air")
        plugin._action_lock.release()

    async def test_drop_item_matches_chinese_inventory_name(self):
        plugin = make_plugin()
        status = {
            "selected_slot": 0,
            "held": "钻石剑",
            "inventory_items": [
                {"id": "minecraft:diamond_sword", "name": "钻石剑", "slot": 0},
                {"id": "minecraft:rotten_flesh", "name": "腐肉", "slot": 10},
            ],
        }
        plugin.bridge.call = AsyncMock(side_effect=[status, {"item": "minecraft:rotten_flesh", "count": 1, "dropped": True}])
        result = await plugin.mc_drop_item(Event("mc丢一个腐肉出来"))
        self.assertEqual(plugin.bridge.call.await_args_list[1].args, ("drop_item", {"item": "minecraft:rotten_flesh", "count": 1}, 10))
        self.assertIn("rotten_flesh", result)

    async def test_drop_item_all_uses_whole_stack(self):
        plugin = make_plugin()
        status = {
            "selected_slot": 0,
            "held": "钻石剑",
            "inventory_items": [{"id": "minecraft:rotten_flesh", "name": "腐肉", "slot": 10}],
        }
        plugin.bridge.call = AsyncMock(side_effect=[status, {"item": "minecraft:rotten_flesh", "count": 5, "dropped": True}])
        await plugin.mc_drop_item(Event("mc把腐肉都丢出来"))
        self.assertEqual(plugin.bridge.call.await_args_list[1].args[1], {"item": "minecraft:rotten_flesh", "count": 64})

    async def test_drop_item_selected_only_when_explicit(self):
        plugin = make_plugin()
        status = {
            "selected_slot": 0,
            "held": "钻石剑",
            "inventory_items": [{"id": "minecraft:diamond_sword", "name": "钻石剑", "slot": 0}],
        }
        plugin.bridge.call = AsyncMock(side_effect=[status, {"item": "minecraft:diamond_sword", "count": 1, "dropped": True}])
        await plugin.mc_drop_item(Event("mc把手里这个丢掉"))
        self.assertEqual(plugin.bridge.call.await_args_list[1].args[1], {"item": "minecraft:diamond_sword", "count": 1})

    async def test_drop_item_without_match_does_not_drop_selected(self):
        plugin = make_plugin()
        status = {
            "selected_slot": 0,
            "held": "钻石剑",
            "inventory_items": [{"id": "minecraft:diamond_sword", "name": "钻石剑", "slot": 0}],
        }
        plugin.bridge.call = AsyncMock(return_value=status)
        result = await plugin.mc_drop_item(Event("mc丢一个"))
        self.assertIn("请指定", result)
        self.assertEqual(plugin.bridge.call.await_count, 1)

    async def test_new_l0_wrappers_dispatch_exact_actions(self):
        plugin = make_plugin()
        plugin._run_operation = AsyncMock(return_value={"phase": "completed", "success": True})

        await plugin.mc_unequip_armor(Event("mc脱下胸甲"), slot="chest")
        plugin._run_operation.assert_awaited_once_with("unequip_armor", {"slot": "chest"})

        plugin._run_operation.reset_mock()
        await plugin.mc_till_block(Event("mc把这块地耕了"), 1, 2, 3)
        plugin._run_operation.assert_awaited_once_with(
            "till", {"x": 1, "y": 2, "z": 3, "item": ""}, seconds=5
        )

        plugin._run_operation.reset_mock()
        await plugin.mc_jump_once(Event("mc跳一下"))
        plugin._run_operation.assert_awaited_once_with("jump", {}, seconds=5)

    async def test_menu_and_bonemeal_wrappers_dispatch_exact_actions(self):
        plugin = make_plugin()
        plugin._call = AsyncMock(return_value={"screen": "none", "slots": []})
        await plugin.mc_get_menu(Event("mc看看菜单"))
        plugin._call.assert_awaited_once_with("get_menu", {}, timeout=10)

        plugin._run_operation = AsyncMock(return_value={"phase": "completed", "success": True})
        await plugin.mc_use_bonemeal(Event("mc给作物施肥"), 1, 64, 2)
        plugin._run_operation.assert_awaited_once_with(
            "use_bonemeal", {"x": 1, "y": 64, "z": 2, "item": "minecraft:bone_meal"}, seconds=5
        )


if __name__ == '__main__':
    unittest.main()
