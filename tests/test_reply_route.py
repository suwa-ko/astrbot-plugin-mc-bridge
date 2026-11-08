import asyncio
import importlib
import json
import tempfile
import types
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from test_base import make_plugin,Event,m
from test_chat_context import game_plugin,payload

routes=importlib.import_module(m.__name__+'.reply_route')
obs=importlib.import_module(m.__name__+'.telemetry')
QQ=routes.ReplyRoute('qq','default:FriendMessage:12345')
OTHER_QQ=routes.ReplyRoute('qq','default:FriendMessage:67890')
MC=routes.ReplyRoute('minecraft',connection_id='session')

class QQEvent(Event):
    def __init__(self,text='mc跟着我',sender='12345',private=True):
        super().__init__(text,private=private)
        self.sender=sender;self.unified_msg_origin=f'default:{"FriendMessage" if private else "GroupMessage"}:{sender}'
    def get_sender_id(self):return self.sender

def plugin():
    p=make_plugin();p._task_chat=types.MethodType(m.McBridgePlugin._task_chat,p)
    p._shared_memory=None;p.context=SimpleNamespace(send_message=AsyncMock(return_value=True))
    p._send_stop=AsyncMock(return_value=None)
    return p

def game_texts(p):return [c.args[1]['text'] for c in p.bridge.call.await_args_list if c.args[0]=='chat']
def qq_texts(p):return [(c.args[0],c.args[1].get_plain_text()) for c in p.context.send_message.await_args_list]

class RouteValidation(unittest.TestCase):
    def test_qq_route_uses_exact_private_sender_session(self):
        self.assertEqual(routes.event_route(QQEvent(),SimpleNamespace()),QQ)
        event=QQEvent();event.unified_msg_origin='default:FriendMessage:67890'
        self.assertIsNone(routes.event_route(event,SimpleNamespace()))
    def test_group_or_unknown_platform_has_no_destination(self):
        self.assertIsNone(routes.event_route(QQEvent(private=False),SimpleNamespace()))
        event=QQEvent();event.platform='other'
        self.assertIsNone(routes.event_route(event,SimpleNamespace()))
    def test_mc_route_is_connection_bound_and_requires_trusted_identity(self):
        event=Event(platform='minecraft');bridge=SimpleNamespace(session_id='session')
        self.assertIsNone(routes.event_route(event,bridge))
        event.set_extra('_mc_connection_id','session');event.set_extra('_mc_trusted_player','Player');event.set_extra('_mc_sender_uuid','00000000-0000-0000-0000-000000000001')
        self.assertEqual(routes.event_route(event,bridge),MC)
        bridge.session_id='new';self.assertIsNone(routes.event_route(event,bridge))

class NoticeDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_qq_notification_never_calls_mc_chat(self):
        p=plugin();self.assertTrue(await p._task_chat('任务完成：一把木镐',route=QQ))
        self.assertEqual(qq_texts(p),[(QQ.target,'任务完成：一把木镐')]);self.assertEqual(game_texts(p),[])
    async def test_game_notification_never_calls_qq(self):
        p=plugin();self.assertTrue(await p._task_chat('任务完成：原木新增1',route=MC))
        self.assertEqual(game_texts(p),['任务完成：原木新增1']);p.context.send_message.assert_not_awaited()
    async def test_qq_failed_delivery_has_no_other_platform_fallback(self):
        p=plugin();p.context.send_message.side_effect=RuntimeError('unavailable')
        self.assertFalse(await p._task_chat('任务完成',route=QQ));self.assertEqual(game_texts(p),[])
    async def test_qq_false_or_missing_ack_is_not_claimed_delivered(self):
        for ack in (False,None):
            p=plugin();p.context.send_message.return_value=ack
            self.assertFalse(await p._task_chat('任务完成',route=QQ));self.assertEqual(game_texts(p),[])
    async def test_game_failure_has_no_qq_fallback(self):
        p=plugin();p.bridge.call.side_effect=RuntimeError('offline')
        self.assertFalse(await p._task_chat('任务失败',route=MC));p.context.send_message.assert_not_awaited()
    async def test_old_mc_connection_never_posts_to_new_connection(self):
        p=plugin();p.bridge.session_id='new'
        self.assertFalse(await p._task_chat('旧任务完成',route=MC));p.bridge.call.assert_not_awaited();p.context.send_message.assert_not_awaited()
    async def test_missing_origin_is_silent_without_guessing_last_platform(self):
        p=plugin();self.assertFalse(await p._task_chat('任务完成'))
        p.bridge.call.assert_not_awaited();p.context.send_message.assert_not_awaited()
    async def test_old_action_epoch_cannot_emit_false_completion(self):
        p=plugin();token=m.CONTROL_EPOCH.set(-1)
        try:self.assertFalse(await p._task_chat('任务完成',route=QQ))
        finally:m.CONTROL_EPOCH.reset(token)
        p.context.send_message.assert_not_awaited()
    async def test_notice_observation_does_not_store_message_or_create_model_usage(self):
        p=plugin();native=SimpleNamespace(insert_provider_stat=AsyncMock())
        with tempfile.TemporaryDirectory() as folder:
            store=obs.Telemetry(SimpleNamespace(get_db=lambda:native),folder);await store.start();p._telemetry=store
            try:
                await p._task_chat('任务完成：只给原会话看的结果ABC',route=QQ)
                row=await (await store.db.execute('SELECT data FROM turns')).fetchone();data=json.loads(row[0])
                self.assertEqual(data['source'],'notice');self.assertEqual(data['context']['channel'],'qq');self.assertEqual(data['status'],'delivered')
                self.assertNotIn('只给原会话看的结果ABC',row[0]);self.assertEqual(data['calls'],[])
                await store.flush_native();native.insert_provider_stat.assert_not_awaited()
            finally:await store.close()

