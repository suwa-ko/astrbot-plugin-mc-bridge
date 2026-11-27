"""AstrBot Minecraft bridge.

A local NeoForge client connects here over WebSocket. The plugin exposes
``mc_*`` LLM tools that turn natural language into client actions:

- status / chat / look
- Baritone goto / follow / stop
- screenshot + optional vision description

The client is the hands and eyes; AstrBot remains the brain and persona.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import hashlib
import json
import re
import time
import uuid
import copy
import math
from contextvars import ContextVar
from functools import wraps
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.tool import ToolSet
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata

from astrbot.core.agent.message import TextPart
from .shared_memory import McSharedMemory, MARKER
from .core import ActionError, SurvivalError

from .chat_policy import bound_history, history_budget, estimate_tokens, memory_needed, is_memory_question, tool_group, action_intent, is_confirmation, READONLY, SURVIVAL as SURVIVAL_TOOL_NAMES, SIMPLE as SIMPLE_TOOLS, COMPACT as COMPACT_TOOLS
from .telemetry import Telemetry, Trace, ToolHooks, CURRENT_TRACE, observed_context
from .scheduler import UnifiedScheduler, Priority, TaskState, ScheduledTask
from .skills import VanillaSkills, SkillError, parse_transfer_items, HUNTABLE, HUNT_ALIASES
from .contracts import L0_OPCODE_NAMES, L1_SPECS, L2_SPECS
from .task_metrics import TaskMetrics
from .task_journal import TaskJournal
from .intent import classify_task
from .task_recovery import TaskRecovery
from .task_checkpoint import baseline_from_status, clean_checkpoint, resume_plan, validate_resume
from .break_policy import BreakPolicy
from .short_history import ShortHistoryStore
from .poi_memory import PoiMemory
from .farm_memory import FarmMemory
from .memory_store import MemoryStore
from .privacy import redact_text
from .survival import SurvivalSupervisor

from .reply_route import ReplyRoute, REPLY_ROUTE, UNSET, event_route, game_route
from .vision import vision_input

from .mc_bridge.context import CONTROL_EPOCH, CURRENT_EVENT, PLUGIN_NAME, RESOURCE_MENTION
from .mc_bridge.server import BridgeError, BridgeServer, QueuedTask, _fmt_status, classify_goal_outcome, parse_capabilities
from .mc_bridge.runtime import RuntimeMixin
from .mc_bridge.async_memory import AsyncMemoryStore



def require_mc_tool(name):
    def decorate(handler):
        @wraps(handler)
        async def guarded(self, event, *args, **kwargs):
            token = None
            reply_token = None
            event_token = None
            if event is not None:
                event_token = CURRENT_EVENT.set(event)
            if event is not None and CONTROL_EPOCH.get() is None:
                epoch = event.get_extra("_mc_control_epoch", None)
                if epoch is None:
                    epoch = self._control_epoch
                    event.set_extra("_mc_control_epoch", epoch)
                token = CONTROL_EPOCH.set(epoch)
            try:
                denied = self._tool_denial(event, name)
                if denied:
                    return denied
                reply_token=REPLY_ROUTE.set(event_route(event,self.bridge))
                return await handler(self, event, *args, **kwargs)
            finally:
                if reply_token is not None:REPLY_ROUTE.reset(reply_token)
                if token is not None:
                    CONTROL_EPOCH.reset(token)
                if event_token is not None:
                    CURRENT_EVENT.reset(event_token)
        return guarded
    return decorate




class McBridgePlugin(RuntimeMixin, Star):
    """Minecraft bridge plugin."""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        self.bridge = BridgeServer(
            host=str(self.config.get("host", "0.0.0.0")),
            port=int(self.config.get("port", 8766)),
            path=str(self.config.get("path", "/ws")),
            token=str(self.config.get("token", "change-me")),
            on_chat=self._on_game_chat,
            on_disconnect=self._connection_lost,
            on_ready=self._client_ready,
            chat_priority=self._priority_game_chat,
            on_chat_overflow=self._game_chat_overflow,
            chat_pending_limit=self.config.get("game_chat_pending_limit",4),
            on_survival_event=self._on_survival_event,
            allow_query_token=bool(self.config.get("allow_query_token", False)),
        )
        self.bridge.on_respawn=self._respawn_signal
        self._started = False
        self._shared_memory = None
        self._memory_store = None
        self._memory_world_cache: dict[str, str] = {}
        self._last_status: dict = {}
        self._last_owner_route = None
        self._survival: SurvivalSupervisor | None = None
        self._closing = False
        self._manual_movement = False
        self._control_epoch = 0
        self._auto_eat_requested = False
        self._interrupted_task = None
        self._resume_attempts = 0
        self._action_lock = asyncio.Lock()
        self._agent_lock = asyncio.Lock()
        self._agent_jobs: set[asyncio.Task] = set()
        self._agent_notice_routes = {}
        self._active_agent_task = None
        self._manual_notice_route = None
        self._game_chat_history: deque[str] = deque(maxlen=20)
        self._mc_histories: dict[str, deque[str]] = {}
        self._pending_actions: dict[str, str] = {}
        self._game_chat_lock = asyncio.Lock()
        self._game_chat_last = 0.0
        self._game_chat_waiters = 0
        self._game_chat_recent = {}
        self._game_queue_notice_last = 0.0
        self._telemetry = None
        self._last_game_reply = ""
        self._active_task: asyncio.Task | None = None
        self._task_stop = asyncio.Event()
        self._task_queue: deque[QueuedTask] = deque()
        self._scheduler = UnifiedScheduler()
        self._worker_generation = 0
        self._task_current: QueuedTask | None = None
        self._last_task_result: dict | None = None
        self._preempting = False
        self._eating_until = 0.0
        self._group_memory_last: dict[str, float] = {}

    @staticmethod
    def _split_game_chat(text: str) -> tuple[str, str]:
        """Return (sender, body) from the vanilla chat line."""
        text = str(text or "").strip()
        m = re.match(r"^<([^>]{1,32})>\s*(.*)$", text)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        m = re.match(r"^([^\s:：<>]{1,32})\s*[:：]\s*(.*)$", text)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        return "", text

    def _player_allowed(self, sender: str) -> bool:
        if not sender:
            return False
        raw = str(self.config.get("game_allowed_players", "Player"))
        names = [x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()]
        owner = str(self.config.get("owner_name", "Player")).strip()
        if owner and owner not in names:
            names.append(owner)
        return any(sender.lower() == name.lower() for name in names)

    def _safe_tool_names(self) -> set[str]:
        raw = str(
            self.config.get(
                "game_safe_tools",
                "mc_status,mc_look,mc_see,mc_screenshot",
            )
        )
        return {x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()}

    @staticmethod
    def _make_game_event(text: str, sender: str = "") -> AstrMessageEvent:
        """Build a minimal private AstrBot event for in-game chat.

        The game-chat path has no QQ/NapCat event, but tool_loop_agent needs an
        event for tool execution and conversation bookkeeping. This synthetic
        session is isolated under platform id ``minecraft``.
        """
        msg = AstrBotMessage()
        msg.type = MessageType.FRIEND_MESSAGE
        msg.self_id = "MinecraftBot"
        msg.session_id = "minecraft:gamechat"
        msg.message_id = f"gamechat-{uuid.uuid4().hex[:10]}"
        msg.group = None
        msg.sender = MessageMember(
            user_id=f"minecraft:{sender or 'player'}",
            nickname=sender or "玩家",
        )
        msg.message = [Plain(text)]
        msg.message_str = text
        msg.raw_message = None
        msg.timestamp = int(time.time())
        meta = PlatformMetadata(
            name="minecraft",
            description="Minecraft game chat",
            id="minecraft",
        )
        event = AstrMessageEvent(text, msg, meta, "minecraft:gamechat")
        event.is_wake = True
        event.is_at_or_wake_command = True
        return event

    def _priority_game_chat(self,payload):
        body=str(payload.get('body',''))[:1000]
        return bool(payload.get('kind')=='player' and re.fullmatch(r'[0-9a-fA-F-]{36}',str(payload.get('sender_uuid',''))) and self._player_allowed(str(payload.get('sender_name',''))) and not self._is_memory_query(body) and self._is_stop_request(body))

    async def _game_chat_overflow(self,payload):
        if not self.config.get('reply_game_chat',False) or payload.get('_connection_id')!=self.bridge.session_id or payload.get('kind')!='player' or not re.fullmatch(r'[0-9a-fA-F-]{36}',str(payload.get('sender_uuid',''))):return
        raw=self.config.get('game_chat_names','MinecraftBot,Bot')
        names=raw if isinstance(raw,list) else re.split(r'[,，\s]+',str(raw))
        if not any(str(n).strip() and str(n).lower() in str(payload.get('body','')).lower() for n in names):return
        if getattr(self,'_telemetry',None) is not None:await self._telemetry.note('game','queue_full')
        now=time.time()
        if now-getattr(self,'_game_queue_notice_last',0)>15:
            self._game_queue_notice_last=now
            await self.bridge.call('chat',{'text':'这边还有几句话在排队，等我回完再叫我一下呀'},timeout=5)

    async def _on_game_chat(self, payload: dict) -> None:
        """Answer and act in Minecraft when someone addresses the bot by name.

        Uses a synthetic event so the normal ``mc_*`` tool loop is available:
        chat is just the fallback, while movement/follow/vision can be invoked
        directly from game chat.
        """
        if not bool(self.config.get("reply_game_chat", False)):
            return
        if self._closing or not isinstance(payload, dict):
            return
        if payload.get("_connection_id") != self.bridge.session_id:
            return
        sender = str(payload.get("sender_name", "")).strip()
        identity = str(payload.get("sender_uuid", ""))
        trusted = payload.get("kind") == "player" and bool(re.fullmatch(r"[0-9a-fA-F-]{36}", identity))
        if not trusted or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", sender):
            return
        text = " ".join(str(payload.get("body", "")).split())[:1000]
        if not text or sender.lower() == str(self.config.get("self_name", "MinecraftBot")).lower():
            return
        received=time.monotonic();received_wall=time.time();received_epoch=getattr(self,'_control_epoch',0)
        allowed = self._player_allowed(sender)
        raw_names = self.config.get("game_chat_names", "MinecraftBot,Bot")
        if isinstance(raw_names, list):
            names = [str(x).strip() for x in raw_names]
        else:
            names = [x.strip() for x in re.split(r"[,，\s]+", str(raw_names))]
        names = [x for x in names if x]
        if not names or not any(x.lower() in text.lower() for x in names):
            return

        route=game_route(self.bridge)
        memory_query = self._is_memory_query(text)
        busy_chat = self._active_task is not None and not self._active_task.done()
        task_like = not memory_query and bool(
            self._queue_requested(text)
            or "然后" in text
            or re.search(
                r"(来我这边|过来|到我这|来我这|去坐标|前往|goto|去\s*[A-Za-z0-9_]{2,16}\s*那边)",
                text,
                re.I,
            )
        )
        if self._active_task is not None and not self._active_task.done():
            if not allowed:
                return
            if not self._is_memory_query(text) and self._is_stop_request(text):
                await self._interrupt_actions("任务已取消",requester_route=route)
                return
            if task_like:
                tasks, unparsed = self._parse_tasks_ex(text, sender)
                if tasks and not unparsed:
                    accepted = self._enqueue_routed(tasks,route)
                    await self._task_chat(f"已加入队列（{len(tasks)} 项）" if accepted else "任务队列已满，未加入",route=route)
                    return
                if unparsed:
                    await self._task_chat("这条复合指令没有全部解析，当前任务进行中，未执行；请等任务结束或拆开说",route=route)
                    return
                if self._queue_requested(text):
                    await self._task_chat(self._task_status_text(),route=route)
                    return
                return
            if not memory_query and re.search(r"跟着|跟随|揍|攻击|撤离|快跑|吃东西|看着", text):
                await self._task_chat("任务进行中，先对我说“停”再换动作",route=route)
                return
        if task_like:
            if not allowed:
                return
            tasks, unparsed = self._parse_tasks_ex(text, sender)
            if tasks and not unparsed:
                if not self._enqueue_routed(tasks,route):
                    await self._task_chat("任务队列已满，未加入",route=route)
                return
            if unparsed:
                # Hand the whole compound sentence to the planner instead of dropping steps.
                pass
            elif self._queue_requested(text):
                await self._task_chat(self._task_status_text(),route=route)
                return
        if not self._is_memory_query(text) and self._is_stop_request(text):
            if not allowed:
                return
            await self._interrupt_actions("已停止",requester_route=route)
            return

        now = time.time()
        try:
            cooldown = float(self.config.get("game_chat_cooldown", 3))
        except (TypeError, ValueError):
            cooldown = 3.0
        recent=getattr(self,'_game_chat_recent',{})
        recent={k:v for k,v in recent.items() if now-v<max(0.0,cooldown)}
        key=(identity,text)
        if key in recent:
            if getattr(self,'_telemetry',None) is not None:await self._telemetry.note('game','duplicate')
            return
        if len(recent)>=128:recent.pop(next(iter(recent)))
        recent[key]=now;self._game_chat_recent=recent
        if getattr(self,'_game_chat_waiters',0)>=max(1,min(8,int(self.config.get('game_chat_pending_limit',4)))):
            if getattr(self,'_telemetry',None) is not None:await self._telemetry.note('game','queue_full')
            if now-getattr(self,'_game_queue_notice_last',0)>15:
                self._game_queue_notice_last=now
                await self.bridge.call('chat',{'text':'这边还有几句话在排队，等我回完再叫我一下呀'},timeout=5)
            return

        provider = str(self.config.get("mc_provider_id", "your-provider/chat-model")).strip()
        if not provider:
            return

        self._game_chat_waiters=getattr(self,'_game_chat_waiters',0)+1
        try:
            async with self._game_chat_lock:
                if self._closing or payload['_connection_id']!=self.bridge.session_id or received_epoch!=getattr(self,'_control_epoch',0):
                    if getattr(self,'_telemetry',None) is not None:await self._telemetry.note('game','stale_queued')
                    return
                self._game_chat_last=time.time()
                busy_chat=self._active_task is not None and not self._active_task.done()
                bounded=bound_history(self._game_chat_history,history_budget(self.config))
                history = "\n".join(bounded)
                who = sender or "玩家"
                pending_key='minecraft:' + (sender or 'gamechat')
                if not hasattr(self,'_pending_actions'): self._pending_actions={}
                pending=self._pending_actions.get(pending_key)
                confirmed=is_confirmation(text)
                pending_text=f"上一轮待确认的游戏请求：{pending}\n玩家现在确认：" if (allowed and pending and confirmed) else ""
                if allowed and action_intent(text) and not confirmed:
                    self._pending_actions[pending_key]=text
                prompt = (
                    (f"最近的游戏内对话：\n{history}\n\n" if history else "")
                    + pending_text
                    + f"玩家 {who} 在游戏里说：{text}\n\n"
                    "如果只是聊天就直接回答；如果玩家要求动作，调用对应的 mc_* 工具后再简短汇报。不要调用 mc_chat，"
                    "你的最终回复会自动发到游戏聊天。用 1-2 句简短自然的中文。"
                )
                try:
                    event = self._make_game_event(text, sender)
                    if allowed and pending and confirmed:
                        event.set_extra('_mc_pending_action',pending)
                    event.set_extra('_mc_received_perf',received)
                    event.set_extra('_mc_received_wall',received_wall)
                    event.set_extra('_mc_queue_seconds',time.monotonic()-received)
                    event.set_extra('_mc_history_tokens',estimate_tokens(history))
                    event.set_extra('_mc_history_lines',len(bounded))
                    event.set_extra("_mc_trusted_player", sender)
                    event.set_extra("_mc_sender_uuid", identity)
                    event.set_extra("_mc_connection_id", payload["_connection_id"])
                    reply = await self._agent_reply(
                        event,
                        prompt,
                        exclude_tools={"mc_chat"},
                        allow_actions=allowed and not busy_chat,
                    )
                except Exception as exc:
                    logger.warning("[mc-bridge] game chat request failed: %s", type(exc).__name__)
                    reply = "刚才这次没有处理完，操作未确认成功，稍后再叫我一下"

                reply = " ".join(reply.split())
                if reply.startswith(("/", "／", "#", "＃")):
                    reply = " " + reply.lstrip("/／#＃").strip()
                if not reply:
                    return
                reply = reply[:200]
                self._last_game_reply = reply
                if received_epoch!=getattr(self,'_control_epoch',0) or payload['_connection_id']!=self.bridge.session_id:
                    trace=event.get_extra('_mc_trace',None)
                    if trace is not None:await trace.finish('stale_before_delivery')
                    return
                try:
                    await self.bridge.call("chat", {"text": reply}, timeout=10)
                    self._game_chat_history.extend([f"{sender}：{text}",f"你：{reply}"])
                    if (confirmed or action_intent(text)) and not re.search(r'[?？]|吗|还是|是否|哪|需不需要|要不要', reply):
                        self._pending_actions.pop(pending_key,None)
                    bounded=bound_history(self._game_chat_history,history_budget(self.config))
                    self._game_chat_history.clear();self._game_chat_history.extend(bounded)
                    trace=event.get_extra('_mc_trace',None)
                    if trace is not None:await trace.delivered()
                    logger.info(f"[mc-bridge] game chat reply: {reply}")
                    if self._shared_memory is not None:
                        await self._shared_memory.capture_game(event, self.bridge, text, reply)
                except Exception as exc:
                    trace=event.get_extra('_mc_trace',None)
                    if trace is not None:await trace.delivered(error=exc)
                    logger.warning('[mc-bridge] game chat send failed: %s',type(exc).__name__)
        finally:
            self._game_chat_waiters-=1


    async def initialize(self) -> None:
        if str(self.config.get("token", "change-me")) == "change-me":
            logger.warning("[mc-bridge] token 仍是 change-me，请尽快修改")
        self._apply_tool_profile()
        try:
            await self.bridge.start()
            self._started = True
            try:
                self._telemetry=Telemetry(self.context,StarTools.get_data_dir(PLUGIN_NAME))
                await self._telemetry.start()
            except Exception as observation_error:
                if self._telemetry is not None:await self._telemetry.close()
                self._telemetry=None
                logger.warning('[mc-bridge] observation unavailable: %s',type(observation_error).__name__)
            self._shared_memory = McSharedMemory(self.context, self.config, StarTools.get_data_dir(PLUGIN_NAME))
            try:
                root = Path(StarTools.get_data_dir(PLUGIN_NAME))
                self._memory_store = MemoryStore(root / "memory.db")
                legacy = root / "poi_memory.json"
                if legacy.exists():
                    self._memory_store.migrate_legacy_file(legacy)
                self._shared_memory.attach_memory_store(self._memory_store)
            except Exception as memory_error:
                logger.warning("[mc-bridge] structured memory unavailable: %s", type(memory_error).__name__)
                self._memory_store = None
            await self._shared_memory.start()
            self._survival = SurvivalSupervisor(self)
            await self._survival.start()
        except Exception as exc:
            logger.error(f"[mc-bridge] failed to start WebSocket server: {exc}")

    async def terminate(self) -> None:
        self._closing = True
        self._control_epoch += 1
        if getattr(self, "_survival", None) is not None:
            try:
                await self._survival.stop()
            except Exception:
                pass
            self._survival = None
        self._persist_recovery_snapshot()
        active = self._active_task
        self._cancel_active_task()
        jobs = {job for job in (active, *self._agent_jobs)
                if job is not None and job is not asyncio.current_task()}
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        await self._send_stop()
        self._agent_jobs.clear()
        if self._started:
            await self.bridge.stop()
        if getattr(self, "_shared_memory", None) is not None:
            await self._shared_memory.stop()
        if getattr(self, "_memory_store", None) is not None:
            try:
                self._memory_store.close()
            except Exception:
                pass
            self._memory_store = None
        if getattr(self, "_coach", None) is not None:
            try:
                self._coach.store.close()
            except Exception:
                pass
            self._coach = None
        if getattr(self,'_telemetry',None) is not None:
            await self._telemetry.close()
        self._started = False

    async def _call(self, action: str, params: dict | None = None, timeout: float = 20.0):
        readonly = action in {"status", "screenshot", "scan", "scan_blocks", "scan_entities", "inspect_block", "block_context", "get_menu", "operation","knowledge","sense"} or (action == "inventory" and (params or {}).get("operation", "view") == "view")
        epoch = CONTROL_EPOCH.get()
        if not readonly and (self._closing or (epoch is not None and epoch != self._control_epoch)):
            return {"__error__": "请求已停止或被新的控制操作中断"}
        if action in {"inventory", "equip", "craft", "workbench", "scan", "mine_block", "sleep", "operation", "navigate"} and self.bridge.client_info.get("protocol", 1) < 3:
            return {"__error__": "此动作需要0.3.0 Mod / 协议3，请重新启动更新后的客户端"}
        if action in {'knowledge','sense','station','smelt','interact','pickup','move_item','trade','ride','dismount','wake','plant','dig_down','block_context','get_menu','interact_block','release_use','mine_below','align','unequip_armor','till','jump','use_bonemeal','hunt'} and action not in parse_capabilities(self.bridge.client_info.get('capabilities')):
            return {'__error__':'此功能需要0.4.0或更新的客户端 Mod，请安装新版后重连'}
        if not readonly and self.bridge.connected and self.bridge.client_info.get("protocol", 1) < 2:
            return {"__error__": "客户端协议过旧，请安装 0.2.0 Mod 后重新进入游戏"}
        guard_error = await self._guard_break_action(action, params)
        if guard_error:
            return {"__error__": guard_error}
        try:
            if readonly:
                result = await self.bridge.call(action, params, timeout)
            else:
                async with self._action_lock:
                    if self._closing or (epoch is not None and epoch != self._control_epoch):
                        return {"__error__": "请求已停止或被新的控制操作中断"}
                    result = await self.bridge.call(action, params, timeout)
        except (BridgeError, ConnectionError, RuntimeError, asyncio.TimeoutError) as exc:
            return {"__error__": str(exc) or "客户端动作响应超时"}
        if action == "status" and isinstance(result, dict):
            if not self._err(result):
                self._last_status = dict(result)
            self._note_world_status(result)
        return result

    @staticmethod
    def _err(data: Any) -> str | None:
        if isinstance(data, dict) and data.get("__error__"):
            return str(data["__error__"])
        return None

    async def _run_operation(self, action: str, params: dict, seconds: float = 10, timeout: float = 10) -> dict:
        """Start a client operation and await push completion (Protocol 4) or poll fallback (Protocol 3)."""
        result = await self._call(action, params, timeout=timeout)
        err = self._err(result)
        if err:
            raise SurvivalError(err)
        if not isinstance(result, dict) or not result.get("operation_id") or result.get("phase") != "started":
            raise SurvivalError("客户端没有确认动作启动")
        identifier = result["operation_id"]
        deadline = time.monotonic() + max(1.0, float(seconds))
        use_push = (
            "operation_events" in parse_capabilities(self.bridge.client_info.get("capabilities"))
            and callable(getattr(self.bridge, "wait_operation", None))
        )
        try:
            while time.monotonic() < deadline:
                data = None
                if use_push:
                    remaining = max(0.05, deadline - time.monotonic())
                    try:
                        data = await self.bridge.wait_operation(identifier, timeout=min(remaining, 5.0))
                    except BridgeError as exc:
                        raise SurvivalError(str(exc)) from exc
                else:
                    await asyncio.sleep(0.3)
                if data is None:
                    data = await self._call("operation", {"operation_id": identifier}, timeout=8)
                err = self._err(data)
                if err:
                    raise SurvivalError(err)
                phase = data.get("phase")
                if phase == "completed" and data.get("success") is True:
                    return data
                if phase == "failed":
                    raise SurvivalError(data.get("error", "动作未完成"))
                if phase not in {"running", "started"}:
                    raise SurvivalError("动作状态无效")
        except asyncio.CancelledError:
            try:
                await self._call("baritone", {"command": "stop"}, timeout=5)
            except Exception:
                pass
            raise
        await self._call("baritone", {"command": "stop"}, timeout=5)
        raise SurvivalError("动作超时，已停止，未确认完成")

    def _mc_toolset(self) -> ToolSet:
        """Only expose mc_* tools to the MC operator agent."""
        manager = self.context.get_llm_tool_manager()
        full = manager.get_full_tool_set()
        filtered = ToolSet()
        for tool in full.tools:
            if getattr(tool, "active", True) and str(getattr(tool, "name", "")).startswith("mc_"):
                filtered.add_tool(tool)
        return filtered

    def _persona_prompt(self) -> str:
        persona_id = str(self.config.get("persona_id", "MinecraftBot")).strip()
        manager = self.context.persona_manager
        persona = None
        try:
            persona = manager.get_persona_v3_by_id(persona_id)
        except Exception:
            persona = None
        if persona is None:
            persona = getattr(manager, "selected_default_persona_v3", None)
        if isinstance(persona, dict):
            return str(persona.get("prompt") or "")
        return ""

    def _tool_profile(self) -> str:
        raw = str(self.config.get("tool_profile", "simple")).strip().lower()
        if raw in {"compact", "simple", "full"}:
            return raw
        return "simple"

    def _apply_tool_profile(self) -> None:
        profile = self._tool_profile()
        if profile == "full":
            return
        allowed = COMPACT_TOOLS if profile == "compact" else SIMPLE_TOOLS
        try:
            manager = self.context.get_llm_tool_manager()
            for tool in list(getattr(manager, "func_list", [])):
                name = str(getattr(tool, "name", "") or "")
                if name.startswith("mc_") and name not in allowed:
                    manager.remove_func(name)
        except Exception as exc:
            logger.warning("[mc-bridge] tool profile filter skipped: %s", type(exc).__name__)

    def _simple_system_prompt(self, allow_actions: bool, owner: bool) -> str:
        owner_name = str(self.config.get("owner_name", "Player") or "Player")
        prompt = (
            f"\n\n你现在通过 Minecraft 客户端账号行动，默认主人游戏名是 {owner_name}。"
            "常用入口：mc_brief 一次查看位置、血量、饥饿、背包摘要、家床和任务；mc_inventory 查看完整背包；"
            "mc_goto/mc_come/mc_follow/mc_stop 控制移动；mc_tunnel_to 有界挖掘隧道接近目标；mc_return_home 从地下或远处回家；mc_mine_at/mc_dig_down/mc_place_at 挖掘或放置；"
            "mc_pickup 拾取掉落物；mc_container 打开/读取/点击容器；mc_interact 开关门和按钮；"
            "mc_eat 进食、mc_equip 换装备、mc_attack 攻击、mc_block_context 查看方块和站位；"
            "mc_scan 扫描附近方块/实体；mc_set_respawn 设置重生床；mc_recipes 查配方；"
            "mc_memory 查询/记录地点（模型只看到 place_ref 和安全标签，精确坐标由本地回执卡片显示）；"
            "mc_goto 支持 place_ref 导航；mc_protect 管理保护区域；mc_screenshot 只在确实需要看画面时使用。"
            "多步骤任务优先用 mc_plan 一次提交 steps 数组，按顺序执行，完成或失败后统一通知；只在需要单独控制某一步时用 mc_task_start。mc_task_start 的 opcode 支持 mine_ore(resource,count,return_home)（煤/铁/铜/金/红石/青金石/钻石/绿宝石，深层矿会自动向下挖）、gather(resource,count)、craft(item,count)、smelt(item,count)、"
            "container_transfer(items 或 item,count,direction,x,y,z)、build(pattern,material,x,y,z,length,width,height,direction)、"
            "farm(operation,count,radius,replant)、farm_plot(operation,plot,radius,count,crop,cells,mask,origin,replant,dry_run)（支持不规则格子集合）、sleep(source,remember)、combat(target,max_seconds,retreat_health)、hunt(target,count,radius,max_seconds,return_home,cook)、"
            "explore(goal,max_seconds,max_distance)、return_to(target_pos)、return_home(reach)、tunnel_to(target_pos,max_distance,reach)、resupply(...)、trade_for(item,count,radius)；"
            "params_json 传 JSON 对象，不确定时先用 dry_run=true 预览。"
            "mc_task_status 查看进度、mc_task_cancel 取消任务。"
            "任务提交后：如果返回 inline=true，说明短任务已在本次调用内完成，直接汇报结果；否则只回复“已提交/已开始”并结束本轮，完成或失败会主动通知，不要连续轮询。"
            "实际结果以工具返回为准，加入队列不等于完成；低血、敌人靠近或目标不可达时会停止；不要编造没有工具确认的结果。"
        )
        if not allow_actions:
            prompt += "\n本轮只能聊天或查看，不执行新的游戏动作。" if owner else "\n当前说话的人不是主人，只能聊天和查看；动作需要主人下令。"
        return prompt

    # ---------------------------------------------------------------- tools

    @filter.llm_tool(name="mc_status")
    @require_mc_tool("mc_status")
    async def mc_status(self, event: AstrMessageEvent):
        """查看 Minecraft 机器人当前状态：坐标、维度、血量、饱食、手持、背包、附近玩家/实体。"""
        data = await self._call("status", timeout=15)
        err = self._err(data)
        if err:
            return err
        if self._audience(event) != "owner_private" and isinstance(data, dict):
            # Public Minecraft chat is a public destination even when the
            # speaker is the owner.  Keep only non-locating status fields.
            data = dict(data)
            for key in ("position", "server", "nearby_players", "nearby_entities",
                        "navigation_protection", "path_feedback", "inventory", "held",
                        "armor", "screen"):
                data.pop(key, None)
            return _fmt_status(data)
        return _fmt_status(data)

    @filter.llm_tool(name="mc_brief")
    @require_mc_tool("mc_brief")
    async def mc_brief(self, event: AstrMessageEvent):
        """一次返回位置/血量/任务/家床等常用只读信息，替代多次 mc_status/mc_task_status/mc_memory。"""
        data = await self._call("status", timeout=10)
        err = self._err(data)
        if err:
            return "状态查询失败：" + err
        audience = self._audience(event)
        private_view = audience == "owner_private"
        pos = data.get("position") or {}
        try:
            task = self._agent_task_status("", detail=False)
        except Exception:
            task = {}

        def safe_memory(row):
            if not isinstance(row, dict):
                return None
            store = getattr(self, "_memory_store", None)
            if store is None and not private_view:
                return None
            if store is not None:
                try:
                    return store.safe_view(row, audience=audience)
                except Exception:
                    pass
            return {
                "kind": row.get("kind"),
                "key": row.get("key") if private_view else "",
                "label": row.get("name") or row.get("kind") or "地点",
                "name": row.get("name") or row.get("kind") or "地点",
                "dimension": row.get("dimension") or "",
                "status": "legacy",
                "warnings": ["旧记录，未验证；精确坐标不在模型上下文中"],
            }

        home_bed = home = None
        try:
            store = getattr(self, "_memory_store", None)
            if store is not None:
                scope = self._memory_scope_for(data)
                if private_view:
                    home_bed = store.latest_place(**scope, kind="home_bed", key="main")
                    home = store.latest_place(**scope, kind="home", key="main")
                else:
                    beds = store.find_places(**scope, kind="home_bed", key="main",
                                              audience=audience, limit=1)
                    homes = store.find_places(**scope, kind="home", key="main",
                                              audience=audience, limit=1)
                    home_bed = beds[0] if beds else None
                    home = homes[0] if homes else None
            else:
                memory = self._poi_memory_for()
                home_bed = memory.get("home_bed", "main")
                home = memory.get("home", "main")
        except Exception:
            home_bed = home = None
        if private_view:
            self._queue_place_cards(event, [row for row in (home_bed, home) if isinstance(row, dict)])
        result = {
            "dimension": data.get("dimension"),
            "health": data.get("health"),
            "food": data.get("food"),
            "on_ground": data.get("on_ground"),
            "screen": data.get("screen"),
            "home_bed": safe_memory(home_bed),
            "home": safe_memory(home),
        }
        if private_view:
            result.update({
                "position": {k: pos.get(k) for k in ("x", "y", "z")},
                "hostiles": (data.get("hostiles") or [])[:3],
                "path_feedback": data.get("path_feedback"),
                "task": {k: task.get(k) for k in ("active", "pending_count", "current_task_id", "queue")},
            })
        else:
            task_state = task if isinstance(task, dict) else {}
            result["task"] = {"active": bool(task_state.get("active"))}
        return json.dumps(result, ensure_ascii=False, default=str)

    @filter.llm_tool(name="mc_chat")
    @require_mc_tool("mc_chat")
    async def mc_chat(self, event: AstrMessageEvent, text: str):
        """让机器人在 Minecraft 里说一句话（只发送普通聊天，不能执行服务器指令）。

        Args:
            text(string): 要说的话，尽量简短。
        """
        text = str(text or "").strip()
        if not text:
            return "没有要发送的内容"
        if text.startswith(("/", "／", "#", "＃")):
            return "mc_chat 只允许普通聊天；服务器指令请用专用工具或让管理员执行"
        if len(text) > 240:
            text = text[:240]
        data = await self._call("chat", {"text": text}, timeout=10)
        err = self._err(data)
        return err or "已发送"

    @filter.llm_tool(name="mc_goto")
    @require_mc_tool("mc_goto")
    async def mc_goto(self, event: AstrMessageEvent, x: int = 0, y: int = 0, z: int = 0,
                      allow_break: bool = False, place_ref: str = "", strict: bool = False):
        """加入受监控的坐标移动任务。目标不可站立时会自动改用最近站位。

        Args:
            x(int): 目标方块或站位 X。
            y(int): 目标方块或站位 Y。
            z(int): 目标方块或站位 Z。
            allow_break(bool): 是否允许本次移动受控掘进穿过方块；默认否，普通移动不会自动挖方块。
            place_ref(string): 记忆地点的不透明句柄；提供时由本地解析坐标，优先于 x/y/z。
            strict(bool): 精确走到给定坐标，不做最近站位解析；用于已知可站立但解析器会偏开的点。
        """
        place_ref = str(place_ref or "").strip()
        if place_ref:
            try:
                store = self._memory_store_for()
                status = await self._call("status", timeout=10)
                err = self._err(status)
                if err:
                    return err
                scope = self._memory_scope_for(status)
                row, message = store.resolve_for_navigation(
                    place_ref, world_key=scope["world_key"],
                    world_instance=scope["world_instance"],
                    dimension=scope["dimension"], audience=self._audience(event),
                )
                if row is None and self._audience(event) == "owner_private":
                    # A legacy/expired place may become usable after a local
                    # block-context check; never trust it without verification.
                    candidate = store.get_place(place_ref)
                    if candidate is not None and store.can_view(candidate, "owner_private"):
                        await self._verify_memory_place(candidate, scope)
                        row, message = store.resolve_for_navigation(
                            place_ref, world_key=scope["world_key"],
                            world_instance=scope["world_instance"],
                            dimension=scope["dimension"], audience=self._audience(event),
                        )
                if row is None:
                    return message or "这个地点不能自动导航"
                local = store.local_view(row)
                stand = local.get("stand") or local.get("target")
                if not stand or stand.get("x") is None:
                    return "这个地点没有可用站位，未加入移动任务"
                tx, ty, tz = int(round(float(stand["x"]))), int(round(float(stand.get("feet_y", stand["y"])))), int(round(float(stand["z"])))
                label = local.get("label") or local.get("kind") or "地点"
                dependency = self._chain_dependency()
                item = QueuedTask("goto", f"去 {label}", x=tx, y=ty, z=tz,
                                  params={"allow_break": bool(allow_break), "place_ref": place_ref},
                                  dependencies=(dependency,) if dependency else ())
                if not self._enqueue_tasks([item]):
                    return "队列已满，未加入"
                self._remember_task_chain(item.task_id)
                self._queue_place_cards(event, [row])
                return f"已按地点记忆加入移动任务（{label}），实际到达后才汇报完成；精确坐标由本地卡片显示"
            except Exception as exc:
                return "地点导航未完成：" + type(exc).__name__
        if strict:
            target, info = (int(x), int(y), int(z)), {"adjusted": False, "source": "strict"}
        else:
            target, info = await self._resolve_navigation_target(x, y, z)
        if target is None:
            return info
        tx, ty, tz = target
        if info.get("adjusted") and (
            abs(tx - int(x)) > 3 or abs(ty - int(y)) > 3 or abs(tz - int(z)) > 3
        ):
            return f"目标不可站立，最近可用站位偏离目标（{tx} {ty} {tz}），未加入移动任务；请换一个目标"
        dependency = self._chain_dependency()
        item = QueuedTask(
            "goto", f"去 {tx} {ty} {tz}", x=tx, y=ty, z=tz,
            params={"allow_break": bool(allow_break)},
            dependencies=(dependency,) if dependency else (),
        )
        if not self._enqueue_tasks([item]):
            return "队列已满，未加入"
        self._remember_task_chain(item.task_id)
        if info.get("adjusted"):
            return f"目标方块不可站立，已改到最近站位 {tx} {ty} {tz}；已加入移动任务，实际到达后才汇报完成"
        return "已加入移动任务，实际到达后才汇报完成"

    @filter.llm_tool(name="mc_tunnel_to")
    @require_mc_tool("mc_tunnel_to")
    async def mc_tunnel_to(self, event: AstrMessageEvent, x: int, y: int, z: int,
                           max_distance: int = 32, reach: float = 2.0):
        """挖掘一条有界隧道/台阶到附近可站立目标，最多 32 格、垂直差不超过 12 格。

        Args:
            x(int): 目标 X。
            y(int): 目标 Y。
            z(int): 目标 Z。
            max_distance(int): 最大水平范围 1-32。
            reach(float): 可接受距离 0.5-4。
        """
        if self._queue_skill(
            f"隧道到 {x} {y} {z}",
            "tunnel_to",
            target_pos=[int(x), int(y), int(z)],
            max_distance=max(1, min(32, int(max_distance))),
            reach=max(0.5, min(4.0, float(reach))),
        ):
            return "已加入隧道接近任务，到达后才汇报完成"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_return_home")
    @require_mc_tool("mc_return_home")
    async def mc_return_home(self, event: AstrMessageEvent, reach: float = 2.0):
        """从地下或远处返回记录的家床；优先走可用路线，无路时会向上挖出地表，近距离还会尝试有界隧道。

        Args:
            reach(float): 到达家的可接受距离 0.5-8。
        """
        if self._queue_skill(
            "回家",
            "return_home",
            reach=max(0.5, min(8.0, float(reach))),
        ):
            return "已加入回家任务，实际到达后才汇报完成"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_follow")
    @require_mc_tool("mc_follow")
    async def mc_follow(self, event: AstrMessageEvent, player: str):
        """让机器人持续跟随指定玩家。

        Args:
            player(string): 玩家名，例如 Player。
        """
        player = str(player or "").strip()
        if not player or " " in player:
            return "玩家名不合法"
        data = await self._call(
            "baritone", {"command": f"follow player {player}"}, timeout=10
        )
        err = self._err(data)
        if not err:
            self._manual_movement = True
            self._manual_notice_route=REPLY_ROUTE.get()
        return err or f"已开始跟随 {player}"

    @filter.llm_tool(name="mc_stop")
    @require_mc_tool("mc_stop")
    async def mc_stop(self, event: AstrMessageEvent):
        """立刻停止机器人的 Baritone 移动/跟随，并取消当前任务。"""
        error = await self._stop_task_runtime()
        return error or "停止指令已确认（client_stop_confirmed=true），任务队列已清空"

    @filter.llm_tool(name="mc_look")
    @require_mc_tool("mc_look")
    async def mc_look(self, event: AstrMessageEvent, player: str = "", yaw: float = 999, pitch: float = 999):
        """看向玩家、调整摄像头或读取当前视角；不移动角色。留空返回当前视角和准星信息。

        Args:
            player(string): 玩家名；留空且不提供角度时只读取当前视角。
            yaw(float): 可选水平角-360至360；0南、90西、180北、-90东。
            pitch(float): 可选俯仰角-90至90；0平视，正数向下。
        """
        if yaw!=999 or pitch!=999:
            if 'look_angles' not in parse_capabilities(self.bridge.client_info.get('capabilities')):return '自由视角需要0.4.1客户端'
            if self._active_task is not None:return '正在执行动作，暂不改变摄像头'
            if not math.isfinite(float(yaw)+float(pitch)):return '视角参数无效'
            if yaw==999 or pitch==999:
                current=await self._call('sense',timeout=10)
                if self._err(current):return self._err(current)
                if yaw==999:yaw=float(current['yaw'])
                if pitch==999:pitch=float(current['pitch'])
            if abs(yaw)>360 or abs(pitch)>90:return '视角参数超出范围'
            result=await self._call('look',{'yaw':float(yaw),'pitch':float(pitch)},timeout=10)
            return self._err(result) or json.dumps(result,ensure_ascii=False)
        player = str(player or "").strip()
        if not player:
            data = await self._call("sense", timeout=10)
            return self._err(data) or json.dumps(data, ensure_ascii=False)
        data = await self._call("look", {"player": player}, timeout=10)
        err = self._err(data)
        return err or f"已看向 {player}"

    @filter.llm_tool(name="mc_look_at")
    @require_mc_tool("mc_look_at")
    async def mc_look_at(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """把视角对准指定方块坐标，不移动角色。

        Args:
            x(int): 方块X坐标。
            y(int): 方块Y坐标。
            z(int): 方块Z坐标。
        """
        if self._active_task is not None:
            return "正在执行动作，暂不改变摄像头"
        data = await self._call("look", {"x": int(x), "y": int(y), "z": int(z)}, timeout=10)
        err = self._err(data)
        return err or json.dumps(data, ensure_ascii=False)

    @filter.llm_tool(name="mc_inspect_target")
    @require_mc_tool("mc_inspect_target")
    async def mc_inspect_target(self, event: AstrMessageEvent):
        """读取当前准星指向的方块/实体和同帧视角信息；不移动角色。"""
        data = await self._call("sense", timeout=10)
        return self._err(data) or json.dumps(data, ensure_ascii=False)

    @filter.llm_tool(name="mc_come")
    @require_mc_tool("mc_come")
    async def mc_come(self, event: AstrMessageEvent, player: str = ""):
        """加入走向指定玩家的受监控任务，目标需要在64格内。

        Args:
            player(string): 玩家名；留空使用主人名。
        """
        player = str(player or self.config.get("owner_name", "Player")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player):
            return "玩家名无效，未执行"
        return "已加入走向玩家的任务，尚未确认到达" if self._enqueue_tasks([QueuedTask("come", "去 " + player, target=player)]) else "队列已满，未加入"

    @filter.llm_tool(name="mc_return_to")
    @require_mc_tool("mc_return_to")
    async def mc_return_to(self, event: AstrMessageEvent, x: int, y: int, z: int, reach: float = 2.0):
        """导航到已知坐标并校验实际到达距离。

        Args:
            x(int): 目标X坐标。
            y(int): 目标Y坐标。
            z(int): 目标Z坐标。
            reach(float): 可接受距离，0.5-8。
        """
        reach = max(0.5, min(8.0, float(reach)))
        if self._queue_skill(
            f"返回坐标 {x} {y} {z}",
            "return_to",
            target_pos=[int(x), int(y), int(z)],
            reach=reach,
        ):
            return "已加入返回坐标任务，完成后会确认到达距离"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_attack")
    @require_mc_tool("mc_attack")
    async def mc_attack(self, event: AstrMessageEvent, target: str = ""):
        """攻击附近的敌对生物（僵尸、骷髅、苦力怕等，不会攻击玩家）。

        Args:
            target(string): 可选的生物名或类型；留空攻击最近的敌对生物。
        """
        target_low = (target or "").strip().lower()
        await self._select_best_weapon()
        for _ in range(3):
            status = await self._call("status", timeout=15)
            err = self._err(status)
            if err:
                return err
            hostiles = status.get("hostiles") or []
            if not hostiles:
                return "附近没有敌对生物"
            nearest = None
            for hostile in hostiles:
                if not isinstance(hostile, dict):
                    continue
                name = str(hostile.get("name", "")).lower()
                kind = str(hostile.get("type", "")).lower()
                if target_low and target_low not in name and target_low not in kind:
                    continue
                if nearest is None or float(hostile.get("distance", 999)) < float(
                    nearest.get("distance", 999)
                ):
                    nearest = hostile
            if nearest is None:
                return f"附近没有目标：{target}"
            try:
                dist = float(nearest.get("distance", 999))
            except (TypeError, ValueError):
                dist = 999.0
            if dist <= 3.0:
                break
            pos = nearest.get("position") or {}
            try:
                tx = int(float(pos.get("x", 0)))
                ty = int(float(pos.get("y", 64)))
                tz = int(float(pos.get("z", 0)))
            except (TypeError, ValueError):
                return "敌人坐标异常"
            await self._call(
                "baritone", {"command": f"goto {tx} {ty} {tz}"}, timeout=10
            )
            await asyncio.sleep(2.5)
        else:
            return "追不上目标"

        last = None
        for _ in range(6):
            data = await self._call("attack", {"target": target}, timeout=10)
            err = self._err(data)
            if err:
                return err
            last = data
            if data.get("dead"):
                return f"已击杀 {data.get('target')}"
            await asyncio.sleep(0.7)
        if last:
            return f"攻击了 {last.get('target')}，剩 {last.get('health')} 血，还没打死"
        return "没有攻击目标"

    @filter.llm_tool(name="mc_eat")
    @require_mc_tool("mc_eat")
    async def mc_eat(self, event: AstrMessageEvent):
        """将背包里的安全食物移入快捷栏并吃下；确认实际饱食变化。"""
        before = await self._call("status", timeout=10)
        if self._err(before):
            return self._err(before)
        if float(before.get("food", 20)) >= 20:
            return "当前饱食已满，不额外消耗食物"
        if self.bridge.client_info.get("protocol", 1) >= 3:
            try:
                await self._run_operation("inventory", {"operation": "food"}, seconds=5)
            except SurvivalError as exc:
                return "未能准备食物：" + str(exc)
        result = await self._call("eat", timeout=10)
        if self._err(result):
            return self._err(result)
        duration=max(2.6,min(11,float(result.get("eat_ms",1600))/1000+0.6))
        self._eating_until = time.monotonic() + duration + 0.5
        await asyncio.sleep(duration)
        self._eating_until = 0.0
        after = await self._call("status", timeout=10)
        if self._err(after):
            return "已经发出进食操作，但状态查询失败，未确认吃完"
        if float(after.get("food", 0)) <= float(before.get("food", 0)):
            return "已经尝试进食，但饱食度未增加，未确认成功"
        return f"吃完 {result.get('item', '食物')}，当前饱食 {float(after['food']) / 2:.1f}/10 格"

    @filter.llm_tool(name="mc_eat_until_full")
    @require_mc_tool("mc_eat_until_full")
    async def mc_eat_until_full(self, event: AstrMessageEvent):
        """反复吃安全食物直到饱食满，最多 8 次且有总时限。"""
        try:
            result = await self._skills_for().eat_until_full()
        except SkillError as exc:
            return "进食未完成：" + str(exc)
        return json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="mc_flee")
    @require_mc_tool("mc_flee")
    async def mc_flee(self, event: AstrMessageEvent):
        """中断当前任务，远离最近的敌对生物；返回撤离请求结果。"""
        status = await self._call("status", timeout=10)
        if self._err(status):
            return self._err(status)
        hostile = min((h for h in status.get("hostiles", []) if isinstance(h, dict)),
                      key=lambda h: float(h.get("distance", 999)), default=None)
        if hostile is None:
            return "附近没有敌对生物，不用跑"
        await self._stop_task_runtime()
        token = CONTROL_EPOCH.set(self._control_epoch)
        try:
            outcome = await self._auto_flee(status, hostile)
            event.set_extra("_mc_emergency_outcome", outcome)
            return outcome
        finally:
            CONTROL_EPOCH.reset(token)

    @filter.llm_tool(name="mc_screenshot")
    @require_mc_tool("mc_screenshot")
    async def mc_screenshot(self, event: AstrMessageEvent):
        """保存当前第一视角截图；让模型看画面请用 mc_see。"""
        try:
            path, _ = await self._capture()
            return f"截图已保存：{path}"
        except Exception as exc:
            return f"截图失败：{exc}"

    @staticmethod
    def _vision_question_allowed(question: str) -> bool:
        text = str(question or "")
        if re.search(r"画面|截图|颜色|外观|长什么样|什么样子|长相|穿着|表情|动作|风景|视野", text):
            return True
        if re.search(r"坐标|在哪|哪里|位置|多远|几格|路|怎么走|楼梯|梯子|门|方块|床|附魔台|工作台|箱子|屏幕|提示|进度|到达", text):
            return False
        return True

    @filter.llm_tool(name="mc_see")
    @require_mc_tool("mc_see")
    async def mc_see(self, event: AstrMessageEvent, question: str = ""):
        """查看当前第一视角画面。

        Args:
            question(string): 希望观察的问题，例如前面有什么。
        """
        if not self._vision_question_allowed(question):
            return "这类坐标/路线/方块/进度问题请先用 mc_scan、mc_block_context、mc_inspect_block 或 mc_task_status；本轮不调用视觉模型。"
        provider = str(self.config.get("vision_provider_id", "")).strip()
        if not provider:
            return "没有配置看图模型"
        try:
            _, encoded, observation = await self._capture(with_observation=True)
            target=observation.get('crosshair',{}).get('id','')
            if target and not target.startswith('minecraft:') and 'knowledge' in parse_capabilities(self.bridge.client_info.get('capabilities')):
                detail=await self._call('knowledge',{'kind':'items','query':target,'limit':1},timeout=10)
                if not self._err(detail) and detail.get('results'):
                    row=detail['results'][0]
                    observation['target_details']={k:row[k] for k in ('id','name','description') if k in row}
            prompt,images=vision_input(encoded,question,observation)
            trace=CURRENT_TRACE.get()
            standalone=trace is None
            if standalone:trace=Trace(getattr(self,'_telemetry',None),'vision_tool',str(getattr(event,'unified_msg_origin','minecraft:vision')))
            ctx=observed_context(self.context,trace,kind='vision') if trace.store is not None else self.context
            result = await asyncio.wait_for(ctx.llm_generate(
                chat_provider_id=provider,
                prompt=prompt,
                image_urls=images,
            ), timeout=30)
            if standalone:await trace.finish()
            return str(getattr(result, "completion_text", "") or "没有得到可确认的画面结果")
        except BaseException as exc:
            if locals().get('standalone') and locals().get('trace') is not None:await trace.finish('error',exc)
            if isinstance(exc,asyncio.CancelledError):raise
            return "看图未完成：" + type(exc).__name__

    # -------------------------------------------------------------- command

    @staticmethod
    def _stop_llm(event: AstrMessageEvent) -> None:
        try:
            event.should_call_llm(False)
        except Exception:
            pass
        try:
            event.stop_event()
        except Exception:
            pass

    @staticmethod
    def _mc_session_key(event):
        return str(getattr(event, 'unified_msg_origin', '') or (event.get_platform_name() + ':' + str(getattr(event, 'sender_id', ''))))

    def _mc_history(self, event):
        key = self._mc_session_key(event)
        histories = getattr(self, '_mc_histories', None)
        if histories is None:
            histories = {}
            stored = self._short_history_for().items()
            for stored_key, lines in stored.items():
                safe_lines = [redact_text(line) for line in lines]
                histories[stored_key] = deque(safe_lines, maxlen=20)
                if safe_lines != list(lines):
                    try:
                        self._short_history_for().set(stored_key, safe_lines)
                    except Exception:
                        pass
            self._mc_histories = histories
        history = histories.get(key)
        if history is None:
            history = deque(maxlen=20)
            histories[key] = history
        return key, history

    def _mc_history_text(self, event):
        key, history = self._mc_history(event)
        bounded = bound_history(history, history_budget(self.config))
        text = redact_text("\n".join(bounded))
        event.set_extra('_mc_history_tokens', estimate_tokens(text))
        event.set_extra('_mc_history_lines', len(bounded))
        return key, text

    def _mc_record_history(self, key, user, reply):
        histories = getattr(self, '_mc_histories', None)
        if histories is None:
            histories = {}
            stored = self._short_history_for().items()
            for stored_key, lines in stored.items():
                safe_lines = [redact_text(line) for line in lines]
                histories[stored_key] = deque(safe_lines, maxlen=20)
                if safe_lines != list(lines):
                    try:
                        self._short_history_for().set(stored_key, safe_lines)
                    except Exception:
                        pass
            self._mc_histories = histories
        history = histories.setdefault(key, deque(maxlen=20))
        history.extend([f"主人：{redact_text(user)}", f"你：{redact_text(reply)}"])
        bounded = bound_history(history, history_budget(self.config))
        history.clear()
        history.extend(bounded)
        try:
            self._short_history_for().set(key, list(history))
        except Exception:
            pass

    async def _agent_reply(
        self,
        event: AstrMessageEvent,
        prompt: str,
        exclude_tools: set[str] | None = None,
        allow_actions: bool = True,
    ) -> str:
        provider = str(self.config.get("mc_provider_id", "your-provider/chat-model")).strip()
        if not provider:
            return "没有配置 mc_provider_id"
        raw_query=str(event.message_str or '')
        pending=event.get_extra('_mc_pending_action')
        group_query=raw_query
        if allow_actions and (pending or getattr(self,'_task_current',None) is not None):
            group_query=raw_query+' '+str(pending or '')+' 继续任务'
        shared=getattr(self,'_shared_memory',None)
        owner=shared is not None and shared.owner_event(event,self.bridge)
        qq_owner=owner and shared.qq_owner(event)
        group,names=tool_group(group_query,allow_actions=allow_actions,owner=owner,qq_owner=qq_owner)
        if not allow_actions:names=(names-READONLY)|((READONLY & self._safe_tool_names()) | {'mc_brief'})
        tools=self._mc_toolset()
        tools.tools=[tool for tool in tools.tools if tool.name in names and tool.name not in (exclude_tools or set())]
        event.set_extra('_mc_selected_tools', {tool.name for tool in tools.tools})
        if tools.empty():return "mc_* 工具没有注册成功"
        source='diagnostic' if event.get_extra('_mc_observation_source','')=='diagnostic' else ('game' if event.get_platform_name()=='minecraft' else 'qq_mc')
        trace=Trace(getattr(self,'_telemetry',None),source,'minecraft:Diagnostic:context-fix' if source=='diagnostic' else str(getattr(event,'unified_msg_origin','minecraft:gamechat')))
        if event.get_extra('_mc_received_perf',None) is not None:
            trace.perf=event.get_extra('_mc_received_perf')
            trace.data['start_time']=event.get_extra('_mc_received_wall',trace.data['start_time'])
        trace.data['context']={'tool_group':group,'tools':sorted(t.name for t in tools.tools),'tool_schema_chars':len(json.dumps(tools.openai_schema(),ensure_ascii=False,separators=(',',':'))),'history_estimated_tokens':event.get_extra('_mc_history_tokens',0),'history_lines':event.get_extra('_mc_history_lines',0),'history_token_budget':history_budget(self.config)}
        event.set_extra('_mc_trace',trace)
        epoch=self._control_epoch
        event.set_extra('_mc_control_epoch',epoch)
        current=asyncio.current_task();self._agent_jobs.add(current)
        reply_token=REPLY_ROUTE.set(event_route(event,self.bridge))
        if not hasattr(self,'_agent_notice_routes'):self._agent_notice_routes={}
        self._agent_notice_routes[current]=REPLY_ROUTE.get()
        control_token=None;trace_token=None
        try:
            async with self._agent_lock:
                trace.data['phases']['queue_seconds']=trace.elapsed()
                if self._closing or epoch!=self._control_epoch:
                    await trace.finish('stale_queued')
                    return "这次请求已被停止，未执行旧动作"
                if source=='game' and event.get_extra('_mc_connection_id',None)!=self.bridge.session_id:
                    await trace.finish('stale_connection')
                    return "游戏连接已变化，旧请求未执行"
                self._active_agent_task=current
                control_token=CONTROL_EPOCH.set(epoch);trace_token=CURRENT_TRACE.set(trace)
                memory_parts=[];start=time.monotonic()
                # Recall uses this message, never the flattened historical prompt.
                trace.data['context']['memory_requested']=bool(shared is not None and owner and memory_needed(raw_query))
                if trace.data['context']['memory_requested']:
                    memories=await shared.context_for(event,self.bridge,raw_query)
                    if memories:memory_parts.append(TextPart(text=memories).mark_as_temp())
                trace.data['phases']['memory_seconds']=round(time.monotonic()-start,6)
                trace.data['context']['memory_chars']=sum(len(p.text) for p in memory_parts)
                persona=self._persona_prompt()
                if self._tool_profile() in {"simple", "compact"}:
                    system_prompt = persona + self._simple_system_prompt(allow_actions, owner)
                else:
                    system_prompt=persona+(
                        "\n\n你现在通过 Minecraft 客户端账号 MinecraftBot 行动。"
                        "你可以用 mc_* 原语工具观察、移动、跟随、截图、挖掘、放置、开关容器、交易、骑乘、搬运、拾取和合成。"
                        "技能工具：mc_gather 采集、mc_craft 合成、mc_smelt 烧炼、mc_container_transfer 搬运、mc_build 建造、mc_farm 农业、mc_sleep 睡觉、mc_trade_for 村民交易、mc_combat 战斗、mc_explore 探索、mc_resupply 补给。技能会进入任务队列，完成后才通知；不要把“已加入队列”当成完成。"
                        "砍树/挖矿/收小麦优先 mc_gather；需要工具/材料优先 mc_craft；容器来回搬运优先 mc_container_transfer，不要连续手点。"
                        "mc_recipes查当前服务器同步的真实配方/物品/客户端模组；不要编造模组用法。"
                        "mc_interact只用于门、按钮、拉杆等交互。"
                        f"主人在游戏中的默认名字是 {self.config.get('owner_name','Player')}，"
                        "主人说跟着我或看着我时使用这个名字。"
                        "需要移动或跟随时优先用 mc_goto / mc_follow / mc_stop，"
                        "玩家说“来我这边/过来”时用 mc_come，并把当前游戏玩家名作为 player；"
                        "不要声称自己做了没有调用工具的事。已加入/已开始不等于完成，禁止据此声称已经到达或完成任务。"
                        "闲聊直接回答；实际操作以本轮提供的工具为准，不编造执行结果。"
                        "查单个坐标/方块时优先用 mc_inspect_block，不要先 mc_goto，也不要连续 mc_scan。"
                        "mc_scan只做区域概览；单点查询不要用 scan，扫描结果多时只取需要的最近几项。"
                        "拆/打掉方块用 mc_mine_at；只有门、按钮、拉杆等交互才用 mc_interact，不要用 mc_interact 拆火把或方块。向下竖井用 mc_dig_down，不做自动封顶/出井。"
                        "mc_see只描述画面，不能提供精确坐标；需要坐标用 mc_scan/mc_inspect_block，需要对准坐标用 mc_look_at，读取准星用 mc_inspect_target。"
                        "坐标、方块、菜单、任务进度、是否到达、重生点验证优先用 mc_status/mc_scan/mc_block_context/mc_inspect_block/mc_get_menu/mc_task_status；除非主人明确要求看画面、颜色、外观，或坐标工具确实无法判断，否则不要调用 mc_see/mc_screenshot。"
                        "禁止用 mc_see 代替 mc_block_context 选床、选附魔台或确认工具是否成功；禁止用截图文字作为任务完成凭证。"
                        "mc_mine_at/mc_place_at 超距时会自动靠近；不要先 mc_goto 再立刻重试，也不要在同一轮用 mc_stop 清路后继续动作。"
                        "mc_goto 会自动把不可站立方块改成最近站位；mc_align 只用于最后 1.5 格微调，不要用 mc_align 做长距离移动。"
                        "被方块挡住、主人说挖过去/上去/下去时，可以给 mc_goto 传 allow_break=true，让本次移动掘进穿过方块；普通 mc_goto 在确认无路时也会自动尝试一次掘进。"
                        "mc_pickup 会自动尝试接近掉落物；如果掉落物被石头卡住，会再用 allow_break 掘进接近后重试拾取。"
                        "整理背包优先用 mc_inventory(operation=sort)，不要连续多次 swap。"
                        "箱子/熔炉搬运优先用 mc_container_transfer；多种物品一次传 items（JSON数组或 item:count 逗号分隔），技能只开一次容器；不要每种物品单独调用或并行排队。mc_move_item 的 count 会一次搬多个，容器已打开时不必重复开关。"
                        "村民交易用 mc_trade(open/read/trade/close)，按报价 index 操作；不要用 mc_use_item 猜交易结果。"
                        "骑船/矿车/马用 mc_ride(action=ride)，下车用 mc_ride(action=dismount)；喂食/拴绳等实体交互用 mc_use_on_entity。"
                        "起床用 mc_wake；交易、骑乘、搬运都要以工具返回为准，不要编造成功。"
                        "设置重生点/家床必须调用 mc_set_respawn；它会验证床并写入 home_bed。只有 saved=true 才能说“记住了/设置好了”。"
                        "mc_see 的截图文字只能辅助，不能替代 mc_set_respawn 或 mc_memory 的成功结果；不要仅凭看到“已设置重生点”就声称自己完成了。"
                        "用户指床但没给坐标时，先用 mc_block_context 确认床坐标，再调用 mc_set_respawn；不要用 mc_see 的推测坐标写记忆。"
                        "mc_set_respawn 会自己走到床边；不要用 mc_goto + mc_wait 拼设床，也不要在床不可见时直接硬点。"
                        "明确要求“设重生点”属于已授权动作，不需要再次征求批准；一次不可达时在安全范围内换同一张床的另一半站位或重试一次，仍失败再报告具体原因。"
                        "任务执行中查询进度用 mc_task_status，等待用 mc_wait；这两个工具不会被当前任务阻塞。"
                        "状态/任务/家床一起查优先用 mc_brief，一次拿全，不要分开调用 mc_status + mc_task_status + mc_memory；独立的只读查询可以在同一轮一起发起，但移动/挖掘/放置/停止等有依赖的动作必须等前一个结果。"
                        "短任务会由 mc_task_start 在 short_task_wait_seconds 内直接返回结果（inline=true）；只有 inline=false 的长任务才回复已提交并等待通知，不要连续 mc_wait/mc_task_status 轮询。mc_wait 仍可用于手动等待。"
                        "主人明确说去某处/下楼/去房间时，先用 mc_status 确认位置（最多一次），然后直接 mc_goto 到已知坐标或 place_ref；不要为了找路先连续 mc_scan 门/梯子/楼梯，路线由 Baritone 负责。"
                        "路线、门、梯子、楼梯、坐标类问题禁止调用 mc_see；它会被直接拦截并浪费一轮，直接用 mc_scan/mc_block_context。"
                        "说“记下了/记住了”必须对应 mc_set_respawn 或 mc_memory(remember/verify) 的写入成功，不能只凭上下文。"
                        "不要在 mc_memory 参数里传坐标；记住附近地点用 operation=remember，精确坐标由本地卡片补充。"
                        "你是第一视角玩家，不要在截图里找自己；用户说刚刚/对的/继续时先参考短期历史，没有上下文就明确问。"
                    )
                if not allow_actions:
                    system_prompt+=("\n本轮是回忆/任务期间聊天，只能查看或回忆，不执行新的游戏动作。" if owner else "\n当前说话的人不是主人，只能聊天和查看状态/画面；动作需要主人下令。")
                elif owner:
                    coach = getattr(self, "_coach", None) or self._coach_for()
                    if coach is not None:
                        playbook_block, lesson_ids = coach.format_playbook_prompt()
                        if playbook_block:
                            system_prompt += playbook_block
                        self._last_injected_lesson_ids = lesson_ids
                trace.data['context'].update(persona_chars=len(persona),persona_sha256=hashlib.sha256(persona.encode()).hexdigest(),system_chars=len(system_prompt),prompt_estimated_tokens=estimate_tokens(prompt))
                ctx=observed_context(self.context,trace) if trace.store is not None else self.context
                start=time.monotonic()
                resp=await asyncio.wait_for(ctx.tool_loop_agent(event=event,chat_provider_id=provider,prompt=prompt,tools=tools,extra_user_content_parts=memory_parts,system_prompt=system_prompt,max_steps=16,tool_call_timeout=30,stream=True,agent_hooks=ToolHooks(trace)),timeout=120)
                trace.data['phases']['agent_seconds']=round(time.monotonic()-start,6)
                if epoch!=self._control_epoch:
                    await trace.finish('interrupted')
                    return event.get_extra('_mc_emergency_outcome','') or "这次操作已被停止或紧急避险中断"
                await trace.finish()
                return str(getattr(resp,'completion_text','') or '').strip() or "没有返回可确认的结果"
        except asyncio.TimeoutError as ex:
            await trace.finish('timeout',ex)
            await self._interrupt_actions()
            return "Minecraft 请求处理超时，已停止相关动作"
        except BaseException as ex:
            await trace.finish('cancelled' if isinstance(ex,asyncio.CancelledError) else 'error',ex)
            raise
        finally:
            self._agent_jobs.discard(current)
            self._agent_notice_routes.pop(current,None)
            if getattr(self,'_active_agent_task',None) is current:self._active_agent_task=None
            REPLY_ROUTE.reset(reply_token)
            if trace_token is not None:CURRENT_TRACE.reset(trace_token)
            if control_token is not None:CONTROL_EPOCH.reset(control_token)


    async def _capture(self,with_observation=False):
        data = await self._call("screenshot", timeout=30)
        if self._err(data):
            raise BridgeError(self._err(data))
        encoded = str(data.get("base64", ""))
        limit = max(1, min(16, int(self.config.get("max_screenshot_mb", 8)))) * 1024 * 1024
        if len(encoded) > (limit + 2) // 3 * 4:
            raise BridgeError("截图超过大小限制")
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > limit:
            raise BridgeError("截图为空或超过大小限制")
        folder = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        files = sorted((p for p in folder.glob("mc-*.png") if p.is_file() and not p.is_symlink()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        keep = max(1, min(100, int(self.config.get("screenshot_keep_count", 20))))
        for old in files[keep - 1:]:
            old.unlink()
        path = folder / f"mc-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.png"
        path.write_bytes(raw)
        return (path,encoded,data.get('observation',{})) if with_observation else (path, encoded)

    def _help_text(self) -> str:
        return (
            "私聊直接说，不用 /：\n"
            "mc你现在在哪，血量多少\n"
            "mc跟着我 / mc来我这边 / mc去坐标100 64 -200\n"
            "mc看看前面有什么\n"
            "快捷操作：mcstatus 查状态、mcstop 停止、mcshot 截图、mccoach 教练经验。"
        )

    def _handle_coach_command(self, args: str) -> str:
        coach = self._coach_for()
        if coach is None:
            return "Coach 教练学习未启用（可在配置中开启 coach_enabled）"
        parts = str(args or "").strip().split(None, 2)
        if not parts:
            return coach.format_digest()
        cmd = parts[0].lower()
        if cmd == "approve" and len(parts) >= 2 and parts[1].isdigit():
            ok = coach.store.approve_lesson(int(parts[1]))
            return f"已转正经验 #{parts[1]}" if ok else f"未找到经验 #{parts[1]}"
        if cmd == "retire" and len(parts) >= 2 and parts[1].isdigit():
            ok = coach.store.retire_lesson(int(parts[1]))
            return f"已废弃经验 #{parts[1]}" if ok else f"未找到经验 #{parts[1]}"
        if cmd == "teach" and len(parts) >= 3:
            lid = coach.record_owner_feedback(parts[1], parts[2])
            return f"已记入生效经验 #{lid}（scope={parts[1]}）" if lid else "内容无效或包含坐标，未写入"
        if cmd == "bugs":
            bugs = coach.store.list_code_issues(status="open", limit=10)
            if not bugs:
                return "当前没有待处理的代码/环境问题。"
            return "\n".join(f"#{b['id']} [{b['scope']} x{b['occurrences']}] {b['error_summary']}" for b in bugs)
        return coach.format_digest()

    @staticmethod
    def _parse_input(text: str) -> tuple[str, str] | None:
        """Recognize short operations and MC prose without taking English words."""
        text = text.strip()
        low = text.lower()
        shortcuts = {
            "mc": "help",
            "mchelp": "help",
            "mcstatus": "status",
            "mcstop": "stop",
            "mcshot": "shot",
            "mccoach": "coach",
        }
        if low in shortcuts:
            return shortcuts[low], ""
        if low.startswith("mccoach "):
            return "coach", text[7:].strip()
        if not re.match(r"(?i)^mc(?![a-z0-9_])", text):
            return None
        prompt = text[2:].lstrip(" \t\r\n:：,，")
        if prompt.startswith(("/", "／")):
            return None
        return ("agent", prompt) if prompt else ("help", "")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def mc_short_commands(self, event: AstrMessageEvent):
        """Private MC shortcuts and natural language, before other handlers and sleep."""
        if not event.is_private_chat():
            return
        parsed = self._parse_input(str(event.message_str or ""))
        if parsed is None:
            return
        route=event_route(event,self.bridge)
        action, prompt = parsed
        self._stop_llm(event)
        logger.info("[mc-bridge] private request: action=%s", action)
        if bool(self.config.get("qq_admin_only", True)):
            try:
                is_admin = bool(event.is_admin())
            except Exception:
                is_admin = getattr(event, "role", "") == "admin"
            if not is_admin:
                yield event.plain_result("只有主人能控制 Minecraft 机器人。")
                return
        shared = getattr(self, "_shared_memory", None)
        if shared is not None:
            shared.prepare_qq(event)
        if action == "agent" and not self.bridge.connected:
            if shared is not None and re.search(r"记得|记忆|回忆|聊过|昨天|前天|忘记", prompt):
                reply = await self._agent_reply(event, prompt)
                yield event.plain_result(self._reply_with_place_cards(event, reply))
                return
            yield event.plain_result("Minecraft 客户端未连接，请先启动游戏客户端")
            return
        task_like = not self._is_memory_query(prompt) and bool(
            action == "agent"
            and (
                self._queue_requested(prompt)
                or "然后" in prompt
                or re.search(
                    r"(来我这边|过来|到我这|来我这|去坐标|前往|goto|去\s*[A-Za-z0-9_]{2,16}\s*那边)",
                    prompt,
                    re.I,
                )
            )
        )
        if action == "agent" and self._active_task is not None and not self._active_task.done():
            if not self._is_memory_query(prompt) and self._is_stop_request(prompt):
                await self._interrupt_actions(requester_route=route)
                yield event.plain_result("已取消任务")
                return
            if task_like:
                tasks, unparsed = self._parse_tasks_ex(prompt, "")
                if tasks and not unparsed:
                    accepted = self._enqueue_routed(tasks,route)
                    yield event.plain_result(f"已加入队列（{len(tasks)} 项）" if accepted else "任务队列已满，未加入")
                    return
                if unparsed:
                    yield event.plain_result("这条复合指令没有全部解析，当前任务进行中，未执行；请等任务结束或拆开说")
                    return
                if self._queue_requested(prompt):
                    yield event.plain_result(self._task_status_text())
                    return
                return
            if self._is_memory_query(prompt):
                reply = await self._agent_reply(event, prompt, allow_actions=False)
                yield event.plain_result(self._reply_with_place_cards(event, reply))
                return
            control_like = bool(re.search(
                r"mc_task_cancel|mc_task_status|mc_task_start|mc_brief|任务状态|任务进度|任务队列|终止任务|结束任务|看看状态|查任务",
                prompt,
                re.I,
            ))
            if control_like:
                # Read-only/control/queue requests may still reach the Agent; immediate
                # actions remain filtered by _tool_denial while the task is active.
                pass
            else:
                yield event.plain_result("任务进行中，先发 mcstop 再换动作")
                return
        if action == "agent" and task_like:
            tasks, unparsed = self._parse_tasks_ex(prompt, "")
            if tasks and not unparsed:
                accepted = self._enqueue_routed(tasks,route)
                yield event.plain_result(f"开始任务（{len(tasks)} 项）" if accepted else "任务队列已满，未加入")
                return
            if unparsed:
                # Hand the whole compound sentence to the planner instead of dropping steps.
                pass
            elif self._queue_requested(prompt):
                yield event.plain_result(self._task_status_text())
                return
        if action == "help":
            yield event.plain_result(self._help_text())
            return
        if action == "status":
            data = await self._call("status", timeout=15)
            yield event.plain_result(self._err(data) or _fmt_status(data))
            return
        if action == "stop":
            error = await self._interrupt_actions(requester_route=route)
            yield event.plain_result(error or "停止指令已确认（client_stop_confirmed=true），任务队列已清空")
            return
        if action == "shot":
            try:
                path, _ = await self._capture()
            except Exception as exc:
                logger.warning("[mc-bridge] screenshot failed: %s", exc)
                yield event.plain_result("截图失败：" + str(exc))
                return
            yield event.image_result(str(path))
            return
        if action == "coach":
            yield event.plain_result(self._handle_coach_command(prompt))
            return
        key = self._mc_session_key(event)
        if not hasattr(self,'_pending_actions'): self._pending_actions={}
        pending = self._pending_actions.get(key)
        confirmed = is_confirmation(prompt)
        if pending and confirmed:
            event.set_extra('_mc_pending_action', pending)
            pending_text = f"上一轮待确认的MC请求：{pending}\n主人现在确认："
        else:
            pending_text = ""
        if action_intent(prompt) and not confirmed:
            self._pending_actions[key] = prompt
        key, history_text = self._mc_history_text(event)
        agent_prompt = (
            (f"最近的MC私聊：\n{history_text}\n\n" if history_text else "")
            + pending_text
            + f"主人说：{prompt}\n\n"
            "如果只是聊天就直接回答；如果主人要求动作，调用对应的 mc_* 工具后再简短汇报。"
        )
        try:
            reply = await self._agent_reply(event, agent_prompt)
            self._mc_record_history(key, prompt, reply)
            if (confirmed or action_intent(prompt)) and not re.search(r'[?？]|吗|还是|是否|哪|需不需要|要不要', reply):
                self._pending_actions.pop(key, None)
        except Exception as exc:
            logger.warning("[mc-bridge] mc agent failed: %s", exc)
            reply = "MC 请求未完成：" + str(exc)
        yield event.plain_result(self._reply_with_place_cards(event, reply))

    def _tool_denial(self, event, name):
        if name in {"mc_recall_memory", "mc_forget_memory"}:
            shared = getattr(self, "_shared_memory", None)
            if self._closing or shared is None:
                return "MC共享记忆尚未就绪"
            if CONTROL_EPOCH.get() not in (None, self._control_epoch):
                return "旧请求已中断，未读取共享记忆"
            if name == "mc_recall_memory" and self._audience(event).startswith("group:"):
                return shared.group_denial(event, self.bridge)
            return shared.denial(event, self.bridge, forget=name == "mc_forget_memory")
        if self._closing:
            return "Minecraft 插件正在停止，未执行"
        text = str(event.message_str or "") if event is not None else ""
        recall_question = is_memory_question(text)
        if name not in {"mc_status", "mc_look", "mc_see", "mc_screenshot", "mc_memory", "mc_brief", "mc_recall_memory", "mc_task_status", "mc_capabilities"} and event is not None and self._is_memory_query(text) and (not action_intent(text) or recall_question):
            return "本轮是回忆，不执行新的游戏动作"
        if not self.bridge.connected and name not in {"mc_stop", "mc_status", "mc_screenshot", "mc_see", "mc_memory", "mc_recall_memory"}:
            return "Minecraft 客户端未连接，未执行"
        epoch = CONTROL_EPOCH.get()
        if epoch is not None and epoch != self._control_epoch:
            return "这次操作已被停止或紧急避险中断，未执行"
        if self._active_task is not None and not self._active_task.done() and name not in {
            "mc_stop", "mc_flee", "mc_status", "mc_look", "mc_see", "mc_screenshot", "mc_recipes",
            "mc_scan", "mc_inspect_block", "mc_inspect_target", "mc_wake",
            "mc_task_status", "mc_task_start", "mc_task_cancel", "mc_wait", "mc_capabilities",
            "mc_brief", "mc_block_context", "mc_get_menu", "mc_memory", "mc_recall_memory"
        }:
            return "任务正在执行，请先停止再切换操作，未执行"
        if event is None:
            return "没有可信会话，未执行"
        try:
            platform = event.get_platform_name()
            private = event.is_private_chat()
        except Exception:
            return "无法确认会话来源，未执行"
        if platform == "minecraft":
            if event.get_extra("_mc_connection_id", None) != self.bridge.session_id:
                return "游戏连接已变更，旧请求未执行"
            sender = event.get_extra("_mc_trusted_player", "")
            identity = event.get_extra("_mc_sender_uuid", "")
            if not sender or not identity:
                return "游戏消息身份未验证，未执行"
            safe = {"mc_status", "mc_look", "mc_see", "mc_screenshot", "mc_capabilities"}
            if name in safe & self._safe_tool_names() or name == "mc_capabilities":
                return None
            if self._player_allowed(sender):
                return "游戏公共聊天只允许只读状态查询；动作和私有记忆请在主人私聊中下达"
            return "这项动作需要主人下令，未执行"
        if platform == "aiocqhttp" and not private:
            audience = self._audience(event)
            if not audience.startswith("group:"):
                return "这个群没有开放MC只读记忆，未执行"
            if name in {"mc_memory", "mc_brief", "mc_status", "mc_task_status",
                        "mc_capabilities", "mc_recall_memory"}:
                return None
            return "白名单群为永久只读：只能查询共享状态和记忆，不能写入或控制游戏"
        if platform not in {"aiocqhttp", "webchat"} or not private:
            return "Minecraft 工具仅在授权私聊或可信游戏会话中使用，未执行"
        if platform == "aiocqhttp":
            owner_qq = str(self.config.get("shared_memory_owner_qq", "")).strip()
            if owner_qq:
                try:
                    sender = str(event.get_sender_id())
                except Exception:
                    sender = ""
                if sender != owner_qq:
                    return "只有主人 QQ 私聊能控制 Minecraft 机器人，未执行"
        if self.config.get("qq_admin_only", True) and not event.is_admin():
            return "只有主人能控制 Minecraft 机器人，未执行"
        text = str(getattr(event, "message_str", ""))
        if not (self._parse_input(text) or re.search(r"Minecraft|我的世界|(?<![a-zA-Z0-9_])mc(?![a-zA-Z0-9_])", text, re.I)):
            return "本轮未提出 Minecraft 请求，未执行"
        return None

    @staticmethod
    def _replace_prompt_text(value, needle, replacement):
        pattern = re.compile(rf"(?<!\d){re.escape(needle)}(?!\d)")
        if isinstance(value, str):
            return pattern.sub(replacement, value)
        if isinstance(value, list):
            return [McBridgePlugin._replace_prompt_text(item, needle, replacement) for item in value]
        if isinstance(value, dict):
            return {key: McBridgePlugin._replace_prompt_text(item, needle, replacement)
                    for key, item in value.items()}
        text = getattr(value, "text", None)
        if isinstance(text, str) and pattern.search(text):
            try:
                value.text = pattern.sub(replacement, text)
            except Exception:
                return value
        return value

    @filter.on_llm_request(priority=-10001)
    async def sanitize_prompt_identity(self, event, request):
        """Remove the owner QQ from prompt data and label the owner explicitly.

        The persona no longer carries a numeric owner id.  The platform still
        emits the sender id in quote lists and `<system_reminder>`, so replace
        it here and add a short, non-numeric identity marker for the owner.
        Non-owner prompts must not inherit the owner's numeric id from quoted
        history either.
        """
        owner_qq = str(self.config.get("shared_memory_owner_qq", "")).strip()
        if not owner_qq:
            return
        try:
            sender = str(event.get_sender_id())
        except Exception:
            sender = ""
        owner = sender == owner_qq
        label = "主人" if owner else "用户"
        system_prompt = str(getattr(request, "system_prompt", "") or "")
        if owner_qq in system_prompt:
            request.system_prompt = self._replace_prompt_text(system_prompt, owner_qq, label)
        contexts = getattr(request, "contexts", None)
        if isinstance(contexts, list):
            request.contexts = self._replace_prompt_text(contexts, owner_qq, label)
        parts = getattr(request, "extra_user_content_parts", None)
        if isinstance(parts, list):
            request.extra_user_content_parts = self._replace_prompt_text(parts, owner_qq, label)
        if owner:
            marker = "[应用层身份]"
            current = str(getattr(request, "system_prompt", "") or "")
            if marker not in current:
                request.system_prompt = current + f"\n\n{marker} 本轮发言者经可信身份确认是主人。"

    @filter.on_llm_request(priority=-10000)
    async def filter_mc_tools(self, event, request):
        original = getattr(request, "func_tool", None)
        if original is None:
            return
        allowed={tool.name for tool in original.tools if tool.name.startswith('mc_') and self._tool_denial(event,tool.name) is None}
        selected=event.get_extra('_mc_selected_tools', None)
        if selected:
            keep=set(selected) & allowed
            filtered = copy.copy(original)
            filtered.tools = [tool for tool in original.tools if not tool.name.startswith('mc_') or tool.name in keep]
            request.func_tool = filtered
            return
        shared=getattr(self,'_shared_memory',None)
        owner=shared is not None and shared.owner_event(event,self.bridge)
        qq_owner=owner and shared.qq_owner(event)
        pending=event.get_extra('_mc_pending_action')
        query=str(getattr(event,'message_str',''))
        if pending:query+=' '+str(pending)
        if getattr(self,'_task_current',None) is not None:query+=' 继续任务'
        _,profile=tool_group(query,allow_actions=bool(allowed & (set(SURVIVAL_TOOL_NAMES)-set(READONLY))),owner=owner,qq_owner=qq_owner)
        filtered = copy.copy(original)
        filtered.tools = [tool for tool in original.tools if not tool.name.startswith('mc_') or tool.name in allowed & profile]
        request.func_tool = filtered

    async def _send_stop(self):
        if not self.bridge.connected:
            return "客户端未连接；云端任务已取消，未确认客户端停止"
        try:
            async with self._action_lock:
                await self.bridge.call("baritone", {"command": "stop"}, timeout=5)
        except Exception:
            return "停止请求未送达；云端任务已取消，未确认客户端停止"
        for _ in range(5):
            await asyncio.sleep(0.12)
            status = await self._call("status", timeout=5)
            if self._err(status):
                continue
            if status.get("survival_busy") is False:
                return None
        return "停止请求已送达，但客户端仍报告动作忙碌，未确认停止"

    async def _stop_task_runtime(self):
        """Stop the background task worker and confirm the client is idle.

        This intentionally does not cancel the current Agent tool-loop task; tools
        such as mc_task_cancel/mc_stop are invoked from inside that loop.
        """
        self._cancel_active_task()
        return await self._send_stop()

    async def _interrupt_actions(self,reason='',*,requester_route=UNSET,automatic=False):
        affected=self._notice_routes()
        requester=REPLY_ROUTE.get() if requester_route is UNSET else requester_route
        self._control_epoch+=1;self._eating_until=0.0
        self._manual_movement=False;self._manual_notice_route=None
        task=self._active_task;self._cancel_active_task()
        candidates=(task,*self._agent_jobs)
        jobs={job for job in candidates if job is not None and job is not asyncio.current_task()}
        for job in jobs:job.cancel()
        if jobs:await asyncio.gather(*jobs,return_exceptions=True)
        error=await self._send_stop()
        if not self._closing:
            if automatic:
                for destination in affected or ([requester] if requester is not None else []):
                    await self._task_chat(reason if error is None else reason+'；'+error,route=destination,kind='cancel')
            else:
                if reason and requester is not None:
                    await self._task_chat(reason if error is None else reason+'；'+error,route=requester,kind='cancel')
                for destination in affected:
                    if destination!=requester:
                        await self._task_chat('你发起的MC任务已取消或中断，尚未完成的部分需要重新下达',route=destination,kind='cancel')
        return error

    async def _connection_lost(self):
        item = self._task_current
        interrupted = None
        if (
            item is not None
            and not self._closing
            and getattr(item, "kind", "") == "skill"
            and str(getattr(item, "target", "") or "") in {"mine_ore", "return_home", "tunnel_to", "gather", "plan"}
        ):
            interrupted = {
                "opcode": str(getattr(item, "target", "") or ""),
                "params": dict(getattr(item, "params", {}) or {}),
                "priority": int(getattr(item, "priority", 10) or 10),
                "route": getattr(item, "reply_route", None),
                "checkpoint": dict(getattr(item, "checkpoint", {}) or {}),
                "at": time.time(),
            }
        affected=self._notice_routes()
        self._control_epoch+=1
        self._manual_movement=False;self._manual_notice_route=None
        task=self._active_task;self._cancel_active_task()
        self._interrupted_task = interrupted
        jobs={job for job in (task,*self._agent_jobs) if job is not None and job is not asyncio.current_task()}
        for job in jobs:job.cancel()
        if jobs:await asyncio.gather(*jobs,return_exceptions=True)
        if not self._closing:
            auto = bool(self.config.get("auto_resume_after_disconnect", True))
            note = "重连后会尝试自动续跑" if auto else "重连后不会自动续跑"
            for destination in affected:
                await self._task_chat(f"Minecraft连接已断开，你发起的任务已中断；{note}",route=destination,kind='disconnect')

    async def _client_ready(self, _data):
        pending = self._interrupted_task
        self._interrupted_task = None
        if not pending or self._closing:
            return
        if not bool(self.config.get("auto_resume_after_disconnect", True)):
            return
        try:
            max_age = float(self.config.get("auto_resume_disconnect_seconds", 300) or 300)
        except (TypeError, ValueError):
            max_age = 300.0
        if time.time() - float(pending.get("at", 0) or 0) > max_age:
            return
        if self._resume_attempts >= 2:
            return
        status = await self._call("status", timeout=8)
        if self._err(status):
            return
        health = float(status.get("health", 20) or 20)
        hostiles = [
            row for row in (status.get("hostiles") or [])
            if isinstance(row, dict) and float(row.get("distance", 999) or 999) <= 8
        ]
        if health <= 8 or hostiles:
            return
        opcode = str(pending.get("opcode") or "")
        params = dict(pending.get("params") or {})
        stage = str((pending.get("checkpoint") or {}).get("stage") or "")
        if opcode == "mine_ore" and stage == "returning":
            opcode = "return_home"
            params = {"reach": 2.0}
        if opcode == "plan":
            plan_checkpoint = (pending.get("checkpoint") or {}).get("plan") or {}
            try:
                next_index = max(0, int(plan_checkpoint.get("next", 0) or 0))
            except (TypeError, ValueError):
                next_index = 0
            all_steps = [step for step in (params.get("steps") or []) if isinstance(step, dict)]
            remaining_steps = all_steps[next_index:]
            if not remaining_steps:
                return
            params = {
                "steps": remaining_steps,
                "break_budget": params.get("break_budget", 2048),
            }
        self._resume_attempts += 1
        route = pending.get("route")
        if route is None:
            route = UNSET
        route_token = None
        try:
            if route is not UNSET:
                route_token = REPLY_ROUTE.set(route)
            if opcode == "plan":
                task_id = self._enqueue_plan(
                    params["steps"], priority=int(pending.get("priority", 10) or 10),
                    goal="恢复多步骤计划",
                )
                if not task_id:
                    raise SkillError("任务队列已满")
            else:
                await self._start_agent_task(
                    opcode, params, int(pending.get("priority", 10) or 10),
                )
            await self._task_chat(
                "Minecraft重连，已自动继续之前中断的任务",
                route=route, kind="resume",
            )
        except Exception as exc:
            await self._task_chat(
                "Minecraft重连，但任务自动恢复失败：" + type(exc).__name__,
                route=route, kind="resume",
            )
        finally:
            if route_token is not None:
                REPLY_ROUTE.reset(route_token)

    async def _wait_arrival(self, x, y, z, seconds, tolerance=1.8, expected_scope=None, allow_ladder_assist=False, allow_break=False):
        progress_at=time.monotonic();deadline=progress_at+seconds;progress_position=None;ladder_assist_tried=False;last_status=None
        try:
            while time.monotonic() < deadline:
                if self._task_stop.is_set():
                    raise asyncio.CancelledError
                status = await self._call("status", timeout=10)
                if self._err(status):
                    raise BridgeError("位置查询失败：" + self._err(status))
                last_status = status
                if expected_scope and any(status.get(key) != expected_scope.get(key) for key in ("world_key", "dimension")):
                    raise BridgeError("世界或维度已改变，已停止寻路")
                position = status.get("position") or {}
                try:
                    distance = sum((float(position[k]) - target) ** 2
                                   for k, target in zip(("x", "y", "z"), (x, y, z))) ** 0.5
                except (KeyError, TypeError, ValueError) as exc:
                    raise BridgeError("客户端位置数据不完整") from exc
                if distance <= max(0.2, min(1.8, float(tolerance))):
                    return
                if (status.get("navigation_protection") or {}).get("break_budget_exhausted"):
                    raise BridgeError("客户端侧破坏预算已用完，已停止导航")
                stuck = None
                if (status.get('path_feedback') or {}).get('phase')=='CALC_FAILED':
                    stuck = '客户端确认当前路线无路，已停止，不再等到超时'
                if float(status.get('client_tps',0))>=10 and not status.get('client_paused') and status.get('screen','none')=='none':
                    coords=tuple(float(position[k]) for k in ('x','y','z'))
                    if progress_position is None or sum((a-b)**2 for a,b in zip(coords,progress_position))>=.3**2:
                        progress_position=coords;progress_at=time.monotonic()
                    elif time.monotonic()-progress_at>=4 and stuck is None:
                        stuck='寻路无进展，停止当前路线并重新选择'
                else:progress_at=time.monotonic();progress_position=None
                if stuck:
                    if allow_ladder_assist and not ladder_assist_tried:
                        ladder_assist_tried=True
                        if await self._try_ladder_assist(x, y, z):
                            progress_at=time.monotonic();progress_position=None
                            deadline=max(deadline,progress_at+seconds)
                            await asyncio.sleep(.3)
                            continue
                    raise BridgeError(stuck)
                await asyncio.sleep(.5)
            raise BridgeError("寻路超时，未确认到达")
        finally:
            if allow_break and isinstance(last_status, dict):
                try:
                    spent = int((last_status.get("navigation_protection") or {}).get("break_count", 0) or 0)
                    self._break_policy_for().record_navigation_breaks(spent)
                except Exception:
                    pass
            await self._call("baritone", {"command": "stop"}, timeout=5)

    async def _try_ladder_assist(self, x, y, z):
        """Try one bounded local ladder-entry assist, then reissue the same goto."""
        capabilities = parse_capabilities(self.bridge.client_info.get("capabilities"))
        if "ladder_assist" not in capabilities:
            return False
        try:
            data = await self._run_operation(
                "ladder_assist",
                {"x": math.floor(x), "y": math.floor(y), "z": math.floor(z)},
                seconds=6,
                timeout=10,
            )
        except SurvivalError:
            return False
        if not data.get("entered"):
            return False
        result = await self._call(
            "baritone",
            {"command": f"goto {math.floor(x)} {math.floor(y)} {math.floor(z)}"},
            timeout=10,
        )
        return not self._err(result)

    def _navigation_break_budget(self) -> int:
        try:
            policy = self._break_policy_for()
            task_id = str(getattr(getattr(self, "_task_current", None), "task_id", "") or "direct")
            policy.ensure_task(task_id)
            per_action = int(policy.per_action)
            opcode = str(getattr(getattr(self, "_task_current", None), "target", "") or "")
            if opcode in {"mine_ore", "return_home", "tunnel_to", "gather", "plan"}:
                try:
                    mining_action = int(self.config.get("break_budget_per_mining_action", 32) or 32)
                except (TypeError, ValueError):
                    mining_action = 32
                per_action = max(per_action, mining_action)
            return min(max(1, per_action), max(0, int(policy.remaining_task_budget())))
        except Exception:
            return 0

    async def _try_step_off_bed(self) -> bool:
        """Step off a bed to nearby ground before retrying a blocked route."""
        import math
        try:
            status = await self._call("status", timeout=8)
            if self._err(status):
                return False
            pos = status.get("position") or {}
            x = int(math.floor(float(pos.get("x"))))
            y = int(math.floor(float(pos.get("y"))))
            z = int(math.floor(float(pos.get("z"))))
            context = await self._call("block_context", {"x": x, "y": y, "z": z}, timeout=8)
            if self._err(context) or not str(context.get("block") or "").endswith("_bed"):
                return False
            candidates = []
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for dy in (0, -1):
                        if dx == 0 and dy == 0 and dz == 0:
                            continue
                        cell = (x + dx, y + dy, z + dz)
                        nearby = await self._call(
                            "block_context",
                            {"x": cell[0], "y": cell[1], "z": cell[2]},
                            timeout=6,
                        )
                        if self._err(nearby):
                            continue
                        block = str(nearby.get("block") or "")
                        if nearby.get("standable") and not block.endswith("_bed") and cell not in candidates:
                            candidates.append(cell)
                        for row in nearby.get("stands") or []:
                            if not isinstance(row, dict):
                                continue
                            try:
                                candidate = (
                                    int(row["x"]),
                                    int(round(float(row.get("feet_y", row.get("y", cell[1]))))),
                                    int(row["z"]),
                                )
                            except (KeyError, TypeError, ValueError):
                                continue
                            if candidate != (x, y, z) and candidate not in candidates:
                                candidates.append(candidate)
            if not candidates:
                return False
            valid = []
            for cell in candidates:
                check = await self._call(
                    "block_context",
                    {"x": cell[0], "y": cell[1], "z": cell[2]},
                    timeout=6,
                )
                if self._err(check):
                    continue
                if str(check.get("block") or "").endswith("_bed"):
                    continue
                if not check.get("standable"):
                    continue
                valid.append(cell)
            if not valid:
                return False
            ordered = sorted(
                valid,
                key=lambda cell: (cell[0] - x) ** 2 + (cell[1] - y) ** 2 + (cell[2] - z) ** 2,
            )
            for gx, gy, gz in ordered:
                try:
                    await self._run_operation("align", {"x": gx, "y": gy, "z": gz}, seconds=6)
                except Exception:
                    continue
                check_status = await self._call("status", timeout=8)
                if self._err(check_status):
                    continue
                current = check_status.get("position") or {}
                try:
                    bx = int(math.floor(float(current.get("x"))))
                    by = int(math.floor(float(current.get("y"))))
                    bz = int(math.floor(float(current.get("z"))))
                except (TypeError, ValueError):
                    continue
                check = await self._call("block_context", {"x": bx, "y": by, "z": bz}, timeout=6)
                if self._err(check):
                    continue
                if not str(check.get("block") or "").endswith("_bed"):
                    return True
            return False
        except Exception:
            return False

    async def _try_navigate(self, x, y, z, seconds=30) -> bool:
        """Try the client's collision-aware navigation for a short target."""
        import math
        result = await self._call(
            "navigate",
            {"x": math.floor(x), "y": math.floor(y), "z": math.floor(z), "allow_break": False},
            timeout=10,
        )
        if self._err(result):
            return False
        await self._wait_arrival(x, y, z, seconds, allow_ladder_assist=True)
        return True

    async def _move_and_wait(self, x, y, z, seconds, allow_break=False):
        import math
        if allow_break:
            budget = self._navigation_break_budget()
            if budget <= 0:
                raise BridgeError("任务破坏预算已用完，不能继续破坏移动")
            result = await self._call(
                "navigate",
                {"x": math.floor(x), "y": math.floor(y), "z": math.floor(z),
                 "allow_break": True, "max_break": budget},
                timeout=10,
            )
        else:
            result = await self._call("baritone", {"command": f"goto {math.floor(x)} {math.floor(y)} {math.floor(z)}"}, timeout=10)
        if self._err(result):
            raise BridgeError(self._err(result))
        await self._wait_arrival(x, y, z, seconds, allow_ladder_assist=True, allow_break=allow_break)

    async def _resolve_navigation_target(self, x, y, z):
        """Resolve a block coordinate to a standable cell before queuing goto."""
        x, y, z = int(x), int(y), int(z)
        capabilities = parse_capabilities(self.bridge.client_info.get("capabilities"))
        if "block_context" not in capabilities:
            return (x, y, z), {"adjusted": False, "source": "capability_missing"}
        context = await self._call("block_context", {"x": x, "y": y, "z": z}, timeout=10)
        error = self._err(context)
        if error:
            return None, "无法确认目标方块：" + error
        if context.get("standable"):
            return (x, y, z), {"adjusted": False, "source": "target"}
        stands = [row for row in (context.get("stands") or []) if isinstance(row, dict)]
        if stands:
            stand = min(stands, key=lambda row: float(row.get("distance", 999) or 999))
            return (int(stand["x"]), int(round(float(stand.get("feet_y", stand.get("y", y))))), int(stand["z"])), {
                "adjusted": True,
                "source": "block_context",
                "block": context.get("block", ""),
                "original": {"x": x, "y": y, "z": z},
            }
        for candidate in await self._goto_candidates(x, y, z):
            if candidate == (x, y, z):
                continue
            return candidate, {
                "adjusted": True,
                "source": "nearby_stand",
                "block": context.get("block", ""),
                "original": {"x": x, "y": y, "z": z},
            }
        return None, "目标不可站立，附近也没有可用站位；请先换一个入口或站位"

    async def _approach_target(self, x, y, z, seconds=20, reach=4.0, allow_break=False):
        """Move within interaction reach without queuing a user task; stop before retrying."""
        epoch = CONTROL_EPOCH.get()
        params={"x": int(x), "y": int(y), "z": int(z)}
        if allow_break:
            budget = self._navigation_break_budget()
            if budget <= 0:
                return "任务破坏预算已用完，不能继续破坏移动"
            params["allow_break"] = True
            params["max_break"] = budget
        nav = await self._call("navigate", params, timeout=10)
        if self._err(nav):
            return self._err(nav)
        deadline = time.monotonic() + max(5, min(30, float(seconds)))
        last = None
        last_change = time.monotonic()
        last_status = None
        target = (float(x) + 0.5, float(y) + 0.5, float(z) + 0.5)
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                if self._closing or (epoch is not None and epoch != self._control_epoch):
                    return "这次操作已被停止，未继续靠近"
                status = await self._call("status", timeout=10)
                if self._err(status):
                    return self._err(status)
                last_status = status
                pos = status.get("position") or {}
                try:
                    distance = sum((float(pos[k]) - point) ** 2 for k, point in zip(("x", "y", "z"), target)) ** 0.5
                except (KeyError, TypeError, ValueError):
                    return "客户端位置数据不完整"
                if distance <= reach:
                    return None
                if (status.get("navigation_protection") or {}).get("break_budget_exhausted"):
                    return "客户端侧破坏预算已用完，已停止靠近"
                if (status.get("path_feedback") or {}).get("phase") == "CALC_FAILED":
                    return "客户端确认当前路线无路，已停止"
                coords = tuple(float(pos[k]) for k in ("x", "y", "z"))
                if last is None or sum((a - b) ** 2 for a, b in zip(coords, last)) >= 0.3 ** 2:
                    last = coords
                    last_change = time.monotonic()
                elif time.monotonic() - last_change >= 4:
                    return "靠近目标无进展，已停止"
            return "靠近目标超时，未确认到达"
        finally:
            if allow_break and isinstance(last_status, dict):
                try:
                    spent = int((last_status.get("navigation_protection") or {}).get("break_count", 0) or 0)
                    self._break_policy_for().record_navigation_breaks(spent)
                except Exception:
                    pass
            await self._call("baritone", {"command": "stop"}, timeout=5)

    @filter.on_llm_request(priority=-9999)
    async def inject_shared_mc_memory(self, event, request):
        shared = getattr(self, "_shared_memory", None)
        if shared is None:
            return
        audience = self._audience(event)
        if shared.qq_owner(event):
            shared.prepare_qq(event)
            context = await shared.context_for(event, self.bridge, str(event.message_str or ""))
        elif audience.startswith("group:"):
            gid = audience.split(":", 1)[1]
            try:
                cooldown = max(0.0, float(self.config.get("shared_memory_group_cooldown_seconds", 10) or 10))
            except (TypeError, ValueError):
                cooldown = 10.0
            now = time.monotonic()
            last = getattr(self, "_group_memory_last", {})
            if now - float(last.get(gid, 0.0) or 0.0) < cooldown:
                return
            context = await shared.group_context_for(event, self.bridge, str(event.message_str or ""))
            if context:
                last[gid] = now
                self._group_memory_last = last
        else:
            return
        parts = request.extra_user_content_parts
        if context and not any(MARKER in str(getattr(part, "text", "")) for part in parts):
            parts.append(TextPart(text=context).mark_as_temp())

    @filter.on_decorating_result(priority=100)
    async def append_local_memory_card(self, event):
        cards = event.get_extra("_mc_place_cards", None)
        if not cards:
            return
        event.set_extra("_mc_place_cards", [])
        if self._audience(event) != "owner_private":
            return
        result = event.get_result()
        chain = getattr(result, "chain", None)
        if chain is None:
            return
        unique = []
        for item in cards:
            text = item.get("text") if isinstance(item, dict) else item
            text = str(text or "").strip()
            if text and text not in unique:
                unique.append(text)
        if unique:
            chain.append(Plain(text="\n" + "\n".join(unique)[:1200]))

    @filter.after_message_sent(priority=-9999)
    async def record_mc_delivery(self,event,*_args):
        trace=event.get_extra('_mc_trace',None)
        if trace is not None and trace.data.get('status')=='generated':
            await trace.delivered(stage='framework_send')

    @filter.after_message_sent(priority=-10000)
    async def capture_shared_mc_private(self, event, *_args):
        shared = getattr(self, "_shared_memory", None)
        if shared is None or not shared.qq_owner(event):
            return
        result = event.get_result()
        parts = getattr(result, "chain", []) if result is not None else []
        reply = " ".join(part.text for part in parts if isinstance(part, Plain))
        try:
            await shared.capture_qq(event, reply)
        except Exception as exc:
            shared.last_error = type(exc).__name__

    @filter.llm_tool(name="mc_inventory")
    @require_mc_tool("mc_inventory")
    async def mc_inventory(self, event: AstrMessageEvent, operation: str = "view", slot: int = -1, hotbar: int = -1):
        """查看背包、整理快捷栏、交换物品到快捷栏、选中槽位或将安全食物移入快捷栏。不丢弃物品。

        Args:
            operation(string): view、sort、swap、select、food。
            slot(int): swap来源背包索引0到35。
            hotbar(int): 目标快捷栏索引0到8。
        """
        if operation not in {"view", "sort", "swap", "select", "food"}:
            return "不支持的背包操作，未执行"
        try:
            if operation == "view":
                data = await self._call("inventory", {"operation": "view"})
                if self._err(data): return self._err(data)
                return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            if operation == "sort":
                data = await self._call("sort_inventory", {}, timeout=10)
                return self._err(data) or json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            return json.dumps(await self._run_operation("inventory", {"operation": operation, "slot": slot, "hotbar": hotbar}), ensure_ascii=False)
        except SurvivalError as exc:
            return "背包操作未完成：" + str(exc)

    @filter.llm_tool(name="mc_equip")
    @require_mc_tool("mc_equip")
    async def mc_equip(self, event: AstrMessageEvent, item: str = "best"):
        """穿戴已有护甲，旧装备放回背包；best只选择比当前更好的护甲。

        Args:
            item(string): best或物品注册ID，如minecraft:iron_chestplate。
        """
        try:
            return json.dumps(await self._run_operation("equip", {"item": item}), ensure_ascii=False)
        except SurvivalError as exc:
            return "装备未更换：" + str(exc)

    @filter.llm_tool(name="mc_equip_best")
    @require_mc_tool("mc_equip_best")
    async def mc_equip_best(self, event: AstrMessageEvent, tool_type: str = "pickaxe"):
        """从背包选择最佳工具并切到快捷栏。

        Args:
            tool_type(string): pickaxe、axe、sword、shovel、hoe。
        """
        tool_type = str(tool_type or "pickaxe").strip().lower()
        if tool_type not in {"pickaxe", "axe", "sword", "shovel", "hoe"}:
            return "tool_type 只能是 pickaxe/axe/sword/shovel/hoe"
        if self._queue_skill(f"装备最佳{tool_type}", "equip_best", tool_type=tool_type):
            return "已加入装备技能队列，完成后会通知实际工具"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_unequip_armor")
    @require_mc_tool("mc_unequip_armor")
    async def mc_unequip_armor(self, event: AstrMessageEvent, slot: str = "head"):
        """脱下护甲并放回背包；不移动角色。

        Args:
            slot(string): head、chest、legs、feet。
        """
        try:
            return json.dumps(await self._run_operation("unequip_armor", {"slot": str(slot or "head")}), ensure_ascii=False)
        except SurvivalError as exc:
            return "护甲未脱下：" + str(exc)

    @filter.llm_tool(name="mc_craft")
    @require_mc_tool("mc_craft")
    async def mc_craft(self, event: AstrMessageEvent, item: str, count: int = 1):
        """合成技能：检查/准备材料、必要时放置工作台，按服务器配方分批合成并核对入包。

        Args:
            item(string): 物品注册ID，如minecraft:stick。
            count(int): 希望新增的成品数量1到64。
        """
        item = str(item or "").strip()
        if not re.fullmatch(r"(?:[a-z0-9_]+:)?[a-z0-9_][a-z0-9_./-]*", item) or not 1 <= count <= 64:
            return "物品ID或数量无效，未执行"
        item = item if ":" in item else "minecraft:" + item
        if self._queue_skill(f"合成{item} x{int(count)}", "craft_item", item=item, count=int(count)):
            return "已加入合成技能队列，完成后会通知实际入包数量"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_gather")
    @require_mc_tool("mc_gather")
    async def mc_gather(self, event: AstrMessageEvent, resource: str = "logs", count: int = 8, radius: int = 12, max_seconds: int = 180, allow_night: bool = False):
        """采集技能：扫描自然资源，走近、清障、挖掘、拾取并核对库存增量。

        Args:
            resource(string): logs/stone/wheat/sand 或煤/铁/铜/金/红石/青金石/钻石/绿宝石等原矿及对应中文。
            count(int): 目标新增数量1-256。
            radius(int): 搜索半径6-16。
            max_seconds(int): 总时限20-600秒。
            allow_night(bool): 已废弃，忽略；夜间不再拦截采集。
        """
        if not 1 <= int(count) <= 256:
            return "采集数量必须在1-256"
        aliases = {
            "煤": "coal", "煤炭": "coal", "煤矿": "coal",
            "铁": "iron", "铁矿": "iron",
            "铜": "copper", "铜矿": "copper",
            "金": "gold", "金矿": "gold",
            "红石": "redstone", "红石矿": "redstone",
            "青金石": "lapis", "青金石矿": "lapis",
            "钻石": "diamond", "钻石矿": "diamond",
            "绿宝石": "emerald", "绿宝石矿": "emerald",
            "石头": "stone", "圆石": "stone", "石料": "stone",
            "木头": "logs", "原木": "logs", "树": "logs",
            "小麦": "wheat",
            "沙子": "sand", "沙": "sand",
        }
        resource = aliases.get(str(resource or "logs").strip().lower(), str(resource or "logs").strip().lower())
        error = await self._gather_guard(event, resource)
        if error:
            return error
        if self._queue_skill(
            f"采集{resource} x{int(count)}",
            "gather",
            resource=resource,
            count=int(count),
            radius=max(6, min(12, int(radius))),
            max_seconds=max(20, min(600, int(max_seconds))),
            allow_night=bool(allow_night),
        ):
            self._remember_gather_resource(resource)
            return "采集任务已接受并开始检查；敌人/目标不可达会立即失败，只有实际入包后才算完成"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_smelt")
    @require_mc_tool("mc_smelt")
    async def mc_smelt(self, event: AstrMessageEvent, item: str, count: int = 1, collect_only: bool = False):
        """烧炼技能：找附近普通熔炉，准备原料/燃料，烧炼并核对产物入包。

        Args:
            item(string): 产物物品ID，如minecraft:iron_ingot。
            count(int): 产物数量1-32。
            collect_only(bool): 只收取熔炉里已就绪的产物，不补材料。
        """
        item = str(item or "").strip()
        if not re.fullmatch(r"[a-z0-9_]+:[a-z0-9_./-]+", item):
            return "物品ID无效"
        if not 1 <= int(count) <= 32:
            return "烧炼数量必须在1-32"
        if self._queue_skill(
            f"烧炼{item} x{int(count)}",
            "smelt_item",
            item=item,
            count=int(count),
            collect_only=bool(collect_only),
        ):
            return "已加入烧炼技能队列，完成后会通知实际产物数量"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_container_transfer")
    @require_mc_tool("mc_container_transfer")
    async def mc_container_transfer(
        self,
        event: AstrMessageEvent,
        item: str = "",
        count: int = 1,
        direction: str = "to_container",
        x: int = 0,
        y: int = 0,
        z: int = 0,
        close: bool = True,
        items: str = "",
    ):
        """容器搬运技能：一次打开容器，搬完单个或多个物品后关闭。

        Args:
            item(string): 单个物品ID或显示名；批量时留空。
            count(int): 单个物品的搬运数量1-256。
            direction(string): to_container 放进容器，from_container 拿回背包。
            x(int): 容器方块X；为0且y/z也为0时使用当前已打开容器。
            y(int): 容器方块Y。
            z(int): 容器方块Z。
            close(bool): 完成后是否关闭容器，默认是。
            items(string): 批量清单；JSON数组如 [{"item":"minecraft:golden_axe","count":1},{"item":"minecraft:furnace","count":1}]，也可用 "minecraft:golden_axe:1,minecraft:furnace:1"。填写后忽略 item/count，只开一次容器。
        """
        direction = str(direction or "to_container").strip().lower()
        if direction not in {"to_container", "from_container"}:
            return "方向只能是 to_container 或 from_container"
        moves = parse_transfer_items(items) if str(items or "").strip() else []
        if str(items or "").strip() and not moves:
            return "批量清单为空或格式不正确"
        if len(moves) > 32:
            return "单次批量搬运最多32种物品"
        if moves:
            total = sum(move["count"] for move in moves)
            if total > 512:
                return "单次批量搬运总数不能超过512"
            label = f"批量搬运{len(moves)}种物品 x{total}"
            params = {"items": moves, "direction": direction, "x": int(x), "y": int(y), "z": int(z), "close": bool(close)}
        else:
            if not 1 <= int(count) <= 256:
                return "搬运数量必须在1-256"
            if not str(item or "").strip():
                return "请提供 item 或 items"
            label = f"容器搬运{item} x{int(count)}"
            params = {"item": str(item or ""), "count": int(count), "direction": direction, "x": int(x), "y": int(y), "z": int(z), "close": bool(close)}
        if self._queue_skill(label, "container_transfer", **params):
            if moves:
                return f"已加入批量搬运队列（{len(moves)} 种物品，共 {total} 个），只打开一次容器，完成后通知"
            return "已加入容器搬运技能队列，完成后会通知实际搬运数量"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_build")
    @require_mc_tool("mc_build")
    async def mc_build(
        self,
        event: AstrMessageEvent,
        pattern: str = "line",
        material: str = "minecraft:cobblestone",
        x: int = 0,
        y: int = 64,
        z: int = 0,
        length: int = 4,
        width: int = 1,
        height: int = 1,
        direction: str = "east",
    ):
        """建造技能：按 line/wall/floor/pillar/box 放置方块，最多64格。

        Args:
            pattern(string): line、wall、floor、pillar、box。
            material(string): 方块物品ID，如minecraft:cobblestone。
            x(int): 起点X。
            y(int): 起点Y。
            z(int): 起点Z。
            length(int): 长度1-16。
            width(int): 宽度1-16，floor/box使用。
            height(int): 高度1-8，wall/pillar/box使用。
            direction(string): east/west/south/north。
        """
        if str(pattern or "").lower() not in {"line", "wall", "floor", "pillar", "box"}:
            return "pattern 只能是 line/wall/floor/pillar/box"
        if self._queue_skill(
            f"建造{pattern} {material}",
            "build",
            pattern=str(pattern),
            material=str(material or "minecraft:cobblestone"),
            x=int(x),
            y=int(y),
            z=int(z),
            length=max(1, min(16, int(length))),
            width=max(1, min(16, int(width))),
            height=max(1, min(8, int(height))),
            direction=str(direction or "east"),
        ):
            return "已加入建造技能队列，完成后会通知实际放置数量"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_farm")
    @require_mc_tool("mc_farm")
    async def mc_farm(self, event: AstrMessageEvent, operation: str = "harvest", radius: int = 8, count: int = 8, replant: bool = True, crop: str = "auto"):
        """农业技能：收割成熟作物并补种，或给空农田补种。

        Args:
            operation(string): harvest、plant 或 both。
            radius(int): 搜索半径2-12。
            count(int): 最多处理数量1-32。
            replant(bool): 收割后是否补种，默认是。
            crop(string): auto/wheat/carrots/potatoes/beetroots。
        """
        operation = str(operation or "harvest").strip().lower()
        if operation not in {"harvest", "plant", "both"}:
            return "operation 只能是 harvest/plant/both"
        crop = str(crop or "auto").strip().lower()
        if crop not in {"auto", "wheat", "carrots", "potatoes", "beetroots"}:
            return "crop 只能是 auto/wheat/carrots/potatoes/beetroots"
        if self._queue_skill(
            f"农业{operation} {crop} x{int(count)}",
            "farm",
            operation=operation,
            radius=max(2, min(12, int(radius))),
            count=max(1, min(32, int(count))),
            replant=bool(replant),
            crop=crop,
        ):
            return "已加入农业技能队列，完成后会通知收割/补种数量"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_farm_plot")
    @require_mc_tool("mc_farm_plot")
    async def mc_farm_plot(
        self,
        event: AstrMessageEvent,
        operation: str = "status",
        plot: str = "",
        radius: int = 12,
        count: int = 32,
        crop: str = "auto",
        cells_json: str = "",
        mask: str = "",
        origin_json: str = "",
        replant: bool = True,
        dry_run: bool = False,
    ):
        """不规则田块：发现/记录/收割/补种/开垦，按格子集合而不是矩形处理。

        Args:
            operation(string): discover/status/harvest/plant/create/list/forget。
            plot(string): 可选田块 ID；留空表示发现全部或使用记忆里的田块。
            radius(int): 搜索半径2-16。
            count(int): 最多处理数量1-256。
            crop(string): auto/wheat/carrots/potatoes/beetroots。
            cells_json(string): create 用的格子 JSON，如 [[x,y,z], ...] 或 [{"x":..,"y":..,"z":..,"crop":"potatoes"}]。
            mask(string): create 用的不规则形状，如 "##./##~/.##"；# 作物，~ 水，. 跳过；行间用 / 或换行。
            origin_json(string): mask 的原点 [x,y,z]（y 是耕地层）；留空用当前位置。
            replant(bool): 收割后是否补种。
            dry_run(bool): create 只预览，不翻地/种植。
        """
        operation = str(operation or "status").strip().lower()
        if operation not in {"discover", "status", "harvest", "plant", "create", "list", "forget"}:
            return "operation 只能是 discover/status/harvest/plant/create/list/forget"
        crop = str(crop or "auto").strip().lower()
        if crop not in {"auto", "wheat", "carrots", "potatoes", "beetroots"}:
            return "crop 只能是 auto/wheat/carrots/potatoes/beetroots"
        cells = None
        if str(cells_json or "").strip():
            try:
                cells = json.loads(cells_json)
            except Exception:
                return "cells_json 不是有效 JSON"
            if not isinstance(cells, list):
                return "cells_json 必须是数组"
        origin = None
        if str(origin_json or "").strip():
            try:
                origin = json.loads(origin_json)
            except Exception:
                return "origin_json 不是有效 JSON"
        if self._queue_skill(
            f"田块{operation}",
            "farm_plot",
            operation=operation,
            plot=str(plot or "").strip(),
            radius=max(2, min(16, int(radius))),
            count=max(1, min(256, int(count))),
            crop=crop,
            cells=cells,
            mask=str(mask or ""),
            origin=origin,
            replant=bool(replant),
            dry_run=bool(dry_run),
        ):
            return "已加入田块任务，完成后会返回田块数量、成熟/空地统计和实际操作结果"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_resupply")
    @require_mc_tool("mc_resupply")
    async def mc_resupply(
        self,
        event: AstrMessageEvent,
        food: int = 8,
        torches: int = 8,
        blocks: int = 32,
        tools: bool = True,
        max_seconds: int = 300,
    ):
        """补给技能：按目标补齐安全食物、火把、圆石和基础工具，材料不足时返回缺口。

        Args:
            food(int): 目标安全食物份数0-64（约5点/份，优先从食物箱、现有农田和烹饪获取）。
            torches(int): 目标火把数量0-64。
            blocks(int): 目标圆石数量0-256。
            tools(bool): 是否补齐铁镐/铁斧/铁剑；无铁材料时返回缺口，不降级木器。
            max_seconds(int): 总时限30-900秒。
        """
        if not all(0 <= int(value) <= limit for value, limit in ((food, 64), (torches, 64), (blocks, 256))):
            return "补给数量超出范围"
        if self._queue_skill(
            "基础补给",
            "resupply",
            food=max(0, int(food)),
            torches=max(0, int(torches)),
            blocks=max(0, int(blocks)),
            tools=bool(tools),
            max_seconds=max(30, min(900, int(max_seconds))),
        ):
            return "已加入补给技能队列，完成后会返回实际库存和缺口"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_explore")
    @require_mc_tool("mc_explore")
    async def mc_explore(
        self,
        event: AstrMessageEvent,
        goal: str = "generic",
        max_seconds: int = 120,
        max_distance: int = 64,
        return_home: bool = False,
        allow_night: bool = False,
    ):
        """探索技能：有界侦察附近资源/村庄/POI，只走和看，不挖不建不打架。

        Args:
            goal(string): generic/logs/stone/coal/iron/wheat/village。
            max_seconds(int): 总时限10-300秒。
            max_distance(int): 离起点最大水平距离16-128。
            return_home(bool): 结束后是否返回起点，默认否。
            allow_night(bool): 已废弃，忽略；夜间不再拦截探索。
        """
        goal = str(goal or "generic").strip().lower()
        if goal not in {"generic", "logs", "stone", "coal", "iron", "wheat", "village"}:
            return "探索目标只能是 generic/logs/stone/coal/iron/wheat/village"
        if self._queue_skill(
            f"探索{goal}",
            "explore",
            goal=goal,
            max_seconds=max(10, min(300, int(max_seconds))),
            max_distance=max(16, min(128, int(max_distance))),
            return_home=bool(return_home),
            allow_night=bool(allow_night),
        ):
            return "已加入探索技能队列，完成后会返回发现列表"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_combat")
    @require_mc_tool("mc_combat")
    async def mc_combat(self, event: AstrMessageEvent, target: str = "", max_seconds: int = 30, retreat_health: int = 8):
        """战斗技能：追击并攻击敌对生物；血量过低时自动撤离，超时/无目标明确返回。

        Args:
            target(string): 可选目标名/类型；留空攻击最近敌对生物。
            max_seconds(int): 最长战斗时间5-120秒。
            retreat_health(int): 低于该血量撤离，1-20。
        """
        if self._queue_skill(
            "战斗" + (target or "最近敌人"),
            "combat",
            target=str(target or ""),
            max_seconds=max(5, min(120, int(max_seconds))),
            retreat_health=max(1, min(20, int(retreat_health))),
        ):
            return "已加入战斗技能队列，完成后会通知击杀/撤离结果"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_hunt")
    @require_mc_tool("mc_hunt")
    async def mc_hunt(self, event: AstrMessageEvent, target: str = "auto", count: int = 2,
                      radius: int = 24, max_seconds: int = 300, return_home: bool = True,
                      cook: bool = True, meat_target: int = 0, search: bool = True,
                      search_rounds: int = 6):
        """打猎技能：猎取牛/猪/羊/鸡/兔，拾取生肉，可回家后用熔炉烤熟。

        Args:
            target(string): auto 或具体猎物类型（cow/pig/sheep/chicken/rabbit/boar/shroomboar/snowboar/azurecrane/duck/ibis/crow/squirrel/weasel/unagi 等）。
            count(int): 最多猎取动物数量1-20。
            radius(int): 搜索半径4-32。
            max_seconds(int): 最长打猎时间10-600秒。
            return_home(bool): 打猎后是否回家，默认是。
            cook(bool): 回家后是否把生肉烤熟，默认是。
            meat_target(int): 目标生肉数量0-64；大于0时达到数量就停，不再按动物数停。
            search(bool): 附近没有猎物时是否边探索边找，默认是。
            search_rounds(int): 最多探索搜索轮数0-12。
        """
        target = str(target or "auto").strip().lower()
        if target != "auto":
            target = HUNT_ALIASES.get(target, target)
            if target not in HUNTABLE:
                return "target 只能是 auto 或具体猎物类型（cow/pig/sheep/chicken/rabbit/boar/azurecrane/duck/crow/squirrel/weasel/unagi 等）"
        if not 1 <= int(count) <= 20:
            return "打猎数量必须在1-20"
        if not 0 <= int(meat_target) <= 64:
            return "肉类目标必须在0-64"
        if self._queue_skill(
            f"打猎{target} x{int(count)}",
            "hunt",
            target=target,
            count=max(1, min(20, int(count))),
            radius=max(4, min(32, int(radius))),
            max_seconds=max(10, min(600, int(max_seconds))),
            return_home=bool(return_home),
            cook=bool(cook),
            meat_target=max(0, min(64, int(meat_target))),
            search=bool(search),
            search_rounds=max(0, min(12, int(search_rounds))),
        ):
            return "已加入打猎技能队列，完成后会返回猎物、生肉和熟肉数量"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_sleep")
    @require_mc_tool("mc_sleep")
    async def mc_sleep(self, event: AstrMessageEvent, source: str = "nearby", remember: bool = False):
        """睡觉技能：找附近空床或已记录的家床，走近并确认进入睡眠。

        Args:
            source(string): nearby 找附近床，home 使用家床记忆。
            remember(bool): nearby 睡觉成功后是否把这张床记为家床。
        """
        source = str(source or "nearby").strip().lower()
        source = "home" if source in {"home", "home_bed", "家", "家床"} else "nearby"
        goal = "回家床睡觉" if source == "home" else "寻找空床睡觉"
        if self._queue_skill(goal, "sleep", source=source, remember=bool(remember)):
            return "已加入睡觉技能队列，完成后会通知是否进入睡眠"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_trade_for")
    @require_mc_tool("mc_trade_for")
    async def mc_trade_for(self, event: AstrMessageEvent, item: str, count: int = 1, radius: int = 16):
        """交易技能：找村民，读取报价，选择能产出目标物品的交易并执行。

        Args:
            item(string): 目标产物ID或显示名。
            count(int): 交易次数1-8。
            radius(int): 搜索半径4-32。
        """
        item = str(item or "").strip()
        if not item:
            return "请提供目标物品"
        if not 1 <= int(count) <= 8:
            return "交易次数必须在1-8"
        if self._queue_skill(
            f"交易{item} x{int(count)}",
            "trade_for",
            item=item,
            count=int(count),
            radius=max(4, min(32, int(radius))),
        ):
            return "已加入交易技能队列，完成后会通知实际交易次数"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_scan")
    @require_mc_tool("mc_scan")
    async def mc_scan(self, event: AstrMessageEvent, kind: str = "blocks", radius: int = 8, query: str = "", limit: int = 16, include_air: bool = False):
        """区域概览：扫描附近方块或实体。单点坐标查询请用 mc_inspect_block，不要用 scan 代替。

        Args:
            kind(string): blocks 或 entities。
            radius(int): 扫描半径，方块1-16，实体1-32。
            query(string): blocks时填方块ID（可逗号分隔，可省略 minecraft: 前缀）；entities时填实体类型/名称/UUID；留空表示全部。
            limit(int): 最多返回数量1-32。
            include_air(bool): 是否包含空气方块，默认否。
        """
        mode = (kind or "blocks").strip().lower()
        if mode not in {"blocks", "entities"}:
            return "kind 只能是 blocks 或 entities"
        radius = max(1, min(32, int(radius)))
        limit = max(1, min(32, int(limit)))
        params = {"radius": radius, "limit": limit}
        if mode == "blocks":
            params["block"] = str(query or "")
            params["include_air"] = bool(include_air)
        else:
            params["type"] = str(query or "")
        result = await self._call("scan_blocks" if mode == "blocks" else "scan_entities", params, timeout=15)
        err = self._err(result)
        return err or json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="mc_inspect_block")
    @require_mc_tool("mc_inspect_block")
    async def mc_inspect_block(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """查看指定方块的ID、状态、硬度、所需工具、是否可交互。"""
        result = await self._call("inspect_block", {"x": int(x), "y": int(y), "z": int(z)}, timeout=10)
        err = self._err(result)
        return err or json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="mc_block_context")
    @require_mc_tool("mc_block_context")
    async def mc_block_context(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """查看方块上下文：是否床/门/容器、相邻可站位、可交互面。只读。

        Args:
            x(int): 方块X坐标。
            y(int): 方块Y坐标。
            z(int): 方块Z坐标。
        """
        result = await self._call("block_context", {"x": int(x), "y": int(y), "z": int(z)}, timeout=10)
        err = self._err(result)
        return err or json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="mc_get_menu")
    @require_mc_tool("mc_get_menu")
    async def mc_get_menu(self, event: AstrMessageEvent):
        """只读查看当前菜单/容器：界面、标题、槽位、光标和玩家背包槽。"""
        result = await self._call("get_menu", {}, timeout=10)
        err = self._err(result)
        return err or json.dumps(result, ensure_ascii=False)

    async def _operation_near(self, action, params, seconds):
        try:
            return await self._run_operation(action, params, seconds=seconds)
        except SurvivalError as exc:
            message = str(exc)
            if "outside interaction reach" not in message and "OUT_OF_REACH" not in message:
                raise
            error = await self._approach_target(params["x"], params["y"], params["z"])
            if error:
                raise SurvivalError(error)
            await asyncio.sleep(0.3)
            return await self._run_operation(action, params, seconds=seconds)

    @filter.llm_tool(name="mc_mine_at")
    @require_mc_tool("mc_mine_at")
    async def mc_mine_at(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """挖掘指定坐标的任意可破坏方块；超距时会先自动靠近到交互距离内，再确认方块变化。

        Args:
            x(int): 方块X坐标。
            y(int): 方块Y坐标。
            z(int): 方块Z坐标。
        """
        try:
            result = await self._operation_near("mine_at", {"x": int(x), "y": int(y), "z": int(z)}, 12)
            return json.dumps(result, ensure_ascii=False)
        except SurvivalError as exc:
            return "挖掘未完成：" + str(exc)

    @filter.llm_tool(name="mc_dig_down")
    @require_mc_tool("mc_dig_down")
    async def mc_dig_down(self, event: AstrMessageEvent, depth: int = 3):
        """受控向下挖竖井：逐格挖脚下方块，确认玩家下降；不自动封顶或出井。

        Args:
            depth(int): 向下挖几格1-4，默认3；每格都会检查流体、沙砾、基岩和落脚面。
        """
        if not 1 <= int(depth) <= 4:
            return "向下挖掘深度必须是1-4"
        try:
            result = await self._run_operation("dig_down", {"depth": int(depth)}, seconds=28)
            return json.dumps(result, ensure_ascii=False)
        except SurvivalError as exc:
            return "向下挖掘未完成：" + str(exc)

    @filter.llm_tool(name="mc_place_at")
    @require_mc_tool("mc_place_at")
    async def mc_place_at(self, event: AstrMessageEvent, x: int, y: int, z: int, item: str):
        """在指定坐标放置背包中的方块物品；超距时会先自动靠近，再确认目标方块出现。

        Args:
            x(int): 目标X坐标。
            y(int): 目标Y坐标。
            z(int): 目标Z坐标。
            item(string): 物品ID，例如 minecraft:dirt。
        """
        import math
        try:
            status = await self._call("status", timeout=10)
            if not self._err(status):
                pos = status.get("position") or {}
                feet = (math.floor(float(pos["x"])), math.floor(float(pos["y"])), math.floor(float(pos["z"])))
                if (int(x), int(y), int(z)) in {feet, (feet[0], feet[1] + 1, feet[2])}:
                    return "放置目标在脚下或身体所在位置，先走开或换一个坐标"
        except (KeyError, TypeError, ValueError):
            pass
        try:
            result = await self._operation_near("place_at", {"x": int(x), "y": int(y), "z": int(z), "item": str(item)}, 10)
            return json.dumps(result, ensure_ascii=False)
        except SurvivalError as exc:
            return "放置未完成：" + str(exc)

    @filter.llm_tool(name="mc_use_item")
    @require_mc_tool("mc_use_item")
    async def mc_use_item(self, event: AstrMessageEvent, item: str = "", target: str = "", x: int = 0, y: int = 0, z: int = 0):
        """使用手中或背包中的物品，可对准方块或实体。

        Args:
            item(string): 可选物品ID；留空使用当前手持。
            target(string): 可选实体名称/UUID；留空则使用方块坐标或直接使用。
            x(int): 可选方块X坐标。
            y(int): 可选方块Y坐标。
            z(int): 可选方块Z坐标。
        """
        params = {"item": str(item or ""), "target": str(target or "")}
        if x or y or z:
            params.update({"x": int(x), "y": int(y), "z": int(z)})
        result = await self._call("use_item", params, timeout=10)
        err = self._err(result)
        return err or json.dumps(result, ensure_ascii=False)

    async def _resolve_inventory_item(self, event, item, *, verb="使用", allow_selected=True):
        item = str(item or "").strip()
        if item:
            return item, None, None
        status = await self._call("status", timeout=10)
        err = self._err(status)
        if err:
            return "", status, err
        text = str(getattr(event, "message_str", "") or "")
        rows = status.get("inventory_items") or []
        matches = {}
        for row in rows:
            name = str(row.get("name") or "").strip()
            identity = str(row.get("id") or "").strip()
            if name and identity and name in text:
                matches[identity] = row
        if len(matches) == 1:
            return next(iter(matches)), status, None
        if len(matches) > 1:
            names = "、".join(sorted({str(row.get("name") or identity) for identity, row in matches.items()}))
            return "", status, f"背包里有多个匹配项：{names}；请指定物品ID"
        if allow_selected and re.search(r"这个|它|手里|当前|选中|手持", text):
            selected = int(status.get("selected_slot", -1))
            row = next((row for row in rows if int(row.get("slot", -1)) == selected), None)
            if row and row.get("id"):
                return str(row["id"]), status, None
        held = str(status.get("held") or "空")
        return "", status, f"请指定要{verb}的物品ID；当前选中：{held}"

    @filter.llm_tool(name="mc_drop_item")
    @require_mc_tool("mc_drop_item")
    async def mc_drop_item(self, event: AstrMessageEvent, item: str = "", count: int = 1):
        """从背包丢弃指定数量物品；item 留空时按主人这句话匹配背包物品名，或丢弃“手里这个”。

        Args:
            item(string): 物品ID，例如 minecraft:rotten_flesh；留空时从当前消息匹配物品名。
            count(int): 数量1-64；消息说“都/全部/所有”时按整组丢。
        """
        item = str(item or "").strip()
        text = str(getattr(event, "message_str", "") or "")
        amount = max(1, min(64, int(count)))
        if re.search(r"都|全部|所有", text) and amount == 1:
            amount = 64
        if not item:
            item, _status, error = await self._resolve_inventory_item(event, item, verb="丢")
            if error:
                return error
        result = await self._call("drop_item", {"item": item, "count": amount}, timeout=10)
        err = self._err(result)
        return err or json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="mc_select_slot")
    @require_mc_tool("mc_select_slot")
    async def mc_select_slot(self, event: AstrMessageEvent, slot: int):
        """切换快捷栏槽位，slot 为 0-8。"""
        result = await self._call("select_slot", {"slot": int(slot)}, timeout=10)
        err = self._err(result)
        return err or json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="mc_container")
    @require_mc_tool("mc_container")
    async def mc_container(self, event: AstrMessageEvent, operation: str = "read", x: int = 0, y: int = 0, z: int = 0, slot: int = -1, button: int = 0, mode: str = "pickup"):
        """操作容器/菜单：open 打开方块容器，read 读取槽位，click 点击槽位，close 关闭界面。

        Args:
            operation(string): open、read、click 或 close。
            x(int): open时容器方块X坐标。
            y(int): open时容器方块Y坐标。
            z(int): open时容器方块Z坐标。
            slot(int): click时的槽位索引。
            button(int): click时的按钮，0左键，1右键。
            mode(string): click模式 pickup、quick_move、swap、throw。
        """
        action = (operation or "read").strip().lower()
        params = {"operation": action}
        if action == "open":
            params.update({"x": int(x), "y": int(y), "z": int(z)})
        if action == "click":
            params.update({"slot": int(slot), "button": int(button), "mode": str(mode)})
        result = await self._call("container", params, timeout=10)
        err = self._err(result)
        if not err and action == "open":
            self._register_container(int(x), int(y), int(z))
        return err or json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="mc_move_item")
    @require_mc_tool("mc_move_item")
    async def mc_move_item(self, event: AstrMessageEvent, item: str = "", count: int = 1, direction: str = "to_container"):
        """在当前打开的容器和背包之间搬运物品；count 会一次搬多个，不必循环 count=1。

        多种物品请优先用 mc_container_transfer(items=...)，只开一次容器。

        Args:
            item(string): 物品ID或背包显示名；留空时从这句话匹配，或“手里这个”。
            count(int): 搬运数量1-64；一次调用可搬多组。
            direction(string): to_container 放进容器，from_container 拿回背包。
        """
        direction = str(direction or "to_container").strip().lower()
        if direction not in {"to_container", "from_container"}:
            return "方向只能是 to_container 或 from_container"
        item, _status, error = await self._resolve_inventory_item(event, item, verb="搬运")
        if error:
            return error
        result = await self._call(
            "move_item",
            {"item": item, "count": max(1, min(64, int(count))), "direction": direction},
            timeout=10,
        )
        err = self._err(result)
        return err or json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="mc_protect")
    @require_mc_tool("mc_protect")
    async def mc_protect(
        self,
        event: AstrMessageEvent,
        operation: str = "list",
        name: str = "",
        x: int = 0,
        y: int = 0,
        z: int = 0,
        radius: int = 3,
        kind: str = "building",
    ):
        """管理受保护区域：list 查看，add 添加，remove 删除。受保护区域不会被挖掘或导航破坏。

        Args:
            operation(string): list、add 或 remove。
            name(string): 区域名称；add 时可留空自动生成，remove 时必填。
            x(int): 区域中心X；add 时使用。
            y(int): 区域中心Y；add 时使用。
            z(int): 区域中心Z；add 时使用。
            radius(int): 保护半径1-16，默认3。
            kind(string): building 或 container。
        """
        action = str(operation or "list").strip().lower()
        policy = self._break_policy_for()
        if action == "list":
            return json.dumps(policy.snapshot(), ensure_ascii=False)
        if action == "remove":
            if not str(name or "").strip():
                return "删除保护区域需要 name"
            removed = policy.remove(name)
            return json.dumps({"removed": removed, "name": name}, ensure_ascii=False)
        if action != "add":
            return "operation 只能是 list、add 或 remove"
        kind = str(kind or "building").strip().lower()
        if kind not in {"building", "container"}:
            return "kind 只能是 building 或 container"
        radius = max(1, min(16, int(radius)))
        dimension = ""
        if int(x) == 0 and int(y) == 0 and int(z) == 0:
            status = await self._call("status", timeout=10)
            error = self._err(status)
            if error:
                return "无法获取当前位置：" + error
            position = status.get("position") or {}
            x, y, z = int(position.get("x", 0)), int(position.get("y", 0)), int(position.get("z", 0))
            dimension = str(status.get("dimension") or "")
        region = policy.add_around(
            str(name or f"{kind}-{int(x)}-{int(y)}-{int(z)}"),
            int(x), int(y), int(z), radius=radius, kind=kind, dimension=dimension,
        )
        return json.dumps({"added": True, "region": region}, ensure_ascii=False)

    @filter.llm_tool(name="mc_pickup")
    @require_mc_tool("mc_pickup")
    async def mc_pickup(self, event: AstrMessageEvent, item: str = "", count: int = 1, radius: int = 8,
                        allow_break: bool = False):
        """拾取附近掉落物；可指定物品名/ID，留空拾取最近的掉落物。

        Args:
            item(string): 可选物品ID或显示名，例如 minecraft:rotten_flesh / 腐肉。
            count(int): 最多拾取几堆1-8。
            radius(int): 扫描半径1-16。
            allow_break(bool): 只有显式允许时才挖开挡路方块靠近掉落物，默认否。
        """
        count = max(1, min(8, int(count)))
        radius = max(1, min(16, int(radius)))
        allow_break = bool(allow_break)
        data = await self._call("scan_entities", {"radius": radius, "limit": 32, "type": "minecraft:item"}, timeout=10)
        err = self._err(data)
        if err:
            return err
        wanted = str(item or "").strip()
        rows = []
        for attempt in range(3):
            rows = [row for row in (data.get("entities") or []) if isinstance(row.get("item"), dict)]
            if wanted:
                rows = [
                    row for row in rows
                    if wanted == str((row.get("item") or {}).get("id") or "")
                    or wanted == str((row.get("item") or {}).get("name") or "")
                    or wanted in str(row.get("name") or "")
                ]
            if rows:
                break
            if attempt < 2:
                await asyncio.sleep(0.35)
                data = await self._call("scan_entities", {"radius": radius, "limit": 32}, timeout=10)
                if self._err(data):
                    break
        if not rows:
            return "附近没有匹配的掉落物"
        picked, errors = [], []
        for row in rows[:count]:
            drop = row.get("item") or {}
            identity = str(drop.get("id") or "")
            if not identity:
                continue
            try:
                if float(row.get("distance", 99)) > 2.2:
                    error = await self._approach_target(
                        row["x"], row["y"], row["z"], seconds=20, reach=1.8, allow_break=allow_break
                    )
                    if error:
                        errors.append({"item": identity, "error": error})
                        continue
                try:
                    result = await self._run_operation("pickup", {"item": identity, "entity_uuid": str(row.get("uuid") or "")}, seconds=5)
                except SurvivalError as exc:
                    if "PICKUP_UNAVAILABLE" not in str(exc):
                        raise
                    if not allow_break:
                        raise
                    error = await self._approach_target(row["x"], row["y"], row["z"], seconds=20, reach=1.8, allow_break=True)
                    if error:
                        raise
                    result = await self._run_operation("pickup", {"item": identity, "entity_uuid": str(row.get("uuid") or "")}, seconds=5)
                picked.append({"item": identity, "result": result})
            except SurvivalError as exc:
                errors.append({"item": identity, "error": str(exc)})
        return json.dumps({"picked": len(picked), "drops": picked, "errors": errors}, ensure_ascii=False)

    @filter.llm_tool(name="mc_use_on_entity")
    @require_mc_tool("mc_use_on_entity")
    async def mc_use_on_entity(self, event: AstrMessageEvent, target: str, item: str = ""):
        """对附近实体右键使用物品，例如喂食、拴绳、桶、命名牌、上船/上马。

        Args:
            target(string): 实体名、类型ID或UUID，需要在8格内。
            item(string): 可选物品ID；留空使用当前手持。
        """
        target = str(target or "").strip()
        if not target:
            return "请提供实体名、类型ID或UUID"
        params = {"target": target}
        if str(item or "").strip():
            params["item"] = str(item).strip()
        result = await self._call("use_item", params, timeout=10)
        err = self._err(result)
        return err or json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="mc_trade")
    @require_mc_tool("mc_trade")
    async def mc_trade(self, event: AstrMessageEvent, operation: str = "read", target: str = "", index: int = -1, count: int = 1):
        """与村民交易：open 打开村民，read 读取报价，trade 按报价交易，close 关闭。

        Args:
            operation(string): open、read、trade 或 close。
            target(string): open时的村民名/类型ID/UUID，需要在8格内。
            index(int): trade时的报价序号，从read结果取。
            count(int): trade时连续交易次数1-8。
        """
        action = str(operation or "read").strip().lower()
        if action == "open":
            target = str(target or "").strip()
            if not target:
                return "请提供村民名、类型ID或UUID"
            result = await self._call("use_item", {"target": target}, timeout=10)
            err = self._err(result)
            if err:
                return err
            await asyncio.sleep(0.6)
            status = await self._call("status", timeout=10)
            screen = str((status or {}).get("screen") or "none")
            return json.dumps({"opened": screen != "none", "screen": screen}, ensure_ascii=False)
        if action == "read":
            result = await self._call("trade", {"operation": "read"}, timeout=10)
            err = self._err(result)
            return err or json.dumps(result, ensure_ascii=False)
        if action == "trade":
            if int(index) < 0:
                return "请先 read，并提供报价序号 index"
            trades = []
            for attempt in range(max(1, min(8, int(count)))):
                result = await self._call("trade", {"operation": "trade", "index": int(index)}, timeout=10)
                err = self._err(result)
                if err:
                    if trades:
                        return "部分完成：" + json.dumps({"trades": trades, "error": err}, ensure_ascii=False)
                    return err
                trades.append(result)
                if attempt + 1 < max(1, min(8, int(count))):
                    await asyncio.sleep(0.35)
            return json.dumps({"trades": trades, "count": len(trades)}, ensure_ascii=False)
        if action == "close":
            result = await self._call("container", {"operation": "close"}, timeout=10)
            err = self._err(result)
            return err or json.dumps(result, ensure_ascii=False)
        return "trade operation 只能是 open/read/trade/close"

    @filter.llm_tool(name="mc_ride")
    @require_mc_tool("mc_ride")
    async def mc_ride(self, event: AstrMessageEvent, action: str = "ride", target: str = ""):
        """骑乘或离开船、矿车、马等实体；dismount 会按 shift 下车并确认。

        Args:
            action(string): ride 骑乘，dismount 下车。
            target(string): ride时的实体名/类型ID/UUID，需要在8格内。
        """
        action = str(action or "ride").strip().lower()
        if action == "dismount":
            try:
                result = await self._run_operation("dismount", {}, seconds=5)
                return json.dumps(result, ensure_ascii=False)
            except SurvivalError as exc:
                return "下车未完成：" + str(exc)
        if action != "ride":
            return "action 只能是 ride 或 dismount"
        target = str(target or "").strip()
        if not target:
            return "请提供实体名、类型ID或UUID"
        result = await self._call("ride", {"target": target}, timeout=10)
        err = self._err(result)
        if err:
            return err
        await asyncio.sleep(0.5)
        status = await self._call("status", timeout=10)
        passenger = bool((status or {}).get("passenger"))
        return json.dumps({**result, "riding": passenger}, ensure_ascii=False)

    @filter.llm_tool(name="mc_wake")
    @require_mc_tool("mc_wake")
    async def mc_wake(self, event: AstrMessageEvent):
        """起床/离开床；不移动角色。"""
        result = await self._call("wake", {}, timeout=10)
        err = self._err(result)
        if err:
            return err
        await asyncio.sleep(0.3)
        status = await self._call("status", timeout=10)
        return json.dumps({"awake": not bool((status or {}).get("sleeping")), "sleeping": bool((status or {}).get("sleeping"))}, ensure_ascii=False)

    @filter.llm_tool(name="mc_set_respawn")
    @require_mc_tool("mc_set_respawn")
    async def mc_set_respawn(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """把指定床设为重生点，并在床交互成功后写入 home_bed 记忆。

        Args:
            x(int): 床方块X坐标。
            y(int): 床方块Y坐标。
            z(int): 床方块Z坐标。
        """
        status = await self._call("status", timeout=10)
        err = self._err(status)
        if err:
            return err
        context = await self._call("block_context", {"x": int(x), "y": int(y), "z": int(z)}, timeout=10)
        err = self._err(context)
        if err:
            return err
        if context.get("bed_occupied"):
            return "这张床已被占用，未设置重生点"
        if not context.get("bed"):
            return "目标不是可用床，未设置重生点"
        stands = [row for row in (context.get("stands") or []) if isinstance(row, dict)]
        if not stands:
            # A bed has two halves.  The requested half may face a wall while the
            # other half has a usable stand; try the four horizontal neighbours.
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbor = await self._call(
                    "block_context", {"x": int(x) + dx, "y": int(y), "z": int(z) + dz}, timeout=10
                )
                if self._err(neighbor):
                    continue
                if not neighbor.get("bed") or neighbor.get("bed_occupied"):
                    continue
                if str(neighbor.get("block") or "") != str(context.get("block") or ""):
                    continue
                if str(neighbor.get("state") or "") == str(context.get("state") or ""):
                    continue
                candidate = [row for row in (neighbor.get("stands") or []) if isinstance(row, dict)]
                if candidate:
                    stands = candidate
                    break
        if not stands:
            return "床周围没有可站立位置，未设置重生点"
        stand = min(stands, key=lambda row: float(row.get("distance", 999) or 999))
        try:
            error = await self._approach_target(
                stand["x"], stand.get("feet_y", stand["y"]), stand["z"], seconds=12, reach=1.5
            )
        except Exception as exc:
            return "接近床失败：" + str(exc)
        if error:
            return "接近床失败：" + error
        await asyncio.sleep(0.2)
        result = await self._call("interact_block", {"x": int(x), "y": int(y), "z": int(z)}, timeout=10)
        err = self._err(result)
        if err and "not visible" in err.lower():
            try:
                error = await self._approach_target(
                    stand["x"], stand.get("feet_y", stand["y"]), stand["z"], seconds=5, reach=1.2
                )
            except Exception:
                error = "retry failed"
            if not error:
                await asyncio.sleep(0.2)
                result = await self._call("interact_block", {"x": int(x), "y": int(y), "z": int(z)}, timeout=10)
                err = self._err(result)
        if err:
            return "床交互失败：" + err
        if not result.get("interacted"):
            return "床交互没有确认成功，未写入家床记忆"
        try:
            memory = self._poi_memory_for()
            memory.set(
                "home_bed", "main", name="重生点",
                world_key=str(status.get("world_key") or ""),
                dimension=str(status.get("dimension") or ""),
                x=int(x), y=int(y), z=int(z),
            )
            saved = memory.get("home_bed", "main")
        except Exception as exc:
            return "床已交互，但家床记忆写入失败：" + str(exc)
        place_ref = ""
        place = self._remember_home_bed(
            status, {"x": int(x), "y": int(y), "z": int(z)}, stand,
            source="task_result", notes="mc_set_respawn verified bed interaction",
        )
        if place is not None:
            place_ref = place.get("public_ref", "")
            self._queue_place_cards(event, [place])
        return json.dumps({
            "saved": saved is not None,
            "bed": context.get("block"),
            "position": {"x": int(x), "y": int(y), "z": int(z)},
            "approached_to": {"x": stand["x"], "y": stand.get("feet_y", stand["y"]), "z": stand["z"]},
            "memory": saved,
            "place_ref": place_ref,
            "note": "已走到床边、完成交互并写入 home_bed；服务端重生点提示仍以游戏画面为准",
        }, ensure_ascii=False)

    def _reply_with_place_cards(self, event, reply):
        getter = getattr(event, "get_extra", None)
        cards = getter("_mc_place_cards", None) if callable(getter) else None
        if not cards or self._audience(event) != "owner_private":
            return reply
        event.set_extra("_mc_place_cards", [])
        unique = []
        for item in cards:
            text = item.get("text") if isinstance(item, dict) else item
            text = str(text or "").strip()
            if text and text not in unique:
                unique.append(text)
        if not unique:
            return reply
        body = str(reply or "").rstrip()
        return (body + "\n\n" + "\n".join(unique)[:1200]).strip()

    def _queue_place_cards(self, event, rows) -> None:
        if event is None or self._audience(event) != "owner_private":
            return
        cards = []
        store = getattr(self, "_memory_store", None)
        if store is None:
            return
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            try:
                card = store.local_card(row)
            except Exception:
                card = ""
            if card:
                key = str(row.get("public_ref") or f"{row.get('kind')}:{row.get('key')}")
                cards.append({"key": key, "text": card})
        if cards:
            existing = event.get_extra("_mc_place_cards", None) or []
            merged = {}
            order = []
            for item in list(existing) + cards:
                if isinstance(item, dict):
                    key = str(item.get("key") or item.get("text") or "")
                    text = str(item.get("text") or "")
                else:
                    key = str(item or "")
                    text = str(item or "")
                if not text:
                    continue
                if key not in merged:
                    order.append(key)
                merged[key] = text
            event.set_extra("_mc_place_cards", [{"key": key, "text": merged[key]}
                                                 for key in order][:5])

    async def _remember_nearby(self, kind: str, key: str, name: str, status: dict):
        queries = {
            "home_bed": "#minecraft:beds",
            "chest": "minecraft:chest",
            "workbench": "minecraft:crafting_table",
            "furnace": "minecraft:furnace",
            "portal": "minecraft:nether_portal",
        }
        scope = self._memory_scope_for(status)
        store = self._memory_store_for()
        query = queries.get(kind, "")
        target = stand = None
        target_status = stand_status = "unknown"
        if query:
            scan = await self._call("scan_blocks", {"radius": 8, "limit": 8, "block": query}, timeout=15)
            err = self._err(scan)
            if err:
                return None, err
            blocks = [item for item in (scan.get("blocks") or []) if isinstance(item, dict)
                      and item.get("x") is not None]
            blocks.sort(key=lambda item: float(item.get("distance", 999) or 999))
            if not blocks:
                return None, "附近没有找到要记录的地点，请先靠近或换一个类型"
            block = blocks[0]
            target = (int(block["x"]), int(block["y"]), int(block["z"]))
            context = await self._call("block_context", {"x": target[0], "y": target[1], "z": target[2]}, timeout=10)
            err = self._err(context)
            if err:
                return None, err
            block_id = str(context.get("block") or block.get("block") or "")
            if kind == "home_bed" and not block_id.endswith("_bed"):
                return None, "找到的目标不是床，未记录家床"
            stands = [item for item in (context.get("stands") or []) if isinstance(item, dict)]
            stands.sort(key=lambda item: float(item.get("distance", 999) or 999))
            if stands:
                stand = stands[0]
                stand_status = "valid"
            target_status = "valid"
        else:
            position = status.get("position") if isinstance(status.get("position"), dict) else {}
            if position.get("x") is None:
                return None, "无法确认当前位置，未写入"
            target = (int(position.get("x")), int(position.get("y", 0)), int(position.get("z")))
            stand = {"x": position.get("x"), "y": position.get("y"), "z": position.get("z"),
                     "feet_y": position.get("y")}
            target_status = stand_status = "valid"
        row = store.remember_place(
            world_key=scope["world_key"],
            world_instance=scope["world_instance"],
            dimension=scope["dimension"],
            kind=kind,
            key=key,
            name=name,
            target=target,
            stand=stand,
            target_status=target_status,
            stand_status=stand_status,
            observation_status="loaded",
            confidence=0.7,
            source="observed",
            privacy="owner_only",
            notes="remembered from local observation",
            actor="mc_memory",
        )
        return row, ""

    async def _verify_memory_place(self, row: dict, scope: dict):
        store = self._memory_store_for()
        target_x = row.get("target_x")
        target_y = row.get("target_y")
        target_z = row.get("target_z")
        if target_x is None:
            return store.verify_place(row["public_ref"], target_status="invalid",
                                      stand_status="unknown", observation_status="unloaded",
                                      world_instance=scope.get("world_instance", ""),
                                      actor="mc_memory")
        context = await self._call("block_context", {"x": target_x, "y": target_y, "z": target_z}, timeout=10)
        err = self._err(context)
        if err:
            return {"__error__": err}
        block_id = str(context.get("block") or "")
        expected = {
            "home_bed": "_bed",
            "chest": "chest",
            "workbench": "crafting_table",
            "furnace": "furnace",
            "portal": "nether_portal",
        }.get(str(row.get("kind") or ""))
        target_status = "valid" if block_id and (not expected or expected in block_id) else "invalid"
        stands = [item for item in (context.get("stands") or []) if isinstance(item, dict)]
        stands.sort(key=lambda item: float(item.get("distance", 999) or 999))
        stand_status = "valid" if stands else "unknown"
        return store.verify_place(
            row["public_ref"], target_status=target_status,
            stand_status=stand_status, observation_status="loaded",
            note=f"block={block_id[:80]}", actor="mc_memory",
            world_instance=scope.get("world_instance", ""),
            stand=stands[0] if stands else None,
        )

    @staticmethod
    def _legacy_memory_safe(row: dict) -> dict:
        return {
            "kind": row.get("kind"),
            "key": row.get("key"),
            "label": row.get("name") or row.get("kind") or "地点",
            "name": row.get("name") or row.get("kind") or "地点",
            "dimension": row.get("dimension") or "",
            "status": "legacy",
            "warnings": ["新记忆库不可用，当前只显示旧记录；不能自动导航"],
        }

    def _legacy_memory_fallback(self, event, operation: str, kind: str, key: str):
        try:
            memory = self._poi_memory_for()
        except Exception:
            return None
        if operation in {"list", "find", "nearby"}:
            rows = memory.list("" if kind in {"", "all"} else kind)
            if rows and self._audience(event) == "owner_private":
                cards = []
                for row in rows[:5]:
                    label = row.get("name") or row.get("kind") or "地点"
                    cards.append(
                        f"[本地坐标卡片] {label}（legacy）；目标({row.get('x')}, {row.get('y')}, {row.get('z')})；"
                        f"维度 {row.get('dimension') or '未知'}；旧记录，未验证"
                    )
                if cards:
                    event.set_extra("_mc_place_cards", cards)
            return json.dumps({"found": bool(rows), "items": [self._legacy_memory_safe(row) for row in rows],
                               "note": "新记忆库不可用，当前为旧 JSON 只读回退。"}, ensure_ascii=False)
        if operation == "get":
            row = memory.get(kind, key)
            if row and self._audience(event) == "owner_private":
                label = row.get("name") or row.get("kind") or "地点"
                event.set_extra("_mc_place_cards", [
                    f"[本地坐标卡片] {label}（legacy）；目标({row.get('x')}, {row.get('y')}, {row.get('z')})；"
                    f"维度 {row.get('dimension') or '未知'}；旧记录，未验证"
                ])
            return json.dumps({"found": row is not None,
                               "item": self._legacy_memory_safe(row) if row else None,
                               "note": "新记忆库不可用，当前为旧 JSON 只读回退。"}, ensure_ascii=False)
        if operation == "forget":
            return json.dumps({"forgot": bool(memory.forget(kind, key)),
                               "note": "新记忆库不可用，已删除旧 JSON 记录。"}, ensure_ascii=False)
        return None

    @filter.llm_tool(name="mc_memory")
    @require_mc_tool("mc_memory")
    async def mc_memory(
        self,
        event: AstrMessageEvent,
        operation: str = "get",
        kind: str = "home_bed",
        key: str = "main",
        place_ref: str = "",
        name: str = "",
        query: str = "",
        share_scope: str = "",
    ):
        """读写 MC 地点记忆。模型只看到不透明 place_ref 和安全标签；精确坐标由本地卡片显示。

        Args:
            operation(string): get、list、find、history、remember、verify、forget、pin、share、bind_world。
            kind(string): home/home_bed/base/portal/farm/mine/storage/chest/workbench/furnace/villager/danger/waypoint/resource/structure。
            key(string): 记录键，默认 main。
            place_ref(string): 地点的不透明句柄，优先于 kind/key。
            name(string): remember 时的安全显示名；也可作为 list/find 的筛选词。
            query(string): list/find 的可选筛选词。
            share_scope(string): share 时的目标：owner_only、shared 或 group:<群号>；群号必须在白名单。
        """
        operation = str(operation or "get").strip().lower()
        kind = str(kind or "home_bed").strip()
        key = str(key or "main").strip()
        place_ref = str(place_ref or "").strip()
        name = str(name or "").strip()
        query = str(query or "").strip()
        share_scope = str(share_scope or "").strip()
        audience = self._audience(event)
        is_owner = audience == "owner_private"
        is_group = audience.startswith("group:")
        if not (is_owner or is_group):
            return "MC地点记忆仅授权私聊或白名单群只读使用；当前会话未读取私有记忆"
        if is_group and operation in {"remember", "verify", "forget", "pin", "share", "bind_world"}:
            return "群聊永久只读：不能写入、复核、删除或分享地点记忆"
        try:
            store = self._memory_store_for()
            status = await self._call("status", timeout=10)
            err = self._err(status)
            if err and operation in {"remember", "verify", "bind_world"}:
                return err
            offline = bool(err)
            if offline:
                scope = {"world_key": "", "world_instance": "", "dimension": ""}
            else:
                scope = self._memory_scope_for(status)

            def find_row():
                if place_ref:
                    row = store.get_place(place_ref)
                    if row is not None and not store.can_view(row, audience):
                        return None
                    return row
                if is_group:
                    rows = store.find_places(
                        world_key=scope["world_key"], world_instance=scope["world_instance"],
                        dimension=scope["dimension"], kind=kind, key=key,
                        audience=audience, limit=5,
                    )
                    return rows[0] if rows else None
                return store.latest_place(
                    world_key=scope["world_key"], world_instance=scope["world_instance"],
                    dimension=scope["dimension"], kind=kind, key=key,
                )

            if operation in {"list", "find", "nearby"}:
                use_kind = "" if kind in {"", "all"} else kind
                limit = 20
                if is_group:
                    try:
                        limit = max(1, min(4, int(self.config.get("shared_memory_group_max_results", 2) or 2)))
                    except (TypeError, ValueError):
                        limit = 2
                rows = store.find_places(
                    world_key=scope["world_key"], world_instance=scope["world_instance"],
                    dimension=scope["dimension"], kind=use_kind,
                    query=query or name, audience=audience, limit=limit,
                )
                self._queue_place_cards(event, rows)
                try:
                    store.record_retrieval(audience=audience, query=query or name, rows=rows)
                except Exception:
                    pass
                items = [store.safe_view(row, audience=audience) for row in rows]
                return json.dumps({
                    "found": bool(items), "items": items,
                    "offline": offline,
                    "note": ("群聊只读：精确坐标不公开，也没有本地坐标卡片；不要提示用户看卡片，不要编造坐标。"
                             if is_group else
                             ("游戏未连接，按最近保存的地点查询；精确坐标只在主人私聊由本地卡片补充。"
                              if offline else
                              "精确坐标不会进入模型；主人私聊会由本地卡片补充坐标。")),
                }, ensure_ascii=False)

            if operation == "history":
                row = find_row()
                if row is None:
                    return json.dumps({"found": False, "note": "没有这个地点记录"}, ensure_ascii=False)
                self._queue_place_cards(event, [row])
                history = []
                for item in store.place_history(row["public_ref"], limit=2 if is_group else 20):
                    history.append({
                        "changed_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(item.get("changed_at", 0))),
                        "change_kind": item.get("change_kind", ""),
                        "actor": item.get("actor", ""),
                        "note": redact_text(item.get("note", ""))[:200],
                    })
                return json.dumps({"found": True, "item": store.safe_view(row, audience=audience),
                                   "history": history}, ensure_ascii=False)

            if operation == "get":
                row = find_row()
                if row is None:
                    return json.dumps({"found": False, "note": "没有这个地点记录，不要编造坐标"}, ensure_ascii=False)
                self._queue_place_cards(event, [row])
                return json.dumps({"found": True, "item": store.safe_view(row, audience=audience),
                                   "offline": offline,
                                   "note": ("群聊只读：精确坐标不公开，没有本地卡片；不要提示用户看卡片或编造坐标。"
                                            if is_group else
                                            "精确坐标由本地卡片补充，不要自己编造。")}, ensure_ascii=False)

            if operation == "forget":
                row = find_row()
                if row is None:
                    return json.dumps({"forgot": False, "note": "没有这个地点记录"}, ensure_ascii=False)
                forgot = store.forget_place(row["public_ref"], actor="mc_memory", note="owner forget")
                return json.dumps({"forgot": bool(forgot), "place_ref": row["public_ref"]}, ensure_ascii=False)

            if operation == "pin":
                row = find_row()
                if row is None:
                    return json.dumps({"pinned": False, "note": "没有这个地点记录"}, ensure_ascii=False)
                pinned = store.pin_place(row["public_ref"], True)
                self._queue_place_cards(event, [store.get_place(row["public_ref"]) or row])
                return json.dumps({"pinned": bool(pinned), "place_ref": row["public_ref"]}, ensure_ascii=False)

            if operation == "verify":
                row = find_row()
                if row is None:
                    return json.dumps({"verified": False, "note": "没有这个地点记录"}, ensure_ascii=False)
                verified = await self._verify_memory_place(row, scope)
                if isinstance(verified, dict) and verified.get("__error__"):
                    return "复核失败：" + str(verified["__error__"])
                self._queue_place_cards(event, [verified] if verified else [])
                return json.dumps({"verified": bool(verified),
                                   "item": store.safe_view(verified, audience=audience) if verified else None},
                                  ensure_ascii=False)

            if operation == "share":
                row = find_row()
                if row is None:
                    return json.dumps({"shared": False, "note": "没有这个地点记录"}, ensure_ascii=False)
                target = share_scope or "shared"
                privacy = ""
                if target in {"owner", "owner_only", "private"}:
                    privacy = "owner_only"
                elif target in {"shared", "group_shared", "all"}:
                    privacy = "group_shared"
                elif target.startswith("group:"):
                    gid = target.split(":", 1)[1].strip()
                    whitelist = self._group_whitelist()
                    allowed = bool(gid) and any(entry == gid or entry.endswith(":" + gid) for entry in whitelist)
                    if not allowed:
                        return "群号不在白名单，未分享"
                    privacy = "group_only:" + gid
                else:
                    return "share_scope 只能是 owner_only、shared 或 group:<群号>"
                updated = store.set_privacy(row["public_ref"], privacy, actor="mc_memory")
                self._queue_place_cards(event, [updated] if updated else [])
                return json.dumps({
                    "shared": bool(updated),
                    "place_ref": row["public_ref"],
                    "scope": privacy,
                    "item": store.safe_view(updated, audience="owner_private") if updated else None,
                }, ensure_ascii=False)

            if operation == "bind_world":
                instance = store.rebind_world(scope["world_key"], alias=name)
                cache = getattr(self, "_memory_world_cache", None)
                if isinstance(cache, dict):
                    cache[scope["world_key"]] = instance
                return json.dumps({
                    "bound": True,
                    "note": "已在当前世界地址创建新的世界实例；旧地点不会继承，需要重新确认。",
                }, ensure_ascii=False)

            if operation == "remember":
                row, message = await self._remember_nearby(kind, key, name, status)
                if row is None:
                    return message or "没有记录到地点"
                self._queue_place_cards(event, [row])
                return json.dumps({
                    "saved": True,
                    "item": store.safe_view(row, audience=audience),
                    "note": "地点已写入本地库；精确坐标由本地卡片补充。",
                }, ensure_ascii=False)

            return "operation 只能是 get/list/find/history/remember/verify/forget/pin/share/bind_world"
        except ValueError as exc:
            return "记忆操作失败：" + str(exc)
        except Exception as exc:
            logger.warning("[mc-bridge] memory operation failed: %s", type(exc).__name__)
            fallback = self._legacy_memory_fallback(event, operation, kind, key)
            if fallback is not None:
                return fallback
            return "记忆操作未完成：" + type(exc).__name__

    @filter.llm_tool(name="mc_interact_block")
    @require_mc_tool("mc_interact_block")
    async def mc_interact_block(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """与白名单方块交互：门、栅栏门、按钮、拉杆、床、活板门。

        Args:
            x(int): 方块X坐标。
            y(int): 方块Y坐标。
            z(int): 方块Z坐标。
        """
        result = await self._call("interact_block", {"x": int(x), "y": int(y), "z": int(z)}, timeout=10)
        err = self._err(result)
        return err or json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="mc_release_use")
    @require_mc_tool("mc_release_use")
    async def mc_release_use(self, event: AstrMessageEvent):
        """松开正在使用的物品（弓、盾、三叉戟等）。"""
        result = await self._call("release_use", {}, timeout=10)
        err = self._err(result)
        return err or json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="mc_mine_below")
    @require_mc_tool("mc_mine_below")
    async def mc_mine_below(self, event: AstrMessageEvent):
        """受控挖脚下一格，并确认玩家下降；不做连续竖井。"""
        try:
            result = await self._run_operation("mine_below", {}, seconds=10)
            return json.dumps(result, ensure_ascii=False)
        except SurvivalError as exc:
            return "向下挖单格未完成：" + str(exc)

    @filter.llm_tool(name="mc_harvest_block")
    @require_mc_tool("mc_harvest_block")
    async def mc_harvest_block(self, event: AstrMessageEvent, x: int, y: int, z: int, item_filter: str = ""):
        """采集指定单个方块，必要时接近、挖掉并拾取指定掉落物。

        Args:
            x(int): 方块X坐标。
            y(int): 方块Y坐标。
            z(int): 方块Z坐标。
            item_filter(string): 可选掉落物ID，用于拾取和入包校验。
        """
        if self._queue_skill(
            f"采集方块 {x} {y} {z}",
            "harvest_block",
            block_pos=[int(x), int(y), int(z)],
            item_filter=str(item_filter or ""),
        ):
            return "已加入单方块采集队列，完成后会返回掉落物校验结果"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_plant_at")
    @require_mc_tool("mc_plant_at")
    async def mc_plant_at(self, event: AstrMessageEvent, x: int, y: int, z: int, seed: str = "minecraft:wheat_seeds"):
        """在已知耕地上种植种子，并校验上方作物出现。

        Args:
            x(int): 耕地X坐标。
            y(int): 耕地Y坐标。
            z(int): 耕地Z坐标。
            seed(string): 种子物品ID。
        """
        if self._queue_skill(
            f"种植 {x} {y} {z}",
            "plant_at",
            farmland_pos=[int(x), int(y), int(z)],
            seed=str(seed or "minecraft:wheat_seeds"),
        ):
            return "已加入种植队列，完成后会确认作物出现"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_till_and_plant")
    @require_mc_tool("mc_till_and_plant")
    async def mc_till_and_plant(self, event: AstrMessageEvent, x: int, y: int, z: int, seed: str = "minecraft:wheat_seeds"):
        """先用锄头耕地，再立即种植并校验。

        Args:
            x(int): 目标泥土/草方块X坐标。
            y(int): 目标泥土/草方块Y坐标。
            z(int): 目标泥土/草方块Z坐标。
            seed(string): 种子物品ID。
        """
        if self._queue_skill(
            f"耕地并种植 {x} {y} {z}",
            "till_and_plant",
            block_pos=[int(x), int(y), int(z)],
            seed=str(seed or "minecraft:wheat_seeds"),
        ):
            return "已加入耕地并种植队列，完成后会确认作物出现"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_open_and_read")
    @require_mc_tool("mc_open_and_read")
    async def mc_open_and_read(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """打开已知容器、读取槽位并关闭。只读操作。

        Args:
            x(int): 容器X坐标。
            y(int): 容器Y坐标。
            z(int): 容器Z坐标。
        """
        if self._queue_skill(
            f"读取容器 {x} {y} {z}",
            "open_and_read",
            container_pos=[int(x), int(y), int(z)],
        ):
            return "已加入容器读取队列，完成后会返回槽位"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_place_line")
    @require_mc_tool("mc_place_line")
    async def mc_place_line(self, event: AstrMessageEvent, x: int, y: int, z: int, direction: str = "east", length: int = 4, material: str = "minecraft:cobblestone"):
        """从起点沿一个方向放置单行方块。

        Args:
            x(int): 起点X坐标。
            y(int): 起点Y坐标。
            z(int): 起点Z坐标。
            direction(string): east/west/south/north。
            length(int): 长度1-64。
            material(string): 方块物品ID。
        """
        direction = str(direction or "east").strip().lower()
        if direction not in {"east", "west", "south", "north"}:
            return "direction 只能是 east/west/south/north"
        length = max(1, min(64, int(length)))
        if self._queue_skill(
            f"放置单行 {x} {y} {z}",
            "place_line",
            start=[int(x), int(y), int(z)],
            direction=direction,
            length=length,
            material=str(material or "minecraft:cobblestone"),
        ):
            return "已加入单行放置队列，完成后会返回实际放置数量"
        return "任务队列已满或正在停止，未执行"

    @filter.llm_tool(name="mc_till_block")
    @require_mc_tool("mc_till_block")
    async def mc_till_block(self, event: AstrMessageEvent, x: int, y: int, z: int, item: str = ""):
        """用锄头把泥土/草方块变成耕地，自动选择背包里的锄头。

        Args:
            x(int): 方块X坐标。
            y(int): 方块Y坐标。
            z(int): 方块Z坐标。
            item(string): 可选锄头注册ID；留空自动选择。
        """
        try:
            result = await self._run_operation("till", {"x": int(x), "y": int(y), "z": int(z), "item": str(item or "")}, seconds=5)
            return json.dumps(result, ensure_ascii=False)
        except SurvivalError as exc:
            return "耕地未完成：" + str(exc)

    @filter.llm_tool(name="mc_use_bonemeal")
    @require_mc_tool("mc_use_bonemeal")
    async def mc_use_bonemeal(self, event: AstrMessageEvent, x: int, y: int, z: int, item: str = "minecraft:bone_meal"):
        """对已种植的作物使用骨粉，并确认作物状态发生变化。

        Args:
            x(int): 作物X坐标。
            y(int): 作物Y坐标。
            z(int): 作物Z坐标。
            item(string): 骨粉物品ID，默认minecraft:bone_meal。
        """
        try:
            result = await self._run_operation(
                "use_bonemeal",
                {"x": int(x), "y": int(y), "z": int(z), "item": str(item or "minecraft:bone_meal")},
                seconds=5,
            )
            return json.dumps(result, ensure_ascii=False)
        except SurvivalError as exc:
            return "骨粉使用未完成：" + str(exc)

    @filter.llm_tool(name="mc_jump_once")
    @require_mc_tool("mc_jump_once")
    async def mc_jump_once(self, event: AstrMessageEvent):
        """原地跳一次，并确认离开地面。"""
        try:
            result = await self._run_operation("jump", {}, seconds=5)
            return json.dumps(result, ensure_ascii=False)
        except SurvivalError as exc:
            return "跳跃未完成：" + str(exc)

    @filter.llm_tool(name="mc_align")
    @require_mc_tool("mc_align")
    async def mc_align(self, event: AstrMessageEvent, x: int, y: int, z: int):
        """精确对齐到指定方块的站立中心。

        Args:
            x(int): 方块X坐标。
            y(int): 方块Y坐标。
            z(int): 方块Z坐标。
        """
        status = await self._call("status", timeout=10)
        error = self._err(status)
        if error:
            return error
        position = status.get("position") or {}
        try:
            distance = math.hypot(
                float(position.get("x", 0)) - (int(x) + 0.5),
                float(position.get("z", 0)) - (int(z) + 0.5),
            )
        except (TypeError, ValueError):
            distance = 999.0
        if distance > 1.5:
            return f"距离目标 {distance:.1f} 格；mc_align 只做最后微调，请先用 mc_goto 到 {int(x)} {int(y)} {int(z)} 旁的站位"
        try:
            result = await self._run_operation("align", {"x": int(x), "y": int(y), "z": int(z)}, seconds=5)
            return json.dumps(result, ensure_ascii=False)
        except SurvivalError as exc:
            return "对齐未完成：" + str(exc)

    @filter.llm_tool(name="mc_wait")
    @require_mc_tool("mc_wait")
    async def mc_wait(self, event: AstrMessageEvent, seconds: float = 1):
        """等待指定秒数；当前任务完成时提前返回，并附带 last 最近任务结果。"""
        duration = max(0.1, min(30.0, float(seconds)))
        started = time.monotonic()
        active = getattr(self, "_active_task", None)
        if active is not None and not active.done():
            try:
                await asyncio.wait_for(asyncio.shield(active), timeout=duration)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        else:
            await asyncio.sleep(duration)
        current = getattr(self, "_task_current", None)
        return json.dumps({
            "waited": round(time.monotonic() - started, 2),
            "active": bool(current is not None and active is not None and not active.done()),
            "current_task_id": getattr(current, "task_id", "") if current is not None else "",
            "goal": getattr(current, "goal", "") if current is not None else "",
            "last": getattr(self, "_last_task_result", None),
        }, ensure_ascii=False)

    @filter.llm_tool(name="mc_capabilities")
    @require_mc_tool("mc_capabilities")
    async def mc_capabilities(self, event: AstrMessageEvent):
        """列出当前 L1/L2 技能、资源需求和客户端能力。只读。"""
        l1 = [
            {"name": spec.opcode.value, "resources": sorted(resource.value for resource in spec.resources),
             "max_seconds": spec.max_seconds}
            for spec in L1_SPECS.values()
        ]
        l2 = [
            {"name": spec.opcode.value, "resources": sorted(resource.value for resource in spec.resources),
             "max_seconds": spec.max_seconds}
            for spec in L2_SPECS.values()
        ]
        return json.dumps({
            "l0_count": len(L0_OPCODE_NAMES),
            "l1": l1,
            "l2": l2,
            "client_protocol": self.bridge.client_info.get("protocol", 1),
            "client_capabilities": sorted(parse_capabilities(self.bridge.client_info.get("capabilities"))),
        }, ensure_ascii=False)

    @filter.llm_tool(name="mc_plan")
    @require_mc_tool("mc_plan")
    async def mc_plan(self, event: AstrMessageEvent, steps_json: str, priority: int = int(Priority.NORMAL),
                      dry_run: bool = False):
        """把多步骤任务一次提交为顺序计划；每步失败默认停止，可标记继续。

        Args:
            steps_json(string): JSON 数组，每项如 {"opcode":"mine_ore","params":{"resource":"lapis","count":1,"return_home":false}}；最多 8 步。craft_item 可选 table_pos=[x,y,z] 指定工作台；gather 支持 sand。
            priority(int): 0紧急、10主人、20普通、30自主、40后台。
            dry_run(bool): 只预览步骤和预算，不加入队列。
        """
        try:
            steps = json.loads(str(steps_json or "[]"))
        except Exception:
            return "steps_json 不是有效JSON"
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            return "steps_json 必须是 1-8 步的数组"
        cleaned = []
        for raw in steps:
            if not isinstance(raw, dict):
                return "每个步骤必须是JSON对象"
            opcode = str(raw.get("opcode") or "").strip()
            params = raw.get("params") or {}
            if not isinstance(params, dict):
                return f"{opcode or '空步骤'} 的 params 必须是对象"
            if self._agent_skill_spec(opcode) is None:
                return f"计划步骤不支持：{opcode or '空'}"
            cleaned.append({
                "opcode": opcode,
                "params": params,
                "continue_on_failure": bool(raw.get("continue_on_failure", False)),
            })
        try:
            seconds, breaks, resources = self._plan_limits(cleaned)
        except SkillError as exc:
            return str(exc)
        if dry_run:
            return json.dumps({
                "planned": True, "steps": cleaned, "max_seconds": seconds,
                "break_budget": breaks, "resources": list(resources),
            }, ensure_ascii=False)
        goal = "多步骤计划：" + " → ".join(step["opcode"] for step in cleaned)
        task_id = self._enqueue_plan(cleaned, priority=max(0, min(40, int(priority))), goal=goal[:160])
        if not task_id:
            return "任务队列已满或正在停止，未执行"
        self._resume_attempts = 0
        return json.dumps({
            "accepted": True, "task_id": task_id, "steps": len(cleaned),
            "note": "计划会按顺序执行，完成或失败后统一通知",
        }, ensure_ascii=False)

    @filter.llm_tool(name="mc_task_start")
    @require_mc_tool("mc_task_start")
    async def mc_task_start(
        self,
        event: AstrMessageEvent,
        opcode: str,
        params_json: str = "{}",
        priority: int = int(Priority.NORMAL),
        dry_run: bool = False,
        depends_on: str = "",
    ):
        """启动一个 L1/L2 技能任务，返回 task_id；参数用 JSON 对象传入。

        Args:
            opcode(string): mine_ore(resource,count,return_home)（煤/铁/铜/金/红石/青金石/钻石/绿宝石，深层矿自动下挖）、gather(resource,count)、craft(item,count)、smelt(item,count)、container_transfer(items 或 item,count,direction,x,y,z)、build(pattern,material,x,y,z,length,width,height,direction)、farm(operation,count,radius,replant)、farm_plot(operation,plot,radius,count,crop,cells,mask,origin,replant,dry_run)、hunt(target,count,radius,max_seconds,return_home,cook)、sleep(source,remember)、combat(target,max_seconds,retreat_health)、explore(goal,max_seconds,max_distance)、return_to(target_pos)、return_home(reach)、tunnel_to(target_pos,max_distance,reach)、resupply(...)、trade_for(item,count,radius)；auto 表示按文本分类；resume 表示恢复检查点任务。
            params_json(string): 技能参数JSON；auto 模式用 {"text":"砍8个木头"}。
            priority(int): 0紧急、10主人、20普通、30自主、40后台。
            dry_run(bool): 只校验并返回资源/时限预览，不加入队列。
            depends_on(string): 可选依赖任务ID；依赖完成后才启动。
        """
        try:
            params = json.loads(str(params_json or "{}"))
        except Exception:
            return "params_json 不是有效JSON"
        if not isinstance(params, dict):
            return "params_json 必须是JSON对象"
        if len(json.dumps(params, ensure_ascii=False)) > 4096:
            return "参数过大，拒绝启动"
        if not dry_run:
            depends_on = self._chain_dependency(str(depends_on or ""))
        try:
            wait_seconds = max(0.0, min(15.0, float(self.config.get("short_task_wait_seconds", 5) or 0)))
        except (TypeError, ValueError):
            wait_seconds = 5.0
        inline_wait = wait_seconds > 0 and not depends_on
        intent_info = None
        opcode = str(opcode or "").strip()
        if opcode in {"resume", "recover"}:
            recovery_id = str(params.get("task_id") or params.get("recovery_id") or "").strip()
            entry = self._task_recovery_for().get(recovery_id)
            if entry is None:
                return "没有可恢复任务：" + recovery_id
            result = await self._resume_recovery_group(entry, int(priority), dry_run=bool(dry_run))
            result.setdefault("opcode", entry.get("opcode") or "")
            return json.dumps(result, ensure_ascii=False)
        if opcode in {"", "auto", "intent"}:
            text = str(params.get("text") or params.get("command") or "").strip()
            if not text:
                return "auto 模式需要 params_json 里的 text"
            intent_info = classify_task(text)
            if not intent_info.get("opcode"):
                return "无法识别任务：" + text
            opcode = intent_info["opcode"]
            params = {**intent_info.get("params", {}), **{
                key: value for key, value in params.items() if key not in {"text", "command"}
            }}
        if opcode in {"gather", "mine_ore"}:
            resource = params.get("resource", "coal" if opcode == "mine_ore" else "logs")
            guard_error = await self._gather_guard(event, resource)
            if guard_error:
                return "任务未启动：" + guard_error
        try:
            if dry_run:
                preview = self._agent_task_preview(opcode, params, int(priority), depends_on)
                payload = {"planned": True, **preview}
                if intent_info is not None:
                    payload["intent"] = intent_info
                return json.dumps(payload, ensure_ascii=False)
            task_id = await self._start_agent_task(opcode, params, int(priority), depends_on, suppress_notify=inline_wait)
            if not dry_run:
                self._resume_attempts = 0
                self._remember_task_chain(task_id)
            if opcode in {"gather", "mine_ore"}:
                self._remember_gather_resource(params.get("resource", "coal" if opcode == "mine_ore" else "logs"))
        except SkillError as exc:
            return "任务未启动：" + str(exc)
        payload = {"accepted": True, "task_id": task_id, "opcode": opcode, "params": params}
        if intent_info is not None:
            payload["intent"] = intent_info
        if inline_wait:
            status = await self._await_short_task(task_id, wait_seconds)
            terminal = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
            if not (status and status.get("state") in terminal):
                self._clear_inline_suppression(task_id)
                status = self._scheduler_for().task_status(task_id)
            if status and status.get("state") in terminal:
                state = str(status.get("state") or "")
                payload.update({
                    "inline": True,
                    "status": state,
                    "goal_outcome": status.get("goal_outcome", ""),
                    "result": status.get("result"),
                    "error": status.get("error", ""),
                    "note": "短任务已在本次调用内完成" if state == TaskState.COMPLETED else (
                        "短任务已在本次调用内失败" if state == TaskState.FAILED else "短任务已在本次调用内取消"),
                })
            else:
                payload["inline"] = False
                payload["note"] = f"短任务未在 {wait_seconds:g} 秒内完成，完成后会异步通知"
        return json.dumps(payload, ensure_ascii=False)

    @filter.llm_tool(name="mc_task_status")
    @require_mc_tool("mc_task_status")
    async def mc_task_status(self, event: AstrMessageEvent, task_id: str = "", detail: bool = False):
        """查看指定任务或当前调度器/队列状态。只读。

        Args:
            task_id(string): 可选任务ID；留空查看当前队列和活动任务。
            detail(bool): 是否返回完整租约/审计/历史；默认只返回紧凑摘要。
        """
        data = self._agent_task_status(task_id, detail=bool(detail))
        if self._audience(event) != "owner_private" and isinstance(data, dict):
            data = {"active": bool(data.get("active")),
                    "pending_count": int(data.get("pending_count", 0) or 0)}
        return json.dumps(data, ensure_ascii=False)

    @filter.llm_tool(name="mc_task_cancel")
    @require_mc_tool("mc_task_cancel")
    async def mc_task_cancel(self, event: AstrMessageEvent, task_id: str = ""):
        """取消指定任务；留空取消当前任务和整个队列。

        Args:
            task_id(string): 可选任务ID；留空取消全部。
        """
        task_id = str(task_id or "").strip()
        if not task_id:
            error = await self._stop_task_runtime()
            return json.dumps({
                "cancelled": "all",
                "queue_cleared": True,
                "client_stop_confirmed": error is None,
                "client_error": error or "",
            }, ensure_ascii=False)
        task = self._scheduler_for().get(task_id)
        if task is None:
            return json.dumps(self._cancel_agent_task(task_id), ensure_ascii=False)
        if task.state == TaskState.RUNNING:
            error = await self._stop_task_runtime()
            return json.dumps({
                "found": True,
                "task_id": task_id,
                "active": True,
                "queue_cleared": True,
                "client_stop_confirmed": error is None,
                "client_error": error or "",
            }, ensure_ascii=False)
        return json.dumps(self._cancel_agent_task(task_id), ensure_ascii=False)

    @filter.llm_tool(name="mc_recipes")
    @require_mc_tool("mc_recipes")
    async def mc_recipes(self,event:AstrMessageEvent,query:str,kind:str="recipes",limit:int=3):
        """查当前服务器同步配方、中文物品名和客户端已加载模组，只读。

        Args:
            query(string): 中文物品名、注册ID或模组名；mods时可留空。
            kind(string): recipes配方、items物品、mods客户端模组。
            limit(int): 返回数量1—5，默认3。
        """
        if kind not in {'recipes','items','mods'}:return 'kind仅支持recipes/items/mods'
        if len(query)>80:return '查询词过长'
        aliases={'农夫乐事':'farmersdelight','森罗厨房':'kaleidoscope_cookery','森罗物语':'kaleidoscope','车万女仆':'touhou_little_maid','东方女仆':'touhou_little_maid','铁魔法':'irons_spellbooks','精妙背包':'sophisticatedbackpacks','精妙存储':'sophisticatedstorage','传送石碑':'waystones'}
        if kind=='mods':query=aliases.get(query,query)
        data=await self._call('knowledge',{'query':query,'kind':kind,'limit':max(1,min(5,int(limit)))},timeout=20)
        if self._err(data):return self._err(data)
        # Keep machine-readable JSON bounded; do not cut the raw JSON string.
        rows=data.get('results',[])
        for row in rows:
            for group in row.get('ingredients',[]):
                group['alternatives']=[{k:r[k] for k in ('id','name','count') if k in r} for r in group.get('alternatives',[])[:3]]
        while len(json.dumps(data,ensure_ascii=False))>12000 and len(rows)>1:rows.pop();data['truncated']=True
        return json.dumps(data,ensure_ascii=False,separators=(',',':'))

    @filter.llm_tool(name="mc_interact")
    @require_mc_tool("mc_interact")
    async def mc_interact(self,event:AstrMessageEvent,operation:str="inspect",x:int=0,y:int=-9999,z:int=0):
        """查看准星目标/近处方块，或开关手动木门等交互；拆方块请用 mc_mine_at。

        只用于门、按钮、拉杆等交互；不要用本工具拆火把、方块或容器。

        Args:
            operation(string): inspect观察准星，open开门，close关门。
            x(int): 开关门的方块x坐标。
            y(int): 开关门的方块y坐标；inspect无需坐标。
            z(int): 开关门的方块z坐标。
        """
        if operation=='inspect':
            data=await self._call('sense',timeout=10)
            return self._err(data) or json.dumps(data,ensure_ascii=False,separators=(',',':'))
        if operation not in {'open','close'}:return 'operation仅支持inspect/open/close'
        status=await self._call('status',timeout=10)
        if self._err(status):return self._err(status)
        pos=status.get('position') or {}
        try:
            if abs(int(y)-float(pos['y']))>4 or abs(int(x)-float(pos['x']))+abs(int(z)-float(pos['z']))>8:
                return '门不在近处交互范围内，未执行'
        except (KeyError,TypeError,ValueError):
            return '位置数据不完整，未执行'
        try:
            return json.dumps(await self._run_operation('interact',{'operation':operation,'x':int(x),'y':int(y),'z':int(z)},seconds=6),ensure_ascii=False)
        except SurvivalError as ex:return str(ex)

    @filter.llm_tool(name="mc_recall_memory")
    @require_mc_tool("mc_recall_memory")
    async def mc_recall_memory(self, event: AstrMessageEvent, query: str, date: str = ""):
        """查主人与MinecraftBot的MC共享经历及游戏相关私聊；不含其他私聊。

        Args:
            query(string): 要查的MC经历或话题。
            date(string): 可选，YYYY-MM-DD或今天/昨天/前天。
        """
        try:
            audience = self._audience(event)
            if audience.startswith("group:"):
                return await self._shared_memory.group_recall(
                    event, self.bridge, audience.split(":", 1)[1], query)
            return await self._shared_memory.recall(event, self.bridge, query, date)
        except Exception as exc:
            return "MC共享记忆未能查询：" + type(exc).__name__

    @filter.llm_tool(name="mc_forget_memory")
    @require_mc_tool("mc_forget_memory")
    async def mc_forget_memory(self, event: AstrMessageEvent, memory_id: int):
        """仅在主人QQ私聊明确要求遗忘时删除指定MC共享记忆，不影响其他记忆。

        Args:
            memory_id(int): 查到的MC共享记忆编号。
        """
        return await self._shared_memory.forget(event, self.bridge, memory_id)

    @staticmethod
    def _is_memory_query(text):
        if re.search(r"记忆|回忆|还记得|你记得|聊过|发生过", text):
            return True
        return bool(re.search(r"昨天|昨晚|前天|上次|之前", text) and re.search(r"吗|什么|怎么|多少|哪|做了|干了", text))
