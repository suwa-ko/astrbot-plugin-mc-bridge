"""MC timing/usage only. Never persist prompts, replies, arguments or images."""
from __future__ import annotations
import asyncio
import copy
import json
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

import aiosqlite
from astrbot.api import logger
from astrbot.core.agent.hooks import BaseAgentRunHooks

CURRENT_TRACE=ContextVar('mc_observation',default=None)

def _usage(value):
    if value is None:return None
    def get(k):return value.get(k) if isinstance(value,dict) else getattr(value,k,None)
    raw={k:get(k) for k in ('input_other','input_cached','output')}
    if any(v is None for v in raw.values()):return None
    return {k:max(0,int(v)) for k,v in raw.items()}

def _reason_tokens(response):
    raw=getattr(response,'raw_completion',None)
    def get(o,k):return o.get(k) if isinstance(o,dict) else getattr(o,k,None)
    usage=get(raw,'usage')
    for key in ('completion_tokens_details','output_tokens_details'):
        value=get(get(usage,key),'reasoning_tokens')
        if isinstance(value,int) and value>=0:return value
    return None

class Trace:
    def __init__(self,store,source,umo=''):
        self.store=store;self.perf=time.monotonic();self.calls=[];self.tools=[]
        self.data={'id':str(uuid.uuid4()),'source':source,'umo':umo,'start_time':time.time(),'status':'running','phases':{},'context':{},'calls':self.calls,'tools':self.tools}
    def elapsed(self):return round(time.monotonic()-self.perf,6)
    async def finish(self,status='generated',error=None):
        self.data.update(status=status,end_time=time.time(),elapsed_seconds=self.elapsed())
        if error:self.data['error_type']=type(error).__name__
        if status in {'error','cancelled','timeout','interrupted'}:
            for tool in self.tools:
                if tool['status']=='running':tool.update(status='interrupted',seconds=max(0,self.elapsed()-tool['started_seconds']))
        if self.store is not None:await self.store.save(self)
    async def delivered(self,stage='game_ack',error=None):
        if self.data.get('delivery_stage'):return
        self.data['delivery_stage']=stage
        self.data['delivery_seconds']=self.elapsed()
        await self.finish('send_failed' if error else 'delivered',error)

class ToolHooks(BaseAgentRunHooks):
    def __init__(self,trace):self.trace=trace;self.pending={}
    async def on_tool_start(self,run_context,tool,tool_args):
        entry={'name':tool.name,'started_seconds':self.trace.elapsed(),'status':'running'}
        self.trace.tools.append(entry);self.pending.setdefault(tool.name,[]).append((time.monotonic(),entry))
    async def on_tool_end(self,run_context,tool,tool_args,tool_result):
        rows=self.pending.get(tool.name,[])
        if not rows:return
        start,entry=rows.pop(0)
        entry['seconds']=round(time.monotonic()-start,6)
        entry['status']='error' if getattr(tool_result,'isError',False) else 'returned'
        entry['result_text_chars']=sum(len(str(getattr(p,'text','') or '')) for p in getattr(tool_result,'content',[]) or [])

def observed_context(context,trace,kind='chat'):
    """Copy Context/manager/provider, leaving live instances and clients intact."""
    if trace is None:return context
    ctx=copy.copy(context);manager=copy.copy(context.provider_manager)
    original=context.provider_manager.get_provider_by_id
    async def resolve(identifier):
        provider=await original(identifier)
        if provider is None:return None
        view=copy.copy(provider)
        async def observe(method,kwargs,stream):
            # Each provider call gets its own row, including tool-loop follow-ups.
            pc=getattr(provider,'provider_config',{})
            call={'id':str(uuid.uuid4()),'kind':kind,'provider_id':str(pc.get('id') or identifier),'model':str(pc.get('model') or ''),'start_time':time.time(),'started_seconds':trace.elapsed(),'status':'running','usage':None,'usage_state':'unknown','reasoning_tokens':None,'reasoning_chars':0,'reply_chars':0,'first_text_seconds':None,'first_reasoning_seconds':None}
            trace.calls.append(call);start=time.monotonic();reason=0;text=0
            def consume(response):
                nonlocal reason,text
                elapsed=time.monotonic()-start
                rc=str(getattr(response,'reasoning_content','') or '')
                tc=str(getattr(response,'completion_text','') or '')
                if rc and call['first_reasoning_seconds'] is None:call['first_reasoning_seconds']=round(elapsed,6)
                if tc and call['first_text_seconds'] is None:
                    call['first_text_seconds']=round(elapsed,6)
                    trace.data.setdefault('first_model_text_seconds',trace.elapsed())
                if getattr(response,'is_chunk',False):reason+=len(rc);text+=len(tc)
                call['reasoning_chars']=max(reason,call['reasoning_chars'],len(rc))
                call['reply_chars']=max(text,call['reply_chars'],len(tc))
                usage=_usage(getattr(response,'usage',None))
                if usage is not None:call['usage']=usage;call['usage_state']='reported'
                rt=_reason_tokens(response)
                if rt is not None:call['reasoning_tokens']=rt
                if not getattr(response,'is_chunk',False):call['status']='model_error' if getattr(response,'role',None)=='err' else 'completed'
            try:
                if stream:
                    async for response in method(**kwargs):
                        consume(response)
                        if not getattr(response,'is_chunk',False):
                            if 'end_time' not in call:call['end_time']=time.time();call['seconds']=round(time.monotonic()-start,6)
                        yield response
                else:
                    response=await method(**kwargs);consume(response);yield response
                if call['status']=='running':call['status']='completed'
            except BaseException as ex:
                if 'end_time' not in call:
                    call['status']='cancelled' if isinstance(ex,(asyncio.CancelledError,GeneratorExit)) else 'error';call['error_type']=type(ex).__name__
                raise
            finally:
                if 'end_time' not in call:call['end_time']=time.time();call['seconds']=round(time.monotonic()-start,6)
        async def text_chat(**kwargs):
            result=None
            async for response in observe(provider.text_chat,kwargs,False):result=response
            return result
        async def text_chat_stream(**kwargs):
            async for response in observe(provider.text_chat_stream,kwargs,True):yield response
        view.text_chat=text_chat;view.text_chat_stream=text_chat_stream
        return view
    manager.get_provider_by_id=resolve;ctx.provider_manager=manager
    return ctx

