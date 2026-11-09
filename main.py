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

from .chat_policy import bound_history, history_budget, estimate_tokens, memory_needed, tool_group, action_intent, is_confirmation, READONLY, SURVIVAL as SURVIVAL_TOOL_NAMES, SIMPLE as SIMPLE_TOOLS
from .telemetry import Telemetry, Trace, ToolHooks, CURRENT_TRACE, observed_context
from .scheduler import UnifiedScheduler, Priority, TaskState, ScheduledTask
from .skills import VanillaSkills, SkillError, parse_transfer_items
from .contracts import L0_OPCODE_NAMES, L1_SPECS, L2_SPECS
from .task_metrics import TaskMetrics
from .task_journal import TaskJournal
from .intent import classify_task
from .task_recovery import TaskRecovery
from .task_checkpoint import baseline_from_status, clean_checkpoint, resume_plan, validate_resume
from .break_policy import BreakPolicy
from .short_history import ShortHistoryStore
from .poi_memory import PoiMemory

from .reply_route import ReplyRoute, REPLY_ROUTE, UNSET, event_route, game_route
from .vision import vision_input

PLUGIN_NAME = "astrbot_plugin_mc_bridge"
CONTROL_EPOCH = ContextVar("mc_control_epoch", default=None)
RESOURCE_MENTION = {
    "coal": ("煤", "coal"),
    "iron": ("铁", "iron"),
    "stone": ("石头", "圆石", "石料", "stone"),
    "logs": ("木", "树", "原木", "log", "wood"),
    "wheat": ("小麦", "wheat"),
}


def require_mc_tool(name):
    def decorate(handler):
        @wraps(handler)
        async def guarded(self, event, *args, **kwargs):
            token = None
            reply_token = None
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
        return guarded
    return decorate


class BridgeError(RuntimeError):
    """Raised when the Minecraft client cannot execute an action."""


def classify_goal_outcome(result, kind: str = "") -> str:
    """Separate execution completion from goal fulfilment for scheduler metrics."""
    kind = str(kind or "")
    if result is None:
        return "fulfilled" if kind in {"goto", "come"} else "unknown"
    if not isinstance(result, dict):
        return "unknown"
    explicit = str(result.get("goal_outcome") or "").strip().lower()
    if explicit in {"fulfilled", "partial", "unmet", "unknown"}:
        return explicit
    if result.get("partial") is True:
        return "partial" if result.get("collected") not in (0, 0.0, None) else "unmet"
    items = result.get("items")
    if isinstance(items, list) and items:
        moved = 0
        for item in items:
            if isinstance(item, dict):
                try:
                    moved += int(item.get("moved", 0) or 0)
                except (TypeError, ValueError):
                    pass
        try:
            requested = int(result.get("requested", 0) or 0)
        except (TypeError, ValueError):
            requested = 0
        all_ok = all(isinstance(item, dict) and item.get("ok") is True for item in items)
        if all_ok and (requested <= 0 or moved >= requested):
            return "fulfilled"
        return "partial" if moved > 0 else "unmet"
    requested = result.get("requested")
    for key in ("moved", "collected", "produced", "placed", "gained"):
        if key not in result or requested is None:
            continue
        try:
            have = float(result.get(key) or 0)
            need = float(requested or 0)
        except (TypeError, ValueError):
            continue
        if need > 0:
            return "fulfilled" if have >= need else ("partial" if have > 0 else "unmet")
    status = str(result.get("result") or result.get("status") or "").strip().lower()
    if status in {"retreated", "retreat", "partial"}:
        return "partial"
    if status in {"unreachable", "timeout", "no_target", "no_frontier", "failed", "blocked", "unmet"}:
        return "unmet"
    if result.get("errors"):
        return "partial"
    if result.get("complete") is False:
        return "partial"
    if result.get("success") is False:
        return "unmet"
    return "fulfilled"


@dataclass
class QueuedTask:
    kind: str
    goal: str
    target: str = ""
    amount: int = 0
    x: int = 0
    y: int = 0
    z: int = 0
    reply_route: ReplyRoute | None = None
    task_id: str = ""
    priority: int = int(Priority.NORMAL)
    attempts: int = 0
    max_attempts: int = 1
    resources: tuple[str, ...] = ("actor",)
    dependencies: tuple[str, ...] = ()
    deadline: float = 0.0
    preemptible: bool = False
    params: dict = field(default_factory=dict)
    goal_outcome: str = ""
    budget_seconds: float = 0.0
    checkpoint: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)
    group_id: str = ""
    suppress_notify: bool = False


class BridgeServer:
    """One-client WebSocket server + request/response correlation."""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        token: str,
        on_chat=None,
        on_disconnect=None,
        chat_priority=None,
        on_chat_overflow=None,
        chat_pending_limit=4,
    ) -> None:
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else "/" + path
        self.token = token
        self.on_chat = on_chat
        self.on_disconnect = on_disconnect
        self.on_respawn=None
        self.chat_priority=chat_priority
        self.on_chat_overflow=on_chat_overflow
        self.chat_pending_limit=max(1,min(8,int(chat_pending_limit)))
        self._overflow_job=None
        self.session_id = ""
        self.app: web.Application | None = None
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.ws: web.WebSocketResponse | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        self._chat_jobs: set[asyncio.Task] = set()
        self.client_info: dict[str, Any] = {}
        self.connected_at: float = 0.0
        self.recent_chat: deque[dict[str, Any]] = deque(maxlen=80)

    async def start(self) -> None:
        self.app = web.Application(client_max_size=32 * 1024 * 1024)
        self.app.router.add_get(self.path, self._ws_handler)
        self.app.router.add_get("/debug/status", self._debug_status)
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        logger.info(
            f"[mc-bridge] WebSocket listening on ws://{self.host}:{self.port}{self.path}"
        )

    def _finish_chat_job(self, job):
        self._chat_jobs.discard(job)
        if not job.cancelled() and job.exception() is not None:
            logger.warning("[mc-bridge] chat job failed: %s", type(job.exception()).__name__)

    async def stop(self) -> None:
        jobs = list(self._chat_jobs)
        if self._overflow_job is not None and not self._overflow_job.done():jobs.append(self._overflow_job)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self._chat_jobs.clear()
        await self._fail_pending("bridge stopping")
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self.runner is not None:
            await self.runner.cleanup()
        self.runner = self.site = None

    @property
    def connected(self) -> bool:
        return self.ws is not None and not self.ws.closed

    async def _fail_pending(self, reason: str) -> None:
        pending, self.pending = self.pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(BridgeError(reason))

    def _check_token(self, request: web.Request) -> bool:
        supplied = request.query.get("token", "")
        if not supplied:
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
        return bool(self.token) and hmac.compare_digest(str(supplied), str(self.token))

    async def _debug_status(self, request: web.Request) -> web.Response:
        if not self._check_token(request):
            return web.json_response({"ok": False, "error": "invalid token"}, status=401)
        try:
            data = await self.call("status", timeout=10)
            return web.json_response({"ok": True, "data": data})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=503)

    async def _ws_handler(self, request: web.Request) -> web.StreamResponse:
        if not self._check_token(request):
            return web.Response(status=401, text="invalid token")
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        async with self._connection_lock:
            previous, self.ws = self.ws, None
            await self._fail_pending("client reconnected")
            if previous is not None and not previous.closed:
                await previous.close()
            if previous is not None and self.on_disconnect is not None:
                await self.on_disconnect()
            self.ws = ws
            self.session_id = uuid.uuid4().hex
            self.connected_at = time.time()
            self.client_info = {}
        logger.info("[mc-bridge] client connected")
        try:
            async for msg in ws:
                if self.ws is not ws:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        await self._on_message(msg.data)
                    except Exception as exc:
                        logger.warning("[mc-bridge] bad message: %s", type(exc).__name__)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        finally:
            if self.ws is ws:
                self.ws = None
                self.session_id = ""
                await self._fail_pending("client disconnected")
                if self.on_disconnect is not None:
                    await self.on_disconnect()
                logger.info("[mc-bridge] client disconnected")
        return ws

    async def _on_message(self, raw: str) -> None:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise BridgeError("invalid message envelope")
        typ = data.get("type")
        if typ == "result":
            fut = self.pending.pop(str(data.get("id", "")), None)
            if fut is not None and not fut.done():
                if data.get("ok"):
                    fut.set_result(data.get("data", {}))
                else:
                    fut.set_exception(BridgeError(str(data.get("error") or "action failed")))
        elif typ == "hello":
            self.client_info = data
            logger.info("[mc-bridge] client protocol=%s", data.get("protocol", 1))
        elif typ=='event' and data.get('event')=='normal_respawn':
            payload=data.get('data',{})
            if not isinstance(payload,dict) or payload.get('reason') not in ('player_died','actor_replaced'):return
            callback=getattr(self,'on_respawn',None)
            if callback is not None:callback(payload)
        elif typ=='event' and data.get('event')=='task_update':
            pass
        elif typ == "event" and data.get("event") == "chat":
            payload = data.get("data")
            if not isinstance(payload, dict) or self.on_chat is None:
                return
            payload = dict(payload, _connection_id=self.session_id)
            priority=bool(self.chat_priority and self.chat_priority(payload))
            limit=self.chat_pending_limit+(1 if priority else 0)
            if len(self._chat_jobs)>=limit:
                if self.on_chat_overflow is not None and not priority and (self._overflow_job is None or self._overflow_job.done()):
                    self._overflow_job=asyncio.create_task(self.on_chat_overflow(payload))
                    self._overflow_job.add_done_callback(self._finish_chat_job)
                return
            job = asyncio.create_task(self.on_chat(payload))
            self._chat_jobs.add(job)
            job.add_done_callback(self._finish_chat_job)

    async def call(self, action: str, params: dict | None = None, timeout: float = 20.0):
        if not self.connected:
            raise BridgeError("Minecraft 客户端没有连接（先启动机器人客户端）")
        req_id = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        try:
            async with self._send_lock:
                await self.ws.send_json(
                    {
                        "type": "action",
                        "id": req_id,
                        "action": action,
                        "params": params or {},
                    }
                )
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BridgeError(f"客户端 {action} 超时") from exc
        finally:
            self.pending.pop(req_id, None)


