import importlib.util
import os
import sys
import unittest
import asyncio
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

source = Path(os.environ["MC_PLUGIN_FILE"])
spec = importlib.util.spec_from_file_location("mc_hooks", source, submodule_search_locations=[str(source.parent)])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


class Hooks(unittest.IsolatedAsyncioTestCase):
    async def test_mc_memory_is_temporary_and_does_not_change_original_prompt(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin.bridge = object()
        plugin._shared_memory = SimpleNamespace(qq_owner=lambda _: True,
            prepare_qq=lambda _: None, context_for=AsyncMock(return_value=m.MARKER + "\nMC记录"))
        request = SimpleNamespace(prompt="昨天MC做了什么", extra_user_content_parts=[])
        event = SimpleNamespace(message_str="昨天MC做了什么")
        await plugin.inject_shared_mc_memory(event, request)
        await plugin.inject_shared_mc_memory(event, request)
        self.assertEqual(request.prompt, "昨天MC做了什么")
        self.assertEqual(len(request.extra_user_content_parts), 1)
        self.assertIsInstance(request.extra_user_content_parts[0], m.TextPart)

    async def test_other_private_or_group_does_not_invoke_retrieval(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin._shared_memory = SimpleNamespace(qq_owner=lambda _: False, context_for=AsyncMock())
        await plugin.inject_shared_mc_memory(object(), object())
        plugin._shared_memory.context_for.assert_not_awaited()

    async def test_owner_qq_is_removed_from_prompt_and_owner_is_labelled(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin.config = {"shared_memory_owner_qq": "123456789"}
        event = SimpleNamespace(get_sender_id=lambda: "123456789")
        request = SimpleNamespace(
            system_prompt="规则\n主人唯一为 QQ 123456789。",
            contexts=[{"role": "user", "content": "[可引用列表]\n1. 123456789：你好"}],
            extra_user_content_parts=[m.TextPart(text="<system_reminder>User ID: 123456789, Nickname: 123456789</system_reminder>")],
        )
        await plugin.sanitize_prompt_identity(event, request)
        self.assertNotIn("123456789", request.system_prompt)
        self.assertIn("[应用层身份]", request.system_prompt)
        self.assertNotIn("123456789", str(request.contexts))
        self.assertIn("主人", str(request.contexts))
        self.assertNotIn("123456789", request.extra_user_content_parts[0].text)
        self.assertIn("主人", request.extra_user_content_parts[0].text)

    async def test_owner_qq_in_quotes_is_hidden_from_other_users(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin.config = {"shared_memory_owner_qq": "123456789"}
        event = SimpleNamespace(get_sender_id=lambda: "999")
        request = SimpleNamespace(
            system_prompt="",
            contexts=[{"role": "user", "content": "1. 123456789：你好"}],
            extra_user_content_parts=[],
        )
        await plugin.sanitize_prompt_identity(event, request)
        self.assertNotIn("123456789", str(request.contexts))
        self.assertNotIn("主人", str(request.contexts))
        self.assertIn("用户", str(request.contexts))
        self.assertNotIn("[应用层身份]", request.system_prompt)

    async def test_identity_sanitizer_is_noop_without_owner_config(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin.config = {}
        event = SimpleNamespace(get_sender_id=lambda: "123456789")
        request = SimpleNamespace(system_prompt="用户 123456789", contexts=[], extra_user_content_parts=[])
        await plugin.sanitize_prompt_identity(event, request)
        self.assertEqual(request.system_prompt, "用户 123456789")

    async def test_identity_sanitizer_does_not_touch_longer_numbers(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin.config = {"shared_memory_owner_qq": "123456789"}
        event = SimpleNamespace(get_sender_id=lambda: "999")
        request = SimpleNamespace(
            system_prompt="",
            contexts=[{"role": "user", "content": "订单 1234567890；账号 123456789"}],
            extra_user_content_parts=[],
        )
        await plugin.sanitize_prompt_identity(event, request)
        text = str(request.contexts)
        self.assertIn("1234567890", text)
        self.assertNotIn("账号 123456789", text)
        self.assertIn("账号 用户", text)

    async def test_reminiscing_about_logging_is_not_a_new_logging_order(self):
        for text in ["还记得上次砍树吗", "昨天MC砍了多少原木", "mc回忆昨天的任务"]:
            self.assertTrue(m.McBridgePlugin._is_memory_query(text))
        for text in ["MinecraftBot砍64个原木", "mc去坐标1 64 2然后砍树"]:
            self.assertFalse(m.McBridgePlugin._is_memory_query(text))

    async def test_recall_question_cannot_execute_goto(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin._closing = False
        plugin._control_epoch = 0
        event = SimpleNamespace(message_str="mc还记得上次去坐标1 64 2吗")
        result = plugin._tool_denial(event, "mc_goto")
        self.assertIn("不执行", result)

    async def test_private_recall_during_task_does_not_trigger_stop(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin.config = {"qq_admin_only": True}
        plugin.bridge = SimpleNamespace(connected=True)
        plugin._active_task = SimpleNamespace(done=lambda: False)
        plugin._cancel_active_task = Mock()
        plugin._agent_reply = AsyncMock(return_value="记得昨天的MC经历")
        event = SimpleNamespace(message_str="mc你还记得昨天停止了吗", is_private_chat=lambda: True,
            is_admin=lambda: True, should_call_llm=lambda _: None, stop_event=lambda: None,
            plain_result=lambda text: text)
        results = [result async for result in plugin.mc_short_commands(event)]
        self.assertEqual(results, ["记得昨天的MC经历"])
        plugin._cancel_active_task.assert_not_called()
        plugin._agent_reply.assert_awaited_once_with(event, "你还记得昨天停止了吗", allow_actions=False)

    async def test_private_conversational_then_falls_through_to_agent(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin.config = {"qq_admin_only": True}
        plugin.bridge = SimpleNamespace(connected=True)
        plugin._active_task = None
        plugin._agent_reply = AsyncMock(return_value="门打开了")
        event = SimpleNamespace(message_str="mc打开门，然后走到门外", is_private_chat=lambda: True,
            is_admin=lambda: True, should_call_llm=lambda _: None, stop_event=lambda: None,
            unified_msg_origin="default:FriendMessage:123456789",
            set_extra=lambda key, value: None,
            plain_result=lambda text: text)
        plugin._mc_histories = {}
        results = [result async for result in plugin.mc_short_commands(event)]
        self.assertEqual(results, ["门打开了"])
        plugin._agent_reply.assert_awaited_once()

    async def test_private_mixed_compound_hands_whole_sentence_to_agent(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin.config = {"qq_admin_only": True}
        plugin.bridge = SimpleNamespace(connected=True)
        plugin._active_task = None
        plugin._agent_reply = AsyncMock(return_value="好的")
        plugin._enqueue_routed = Mock(return_value=True)
        event = SimpleNamespace(message_str="mc先去 -370 84 -145 然后合成铁镐", is_private_chat=lambda: True,
            is_admin=lambda: True, should_call_llm=lambda _: None, stop_event=lambda: None,
            unified_msg_origin="default:FriendMessage:123456789",
            set_extra=lambda key, value: None,
            plain_result=lambda text: text)
        plugin._mc_histories = {}
        results = [result async for result in plugin.mc_short_commands(event)]
        self.assertEqual(results, ["好的"])
        plugin._agent_reply.assert_awaited_once()
        plugin._enqueue_routed.assert_not_called()

    async def test_private_mixed_compound_while_busy_does_not_drop_steps(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin.config = {"qq_admin_only": True}
        plugin.bridge = SimpleNamespace(connected=True)
        plugin._active_task = SimpleNamespace(done=lambda: False)
        plugin._agent_reply = AsyncMock()
        plugin._enqueue_routed = Mock(return_value=True)
        event = SimpleNamespace(message_str="mc先去 -370 84 -145 然后合成铁镐", is_private_chat=lambda: True,
            is_admin=lambda: True, should_call_llm=lambda _: None, stop_event=lambda: None,
            unified_msg_origin="default:FriendMessage:123456789",
            set_extra=lambda key, value: None,
            plain_result=lambda text: text)
        results = [result async for result in plugin.mc_short_commands(event)]
        self.assertIn("复合指令没有全部解析", results[0])
        plugin._enqueue_routed.assert_not_called()
        plugin._agent_reply.assert_not_awaited()

    async def test_private_control_request_while_busy_reaches_agent(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin.config = {"qq_admin_only": True}
        plugin.bridge = SimpleNamespace(connected=True)
        plugin._active_task = SimpleNamespace(done=lambda: False)
        plugin._agent_reply = AsyncMock(return_value="已取消")
        event = SimpleNamespace(message_str="mc用 mc_task_cancel 终止 task-1 并看看 state",
            is_private_chat=lambda: True, is_admin=lambda: True,
            should_call_llm=lambda _: None, stop_event=lambda: None,
            unified_msg_origin="default:FriendMessage:123456789",
            set_extra=lambda key, value: None,
            plain_result=lambda text: text)
        results = [result async for result in plugin.mc_short_commands(event)]
        self.assertEqual(results, ["已取消"])
        plugin._agent_reply.assert_awaited_once()

    async def test_game_owner_can_recall_while_working_without_new_action(self):
        plugin = object.__new__(m.McBridgePlugin)
        plugin.config = {"reply_game_chat": True, "owner_name": "Player"}
        plugin._closing = False
        plugin._shared_memory = None
        plugin._active_task = SimpleNamespace(done=lambda: False)
        plugin._game_chat_lock = asyncio.Lock()
        plugin._game_chat_last = 0
        plugin._game_chat_history = deque()
        plugin._last_game_reply = ""
        plugin._cancel_active_task = Mock()
        plugin._agent_reply = AsyncMock(return_value="记得呀，昨天停下来回家了")
        plugin.bridge = SimpleNamespace(session_id="current", call=AsyncMock(return_value={}))
        await plugin._on_game_chat({"_connection_id": "current", "kind": "player", "sender_name": "Player",
            "sender_uuid": "00000000-0000-0000-0000-000000000001", "body": "MinecraftBot还记得昨天停下吗"})
        plugin._agent_reply.assert_awaited_once()
        self.assertFalse(plugin._agent_reply.await_args.kwargs["allow_actions"])
        plugin._cancel_active_task.assert_not_called()
        self.assertEqual(plugin.bridge.call.await_args.args[0], "chat")
