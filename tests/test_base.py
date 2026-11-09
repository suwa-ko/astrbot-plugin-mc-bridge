import asyncio
import base64
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import unittest
from collections import deque
from astrbot.core.agent.tool import FunctionTool
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

spec = importlib.util.spec_from_file_location("mc_fixed", os.environ["MC_PLUGIN_FILE"], submodule_search_locations=[str(Path(os.environ["MC_PLUGIN_FILE"]).parent)])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


class Event:
    def __init__(self, text="mc跟着我", platform="aiocqhttp", private=True, admin=True):
        self.message_str, self.platform, self.private, self.admin = text, platform, private, admin
        self.extra = {}
        self.call_llm = True

    def get_platform_name(self): return self.platform
    def is_private_chat(self): return self.private
    def is_admin(self): return self.admin
    def get_extra(self, key, default=None): return self.extra.get(key, default)
    def set_extra(self, key, value): self.extra[key] = value
    def should_call_llm(self, value): self.call_llm = value
    def stop_event(self): pass
    def plain_result(self, text): return ("text", text)
    def image_result(self, path): return ("image", path)


def make_plugin():
    p = object.__new__(m.McBridgePlugin)
    p.config = {"qq_admin_only": True, "owner_name": "Player", "game_allowed_players": "Player"}
    p._closing = False
    p._manual_movement = False
    p._control_epoch = 0
    p._action_lock = asyncio.Lock()
    p._agent_lock = asyncio.Lock()
    p._agent_jobs = set()
    p._active_task = p._task_current = None
    p._task_queue = deque()
    p._task_stop = asyncio.Event()
    p._started = True
    p._task_chat = AsyncMock()
    p._short_history = SimpleNamespace(items=lambda: {}, set=lambda *args, **kwargs: None)
    p.bridge = SimpleNamespace(connected=True, session_id="session", client_info={"protocol": 2}, call=AsyncMock(return_value={}), stop=AsyncMock())
    return p