class QueueOrigins(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_qq_game_and_second_qq_tasks_keep_individual_origins(self):
        p=plugin();p._active_task=SimpleNamespace(done=lambda:False)
        async def run(item):
            await p._task_chat('到了'+item.goal)
            await asyncio.sleep(0)
        p._run_goto_task=run
        tasks=[m.QueuedTask('goto','QQ任务',reply_route=QQ),m.QueuedTask('goto','MC任务',reply_route=MC),m.QueuedTask('goto','另一QQ任务',reply_route=OTHER_QQ)]
        p._enqueue_tasks(tasks);p._active_task=None
        token=routes.REPLY_ROUTE.set(MC)
        try:await p._task_worker()
        finally:routes.REPLY_ROUTE.reset(token)
        self.assertEqual(qq_texts(p),[(QQ.target,'到了QQ任务'),(OTHER_QQ.target,'到了另一QQ任务')]);self.assertEqual(game_texts(p),['到了MC任务'])
        self.assertTrue(all(t.reply_route is not None for t in tasks))
    async def test_error_detail_only_returns_to_failed_task_origin(self):
        p=plugin();p._task_queue=deque([m.QueuedTask('goto','private',reply_route=QQ),m.QueuedTask('goto','game',reply_route=MC)])
        p._run_goto_task=AsyncMock(side_effect=RuntimeError('PRIVATE-DETAIL-123'))
        await p._task_worker()
        self.assertIn('PRIVATE-DETAIL-123',qq_texts(p)[0][1]);self.assertNotIn('PRIVATE-DETAIL-123',''.join(game_texts(p)))
        self.assertEqual(len(game_texts(p)),1);self.assertFalse(p._task_queue)
    async def test_unknown_task_origin_does_not_inherit_previous_task_channel(self):
        p=plugin();p._task_queue=deque([m.QueuedTask('goto','QQ',reply_route=QQ),m.QueuedTask('goto','unknown')])
        async def run(item):await p._task_chat('到了'+item.goal)
        p._run_goto_task=run;await p._task_worker()
        self.assertEqual(len(qq_texts(p)),1);self.assertEqual(game_texts(p),[])
    async def test_guarded_qq_tool_copies_origin_onto_queued_task(self):
        p=plugin()
        await p.mc_goto(QQEvent(),1,64,2)
        self.assertEqual(p._task_queue[0].reply_route,QQ);self.assertIsNone(routes.REPLY_ROUTE.get())
        task=p._active_task;p._cancel_active_task()
        await asyncio.gather(task,return_exceptions=True)
    async def test_qq_prefix_parser_copies_origin_without_contextvar_across_yield(self):
        p=plugin();p._active_task=SimpleNamespace(done=lambda:False)
        results=[r async for r in p.mc_short_commands(QQEvent('mc去坐标1 64 2'))]
        self.assertTrue(results);self.assertEqual(p._task_queue[0].reply_route,QQ);self.assertIsNone(routes.REPLY_ROUTE.get())
    async def test_game_parser_queues_game_origin_without_qq_delivery(self):
        p=game_plugin();p._enqueue_tasks=Mock(return_value=True)
        await p._on_game_chat(payload('去坐标1 64 2'))
        self.assertEqual(p._enqueue_tasks.call_args.args[0][0].reply_route,MC)

class StopAndSurvival(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_stop_ack_uses_requester_but_other_task_cancel_uses_its_origin(self):
        p=plugin();p._task_current=m.QueuedTask('goto','QQ任务',reply_route=QQ)
        await p._interrupt_actions('已停止',requester_route=MC)
        self.assertEqual(game_texts(p),['已停止']);self.assertEqual(len(qq_texts(p)),1);self.assertIn('取消',qq_texts(p)[0][1])
    async def test_qq_stop_does_not_emit_second_qq_ack_via_background_notice(self):
        p=plugin();p._task_current=m.QueuedTask('goto','QQ任务',reply_route=QQ)
        await p._interrupt_actions(requester_route=QQ)
        self.assertEqual(qq_texts(p),[]);self.assertEqual(game_texts(p),[])
    async def test_disconnect_alert_keeps_qq_origin_and_cannot_leak_game_alert_to_new_session(self):
        p=plugin();p._task_current=m.QueuedTask('goto','QQ任务',reply_route=QQ);p._task_queue.append(m.QueuedTask('goto','MC任务',reply_route=MC));p.bridge.session_id='new'
        await p._connection_lost()
        self.assertEqual(len(qq_texts(p)),1);self.assertEqual(game_texts(p),[])
    async def test_follow_owner_is_not_a_global_last_chat_destination(self):
        p=plugin();await p.mc_follow(QQEvent(),'Player')
        self.assertEqual(p._current_notice_route(),QQ)
        await p._interrupt_actions(requester_route=QQ)
        self.assertEqual(p._current_notice_route(),MC)
