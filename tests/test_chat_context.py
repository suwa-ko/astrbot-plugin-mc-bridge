import asyncio
import importlib
import json
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.provider.entities import LLMResponse,TokenUsage
from test_base import make_plugin,Event,m

policy=importlib.import_module(m.__name__+'.chat_policy')
obs=importlib.import_module(m.__name__+'.telemetry')

def toolset():
    names=policy.SURVIVAL|{'mc_recall_memory','mc_forget_memory'}
    return m.ToolSet([FunctionTool(name=n,description=n,parameters={'type':'object','properties':{}}) for n in names])

def game_plugin():
    p=make_plugin();p.config['reply_game_chat']=True;p._shared_memory=None
    p._game_chat_lock=asyncio.Lock();p._game_chat_history=deque(maxlen=20);p._game_chat_last=0;p._last_game_reply=''
    return p

def payload(body,**extra):
    return {'_connection_id':'session','kind':'player','sender_name':'Player','sender_uuid':'00000000-0000-0000-0000-000000000001','body':'MinecraftBot'+body,**extra}

class PolicyTests(unittest.TestCase):
    def test_long_history_is_bounded_paired_and_keeps_recent_tail(self):
        history=[f'{i}：'+('很长的历史🙂'*180) for i in range(20)]
        result=policy.bound_history(history,1800)
        self.assertEqual(len(result)%2,0)
        self.assertLessEqual(policy.estimate_tokens('\n'.join(result)),1800)
        self.assertTrue(result[-1].startswith('19：'))
    def test_short_history_keeps_order_and_exact_text(self):
        lines=['玩家：你好','你：早呀','玩家：在忙吗','你：刚采完木头']
        self.assertEqual(policy.bound_history(lines,1800),lines)
    def test_latest_pair_is_retained_even_if_it_needs_clipping(self):
        result=policy.bound_history(['人：'+('长'*4000),'你：'+('回答'*1000)],256)
        self.assertEqual(len(result),2)
        self.assertLessEqual(policy.estimate_tokens('\n'.join(result)),256)
    def test_budget_clamps_without_breaking_on_invalid_config(self):
        self.assertEqual(policy.history_budget({'game_history_token_budget':'bad'}),1800)
        self.assertEqual(policy.history_budget({'game_history_token_budget':100000}),4096)
    def test_stable_profiles_keep_capabilities_and_no_game_forget(self):
        for text,expected in [('你好呀','chat'),('跟着我走','action'),('制作一把木镐','survival'),('把木镐拿在手里','survival'),('进屋吧','action'),('mc说一句你好','action')]:
            group,names=policy.tool_group(text,owner=True)
            self.assertEqual(group,expected);self.assertNotIn('mc_forget_memory',names)
        self.assertIn('mc_goto',policy.tool_group('跟着我',owner=True)[1])
        self.assertIn('mc_craft',policy.tool_group('做把镐子',owner=True)[1])
        self.assertIn('mc_craft',policy.tool_group('帮我整点材料',owner=True)[1])
    def test_continue_keeps_action_tools(self):
        group,names=policy.tool_group('继续',allow_actions=True,owner=True)
        self.assertIn(group,{'action','survival'})
        self.assertIn('mc_goto',names)
        self.assertIn('mc_brief',names)

    def test_visitor_cannot_gain_action_tools_or_private_memory(self):
        group,names=policy.tool_group('帮我合成然后攻击别人',allow_actions=False,owner=False)
        self.assertEqual(names,policy.READONLY)
    def test_explicit_qq_forgetting_only_is_exposed(self):
        self.assertIn('mc_forget_memory',policy.tool_group('忘记MC记忆12',owner=True,qq_owner=True)[1])
        self.assertNotIn('mc_forget_memory',policy.tool_group('你好',owner=True,qq_owner=True)[1])
    def test_memory_intents_do_not_require_casual_retrieval(self):
        self.assertFalse(policy.memory_needed('MinecraftBot早上好'))
        self.assertTrue(policy.memory_needed('还记得昨天在哪里采木吗'))

