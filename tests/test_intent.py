import importlib
import unittest

from test_base import m

intent = importlib.import_module(m.__name__ + ".intent")


class IntentTests(unittest.TestCase):
    def test_gather_logs(self):
        result = intent.classify_task("mc砍8个木头")
        self.assertEqual(result["opcode"], "gather")
        self.assertEqual(result["params"], {"resource": "logs", "count": 8})

    def test_mine_coal(self):
        result = intent.classify_task("去挖点煤矿")
        self.assertEqual(result["opcode"], "mine_ore")
        self.assertEqual(result["params"]["resource"], "coal")
        self.assertEqual(result["params"]["count"], 8)

    def test_mine_coal_return_home(self):
        result = intent.classify_task("mc挖一个煤炭回来")
        self.assertEqual(result["opcode"], "mine_ore")
        self.assertEqual(result["params"]["resource"], "coal")
        self.assertEqual(result["params"]["count"], 1)
        self.assertTrue(result["params"]["return_home"])

    def test_mine_stone_count(self):
        result = intent.classify_task("挖32块石头")
        self.assertEqual(result["opcode"], "mine_ore")
        self.assertEqual(result["params"]["resource"], "stone")
        self.assertEqual(result["params"]["count"], 32)

    def test_craft_iron_pickaxe(self):
        result = intent.classify_task("合成一把铁镐")
        self.assertEqual(result["opcode"], "craft_item")
        self.assertEqual(result["params"]["item"], "minecraft:iron_pickaxe")
        self.assertEqual(result["params"]["count"], 1)

    def test_smelt_iron(self):
        result = intent.classify_task("烧5个铁")
        self.assertEqual(result["opcode"], "smelt_item")
        self.assertEqual(result["params"]["item"], "minecraft:iron_ingot")
        self.assertEqual(result["params"]["count"], 5)

    def test_home_sleep_before_return_home(self):
        result = intent.classify_task("回家睡觉")
        self.assertEqual(result["opcode"], "find_and_sleep")
        self.assertEqual(result["params"]["source"], "home")

    def test_return_home(self):
        result = intent.classify_task("回家")
        self.assertEqual(result["opcode"], "return_home")

    def test_combat_target(self):
        result = intent.classify_task("去打僵尸")
        self.assertEqual(result["opcode"], "combat")
        self.assertEqual(result["params"]["target"], "僵尸")

    def test_resupply_and_explore(self):
        self.assertEqual(intent.classify_task("补给")["opcode"], "resupply")
        self.assertEqual(intent.classify_task("探索一下")["opcode"], "explore")

    def test_container_transfer(self):
        result = intent.classify_task("把腐肉放进箱子")
        self.assertEqual(result["opcode"], "container_transfer")
        self.assertEqual(result["params"]["item"], "minecraft:rotten_flesh")
        self.assertEqual(result["params"]["direction"], "to_container")

    def test_unknown_is_empty(self):
        result = intent.classify_task("不知道")
        self.assertEqual(result["opcode"], "")
        self.assertEqual(result["confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
