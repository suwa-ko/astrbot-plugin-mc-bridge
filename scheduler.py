"""Priority, dependency and cancellation policy for Minecraft tasks.

This module deliberately has no AstrBot, network or Minecraft imports.  It is the
single policy layer used by the plugin task worker: the worker still executes a
task, while this scheduler decides which queued task may run next and records its
lifecycle.  Local goals keep their own client-side scheduler; this layer never
duplicates their action loops.
"""
from __future__ import annotations

import itertools
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Iterable


class Priority(IntEnum):
    EMERGENCY = 0
    OWNER = 10
    NORMAL = 20
    AUTONOMY = 30
    BACKGROUND = 40


class TaskState(str):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}


@dataclass
class ScheduledTask:
    id: str
    kind: str
    goal: str
    payload: Any = None
    priority: int = Priority.NORMAL
    resources: tuple[str, ...] = ("actor",)
    dependencies: tuple[str, ...] = ()
    max_attempts: int = 1
    deadline: float = 0.0
    route: Any = None
    preemptible: bool = False
    state: str = TaskState.PENDING
    attempts: int = 0
    error: str = ""
    result: Any = None
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    lease_id: str = ""
    lease_seconds: float = 600.0
    next_attempt_at: float = 0.0
    retry_delay: float = 0.0
    blocked_reason: str = ""
    goal_outcome: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


@dataclass
class Lease:
    id: str
    task_id: str
    resources: tuple[str, ...]
    acquired_at: float
    expires_at: float
    renewals: int = 0


