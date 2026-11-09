import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite

spec = importlib.util.spec_from_file_location("shared_under_test", Path(os.environ["MC_PLUGIN_FILE"]).parent / "shared_memory.py")
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


class Event:
    def __init__(self, text="聊聊MC", sender="123456789", private=True, platform="aiocqhttp"):
        self.message_str, self.sender, self.private, self.platform = text, sender, private, platform
        self.unified_msg_origin = f"default:{'FriendMessage' if private else 'GroupMessage'}:{sender}"
        self.extra = {}
    def get_platform_name(self): return self.platform
    def is_private_chat(self): return self.private
    def get_sender_id(self): return self.sender
    def get_extra(self, key, default=None): return self.extra.get(key, default)
    def set_extra(self, key, value): self.extra[key] = value


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.native = await aiosqlite.connect(":memory:")
        await self.native.execute("CREATE TABLE documents(id INTEGER PRIMARY KEY,text TEXT,metadata TEXT)")
        self.policy = SimpleNamespace(is_paused=lambda _session: False)
        self.engine = SimpleNamespace(db_connection=self.native)
        self.engine.add_memory = AsyncMock(side_effect=self.add)
        self.engine.search_memories = AsyncMock(side_effect=self.search)
        self.engine.get_memory = AsyncMock(side_effect=self.get)
        self.engine.delete_memory = AsyncMock(side_effect=self.delete)
        manager = SimpleNamespace(user_memory_policy=self.policy, get=lambda _key, default=None: default)
        self.star = SimpleNamespace(activated=True, star_cls=SimpleNamespace(
            config_manager=manager, initializer=SimpleNamespace(memory_engine=self.engine, data_dir=self.root)))
        self.context = SimpleNamespace(get_registered_star=lambda _name: self.star)
        self.config = {"shared_memory_owner_qq": "123456789", "owner_name": "Player", "persona_id": "MinecraftBot"}
        self.service = m.McSharedMemory(self.context, self.config, self.root / "bridge")
        self.service._backfill_done = True
        await self.service.start()
        self.service.task.cancel()
        await asyncio.gather(self.service.task, return_exceptions=True)
        self.service.task = None
        self.bridge = SimpleNamespace(session_id="current")

    async def asyncTearDown(self):
        await self.service.stop()
        await self.native.close()
        self.tmp.cleanup()

    async def add(self, content, session_id, persona_id, importance, metadata):
        meta = dict(metadata, session_id=session_id, persona_id=persona_id, status="active")
        cursor = await self.native.execute("INSERT INTO documents(text,metadata) VALUES(?,?)", (content, json.dumps(meta)))
        await self.native.commit()
        return cursor.lastrowid

    async def search(self, query, k, session_id, persona_id):
        cursor = await self.native.execute("SELECT id,text,metadata FROM documents WHERE json_extract(metadata,'$.session_id')=? AND json_extract(metadata,'$.persona_id')=?", (session_id, persona_id))
        return [SimpleNamespace(doc_id=row[0], content=row[1], metadata=json.loads(row[2])) for row in await cursor.fetchall()][:k]

    async def get(self, identifier):
        cursor = await self.native.execute("SELECT text,metadata FROM documents WHERE id=?", (identifier,))
        row = await cursor.fetchone()
        return {"text": row[0], "metadata": json.loads(row[1])} if row else None

    async def delete(self, identifier):
        await self.native.execute("DELETE FROM documents WHERE id=?", (identifier,))
        await self.native.commit()
        return True

    def game_event(self, sender="Player"):
        event = Event("MinecraftBot还记得吗", platform="minecraft")
        event.extra.update(_mc_trusted_player=sender, _mc_sender_uuid="verified", _mc_connection_id="current")
        return event

    async def seed(self, content="MC家在山上", kind="game_event", occurred=None):
        await self.service.enqueue(content, kind, occurred or time.time(), "minecraft")
        await self.service.flush()

    async def test_owner_qq_and_game_share_native_scoped_memory(self):
        await self.seed()
        self.assertIn("MC家在山上", await self.service.recall(Event(), self.bridge, "MC家"))
        self.assertIn("MC家在山上", await self.service.recall(self.game_event(), self.bridge, "家"))
        self.engine.search_memories.assert_awaited_with("家", k=4, session_id=self.service.scope, persona_id="MinecraftBot")

    async def test_native_scoped_integration_check(self):
        await self.seed()
        await self.service.verify_native()
        self.assertTrue(self.service.retrieval_verified)

    async def test_just_finished_game_is_available_before_embedding_flush(self):
        await self.service.capture_game(self.game_event(), self.bridge, "MinecraftBot刚才砍树怎么样", "采到了原木")
        event = Event("你刚刚在游戏里做什么")
        result = await self.service.context_for(event, self.bridge, event.message_str)
        self.assertIn("原木", result)
        self.engine.search_memories.assert_not_awaited()

    async def test_other_named_game_does_not_open_mc_recall(self):
        self.assertFalse(self.service.relevant(Event("原神游戏里今天做了什么")))

    async def test_other_qq_and_groups_cannot_read(self):
        for event in [Event(sender="other"), Event(private=False), Event(sender="123456789", platform="other")]:
            self.assertIn("仅供主人", await self.service.recall(event, self.bridge, "MC"))
        self.engine.search_memories.assert_not_awaited()

    async def test_other_game_players_cannot_read_owner_private_material(self):
        self.assertIn("仅供主人", await self.service.recall(self.game_event("visitor"), self.bridge, "MC"))
        self.engine.search_memories.assert_not_awaited()

    async def test_stale_game_connection_is_denied(self):
        event = self.game_event()
        event.set_extra("_mc_connection_id", "previous")
        self.assertIn("仅供主人", await self.service.recall(event, self.bridge, "MC"))

    async def test_untrusted_display_name_is_not_identity(self):
        event = Event("<Player> MC记忆", platform="minecraft")
        self.assertIn("仅供主人", await self.service.recall(event, self.bridge, "MC"))

    async def test_ordinary_private_chat_is_not_captured(self):
        event = Event("今晚吃什么")
        self.service.prepare_qq(event)
        await self.service.capture_qq(event, "吃饭吧")
        await self.service.flush()
        self.engine.add_memory.assert_not_awaited()

    async def test_explicit_mc_only_extracts_safe_sentences(self):
        event = Event("MC家建在山上，我的考试成绩是99，验证码123456")
        self.service.prepare_qq(event)
        await self.service.capture_qq(event, "MC家可以建在山上。我的邮箱是secret@example.com")
        await self.service.flush()
        content = self.engine.add_memory.await_args.args[0]
        self.assertIn("MC", content)
        self.assertIn("建造", content)
        self.assertNotIn("MC家建在山上", content)
        self.assertNotIn("成绩", content)
        self.assertNotIn("验证码", content)
        self.assertNotIn("secret", content)

    async def test_mixed_sensitive_sentence_is_not_shared(self):
        event = Event("MC服务器密码是secret")
        self.service.prepare_qq(event)
        await self.service.capture_qq(event)
        self.engine.add_memory.assert_not_awaited()

    async def test_embedding_content_redacts_coordinates_and_world_key(self):
        event = self.game_event()
        await self.service.capture_game(
            event, self.bridge,
            "MC家床坐标是 -375 94 -154，world_key=abc123", "到了家床 -375 94 -154")
        await self.service.flush()
        content = self.engine.add_memory.await_args.args[0]
        self.assertNotIn("-375", content)
        self.assertNotIn("-154", content)
        self.assertNotIn("abc123", content)
        self.assertIn("[坐标]", content)

    async def test_old_document_result_is_redacted_before_context(self):
        meta = {
            "mc_shared": True, "owner_qq": self.service.owner,
            "session_id": self.service.scope, "persona_id": "MinecraftBot",
            "source_kind": "game_event", "source_platform": "minecraft",
            "occurred_at": time.time(), "status": "active",
        }
        await self.native.execute(
            "INSERT INTO documents(text,metadata) VALUES(?,?)",
            ("MC家床在 -375 94 -154，world_key=abc123", json.dumps(meta)))
        await self.native.commit()
        result = await self.service.recall(self.game_event(), self.bridge, "MC家床")
        self.assertNotIn("-375", result)
        self.assertNotIn("-154", result)
        self.assertNotIn("abc123", result)
        self.assertIn("[坐标]", result)

    async def test_private_digest_is_not_verbatim_private_chat(self):
        event = Event("MC家建在山上，验证码是998877")
        self.service.prepare_qq(event)
        await self.service.capture_qq(event, "MC家可以建在山上")
        await self.service.flush()
        content = self.engine.add_memory.await_args.args[0]
        self.assertNotIn("MC家建在山上", content)
        self.assertNotIn("998877", content)
        self.assertIn("建造", content)

    async def test_legacy_documents_with_coordinates_are_marked_redacted(self):
        meta = {
            "mc_shared": True, "owner_qq": self.service.owner,
            "session_id": self.service.scope, "persona_id": "MinecraftBot",
            "source_kind": "game_event", "source_platform": "minecraft",
            "occurred_at": time.time() - 86400, "status": "active",
        }
        await self.native.execute("INSERT INTO documents(text,metadata) VALUES(?,?)",
                                  ("MC家床 -375 94 -154", json.dumps(meta)))
        await self.native.commit()
        await self.service._redact_legacy_native()
        cursor = await self.native.execute("SELECT metadata FROM documents")
        stored = json.loads((await cursor.fetchone())[0])
        self.assertEqual(stored["status"], "redacted")
        self.assertFalse(stored["model_safe"])
        self.assertIn("redaction_reason", stored)
        # A date query may still read the local record, but the model-facing
        # output must be redacted.
        result = await self.service.recall(Event("昨天MC做了什么"), self.bridge, "MC", "昨天")
        self.assertNotIn("-375", result)
        self.assertIn("[坐标]", result)

    async def test_legacy_private_chat_documents_are_marked_redacted(self):
        meta = {
            "mc_shared": True, "owner_qq": self.service.owner,
            "session_id": self.service.scope, "persona_id": "MinecraftBot",
            "source_kind": "qq_mc_dialogue", "source_platform": "qq",
            "occurred_at": time.time(), "status": "active",
        }
        await self.native.execute("INSERT INTO documents(text,metadata) VALUES(?,?)",
                                  ("主人在QQ私聊中说了秘密内容", json.dumps(meta)))
        await self.native.commit()
        await self.service._redact_legacy_native()
        cursor = await self.native.execute("SELECT metadata FROM documents")
        stored = json.loads((await cursor.fetchone())[0])
        self.assertEqual(stored["status"], "redacted")

    async def test_new_sanitized_private_digest_is_not_marked_redacted(self):
        meta = {
            "mc_shared": True, "owner_qq": self.service.owner,
            "session_id": self.service.scope, "persona_id": "MinecraftBot",
            "source_kind": "qq_mc_dialogue", "source_platform": "qq",
            "occurred_at": time.time(), "status": "active",
        }
        await self.native.execute("INSERT INTO documents(text,metadata) VALUES(?,?)",
                                  ("主人在QQ私聊中提到了MC建造话题（原文已脱敏）", json.dumps(meta)))
        await self.native.commit()
        await self.service._redact_legacy_native()
        cursor = await self.native.execute("SELECT metadata FROM documents")
        stored = json.loads((await cursor.fetchone())[0])
        self.assertEqual(stored["status"], "active")

    async def test_group_recall_is_read_only_and_isolated(self):
        self.service.config["shared_memory_group_whitelist"] = "111,222"
        now = time.time()
        def doc(text, scope, visibility, source="task_terminal"):
            return (text, json.dumps({
                "mc_shared": True, "owner_qq": self.service.owner,
                "session_id": scope, "persona_id": "MinecraftBot",
                "visibility": visibility, "source_kind": source,
                "source_platform": "minecraft", "occurred_at": now, "status": "active",
            }))
        rows = [
            doc("OWNER-SECRET", self.service.scope, "owner_private"),
            doc("SHARED-INFO", self.service.shared_scope, "group_shared"),
            doc("NOT-SHARED", self.service.shared_scope, "owner_private"),
            doc("G111-INFO", self.service.group_scope("111"), "group_only:111"),
            doc("G222-INFO", self.service.group_scope("222"), "group_only:222"),
            doc("SHARED-COORD -375 94 -154", self.service.shared_scope, "group_shared"),
        ]
        await self.native.executemany("INSERT INTO documents(text,metadata) VALUES(?,?)", rows)
        await self.native.commit()
        event = Event(private=False)
        event.unified_msg_origin = "default:GroupMessage:111"
        result = await self.service.group_recall(event, self.bridge, "111", "MC")
        self.assertIn("SHARED-INFO", result)
        self.assertIn("G111-INFO", result)
        self.assertNotIn("OWNER-SECRET", result)
        self.assertNotIn("NOT-SHARED", result)
        self.assertNotIn("G222-INFO", result)
        self.assertNotIn("-375", result)
        event.unified_msg_origin = "default:GroupMessage:333"
        self.assertIn("没有开放", await self.service.group_recall(event, self.bridge, "333", "MC"))

    async def test_group_recall_sanitizes_search_query(self):
        self.service.config["shared_memory_group_whitelist"] = "111"
        event = Event(private=False)
        event.unified_msg_origin = "default:GroupMessage:111"
        await self.service.group_recall(event, self.bridge, "111", "MC家床 -375 94 -154 world_key=abc123")
        for call in self.engine.search_memories.await_args_list:
            query = call.args[0] if call.args else call.kwargs.get("query", "")
            self.assertNotIn("-375", query)
            self.assertNotIn("abc123", query)

    async def test_sensitive_query_does_not_touch_native_memory(self):
        result = await self.service.recall(self.game_event(), self.bridge, "我的成绩")
        self.assertIn("没有查询其他私聊", result)
        self.engine.search_memories.assert_not_awaited()

    async def test_pause_policy_applies_to_both_platforms(self):
        self.policy.is_paused = lambda _: True
        self.assertFalse(await self.service.enqueue("MC测试", "game_event", time.time(), "minecraft"))
        self.assertIn("暂停", await self.service.recall(self.game_event(), self.bridge, "MC"))
        self.engine.search_memories.assert_not_awaited()

    async def test_live_native_plugin_replacement_is_resolved(self):
        self.star.activated = False
        self.assertIsNone(self.service._native())
        self.star.activated = True
        self.assertIs(self.service._native(), self.engine)

    async def test_followup_reads_mc_but_does_not_capture_unrelated_content(self):
        self.service.prepare_qq(Event("聊聊MC"))
        event = Event("那你呢")
        self.assertTrue(self.service.relevant(event))
        self.service.prepare_qq(event)
        self.assertFalse(event.get_extra("_mc_shared_private"))

    async def test_outbox_is_idempotent_and_drops_synced_plaintext(self):
        now = time.time()
        self.assertTrue(await self.service.enqueue("MC原木", "game_event", now, "minecraft"))
        self.assertFalse(await self.service.enqueue("MC原木", "game_event", now, "minecraft"))
        await asyncio.gather(self.service.flush(), self.service.flush())
        self.engine.add_memory.assert_awaited_once()
        cursor = await self.service.db.execute("SELECT content,state FROM outbox")
        self.assertEqual(await cursor.fetchone(), ("", "synced"))

    async def test_different_owner_pending_rows_are_not_reindexed(self):
        await self.service.enqueue("MC旧主人", "game_event", time.time(), "minecraft")
        self.service.owner = "different"
        await self.service.flush()
        self.engine.add_memory.assert_not_awaited()

    async def test_wrong_scope_result_is_discarded_even_if_native_returns_it(self):
        self.engine.search_memories.side_effect = None
        self.engine.search_memories.return_value = [SimpleNamespace(doc_id=1, content="PRIVATE SECRET", metadata={"session_id": "other", "mc_shared": True})]
        result = await self.service.recall(Event(), self.bridge, "MC")
        self.assertNotIn("PRIVATE SECRET", result)

    async def test_date_query_uses_original_event_time(self):
        yesterday = datetime.now(m.TZ) - timedelta(days=1)
        await self.seed("MC昨天采木", occurred=yesterday.timestamp())
        await self.seed("MC今天盖房", occurred=time.time())
        result = await self.service.recall(Event("昨天MC干了什么"), self.bridge, "MC", "昨天")
        self.assertIn("昨天采木", result)
        self.assertNotIn("今天盖房", result)
        context = await self.service.context_for(Event("昨天MC做了什么"), self.bridge, "昨天MC做了什么")
        self.assertIn("昨天采木", context)
        self.assertNotIn("今天盖房", context)

    async def test_qq_can_forget_only_own_mc_record(self):
        await self.seed()
        result = await self.service.forget(Event("忘记这条MC记忆"), self.bridge, 1)
        self.assertIn("已忘记", result)
        self.assertIsNone(await self.get(1))
        await self.service.flush()
        self.assertIsNone(await self.get(1))

    async def test_game_cannot_delete_and_missing_intent_cannot_delete(self):
        await self.seed()
        self.assertIn("QQ私聊", await self.service.forget(self.game_event(), self.bridge, 1))
        self.assertIn("明确", await self.service.forget(Event("MC记忆"), self.bridge, 1))
        self.engine.delete_memory.assert_not_awaited()

    async def test_other_native_private_record_cannot_be_deleted(self):
        await self.add("private", "default:FriendMessage:123456789", "MinecraftBot", .5, {})
        self.assertIn("不是本人的MC", await self.service.forget(Event("忘记MC记忆"), self.bridge, 1))
        self.engine.delete_memory.assert_not_awaited()

    async def test_context_is_bounded_and_marked_as_data(self):
        await self.seed("MC经历" * 250)
        text = await self.service.context_for(Event(), self.bridge, "MC")
        self.assertTrue(text.startswith(m.MARKER))
        self.assertLess(len(text), 1400)

    async def test_backfill_filters_owner_session_role_platform_and_sensitive_text(self):
        path = self.root / "conversations.db"
        async with aiosqlite.connect(path) as db:
            await db.execute("CREATE TABLE message_archive(session_id TEXT,sender_id TEXT,role TEXT,platform TEXT,timestamp REAL,content TEXT)")
            rows = [
                (self.service.owner_session, "123456789", "user", "aiocqhttp", time.time(), "MC家在山上"),
                (self.service.owner_session, "123456789", "user", "aiocqhttp", time.time(), "MC密码secret"),
                ("default:GroupMessage:1", "123456789", "user", "aiocqhttp", time.time(), "MC群内容"),
                ("default:FriendMessage:other", "other", "user", "aiocqhttp", time.time(), "MC其他人"),
                (self.service.owner_session, "123456789", "assistant", "aiocqhttp", time.time(), "MC不应补录的回复"),
                (self.service.owner_session, "123456789", "user", "cron", time.time(), "MC定时任务"),
            ]
            await db.executemany("INSERT INTO message_archive VALUES(?,?,?,?,?,?)", rows)
            await db.commit()
        await self.service._backfill()
        await self.service.flush()
        self.assertEqual(self.service.stats["backfilled"], 1)
        self.engine.add_memory.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
