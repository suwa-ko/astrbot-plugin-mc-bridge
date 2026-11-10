"""Deterministic, owner-scoped survival supervision.

The supervisor polls ``status`` as a fallback and consumes lightweight
``survival`` events from the client when available.  It never calls an LLM:
fast survival decisions must be local, auditable and bounded.

Actions reuse the existing skills:

* ``eat_until_full`` for safe food;
* ``combat`` with an entry/hold/flee threshold policy;
* ``_auto_flee`` for a verified retreat.

Notifications are sent only to the owner's private route.  Exact coordinates
are never written to logs, memory or the notification text.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from astrbot.api import logger

from .privacy import redact_text
from .reply_route import ReplyRoute

MODES = {"off", "notify", "defend", "full"}
DEFAULT_BLACKLIST = (
    "minecraft:creeper",
    "minecraft:wither",
    "minecraft:warden",
    "minecraft:ender_dragon",
    "minecraft:enderman",
)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _error(value: Any) -> str:
    if not isinstance(value, dict):
        return "status unavailable"
    return str(value.get("__error__") or "")


class SurvivalSupervisor:
    """Bounded auto-eat / auto-flee / self-defense loop."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.closing = False
        self.session_id = ""
        self.seq = -1
        self.last_event_kind = ""
        self.last_event_at = 0.0
        self.last_status: dict[str, Any] = {}
        self.engaged = False
        self.anchor: dict[str, Any] | None = None
        self.last_action = 0.0
        self.last_eat = 0.0
        self.retreat_until = 0.0
        self.last_notice = 0.0
        self.last_notice_key = ""
        self.stats = {
            "events": 0,
            "polls": 0,
            "eats": 0,
            "fights": 0,
            "flees": 0,
            "notices": 0,
            "errors": 0,
        }

    # ------------------------------------------------------------- lifecycle

    @property
    def mode(self) -> str:
        mode = str(self.plugin.config.get("auto_survive_mode", "notify") or "notify").strip().lower()
        if mode not in MODES:
            mode = "notify"
        # The legacy boolean remains a hard kill switch.  Existing installations
        # without the new ``mode`` key stay in notify-only until explicitly
        # switched to defend/full.
        if not _bool(self.plugin.config.get("auto_survive", True), True):
            return "off"
        return mode

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    async def start(self) -> None:
        if not self.enabled or self.task is not None:
            return
        self.closing = False
        self.task = asyncio.create_task(self._loop(), name="mc-bridge-survival")
        logger.info("[mc-bridge] survival supervisor started mode=%s", self.mode)

    async def stop(self) -> None:
        self.closing = True
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def on_event(self, payload: dict[str, Any]) -> None:
        """Wake the supervisor after a client-side survival event."""
        if not self.enabled or not isinstance(payload, dict):
            return
        seq = payload.get("seq")
        if isinstance(seq, (int, float)):
            seq = int(seq)
            if seq <= self.seq:
                return
            self.seq = seq
        self.last_event_kind = str(payload.get("kind") or "")
        self.last_event_at = time.monotonic()
        self.stats["events"] += 1
        self.wake.set()

    # --------------------------------------------------------------- config

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.plugin.config.get(key, default)

    def _blacklist(self) -> tuple[str, ...]:
        raw = self._cfg("auto_fight_blacklist", ",".join(DEFAULT_BLACKLIST))
        tokens = [x.strip().lower() for x in re.split(r"[,，\s]+", str(raw or "")) if x.strip()]
        return tuple(tokens) or DEFAULT_BLACKLIST

    def _ignored_entities(self) -> tuple[str, ...]:
        raw = self._cfg("auto_survive_ignore_entities", "genshin_nature:luminousseelie")
        return tuple(x.strip().lower() for x in re.split(r"[,，\s]+", str(raw or "")) if x.strip())

    def _poll_interval(self) -> float:
        base = _int(self._cfg("auto_survive_interval", 2), 2, 1, 10)
        capabilities = str(getattr(self.plugin.bridge, "client_info", {}).get("capabilities", ""))
        if "survival_events" in {x.strip() for x in capabilities.split(",")}:
            return float(max(5, base * 2))
        return float(base)

    # ---------------------------------------------------------------- loop

    async def _loop(self) -> None:
        while not self.closing:
            try:
                if getattr(self.plugin, "_closing", False) or not self.plugin.bridge.connected:
                    await asyncio.sleep(2.0)
                    continue
                session = str(getattr(self.plugin.bridge, "session_id", "") or "")
                if session != self.session_id:
                    self.session_id = session
                    self.seq = -1
                    self.engaged = False
                    self.anchor = None
                status = await self.plugin._call("status", timeout=8)
                if not _error(status):
                    self.stats["polls"] += 1
                    await self.evaluate(status, source="poll")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let a status hiccup kill the loop
                self.stats["errors"] += 1
                logger.debug("[mc-bridge] survival poll deferred: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self._poll_interval())
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    # ------------------------------------------------------------- decision

    async def evaluate(self, status: dict[str, Any], *, source: str = "poll") -> None:
        if not self.enabled or not isinstance(status, dict) or _error(status):
            return
        async with self.lock:
            self.last_status = dict(status)
            health = _num(status.get("health"), 20.0)
            max_health = _num(status.get("max_health"), 20.0)
            food = _num(status.get("food"), 20.0)
            dead = bool(status.get("dead")) or health <= 0
            if dead or status.get("sleeping") or status.get("using_item") or status.get("survival_busy"):
                return
            screen = str(status.get("screen") or "none").strip().lower()
            if screen and screen not in {"none", "null"}:
                return
            hostiles = self._hostiles(status)
            nearest = min(hostiles, key=lambda row: row["distance"]) if hostiles else None
            if self.mode == "notify":
                if self._trigger(health, max_health, food, hostiles):
                    await self._notify(
                        self._describe(health, food, hostiles),
                        "仅报告，未执行动作",
                        "等待主人确认或切换 defend 模式",
                        key="notify",
                    )
                return
            # Do not fight/eat in the middle of an owner task.  Critical danger
            # is the only exception; it cancels the task and flees.
            if self._task_busy():
                if nearest is not None and health <= self._flee_health():
                    await self._flee(status, nearest, "当前任务中血量过低")
                return
            if nearest is not None and time.monotonic() < self.retreat_until and health > self._flee_health():
                return
            if nearest is None:
                self.engaged = False
                self.anchor = None
                if self._should_eat(health, max_health, food):
                    await self._eat(status)
                else:
                    self.last_notice_key = ""
                return
            if self.engaged:
                if self._must_abort(status, health, hostiles, nearest):
                    await self._flee(status, nearest, "战斗中止")
                elif self._can_continue(health, hostiles):
                    await self._fight(status, nearest, hostiles)
                else:
                    await self._flee(status, nearest, "血量进入撤离区")
                return
            if self._must_abort(status, health, hostiles, nearest):
                await self._flee(status, nearest, "危险目标或血量过低")
                return
            if self._can_start(health, hostiles):
                await self._fight(status, nearest, hostiles)
                return
            # A threat exists but the current state does not justify fighting.
            await self._notify(
                self._describe(health, food, hostiles),
                "保持距离，未主动开战",
                "等待安全窗口或主人指令",
                key="observe",
            )

    # --------------------------------------------------------------- helpers

    def _flee_health(self) -> float:
        return _num(self._cfg("auto_flee_health", 4), 4.0)

    def _hold_health(self) -> float:
        return _num(self._cfg("auto_hold_health", 6), 6.0)

    def _fight_health(self) -> float:
        return _num(self._cfg("auto_fight_health", 14), 14.0)

    def _eat_food(self) -> float:
        return _num(self._cfg("auto_eat_food", 8), 8.0)

    def _eat_health_food(self) -> float:
        return _num(self._cfg("auto_eat_health_food", 18), 18.0)

    def _attack_range(self) -> float:
        return _num(self._cfg("auto_attack_range", 6), 6.0)

    def _max_targets(self) -> int:
        return _int(self._cfg("auto_fight_max_targets", 2), 2, 1, 8)

    def _leash(self) -> float:
        return _num(self._cfg("auto_fight_leash_radius", 12), 12.0)

    def _finish_low_target(self) -> bool:
        return _bool(self._cfg("auto_finish_low_target", True), True)

    def _notify_enabled(self) -> bool:
        return _bool(self._cfg("auto_survive_notify", True), True)

    def _hostiles(self, status: dict[str, Any]) -> list[dict[str, Any]]:
        limit = max(self._attack_range(), 6.0)
        rows: list[dict[str, Any]] = []
        for raw in status.get("hostiles") or []:
            if not isinstance(raw, dict):
                continue
            distance = _num(raw.get("distance"), 999.0)
            if distance > limit:
                continue
            row = dict(raw)
            row["distance"] = distance
            row["_type"] = str(raw.get("type") or "").lower()
            row["_name"] = str(raw.get("name") or "").lower()
            if any(token in (row["_type"] + " " + row["_name"]) for token in self._ignored_entities()):
                continue
            row["_health"] = _num(raw.get("health"), 20.0)
            row["_los"] = raw.get("line_of_sight", True) is not False
            rows.append(row)
        rows.sort(key=lambda row: row["distance"])
        return rows

    def _blacklisted(self, hostile: dict[str, Any]) -> bool:
        haystack = hostile.get("_type", "") + " " + hostile.get("_name", "")
        return any(token in haystack for token in self._blacklist())

    def _fightable(self, hostiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in hostiles if row.get("_los") and not self._blacklisted(row)]

    def _task_busy(self) -> bool:
        current = getattr(self.plugin, "_task_current", None)
        active = getattr(self.plugin, "_active_task", None)
        return current is not None or (active is not None and not active.done())

    def _trigger(self, health: float, max_health: float, food: float,
                 hostiles: list[dict[str, Any]]) -> bool:
        if hostiles:
            return True
        if health <= self._flee_health() or food <= self._eat_food():
            return True
        return health < max_health - 2 and food < self._eat_health_food()

    def _must_abort(self, status: dict[str, Any], health: float,
                    hostiles: list[dict[str, Any]], nearest: dict[str, Any]) -> bool:
        if not hostiles:
            return False
        if health <= self._flee_health():
            return True
        if status.get("on_fire") or status.get("in_water"):
            return True
        if len(hostiles) > self._max_targets():
            return True
        for row in hostiles:
            if "creeper" in row.get("_type", "") and row["distance"] <= 5.0:
                return True
        if not self.engaged and health < self._fight_health():
            return True
        return False

    def _can_start(self, health: float, hostiles: list[dict[str, Any]]) -> bool:
        if health < self._fight_health():
            return False
        fightable = self._fightable(hostiles)
        return bool(fightable) and len(fightable) <= self._max_targets()

    def _can_continue(self, health: float, hostiles: list[dict[str, Any]]) -> bool:
        fightable = self._fightable(hostiles)
        if not fightable:
            return False
        if health <= self._flee_health():
            return False
        if health <= self._hold_health():
            if not self._finish_low_target() or len(fightable) != 1:
                return False
            target = fightable[0]
            return target["_health"] <= 2 and target["distance"] <= 3.2
        return True

    def _should_eat(self, health: float, max_health: float, food: float) -> bool:
        if time.monotonic() - self.last_eat < _num(self._cfg("auto_eat_cooldown_seconds", 15), 15.0):
            return False
        if food <= self._eat_food():
            return True
        return health < max_health - 2 and food < self._eat_health_food()

    def _describe(self, health: float, food: float, hostiles: list[dict[str, Any]]) -> str:
        parts = [f"血量 {health:.0f}/20", f"饱食 {food:.0f}/20"]
        if hostiles:
            nearest = min(hostiles, key=lambda row: row["distance"])
            name = str(nearest.get("name") or nearest.get("type") or "敌对生物")
            parts.append(f"{len(hostiles)}只敌对生物，最近 {name} {nearest['distance']:.1f} 格")
        return "，".join(parts)

    # -------------------------------------------------------------- actions

    async def _eat(self, status: dict[str, Any]) -> None:
        before_food = _num(status.get("food"), 0)
        before_health = _num(status.get("health"), 0)
        try:
            result = await self.plugin._skills_for().eat_until_full()
            after = await self.plugin._call("status", timeout=8)
            after_food = _num(after.get("food"), before_food) if not _error(after) else before_food
            self.last_eat = time.monotonic()
            self.last_action = time.monotonic()
            self.stats["eats"] += 1
            eaten = result.get("eaten") if isinstance(result, dict) else ""
            await self._notify(
                self._describe(before_health, before_food, []),
                f"自动进食（{eaten or '已吃'}）",
                f"饱食 {before_food:.0f} → {after_food:.0f}/20",
                key="eat",
            )
        except Exception as exc:
            self.last_eat = time.monotonic()
            self.stats["errors"] += 1
            await self._notify(
                self._describe(before_health, before_food, []),
                "自动进食失败",
                redact_text(str(exc))[:120],
                key="eat_failed",
            )

    async def _flee(self, status: dict[str, Any], hostile: dict[str, Any], reason: str) -> None:
        self.engaged = False
        self.anchor = None
        try:
            if self._task_busy():
                try:
                    await self.plugin._stop_task_runtime()
                except Exception:
                    pass
            outcome = await self.plugin._auto_flee(status, hostile)
            self.last_action = time.monotonic()
            confirmed = "已确认到达" in str(outcome)
            self.retreat_until = self.last_action + (
                _num(self._cfg("auto_flee_cooldown_seconds", 15), 15.0)
                if confirmed else 5.0
            )
            self.stats["flees"] += 1
            await self._notify(
                self._describe(_num(status.get("health"), 0), _num(status.get("food"), 0), [hostile]),
                f"撤离（{reason}）",
                redact_text(str(outcome))[:120],
                key="flee",
            )
        except Exception as exc:
            self.stats["errors"] += 1
            await self._notify(
                self._describe(_num(status.get("health"), 0), _num(status.get("food"), 0), [hostile]),
                "撤离失败",
                redact_text(str(exc))[:120],
                key="flee_failed",
            )

    async def _fight(self, status: dict[str, Any], hostile: dict[str, Any],
                     hostiles: list[dict[str, Any]]) -> None:
        target = str(hostile.get("type") or hostile.get("name") or "")
        self.engaged = True
        if self.anchor is None and isinstance(status.get("position"), dict):
            self.anchor = dict(status["position"])
        try:
            result = await self.plugin._skills_for().combat(
                target=target,
                max_seconds=_int(self._cfg("auto_fight_max_seconds", 25), 25, 5, 120),
                retreat_health=int(self._flee_health()),
                hold_health=int(self._hold_health()),
                finish_low_target=self._finish_low_target(),
                max_targets=self._max_targets(),
                leash_radius=self._leash(),
                blacklist=self._blacklist(),
            )
            self.last_action = time.monotonic()
            self.stats["fights"] += 1
            outcome = result.get("result") if isinstance(result, dict) else str(result)
            if outcome in {"retreated", "leash_exceeded", "timeout", "unreachable"}:
                self.retreat_until = self.last_action + _num(
                    self._cfg("auto_flee_cooldown_seconds", 15), 15.0)
            detail = result.get("detail") if isinstance(result, dict) else ""
            await self._notify(
                self._describe(_num(status.get("health"), 0), _num(status.get("food"), 0), hostiles),
                "自卫战斗",
                f"{outcome or '结束'}" + (f"：{detail}" if detail else ""),
                key="fight",
            )
        except Exception as exc:
            self.stats["errors"] += 1
            await self._notify(
                self._describe(_num(status.get("health"), 0), _num(status.get("food"), 0), hostiles),
                "自卫战斗失败",
                redact_text(str(exc))[:120],
                key="fight_failed",
            )
        finally:
            self.engaged = False

    async def _notify(self, trigger: str, action: str, result: str, *, key: str) -> None:
        if not self._notify_enabled():
            return
        now = time.monotonic()
        cooldown = max(5.0, _num(self._cfg("auto_survive_cooldown_seconds", 30), 30.0))
        if key == self.last_notice_key and now - self.last_notice < cooldown:
            return
        if now - self.last_notice < 5.0:
            return
        route = None
        getter = getattr(self.plugin, "_survival_notice_route", None)
        if callable(getter):
            try:
                route = getter()
            except Exception:
                route = None
        if route is None:
            logger.info("[mc-bridge] survival notice (no private route): %s / %s / %s", trigger, action, result)
            return
        text = redact_text(f"[MC生存] 触发：{trigger}；处理：{action}；结果：{result}")
        try:
            sent = await self.plugin._task_chat(text, route=route, kind="survival")
            if sent:
                self.last_notice = now
                self.last_notice_key = key
                self.stats["notices"] += 1
        except Exception as exc:
            logger.warning("[mc-bridge] survival notice failed: %s", type(exc).__name__)