class UnifiedScheduler:
    """One actor, priority-ordered queue with retry and dependency policy.

    `take_next` accepts the plugin's existing deque, so older callers that append
    tasks directly keep working.  Tasks registered through `register` get full
    metadata; unknown tasks are treated as normal-priority owner work.
    """

    def __init__(self, clock=time.monotonic, *, max_pending: int = 0, lease_ttl: float = 600.0):
        self.clock = clock
        self._tasks: dict[str, ScheduledTask] = {}
        self._sequence = itertools.count(1)
        self._lease_sequence = itertools.count(1)
        self._audit_sequence = itertools.count(1)
        self._run_id = uuid.uuid4().hex[:8]
        self.active: ScheduledTask | None = None
        self.history: list[ScheduledTask] = []
        self.leases: dict[str, Lease] = {}
        self._resource_owner: dict[str, str] = {}
        self.audit: list[dict[str, Any]] = []
        self.max_pending = max(0, int(max_pending))
        self.lease_ttl = max(1.0, float(lease_ttl))

    # ------------------------------------------------------------------ submit
    def new_id(self, prefix: str = "task") -> str:
        return f"{prefix}-{self._run_id}-{next(self._sequence):04d}"

    def _audit(self, event: str, task: ScheduledTask | None = None, **extra: Any) -> None:
        row: dict[str, Any] = {"seq": next(self._audit_sequence), "at": self.clock(), "event": event}
        if task is not None:
            row.update({
                "task_id": task.id,
                "kind": task.kind,
                "state": task.state,
                "priority": int(task.priority),
                "resources": list(task.resources),
            })
        row.update(extra)
        self.audit.append(row)
        if len(self.audit) > 200:
            del self.audit[:-200]

    def pending(self) -> list[ScheduledTask]:
        return [task for task in self._tasks.values() if task.state == TaskState.PENDING]

    def task_status(self, task_id: str) -> dict[str, Any] | None:
        task = self._tasks.get(str(task_id or ""))
        return self._brief(task) if task is not None else None

    def register(
        self,
        task: ScheduledTask | None = None,
        *,
        task_id: str = "",
        kind: str = "",
        goal: str = "",
        payload: Any = None,
        priority: int = Priority.NORMAL,
        resources: Iterable[str] = ("actor",),
        dependencies: Iterable[str] = (),
        max_attempts: int = 1,
        deadline: float = 0.0,
        route: Any = None,
        preemptible: bool = False,
        retry_delay: float = 0.0,
        lease_seconds: float = 0.0,
    ) -> ScheduledTask:
        if task is None:
            task = ScheduledTask(
                id=task_id or self.new_id(kind or "task"),
                kind=kind,
                goal=goal,
                payload=payload,
                priority=int(priority),
                resources=tuple(resources),
                dependencies=tuple(dependencies),
                max_attempts=max(1, int(max_attempts)),
                deadline=float(deadline),
                route=route,
                preemptible=preemptible,
            )
        else:
            task.id = task.id or self.new_id(task.kind or "task")
            task.priority = int(task.priority)
            task.resources = tuple(task.resources or ("actor",))
            task.dependencies = tuple(task.dependencies or ())
            task.max_attempts = max(1, int(task.max_attempts or 1))
            task.attempts = max(0, int(task.attempts or 0))
            task.state = task.state if task.state in {TaskState.PENDING, TaskState.RUNNING} else TaskState.PENDING
        task.retry_delay = max(0.0, float(retry_delay))
        task.lease_seconds = max(1.0, float(lease_seconds or self.lease_ttl))
        if self.max_pending and task.state == TaskState.PENDING and len(self.pending()) >= self.max_pending:
            self._audit("rejected_backpressure", task, max_pending=self.max_pending)
            raise RuntimeError("scheduler backpressure: pending queue is full")
        self._tasks[task.id] = task
        self._audit("registered", task)
        return task

    def get(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    # ------------------------------------------------------------------ leases
    def lease_holders(self) -> dict[str, str]:
        self.expire_leases()
        return dict(self._resource_owner)

    def resources_free(self, resources: Iterable[str]) -> bool:
        self.expire_leases()
        return not any(str(resource) in self._resource_owner for resource in resources)

    def acquire(self, task: ScheduledTask) -> bool:
        if task.lease_id and task.lease_id in self.leases:
            return True
        self.expire_leases()
        resources = tuple(str(resource) for resource in (task.resources or ("actor",)))
        if any(resource in self._resource_owner for resource in resources):
            task.blocked_reason = "resources held by another task"
            return False
        now = self.clock()
        lease = Lease(
            id=f"lease-{next(self._lease_sequence):06d}",
            task_id=task.id,
            resources=resources,
            acquired_at=now,
            expires_at=now + max(1.0, float(task.lease_seconds or self.lease_ttl)),
        )
        self.leases[lease.id] = lease
        for resource in resources:
            self._resource_owner[resource] = task.id
        task.lease_id = lease.id
        task.blocked_reason = ""
        self._audit("lease_acquired", task, lease=lease.id, expires_at=lease.expires_at)
        return True

    def renew(self, task: ScheduledTask, seconds: float | None = None) -> bool:
        lease = self.leases.get(task.lease_id)
        if lease is None:
            return False
        lease.expires_at = self.clock() + max(1.0, float(seconds or task.lease_seconds or self.lease_ttl))
        lease.renewals += 1
        self._audit("lease_renewed", task, lease=lease.id, expires_at=lease.expires_at)
        return True

    def release(self, task: ScheduledTask) -> None:
        lease = self.leases.pop(task.lease_id, None) if task.lease_id else None
        if lease is not None:
            for resource in lease.resources:
                if self._resource_owner.get(resource) == task.id:
                    self._resource_owner.pop(resource, None)
            self._audit("lease_released", task, lease=lease.id)
        task.lease_id = ""

    def expire_leases(self, now: float | None = None) -> list[Lease]:
        now = self.clock() if now is None else float(now)
        expired = []
        for lease in list(self.leases.values()):
            if lease.expires_at > now:
                continue
            self.leases.pop(lease.id, None)
            for resource in lease.resources:
                if self._resource_owner.get(resource) == lease.task_id:
                    self._resource_owner.pop(resource, None)
            task = self._tasks.get(lease.task_id)
            if task is not None:
                task.lease_id = ""
                self._audit("lease_expired", task, lease=lease.id, expires_at=lease.expires_at)
            expired.append(lease)
        return expired

    # ------------------------------------------------------------------ order
    def _task_of(self, item: Any) -> ScheduledTask:
        task = self._tasks.get(str(getattr(item, "task_id", "") or ""))
        if task is None:
            task = self.register(
                kind=str(getattr(item, "kind", "") or "task"),
                goal=str(getattr(item, "goal", "") or ""),
                payload=item,
                priority=Priority.NORMAL,
                route=getattr(item, "reply_route", None),
            )
            try:
                setattr(item, "task_id", task.id)
            except Exception:
                pass
        task.payload = item
        return task

    def _ready(self, task: ScheduledTask, now: float) -> bool:
        if task.state != TaskState.PENDING:
            return False
        if task.next_attempt_at and now < task.next_attempt_at:
            task.blocked_reason = "retry backoff"
            return False
        if task.deadline and now > task.deadline:
            task.state = TaskState.FAILED
            task.error = "task deadline exceeded"
            task.finished_at = now
            self._audit("deadline_exceeded", task)
            return False
        for dependency in task.dependencies:
            if dependency == task.id:
                continue
            other = self._tasks.get(dependency)
            if other is None:
                return False
            if other.state == TaskState.COMPLETED:
                outcome = other.goal_outcome or "fulfilled"
                if outcome != "fulfilled":
                    task.state = TaskState.FAILED
                    task.error = f"dependency {dependency} {outcome}"
                    task.finished_at = now
                    self._audit("dependency_unmet", task, dependency=dependency, outcome=outcome)
                    return False
                continue
            if other.state in {TaskState.FAILED, TaskState.CANCELLED}:
                task.state = TaskState.FAILED
                task.error = f"dependency {dependency} {other.state}"
                task.finished_at = now
                self._audit("dependency_unmet", task, dependency=dependency, outcome=other.state)
                return False
            return False
        return True

    def take_next(self, queue) -> Any | None:
        """Remove and return the highest-priority ready item from `queue`."""
        self.watchdog()
        if self.active is not None:
            return None
        now = self.clock()
        best = None
        best_task = None
        for index, item in enumerate(queue):
            task = self._task_of(item)
            if not self._ready(task, now):
                continue
            if not self.resources_free(task.resources):
                task.blocked_reason = "resources held by another lease"
                continue
            key = (int(task.priority), task.created_at, index)
            if best is None or key < best[0]:
                best = (key, index)
                best_task = task
        if best is None:
            return None
        _, index = best
        try:
            item = queue[index]
        except TypeError:
            item = list(queue)[index]
            try:
                queue.remove(item)
            except ValueError:
                return None
        else:
            del queue[index]
        try:
            self.start(best_task, item)
        except RuntimeError:
            try:
                queue.insert(index, item)
            except AttributeError:
                queue.appendleft(item)
            return None
        return item

    def start(self, task: ScheduledTask, item: Any = None) -> ScheduledTask:
        if task.state != TaskState.PENDING:
            raise ValueError(f"task {task.id} is not pending")
        if not self.acquire(task):
            raise RuntimeError(f"resources unavailable for task {task.id}")
        task.payload = item if item is not None else task.payload
        task.attempts += 1
        task.state = TaskState.RUNNING
        task.started_at = self.clock()
        task.error = ""
        task.next_attempt_at = 0.0
        task.blocked_reason = ""
        self.active = task
        self._audit("started", task, attempt=task.attempts)
        return task

    # ------------------------------------------------------------------ finish
    def finish(
        self,
        item: Any,
        *,
        success: bool,
        result: Any = None,
        error: str = "",
        retryable: bool = False,
        goal_outcome: str = "fulfilled",
    ) -> bool:
        """Finish the active task. Returns True when it was requeued for retry."""
        task = self.active
        if task is None:
            task = self._task_of(item)
        now = self.clock()
        task.result = result
        task.finished_at = now
        self.release(task)
        if success:
            task.state = TaskState.COMPLETED
            task.goal_outcome = str(goal_outcome or "fulfilled")
            task.error = ""
            self._audit("completed", task, goal_outcome=task.goal_outcome)
            self._close_active(task)
            return False
        task.error = str(error or "task failed")
        if retryable and task.attempts < task.max_attempts:
            task.state = TaskState.PENDING
            task.goal_outcome = ""
            task.started_at = 0.0
            task.finished_at = 0.0
            task.next_attempt_at = now + max(0.0, float(task.retry_delay or 0.0))
            self._audit("retry_scheduled", task, next_attempt_at=task.next_attempt_at)
            self._close_active(task)
            return True
        task.state = TaskState.FAILED
        task.goal_outcome = "unmet"
        self._audit("failed", task, error=task.error)
        self._close_active(task)
        return False

    def _close_active(self, task: ScheduledTask) -> None:
        if self.active is task:
            self.active = None
        self.history.append(task)
        self._tasks[task.id] = task

    def cancel(self, task_id: str, reason: str = "cancelled", *, propagate: bool = True) -> bool:
        return self._cancel(task_id, str(reason), propagate, set())

    def _cancel(self, task_id: str, reason: str, propagate: bool, seen: set[str]) -> bool:
        if task_id in seen:
            return False
        seen.add(task_id)
        task = self._tasks.get(task_id)
        if task is None:
            return False
        if task.state in TERMINAL:
            return False
        task.state = TaskState.CANCELLED
        task.goal_outcome = task.goal_outcome or "unknown"
        task.error = str(reason)
        task.finished_at = self.clock()
        self.release(task)
        if self.active is task:
            self.active = None
        self._audit("cancelled", task, reason=reason)
        self.history.append(task)
        if propagate:
            for other in list(self._tasks.values()):
                if task_id in other.dependencies and other.state in {TaskState.PENDING, TaskState.RUNNING}:
                    self._cancel(other.id, f"dependency cancelled: {reason}", True, seen)
        return True

    def cancel_all(self, reason: str = "cancelled") -> list[ScheduledTask]:
        candidates = [
            task.id for task in self._tasks.values()
            if task.state in {TaskState.PENDING, TaskState.RUNNING}
        ]
        for task_id in candidates:
            self.cancel(task_id, reason, propagate=True)
        self.active = None
        return [self._tasks[task_id] for task_id in candidates if self._tasks[task_id].state == TaskState.CANCELLED]

    # ------------------------------------------------------------------ policy
    def should_preempt(self, priority: int, resources: Iterable[str] | None = None) -> bool:
        if self.active is None or not self.active.preemptible:
            return False
        if int(priority) >= int(self.active.priority):
            return False
        if resources is not None and not (set(str(item) for item in resources) & set(self.active.resources)):
            return False
        return True

    def can_start(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.state != TaskState.PENDING:
            return False
        if not self._ready(task, self.clock()):
            return False
        if self.active is not None and set(task.resources) & set(self.active.resources):
            return False
        return self.resources_free(task.resources)

    def watchdog(self, now: float | None = None) -> str:
        """Expire leases and stop an active task whose deadline/lease is gone."""
        now = self.clock() if now is None else float(now)
        expired = self.expire_leases(now)
        task = self.active
        if task is None:
            return "lease_expired" if expired else ""
        if task.deadline and now > task.deadline:
            self.cancel(task.id, "watchdog: task deadline exceeded", propagate=True)
            return "deadline_exceeded"
        if not task.lease_id or task.lease_id not in self.leases:
            self.cancel(task.id, "watchdog: lease expired", propagate=True)
            return "lease_expired"
        return ""

    def snapshot(self) -> dict[str, Any]:
        pending = [task for task in self._tasks.values() if task.state == TaskState.PENDING]
        pending.sort(key=lambda task: (int(task.priority), task.created_at, task.id))
        return {
            "active": self._brief(self.active) if self.active else None,
            "pending": [self._brief(task) for task in pending],
            "pending_count": len(pending),
            "history_count": len(self.history),
            "last": self._brief(self.history[-1]) if self.history else None,
            "leases": [
                {"id": lease.id, "task_id": lease.task_id, "resources": list(lease.resources),
                 "expires_at": lease.expires_at, "renewals": lease.renewals}
                for lease in self.leases.values()
            ],
            "lease_count": len(self.leases),
            "audit_count": len(self.audit),
            "audit_tail": list(self.audit[-8:]),
        }

    @staticmethod
    def _brief(task: ScheduledTask) -> dict[str, Any]:
        return {
            "id": task.id,
            "kind": task.kind,
            "goal": task.goal,
            "priority": int(task.priority),
            "state": task.state,
            "attempts": task.attempts,
            "resources": list(task.resources),
            "error": task.error,
            "lease_id": task.lease_id,
            "next_attempt_at": task.next_attempt_at,
            "blocked_reason": task.blocked_reason,
            "result": task.result,
            "goal_outcome": task.goal_outcome,
        }
