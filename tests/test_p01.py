import importlib
import tempfile
import unittest
from pathlib import Path

from test_base import Event, make_plugin, m

cp = importlib.import_module(m.__name__ + '.chat_policy')
history_mod = importlib.import_module(m.__name__ + '.short_history')


class P01Routing(unittest.TestCase):
    def test_action_verbs_keep_action_tools_after_memory_words(self):
        for text, tool in [
            ("mc在刚刚提到的坐标放一个木头", "mc_place_at"),
            ("mc把右边的火把拆了", "mc_mine_at"),
            ("mc放木头", "mc_place_at"),
            ("mc打开门", "mc_interact"),
            ("mc扫描附近10格方块", "mc_scan"),
        ]:
            group, names = cp.tool_group(text, allow_actions=True, owner=True)
            self.assertNotEqual(group, "chat", text)
            self.assertIn(tool, names, text)

    def test_recall_question_stays_readonly(self):
        group, names = cp.tool_group("mc还记得上次去坐标1 64 2吗", allow_actions=True, owner=True)
        self.assertEqual(group, "chat")
        self.assertNotIn("mc_goto", names)
        self.assertIn("mc_scan", names)

    def test_confirmation_is_short(self):
        for text in ["对", "对的", "好", "继续", "OK", "是"]:
            self.assertTrue(cp.is_confirmation(text), text)
        for text in ["不对", "好的我们去挖矿", "是的然后呢"]:
            self.assertFalse(cp.is_confirmation(text), text)

    def test_action_intent(self):
        self.assertTrue(cp.action_intent("把右边的火把拆了"))
        self.assertTrue(cp.action_intent("在刚刚提到的坐标放一个木头"))
        self.assertFalse(cp.action_intent("你还记得上次那条消息吗"))

    def test_mc_short_history_survives_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "short_history.json"
            plugin = make_plugin()
            plugin._short_history = history_mod.ShortHistoryStore(path)
            event = Event("mc把红床设成重生点")
            event.unified_msg_origin = "default:FriendMessage:1"
            key, _ = plugin._mc_history(event)
            plugin._mc_record_history(key, "mc把红床设成重生点", "记下了")
            reloaded = make_plugin()
            reloaded._short_history = history_mod.ShortHistoryStore(path)
            event2 = Event("mc刚才的床呢")
            event2.unified_msg_origin = "default:FriendMessage:1"
            _, text = reloaded._mc_history_text(event2)
            self.assertIn("红床", text)
            self.assertIn("记下了", text)

    def test_mc_short_history_redacts_coordinates(self):
        plugin = make_plugin()
        event = Event("mc去坐标")
        event.unified_msg_origin = "default:FriendMessage:1"
        key, _ = plugin._mc_history(event)
        plugin._mc_record_history(key, "mc去坐标 646 89 -166", "到了 646 89 -166")
        _, text = plugin._mc_history_text(event)
        self.assertNotIn("646", text)
        self.assertNotIn("-166", text)
        self.assertIn("[坐标]", text)

    def test_mc_short_history_sanitizes_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "short_history.json"
            store = history_mod.ShortHistoryStore(path)
            key = "default:FriendMessage:1"
            store.set(key, ["主人：去坐标 -1 2 3", "你：到了 -1 2 3"])
            plugin = make_plugin()
            plugin._short_history = store
            event = Event("mc看看历史")
            event.unified_msg_origin = key
            _, text = plugin._mc_history_text(event)
            self.assertNotIn("-1 2 3", text)
            self.assertIn("[坐标]", text)
            reloaded = history_mod.ShortHistoryStore(path)
            stored = "\n".join(reloaded.get(key))
            self.assertNotIn("-1 2 3", stored)
            self.assertIn("[坐标]", stored)

    def test_mc_short_history_is_scoped_and_bounded(self):
        plugin = make_plugin()
        event = Event("mc放木头")
        event.unified_msg_origin = "default:FriendMessage:1"
        key, text = plugin._mc_history_text(event)
        self.assertEqual(text, "")
        plugin._mc_record_history(key, "mc放木头", "放在地上了")
        key2, text2 = plugin._mc_history_text(event)
        self.assertEqual(key, key2)
        self.assertIn("mc放木头", text2)
        self.assertIn("放在地上了", text2)
        other = Event("mc放木头")
        other.unified_msg_origin = "default:FriendMessage:2"
        _, other_text = plugin._mc_history_text(other)
        self.assertEqual(other_text, "")


if __name__ == '__main__':
    unittest.main()