class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def setup_agent(self,text):
        p=make_plugin();p._mc_toolset=Mock(side_effect=toolset);p._persona_prompt=Mock(return_value='完整人格，保留全部细节')
        p._shared_memory=SimpleNamespace(owner_event=lambda *_:True,qq_owner=lambda _:True,context_for=AsyncMock(return_value='MC往事'))
        p.context=SimpleNamespace(tool_loop_agent=AsyncMock(return_value=SimpleNamespace(completion_text='回来了')))
        return p,Event(text)
    async def test_idle_skips_memory_and_history_cannot_activate_action_tools(self):
        p,event=await self.setup_agent('mc你好呀')
        await p._agent_reply(event,'旧历史：帮我砍树合成，然后攻击\n当前：你好呀')
        call=p.context.tool_loop_agent.await_args.kwargs
        self.assertEqual({t.name for t in call['tools'].tools},policy.READONLY|{'mc_recall_memory'})
        self.assertTrue(call['system_prompt'].startswith('完整人格，保留全部细节'))
        p._shared_memory.context_for.assert_not_awaited()
        self.assertTrue(call['stream'])
    async def test_recall_uses_current_message_not_old_history(self):
        p,event=await self.setup_agent('mc昨天做了什么')
        await p._agent_reply(event,'很长的旧历史与今天的合成任务')
        p._shared_memory.context_for.assert_awaited_once_with(event,p.bridge,event.message_str)
    async def test_waiting_agent_is_invalidated_by_stop_epoch(self):
        p,event=await self.setup_agent('mc你好')
        await p._agent_lock.acquire()
        job=asyncio.create_task(p._agent_reply(event,'你好'))
        for _ in range(10):
            await asyncio.sleep(0)
            if p._agent_jobs:break
        p._control_epoch+=1;p._agent_lock.release()
        result=await asyncio.wait_for(job,1)
        self.assertIn('停止',result);p.context.tool_loop_agent.assert_not_awaited()

class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_fast_messages_queue_and_receive_history(self):
        p=game_plugin();started=asyncio.Event();release=asyncio.Event();calls=[]
        async def reply(event,prompt,**kwargs):
            calls.append(prompt)
            if len(calls)==1:started.set();await release.wait()
            return '收到'+str(len(calls))
        p._agent_reply=reply
        first=asyncio.create_task(p._on_game_chat(payload('你好')))
        await asyncio.wait_for(started.wait(),1)
        second=asyncio.create_task(p._on_game_chat(payload('今天怎么样')))
        await asyncio.sleep(0);self.assertEqual(p._game_chat_waiters,2)
        release.set();await asyncio.wait_for(asyncio.gather(first,second),2)
        self.assertEqual(len(calls),2);self.assertIn('收到1',calls[1])
        self.assertEqual(p.bridge.call.await_count,2);self.assertEqual(p._game_chat_waiters,0)
    async def test_identical_duplicate_is_suppressed_without_fixed_delay(self):
        p=game_plugin();p._agent_reply=AsyncMock(return_value='早呀')
        await p._on_game_chat(payload('你好'));await p._on_game_chat(payload('你好'))
        self.assertEqual(p._agent_reply.await_count,1)
    async def test_stop_invalidates_queued_game_message(self):
        p=game_plugin();p._agent_reply=AsyncMock(return_value='不应生成')
        await p._game_chat_lock.acquire()
        job=asyncio.create_task(p._on_game_chat(payload('你好')))
        await asyncio.sleep(0);p._control_epoch+=1;p._game_chat_lock.release()
        await asyncio.wait_for(job,1);p._agent_reply.assert_not_awaited();p.bridge.call.assert_not_awaited()
    async def test_send_failure_is_not_added_to_history(self):
        p=game_plugin();p._agent_reply=AsyncMock(return_value='还在呢');p.bridge.call.side_effect=RuntimeError('offline')
        await p._on_game_chat(payload('你好'))
        self.assertEqual(list(p._game_chat_history),[])
    async def test_bridge_stop_has_a_reserved_lane_when_normal_slots_are_full(self):
        stop=asyncio.Event()
        async def handler(p):
            if p.get('stop'):stop.set();return
            await asyncio.Event().wait()
        b=m.BridgeServer('127.0.0.1',0,'/ws','synthetic',on_chat=handler,chat_priority=lambda p:p.get('stop',False))
        try:
            for _ in range(4):await b._on_message(json.dumps({'type':'event','event':'chat','data':{}}))
            await b._on_message(json.dumps({'type':'event','event':'chat','data':{'stop':True}}))
            await asyncio.wait_for(stop.wait(),1);self.assertLessEqual(len(b._chat_jobs),5)
        finally:await b.stop()

class ObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.native=SimpleNamespace(insert_provider_stat=AsyncMock())
        self.store=obs.Telemetry(SimpleNamespace(db=self.native),self.tmp.name);await self.store.start()
    async def asyncTearDown(self):
        await self.store.close();self.tmp.cleanup()
    async def fake_context(self,with_usage=True):
        async def plain(**kwargs):return LLMResponse('assistant',completion_text='秘密正文ABC',reasoning_content='秘密思考XYZ',usage=TokenUsage(3,7,2) if with_usage else None)
        async def stream(**kwargs):
            yield LLMResponse('assistant',completion_text='秘密正文ABC',is_chunk=True)
            yield await plain(**kwargs)
        provider=SimpleNamespace(provider_config={'id':'test/provider','model':'model'},text_chat=plain,text_chat_stream=stream)
        original=SimpleNamespace(provider_manager=SimpleNamespace(get_provider_by_id=AsyncMock(return_value=provider)))
        return original,provider
    async def test_isolated_wrapper_counts_multiple_calls_without_mutating_provider(self):
        context,provider=await self.fake_context();old=provider.text_chat
        trace=obs.Trace(self.store,'test','minecraft:test')
        ctx=obs.observed_context(context,trace)
        view=await ctx.provider_manager.get_provider_by_id('test/provider')
        await view.text_chat(prompt='秘密请求123')
        async for _ in view.text_chat_stream(prompt='秘密请求123'):pass
        await trace.finish();await self.store.flush_native()
        self.assertIs(provider.text_chat,old);self.assertIsNot(ctx,context)
        self.assertEqual(len(trace.calls),2);self.assertEqual(self.native.insert_provider_stat.await_count,2)
        self.assertTrue(all('seconds' in c and 'end_time' in c for c in trace.calls))
        await trace.delivered();await self.store.flush_native()
        self.assertEqual(self.native.insert_provider_stat.await_count,2)
        self.assertTrue(all(c['usage']=={'input_other':3,'input_cached':7,'output':2} for c in trace.calls))
        record=(await (await self.store.db.execute('SELECT data FROM turns')).fetchone())[0]
        for secret in ('秘密请求123','秘密正文ABC','秘密思考XYZ'):self.assertNotIn(secret,record)
    async def test_terminal_stream_metrics_exist_before_consumer_breaks(self):
        context,_=await self.fake_context();trace=obs.Trace(self.store,'test')
        provider=await obs.observed_context(context,trace).provider_manager.get_provider_by_id('test/provider')
        generator=provider.text_chat_stream()
        await anext(generator)
        await anext(generator)
        self.assertIn('seconds',trace.calls[0]);self.assertIn('end_time',trace.calls[0])
        duration=trace.calls[0]['seconds']
        await generator.aclose();await asyncio.sleep(0)
        self.assertEqual(trace.calls[0]['status'],'completed')
        self.assertEqual(trace.calls[0]['seconds'],duration)
    async def test_public_context_database_api_is_used(self):
        self.store.context=SimpleNamespace(get_db=lambda:self.native)
        context,_=await self.fake_context();trace=obs.Trace(self.store,'test')
        provider=await obs.observed_context(context,trace).provider_manager.get_provider_by_id('test/provider')
        await provider.text_chat();await trace.finish();await self.store.flush_native()
        self.native.insert_provider_stat.assert_awaited_once()
    async def test_unknown_usage_stays_unknown_and_is_not_written_as_zero(self):
        context,_=await self.fake_context(False);trace=obs.Trace(self.store,'test')
        provider=await obs.observed_context(context,trace).provider_manager.get_provider_by_id('test/provider')
        await provider.text_chat(prompt='test');await trace.finish();await self.store.flush_native()
        self.assertIsNone(trace.calls[0]['usage']);self.assertEqual(trace.calls[0]['usage_state'],'unknown')
        self.assertIsNone(trace.calls[0]['reasoning_tokens']);self.native.insert_provider_stat.assert_not_awaited()
    async def test_tool_metrics_do_not_store_arguments_or_results(self):
        trace=obs.Trace(self.store,'test');hooks=obs.ToolHooks(trace);tool=SimpleNamespace(name='mc_inventory')
        await hooks.on_tool_start(None,tool,{'private':'秘密参数'})
        await hooks.on_tool_end(None,tool,{'private':'秘密参数'},SimpleNamespace(isError=False,content=[SimpleNamespace(text='秘密返回正文')]))
        await trace.finish();body=json.dumps(trace.data,ensure_ascii=False)
        self.assertNotIn('秘密参数',body);self.assertNotIn('秘密返回正文',body);self.assertEqual(trace.tools[0]['status'],'returned')
    async def test_native_failure_remains_pending_for_retry(self):
        context,_=await self.fake_context();trace=obs.Trace(self.store,'test')
        provider=await obs.observed_context(context,trace).provider_manager.get_provider_by_id('test/provider')
        await provider.text_chat();self.native.insert_provider_stat.side_effect=RuntimeError('temporarily unavailable')
        await trace.finish();await self.store.flush_native()
        row=await (await self.store.db.execute('SELECT native_written FROM calls')).fetchone();self.assertEqual(row[0],0)
        self.native.insert_provider_stat.side_effect=None;await self.store.flush_native()
        row=await (await self.store.db.execute('SELECT native_written FROM calls')).fetchone();self.assertEqual(row[0],1)
