import importlib
import json
import tempfile
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from test_base import Event, make_plugin, m

cp = importlib.import_module(m.__name__ + ".chat_policy")
recovery_mod = importlib.import_module(m.__name__ + ".task_recovery")
poi_mod = importlib.import_module(m.__name__ + ".poi_memory")


def recovery_row(recovery_id="old-1", opcode="gather", params=None):
    return {
        "recovery_id": recovery_id,
        "opcode": opcode,
        "params": params or {"resource": "logs", "count": 4},
        "priority": 20,
        "resources": ["actor", "navigation"],
    }


def plugin_with_new_actions():
    plugin = make_plugin()
    plugin.bridge.client_info = {
        "protocol": 3,
        "capabilities": "move_item,trade,ride,dismount,wake,use_item",
    }
    return plugin


class BasicActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_move_item_calls_client_with_direction(self):
        plugin = plugin_with_new_actions()
        plugin.bridge.call = AsyncMock(return_value={"item": "minecraft:cobblestone", "moved": 1})
        result = await plugin.mc_move_item(Event("mc把圆石放进箱子"), item="minecraft:cobblestone", count=1, direction="to_container")
        self.assertEqual(
            plugin.bridge.call.await_args.args,
            ("move_item", {"item": "minecraft:cobblestone", "count": 1, "direction": "to_container"}, 10),
        )
        self.assertIn("cobblestone", result)

    async def test_move_item_resolves_chinese_name(self):
        plugin = plugin_with_new_actions()
        status = {
            "selected_slot": 0,
            "held": "钻石剑",
            "inventory_items": [{"id": "minecraft:rotten_flesh", "name": "腐肉", "slot": 10}],
        }
        plugin.bridge.call = AsyncMock(side_effect=[status, {"item": "minecraft:rotten_flesh", "moved": 5}])
        await plugin.mc_move_item(Event("mc把腐肉放进箱子"), item="", count=5)
        self.assertEqual(
            plugin.bridge.call.await_args_list[1].args[1],
            {"item": "minecraft:rotten_flesh", "count": 5, "direction": "to_container"},
        )

    async def test_container_transfer_batch_queues_one_task(self):
        plugin = plugin_with_new_actions()
        plugin._queue_skill = Mock(return_value=True)
        result = await plugin.mc_container_transfer(
            Event("mc把东西放进箱子"),
            item="",
            direction="to_container",
            x=-374,
            y=84,
            z=-145,
            items='[{"item":"minecraft:golden_axe","count":1},{"item":"minecraft:furnace","count":2}]',
        )
        self.assertIn("2 种物品", result)
        args, kwargs = plugin._queue_skill.call_args
        self.assertEqual(args[1], "container_transfer")
        self.assertEqual(
            kwargs["items"],
            [{"item": "minecraft:golden_axe", "count": 1}, {"item": "minecraft:furnace", "count": 2}],
        )

    async def test_use_on_entity_targets_entity(self):
        plugin = plugin_with_new_actions()
        plugin.bridge.call = AsyncMock(return_value={"used": True, "target_type": "entity"})
        result = await plugin.mc_use_on_entity(Event("mc喂马"), target="马", item="minecraft:apple")
        self.assertEqual(plugin.bridge.call.await_args.args[0], "use_item")
        self.assertEqual(plugin.bridge.call.await_args.args[1], {"target": "马", "item": "minecraft:apple"})
        self.assertIn("entity", result)

    async def test_trade_open_and_read(self):
        plugin = plugin_with_new_actions()
        plugin.bridge.call = AsyncMock(side_effect=[{"used": True}, {"screen": "MerchantScreen"}, {"offers": [{"index": 0}]}])
        opened = await plugin.mc_trade(Event("mc和村民交易"), operation="open", target="村民")
        self.assertIn("MerchantScreen", opened)
        read = await plugin.mc_trade(Event("mc看看交易"), operation="read")
        self.assertIn("offers", read)
        self.assertEqual(plugin.bridge.call.await_args.args[0], "trade")

    async def test_trade_loops_requested_count(self):
        plugin = plugin_with_new_actions()
        plugin.bridge.call = AsyncMock(side_effect=[
            {"traded": 1, "index": 0},
            {"traded": 1, "index": 0},
        ])
        result = await plugin.mc_trade(Event("mc换两个"), operation="trade", index=0, count=2)
        self.assertEqual(plugin.bridge.call.await_count, 2)
        self.assertIn('"count": 2', result)

    async def test_ride_and_dismount(self):
        plugin = plugin_with_new_actions()
        plugin.bridge.call = AsyncMock(side_effect=[{"target": "boat"}, {"passenger": {"type": "boat"}}])
        riding = await plugin.mc_ride(Event("mc上船"), action="ride", target="船")
        self.assertIn('"riding": true', riding)
        plugin._run_operation = AsyncMock(return_value={"phase": "completed", "dismounted": True})
        dismounted = await plugin.mc_ride(Event("mc下船"), action="dismount")
        plugin._run_operation.assert_awaited_once_with("dismount", {}, seconds=5)
        self.assertIn("dismounted", dismounted)

    async def test_pickup_filters_item_and_calls_client_operation(self):
        plugin = plugin_with_new_actions()
        plugin._run_operation = AsyncMock(return_value={"phase": "completed", "success": True})
        plugin.bridge.call = AsyncMock(return_value={
            "entities": [
                {"uuid": "u1", "name": "腐肉", "distance": 1.5, "x": 1, "y": 2, "z": 3,
                 "item": {"id": "minecraft:rotten_flesh", "name": "腐肉"}},
                {"uuid": "u2", "name": "圆石", "distance": 1.2, "x": 2, "y": 2, "z": 3,
                 "item": {"id": "minecraft:cobblestone", "name": "圆石"}},
            ]
        })
        result = await plugin.mc_pickup(Event("mc捡腐肉"), item="腐肉", count=1)
        self.assertIn('"picked": 1', result)
        plugin._run_operation.assert_awaited_once_with(
            "pickup", {"item": "minecraft:rotten_flesh", "entity_uuid": "u1"}, seconds=5
        )

    async def test_pickup_does_not_auto_excavate_when_blocked(self):
        plugin = plugin_with_new_actions()
        plugin._approach_target = AsyncMock(return_value="blocked by stone")
        plugin._run_operation = AsyncMock(return_value={"phase": "completed", "success": True})
        plugin.bridge.call = AsyncMock(return_value={
            "entities": [
                {"uuid": "u1", "name": "粗铁", "distance": 4.0, "x": 1, "y": 2, "z": 3,
                 "item": {"id": "minecraft:raw_iron", "name": "粗铁"}},
            ]
        })
        result = await plugin.mc_pickup(Event("mc捡粗铁"), item="minecraft:raw_iron", count=1)
        self.assertIn('"picked": 0', result)
        self.assertIn("blocked by stone", result)
        self.assertFalse(plugin._approach_target.await_args.kwargs.get("allow_break"))
        plugin._run_operation.assert_not_awaited()

    async def test_pickup_retries_with_excavation_when_allowed(self):
        plugin = plugin_with_new_actions()
        plugin._approach_target = AsyncMock(return_value=None)
        plugin._run_operation = AsyncMock(return_value={"phase": "completed", "success": True})
        plugin.bridge.call = AsyncMock(return_value={
            "entities": [
                {"uuid": "u1", "name": "粗铁", "distance": 4.0, "x": 1, "y": 2, "z": 3,
                 "item": {"id": "minecraft:raw_iron", "name": "粗铁"}},
            ]
        })
        result = await plugin.mc_pickup(Event("mc捡粗铁"), item="minecraft:raw_iron", count=1, allow_break=True)
        self.assertIn('"picked": 1', result)
        self.assertTrue(plugin._approach_target.await_args.kwargs.get("allow_break"))

    async def test_place_at_rejects_target_inside_player(self):
        plugin = plugin_with_new_actions()
        plugin.bridge.call = AsyncMock(return_value={"position": {"x": 1.2, "y": 64.0, "z": 3.4}})
        plugin._run_operation = AsyncMock()
        result = await plugin.mc_place_at(Event("mc在这里放圆石"), 1, 64, 3, "minecraft:cobblestone")
        self.assertIn("脚下", result)
        plugin._run_operation.assert_not_awaited()

    async def test_place_at_allows_target_beside_player(self):
        plugin = plugin_with_new_actions()
        plugin.bridge.call = AsyncMock(return_value={"position": {"x": 1.2, "y": 64.0, "z": 3.4}})
        plugin._run_operation = AsyncMock(return_value={"phase": "completed", "success": True})
        result = await plugin.mc_place_at(Event("mc在这里放圆石"), 2, 64, 3, "minecraft:cobblestone")
        self.assertIn('"success": true', result)
        plugin._run_operation.assert_awaited_once()

    async def test_craft_queues_skill_task(self):
        plugin = plugin_with_new_actions()
        plugin._enqueue_tasks = Mock(return_value=True)
        result = await plugin.mc_craft(Event("mc合成木棍"), "stick", 4)
        self.assertIn("已加入合成技能队列", result)
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual(task.kind, "skill")
        self.assertEqual(task.target, "craft_item")
        self.assertEqual(task.params, {"item": "minecraft:stick", "count": 4})

    async def test_skill_wrappers_queue_params(self):
        plugin = plugin_with_new_actions()
        plugin._enqueue_tasks = Mock(return_value=True)
        await plugin.mc_gather(Event("mc砍8个木头"), "logs", 8)
        gather_task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual((gather_task.target, gather_task.params["count"]), ("gather", 8))
        await plugin.mc_build(Event("mc建一堵墙"), "wall", "minecraft:cobblestone", 0, 64, 0, 4, 1, 2, "east")
        build_task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual((build_task.target, build_task.params["pattern"]), ("build", "wall"))

    async def test_agent_capabilities_lists_l1_and_l2(self):
        plugin = make_plugin()
        result = json.loads(await plugin.mc_capabilities(Event("mc列出能力")))
        self.assertGreaterEqual(len(result["l1"]), 13)
        self.assertGreaterEqual(len(result["l2"]), 13)
        self.assertGreater(result["l0_count"], 0)

    async def test_agent_task_start_validates_and_sets_resources(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        result = json.loads(await plugin.mc_task_start(
            Event("mc启动任务"), "gather", '{"resource":"coal","count":2}', 20
        ))
        self.assertTrue(result["accepted"])
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual(task.target, "gather")
        self.assertEqual(task.params, {"resource": "coal", "count": 2})
        self.assertEqual(set(task.resources), {"actor", "navigation"})
        self.assertTrue(task.task_id)
        self.assertGreater(task.budget_seconds, 0)
        self.assertEqual(task.deadline, 0)

    async def test_parse_tasks_ex_marks_unparsed_tail(self):
        plugin = make_plugin()
        tasks, unparsed = plugin._parse_tasks_ex("先去 -370 84 -145 然后合成铁镐", "Player")
        self.assertEqual([task.kind for task in tasks], ["goto"])
        self.assertEqual(unparsed, ["合成铁镐"])

    async def test_agent_task_preview_rejects_invalid_dry_run_values(self):
        plugin = make_plugin()
        with self.assertRaises(m.SkillError):
            plugin._agent_task_preview("gather", {"count": "banana", "resource": "not_a_resource"})

    async def test_agent_task_start_auto_intent(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        result = json.loads(await plugin.mc_task_start(
            Event("mc自动任务"), "auto", '{"text":"砍8个木头"}', 20
        ))
        self.assertTrue(result["accepted"])
        self.assertEqual(result["opcode"], "gather")
        self.assertEqual(result["intent"]["opcode"], "gather")
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual(task.target, "gather")
        self.assertEqual(task.params, {"resource": "logs", "count": 8})

    async def test_agent_task_start_with_dependency(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        dependency = plugin._scheduler_for().register(kind="skill", goal="first")
        result = json.loads(await plugin.mc_task_start(
            Event("mc链式任务"), "gather", '{"resource":"logs","count":2}', 10, False, dependency.id
        ))
        self.assertTrue(result["accepted"])
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual(task.dependencies, (dependency.id,))
        self.assertEqual(task.resources, ("actor", "navigation"))

    async def test_agent_task_start_rejects_failed_dependency(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        dependency = plugin._scheduler_for().register(kind="skill", goal="first")
        plugin._scheduler_for().cancel(dependency.id, "failed")
        result = await plugin.mc_task_start(
            Event("mc链式任务"), "gather", '{"resource":"logs","count":2}', 10, False, dependency.id
        )
        self.assertIn("依赖任务", result)
        plugin._enqueue_tasks.assert_not_called()

    async def test_agent_task_start_rejects_unknown_params(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        result = await plugin.mc_task_start(Event("mc启动任务"), "gather", '{"bad":1}', 20)
        self.assertIn("任务未启动", result)
        plugin._enqueue_tasks.assert_not_called()

    async def test_agent_task_start_dry_run_does_not_enqueue(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        result = json.loads(await plugin.mc_task_start(
            Event("mc预览任务"), "return_home", "{}", 10, True
        ))
        self.assertTrue(result["planned"])
        self.assertEqual(result["opcode"], "return_home")
        self.assertEqual(result["resources"], ["actor", "navigation"])
        self.assertNotIn("task_id", result)
        plugin._enqueue_tasks.assert_not_called()

    async def test_agent_task_status_and_cancel_unknown(self):
        plugin = make_plugin()
        status = json.loads(await plugin.mc_task_status(Event("mc任务状态")))
        self.assertEqual(status["pending_count"], 0)
        cancelled = json.loads(await plugin.mc_task_cancel(Event("mc取消任务"), "missing-task"))
        self.assertFalse(cancelled["found"])

    async def test_new_l1_capability_tools(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        await plugin.mc_equip_best(Event("mc装备最佳镐"), "pickaxe")
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual((task.target, task.params), ("equip_best", {"tool_type": "pickaxe"}))

        plugin._enqueue_tasks.reset_mock()
        await plugin.mc_harvest_block(Event("mc采集方块"), 1, 64, 2, "minecraft:oak_log")
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual(task.target, "harvest_block")
        self.assertEqual(task.params["block_pos"], [1, 64, 2])
        self.assertEqual(task.params["item_filter"], "minecraft:oak_log")

        plugin._enqueue_tasks.reset_mock()
        await plugin.mc_return_to(Event("mc返回坐标"), 1, 64, 2, 2.5)
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual(task.target, "return_to")
        self.assertEqual(task.params["target_pos"], [1, 64, 2])
        self.assertEqual(task.params["reach"], 2.5)

        plugin._skills_for = Mock(return_value=SimpleNamespace(
            eat_until_full=AsyncMock(return_value={"eaten": 1, "food": 20})
        ))
        result = await plugin.mc_eat_until_full(Event("mc吃到饱"))
        self.assertIn('"food": 20', result)

    async def test_plant_tools_queue_farmland_and_seed(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        await plugin.mc_plant_at(Event("mc种植"), 1, 64, 2)
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual(task.target, "plant_at")
        self.assertEqual(task.params["farmland_pos"], [1, 64, 2])
        self.assertEqual(task.params["seed"], "minecraft:wheat_seeds")

        plugin._enqueue_tasks.reset_mock()
        await plugin.mc_till_and_plant(Event("mc耕地并种"), 3, 64, 4, "minecraft:carrot")
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual(task.target, "till_and_plant")
        self.assertEqual(task.params["block_pos"], [3, 64, 4])
        self.assertEqual(task.params["seed"], "minecraft:carrot")

    async def test_goto_resolves_non_standable_block_to_nearest_stand(self):
        plugin = make_plugin()
        plugin.bridge.client_info = {"protocol": 3, "capabilities": "block_context"}
        plugin._enqueue_tasks = Mock(return_value=True)
        plugin._call = AsyncMock(return_value={
            "standable": False, "block": "minecraft:enchanting_table",
            "stands": [{"x": -363, "y": 89, "z": -140, "feet_y": 89, "distance": 1}],
        })
        result = await plugin.mc_goto(Event("mc去附魔台"), -364, 89, -140)
        self.assertIn("改到最近站位", result)
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual((task.x, task.y, task.z), (-363, 89, -140))

    async def test_goto_rejects_target_without_any_stand(self):
        plugin = make_plugin()
        plugin.bridge.client_info = {"protocol": 3, "capabilities": "block_context"}
        plugin._enqueue_tasks = Mock(return_value=True)
        plugin._call = AsyncMock(return_value={"standable": False, "stands": []})
        result = await plugin.mc_goto(Event("mc去墙里"), 10, 64, 10)
        self.assertIn("不可站立", result)
        plugin._enqueue_tasks.assert_not_called()

    async def test_gather_allows_night_without_extra_authorization(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        result = await plugin.mc_gather(Event("mc去挖煤"), "coal", 1)
        self.assertIn("采集任务已接受", result)
        plugin._enqueue_tasks.assert_called_once()

    async def test_explore_queues_goal_without_keyword_collision(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        result = await plugin.mc_explore(Event("mc探索煤矿"), goal="coal", max_seconds=30, max_distance=32)
        self.assertIn("已加入探索技能队列", result)
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual(task.params["goal"], "coal")

    async def test_gather_rejects_resource_drift_without_explicit_word(self):
        import time
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        plugin._last_gather_resource = "coal"
        plugin._last_gather_resource_at = time.monotonic()
        result = await plugin.mc_gather(Event("mc再去挖呢"), "stone", 20)
        self.assertIn("上一轮采集目标是coal", result)
        plugin._enqueue_tasks.assert_not_called()

    async def test_ladder_assist_retries_same_goto(self):
        plugin = make_plugin()
        plugin.bridge.client_info = {"protocol": 3, "capabilities": "ladder_assist"}
        plugin._run_operation = AsyncMock(return_value={"entered": True, "end_y": 85.0})
        plugin._call = AsyncMock(return_value={"ok": True})
        self.assertTrue(await plugin._try_ladder_assist(-370, 85, -153))
        plugin._run_operation.assert_awaited_once()
        plugin._call.assert_awaited_once()
        self.assertEqual(plugin._call.await_args.args[0], "baritone")
        self.assertIn("goto -370 85 -153", plugin._call.await_args.args[1]["command"])

    async def test_brief_combines_status_task_and_memory(self):
        plugin = make_plugin()
        plugin._call = AsyncMock(return_value={
            "position": {"x": 1, "y": 2, "z": 3},
            "dimension": "minecraft:overworld",
            "health": 20,
            "food": 19,
            "hostiles": [],
        })
        plugin._agent_task_status = Mock(return_value={
            "active": None, "pending_count": 0, "current_task_id": "", "queue": [],
        })
        memory = Mock()
        memory.get.return_value = {"x": 1, "y": 2, "z": 3, "name": "床"}
        plugin._poi_memory_for = Mock(return_value=memory)
        result = json.loads(await plugin.mc_brief(Event("mc简报")))
        self.assertEqual(result["position"]["x"], 1)
        self.assertEqual(result["home_bed"]["name"], "床")
        plugin._call.assert_awaited_once_with("status", timeout=10)

    async def test_wait_returns_compact_task_state(self):
        plugin = make_plugin()
        plugin._active_task = None
        plugin._task_current = None
        result = json.loads(await plugin.mc_wait(Event("mc等等"), 0.1))
        self.assertIn("waited", result)
        self.assertFalse(result["active"])

    def test_vision_guard_blocks_coordinate_questions(self):
        plugin = make_plugin()
        self.assertFalse(plugin._vision_question_allowed("周围环境和下楼的路在哪"))
        self.assertFalse(plugin._vision_question_allowed("前面有几张床，最左边那张在哪"))
        self.assertFalse(plugin._vision_question_allowed("屏幕上有没有重生点提示"))
        self.assertTrue(plugin._vision_question_allowed("描述当前画面"))
        self.assertTrue(plugin._vision_question_allowed("这张床是什么颜色"))

    async def test_align_rejects_far_target_before_client_action(self):
        plugin = make_plugin()
        plugin._call = AsyncMock(return_value={"position": {"x": 0, "y": 64, "z": 0}})
        plugin._run_operation = Mock()
        result = await plugin.mc_align(Event("mc对齐"), 10, 64, 10)
        self.assertIn("先用 mc_goto", result)
        plugin._run_operation.assert_not_called()

    async def test_set_respawn_writes_home_bed_after_bed_interaction(self):
        plugin = make_plugin()
        plugin._approach_target = AsyncMock(return_value=None)
        plugin._call = AsyncMock(side_effect=[
            {"world_key": "world", "dimension": "minecraft:overworld", "position": {"x": 0, "y": 64, "z": 0}},
            {"bed": True, "bed_occupied": False, "block": "minecraft:red_bed",
             "stands": [{"x": 2, "y": 64, "z": 2, "feet_y": 64, "distance": 1}]},
            {"interacted": True, "block": "minecraft:red_bed"},
        ])
        with tempfile.TemporaryDirectory() as folder:
            plugin._poi_memory = poi_mod.PoiMemory(Path(folder) / "poi.json")
            result = json.loads(await plugin.mc_set_respawn(Event("mc把这张床设成重生点"), 1, 64, 2))
            self.assertTrue(result["saved"])
            self.assertEqual(plugin._approach_target.await_args.args[:3], (2, 64, 2))
            row = plugin._poi_memory.get("home_bed", "main")
            self.assertEqual((row["x"], row["y"], row["z"]), (1, 64, 2))
            self.assertEqual(row["world_key"], "world")

    async def test_set_respawn_uses_other_bed_half_when_head_has_no_stand(self):
        plugin = make_plugin()
        plugin._approach_target = AsyncMock(return_value=None)
        plugin._call = AsyncMock(side_effect=[
            {"world_key": "world", "dimension": "minecraft:overworld"},
            {"bed": True, "bed_occupied": False, "block": "minecraft:red_bed",
             "state": "part=head", "stands": []},
            {"bed": True, "bed_occupied": False, "block": "minecraft:red_bed",
             "state": "part=foot", "stands": [{"x": 3, "y": 64, "z": 2, "feet_y": 64, "distance": 1}]},
            {"interacted": True, "block": "minecraft:red_bed"},
        ])
        with tempfile.TemporaryDirectory() as folder:
            plugin._poi_memory = poi_mod.PoiMemory(Path(folder) / "poi.json")
            result = json.loads(await plugin.mc_set_respawn(Event("mc把这张床设成重生点"), 1, 64, 2))
            self.assertTrue(result["saved"])
            self.assertEqual(plugin._approach_target.await_args.args[:3], (3, 64, 2))
            self.assertIsNotNone(plugin._poi_memory.get("home_bed", "main"))

    async def test_set_respawn_rejects_non_bed(self):
        plugin = make_plugin()
        plugin._call = AsyncMock(side_effect=[
            {"world_key": "world", "dimension": "minecraft:overworld"},
            {"bed": False, "bed_occupied": False, "block": "minecraft:stone"},
        ])
        with tempfile.TemporaryDirectory() as folder:
            plugin._poi_memory = poi_mod.PoiMemory(Path(folder) / "poi.json")
            result = await plugin.mc_set_respawn(Event("mc把这块石头设成重生点"), 1, 64, 2)
            self.assertIn("不是可用床", result)
            self.assertIsNone(plugin._poi_memory.get("home_bed", "main"))

    async def test_agent_status_reports_recovered_tasks(self):
        plugin = make_plugin()
        with tempfile.TemporaryDirectory() as folder:
            plugin._task_recovery = recovery_mod.TaskRecovery(Path(folder) / "recovery.json")
            plugin._task_recovery.merge([recovery_row()])
            status = json.loads(await plugin.mc_task_status(Event("mc任务状态")))
            self.assertEqual(status["recovered_count"], 1)
            self.assertNotIn("journal", status)
            self.assertNotIn("by_opcode", status["metrics"])
            detail = json.loads(await plugin.mc_task_status(Event("mc任务状态"), detail=True))
            self.assertEqual(detail["recovered"]["items"][0]["recovery_id"], "old-1")

    async def test_agent_resume_recovered_task(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as folder:
            plugin._task_recovery = recovery_mod.TaskRecovery(Path(folder) / "recovery.json")
            plugin._task_recovery.merge([recovery_row()])
            result = json.loads(await plugin.mc_task_start(
                Event("mc恢复任务"), "resume", '{"task_id":"old-1"}', 10
            ))
            self.assertTrue(result["accepted"])
            self.assertEqual(result["opcode"], "gather")
            self.assertEqual(result["resumed_from"], "old-1")
            task = plugin._enqueue_tasks.call_args.args[0][0]
            self.assertEqual(task.params, {"resource": "logs", "count": 4})
            self.assertIsNone(plugin._task_recovery.get("old-1"))

    async def test_agent_resume_restores_group_dependencies_and_checkpoint(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as folder:
            store = recovery_mod.TaskRecovery(Path(folder) / "recovery.json")
            store.merge([
                {
                    "recovery_id": "t1", "group_id": "g1", "opcode": "gather",
                    "params": {"resource": "logs", "count": 8},
                    "checkpoint": {"skill": "gather", "stage": "collecting", "completed": 3, "total": 8},
                },
                {
                    "recovery_id": "t2", "group_id": "g1", "opcode": "craft",
                    "params": {"item": "minecraft:stick", "count": 4},
                    "dependencies": ["t1"],
                },
            ])
            plugin._task_recovery = store
            result = json.loads(await plugin.mc_task_start(
                Event("mc恢复任务"), "resume", '{"task_id":"t1"}', 10
            ))
            self.assertTrue(result["accepted"])
            self.assertEqual(result["resume_mode"], "checkpoint")
            tasks = plugin._enqueue_tasks.call_args.args[0]
            self.assertEqual(len(tasks), 2)
            by_target = {task.target: task for task in tasks}
            self.assertEqual(by_target["gather"].params["count"], 5)
            self.assertEqual(by_target["craft"].dependencies, (by_target["gather"].task_id,))
            self.assertEqual(result["tasks"], 2)
            self.assertIsNone(store.get("t1"))

    async def test_agent_resume_uses_remaining_budget_and_marks_rerun(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as folder:
            plugin._task_recovery = recovery_mod.TaskRecovery(Path(folder) / "recovery.json")
            row = recovery_row(params={"resource": "logs", "count": 4, "max_seconds": 180})
            row["remaining_seconds"] = 30
            plugin._task_recovery.merge([row])
            result = json.loads(await plugin.mc_task_start(
                Event("mc恢复任务"), "resume", '{"task_id":"old-1"}', 10
            ))
            self.assertEqual(result["resume_mode"], "rerun")
            task = plugin._enqueue_tasks.call_args.args[0][0]
            self.assertEqual(task.params["max_seconds"], 30)

    async def test_agent_cancel_discards_recovered_task(self):
        plugin = make_plugin()
        with tempfile.TemporaryDirectory() as folder:
            plugin._task_recovery = recovery_mod.TaskRecovery(Path(folder) / "recovery.json")
            plugin._task_recovery.merge([recovery_row()])
            result = json.loads(await plugin.mc_task_cancel(Event("mc取消任务"), "old-1"))
            self.assertTrue(result["recovered"])
            self.assertTrue(result["discarded"])
            self.assertIsNone(plugin._task_recovery.get("old-1"))

    async def test_open_and_read_queues_container(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        await plugin.mc_open_and_read(Event("mc打开容器"), 5, 65, 6)
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual(task.target, "open_and_read")
        self.assertEqual(task.params["container_pos"], [5, 65, 6])

    async def test_place_line_queues_validated_line(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        await plugin.mc_place_line(Event("mc铺一行"), 1, 64, 1, "south", 32, "minecraft:stone")
        task = plugin._enqueue_tasks.call_args.args[0][0]
        self.assertEqual(task.target, "place_line")
        self.assertEqual(task.params, {
            "start": [1, 64, 1], "direction": "south", "length": 32, "material": "minecraft:stone",
        })

    async def test_place_line_rejects_unknown_direction(self):
        plugin = make_plugin()
        plugin._enqueue_tasks = Mock(return_value=True)
        result = await plugin.mc_place_line(Event("mc铺一行"), 1, 64, 1, "up")
        self.assertIn("direction", result)
        plugin._enqueue_tasks.assert_not_called()

    async def test_operation_runner_polls_until_completed(self):
        plugin = plugin_with_new_actions()
        plugin._call = AsyncMock(side_effect=[
            {"operation_id": "op-1", "phase": "started"},
            {"phase": "running"},
            {"phase": "completed", "success": True, "produced": 1},
        ])
        result = await plugin._run_operation("craft", {"item": "minecraft:stick", "count": 1}, seconds=2)
        self.assertTrue(result["success"])
        self.assertEqual(plugin._call.await_count, 3)

    async def test_wake_reports_sleep_state(self):
        plugin = plugin_with_new_actions()
        plugin.bridge.call = AsyncMock(side_effect=[{"awake": True}, {"sleeping": False}])
        result = await plugin.mc_wake(Event("mc起床"))
        self.assertIn('"awake": true', result)
        self.assertEqual(plugin.bridge.call.await_args_list[0].args[0], "wake")

    async def test_dig_down_uses_controlled_operation(self):
        plugin = plugin_with_new_actions()
        plugin._run_operation = AsyncMock(return_value={"phase": "completed", "success": True, "depth": 3})
        result = await plugin.mc_dig_down(Event("mc向下挖三格"), depth=3)
        plugin._run_operation.assert_awaited_once_with("dig_down", {"depth": 3}, seconds=28)
        self.assertIn('"depth": 3', result)


class BasicActionRouting(unittest.TestCase):
    def test_new_action_tools_are_in_action_group(self):
        for text, tool in [
            ("把腐肉放进箱子", "mc_move_item"),
            ("喂马吃苹果", "mc_use_on_entity"),
            ("跟村民交易", "mc_trade"),
            ("骑船", "mc_ride"),
            ("下马", "mc_ride"),
            ("起床", "mc_wake"),
            ("捡起地上的腐肉", "mc_pickup"),
            ("打僵尸", "mc_combat"),
            ("探索附近", "mc_explore"),
            ("补给一下", "mc_resupply"),
            ("向下挖三格", "mc_dig_down"),
        ]:
            group, names = cp.tool_group(text, allow_actions=True, owner=True)
            self.assertNotEqual(group, "chat", text)
            self.assertIn(tool, names, text)


class SchedulerIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_weapon_rank_prefers_higher_tier(self):
        plugin = make_plugin()
        self.assertGreater(plugin._weapon_rank("minecraft:diamond_sword"), plugin._weapon_rank("minecraft:iron_sword"))
        self.assertGreater(plugin._weapon_rank("minecraft:iron_sword"), plugin._weapon_rank("minecraft:stone_axe"))
        self.assertLess(plugin._weapon_rank("minecraft:wooden_sword"), plugin._weapon_rank("minecraft:iron_sword"))

    async def test_select_best_weapon_switches_hotbar(self):
        plugin = make_plugin()
        plugin.bridge.call = AsyncMock(side_effect=[
            {"inventory_items": [{"id": "minecraft:iron_axe", "slot": 1}, {"id": "minecraft:diamond_sword", "slot": 2}]},
            {},
        ])
        selected = await plugin._select_best_weapon()
        self.assertEqual(selected, "minecraft:diamond_sword")
        self.assertEqual(plugin.bridge.call.await_args_list[-1].args[0], "select_slot")
        self.assertEqual(plugin.bridge.call.await_args_list[-1].args[1], {"slot": 2})

    async def test_worker_dispatches_skill_task(self):
        plugin = make_plugin()
        fake = SimpleNamespace(run=AsyncMock(return_value={"skill": "gather", "collected": 1}))
        plugin._skills_for = Mock(return_value=fake)
        plugin._task_chat = AsyncMock()
        task = m.QueuedTask("skill", "采集", target="gather", params={"resource": "logs", "count": 1})
        plugin._task_queue.append(task)
        await plugin._task_worker()
        fake.run.assert_awaited_once_with("gather", {"resource": "logs", "count": 1})
        self.assertFalse(plugin._task_queue)

    async def test_enqueue_registers_task_priority_and_id(self):
        plugin = make_plugin()
        plugin._autonomy = None
        plugin._run_goto_task = AsyncMock()
        task = m.QueuedTask("goto", "go", x=1, y=2, z=3)
        self.assertTrue(plugin._enqueue_tasks([task]))
        queued = plugin._task_queue[0]
        self.assertEqual(queued.priority, int(m.Priority.OWNER))
        self.assertTrue(queued.task_id)
        registered = plugin._scheduler.get(queued.task_id)
        self.assertIsNotNone(registered)
        self.assertEqual(registered.priority, int(m.Priority.OWNER))
        plugin._cancel_active_task()
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
