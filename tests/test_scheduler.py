import importlib
import unittest
from collections import deque
from types import SimpleNamespace

from test_base import m

sched = importlib.import_module(m.__name__ + ".scheduler")


def item(task):
    return SimpleNamespace(task_id=task.id, kind=task.kind, goal=task.goal, reply_route=None)


class SchedulerTests(unittest.TestCase):
    def make(self):
        return sched.UnifiedScheduler(clock=lambda: 100.0)

    def test_dependency_requires_goal_fulfilled(self):
        scheduler = self.make()
        dep = scheduler.register(kind="skill", goal="dep")
        queue = deque([item(dep)])
        picked = scheduler.take_next(queue)
        scheduler.finish(picked, success=True, result={"requested": 2, "moved": 1}, goal_outcome="partial")
        child = scheduler.register(kind="skill", goal="child", dependencies=(dep.id,))
        child_queue = deque([item(child)])
        self.assertIsNone(scheduler.take_next(child_queue))
        self.assertEqual(child.state, sched.TaskState.FAILED)
        self.assertIn("partial", child.error)

    def test_dependency_allows_fulfilled_goal(self):
        scheduler = self.make()
        dep = scheduler.register(kind="skill", goal="dep")
        queue = deque([item(dep)])
        picked = scheduler.take_next(queue)
        scheduler.finish(picked, success=True, result={"requested": 2, "moved": 2}, goal_outcome="fulfilled")
        child = scheduler.register(kind="skill", goal="child", dependencies=(dep.id,))
        child_queue = deque([item(child)])
        self.assertEqual(scheduler.take_next(child_queue).task_id, child.id)

    def test_task_ids_are_unique_across_scheduler_instances(self):
        first = self.make().new_id("skill")
        second = self.make().new_id("skill")
        self.assertNotEqual(first, second)

    def test_priority_prefers_owner_over_autonomy(self):
        scheduler = self.make()
        low = scheduler.register(kind="collect", goal="autonomy", priority=sched.Priority.AUTONOMY)
        high = scheduler.register(kind="goto", goal="owner", priority=sched.Priority.OWNER)
        queue = deque([item(low), item(high)])
        picked = scheduler.take_next(queue)
        self.assertEqual(picked.task_id, high.id)
        self.assertEqual(scheduler.active.id, high.id)
        self.assertEqual(len(queue), 1)

    def test_equal_priority_is_fifo(self):
        scheduler = self.make()
        first = scheduler.register(kind="goto", goal="first")
        second = scheduler.register(kind="goto", goal="second")
        queue = deque([item(first), item(second)])
        picked = scheduler.take_next(queue)
        self.assertEqual(picked.task_id, first.id)
        scheduler.finish(picked, success=True)
        picked = scheduler.take_next(queue)
        self.assertEqual(picked.task_id, second.id)

    def test_dependency_blocks_until_completed(self):
        scheduler = self.make()
        base = scheduler.register(kind="craft", goal="base")
        child = scheduler.register(kind="collect", goal="child", dependencies=(base.id,))
        queue = deque([item(base), item(child)])
        self.assertEqual(scheduler.take_next(queue).task_id, base.id)
        self.assertEqual(len(queue), 1)
        scheduler.finish(item(base), success=True)
        self.assertEqual(scheduler.take_next(queue).task_id, child.id)

    def test_retry_budget_requeues_then_fails(self):
        scheduler = self.make()
        task = scheduler.register(kind="collect", goal="retry", max_attempts=2)
        queue = deque([item(task)])
        picked = scheduler.take_next(queue)
        self.assertTrue(scheduler.finish(picked, success=False, error="timeout", retryable=True))
        self.assertEqual(task.state, sched.TaskState.PENDING)
        queue.append(item(task))
        picked = scheduler.take_next(queue)
        self.assertFalse(scheduler.finish(picked, success=False, error="timeout", retryable=True))
        self.assertEqual(task.state, sched.TaskState.FAILED)
        self.assertEqual(task.attempts, 2)

    def test_cancel_all_marks_pending_and_active(self):
        scheduler = self.make()
        active = scheduler.register(kind="goto", goal="active")
        pending = scheduler.register(kind="goto", goal="pending")
        queue = deque([item(active)])
        scheduler.take_next(queue)
        scheduler.cancel_all("owner stop")
        self.assertEqual(active.state, sched.TaskState.CANCELLED)
        self.assertEqual(pending.state, sched.TaskState.CANCELLED)
        self.assertIsNone(scheduler.active)

    def test_preemption_only_for_preemptible_lower_priority(self):
        scheduler = self.make()
        task = scheduler.register(kind="collect", goal="autonomy", priority=sched.Priority.AUTONOMY, preemptible=True)
        scheduler.take_next(deque([item(task)]))
        self.assertTrue(scheduler.should_preempt(sched.Priority.OWNER))
        self.assertFalse(scheduler.should_preempt(sched.Priority.AUTONOMY))
        task.preemptible = False
        self.assertFalse(scheduler.should_preempt(sched.Priority.EMERGENCY))

    def test_expired_task_is_not_taken(self):
        scheduler = sched.UnifiedScheduler(clock=lambda: 200.0)
        task = scheduler.register(kind="goto", goal="late", deadline=150.0)
        self.assertIsNone(scheduler.take_next(deque([item(task)])))
        self.assertEqual(task.state, sched.TaskState.FAILED)

    def test_snapshot_reports_pending(self):
        scheduler = self.make()
        scheduler.register(kind="goto", goal="goal", priority=sched.Priority.OWNER)
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["pending_count"], 1)
        self.assertEqual(snapshot["pending"][0]["goal"], "goal")
        self.assertEqual(snapshot["pending"][0]["priority"], 10)

    def test_lease_blocks_conflicting_resources_and_releases(self):
        scheduler = self.make()
        first = scheduler.register(kind="goto", goal="first", resources=("actor",))
        second = scheduler.register(kind="goto", goal="second", resources=("actor",))
        queue = deque([item(first), item(second)])
        self.assertEqual(scheduler.take_next(queue).task_id, first.id)
        self.assertFalse(scheduler.resources_free(("actor",)))
        self.assertIsNone(scheduler.take_next(queue))
        scheduler.finish(item(first), success=True)
        self.assertTrue(scheduler.resources_free(("actor",)))
        self.assertEqual(scheduler.take_next(queue).task_id, second.id)

    def test_cancel_propagates_to_dependent_tasks(self):
        scheduler = self.make()
        base = scheduler.register(kind="goto", goal="base")
        child = scheduler.register(kind="collect", goal="child", dependencies=(base.id,))
        scheduler.take_next(deque([item(base)]))
        self.assertTrue(scheduler.cancel(base.id, "owner stop"))
        self.assertEqual(base.state, sched.TaskState.CANCELLED)
        self.assertEqual(child.state, sched.TaskState.CANCELLED)

    def test_retry_backoff_uses_next_attempt_time(self):
        now = [100.0]
        scheduler = sched.UnifiedScheduler(clock=lambda: now[0])
        task = scheduler.register(kind="collect", goal="retry", max_attempts=2, retry_delay=5)
        queue = deque([item(task)])
        picked = scheduler.take_next(queue)
        self.assertTrue(scheduler.finish(picked, success=False, error="timeout", retryable=True))
        self.assertEqual(task.next_attempt_at, 105.0)
        queue.append(item(task))
        self.assertIsNone(scheduler.take_next(queue))
        now[0] = 106.0
        self.assertEqual(scheduler.take_next(queue).task_id, task.id)

    def test_watchdog_cancels_active_when_lease_expires(self):
        now = [100.0]
        scheduler = sched.UnifiedScheduler(clock=lambda: now[0], lease_ttl=5)
        task = scheduler.register(kind="goto", goal="slow")
        scheduler.take_next(deque([item(task)]))
        now[0] = 106.0
        self.assertEqual(scheduler.watchdog(), "lease_expired")
        self.assertEqual(task.state, sched.TaskState.CANCELLED)
        self.assertIsNone(scheduler.active)

    def test_backpressure_rejects_second_pending_task(self):
        scheduler = sched.UnifiedScheduler(clock=lambda: 100.0, max_pending=1)
        scheduler.register(kind="goto", goal="first")
        with self.assertRaises(RuntimeError):
            scheduler.register(kind="goto", goal="second")

    def test_snapshot_includes_lease_and_audit(self):
        scheduler = self.make()
        task = scheduler.register(kind="goto", goal="leased")
        scheduler.take_next(deque([item(task)]))
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["lease_count"], 1)
        self.assertGreater(snapshot["audit_count"], 0)
        self.assertEqual(snapshot["active"]["lease_id"], task.lease_id)


if __name__ == "__main__":
    unittest.main()