class Telemetry:
    def __init__(self,context,root):
        self.context=context;self.path=Path(root)/'observability.db';self.db=None;self.lock=asyncio.Lock();self.flush_lock=asyncio.Lock();self.worker=None;self.saved=0;self.wakeup=asyncio.Event()
    async def start(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=await aiosqlite.connect(self.path);self.path.chmod(0o600)
        await self.db.execute('PRAGMA journal_mode=WAL')
        await self.db.executescript('CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY,start_time REAL NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL); CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,turn_id TEXT NOT NULL,start_time REAL NOT NULL,native_written INTEGER NOT NULL DEFAULT 0,data TEXT NOT NULL); CREATE INDEX IF NOT EXISTS calls_pending ON calls(native_written,start_time);')
        await self.db.commit();await self.prune()
        self.worker=asyncio.create_task(self.retry_loop())
    async def save(self,trace):
        if self.db is None:return
        try:
            async with self.lock:
                data=trace.data
                await self.db.execute('INSERT INTO turns VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data',(data['id'],data['start_time'],data['status'],json.dumps(data,ensure_ascii=False)))
                for call in trace.calls:
                    await self.db.execute('INSERT INTO calls(id,turn_id,start_time,data) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',(call['id'],data['id'],call['start_time'],json.dumps({**call,'umo':data['umo']},ensure_ascii=False)))
                await self.db.commit();self.saved+=1
            self.wakeup.set()
            if self.saved%100==0:await self.prune()
        except Exception as ex:logger.warning('[mc-bridge] observation write deferred: %s',type(ex).__name__)
    async def flush_native(self):
        getter=getattr(self.context,'get_db',None)
        native=getter() if callable(getter) else getattr(self.context,'db',None)
        if self.db is None or not callable(getattr(native,'insert_provider_stat',None)):return
        async with self.flush_lock:
            async with self.lock:
                rows=await (await self.db.execute('SELECT id,data FROM calls WHERE native_written=0 ORDER BY start_time LIMIT 30')).fetchall()
            for ident,body in rows:
                call=json.loads(body)
                if call.get('usage') is None:
                    state=-1
                else:
                    try:
                        exists=False
                        if callable(getattr(native,'get_db',None)):
                            from sqlalchemy import text as sql
                            async with native.get_db() as session:
                                result=await session.execute(sql("SELECT id FROM provider_stats WHERE conversation_id=:id AND agent_type='mc' LIMIT 1"),{'id':ident})
                                exists=result.first() is not None
                        if not exists:
                            await asyncio.wait_for(native.insert_provider_stat(umo=call['umo'],provider_id=call['provider_id'],provider_model=call['model'],conversation_id=ident,status='completed' if call['status']=='completed' else 'failed',agent_type='mc',stats={'token_usage':call['usage'],'start_time':call['start_time'],'end_time':call.get('end_time',call['start_time']),'time_to_first_token':call.get('first_text_seconds') or 0}),timeout=3)
                        state=1
                    except Exception as ex:
                        logger.warning('[mc-bridge] native MC statistics pending: %s',type(ex).__name__);break
                async with self.lock:
                    await self.db.execute('UPDATE calls SET native_written=? WHERE id=?',(state,ident))
                    await self.db.commit()
    async def prune(self):
        if self.db is None:return
        async with self.lock:
            await self.db.execute('DELETE FROM turns WHERE start_time<? OR id NOT IN (SELECT id FROM turns ORDER BY start_time DESC LIMIT 5000)',(time.time()-30*86400,))
            await self.db.execute('DELETE FROM calls WHERE turn_id NOT IN (SELECT id FROM turns)')
            await self.db.commit()
    async def note(self,source,status):
        trace=Trace(self,source);await trace.finish(status)
    async def retry_loop(self):
        while True:
            self.wakeup.clear()
            try:await self.flush_native()
            except Exception as ex:logger.warning('[mc-bridge] statistics retry: %s',type(ex).__name__)
            try:await asyncio.wait_for(self.wakeup.wait(),timeout=60)
            except asyncio.TimeoutError:pass
    async def close(self):
        if self.worker:
            self.worker.cancel();await asyncio.gather(self.worker,return_exceptions=True);self.worker=None
        if self.db is not None:
            await self.db.close();self.db=None