class Fixes(unittest.IsolatedAsyncioTestCase):
    async def test_all_tools_have_guard_and_preserve_signatures(self):
        source = Path(os.environ["MC_PLUGIN_FILE"]).read_text()
        self.assertEqual(source.count('@filter.llm_tool(name="mc_'), 73)
        self.assertEqual(source.count('@require_mc_tool("mc_'), 73)
        self.assertIn("x", inspect.signature(m.McBridgePlugin.mc_goto).parameters)

    async def test_non_admin_cannot_directly_move(self):
        p = make_plugin()
        result = await p.mc_goto(Event(admin=False), 1, 64, 1)
        self.assertIn("未执行", result)
        p.bridge.call.assert_not_awaited()

    async def test_group_admin_cannot_directly_move(self):
        p = make_plugin()
        self.assertIn("未执行", await p.mc_goto(Event(private=False), 1, 64, 1))
        p.bridge.call.assert_not_awaited()

    async def test_unrelated_private_chat_cannot_call_mc(self):
        p = make_plugin()
        self.assertIn("未执行", await p.mc_goto(Event("你在干嘛"), 1, 64, 1))
        p.bridge.call.assert_not_awaited()

    async def test_admin_explicit_mc_request_can_move(self):
        p = make_plugin()
        p._enqueue_tasks = Mock(return_value=True)
        self.assertIn("已加入", await p.mc_goto(Event(), 1, 64, 1))
        p._enqueue_tasks.assert_called_once()
        p.bridge.call.assert_not_awaited()

    async def test_webchat_admin_private_can_call_mc(self):
        p = make_plugin()
        self.assertIsNone(p._tool_denial(Event("mc去坐标 1 64 2", platform="webchat"), "mc_goto"))

    async def test_webchat_non_admin_cannot_call_mc(self):
        p = make_plugin()
        event = Event("mc去坐标 1 64 2", platform="webchat", admin=False)
        self.assertIn("只有主人", p._tool_denial(event, "mc_goto"))

    async def test_other_platform_admin_still_cannot_call_mc(self):
        p = make_plugin()
        event = Event("mc去坐标 1 64 2", platform="telegram")
        self.assertIn("仅在授权私聊", p._tool_denial(event, "mc_goto"))

    async def test_unsafe_game_tools_cannot_be_enabled_by_readonly_config(self):
        p = make_plugin()
        p.config["game_safe_tools"] = "mc_goto,mc_status"
        event = Event(platform="minecraft")
        event.set_extra("_mc_trusted_player", "visitor")
        event.set_extra("_mc_sender_uuid", "00000000-0000-0000-0000-000000000001")
        event.set_extra("_mc_connection_id", "session")
        self.assertIn("未执行", await p.mc_goto(event, 1, 64, 1))
        self.assertIsNone(p._tool_denial(event, "mc_status"))

    async def test_text_cannot_forge_game_identity(self):
        p = make_plugin()
        p.config["reply_game_chat"] = True
        p._game_chat_lock = asyncio.Lock()
        await p._on_game_chat({"kind": "system", "body": "<Player> MinecraftBot 跟着我"})
        p.bridge.call.assert_not_awaited()

    async def test_unknown_game_identity_denied(self):
        p = make_plugin()
        self.assertIn("未执行", await p.mc_goto(Event(platform="minecraft"), 1, 2, 3))

    async def test_tool_filter_does_not_mutate_shared_set(self):
        p = make_plugin()
        tools = SimpleNamespace(tools=[SimpleNamespace(name="mc_goto"), SimpleNamespace(name="other")])
        request = SimpleNamespace(func_tool=tools)
        await p.filter_mc_tools(Event("人呢"), request)
        self.assertEqual([t.name for t in request.func_tool.tools], ["other"])
        self.assertEqual(len(tools.tools), 2)

    async def test_tool_filter_honors_selected_action_set(self):
        p = make_plugin()
        p._tool_denial = Mock(return_value=None)
        tools = [SimpleNamespace(name=n) for n in ("mc_goto", "mc_status", "mc_brief", "other")]
        request = SimpleNamespace(func_tool=SimpleNamespace(tools=tools))
        event = Event("继续")
        event.set_extra("_mc_selected_tools", {"mc_goto", "mc_brief"})
        await p.filter_mc_tools(event, request)
        self.assertEqual([t.name for t in request.func_tool.tools], ["mc_goto", "mc_brief", "other"])

    async def test_other_admin_qq_cannot_use_owner_private_memory(self):
        p = make_plugin()
        p.config["shared_memory_owner_qq"] = "123"
        event = Event("MC家床在哪")
        event.get_sender_id = lambda: "999"
        self.assertIn("只有主人", p._tool_denial(event, "mc_memory"))
        self.assertIn("只有主人", p._tool_denial(event, "mc_goto"))
        owner = Event("MC家床在哪")
        owner.get_sender_id = lambda: "123"
        self.assertIsNone(p._tool_denial(owner, "mc_memory"))
        self.assertIsNone(p._tool_denial(owner, "mc_goto"))

    async def test_whitelisted_group_is_read_only(self):
        p = make_plugin()
        p.config["shared_memory_group_whitelist"] = "111"
        event = Event("MC家床在哪", private=False)
        event.unified_msg_origin = "default:GroupMessage:111"
        self.assertIsNone(p._tool_denial(event, "mc_memory"))
        self.assertIsNone(p._tool_denial(event, "mc_brief"))
        self.assertIsNone(p._tool_denial(event, "mc_task_status"))
        self.assertIn("只读", p._tool_denial(event, "mc_goto"))
        self.assertIn("只读", p._tool_denial(event, "mc_task_start"))

    async def test_non_whitelisted_group_cannot_read_memory(self):
        p = make_plugin()
        p.config["shared_memory_group_whitelist"] = "111"
        event = Event("MC家床在哪", private=False)
        event.unified_msg_origin = "default:GroupMessage:222"
        self.assertIn("没有开放", p._tool_denial(event, "mc_memory"))

    async def test_stopped_event_cannot_restart_movement(self):
        p = make_plugin()
        p._enqueue_tasks = Mock(return_value=True)
        event = Event()
        await p.mc_goto(event, 1, 64, 1)
        p._control_epoch += 1
        p.bridge.call.reset_mock()
        self.assertIn("中断", await p.mc_goto(event, 2, 64, 2))
        p.bridge.call.assert_not_awaited()

    async def test_old_context_cannot_mutate_game(self):
        p = make_plugin()
        token = m.CONTROL_EPOCH.set(-1)
        try:
            result = await p._call("baritone", {"command": "goto 1 2 3"})
        finally:
            m.CONTROL_EPOCH.reset(token)
        self.assertIn("中断", result["__error__"])
        p.bridge.call.assert_not_awaited()

    async def test_old_client_cannot_mutate_game(self):
        p = make_plugin()
        p.bridge.client_info = {"protocol": 1}
        self.assertIn("协议过旧", (await p._call("chat", {"text": "hi"}))["__error__"])
        p.bridge.call.assert_not_awaited()

    async def test_goto_no_path_opens_door_before_breaking(self):
        p = make_plugin()
        p._try_open_nearby_door = AsyncMock(return_value=True)
        p._move_and_wait = AsyncMock(side_effect=[
            m.BridgeError("客户端确认当前路线无路，已停止，不再等到超时"),
            None,
        ])
        await p._run_goto_task(m.QueuedTask("goto", "go", x=1, y=64, z=2))
        self.assertEqual(p._move_and_wait.await_count, 2)
        self.assertFalse(p._move_and_wait.await_args_list[1].kwargs.get("allow_break"))
        p._try_open_nearby_door.assert_awaited_once()

    async def test_try_open_nearby_door_uses_interact(self):
        p = make_plugin()
        p._call = AsyncMock(return_value={"doors": [{"x": 3, "y": 64, "z": 4, "distance": 3.0}]})
        p._approach_target = AsyncMock(return_value=None)
        p._run_operation = AsyncMock(return_value={"phase": "completed"})
        self.assertTrue(await p._try_open_nearby_door())
        p._run_operation.assert_awaited_once_with(
            "interact", {"operation": "open", "x": 3, "y": 64, "z": 4}, seconds=6
        )

    async def test_goto_no_path_never_auto_breaks(self):
        p = make_plugin()
        p._try_open_nearby_door = AsyncMock(return_value=False)
        p._goto_candidates = AsyncMock(return_value=[(1, 64, 2)])
        p._move_and_wait = AsyncMock(side_effect=m.BridgeError("客户端确认当前路线无路，已停止，不再等到超时"))
        with self.assertRaises(m.BridgeError):
            await p._run_goto_task(m.QueuedTask("goto", "go", x=1, y=64, z=2))
        self.assertEqual(p._move_and_wait.await_count, 1)
        self.assertFalse(p._move_and_wait.await_args_list[0].kwargs.get("allow_break"))
        self.assertTrue(any("不自动挖方块" in c.args[0] for c in p._task_chat.await_args_list))

    async def test_goto_no_path_tries_collision_navigation_before_door(self):
        p = make_plugin()
        p._try_open_nearby_door = AsyncMock(return_value=False)
        p._move_and_wait = AsyncMock(side_effect=m.BridgeError("客户端确认当前路线无路，已停止，不再等到超时"))
        p._try_navigate = AsyncMock(return_value=True)
        result = await p._run_goto_task(m.QueuedTask("goto", "go", x=1, y=64, z=2))
        self.assertEqual(result["goal_outcome"], "fulfilled")
        p._try_navigate.assert_awaited_once()
        p._try_open_nearby_door.assert_not_awaited()

    async def test_try_navigate_uses_non_breaking_navigate_action(self):
        p = make_plugin()
        p.bridge.client_info = {"protocol": 3}
        p._wait_arrival = AsyncMock(return_value=None)
        p.bridge.call.return_value = {}
        self.assertTrue(await p._try_navigate(1, 64, 2, 10))
        call = p.bridge.call.await_args_list[0]
        self.assertEqual(call.args[0], "navigate")
        self.assertFalse(call.args[1]["allow_break"])
        p._wait_arrival.assert_awaited_once()

    async def test_try_step_off_bed_aligns_to_nearby_ground(self):
        p = make_plugin()
        p.bridge.client_info = {"protocol": 3, "capabilities": "block_context"}
        position = {"x": -375.5, "y": 94.6, "z": -154.5}
        async def call(action, params=None, timeout=20):
            params = params or {}
            if action == "status":
                return {"position": dict(position)}
            if action == "block_context":
                cell = (params.get("x"), params.get("y"), params.get("z"))
                if cell == (-375, 94, -154):
                    return {"block": "minecraft:white_bed", "standable": True, "stands": [
                        {"x": -376, "y": 94, "z": -154, "feet_y": 94, "distance": 1.0},
                        {"x": -375, "y": 94, "z": -153, "feet_y": 94, "distance": 1.5},
                    ]}
                if cell == (-376, 94, -154):
                    return {"block": "minecraft:red_bed", "standable": True, "stands": []}
                if cell == (-375, 94, -153):
                    return {"block": "minecraft:air", "standable": True, "stands": []}
                return {"block": "minecraft:red_bed", "stands": []}
            return {}
        p.bridge.call.side_effect = call
        def align(action, params=None, seconds=0, **kwargs):
            params = params or {}
            position.update({"x": params["x"] + 0.5, "y": params["y"], "z": params["z"] + 0.5})
            return {"phase": "completed"}
        p._run_operation = AsyncMock(side_effect=align)
        self.assertTrue(await p._try_step_off_bed())
        self.assertEqual(p._run_operation.await_count, 1)
        params = p._run_operation.await_args.args[1]
        self.assertEqual((params["x"], params["y"], params["z"]), (-375, 94, -153))

    async def test_task_start_returns_inline_result_for_short_task(self):
        p = make_plugin()
        p.config["short_task_wait_seconds"] = 5
        scheduler = m.UnifiedScheduler()
        p._scheduler = scheduler
        scheduled = scheduler.register(kind="skill", goal="inline")
        scheduler.start(scheduled)
        scheduler.finish(scheduled, success=True, result={"ok": True}, goal_outcome="fulfilled")
        p._start_agent_task = AsyncMock(return_value=scheduled.id)
        payload = json.loads(await p.mc_task_start(Event("mc合成木棍"), "craft",
                                                   '{"item":"minecraft:stick","count":1}'))
        self.assertTrue(payload["inline"])
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["result"], {"ok": True})
        self.assertEqual(payload["goal_outcome"], "fulfilled")
        self.assertTrue(p._start_agent_task.await_args.kwargs.get("suppress_notify"))

    async def test_task_start_timeout_restores_async_notification(self):
        p = make_plugin()
        p.config["short_task_wait_seconds"] = 0.1
        scheduler = m.UnifiedScheduler()
        p._scheduler = scheduler
        item = m.QueuedTask("skill", "slow", target="craft", task_id="task-slow",
                            suppress_notify=True)
        scheduler.register(task_id="task-slow", kind="skill", goal="slow", payload=item)
        p._start_agent_task = AsyncMock(return_value="task-slow")
        payload = json.loads(await p.mc_task_start(Event("mc合成木棍"), "craft",
                                                   '{"item":"minecraft:stick","count":1}'))
        self.assertFalse(payload["inline"])
        self.assertIn("未在", payload["note"])
        self.assertFalse(item.suppress_notify)

    async def test_worker_suppresses_inline_completion_notification(self):
        p = make_plugin()
        p._record_task_metric = Mock()
        p._flush_scheduler_audit = Mock()
        p._task_recovery = SimpleNamespace(discard=Mock(return_value=True))
        p._task_chat = AsyncMock()

        async def run(*args):
            return {"skill": "gather", "collected": 1}

        p._skills_for = Mock(return_value=SimpleNamespace(run=run))
        task = m.QueuedTask("skill", "synthetic", target="gather", suppress_notify=True)
        p._task_queue.append(task)
        await p._task_worker()
        status = p._scheduler_for().task_status(task.task_id)
        self.assertEqual(status["state"], "completed")
        p._task_chat.assert_not_awaited()

    async def test_goto_falls_back_to_nearby_stand_and_reports_partial(self):
        p = make_plugin()
        p._try_open_nearby_door = AsyncMock(return_value=False)
        p._goto_candidates = AsyncMock(return_value=[(1, 64, 2), (2, 64, 2)])
        p._move_and_wait = AsyncMock(side_effect=[
            m.BridgeError("客户端确认当前路线无路，已停止，不再等到超时"),
            None,
        ])
        result = await p._run_goto_task(m.QueuedTask("goto", "go", x=1, y=64, z=2))
        self.assertEqual(result["goal_outcome"], "partial")
        self.assertEqual(result["arrived_near"], {"x": 2, "y": 64, "z": 2})
        self.assertEqual(p._move_and_wait.await_count, 2)
        self.assertFalse(p._move_and_wait.await_args_list[1].kwargs.get("allow_break"))

    async def test_goto_candidates_scans_below_unstandable_target(self):
        p = make_plugin()
        async def fake_call(action, params=None, timeout=10):
            y = int((params or {}).get("y", 0))
            if y == 87:
                return {"stands": [{"x": -349, "y": 86, "z": -152,
                                    "feet_y": 86.0, "distance": 8.0}]}
            return {"stands": []}
        p._call = fake_call
        candidates = await p._goto_candidates(-350, 94, -152)
        self.assertEqual(candidates[0], (-350, 94, -152))
        self.assertIn((-349, 86, -152), candidates)

    async def test_goto_explicit_break_does_not_retry_forever(self):
        p = make_plugin()
        p._try_open_nearby_door = AsyncMock(return_value=False)
        p._goto_candidates = AsyncMock(return_value=[(1, 64, 2)])
        p._move_and_wait = AsyncMock(side_effect=m.BridgeError("客户端确认当前路线无路"))
        with self.assertRaises(m.BridgeError):
            await p._run_goto_task(m.QueuedTask("goto", "go", x=1, y=64, z=2,
                                                params={"allow_break": True}))
        self.assertEqual(p._move_and_wait.await_count, 1)
        self.assertTrue(p._move_and_wait.await_args_list[0].kwargs.get("allow_break"))

    def test_classify_goal_outcome_separates_execution_from_goal(self):
        self.assertEqual(m.classify_goal_outcome({"requested": 8, "collected": 1, "partial": True}), "partial")
        self.assertEqual(m.classify_goal_outcome({"requested": 2, "moved": 2}), "fulfilled")
        self.assertEqual(m.classify_goal_outcome({"requested": 2, "moved": 1}), "partial")
        self.assertEqual(m.classify_goal_outcome({"result": "retreated"}), "partial")
        self.assertEqual(m.classify_goal_outcome(None, "goto"), "fulfilled")
        self.assertEqual(m.classify_goal_outcome({"items": [{"ok": True, "moved": 1}], "requested": 1, "moved": 1}), "fulfilled")

    def test_status_shows_navigation_break_budget(self):
        text = m._fmt_status({"navigation_protection": {
            "break_monitoring": True, "break_count": 2, "break_limit": 8}})
        self.assertIn("导航破坏预算: 2/8", text)
        text = m._fmt_status({"navigation_protection": {
            "break_budget_exhausted": True, "break_count": 1, "break_limit": 1}})
        self.assertIn("导航破坏预算: 1/1，已用完", text)

    async def test_worker_records_partial_goal_outcome(self):
        p = make_plugin()
        p._record_task_metric = Mock()
        p._flush_scheduler_audit = Mock()
        p._task_recovery = SimpleNamespace(discard=Mock(return_value=True))
        p._task_chat = AsyncMock()

        async def run(*args):
            return {"skill": "gather", "requested": 8, "collected": 1, "partial": True}

        p._skills_for = Mock(return_value=SimpleNamespace(run=run))
        task = m.QueuedTask("skill", "synthetic", target="gather")
        p._task_queue.append(task)
        await p._task_worker()
        status = p._scheduler_for().task_status(task.task_id)
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["goal_outcome"], "partial")
        p._task_recovery.discard.assert_called_once_with(task.task_id)

    async def test_queue_skill_uses_contract_budget(self):
        p = make_plugin()
        p._enqueue_tasks = Mock(return_value=True)
        p._queue_skill("采集", "gather", resource="logs", count=1)
        task = p._enqueue_tasks.call_args.args[0][0]
        self.assertGreater(task.budget_seconds, 0)
        self.assertIn("actor", task.resources)

    async def test_worker_enforces_execution_budget(self):
        p = make_plugin()
        p._task_chat = AsyncMock()

        async def run(*args):
            await asyncio.sleep(0.1)
            return {}

        p._skills_for = Mock(return_value=SimpleNamespace(run=run))
        task = m.QueuedTask("skill", "budget", target="gather", params={}, budget_seconds=0.01)
        p._task_queue.append(task)
        await p._task_worker()
        status = p._scheduler_for().task_status(task.task_id)
        self.assertEqual(status["state"], "failed")
        self.assertIn("budget", status["error"])

    async def test_higher_priority_task_preempts_lower_priority(self):
        p = make_plugin()
        scheduler = m.UnifiedScheduler()
        p._scheduler = scheduler
        low = scheduler.register(kind="skill", goal="low", priority=int(m.Priority.AUTONOMY),
                                 preemptible=True, resources=("actor",))
        scheduler.start(low)
        worker = asyncio.create_task(asyncio.Event().wait())
        p._active_task = worker
        p._send_stop = AsyncMock(return_value=None)
        p._enqueue_tasks = Mock(return_value=True)
        task_id = await p._start_agent_task(
            "gather", {"resource": "logs", "count": 1}, priority=int(m.Priority.OWNER)
        )
        self.assertTrue(task_id.startswith("task-"))
        self.assertTrue(worker.cancelled())
        self.assertEqual(low.state, m.TaskState.CANCELLED)
        p._send_stop.assert_awaited_once()
        p._enqueue_tasks.assert_called_once()

    async def test_wait_returns_last_completed_task_result(self):
        p = make_plugin()
        p._last_task_result = {"task_id": "skill-1", "status": "completed", "result": {"moved": 2}}
        p._active_task = None
        p._task_current = None
        result = json.loads(await p.mc_wait(Event("mc等待"), 0.1))
        self.assertEqual(result["last"]["task_id"], "skill-1")
        self.assertEqual(result["last"]["result"]["moved"], 2)

    async def test_busy_allows_readonly_queue_and_cancel_tools(self):
        p = make_plugin()
        p._active_task = SimpleNamespace(done=lambda: False)
        for name, text in (
            ("mc_task_start", "mc开始任务"),
            ("mc_task_cancel", "mc取消任务"),
            ("mc_brief", "mc看看状态"),
            ("mc_block_context", "mc看看这个方块"),
            ("mc_get_menu", "mc看看菜单"),
            ("mc_task_status", "mc任务状态"),
        ):
            self.assertIsNone(p._tool_denial(Event(text), name), name)
        self.assertIn("未执行", p._tool_denial(Event("mc去坐标1 64 2"), "mc_goto"))

    async def test_send_stop_confirms_survival_idle(self):
        p = make_plugin()
        p.bridge.call = AsyncMock(side_effect=[{}, {"survival_busy": False}])
        self.assertIsNone(await p._send_stop())

    async def test_send_stop_reports_unconfirmed_busy(self):
        p = make_plugin()
        p.bridge.call = AsyncMock(return_value={"survival_busy": True})
        self.assertIn("未确认", await p._send_stop())

    async def test_run_operation_cancel_sends_client_stop(self):
        p = make_plugin()
        calls = []

        async def call(action, params=None, timeout=10):
            calls.append(action)
            if action == "operation":
                return {"phase": "running"}
            if action == "baritone":
                return {}
            return {"operation_id": "op-1", "phase": "started"}

        p._call = call
        task = asyncio.create_task(p._run_operation("mine_at", {"x": 1}, seconds=10))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn("baritone", calls)

    async def test_stop_task_runtime_does_not_cancel_agent_tool_loop(self):
        p = make_plugin()
        agent_job = asyncio.create_task(asyncio.Event().wait())
        p._agent_jobs = {agent_job}
        p._cancel_active_task = Mock()
        p._send_stop = AsyncMock(return_value=None)
        self.assertIsNone(await p._stop_task_runtime())
        p._cancel_active_task.assert_called_once()
        self.assertFalse(agent_job.cancelled())
        agent_job.cancel()
        await asyncio.gather(agent_job, return_exceptions=True)

    async def test_task_cancel_active_uses_unified_stop(self):
        p = make_plugin()
        p._stop_task_runtime = AsyncMock(return_value=None)
        task = SimpleNamespace(state=m.TaskState.RUNNING)
        p._scheduler_for = Mock(return_value=SimpleNamespace(get=lambda task_id: task))
        result = json.loads(await p.mc_task_cancel(Event("mc取消任务"), "skill-1"))
        self.assertTrue(result["active"])
        self.assertTrue(result["client_stop_confirmed"])
        p._stop_task_runtime.assert_awaited_once()

    async def test_mc_stop_reports_client_confirmation(self):
        p = make_plugin()
        p._stop_task_runtime = AsyncMock(return_value=None)
        result = await p.mc_stop(Event("mcstop"))
        self.assertIn("client_stop_confirmed=true", result)

    async def test_goto_link_loss_never_reports_arrival(self):
        p = make_plugin()
        p.bridge.call.side_effect = [{}, {"__error__": "link lost"}, {}]
        with self.assertRaises(m.BridgeError):
            await p._run_goto_task(m.QueuedTask("goto", "go", x=1, y=64, z=2))
        self.assertFalse(any("到了" in c.args[0] for c in p._task_chat.await_args_list))

    async def test_come_link_loss_never_reports_arrival(self):
        p = make_plugin()
        p.bridge.call.side_effect = [{"x": 1, "y": 64, "z": 2}, {"__error__": "lost"}, {}]
        with self.assertRaises(m.BridgeError):
            await p._run_come_task(m.QueuedTask("come", "go", target="player"))
        self.assertFalse(any("到了" in c.args[0] for c in p._task_chat.await_args_list))

    async def test_timeout_never_reports_arrival(self):
        p = make_plugin()
        with patch.object(m, "time", SimpleNamespace(monotonic=Mock(side_effect=[0, 200]))):
            with self.assertRaisesRegex(m.BridgeError, "超时"):
                await p._wait_arrival(1, 2, 3, 10)

    async def test_move_and_wait_passes_remaining_navigation_break_budget(self):
        p = make_plugin()
        p.bridge.client_info = {"protocol": 3, "capabilities": "navigation_break_budget"}
        p._break_policy = SimpleNamespace(
            per_action=8, remaining_task_budget=lambda: 5, ensure_task=Mock(),
            record_navigation_breaks=Mock())
        p._wait_arrival = AsyncMock(return_value=None)
        await p._move_and_wait(1, 2, 3, 10, allow_break=True)
        nav = [call for call in p.bridge.call.await_args_list if call.args[0] == "navigate"][0]
        self.assertTrue(nav.args[1]["allow_break"])
        self.assertEqual(nav.args[1]["max_break"], 5)
        self.assertTrue(p._wait_arrival.await_args.kwargs.get("allow_break"))

    async def test_move_and_wait_rejects_exhausted_navigation_budget(self):
        p = make_plugin()
        p.bridge.client_info = {"protocol": 3}
        p._break_policy = SimpleNamespace(
            per_action=8, remaining_task_budget=lambda: 0, ensure_task=Mock(),
            record_navigation_breaks=Mock())
        with self.assertRaisesRegex(m.BridgeError, "任务破坏预算"):
            await p._move_and_wait(1, 2, 3, 10, allow_break=True)
        p.bridge.call.assert_not_awaited()

    async def test_wait_arrival_stops_on_client_break_budget_exhausted(self):
        p = make_plugin()
        policy = SimpleNamespace(record_navigation_breaks=Mock())
        p._break_policy = policy
        status = {
            "position": {"x": 0, "y": 64, "z": 0},
            "client_tps": 20, "client_paused": False, "screen": "none",
            "navigation_protection": {"break_count": 3, "break_budget_exhausted": True},
        }
        async def call(action, params=None, timeout=20):
            return status if action == "status" else {}
        p.bridge.call.side_effect = call
        with self.assertRaisesRegex(m.BridgeError, "客户端侧破坏预算"):
            await p._wait_arrival(10, 64, 0, 5, allow_break=True)
        policy.record_navigation_breaks.assert_called_once_with(3)

    async def test_wait_arrival_records_navigation_break_spend(self):
        p = make_plugin()
        policy = SimpleNamespace(record_navigation_breaks=Mock())
        p._break_policy = policy
        status = {
            "position": {"x": 10, "y": 64, "z": 0},
            "client_tps": 20, "client_paused": False, "screen": "none",
            "navigation_protection": {"break_count": 4},
        }
        async def call(action, params=None, timeout=20):
            return status if action == "status" else {}
        p.bridge.call.side_effect = call
        await p._wait_arrival(10, 64, 0, 5, allow_break=True)
        policy.record_navigation_breaks.assert_called_once_with(4)

    async def test_approach_target_passes_and_records_navigation_break_budget(self):
        p = make_plugin()
        p.bridge.client_info = {"protocol": 3}
        policy = SimpleNamespace(
            per_action=8, remaining_task_budget=lambda: 6, ensure_task=Mock(),
            record_navigation_breaks=Mock())
        p._break_policy = policy
        status = {
            "position": {"x": 0.5, "y": 64.5, "z": 0.5},
            "navigation_protection": {"break_count": 2},
        }
        async def call(action, params=None, timeout=20):
            if action == "navigate":
                return {"phase": "accepted"}
            if action == "status":
                return status
            return {}
        p.bridge.call.side_effect = call
        result = await p._approach_target(0, 64, 0, seconds=5, reach=1.5, allow_break=True)
        self.assertIsNone(result)
        nav = [call for call in p.bridge.call.await_args_list if call.args[0] == "navigate"][0]
        self.assertEqual(nav.args[1]["max_break"], 6)
        policy.record_navigation_breaks.assert_called_once_with(2)

    async def test_real_position_confirms_arrival(self):
        p = make_plugin()
        p.bridge.call.side_effect = [{}, {"position": {"x": 1, "y": 64, "z": 2}}, {}]
        await p._run_goto_task(m.QueuedTask("goto", "go", x=1, y=64, z=2))
        self.assertTrue(any("到了" in c.args[0] for c in p._task_chat.await_args_list))

    async def test_manual_flee_cancels_queue_and_can_issue_new_emergency_action(self):
        p = make_plugin()
        p._active_task = asyncio.create_task(asyncio.Event().wait())
        task = p._active_task
        p.bridge.client_info={'protocol':3}
        async def call(action,*a,**kw):
            if action=='status':return {'world_key':'test','dimension':'minecraft:overworld',"position": {"x": 0, "y": 64, "z": 0}, "hostiles": [{"distance": 2, "position": {"x": 2, "y": 64, "z": 0}}]}
            if action=='scan':return {'ground':[{'x':-6,'y':64,'z':0}]}
            return {'phase':'accepted'}
        p.bridge.call.side_effect=call;p._wait_arrival=AsyncMock()
        result = await p.mc_flee(Event("mc快跑"))
        self.assertTrue(task.cancelled())
        self.assertIn("已确认到达", result)
        self.assertEqual(p.bridge.call.await_args_list[-1].args[1], {'x':-6,'y':64,'z':0})
        p._wait_arrival.assert_awaited_once()

    async def test_failed_emergency_move_never_claims_sent(self):
        p = make_plugin()
        p.bridge.client_info={'protocol':3}
        p.bridge.call.return_value = {"__error__": "lost"}
        result = await p._auto_flee({'world_key':'test','dimension':'minecraft:overworld',"position": {"x": 0, "y": 64, "z": 0}},
                                    {"position": {"x": 1, "y": 64, "z": 0}})
        self.assertIn("未能确认安全撤离落脚点", result)

    async def test_terminate_cancels_tasks_then_sends_stop(self):
        p = make_plugin()
        p._active_task = asyncio.create_task(asyncio.Event().wait())
        job = p._active_task
        await p.terminate()
        self.assertTrue(job.cancelled())
        self.assertEqual(p.bridge.call.await_args_list[0].args, ("baritone", {"command": "stop"}))
        self.assertEqual(p.bridge.call.await_args_list[0].kwargs, {"timeout": 5})
        p.bridge.stop.assert_awaited_once()

    async def test_disconnected_stop_does_not_claim_success(self):
        p = make_plugin()
        p.bridge.connected = False
        result = await p.mc_stop(Event("mcstop"))
        self.assertIn("未确认", result)

    async def test_chat_cannot_be_used_to_run_commands(self):
        p = make_plugin()
        for text in ["/op user", "#goto 1 2 3", "＃stop", "／help"]:
            self.assertIn("只允许普通聊天", await p.mc_chat(Event(), text))
        p.bridge.call.assert_not_awaited()

    async def test_oversized_screenshot_rejected_before_decode(self):
        p = make_plugin()
        p.config["max_screenshot_mb"] = 1
        p.bridge.call.return_value = {"base64": "A" * 1500000}
        with self.assertRaisesRegex(m.BridgeError, "大小限制"):
            await p._capture()

    async def test_vision_cannot_bypass_screenshot_limit(self):
        p = make_plugin()
        p.config.update(max_screenshot_mb=1, vision_provider_id="synthetic")
        p.context = SimpleNamespace(llm_generate=AsyncMock())
        p.bridge.call.return_value = {"base64": "A" * 1500000}
        self.assertIn("看图未完成", await p.mc_see(Event("mc看看前面")))
        p.context.llm_generate.assert_not_awaited()

    async def test_screenshots_are_limited(self):
        p = make_plugin()
        p.config["screenshot_keep_count"] = 2
        p.bridge.call.return_value = {"base64": base64.b64encode(b"synthetic-image").decode()}
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(m.StarTools, "get_data_dir", return_value=folder):
                for _ in range(4): await p._capture()
            self.assertEqual(len(list((Path(folder) / "screenshots").glob("*.png"))), 2)

    async def test_untrusted_network_debug_route_removed(self):
        bridge = m.BridgeServer("127.0.0.1", 0, "/ws", "synthetic")
        await bridge.start()
        try:
            paths = {resource.canonical for resource in bridge.app.router.resources()}
            self.assertNotIn("/debug/gamechat", paths)
        finally:
            await bridge.stop()

    async def test_no_space_and_removed_legacy_commands(self):
        for text in ["mc跟着我", "mc 去坐标 1 64 2"]:
            self.assertEqual(m.McBridgePlugin._parse_input(text)[0], "agent")
        for text in ["人呢", "/mcbridge status", "mcgoto 1 2 3"]:
            self.assertIsNone(m.McBridgePlugin._parse_input(text))

    async def test_connection_change_cancels_old_work(self):
        p = make_plugin()
        job = asyncio.create_task(asyncio.Event().wait())
        p._agent_jobs.add(job)
        p._task_queue.append(m.QueuedTask("goto", "old"))
        await p._connection_lost()
        self.assertTrue(job.cancelled())
        self.assertEqual(p._control_epoch, 1)
        self.assertFalse(p._task_queue)

    async def test_old_game_connection_cannot_call_tools(self):
        p = make_plugin()
        event = Event(platform="minecraft")
        event.set_extra("_mc_trusted_player", "Player")
        event.set_extra("_mc_sender_uuid", "00000000-0000-0000-0000-000000000001")
        event.set_extra("_mc_connection_id", "previous")
        self.assertIn("旧请求", await p.mc_goto(event, 1, 2, 3))
        p.bridge.call.assert_not_awaited()

    async def test_queue_limit_does_not_silently_drop_old_tasks(self):
        p = make_plugin()
        p._active_task = SimpleNamespace(done=lambda: False)
        tasks = [m.QueuedTask("goto", str(i)) for i in range(8)]
        self.assertTrue(p._enqueue_tasks(tasks))
        self.assertFalse(p._enqueue_tasks([m.QueuedTask("goto", "extra")]))
        self.assertEqual([t.goal for t in p._task_queue], [str(i) for i in range(8)])

    async def test_failed_task_stops_remaining_queue(self):
        p = make_plugin()
        p._task_queue.extend([m.QueuedTask("goto", "first"), m.QueuedTask("goto", "second")])
        p._run_goto_task = AsyncMock(side_effect=m.BridgeError("unreachable"))
        await p._task_worker()
        self.assertEqual(p._run_goto_task.await_count, 1)
        self.assertFalse(p._task_queue)

    async def test_agent_jobs_are_cancelled_on_unload(self):
        p = make_plugin()
        p._mc_toolset = Mock(side_effect=lambda: m.ToolSet([FunctionTool(name=n,description=n,parameters={'type':'object','properties':{}}) for n in ('mc_status','mc_stop')]))
        p._persona_prompt = Mock(return_value="persona")
        started = asyncio.Event()
        async def run_agent(**_):
            started.set()
            await asyncio.Event().wait()
        p.context = SimpleNamespace(tool_loop_agent=run_agent)
        task = asyncio.create_task(p._agent_reply(Event(), "look"))
        await asyncio.wait_for(started.wait(),2)
        await p.terminate()
        self.assertTrue(task.cancelled())
        self.assertFalse(p._agent_jobs)

    async def test_stop_from_own_agent_does_not_cancel_its_parent(self):
        p = make_plugin()
        p._send_stop = AsyncMock(return_value=None)
        p._mc_toolset = Mock(side_effect=lambda: m.ToolSet([FunctionTool(name=n,description=n,parameters={'type':'object','properties':{}}) for n in ('mc_status','mc_stop')]))
        p._persona_prompt = Mock(return_value="persona")
        event = Event("mc停止")
        async def agent(**_):
            result = await p.mc_stop(event)
            return SimpleNamespace(completion_text=result)
        p.context = SimpleNamespace(tool_loop_agent=agent)
        result = await asyncio.wait_for(p._agent_reply(event, "停止"), 1)
        self.assertIn("停止", result)
        self.assertFalse(p._agent_jobs)

    async def test_chat_jobs_are_bounded_and_cancelled(self):
        async def wait_chat(_): await asyncio.Event().wait()
        bridge = m.BridgeServer("127.0.0.1", 0, "/ws", "synthetic", on_chat=wait_chat)
        import json
        for _ in range(10):
            await bridge._on_message(json.dumps({"type": "event", "event": "chat", "data": {"body": "test"}}))
        self.assertEqual(len(bridge._chat_jobs), 4)
        jobs = list(bridge._chat_jobs)
        await bridge.stop()
        self.assertTrue(all(job.cancelled() for job in jobs))

    async def test_reconnect_does_not_cancel_new_request(self):
        bridge = m.BridgeServer("127.0.0.1", 0, "/ws", "synthetic")
        await bridge.start()
        port = bridge.site._server.sockets[0].getsockname()[1]
        try:
            async with m.aiohttp.ClientSession() as session:
                first = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers={"Authorization": "Bearer synthetic"})
                previous = bridge.session_id
                close_first = asyncio.create_task(first.receive())
                second = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers={"Authorization": "Bearer synthetic"})
                await close_first
                for _ in range(50):
                    if bridge.connected and bridge.session_id != previous: break
                    await asyncio.sleep(0.01)
                task = asyncio.create_task(bridge.call("status", timeout=2))
                action = await second.receive_json(timeout=2)
                await second.send_json({"type": "result", "id": action["id"], "ok": True, "data": {"health": 20}})
                self.assertEqual(await task, {"health": 20})
                await second.close()
        finally:
            await bridge.stop()


if __name__ == "__main__":
    unittest.main()
