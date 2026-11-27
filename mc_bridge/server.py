"""WebSocket bridge server, task model, and status formatting."""
from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import web
from astrbot.api import logger

from ..reply_route import ReplyRoute
from ..scheduler import Priority


class BridgeError(RuntimeError):
    """Raised when the Minecraft client cannot execute an action."""


def parse_capabilities(raw: Any) -> set[str]:
    """Parse client capabilities from either Protocol 4 JSON list or Protocol 3 CSV string."""
    if isinstance(raw, (list, tuple, set, frozenset)):
        return {str(x).strip() for x in raw if str(x).strip()}
    if isinstance(raw, str):
        return {x.strip() for x in raw.split(",") if x.strip()}
    return set()


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
        on_ready=None,
        chat_priority=None,
        on_chat_overflow=None,
        chat_pending_limit=4,
        on_survival_event=None,
        allow_query_token: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.path = path if path.startswith("/") else "/" + path
        self.token = token
        self.allow_query_token = bool(allow_query_token)
        self.on_chat = on_chat
        self.on_disconnect = on_disconnect
        self.on_ready = on_ready
        self.on_survival_event = on_survival_event
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
        self._op_waiters: dict[str, asyncio.Future] = {}
        self._op_completed: dict[str, dict[str, Any]] = {}
        self._send_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        self._chat_jobs: set[asyncio.Task] = set()
        self._event_jobs: set[asyncio.Task] = set()
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

    def _finish_event_job(self, job):
        self._event_jobs.discard(job)
        if not job.cancelled() and job.exception() is not None:
            logger.warning("[mc-bridge] survival event failed: %s", type(job.exception()).__name__)

    async def stop(self) -> None:
        jobs = list(self._chat_jobs)
        if self._overflow_job is not None and not self._overflow_job.done():jobs.append(self._overflow_job)
        jobs.extend(job for job in self._event_jobs if not job.done())
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        self._chat_jobs.clear()
        self._event_jobs.clear()
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
        waiters, self._op_waiters = self._op_waiters, {}
        for fut in waiters.values():
            if not fut.done():
                fut.set_exception(BridgeError(reason))
        self._op_completed.clear()

    def _check_token(self, request: web.Request) -> bool:
        supplied = ""
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not supplied and self.allow_query_token:
            supplied = request.query.get("token", "")
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
            callback = self.on_ready
            if callback is not None and len(self._event_jobs) < 8:
                job = asyncio.create_task(callback(data))
                self._event_jobs.add(job)
                job.add_done_callback(self._finish_event_job)
        elif typ=='event' and data.get('event')=='normal_respawn':
            payload=data.get('data',{})
            if not isinstance(payload,dict) or payload.get('reason') not in ('player_died','actor_replaced'):return
            callback=getattr(self,'on_respawn',None)
            if callback is not None:callback(payload)
        elif typ == "event" and data.get("event") == "operation_done":
            payload = data.get("data")
            if isinstance(payload, dict):
                op_id = str(payload.get("operation_id") or "").strip()
                if op_id:
                    self._op_completed[op_id] = payload
                    if len(self._op_completed) > 32:
                        oldest = next(iter(self._op_completed))
                        self._op_completed.pop(oldest, None)
                    fut = self._op_waiters.pop(op_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(payload)
        elif typ == "event" and data.get("event") == "operation_progress":
            return
        elif typ == "event" and data.get("event") == "survival":
            payload = data.get("data")
            callback = self.on_survival_event
            if isinstance(payload, dict) and callback is not None and len(self._event_jobs) < 8:
                job = asyncio.create_task(callback(payload))
                self._event_jobs.add(job)
                job.add_done_callback(self._finish_event_job)
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

    async def wait_operation(self, op_id: str, timeout: float = 5.0) -> dict[str, Any] | None:
        """Await a Protocol 4 ``operation_done`` push event; returns None on timeout for poll fallback."""
        key = str(op_id or "").strip()
        if not key:
            return None
        cached = self._op_completed.pop(key, None)
        if cached is not None:
            return cached
        if not self.connected:
            raise BridgeError("Minecraft 客户端没有连接（先启动机器人客户端）")
        fut = self._op_waiters.get(key)
        if fut is None or fut.done():
            fut = asyncio.get_running_loop().create_future()
            self._op_waiters[key] = fut
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=max(0.05, float(timeout)))
        except asyncio.TimeoutError:
            return None
        finally:
            if self._op_waiters.get(key) is fut and fut.done():
                self._op_waiters.pop(key, None)

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