def _fmt_status(data: dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return str(data)
    lines = []
    pos = data.get("position") or {}
    if pos:
        lines.append(
            "位置: %.1f, %.1f, %.1f (%s)"
            % (pos.get("x", 0), pos.get("y", 0), pos.get("z", 0), data.get("dimension", "?"))
        )
    if "health" in data:
        try:
            hp = float(data.get("health", 0) or 0)
        except (TypeError, ValueError):
            hp = 0.0
        try:
            max_hp = float(data.get("max_health", 20) or 20)
        except (TypeError, ValueError):
            max_hp = 20.0
        extra = ""
        try:
            absorption = float(data.get("absorption", 0) or 0)
        except (TypeError, ValueError):
            absorption = 0.0
        if absorption > 0:
            extra = f" + {absorption / 2:.1f} 吸收"
        lines.append(f"血量: {hp / 2:.1f}/{max_hp / 2:.1f} 颗心{extra}（{hp:.1f} 点）")
    if "food" in data:
        try:
            food = float(data.get("food", 0) or 0)
        except (TypeError, ValueError):
            food = 0.0
        lines.append(f"饱食: {food / 2:.1f}/10 格（{food:.0f}/20）")
    if data.get("armor") is not None:
        lines.append(f"护甲: {data.get('armor')}")
    if data.get("held"):
        lines.append(f"手持: {data.get('held')}")
    protection = data.get("navigation_protection") or {}
    if protection.get("break_monitoring") or protection.get("break_budget_exhausted"):
        suffix = "，已用完" if protection.get("break_budget_exhausted") else ""
        lines.append(
            f"导航破坏预算: {int(protection.get('break_count', 0) or 0)}/"
            f"{int(protection.get('break_limit', 0) or 0)}{suffix}"
        )
    if data.get("server"):
        lines.append(f"服务器: {data.get('server')}")
    inv = data.get("inventory") or []
    if inv:
        lines.append("背包: " + ", ".join(str(x) for x in inv[:12]))
    players = data.get("nearby_players") or []
    if players:
        lines.append("附近玩家: " + ", ".join(str(x) for x in players[:8]))
    entities = data.get("nearby_entities") or []
    if entities:
        lines.append("附近实体: " + ", ".join(str(x) for x in entities[:8]))
    hostiles = data.get("hostiles") or []
    if hostiles:
        parts = []
        for hostile in hostiles[:8]:
            if isinstance(hostile, dict):
                name = hostile.get("name") or hostile.get("type") or "?"
                dist = hostile.get("distance")
                if isinstance(dist, (int, float)):
                    parts.append(f"{name}({dist:.1f}m)")
                else:
                    parts.append(str(name))
        if parts:
            lines.append("附近敌人: " + ", ".join(parts))
    return "\n".join(lines) or "客户端没有返回状态"


class McBridgePlugin(Star):
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
            chat_priority=self._priority_game_chat,
            on_chat_overflow=self._game_chat_overflow,
            chat_pending_limit=self.config.get("game_chat_pending_limit",4),
        )
        self.bridge.on_respawn=self._respawn_signal
        self._started = False
        self._shared_memory = None
        self._closing = False
        self._manual_movement = False
        self._control_epoch = 0
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

    def _notice_routes(self):
        routes=[]
        current=getattr(self,'_task_current',None)
        if current is not None:routes.append(getattr(current,'reply_route',None))
        routes.extend(getattr(t,'reply_route',None) for t in getattr(self,'_task_queue',[]))
        routes.extend(getattr(self,'_agent_notice_routes',{}).values())
        if getattr(self,'_manual_movement',False):routes.append(getattr(self,'_manual_notice_route',None))
        return list(dict.fromkeys(r for r in routes if r is not None))

    def _current_notice_route(self):
        current=getattr(self,'_task_current',None)
        if current is not None:return getattr(current,'reply_route',None)
        active=getattr(self,'_active_agent_task',None)
        if active is not None:return getattr(self,'_agent_notice_routes',{}).get(active)
        if getattr(self,'_manual_movement',False):return getattr(self,'_manual_notice_route',None)
        return game_route(self.bridge)

    def _enqueue_routed(self,tasks,route):
        return self._enqueue_tasks([replace(task,reply_route=route) for task in tasks])

    async def _task_chat(self,text,*,route=UNSET,kind='task'):
        text=' '.join(str(text or '').split())[:240]
        route=REPLY_ROUTE.get() if route is UNSET else route
        if not text or self._closing or route is None:return False
        trace=Trace(getattr(self,'_telemetry',None),'notice',route.target or 'minecraft:gamechat')
        trace.data['context']={'channel':route.channel,'notice_kind':kind,'text_chars':len(text)}
        epoch=CONTROL_EPOCH.get()
        if kind=='task' and epoch is not None and epoch!=self._control_epoch:
            await trace.finish('stale_notice');return False
        try:
            if route.channel=='qq':
                if not re.fullmatch(r'.+:FriendMessage:\d+',route.target):
                    await trace.finish('invalid_route');return False
                result=await self.context.send_message(route.target,MessageChain().message(text))
                if result is not True:
                    await trace.finish('unconfirmed');return False
                await trace.delivered(stage='qq_api_ack')
            elif route.channel=='session':
                if not route.target:
                    await trace.finish('invalid_route');return False
                result=await self.context.send_message(route.target,MessageChain().message(text))
                if result is not True:
                    await trace.finish('unconfirmed');return False
                await trace.delivered(stage='session_api_ack')
            elif route.channel=='minecraft':
                if not self.bridge.connected or route.connection_id!=self.bridge.session_id:
                    await trace.finish('stale_or_disconnected');return False
                result=await self.bridge.call('chat',{'text':text},timeout=5)
                if self._err(result):
                    await trace.finish('send_failed');return False
                await trace.delivered(stage='game_ack')
            else:
                await trace.finish('invalid_route');return False
        except asyncio.CancelledError:
            await trace.finish('cancelled');raise
        except Exception as exc:
            await trace.finish('send_failed',exc)
            logger.warning('[mc-bridge] notice failed channel=%s type=%s',route.channel,type(exc).__name__)
            return False
        shared=getattr(self,'_shared_memory',None)
        if shared is not None:
            try:await shared.capture_event(text)
            except Exception as exc:logger.warning('[mc-bridge] event capture deferred: %s',type(exc).__name__)
        return True

    @staticmethod
    def _is_stop_request(text: str) -> bool:
        if any(w in text for w in ("停下", "停止", "别砍", "别挖", "取消任务", "取消", "回来吧")):
            return True
        return bool(re.search(r"(^|\s)停($|\s)", text))

    def _enqueue_tasks(self, tasks: list[QueuedTask], *, priority: int = int(Priority.OWNER)) -> bool:
        tasks = [task for task in tasks if task is not None]
        if self._closing or not tasks or len(self._task_queue) + len(tasks) > 8:
            return False
        self._manual_movement = False
        tasks=[replace(task,reply_route=task.reply_route or REPLY_ROUTE.get()) for task in tasks]
        scheduler = self._scheduler_for()
        for task in tasks:
            task.priority = int(task.priority if task.priority != int(Priority.NORMAL) else priority)
            task.task_id = task.task_id or scheduler.new_id(task.kind or "task")
            task.group_id = task.group_id or task.task_id
            budget = float(getattr(task, "budget_seconds", 0) or 0)
            scheduler.register(
                task_id=task.task_id,
                kind=task.kind,
                goal=task.goal,
                payload=task,
                priority=task.priority,
                resources=task.resources,
                dependencies=task.dependencies,
                max_attempts=task.max_attempts,
                deadline=task.deadline,
                route=task.reply_route,
                preemptible=task.preemptible,
                lease_seconds=max(600.0, budget + 60.0) if budget > 0 else 600.0,
            )
        self._manual_notice_route=None
        self._task_queue.extend(tasks)
        if self._active_task is None or self._active_task.done():
            self._task_stop.clear()
            self._active_task = asyncio.create_task(self._task_worker())
        return True

    def _queue_skill(self, label: str, name: str, **params) -> bool:
        spec = self._agent_skill_spec(name)
        resources = tuple(resource.value for resource in spec.resources) if spec is not None else ("actor",)
        budget = float(spec.max_seconds) if spec is not None else 0.0
        return self._enqueue_tasks([QueuedTask(
            kind="skill",
            goal=label,
            target=name,
            params=params,
            resources=resources,
            budget_seconds=budget,
        )])

    async def _gather_guard(self, event, resource: str) -> str | None:
        aliases = {"煤": "coal", "煤炭": "coal", "铁": "iron", "铁矿": "iron", "石头": "stone", "圆石": "stone", "石料": "stone", "木头": "logs", "原木": "logs", "树": "logs", "小麦": "wheat"}
        resource = str(resource or "logs").strip().lower()
        resource = aliases.get(resource, resource)
        text = str(getattr(event, "message_str", "") or "")
        last = str(getattr(self, "_last_gather_resource", "") or "")
        last_at = float(getattr(self, "_last_gather_resource_at", 0.0) or 0.0)
        if last and last != resource and time.monotonic() - last_at < 600 and not any(word in text for word in RESOURCE_MENTION.get(resource, ())):
            return f"上一轮采集目标是{last}，这句话没有明确改挖{resource}；已拒绝换成{resource}"
        return None

    def _remember_gather_resource(self, resource: str) -> None:
        self._last_gather_resource = str(resource or "logs").strip().lower()
        self._last_gather_resource_at = time.monotonic()

    @staticmethod
    def _agent_skill_spec(opcode: str):
        opcode = str(opcode or "").strip()
        for spec in list(L1_SPECS.values()) + list(L2_SPECS.values()):
            if spec.opcode.value == opcode:
                return spec
        return None

    def _agent_task_preview(self, opcode: str, params: dict, priority: int = int(Priority.NORMAL), depends_on: str = "") -> dict:
        spec = self._agent_skill_spec(opcode)
        if spec is None:
            raise SkillError("未知 L1/L2 技能：" + str(opcode))
        params = dict(params or {})
        _, params = self._skills_for().validate_params(spec.opcode.value, params)
        priority = int(priority)
        if not 0 <= priority <= 40:
            raise SkillError("priority 必须在 0-40")
        dependency = str(depends_on or "").strip()
        if dependency:
            dep = self._scheduler_for().get(dependency)
            if dep is None:
                raise SkillError("依赖任务不存在：" + dependency)
            if dep.state in {TaskState.FAILED, TaskState.CANCELLED}:
                raise SkillError("依赖任务已失败或取消：" + dependency)
        return {
            "opcode": spec.opcode.value,
            "params": params,
            "priority": priority,
            "resources": sorted(resource.value for resource in spec.resources),
            "max_seconds": spec.max_seconds,
            "preemptible": priority >= int(Priority.AUTONOMY),
            "dependencies": [dependency] if dependency else [],
        }

    async def _preempt_active_task(self):
        scheduler = self._scheduler_for()
        active = scheduler.active
        if active is None:
            return None
        worker = self._active_task
        self._preempting = True
        try:
            scheduler.cancel(active.id, "preempted by higher priority", propagate=True)
            self._task_current = None
            self._active_task = None
            if worker is not None and not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            return await self._send_stop()
        finally:
            self._preempting = False

    async def _start_agent_task(self, opcode: str, params: dict, priority: int = int(Priority.NORMAL), depends_on: str = "", suppress_notify: bool = False) -> str:
        preview = self._agent_task_preview(opcode, params, priority, depends_on)
        scheduler = self._scheduler_for()
        if scheduler.should_preempt(int(preview["priority"]), preview["resources"]):
            error = await self._preempt_active_task()
            if error:
                raise SkillError("抢占停止未确认：" + error)
        task_id = scheduler.new_id("task")
        group_id = task_id
        if depends_on:
            dependency = scheduler.get(str(depends_on))
            if dependency is not None:
                group_id = str(getattr(dependency.payload, "group_id", "") or dependency.id or depends_on)
            else:
                group_id = str(depends_on)
        summary = json.dumps(preview["params"], ensure_ascii=False, separators=(",", ":"))
        task = QueuedTask(
            kind="skill",
            goal=f"{preview['opcode']} {summary}"[:160],
            target=preview["opcode"],
            params=preview["params"],
            priority=int(preview["priority"]),
            task_id=task_id,
            resources=tuple(preview["resources"]),
            dependencies=tuple(preview["dependencies"]),
            deadline=0.0,
            budget_seconds=float(preview["max_seconds"]),
            max_attempts=1,
            preemptible=bool(preview["preemptible"]),
            group_id=group_id,
            suppress_notify=bool(suppress_notify),
        )
        if not self._enqueue_tasks([task], priority=int(preview["priority"])):
            raise SkillError("任务队列已满或正在停止")
        return task_id

    async def _resume_recovery_group(self, entry: dict, priority: int, dry_run: bool = False) -> dict:
        recovery = self._task_recovery_for()
        group_id = str(entry.get("group_id") or entry.get("recovery_id") or "")
        rows = recovery.group(group_id) if group_id else []
        if not rows:
            rows = [dict(entry)]
        try:
            current_status = await self._call("status", timeout=10)
            status_error = self._err(current_status)
        except Exception as exc:
            current_status = {}
            status_error = str(exc) or type(exc).__name__
        if status_error:
            return {"accepted": False, "error": "恢复前无法读取客户端状态：" + status_error,
                    "resumed_from": group_id}
        warnings = []
        for row in rows:
            allowed, blocking, warning = validate_resume(row, current_status)
            if not allowed:
                return {"accepted": False, "error": blocking, "resumed_from": group_id}
            if warning:
                warnings.append(warning)
        scheduler = self._scheduler_for()
        plans = []
        for row in rows:
            opcode = str(row.get("opcode") or "")
            plan = resume_plan(opcode, row.get("params") or {}, row.get("checkpoint") or {})
            plans.append((row, plan))
        if all(plan.get("mode") == "complete" for _, plan in plans):
            recovery.discard_group(group_id)
            return {"accepted": False, "complete": True, "resumed_from": group_id,
                    "resume_mode": "checkpoint", "note": "检查点显示该任务已经完成"}
        id_map = {
            row["recovery_id"]: scheduler.new_id("task")
            for row, plan in plans if plan.get("mode") != "complete"
        }
        tasks = []
        for row, plan in plans:
            if plan.get("mode") == "complete":
                continue
            opcode = str(row.get("opcode") or "")
            params = plan.get("params") or {}
            remaining = float(row.get("remaining_seconds") or 0)
            if remaining > 0 and "max_seconds" in params:
                try:
                    params["max_seconds"] = max(5, min(int(params["max_seconds"]), int(remaining)))
                except (TypeError, ValueError):
                    pass
            try:
                _, params = self._skills_for().validate_params(opcode, params)
            except SkillError as exc:
                return {"accepted": False, "error": f"{opcode}: {exc}", "resumed_from": group_id}
            spec = self._agent_skill_spec(opcode)
            resources = tuple(row.get("resources") or ())
            if not resources and spec is not None:
                resources = tuple(resource.value for resource in spec.resources)
            budget = float(spec.max_seconds) if spec is not None else 0.0
            if remaining > 0:
                budget = min(budget or remaining, remaining)
            task_priority = max(0, min(40, int(row.get("priority") or priority)))
            dependencies = [
                id_map[dependency]
                for dependency in (row.get("dependencies") or [])
                if dependency in id_map
            ]
            tasks.append(QueuedTask(
                kind="skill",
                goal=str(row.get("goal") or opcode)[:160],
                target=opcode,
                params=params,
                priority=task_priority,
                task_id=id_map[row["recovery_id"]],
                resources=resources or ("actor",),
                dependencies=tuple(dependencies),
                budget_seconds=budget,
                preemptible=task_priority >= int(Priority.AUTONOMY),
                checkpoint=dict(row.get("checkpoint") or {}),
                group_id=group_id,
            ))
        if dry_run:
            return {
                "planned": True,
                "resumed_from": group_id,
                "resume_mode": "checkpoint" if any(plan.get("mode") == "checkpoint" for _, plan in plans) else "rerun",
                "tasks": [
                    {"task_id": task.task_id, "opcode": task.target, "params": task.params,
                     "priority": task.priority, "dependencies": list(task.dependencies),
                     "remaining_seconds": task.budget_seconds}
                    for task in tasks
                ],
                "warnings": warnings,
            }
        if not tasks:
            recovery.discard_group(group_id)
            return {"accepted": False, "complete": True, "resumed_from": group_id}
        if not self._enqueue_tasks(tasks, priority=int(priority)):
            return {"accepted": False, "error": "任务队列已满或正在停止", "resumed_from": group_id}
        recovery.discard_group(group_id)
        return {
            "accepted": True,
            "task_ids": [task.task_id for task in tasks],
            "task_id": tasks[0].task_id,
            "resumed_from": group_id,
            "resume_mode": "checkpoint" if any(plan.get("mode") == "checkpoint" for _, plan in plans) else "rerun",
            "note": "按阶段检查点恢复；未确认的阶段会重新执行",
            "warnings": warnings,
            "tasks": len(tasks),
        }

    async def _await_short_task(self, task_id: str, timeout: float):
        scheduler = self._scheduler_for()
        deadline = time.monotonic() + max(0.0, float(timeout))
        terminal = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
        while True:
            status = scheduler.task_status(str(task_id))
            if status and status.get("state") in terminal:
                return status
            if time.monotonic() >= deadline:
                return status
            await asyncio.sleep(0.15)

    def _clear_inline_suppression(self, task_id: str) -> None:
        try:
            scheduled = self._scheduler_for().get(str(task_id))
            item = getattr(scheduled, "payload", None) if scheduled is not None else None
            if item is not None:
                item.suppress_notify = False
        except Exception:
            pass

    def _agent_task_status(self, task_id: str = "", detail: bool = False) -> dict:
        scheduler = self._scheduler_for()
        task_id = str(task_id or "").strip()
        if task_id:
            data = scheduler.task_status(task_id)
            return {"found": data is not None, "task": data}
        snapshot = scheduler.snapshot()
        current = getattr(self, "_task_current", None)
        current_task_id = str(getattr(current, "task_id", "") or "")
        queue = [
            {"task_id": str(getattr(item, "task_id", "") or ""), "kind": str(getattr(item, "kind", "") or ""),
             "goal": str(getattr(item, "goal", "") or ""), "target": str(getattr(item, "target", "") or "")}
            for item in list(getattr(self, "_task_queue", []))
        ]
        metrics = self._task_metrics_for().snapshot(limit=10)
        recovered = self._task_recovery_for().snapshot()
        if not detail:
            return {
                "active": snapshot["active"],
                "pending_count": snapshot["pending_count"],
                "pending": snapshot["pending"],
                "current_task_id": current_task_id,
                "queue": queue,
                "metrics": {
                    "overall": metrics["overall"],
                    "alerts": metrics["alerts"],
                },
                "last_result": getattr(self, "_last_task_result", None),
                "recovered_count": recovered["count"],
            }
        snapshot["current_task_id"] = current_task_id
        snapshot["queue"] = queue
        snapshot["metrics"] = metrics
        snapshot["last_result"] = getattr(self, "_last_task_result", None)
        snapshot["journal"] = self._task_journal_for().snapshot(limit=10)
        snapshot["recovered"] = recovered
        return snapshot

    def _cancel_agent_task(self, task_id: str = "") -> dict:
        scheduler = self._scheduler_for()
        task_id = str(task_id or "").strip()
        if not task_id:
            self._cancel_active_task()
            try:
                self._task_recovery_for().clear()
            except Exception:
                pass
            return {"cancelled": "all", "queue_cleared": True, "recovery_cleared": True}
        task = scheduler.get(task_id)
        if task is None:
            if self._task_recovery_for().discard(task_id):
                return {"found": True, "task_id": task_id, "recovered": True, "discarded": True}
            return {"found": False, "task_id": task_id}
        if task.state == TaskState.RUNNING:
            self._cancel_active_task()
            return {"found": True, "task_id": task_id, "active": True, "queue_cleared": True}
        scheduler.cancel(task_id, "task cancelled by agent")
        self._task_queue = deque(
            item for item in self._task_queue
            if not (scheduler.get(str(getattr(item, "task_id", "") or "")) is not None
                    and scheduler.get(str(getattr(item, "task_id", "") or "")).terminal)
        )
        return {"found": True, "task_id": task_id, "active": False, "queue_cleared": False}

    def _scheduler_for(self) -> UnifiedScheduler:
        scheduler = getattr(self, "_scheduler", None)
        if scheduler is None:
            scheduler = UnifiedScheduler()
            self._scheduler = scheduler
        return scheduler

    @staticmethod
    def _weapon_rank(item_id: str) -> int:
        tiers = {"netherite": 5, "diamond": 4, "iron": 3, "stone": 2, "golden": 1, "wooden": 0}
        item_id = str(item_id or "")
        kind = 3 if item_id.endswith("_sword") else 2 if item_id.endswith("_axe") else 4 if item_id == "minecraft:trident" else 0
        if not kind:
            return -1
        for tier, rank in tiers.items():
            if item_id.startswith(f"minecraft:{tier}_"):
                return rank * 10 + kind
        return kind

    async def _select_best_weapon(self) -> str | None:
        status = await self._call("status", timeout=10)
        if self._err(status):
            return None
        rows = [row for row in (status.get("inventory_items") or []) if isinstance(row, dict)]
        if not rows:
            return None
        best = max(rows, key=lambda row: self._weapon_rank(str(row.get("id", ""))))
        if self._weapon_rank(str(best.get("id", ""))) < 0:
            return None
        try:
            slot = int(best.get("slot", -1))
        except (TypeError, ValueError):
            return None
        if slot < 0:
            return None
        if slot < 9:
            await self._call("select_slot", {"slot": slot}, timeout=10)
        else:
            result = await self._run_operation("inventory", {"operation": "swap", "slot": slot, "hotbar": 0}, seconds=6)
            if self._err(result):
                return None
            await self._call("select_slot", {"slot": 0}, timeout=10)
        return str(best.get("id", ""))

    def _skills_for(self) -> VanillaSkills:
        skills = getattr(self, "_skills", None)
        if skills is None:
            skills = VanillaSkills(self)
            skills.checkpoint = self._on_skill_checkpoint
            self._skills = skills
        return skills

    def _on_skill_checkpoint(self, skill: str, stage: str, **detail) -> None:
        item = getattr(self, "_task_current", None)
        if item is None or str(getattr(item, "kind", "") or "") != "skill":
            return
        checkpoint = dict(getattr(item, "checkpoint", {}) or {})
        previous_stage = str(checkpoint.get("stage") or "")
        checkpoint.update({
            "skill": str(skill or "")[:40],
            "stage": str(stage or "")[:80],
            "completed": max(0, int(detail.get("completed", 0) or 0)),
            "total": max(0, int(detail.get("total", 0) or 0)),
            "detail": detail.get("detail") if isinstance(detail.get("detail"), dict) else {
                key: value for key, value in detail.items()
                if key not in {"completed", "total"}
            },
            "at": time.time(),
        })
        item.checkpoint = clean_checkpoint(checkpoint)
        now = time.monotonic()
        if stage != previous_stage or now - float(getattr(self, "_last_recovery_persist", 0.0) or 0.0) >= 1.0:
            self._last_recovery_persist = now
            self._persist_recovery_snapshot()

    async def _capture_task_baseline(self, item) -> None:
        if str(getattr(item, "kind", "") or "") != "skill":
            return
        try:
            status = await self._call("status", timeout=10)
            if not self._err(status):
                item.baseline = baseline_from_status(status)
        except Exception:
            pass

    def _poi_memory_for(self) -> PoiMemory:
        memory = getattr(self, "_poi_memory", None)
        if memory is None:
            memory = PoiMemory(Path(StarTools.get_data_dir(PLUGIN_NAME)) / "poi_memory.json")
            self._poi_memory = memory
        return memory

    def _break_policy_for(self) -> BreakPolicy:
        policy = getattr(self, "_break_policy", None)
        if policy is None:
            try:
                per_action = int(self.config.get("break_budget_per_action", 8) or 8)
            except (TypeError, ValueError):
                per_action = 8
            try:
                per_task = int(self.config.get("break_budget_per_task", 64) or 64)
            except (TypeError, ValueError):
                per_task = 64
            policy = BreakPolicy(
                Path(StarTools.get_data_dir(PLUGIN_NAME)) / "protected_regions.json",
                per_action=per_action,
                per_task=per_task,
                allow_unnatural=bool(self.config.get("break_allow_unnatural", False)),
            )
            self._break_policy = policy
        return policy

    async def _guard_break_action(self, action: str, params: dict | None) -> str | None:
        action = str(action or "")
        params = params if isinstance(params, dict) else {}
        break_actions = {"mine_at", "mine_block", "dig_down", "mine_below"}
        if action == "navigate" and params.get("allow_break"):
            try:
                status = await self._call("status", timeout=8)
                position = status.get("position") or {}
                dimension = str(status.get("dimension") or "")
                target = (int(params.get("x", position.get("x", 0))), int(params.get("y", position.get("y", 0))), int(params.get("z", position.get("z", 0))))
                policy = self._break_policy_for()
                start = (int(position.get("x", target[0])), int(position.get("y", target[1])), int(position.get("z", target[2])))
                low = (min(start[0], target[0]) - 8, min(start[1], target[1]) - 8, min(start[2], target[2]) - 8)
                high = (max(start[0], target[0]) + 8, max(start[1], target[1]) + 8, max(start[2], target[2]) + 8)
                if policy.intersects(low, high, dimension):
                    params["allow_break"] = False
                    return None
                allowed, _ = policy.preflight(target[0], target[1], target[2], "minecraft:stone",
                                              dimension=dimension)
                if not allowed:
                    params["allow_break"] = False
            except Exception:
                pass
            return None
        if action not in break_actions:
            return None
        status = {}
        try:
            status = await self._call("status", timeout=8)
        except Exception:
            pass
        dimension = str(status.get("dimension") or "")
        x = int(params.get("x", 0))
        y = int(params.get("y", 0))
        z = int(params.get("z", 0))
        count = 1
        targets = [(x, y, z)]
        if action in {"dig_down", "mine_below"}:
            try:
                count = max(1, min(8, int(params.get("depth", 3) or 3)))
            except (TypeError, ValueError):
                count = 3
            position = status.get("position") or {}
            x = int(position.get("x", 0))
            y = int(position.get("y", 0)) - 1
            z = int(position.get("z", 0))
            targets = [(x, y - offset, z) for offset in range(count)]
        policy = self._break_policy_for()
        block_ids = []
        for tx, ty, tz in targets:
            block_id = ""
            try:
                context = await self._call("block_context", {"x": tx, "y": ty, "z": tz}, timeout=8)
                block_id = str(context.get("block") or "")
            except Exception:
                block_id = ""
            if not block_id:
                return "无法确认目标方块类型，已拒绝破坏"
            allowed, reason = policy.preflight(tx, ty, tz, block_id, dimension=dimension)
            if not allowed:
                return reason
            block_ids.append(block_id)
        task_id = str(getattr(getattr(self, "_task_current", None), "task_id", "") or "direct")
        ok, reason = policy.check(
            targets[0][0], targets[0][1], targets[0][2], block_ids[0],
            count=count, dimension=dimension, task_id=task_id,
        )
        if not ok:
            return reason
        return None

    def _register_build_region(self, name: str, minimum, maximum, dimension: str = "") -> None:
        try:
            self._break_policy_for().add_box(name, minimum, maximum, kind="building", dimension=dimension)
        except Exception:
            pass

    def _register_container(self, x: int, y: int, z: int, dimension: str = "") -> None:
        try:
            self._break_policy_for().add_around(
                f"container-{int(x)}-{int(y)}-{int(z)}", int(x), int(y), int(z),
                radius=2, kind="container", dimension=dimension,
            )
        except Exception:
            pass

    def _task_metrics_for(self) -> TaskMetrics:
        metrics = getattr(self, "_task_metrics", None)
        if metrics is None:
            metrics = TaskMetrics(Path(StarTools.get_data_dir(PLUGIN_NAME)) / "task_metrics.jsonl")
            self._task_metrics = metrics
        return metrics

    def _task_journal_for(self) -> TaskJournal:
        journal = getattr(self, "_task_journal", None)
        if journal is None:
            journal = TaskJournal(Path(StarTools.get_data_dir(PLUGIN_NAME)) / "task_journal.jsonl")
            self._task_journal = journal
        return journal

    def _task_recovery_for(self) -> TaskRecovery:
        recovery = getattr(self, "_task_recovery", None)
        if recovery is None:
            recovery = TaskRecovery(Path(StarTools.get_data_dir(PLUGIN_NAME)) / "task_recovery.json")
            self._task_recovery = recovery
        return recovery

    def _short_history_for(self) -> ShortHistoryStore:
        history = getattr(self, "_short_history", None)
        if history is None:
            history = ShortHistoryStore(Path(StarTools.get_data_dir(PLUGIN_NAME)) / "short_history.json")
            self._short_history = history
        return history

    def _persist_recovery_snapshot(self) -> None:
        try:
            rows = []
            candidates = []
            current = getattr(self, "_task_current", None)
            if current is not None:
                candidates.append(current)
            candidates.extend(list(getattr(self, "_task_queue", [])))
            valid_targets = {spec.opcode.value for spec in L1_SPECS.values()}
            valid_targets.update(spec.opcode.value for spec in L2_SPECS.values())
            seen = set()
            for item in candidates:
                if str(getattr(item, "kind", "") or "") != "skill":
                    continue
                target = str(getattr(item, "target", "") or "")
                if target not in valid_targets:
                    continue
                task_id = str(getattr(item, "task_id", "") or "")
                if not task_id or task_id in seen:
                    continue
                seen.add(task_id)
                deadline = float(getattr(item, "deadline", 0) or 0)
                remaining = max(1.0, min(3600.0, deadline - time.monotonic())) if deadline else 60.0
                rows.append({
                    "recovery_id": task_id,
                    "group_id": str(getattr(item, "group_id", "") or task_id),
                    "opcode": target,
                    "params": dict(getattr(item, "params", {}) or {}),
                    "priority": int(getattr(item, "priority", int(Priority.NORMAL)) or int(Priority.NORMAL)),
                    "resources": list(getattr(item, "resources", ()) or ()),
                    "dependencies": list(getattr(item, "dependencies", ()) or ()),
                    "goal": str(getattr(item, "goal", "") or ""),
                    "remaining_seconds": remaining,
                    "checkpoint": clean_checkpoint(getattr(item, "checkpoint", {})),
                    "baseline": getattr(item, "baseline", {}) or {},
                })
            if rows:
                self._task_recovery_for().merge(rows)
        except Exception:
            pass

    def _flush_scheduler_audit(self) -> None:
        try:
            scheduler = getattr(self, "_scheduler", None)
            if scheduler is None:
                return
            cursor = int(getattr(self, "_audit_cursor", 0) or 0)
            rows = [row for row in scheduler.audit if int(row.get("seq", 0) or 0) > cursor]
            if not rows:
                return
            self._task_journal_for().record_many(rows)
            self._audit_cursor = max(int(row.get("seq", 0) or 0) for row in rows)
        except Exception:
            pass

    def _record_task_metric(self, item, status: str, started: float, result=None, error: str = "", goal_outcome: str = "") -> None:
        try:
            self._task_metrics_for().record(
                str(getattr(item, "target", "") or getattr(item, "kind", "") or "task"),
                status,
                time.monotonic() - float(started),
                task_id=str(getattr(item, "task_id", "") or ""),
                priority=int(getattr(item, "priority", int(Priority.NORMAL)) or int(Priority.NORMAL)),
                error=str(error or ""),
                result=result,
                goal_outcome=goal_outcome,
            )
        except Exception:
            pass

    async def _task_error_text(self, exc: Exception) -> str:
        raw = str(exc).strip() or type(exc).__name__
        low = raw.lower()
        if "local task unknown or expired" in low:
            return "任务没有成功创建或已经过期，未执行任何动作。"
        if "client session changed" in low:
            return "游戏连接已经变化，旧任务已取消，不会自动恢复。"
        if "deadline" in low or "timeout" in low:
            return "任务超时仍未完成，已停止，不会自动续跑。"
        if "not connected" in low or "disconnected" in low:
            return "Minecraft 客户端没有连接，请先启动或重连机器人客户端。"
        return f"客户端返回：{raw}"

    async def _execute_task_item(self, item):
        if item.kind == "goto":
            return await self._run_goto_task(item)
        if item.kind == "come":
            await self._run_come_task(item)
            return None
        if item.kind == "skill":
            return await self._skills_for().run(str(item.target or ""), getattr(item, "params", {}))
        raise BridgeError("未知任务类型")

    async def _renew_task_lease(self, item, budget):
        scheduler = self._scheduler_for()
        scheduled = scheduler.get(str(getattr(item, "task_id", "") or ""))
        interval = max(5.0, min(30.0, budget / 3.0)) if budget > 0 else 30.0
        while True:
            await asyncio.sleep(interval)
            if scheduled is not None:
                scheduler.renew(scheduled)

    async def _run_with_lease(self, item, budget):
        monitor = asyncio.create_task(self._renew_task_lease(item, budget))
        try:
            if budget > 0:
                try:
                    return await asyncio.wait_for(self._execute_task_item(item), timeout=budget)
                except asyncio.TimeoutError as exc:
                    raise BridgeError(f"task timeout: execution budget exceeded ({budget:.1f}s)") from exc
            return await self._execute_task_item(item)
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    async def _task_worker(self) -> None:
        token = CONTROL_EPOCH.set(self._control_epoch)
        generation = getattr(self, "_worker_generation", 0)
        failed = False
        try:
            while self._task_queue and not self._task_stop.is_set():
                item = self._scheduler_for().take_next(self._task_queue)
                if item is None:
                    scheduler = self._scheduler_for()
                    self._task_queue = deque(
                        queued for queued in self._task_queue
                        if not (scheduler.get(str(getattr(queued, "task_id", "") or "")) is not None
                                and scheduler.get(str(getattr(queued, "task_id", "") or "")).terminal)
                    )
                    if not self._task_queue or self._task_stop.is_set():
                        break
                    await asyncio.sleep(0.2)
                    continue
                self._task_current = item
                result = None
                started = time.monotonic()
                budget = float(getattr(item, "budget_seconds", 0) or 0)
                if budget > 0:
                    item.deadline = time.monotonic() + budget
                    scheduled = self._scheduler_for().get(str(getattr(item, "task_id", "") or ""))
                    if scheduled is not None:
                        scheduled.deadline = item.deadline
                route_token=REPLY_ROUTE.set(item.reply_route)
                try:
                    self._break_policy_for().begin_task(str(getattr(item, "task_id", "") or "direct"))
                    await self._capture_task_baseline(item)
                    result = await self._run_with_lease(item, budget)
                    outcome = classify_goal_outcome(result, item.kind)
                    item.goal_outcome = outcome
                    if item.kind == "skill" and not getattr(item, "suppress_notify", False):
                        label = {"fulfilled": "技能完成", "partial": "技能部分完成", "unmet": "技能未达成"}.get(outcome, "技能结束")
                        await self._task_chat(f"{label}（{getattr(item, 'task_id', '')}）：{json.dumps(result, ensure_ascii=False, separators=(',', ':'))}")
                    self._scheduler_for().finish(item, success=True, result=result, goal_outcome=outcome)
                    self._last_task_result = {
                        "task_id": str(getattr(item, "task_id", "") or ""),
                        "kind": str(getattr(item, "kind", "") or ""),
                        "goal": str(getattr(item, "goal", "") or ""),
                        "status": "completed",
                        "goal_outcome": outcome,
                        "result": result,
                        "finished_at": time.time(),
                    }
                    self._record_task_metric(item, "completed", started, result=result, goal_outcome=outcome)
                    self._flush_scheduler_audit()
                    try:
                        self._task_recovery_for().discard(str(getattr(item, "task_id", "") or ""))
                    except Exception:
                        pass
                except asyncio.CancelledError:
                    self._record_task_metric(item, "cancelled", started, error="task cancelled", goal_outcome="unknown")
                    self._last_task_result = {
                        "task_id": str(getattr(item, "task_id", "") or ""),
                        "kind": str(getattr(item, "kind", "") or ""),
                        "goal": str(getattr(item, "goal", "") or ""),
                        "status": "cancelled",
                        "goal_outcome": "unknown",
                        "error": "task cancelled",
                        "finished_at": time.time(),
                    }
                    self._scheduler_for().cancel(str(getattr(item, "task_id", "") or ""), "task cancelled")
                    self._flush_scheduler_audit()
                    raise
                except Exception as exc:
                    failed = True
                    self._record_task_metric(item, "failed", started, error=str(exc), goal_outcome="unmet")
                    self._last_task_result = {
                        "task_id": str(getattr(item, "task_id", "") or ""),
                        "kind": str(getattr(item, "kind", "") or ""),
                        "goal": str(getattr(item, "goal", "") or ""),
                        "status": "failed",
                        "goal_outcome": "unmet",
                        "error": str(exc),
                        "finished_at": time.time(),
                    }
                    self._scheduler_for().finish(item, success=False, error=str(exc), retryable=False)
                    self._flush_scheduler_audit()
                    other_routes={t.reply_route for t in self._task_queue if t.reply_route is not None and t.reply_route!=item.reply_route}
                    self._scheduler_for().cancel_all("previous task failed; queue cancelled")
                    self._task_queue.clear()
                    await self._call("baritone", {"command": "stop"}, timeout=5)
                    if not getattr(item, "suppress_notify", False):
                        await self._task_chat(f"任务失败（{getattr(item, 'task_id', '')}），后续队列已停止：{await self._task_error_text(exc)}")
                    for destination in other_routes:
                        await self._task_chat('前一个任务未完成，你的排队任务已取消，请重新下达',route=destination,kind='cancel')
                    break
                finally:
                    REPLY_ROUTE.reset(route_token)
        except asyncio.CancelledError:
            pass
        finally:
            CONTROL_EPOCH.reset(token)
            if generation == getattr(self, "_worker_generation", 0):
                self._task_current = None
                if not getattr(self, "_preempting", False):
                    self._task_queue.clear()
                self._active_task = None

    async def _try_open_nearby_door(self):
        """Try one normal door open before falling back to destructive navigation."""
        try:
            scan = await self._call("scan", {"resource": "logs", "radius": 8}, timeout=10)
        except Exception:
            return False
        if self._err(scan):
            return False
        doors = [row for row in (scan.get("doors") or []) if isinstance(row, dict)]
        if not doors:
            return False
        try:
            door = min(doors, key=lambda row: float(row.get("distance", 999)))
            x, y, z = int(door["x"]), int(door["y"]), int(door["z"])
            distance = float(door.get("distance", 999))
        except (KeyError, TypeError, ValueError):
            return False
        if distance > 4.0:
            error = await self._approach_target(x, y, z, seconds=8, reach=3.0)
            if error:
                return False
        try:
            await self._run_operation("interact", {"operation": "open", "x": x, "y": y, "z": z}, seconds=6)
            return True
        except Exception:
            return False

    async def _goto_candidates(self, x, y, z):
        """Return standable cells near the target, nearest first.

        A valid coordinate can still be unstandable, for example the mouth of a
        shaft where the floor is many blocks below.  Probe the target block
        first, then the target column up/down and finally four adjacent columns;
        all probes are bounded so a failed move cannot stall the task queue.
        """
        target = (int(x), int(y), int(z))
        found = []
        seen = {target}

        async def probe(px: int, py: int, pz: int) -> None:
            try:
                context = await self._call("block_context", {"x": px, "y": py, "z": pz}, timeout=8)
            except Exception:
                return
            if self._err(context):
                return
            for row in context.get("stands") or []:
                if not isinstance(row, dict):
                    continue
                try:
                    candidate = (
                        int(row["x"]),
                        int(round(float(row.get("feet_y", row.get("y", py))))),
                        int(row["z"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if candidate not in seen:
                    seen.add(candidate)
                    found.append(candidate)

        limit = 6
        await probe(*target)
        for delta in range(1, 13):
            if len(found) >= limit:
                break
            await probe(target[0], target[1] - delta, target[2])
        for delta in range(1, 5):
            if len(found) >= limit:
                break
            await probe(target[0], target[1] + delta, target[2])
        if len(found) < 2:
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if len(found) >= limit:
                    break
                await probe(target[0] + dx, target[1], target[2] + dz)
                for delta in range(1, 9):
                    if len(found) >= limit:
                        break
                    await probe(target[0] + dx, target[1] - delta, target[2] + dz)
        found.sort(key=lambda cell: (
            (cell[0] - target[0]) ** 2 + (cell[1] - target[1]) ** 2 + (cell[2] - target[2]) ** 2,
            abs(cell[1] - target[1]),
        ))
        return [target] + found

    async def _run_goto_task(self, item: QueuedTask) -> dict:
        await self._task_chat(f"移动到 {item.x} {item.y} {item.z}")
        explicit = bool(item.params.get("allow_break"))
        target = (int(item.x), int(item.y), int(item.z))

        def no_path(exc: BridgeError) -> bool:
            return any(word in str(exc) for word in ("当前路线无路", "寻路无进展"))

        try:
            await self._move_and_wait(item.x, item.y, item.z, 120, allow_break=explicit)
            await self._task_chat(f"到了 {item.x} {item.y} {item.z}")
            return {"goal_outcome": "fulfilled", "target": target}
        except BridgeError as exc:
            last_error = exc
            if not no_path(exc):
                raise

        if not explicit:
            if await self._try_step_off_bed():
                try:
                    await self._move_and_wait(item.x, item.y, item.z, 120, allow_break=False)
                    await self._task_chat(f"到了 {item.x} {item.y} {item.z}")
                    return {"goal_outcome": "fulfilled", "target": target}
                except BridgeError as exc:
                    last_error = exc
            try:
                if await self._try_navigate(item.x, item.y, item.z, 30):
                    await self._task_chat(f"到了 {item.x} {item.y} {item.z}")
                    return {"goal_outcome": "fulfilled", "target": target}
            except BridgeError:
                pass

        if await self._try_open_nearby_door():
            try:
                await self._move_and_wait(item.x, item.y, item.z, 120, allow_break=False)
                await self._task_chat(f"到了 {item.x} {item.y} {item.z}")
                return {"goal_outcome": "fulfilled", "target": target}
            except BridgeError as exc:
                last_error = exc
        if explicit:
            await self._task_chat("受控掘进后仍无路，尝试附近可站立位置")
        else:
            await self._task_chat("普通寻路失败，尝试附近可站立位置，不自动挖方块")

        for candidate in await self._goto_candidates(*target):
            if candidate == target:
                continue
            try:
                await self._move_and_wait(candidate[0], candidate[1], candidate[2], 90, allow_break=False)
                await self._task_chat(
                    f"到了目标附近 {candidate[0]} {candidate[1]} {candidate[2]}"
                    f"（原目标 {item.x} {item.y} {item.z}）"
                )
                return {
                    "goal_outcome": "partial",
                    "target": target,
                    "arrived_near": {"x": candidate[0], "y": candidate[1], "z": candidate[2]},
                }
            except BridgeError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise BridgeError("寻路失败，未找到可到达的目标或附近站位")

    async def _run_come_task(self, item: QueuedTask) -> None:
        data = await self._call("come", {"player": item.target}, timeout=10)
        if self._err(data):
            raise BridgeError(self._err(data))
        try:
            xyz = [float(data[key]) for key in ("x", "y", "z")]
        except (KeyError, TypeError, ValueError) as exc:
            raise BridgeError("客户端没有返回有效目标坐标") from exc
        await self._wait_arrival(*xyz, seconds=90)
        await self._task_chat(f"到了 {data.get('target', item.target)} 那边")

    def _start_wood_task(self, text: str, target: int) -> bool:
        return False

    def _cancel_active_task(self) -> None:
        self._task_stop.set()
        self._task_queue.clear()
        self._task_current = None
        scheduler = getattr(self, "_scheduler", None)
        if scheduler is not None:
            scheduler.cancel_all("task queue cancelled")
        task = self._active_task
        self._worker_generation = getattr(self, "_worker_generation", 0) + 1
        self._active_task = None
        if task is not None and not task.done():
            task.cancel()

    @staticmethod
    def _queue_requested(text: str) -> bool:
        return any(w in text for w in ("队列", "排队", "任务队列"))

    def _parse_tasks_ex(self, text: str, sender: str) -> tuple[list[QueuedTask], list[str]]:
        raw = re.sub(
            r"^(?:mc)?\s*(?:任务队列|队列|排队|任务)\s*[:：]?\s*",
            "",
            str(text or "").strip(),
            flags=re.I,
        )
        parts = [
            p.strip()
            for p in re.split(r"\s*(?:然后|→|;|；|\n)\s*", raw)
            if p.strip()
        ]
        tasks: list[QueuedTask] = []
        unparsed: list[str] = []
        for part in parts:
            m = re.search(
                r"(?:去|到|前往|goto)\s*(?:坐标)?\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)",
                part,
                re.I,
            )
            if m:
                tasks.append(
                    QueuedTask(
                        "goto",
                        part,
                        x=int(m.group(1)),
                        y=int(m.group(2)),
                        z=int(m.group(3)),
                    )
                )
                continue
            if re.search(r"(来我这边|过来|到我这|来我这|来我身边)", part):
                tasks.append(
                    QueuedTask(
                        "come",
                        part,
                        target=sender
                        or str(self.config.get("owner_name", "Player")).strip(),
                    )
                )
                continue
            m = re.search(
                r"(?:去|到|前往)\s*([A-Za-z0-9_]{2,16})\s*(?:那边|身边|这里|这儿)",
                part,
            )
            if m:
                tasks.append(QueuedTask("come", part, target=m.group(1)))
                continue
            unparsed.append(part)
        return tasks, unparsed

    def _parse_tasks(self, text: str, sender: str) -> list[QueuedTask]:
        return self._parse_tasks_ex(text, sender)[0]

    def _task_status_text(self) -> str:
        labels = {0: "紧急", 10: "主人", 20: "普通", 30: "自主", 40: "后台"}
        lines = []
        if self._task_current is not None:
            priority = int(getattr(self._task_current, "priority", 20))
            lines.append(f"当前[{labels.get(priority, priority)}]: {self._task_current.goal}")
        if self._task_queue:
            queued = sorted(list(self._task_queue), key=lambda task: (int(getattr(task, "priority", 20)),))
            lines.append(
                "排队: "
                + " | ".join(
                    f"{i + 1}.{labels.get(int(getattr(task, 'priority', 20)), int(getattr(task, 'priority', 20)))}:{task.goal}"
                    for i, task in enumerate(queued[:8])
                )
            )
        scheduler = getattr(self, "_scheduler", None)
        if scheduler is not None and scheduler.active is not None:
            lines.append(f"调度器: {scheduler.active.id} {scheduler.active.state} 尝试{scheduler.active.attempts}")
        return "\n".join(lines) or "任务队列是空的"

    async def _task_fail(self, reason: str) -> None:
        await self._call("baritone", {"command": "stop"}, timeout=10)
        await self._task_chat(f"任务失败：{await self._task_error_text(BridgeError(reason))}")

    async def _auto_flee(self, status: dict, hostile: dict) -> str:
        import math
        try:
            position=status['position'];enemy=hostile['position']
            baseline=math.hypot(float(position['x'])-float(enemy['x']),float(position['z'])-float(enemy['z']))
        except (KeyError, TypeError, ValueError):
            return "位置数据不完整，未执行撤离"
        if self.bridge.client_info.get('protocol',1)<3:return '旧客户端无法验证撤离落脚点，未发送盲目移动'
        scan=await self._call('scan',{'radius':10,'resource':'logs'},timeout=10)
        if self._err(scan):return '未能确认安全撤离落脚点'
        candidates=[]
        for point in scan.get('ground',[]):
            distance=math.hypot(point['x']+.5-position['x'],point['z']+.5-position['z'])
            if not 3<=distance<=12:continue
            separation=math.hypot(point['x']+.5-enemy['x'],point['z']+.5-enemy['z'])
            if separation<baseline+2:continue
            candidates.append((separation-distance*.25,point))
        candidates.sort(key=lambda row:-row[0])
        for _,point in candidates[:2]:
            result=await self._call('navigate',{k:point[k] for k in ('x','y','z')},timeout=5)
            if self._err(result):continue
            try:await self._wait_arrival(point['x']+.5,float(point.get('feet_y',point['y'])),point['z']+.5,seconds=4,tolerance=.8)
            except asyncio.CancelledError:raise
            except BridgeError:
                continue
            return '已确认到达撤离落脚点；仍需继续观察威胁'
        return '没有确认到可达撤离点，未把移动指令当作逃脱成功'

    def _respawn_signal(self,payload):
        # Synchronous invalidation: never await an RPC from the WebSocket reader.
        self._control_epoch+=1;self._task_stop.set();self._task_queue.clear()
        self._manual_movement=False;self._manual_notice_route=None
        self._cancel_active_task()
        for job in self._agent_jobs:
            if job is not asyncio.current_task():job.cancel()

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
            await self._shared_memory.start()
        except Exception as exc:
            logger.error(f"[mc-bridge] failed to start WebSocket server: {exc}")

    async def terminate(self) -> None:
        self._closing = True
        self._control_epoch += 1
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
        if action in {'knowledge','sense','station','smelt','interact','pickup','move_item','trade','ride','dismount','wake','plant','dig_down','block_context','get_menu','interact_block','release_use','mine_below','align','unequip_armor','till','jump','use_bonemeal'} and action not in str(self.bridge.client_info.get('capabilities','')).split(','):
            return {'__error__':'此功能需要0.4.0或更新的客户端 Mod，请安装新版后重连'}
        if not readonly and self.bridge.connected and self.bridge.client_info.get("protocol", 1) < 2:
            return {"__error__": "客户端协议过旧，请安装 0.2.0 Mod 后重新进入游戏"}
        guard_error = await self._guard_break_action(action, params)
        if guard_error:
            return {"__error__": guard_error}
        try:
            if readonly:
                return await self.bridge.call(action, params, timeout)
            async with self._action_lock:
                if self._closing or (epoch is not None and epoch != self._control_epoch):
                    return {"__error__": "请求已停止或被新的控制操作中断"}
                return await self.bridge.call(action, params, timeout)
        except (BridgeError, ConnectionError, RuntimeError, asyncio.TimeoutError) as exc:
            return {"__error__": str(exc) or "客户端动作响应超时"}

    @staticmethod
    def _err(data: Any) -> str | None:
        if isinstance(data, dict) and data.get("__error__"):
            return str(data["__error__"])
        return None

    async def _run_operation(self, action: str, params: dict, seconds: float = 10, timeout: float = 10) -> dict:
        """Start a client operation and poll it to a terminal phase."""
        result = await self._call(action, params, timeout=timeout)
        err = self._err(result)
        if err:
            raise SurvivalError(err)
        if not isinstance(result, dict) or not result.get("operation_id") or result.get("phase") != "started":
            raise SurvivalError("客户端没有确认动作启动")
        identifier = result["operation_id"]
        deadline = time.monotonic() + max(1.0, float(seconds))
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(0.3)
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
        return "full" if str(self.config.get("tool_profile", "simple")).strip().lower() == "full" else "simple"

    def _apply_tool_profile(self) -> None:
        if self._tool_profile() == "full":
            return
        try:
            manager = self.context.get_llm_tool_manager()
            for tool in list(getattr(manager, "func_list", [])):
                name = str(getattr(tool, "name", "") or "")
                if name.startswith("mc_") and name not in SIMPLE_TOOLS:
                    manager.remove_func(name)
        except Exception as exc:
            logger.warning("[mc-bridge] tool profile filter skipped: %s", type(exc).__name__)

    def _simple_system_prompt(self, allow_actions: bool, owner: bool) -> str:
        owner_name = str(self.config.get("owner_name", "Player") or "Player")
        prompt = (
            f"\n\n你现在通过 Minecraft 客户端账号行动，默认主人游戏名是 {owner_name}。"
            "常用入口：mc_brief 一次查看位置、血量、饥饿、背包摘要、家床和任务；mc_inventory 查看完整背包；"
            "mc_goto/mc_come/mc_follow/mc_stop 控制移动；mc_mine_at/mc_dig_down/mc_place_at 挖掘或放置；"
            "mc_pickup 拾取掉落物；mc_container 打开/读取/点击容器；mc_interact 开关门和按钮；"
            "mc_eat 进食、mc_equip 换装备、mc_attack 攻击、mc_block_context 查看方块和站位；"
            "mc_scan 扫描附近方块/实体；mc_set_respawn 设置重生床；mc_recipes 查配方；"
            "mc_memory 读写家床/工位/资源点；mc_protect 管理保护区域；mc_screenshot 只在确实需要看画面时使用。"
            "多步骤任务用 mc_task_start：opcode 支持 gather(resource,count)、craft(item,count)、smelt(item,count)、"
            "container_transfer(items 或 item,count,direction,x,y,z)、build(pattern,material,x,y,z,length,width,height,direction)、"
            "farm(operation,count,radius,replant)、sleep(source,remember)、combat(target,max_seconds,retreat_health)、"
            "explore(goal,max_seconds,max_distance)、return_to(target_pos)、resupply(...)、trade_for(item,count,radius)；"
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
        return _fmt_status(data)

    @filter.llm_tool(name="mc_brief")
    @require_mc_tool("mc_brief")
    async def mc_brief(self, event: AstrMessageEvent):
        """一次返回位置/血量/任务/家床等常用只读信息，替代多次 mc_status/mc_task_status/mc_memory。"""
        data = await self._call("status", timeout=10)
        err = self._err(data)
        if err:
            return "状态查询失败：" + err
        pos = data.get("position") or {}
        try:
            task = self._agent_task_status("", detail=False)
        except Exception:
            task = {}
        try:
            memory = self._poi_memory_for()
            home_bed = memory.get("home_bed", "main")
            home = memory.get("home", "main")
        except Exception:
            home_bed = None
            home = None

        def coord(row):
            if not isinstance(row, dict):
                return None
            return {k: row.get(k) for k in ("x", "y", "z", "name", "world_key", "dimension") if row.get(k) is not None}

        return json.dumps({
            "position": {k: pos.get(k) for k in ("x", "y", "z")},
            "dimension": data.get("dimension"),
            "health": data.get("health"),
            "food": data.get("food"),
            "on_ground": data.get("on_ground"),
            "screen": data.get("screen"),
            "hostiles": (data.get("hostiles") or [])[:3],
            "path_feedback": data.get("path_feedback"),
            "task": {k: task.get(k) for k in ("active", "pending_count", "current_task_id", "queue")},
            "home_bed": coord(home_bed),
            "home": coord(home),
        }, ensure_ascii=False, default=str)

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
    async def mc_goto(self, event: AstrMessageEvent, x: int, y: int, z: int, allow_break: bool = False):
        """加入受监控的坐标移动任务。目标不可站立时会自动改用最近站位。

        Args:
            x(int): 目标方块或站位 X。
            y(int): 目标方块或站位 Y。
            z(int): 目标方块或站位 Z。
            allow_break(bool): 是否允许本次移动受控掘进穿过方块；默认否，普通移动不会自动挖方块。
        """
        target, info = await self._resolve_navigation_target(x, y, z)
        if target is None:
            return info
        tx, ty, tz = target
        item = QueuedTask("goto", f"去 {tx} {ty} {tz}", x=tx, y=ty, z=tz, params={"allow_break": bool(allow_break)})
        if not self._enqueue_tasks([item]):
            return "队列已满，未加入"
        if info.get("adjusted"):
            return f"目标方块不可站立，已改到最近站位 {tx} {ty} {tz}；已加入移动任务，实际到达后才汇报完成"
        return "已加入移动任务，实际到达后才汇报完成"

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
            if 'look_angles' not in str(self.bridge.client_info.get('capabilities','')).split(','):return '自由视角需要0.4.1客户端'
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
            if target and not target.startswith('minecraft:') and 'knowledge' in str(self.bridge.client_info.get('capabilities','')).split(','):
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
            histories = {
                stored_key: deque(lines, maxlen=20)
                for stored_key, lines in self._short_history_for().items().items()
            }
            self._mc_histories = histories
        history = histories.get(key)
        if history is None:
            history = deque(maxlen=20)
            histories[key] = history
        return key, history

    def _mc_history_text(self, event):
        key, history = self._mc_history(event)
        bounded = bound_history(history, history_budget(self.config))
        text = "\n".join(bounded)
        event.set_extra('_mc_history_tokens', estimate_tokens(text))
        event.set_extra('_mc_history_lines', len(bounded))
        return key, text

    def _mc_record_history(self, key, user, reply):
        histories = getattr(self, '_mc_histories', None)
        if histories is None:
            histories = {
                stored_key: deque(lines, maxlen=20)
                for stored_key, lines in self._short_history_for().items().items()
            }
            self._mc_histories = histories
        history = histories.setdefault(key, deque(maxlen=20))
        history.extend([f"主人：{user}", f"你：{reply}"])
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
                if self._tool_profile() == "simple":
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
                        "主人明确说去某处/下楼/去房间时，先用 mc_status 确认位置（最多一次），然后直接 mc_goto 到已知坐标或记忆坐标；不要为了找路先连续 mc_scan 门/梯子/楼梯，路线由 Baritone 负责。"
                        "路线、门、梯子、楼梯、坐标类问题禁止调用 mc_see；它会被直接拦截并浪费一轮，直接用 mc_scan/mc_block_context。"
                        "说“记下了/记住了”必须对应 mc_set_respawn 或 mc_memory(set) 的写入成功，不能只凭上下文。"
                        "你是第一视角玩家，不要在截图里找自己；用户说刚刚/对的/继续时先参考短期历史，没有上下文就明确问。"
                    )
                if not allow_actions:
                    system_prompt+=("\n本轮是回忆/任务期间聊天，只能查看或回忆，不执行新的游戏动作。" if owner else "\n当前说话的人不是主人，只能聊天和查看状态/画面；动作需要主人下令。")
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
            "快捷操作：mcstatus 查状态、mcstop 停止、mcshot 截图。"
        )

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
        }
        if low in shortcuts:
            return shortcuts[low], ""
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
                yield event.plain_result(reply)
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
                yield event.plain_result(reply)
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
        yield event.plain_result(reply)

    def _tool_denial(self, event, name):
        if name in {"mc_recall_memory", "mc_forget_memory"}:
            shared = getattr(self, "_shared_memory", None)
            if self._closing or shared is None:
                return "MC共享记忆尚未就绪"
            if CONTROL_EPOCH.get() not in (None, self._control_epoch):
                return "旧请求已中断，未读取共享记忆"
            return shared.denial(event, self.bridge, forget=name == "mc_forget_memory")
        if self._closing:
            return "Minecraft 插件正在停止，未执行"
        text = str(event.message_str or "") if event is not None else ""
        recall_question = bool(re.search(r'(?:记得|回忆|聊过|上次|之前|昨天|前天).{0,48}(?:吗|什么|怎么|多少|哪|是否|[?？])', text))
        if name not in {"mc_status", "mc_look", "mc_see", "mc_screenshot"} and event is not None and self._is_memory_query(text) and (not action_intent(text) or recall_question):
            return "本轮是回忆，不执行新的游戏动作"
        if not self.bridge.connected and name not in {"mc_stop", "mc_status", "mc_screenshot", "mc_see"}:
            return "Minecraft 客户端未连接，未执行"
        epoch = CONTROL_EPOCH.get()
        if epoch is not None and epoch != self._control_epoch:
            return "这次操作已被停止或紧急避险中断，未执行"
        if self._active_task is not None and not self._active_task.done() and name not in {
            "mc_stop", "mc_flee", "mc_status", "mc_look", "mc_see", "mc_screenshot", "mc_recipes",
            "mc_scan", "mc_inspect_block", "mc_inspect_target", "mc_wake",
            "mc_task_status", "mc_task_start", "mc_task_cancel", "mc_wait", "mc_capabilities",
            "mc_brief", "mc_block_context", "mc_get_menu"
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
            if self._player_allowed(sender):
                return None
            safe = {"mc_status", "mc_look", "mc_see", "mc_screenshot"}
            if name in safe & self._safe_tool_names():
                return None
            return "这项动作需要主人下令，未执行"
        if platform not in {"aiocqhttp", "webchat"} or not private:
            return "Minecraft 工具仅在授权私聊或可信游戏会话中使用，未执行"
        if self.config.get("qq_admin_only", True) and not event.is_admin():
            return "只有主人能控制 Minecraft 机器人，未执行"
        text = str(getattr(event, "message_str", ""))
        if not (self._parse_input(text) or re.search(r"Minecraft|我的世界|(?<![a-zA-Z0-9_])mc(?![a-zA-Z0-9_])", text, re.I)):
            return "本轮未提出 Minecraft 请求，未执行"
        return None

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
        affected=self._notice_routes()
        self._control_epoch+=1
        self._manual_movement=False;self._manual_notice_route=None
        task=self._active_task;self._cancel_active_task()
        jobs={job for job in (task,*self._agent_jobs) if job is not None and job is not asyncio.current_task()}
        for job in jobs:job.cancel()
        if jobs:await asyncio.gather(*jobs,return_exceptions=True)
        if not self._closing:
            for destination in affected:
                await self._task_chat('Minecraft连接已断开，你发起的任务已中断；重连后不会自动续跑',route=destination,kind='disconnect')

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
        capabilities = str(self.bridge.client_info.get("capabilities", "")).split(",")
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
            return min(max(1, int(policy.per_action)), max(0, int(policy.remaining_task_budget())))
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
        capabilities = str(self.bridge.client_info.get("capabilities", "")).split(",")
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
        if shared is None or not shared.qq_owner(event):
            return
        shared.prepare_qq(event)
        context = await shared.context_for(event, self.bridge, str(event.message_str or ""))
        parts = request.extra_user_content_parts
        if context and not any(MARKER in str(getattr(part, "text", "")) for part in parts):
            parts.append(TextPart(text=context).mark_as_temp())

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
            resource(string): logs/stone/coal/iron/wheat 或对应中文。
            count(int): 目标新增数量1-256。
            radius(int): 搜索半径6-12。
            max_seconds(int): 总时限20-600秒。
            allow_night(bool): 已废弃，忽略；夜间不再拦截采集。
        """
        if not 1 <= int(count) <= 256:
            return "采集数量必须在1-256"
        aliases = {"煤": "coal", "煤炭": "coal", "铁": "iron", "铁矿": "iron", "石头": "stone", "圆石": "stone", "石料": "stone", "木头": "logs", "原木": "logs", "树": "logs", "小麦": "wheat"}
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
    async def mc_farm(self, event: AstrMessageEvent, operation: str = "harvest", radius: int = 8, count: int = 8, replant: bool = True):
        """农业技能：收割成熟小麦并拾取，或给空农田补种小麦。

        Args:
            operation(string): harvest、plant 或 both。
            radius(int): 搜索半径2-12。
            count(int): 最多处理数量1-32。
            replant(bool): 收割后是否补种，默认是。
        """
        operation = str(operation or "harvest").strip().lower()
        if operation not in {"harvest", "plant", "both"}:
            return "operation 只能是 harvest/plant/both"
        if self._queue_skill(
            f"农业{operation} x{int(count)}",
            "farm",
            operation=operation,
            radius=max(2, min(12, int(radius))),
            count=max(1, min(32, int(count))),
            replant=bool(replant),
        ):
            return "已加入农业技能队列，完成后会通知收割/补种数量"
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
            food(int): 目标安全食物数量0-64。
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
        data = await self._call("scan_entities", {"radius": radius, "limit": 32}, timeout=10)
        err = self._err(data)
        if err:
            return err
        rows = [row for row in (data.get("entities") or []) if isinstance(row.get("item"), dict)]
        wanted = str(item or "").strip()
        if wanted:
            rows = [
                row for row in rows
                if wanted == str((row.get("item") or {}).get("id") or "")
                or wanted == str((row.get("item") or {}).get("name") or "")
                or wanted in str(row.get("name") or "")
            ]
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
        return json.dumps({
            "saved": saved is not None,
            "bed": context.get("block"),
            "position": {"x": int(x), "y": int(y), "z": int(z)},
            "approached_to": {"x": stand["x"], "y": stand.get("feet_y", stand["y"]), "z": stand["z"]},
            "memory": saved,
            "note": "已走到床边、完成交互并写入 home_bed；服务端重生点提示仍以游戏画面为准",
        }, ensure_ascii=False)

    @filter.llm_tool(name="mc_memory")
    @require_mc_tool("mc_memory")
    async def mc_memory(
        self,
        event: AstrMessageEvent,
        operation: str = "get",
        kind: str = "home_bed",
        key: str = "main",
        x: int = 0,
        y: int = -9999,
        z: int = 0,
        name: str = "",
    ):
        """读写 MC 坐标记忆：家、家床、工作台、熔炉、资源点、村庄、危险点、箱子。

        Args:
            operation(string): get、set、forget、list。
            kind(string): home/home_bed/workbench/furnace/resource/village/danger/chest。
            key(string): 记录键，默认 main。
            x(int): set时X坐标。
            y(int): set时Y坐标。
            z(int): set时Z坐标。
            name(string): 可选显示名。
        """
        operation = str(operation or "get").strip().lower()
        kind = str(kind or "home_bed").strip()
        key = str(key or "main").strip()
        try:
            memory = self._poi_memory_for()
            if operation == "list":
                return json.dumps({"items": memory.list(kind if kind not in {"", "all"} else "")}, ensure_ascii=False)
            if operation == "get":
                row = memory.get(kind, key)
                return json.dumps({"found": row is not None, "item": row}, ensure_ascii=False)
            if operation == "forget":
                return json.dumps({"forgot": memory.forget(kind, key)}, ensure_ascii=False)
            if operation != "set":
                return "operation 只能是 get/set/forget/list"
            if int(y) == -9999:
                return "set 需要 x/y/z 坐标"
            status = await self._call("status", timeout=10)
            err = self._err(status)
            if err:
                return err
            if kind == "home_bed":
                check = await self._call("inspect_block", {"x": int(x), "y": int(y), "z": int(z)}, timeout=10)
                if self._err(check):
                    return self._err(check)
                block = str(check.get("block") or "")
                if not block.endswith("_bed"):
                    return "目标不是床，未写入家床记忆"
            row = memory.set(
                kind, key, name=name, world_key=str(status.get("world_key") or ""),
                dimension=str(status.get("dimension") or ""), x=int(x), y=int(y), z=int(z),
            )
            return json.dumps({"saved": True, "item": row}, ensure_ascii=False)
        except ValueError as exc:
            return "记忆操作失败：" + str(exc)

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
            "client_capabilities": str(self.bridge.client_info.get("capabilities", "")).split(","),
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
            opcode(string): gather(resource,count)、craft(item,count)、smelt(item,count)、container_transfer(items 或 item,count,direction,x,y,z)、build(pattern,material,x,y,z,length,width,height,direction)、farm(operation,count,radius,replant)、sleep(source,remember)、combat(target,max_seconds,retreat_health)、explore(goal,max_seconds,max_distance)、return_to(target_pos)、resupply(...)、trade_for(item,count,radius)；auto 表示按文本分类；resume 表示恢复检查点任务。
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
        return json.dumps(self._agent_task_status(task_id, detail=bool(detail)), ensure_ascii=False)

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
