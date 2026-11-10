import importlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from test_base import Event, make_plugin, m

memory_store = importlib.import_module(m.__name__ + ".memory_store")
privacy = importlib.import_module(m.__name__ + ".privacy")


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = memory_store.MemoryStore(self.root / "memory.db")
        self.world = self.store.ensure_world("world-a")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def remember(self, *, kind="home_bed", key="main", target=(1, 64, 2), stand=(2, 64, 2),
                 world="world-a", instance=None, dimension="minecraft:overworld", **kwargs):
        return self.store.remember_place(
            world_key=world,
            world_instance=self.world if instance is None else instance,
            dimension=dimension,
            kind=kind,
            key=key,
            target=target,
            stand=stand,
            target_status=kwargs.pop("target_status", "valid"),
            stand_status=kwargs.pop("stand_status", "valid"),
            observation_status=kwargs.pop("observation_status", "loaded"),
            **kwargs,
        )

    def test_legacy_migration_is_idempotent_and_does_not_fake_verification(self):
        legacy = self.root / "poi.json"
        legacy.write_text(json.dumps({"version": 1, "items": [
            {"kind": "home_bed", "key": "main", "name": "家床", "world_key": "world-a",
             "dimension": "minecraft:overworld", "x": 1, "y": 64, "z": 2, "updated_at": time.time()},
            {"kind": "chest", "key": "main", "name": "箱子", "world_key": "world-a",
             "dimension": "minecraft:overworld", "x": 3, "y": 64, "z": 4,
             "updated_at": time.time() - 30 * 86400},
        ]}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.store.migrate_legacy_file(legacy), 2)
        self.assertEqual(self.store.migrate_legacy_file(legacy), 0)
        rows = self.store.find_places(world_key="world-a", dimension="minecraft:overworld")
        self.assertEqual(len(rows), 2)
        bed = next(row for row in rows if row["kind"] == "home_bed")
        self.assertEqual((bed["target_x"], bed["target_y"], bed["target_z"]), (1, 64, 2))
        self.assertIsNone(bed["last_verified_at"])
        self.assertEqual(bed["source"], "legacy")
        self.assertEqual(bed["privacy"], "owner_only")
        self.assertEqual(bed["world_instance"], "")
        self.assertEqual(bed["status"], "candidate")
        chest = next(row for row in rows if row["kind"] == "chest")
        self.assertEqual(chest["status"], "stale")

    def test_world_instance_and_dimension_filtering(self):
        first = self.remember(target=(1, 64, 2), stand=(2, 64, 2))
        second = self.store.remember_place(
            world_key="world-b", world_instance="w_other", dimension="minecraft:overworld",
            kind="home_bed", key="main", target=(9, 64, 9), stand=(8, 64, 9),
            target_status="valid", stand_status="valid", observation_status="loaded",
        )
        rows = self.store.find_places(world_key="world-a", world_instance=self.world,
                                      dimension="minecraft:overworld")
        self.assertEqual([row["public_ref"] for row in rows], [first["public_ref"]])
        rows = self.store.find_places(world_key="world-b", world_instance="w_other",
                                      dimension="minecraft:overworld")
        self.assertEqual([row["public_ref"] for row in rows], [second["public_ref"]])
        self.assertEqual(self.store.find_places(world_key="world-b", dimension="minecraft:the_nether"), [])

    def test_unverified_conflict_keeps_active_and_verified_conflict_supersedes(self):
        active = self.remember(target=(1, 64, 2), stand=(2, 64, 2))
        candidate = self.remember(
            target=(10, 64, 2), stand=(11, 64, 2),
            target_status="invalid", stand_status="invalid", observation_status="unloaded",
            status="candidate",
        )
        self.assertEqual(self.store.get_place(active["public_ref"])["status"], "active")
        self.assertEqual(self.store.get_place(candidate["public_ref"])["status"], "candidate")

        newer = self.remember(target=(20, 64, 2), stand=(21, 64, 2))
        self.assertEqual(self.store.get_place(active["public_ref"])["status"], "stale")
        self.assertEqual(self.store.get_place(newer["public_ref"])["status"], "active")
        self.assertEqual(self.store.get_place(newer["public_ref"])["supersedes_id"], active["id"])

    def test_expiry_and_navigation_requirements(self):
        old = self.store.remember_place(
            world_key="world-a", world_instance=self.world, dimension="minecraft:overworld",
            kind="home_bed", key="main", target=(1, 64, 2), stand=(2, 64, 2),
            target_status="valid", stand_status="valid", observation_status="loaded",
            last_verified_at=time.time() - 200 * 86400, updated_at=time.time() - 200 * 86400,
        )
        self.assertTrue(self.store.is_expired(old))
        self.assertTrue(any("有效期" in item for item in self.store.warnings(old)))
        row, error = self.store.resolve_for_navigation(
            old["public_ref"], world_key="world-a", world_instance=self.world,
            dimension="minecraft:overworld",
        )
        self.assertIsNone(row)
        self.assertIn("有效期", error)

    def test_legacy_unbound_place_cannot_auto_navigate(self):
        legacy = self.remember(instance="", target=(1, 64, 2), stand=(2, 64, 2))
        self.assertIn("未绑定", self.store.warnings(legacy)[-1])
        row, error = self.store.resolve_for_navigation(
            legacy["public_ref"], world_key="world-a", world_instance=self.world,
            dimension="minecraft:overworld",
        )
        self.assertIsNone(row)
        self.assertIn("未绑定", error)

    def test_audience_filter_hides_owner_private_and_other_group_places(self):
        private = self.remember(privacy="owner_only")
        shared = self.remember(kind="waypoint", key="public", privacy="group_shared")
        group_one = self.remember(kind="waypoint", key="g1", privacy="group_only:111")
        group_two = self.remember(kind="waypoint", key="g2", privacy="group_only:222")
        base = dict(world_key="world-a", world_instance=self.world, dimension="minecraft:overworld")
        self.assertEqual({row["public_ref"] for row in self.store.find_places(**base, audience="owner_private")},
                         {private["public_ref"], shared["public_ref"], group_one["public_ref"], group_two["public_ref"]})
        self.assertEqual({row["public_ref"] for row in self.store.find_places(**base, audience="group:111")},
                         {shared["public_ref"], group_one["public_ref"]})
        self.assertEqual({row["public_ref"] for row in self.store.find_places(**base, audience="public_mc")},
                         {shared["public_ref"]})

    def test_safe_view_contains_no_coordinates_or_world_identity(self):
        row = self.remember(name="家床", target=(-375, 94, -154), stand=(-375, 94, -153))
        view = self.store.safe_view(row)
        payload = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("-375", payload)
        self.assertNotIn("-154", payload)
        self.assertNotIn("world-a", payload)
        self.assertNotIn(self.world, payload)
        self.assertIn("place_ref", view)
        local = self.store.local_view(row)
        self.assertEqual(local["target"], {"x": -375, "y": 94, "z": -154})
        self.assertIn("-375", self.store.local_card(row))

    def test_safe_view_refuses_owner_private_for_group(self):
        row = self.remember(privacy="owner_only")
        self.assertEqual(self.store.safe_view(row, audience="group:111"), {})
        self.assertTrue(self.store.safe_view(row, audience="owner_private"))
        shared = self.remember(kind="waypoint", key="public", privacy="group_shared")
        self.assertTrue(self.store.safe_view(shared, audience="group:111"))

    def test_legacy_migration_does_not_resurrect_forgotten_place(self):
        legacy = self.root / "poi_retry.json"
        legacy.write_text(json.dumps({"version": 1, "items": [
            {"kind": "home_bed", "key": "main", "name": "家床", "world_key": "world-a",
             "dimension": "minecraft:overworld", "x": 1, "y": 64, "z": 2, "updated_at": time.time()},
        ]}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.store.migrate_legacy_file(legacy), 1)
        row = self.store.find_places(world_key="world-a", dimension="minecraft:overworld")[0]
        self.assertTrue(self.store.forget_place(row["public_ref"]))
        # Simulate a retry with a fresh migration marker: the tombstoned
        # source_key must still block resurrection.
        retry = self.root / "poi_retry.json"
        self.assertEqual(self.store.migrate_legacy_file(retry, migration_key="legacy_poi_retry"), 0)
        self.assertEqual(self.store.find_places(world_key="world-a", dimension="minecraft:overworld"), [])
        self.assertEqual(self.store.count_places(include_deleted=True), 1)

    def test_forget_uses_tombstone_and_does_not_return_place(self):
        row = self.remember()
        self.assertTrue(self.store.forget_place(row["public_ref"]))
        self.assertIsNone(self.store.get_place(row["public_ref"]))
        self.assertEqual(self.store.find_places(world_key="world-a", dimension="minecraft:overworld"), [])
        self.assertTrue(self.store.place_history(row["public_ref"]))

    def test_task_events_are_idempotent_and_outbox_text_is_redacted(self):
        first, inserted = self.store.record_task_event(
            task_id="task-1", run_id="1", event_type="task_completed", opcode="goto",
            goal="去 -350 94 -152", status="completed", goal_outcome="fulfilled",
            world_key="world-a", world_instance=self.world, dimension="minecraft:overworld",
            position={"x": -350, "y": 94, "z": -152}, confirmed_count=1,
            result_summary="到达 (-350, 94, -152)",
        )
        second, inserted_again = self.store.record_task_event(
            task_id="task-1", run_id="1", event_type="task_completed", opcode="goto",
            goal="去 -350 94 -152", status="completed", goal_outcome="fulfilled",
            world_key="world-a", world_instance=self.world, dimension="minecraft:overworld",
        )
        self.assertTrue(inserted)
        self.assertFalse(inserted_again)
        self.assertEqual(first, second)
        rows = self.store.pending_episodes()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("-350", rows[0]["content"])
        self.assertNotIn("-152", rows[0]["content"])
        self.assertNotIn("world-a", rows[0]["content"])

    def test_verify_place_records_stand_from_block_context(self):
        row = self.store.remember_place(
            world_key="world-a", world_instance=self.world, dimension="minecraft:overworld",
            kind="home_bed", key="main", target=(1, 64, 2), stand=None,
            target_status="unknown", stand_status="unknown", observation_status="unloaded",
            status="candidate",
        )
        self.assertIsNone(row["stand_x"])
        verified = self.store.verify_place(
            row["public_ref"], target_status="valid", stand_status="valid",
            observation_status="loaded", stand={"x": 2, "y": 64, "z": 2, "feet_y": 64},
        )
        self.assertEqual(verified["stand_x"], 2)
        self.assertTrue(self.store.safe_view(verified)["has_stand"])

    def test_verify_place_activates_and_navigation_resolves(self):
        row = self.remember(target_status="unknown", stand_status="unknown",
                            observation_status="unloaded", status="candidate")
        self.assertFalse(row["last_verified_at"])
        verified = self.store.verify_place(row["public_ref"])
        self.assertEqual(verified["status"], "active")
        resolved, error = self.store.resolve_for_navigation(
            row["public_ref"], world_key="world-a", world_instance=self.world,
            dimension="minecraft:overworld",
        )
        self.assertFalse(error)
        self.assertEqual(resolved["public_ref"], row["public_ref"])


class MemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = memory_store.MemoryStore(Path(self.tmp.name) / "memory.db")
        self.plugin = make_plugin()
        self.plugin._memory_store = self.store
        self.plugin._memory_world_cache = {}
        self.plugin._call = AsyncMock(return_value={
            "world_key": "world-a", "dimension": "minecraft:overworld",
            "position": {"x": 1, "y": 64, "z": 2},
        })
        self.status = {"world_key": "world-a", "dimension": "minecraft:overworld",
                       "position": {"x": 1, "y": 64, "z": 2}}
        self.scope = self.plugin._memory_scope_for(self.status)

    async def asyncTearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def remember(self, **kwargs):
        values = dict(world_key="world-a", world_instance=self.scope["world_instance"],
                      dimension="minecraft:overworld", kind="home_bed", key="main",
                      target=(-375, 94, -154), stand=(-375, 94, -153),
                      target_status="valid", stand_status="valid", observation_status="loaded")
        values.update(kwargs)
        return self.store.remember_place(**values)

    async def test_mc_memory_model_view_has_no_coordinates_and_card_does(self):
        row = self.remember()
        event = Event("MC家床在哪")
        result = json.loads(await self.plugin.mc_memory(event, operation="get", kind="home_bed"))
        self.assertTrue(result["found"])
        payload = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("-375", payload)
        self.assertNotIn("-154", payload)
        self.assertNotIn("world-a", payload)
        self.assertIn(row["public_ref"], payload)
        cards = event.get_extra("_mc_place_cards")
        self.assertTrue(cards)
        text = "\n".join(item.get("text", "") if isinstance(item, dict) else str(item)
                         for item in cards)
        self.assertIn("-375", text)

    async def test_mc_memory_can_read_saved_places_while_game_offline(self):
        self.remember()
        self.plugin._call = AsyncMock(return_value={"__error__": "not connected"})
        event = Event("MC家床在哪")
        result = json.loads(await self.plugin.mc_memory(event, operation="get", kind="home_bed"))
        self.assertTrue(result["found"])
        self.assertTrue(result["offline"])
        cards = event.get_extra("_mc_place_cards") or []
        text = "\n".join(item.get("text", "") if isinstance(item, dict) else str(item)
                         for item in cards)
        self.assertIn("-375", text)

    async def test_group_memory_only_returns_shared_projection(self):
        self.plugin.config["shared_memory_group_whitelist"] = "111"
        self.remember(kind="home_bed", key="main", privacy="owner_only")
        self.remember(kind="waypoint", key="public", target=(10, 64, 10),
                      stand=(11, 64, 10), privacy="group_shared")
        event = Event("MC有哪些共享地点", private=False)
        event.unified_msg_origin = "default:GroupMessage:111"
        result = json.loads(await self.plugin.mc_memory(event, operation="list", kind="all"))
        payload = json.dumps(result, ensure_ascii=False)
        self.assertIn("waypoint", payload)
        self.assertNotIn("home_bed", payload)
        self.assertNotIn("-375", payload)
        self.assertNotIn("world-a", payload)
        self.assertFalse(event.get_extra("_mc_place_cards"))

    async def test_group_cannot_read_private_place_by_forged_ref(self):
        self.plugin.config["shared_memory_group_whitelist"] = "111"
        owner_only = self.remember(kind="waypoint", key="secret", privacy="owner_only")
        other_group = self.remember(kind="waypoint", key="other", privacy="group_only:222")
        event = Event("MC查这个地点", private=False)
        event.unified_msg_origin = "default:GroupMessage:111"
        for ref in (owner_only["public_ref"], other_group["public_ref"]):
            result = json.loads(await self.plugin.mc_memory(
                event, operation="get", place_ref=ref))
            self.assertFalse(result["found"])
            self.assertNotIn("-375", json.dumps(result, ensure_ascii=False))

    async def test_group_cannot_write_memory(self):
        event = Event("MC记住这个点", private=False)
        event.unified_msg_origin = "default:GroupMessage:111"
        self.plugin.config["shared_memory_group_whitelist"] = "111"
        result = await self.plugin.mc_memory(event, operation="remember", kind="waypoint")
        self.assertIn("只读", result)
        self.plugin._call.assert_not_awaited()


class PrivacyTests(unittest.TestCase):
    def test_redacts_coordinates_world_keys_and_private_chat(self):
        text = "主人从家床(-375, 94, -154)出发，world_key=abc123，去 -350 94 -152"
        redacted = privacy.redact_text(text, world_keys=("abc123",))
        self.assertNotIn("-375", redacted)
        self.assertNotIn("-154", redacted)
        self.assertNotIn("-350", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertIn("[坐标]", redacted)
        self.assertIn("[世界]", redacted)
        self.assertTrue(privacy.contains_coordinate(text))
        self.assertFalse(privacy.contains_coordinate(redacted))

    def test_redacts_python_dict_coordinates(self):
        text = "工作台位于{'x': -339, 'y': 69, 'z': -188}，已返回。"
        redacted = privacy.redact_text(text)
        self.assertNotIn("-339", redacted)
        self.assertNotIn("-188", redacted)
        self.assertIn("[坐标]", redacted)
        self.assertTrue(privacy.contains_coordinate(text))
        self.assertFalse(privacy.contains_coordinate(redacted))

    def test_private_digest_never_keeps_verbatim_private_chat(self):
        text = "MC家建在山上，验证码是998877，记住这条路"
        digest = privacy.private_digest(text)
        self.assertNotIn("998877", digest)
        self.assertNotIn("MC家建在山上", digest)
        self.assertTrue(digest)


if __name__ == "__main__":
    unittest.main()
