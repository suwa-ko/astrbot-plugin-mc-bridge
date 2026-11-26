"""Task runtime, scheduler integration, plan runner, recovery, and break guards."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import time
import uuid
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from ..break_policy import BreakPolicy
from ..contracts import L0_OPCODE_NAMES, L1_SPECS, L2_SPECS
from ..core import ActionError, SurvivalError
from ..farm_memory import FarmMemory
from ..intent import classify_task
from ..memory_store import MemoryStore
from ..poi_memory import PoiMemory
from ..privacy import redact_text
from ..reply_route import REPLY_ROUTE, UNSET, ReplyRoute, event_route, game_route
from ..scheduler import Priority, ScheduledTask, TaskState, UnifiedScheduler
from ..short_history import ShortHistoryStore
from ..skills import HUNTABLE, HUNT_ALIASES, SkillError, VanillaSkills, parse_transfer_items
from ..task_checkpoint import baseline_from_status, clean_checkpoint, resume_plan, validate_resume
from ..task_journal import TaskJournal
from ..task_metrics import TaskMetrics
from ..task_recovery import TaskRecovery
from ..telemetry import CURRENT_TRACE, Telemetry, ToolHooks, Trace, observed_context
from .context import CONTROL_EPOCH, CURRENT_EVENT, PLUGIN_NAME, RESOURCE_MENTION
from .server import BridgeError, QueuedTask, classify_goal_outcome


class RuntimeMixin:
    """Core task scheduling, execution, recovery, and world-safety mixin."""

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

    def _chain_dependency(self, depends_on: str = "") -> str:
        """Return the previous task in this Agent turn, preserving time order."""
        depends_on = str(depends_on or "").strip()
        if depends_on:
            return depends_on
        event = CURRENT_EVENT.get()
        chain = getattr(self, "_task_chain", None)
        if event is None or not isinstance(chain, dict) or chain.get("event") is not event:
            return ""
        last = str(chain.get("last") or "")
        if last:
            try:
                task = self._scheduler_for().get(last)
                if task is not None and str(getattr(task, "state", "")) in {"failed", "cancelled"}:
                    self._task_chain = None
                    return ""
            except Exception:
                pass
        return last

    def _remember_task_chain(self, task_id: str) -> None:
        event = CURRENT_EVENT.get()
        if event is not None and task_id:
            self._task_chain = {"event": event, "last": str(task_id)}

    def _plan_limits(self, steps):
        seconds = 0
        breaks = 0
        resources = set()
        for step in steps:
            opcode = str(step.get("opcode") or "").strip()
            spec = self._agent_skill_spec(opcode)
            if spec is None or opcode in {"plan"}:
                raise SkillError(f"计划步骤不支持：{opcode or '空'}")
            seconds += int(spec.max_seconds)
            resources.update(resource.value for resource in spec.resources)
            breaks += {
                "mine_ore": 640, "return_home": 640,
                "tunnel_to": 128, "gather": 128,
            }.get(opcode, 64)
        return min(3600, max(30, seconds)), min(4096, max(64, breaks)), tuple(sorted(resources))

    @staticmethod
    def _plan_steps_key(steps) -> str:
        try:
            return json.dumps(steps or [], ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(steps)

    def _find_matching_plan(self, steps):
        key = self._plan_steps_key(steps)
        current = getattr(self, "_task_current", None)
        candidates = []
        if current is not None:
            candidates.append(current)
        candidates.extend(list(getattr(self, "_task_queue", []) or []))
        for existing in candidates:
            if existing is None or str(getattr(existing, "kind", "") or "") != "plan":
                continue
            existing_steps = (getattr(existing, "params", {}) or {}).get("steps") or []
            if self._plan_steps_key(existing_steps) == key:
                task_id = str(getattr(existing, "task_id", "") or "")
                if task_id:
                    return task_id
        return ""

    def _enqueue_plan(self, steps, *, priority: int = int(Priority.OWNER), goal: str = "多步骤计划"):
        existing = self._find_matching_plan(steps)
        if existing:
            return existing
        seconds, breaks, resources = self._plan_limits(steps)
        task = QueuedTask(
            kind="plan",
            goal=goal,
            target="plan",
            params={"steps": steps, "break_budget": breaks},
            priority=int(priority),
            resources=resources or ("actor",),
            budget_seconds=float(seconds),
        )
        if not self._enqueue_tasks([task], priority=int(priority)):
            return self._find_matching_plan(steps)
        self._remember_task_chain(task.task_id)
        return task.task_id

    def _queue_skill(self, label: str, name: str, **params) -> bool:
        self._resume_attempts = 0
        spec = self._agent_skill_spec(name)
        resources = tuple(resource.value for resource in spec.resources) if spec is not None else ("actor",)
        budget = float(spec.max_seconds) if spec is not None else 0.0
        dependency = self._chain_dependency()
        task = QueuedTask(
            kind="skill",
            goal=label,
            target=name,
            params=params,
            resources=resources,
            budget_seconds=budget,
            dependencies=(dependency,) if dependency else (),
        )
        if not self._enqueue_tasks([task]):
            return False
        self._remember_task_chain(task.task_id)
        return True

    async def _gather_guard(self, event, resource: str) -> str | None:
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
        # Keep the documented L2 spelling and accept the shorter user-facing
        # alias used by the tool description and by the model.
        if opcode == "smelt":
            opcode = "smelt_item"
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

    def _farm_memory_for(self) -> FarmMemory:
        memory = getattr(self, "_farm_memory", None)
        if memory is None:
            memory = FarmMemory(Path(StarTools.get_data_dir(PLUGIN_NAME)) / "farm_plots.json")
            self._farm_memory = memory
        return memory

    def _memory_store_for(self) -> MemoryStore:
        store = getattr(self, "_memory_store", None)
        if store is None:
            root = Path(StarTools.get_data_dir(PLUGIN_NAME))
            store = MemoryStore(root / "memory.db")
            try:
                legacy = root / "poi_memory.json"
                if legacy.exists():
                    store.migrate_legacy_file(legacy)
            except Exception as exc:
                logger.warning("[mc-bridge] legacy memory migration deferred: %s", type(exc).__name__)
            self._memory_store = store
        return store

    def _memory_scope_for(self, status: dict | None = None) -> dict:
        status = status if isinstance(status, dict) else {}
        world_key = str(status.get("world_key") or "")
        dimension = str(status.get("dimension") or "")
        instance = ""
        if world_key:
            cache = getattr(self, "_memory_world_cache", None)
            if not isinstance(cache, dict):
                cache = {}
                self._memory_world_cache = cache
            instance = str(cache.get(world_key, "") or "")
            if not instance:
                try:
                    instance = self._memory_store_for().ensure_world(world_key)
                    cache[world_key] = instance
                except Exception:
                    instance = ""
        return {"world_key": world_key, "world_instance": instance, "dimension": dimension}

    def _group_whitelist(self) -> set[str]:
        raw = self.config.get("shared_memory_group_whitelist", "") or self.config.get("memory_group_whitelist", "")
        if isinstance(raw, list):
            values = [str(item).strip() for item in raw]
        else:
            values = [item.strip() for item in re.split(r"[,，;\s]+", str(raw)) if item.strip()]
        return {item for item in values if item}

    def _owner_private(self, event) -> bool:
        if event is None:
            return False
        try:
            platform = event.get_platform_name()
            private = event.is_private_chat()
        except Exception:
            return False
        if platform == "aiocqhttp" and private:
            owner_qq = str(self.config.get("shared_memory_owner_qq", "")).strip()
            try:
                sender = str(event.get_sender_id())
            except Exception:
                sender = ""
            if owner_qq:
                return bool(sender) and sender == owner_qq
            try:
                return bool(event.is_admin())
            except Exception:
                return False
        if platform == "webchat" and private:
            try:
                return bool(event.is_admin())
            except Exception:
                return False
        return False

    def _audience(self, event) -> str:
        """Return the effective destination, not just the speaker identity."""
        if event is None:
            return "unknown"
        try:
            platform = event.get_platform_name()
            private = event.is_private_chat()
        except Exception:
            return "unknown"
        if platform == "minecraft":
            if (event.get_extra("_mc_connection_id", None) == self.bridge.session_id
                    and event.get_extra("_mc_trusted_player", "")):
                return "public_mc"
            return "unknown"
        if platform in {"aiocqhttp", "webchat"} and private:
            if self._owner_private(event):
                try:
                    route = event_route(event, self.bridge)
                    if route is not None:
                        self._last_owner_route = route
                except Exception:
                    pass
                return "owner_private"
            return "unknown"
        if platform == "aiocqhttp" and not private:
            try:
                target = str(event.unified_msg_origin)
            except Exception:
                target = ""
            parts = target.rsplit(":", 1)
            gid = parts[-1] if len(parts) == 2 and parts[-1].isdigit() else ""
            whitelist = self._group_whitelist()
            if gid and (gid in whitelist or target in whitelist):
                return "group:" + gid
        return "unknown"

    def _survival_notice_route(self):
        """Owner-private route for survival notices; never public game chat."""
        shared = getattr(self, "_shared_memory", None)
        target = str(getattr(shared, "owner_session", "") or "").strip()
        if target:
            if re.fullmatch(r".+:FriendMessage:\d+", target):
                return ReplyRoute("qq", target=target)
            if target.startswith("webchat:"):
                return ReplyRoute("session", target=target)
        route = getattr(self, "_last_owner_route", None)
        return route if isinstance(route, ReplyRoute) else None

    async def _on_survival_event(self, payload: dict) -> None:
        supervisor = getattr(self, "_survival", None)
        if supervisor is not None:
            await supervisor.on_event(payload)

    def _note_world_status(self, status: dict) -> None:
        if not isinstance(status, dict):
            return
        world_key = str(status.get("world_key") or "")
        if not world_key:
            return
        shared = getattr(self, "_shared_memory", None)
        if shared is not None:
            try:
                shared.note_world_key(world_key)
            except Exception:
                pass
        cache = getattr(self, "_memory_world_cache", None)
        if isinstance(cache, dict) and world_key in cache:
            return
        if getattr(self, "_memory_store", None) is None:
            return
        try:
            self._memory_scope_for(status)
        except Exception:
            pass

    @staticmethod
    def _task_result_summary(result) -> str:
        if not isinstance(result, dict):
            return redact_text(result)[:300]
        bits = []
        for key in ("skill", "status", "goal_outcome", "partial", "complete", "success",
                    "requested", "confirmed", "collected", "moved", "planted", "placed",
                    "mined", "killed", "crafted", "smelted", "arrived_near"):
            value = result.get(key)
            if value is None:
                continue
            if isinstance(value, (int, float, bool)):
                bits.append(f"{key}={value}")
            elif isinstance(value, str):
                bits.append(f"{key}={redact_text(value)[:80]}")
            elif isinstance(value, list):
                bits.append(f"{key}={len(value)}项")
        return redact_text("；".join(bits))[:400]

    @staticmethod
    def _confirmed_count(result) -> int:
        if not isinstance(result, dict):
            return 0
        for key in ("confirmed", "collected", "moved", "planted", "placed", "mined", "killed"):
            value = result.get(key)
            if isinstance(value, bool):
                continue
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
        items = result.get("items")
        return len(items) if isinstance(items, list) else 0

    def _remember_home_bed(self, status: dict, bed: dict, stand: dict | None = None,
                           *, source: str = "task_result", notes: str = ""):
        store = getattr(self, "_memory_store", None)
        if store is None or not isinstance(status, dict) or not isinstance(bed, dict):
            return None
        try:
            scope = self._memory_scope_for(status)
            return store.remember_place(
                world_key=scope["world_key"], world_instance=scope["world_instance"],
                dimension=scope["dimension"], kind="home_bed", key="main", name="重生点",
                target=(bed.get("x"), bed.get("y"), bed.get("z")), stand=stand,
                target_status="valid",
                stand_status="valid" if isinstance(stand, dict) and stand.get("x") is not None else "unknown",
                observation_status="loaded",
                confidence=0.95, source=source, privacy="owner_only",
                notes=notes or "verified home bed", actor="home_bed",
            )
        except Exception as exc:
            logger.warning("[mc-bridge] structured home_bed write deferred: %s", type(exc).__name__)
            return None

    def _record_task_terminal_event(self, item, status: str, *, result=None, error="", goal_outcome=""):
        store = getattr(self, "_memory_store", None)
        if store is None:
            return None
        task_id = str(getattr(item, "task_id", "") or "")
        if not task_id:
            return None
        try:
            baseline = getattr(item, "baseline", {}) or {}
            last_status = getattr(self, "_last_status", {}) or {}
            world_key = baseline.get("world_key") or last_status.get("world_key", "")
            dimension = baseline.get("dimension") or last_status.get("dimension", "")
            scope = self._memory_scope_for({
                "world_key": world_key,
                "dimension": dimension,
            })
            scheduled = self._scheduler_for().get(task_id)
            attempts = int(getattr(scheduled, "attempts", 0) or getattr(item, "attempts", 0) or 1)
            position = baseline.get("position") if isinstance(baseline.get("position"), dict) else {}
            if not position and isinstance(last_status.get("position"), dict):
                position = dict(last_status["position"])
            target = None
            if str(getattr(item, "kind", "") or "") == "goto":
                target = {"x": getattr(item, "x", None), "y": getattr(item, "y", None), "z": getattr(item, "z", None)}
            event_type = {
                "completed": "task_completed",
                "failed": "task_failed",
                "cancelled": "task_cancelled",
            }.get(status, "task_completed")
            error_code = type(error).__name__ if not isinstance(error, str) and error else str(error or "")[:80]
            summary = self._task_result_summary(result)
            try:
                place_ref = str((getattr(item, "params", {}) or {}).get("place_ref") or "")
                if place_ref:
                    place = store.get_place(place_ref)
                    if place:
                        label = store.safe_view(place, audience="owner_private").get("label")
                        if label:
                            summary = f"到达 {label}；{summary}" if summary else f"到达 {label}"
            except Exception:
                pass
            store.record_task_event(
                task_id=task_id,
                run_id=str(attempts),
                event_type=event_type,
                opcode=str(getattr(item, "kind", "") or ""),
                goal=str(getattr(item, "goal", "") or ""),
                status=status,
                goal_outcome=str(goal_outcome or getattr(item, "goal_outcome", "") or ""),
                world_key=scope.get("world_key", ""),
                world_instance=scope.get("world_instance", ""),
                dimension=scope.get("dimension", ""),
                position=position,
                target=target,
                confirmed_count=self._confirmed_count(result),
                error_code=error_code,
                error=str(error or ""),
                result_summary=summary,
                observation_scope="task_worker",
                source="task_worker",
            )
            return task_id
        except Exception as exc:
            logger.warning("[mc-bridge] task event index deferred: %s", type(exc).__name__)
            return None


    def _break_budget_for_task(self, item) -> int | None:
        """Choose a task budget that matches the kind of work being done.

        General navigation keeps the configured per-task limit.  Deliberate
        digging/climbing tasks receive larger, separately configurable budgets.
        """
        opcode = str(getattr(item, "target", "") or "")
        keys = {
            "mine_ore": ("break_budget_per_mining_task", 640),
            "return_home": ("break_budget_per_return_task", 640),
            "tunnel_to": ("break_budget_per_tunnel_task", 128),
            "gather": ("break_budget_per_gather_task", 128),
        }
        setting = keys.get(opcode)
        if setting is None:
            if opcode == "plan":
                try:
                    return max(64, min(4096, int((getattr(item, "params", {}) or {}).get("break_budget", 2048) or 2048)))
                except (TypeError, ValueError):
                    return 2048
            return None
        key, default = setting
        try:
            return int(self.config.get(key, default) or default)
        except (TypeError, ValueError):
            return default

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
                radius=0, kind="container", dimension=dimension,
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
        if item.kind == "plan":
            return await self._run_plan_task(item)
        if item.kind == "skill":
            return await self._skills_for().run(str(item.target or ""), getattr(item, "params", {}))
        raise BridgeError("未知任务类型")

    async def _run_plan_task(self, item):
        """Run a bounded sequence of skills in one task, with checkpoints.

        A failed step stops the plan unless the step sets ``continue_on_failure``.
        Intermediate steps never trigger the normal per-task chat notification;
        the plan sends one final summary instead.
        """
        steps = [step for step in (item.params.get("steps") or []) if isinstance(step, dict)]
        if not steps:
            raise SkillError("计划没有步骤")
        results = []
        completed_count = 0
        partial_count = 0
        failed = 0
        for index, step in enumerate(steps):
            opcode = str(step.get("opcode") or "").strip()
            params = dict(step.get("params") or {})
            if not opcode:
                raise SkillError("计划步骤缺少 opcode")
            if opcode in {"goto", "come"}:
                x = int(params.get("x", 0) or 0)
                y = int(params.get("y", 0) or 0)
                z = int(params.get("z", 0) or 0)
                step_item = QueuedTask(
                    kind=opcode, goal=f"{opcode} {x} {y} {z}",
                    target=str(params.get("player") or ""), x=x, y=y, z=z,
                    params=params, task_id=f"{item.task_id}-s{index}",
                )
            else:
                step_item = QueuedTask(
                    kind="skill", goal=opcode, target=opcode,
                    params=params, task_id=f"{item.task_id}-s{index}",
                )
            try:
                result = await self._execute_task_item(step_item)
                step_outcome = classify_goal_outcome(result, step_item.kind)
                results.append({
                    "opcode": opcode,
                    "status": "completed",
                    "goal_outcome": step_outcome,
                    "result": result,
                })
                completed_count += 1
                if step_outcome == "partial":
                    partial_count += 1
                item.checkpoint = {"plan": {"next": index + 1, "results": results[-4:]}}
            except asyncio.CancelledError:
                item.checkpoint = {"plan": {"next": index, "results": results[-4:]}}
                raise
            except Exception as exc:
                failed += 1
                results.append({"opcode": opcode, "status": "failed", "error": str(exc)})
                keep_going = bool(step.get("continue_on_failure", False))
                item.checkpoint = {"plan": {"next": index + (1 if keep_going else 0), "results": results[-4:]}}
                if not keep_going:
                    break
        outcome = (
            "fulfilled" if failed == 0 and partial_count == 0
            else ("partial" if completed_count else "unmet")
        )
        summary = {
            "skill": "plan",
            "goal_outcome": outcome,
            "completed": completed_count,
            "partial_steps": partial_count,
            "total": len(steps),
            "failed": failed,
            "steps": results[-8:],
        }
        label = {"fulfilled": "计划完成", "partial": "计划部分完成", "unmet": "计划未达成"}[outcome]
        await self._task_chat(
            f"{label}：{completed_count}/{len(steps)} 步成功" + (f"，{failed} 步失败" if failed else ""),
            kind="plan",
        )
        return summary

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
                    self._break_policy_for().begin_task(
                        str(getattr(item, "task_id", "") or "direct"),
                        per_task=self._break_budget_for_task(item),
                    )
                    await self._capture_task_baseline(item)
                    result = await self._run_with_lease(item, budget)
                    outcome = classify_goal_outcome(result, item.kind)
                    item.goal_outcome = outcome
                    if item.kind == "skill" and not getattr(item, "suppress_notify", False):
                        label = {"fulfilled": "技能完成", "partial": "技能部分完成", "unmet": "技能未达成"}.get(outcome, "技能结束")
                        await self._task_chat(f"{label}（{getattr(item, 'task_id', '')}）：{json.dumps(result, ensure_ascii=False, separators=(',', ':'))}")
                    self._scheduler_for().finish(item, success=True, result=result, goal_outcome=outcome)
                    self._record_task_terminal_event(item, "completed", result=result, goal_outcome=outcome)
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
                    self._record_task_terminal_event(item, "cancelled", error="task cancelled", goal_outcome="unknown")
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
                    self._record_task_terminal_event(item, "failed", error=exc, goal_outcome="unmet")
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
        self._interrupted_task = None
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
        self._interrupted_task=None
        self._cancel_active_task()
        for job in self._agent_jobs:
            if job is not asyncio.current_task():job.cancel()
