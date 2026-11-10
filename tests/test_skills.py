import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_base import m

skills_mod = importlib.import_module(m.__name__ + ".skills")


class FakePlugin:
    def __init__(self):
        self.calls = []
        self.runs = []
        self.approaches = []
        self.position = {"x": 5, "y": 64, "z": 0}
        self.inventory = []
        self.status_calls = 0
        self.scan_extra = {}
        self.beds = []
        self.bed_context = {}
        self.sleeping = False
        self.screen = "none"

    @staticmethod
    def _err(data):
        return data.get("__error__") if isinstance(data, dict) else None

    async def _call(self, action, params=None, timeout=10):
        self.calls.append((action, dict(params or {})))
        if action == "status":
            self.status_calls += 1
            return {
                "position": dict(self.position),
                "health": 20,
                "food": 20,
                "day_time": 0,
                "hostiles": [],
                "sleeping": self.sleeping,
                "can_sleep": True,
                "world_key": "test-world",
                "dimension": "minecraft:overworld",
                "screen": self.screen,
                "inventory_items": [dict(row) for row in self.inventory],
            }
        if action == "scan":
            result = {"resources": [{"x": 5, "y": 64, "z": 0, "distance": 0, "approaches": []}], "ground": []}
            result.update(self.scan_extra)
            return result
        if action == "scan_blocks":
            return {"blocks": [dict(row) for row in self.beds]}
        if action == "block_context":
            return dict(self.bed_context)
        if action == "scan_entities":
            return {"entities": []}
        if action == "move_item":
            return {"moved": 1}
        if action == "container":
            return {"operation": params.get("operation"), "closed": True}
        return {}

    async def _run_operation(self, action, params, seconds=10):
        self.runs.append((action, dict(params)))
        if action == "mine_at":
            self.inventory.append({"id": "minecraft:oak_log", "count": 1, "slot": 20})
        if action == "interact_block":
            self.sleeping = True
        if action == "workbench":
            self.screen = "Crafting"
        return {"phase": "completed", "success": True}

    async def _approach_target(self, x, y, z, seconds=20, reach=4.0, allow_break=False):
        self.approaches.append((x, y, z))
        return None


class FakeMemory:
    def __init__(self):
        self.items = {}

    def get(self, kind, key):
        return self.items.get(f"{kind}:{key}")

    def set(self, kind, key, **values):
        row = {"kind": kind, "key": key, **values}
        self.items[f"{kind}:{key}"] = row
        return row


class CombatPlugin(FakePlugin):
    def __init__(self, statuses, *, dead=True):
        super().__init__()
        self.statuses = list(statuses)
        self.index = 0
        self.dead = dead
        self.attacks = []
        self.fled = []

    async def _call(self, action, params=None, timeout=10):
        if action == "status":
            value = self.statuses[min(self.index, len(self.statuses) - 1)]
            self.index += 1
            return dict(value)
        if action == "attack":
            self.attacks.append(dict(params or {}))
            return {"target": "Zombie", "dead": self.dead, "health": 0 if self.dead else 12}
        return await super()._call(action, params, timeout)

    async def _auto_flee(self, status, hostile):
        self.fled.append(dict(hostile))
        return "已确认到达撤离落脚点"


class SkillTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_skill_is_rejected(self):
        with self.assertRaises(skills_mod.SkillError):
            await skills_mod.VanillaSkills(FakePlugin()).run("missing", {})

    async def test_gather_mines_and_verifies_inventory(self):
        plugin = FakePlugin()
        skills = skills_mod.VanillaSkills(plugin)
        result = await skills.gather("logs", 1, radius=6, max_seconds=30)
        self.assertEqual(result["collected"], 1)
        self.assertIn(("mine_at", {"x": 5, "y": 64, "z": 0}), plugin.runs)

    async def test_build_places_requested_positions(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "minecraft:cobblestone", "count": 4, "slot": 20}]
        skills = skills_mod.VanillaSkills(plugin)
        result = await skills.build("line", "minecraft:cobblestone", x=1, y=64, z=1, length=2, direction="east")
        self.assertEqual(result["placed"], 2)
        self.assertEqual([run[1] for run in plugin.runs], [
            {"x": 1, "y": 64, "z": 1, "item": "minecraft:cobblestone"},
            {"x": 2, "y": 64, "z": 1, "item": "minecraft:cobblestone"},
        ])

    async def test_build_rejects_insufficient_material(self):
        plugin = FakePlugin()
        skills = skills_mod.VanillaSkills(plugin)
        with self.assertRaises(skills_mod.SkillError):
            await skills.build("line", "minecraft:cobblestone", x=1, y=64, z=1, length=2)

    def test_validate_params_normalizes_gather_values(self):
        skills = skills_mod.VanillaSkills(FakePlugin())
        _, params = skills.validate_params("gather", {"resource": "石头", "count": "3", "radius": "8", "max_seconds": "60"})
        self.assertEqual(params["resource"], "stone")
        self.assertEqual(params["count"], 3)
        self.assertEqual(params["radius"], 8)
        self.assertEqual(params["max_seconds"], 60)

    def test_validate_params_rejects_invalid_gather_values(self):
        skills = skills_mod.VanillaSkills(FakePlugin())
        with self.assertRaises(skills_mod.SkillError):
            skills.validate_params("gather", {"count": "banana"})
        with self.assertRaises(skills_mod.SkillError):
            skills.validate_params("gather", {"resource": "not_a_resource"})

    def test_validate_params_enforces_batch_total_limit(self):
        skills = skills_mod.VanillaSkills(FakePlugin())
        with self.assertRaisesRegex(skills_mod.SkillError, "512"):
            skills.validate_params("container_transfer", {
                "items": [{"item": "minecraft:cobblestone", "count": 256}] * 3,
                "direction": "to_container",
            })

    async def test_transfer_reports_client_move_count(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "minecraft:cobblestone", "count": 4, "slot": 20}]
        skills = skills_mod.VanillaSkills(plugin)
        result = await skills.transfer("minecraft:cobblestone", 1, direction="to_container")
        self.assertEqual(result["moved"], 1)
        self.assertIn("move_item", [name for name, _ in plugin.calls])

    async def test_transfer_many_opens_and_closes_container_once(self):
        plugin = FakePlugin()
        skills = skills_mod.VanillaSkills(plugin)
        result = await skills.transfer_many(
            [
                {"item": "minecraft:golden_axe", "count": 1},
                {"item": "minecraft:furnace", "count": 1},
            ],
            direction="to_container",
            x=7,
            y=64,
            z=0,
        )
        self.assertEqual(result["moved"], 2)
        self.assertTrue(result["opened_once"])
        self.assertEqual(len([call for call in plugin.calls if call[0] == "move_item"]), 2)
        container_calls = [call for call in plugin.calls if call[0] == "container"]
        self.assertEqual([call[1]["operation"] for call in container_calls], ["open", "close"])

    def test_parse_transfer_items_accepts_json_mapping_and_compact(self):
        self.assertEqual(
            skills_mod.parse_transfer_items(
                '[{"item":"minecraft:golden_axe","count":1},{"item":"minecraft:furnace","count":2}]'
            ),
            [{"item": "minecraft:golden_axe", "count": 1}, {"item": "minecraft:furnace", "count": 2}],
        )
        self.assertEqual(
            skills_mod.parse_transfer_items('{"minecraft:coal":64,"minecraft:torch":8}'),
            [{"item": "minecraft:coal", "count": 64}, {"item": "minecraft:torch", "count": 8}],
        )
        self.assertEqual(
            skills_mod.parse_transfer_items("minecraft:golden_axe:1, minecraft:furnace x2"),
            [{"item": "minecraft:golden_axe", "count": 1}, {"item": "minecraft:furnace", "count": 2}],
        )

    async def test_smelt_without_furnace_attempts_crafting(self):
        skills = skills_mod.VanillaSkills(FakePlugin())
        with self.assertRaises(skills_mod.SkillError):
            await skills.smelt("minecraft:iron_ingot", 1)

    async def test_smelt_skips_reachable_stand_pathing(self):
        plugin = FakePlugin()
        plugin.bed_context = {
            "block": "minecraft:furnace",
            "distance": 4.0,
            "stands": [{"x": 0, "y": 64, "z": 1, "distance": 3.0}],
        }
        original_run = plugin._run_operation

        async def run(action, params, seconds=10):
            if action == "smelt":
                plugin.inventory.append({"id": "minecraft:iron_ingot", "count": 1, "slot": 10})
            return await original_run(action, params, seconds)

        plugin._run_operation = run
        result = await skills_mod.VanillaSkills(plugin).smelt_batch(
            {"x": 1, "y": 64, "z": 0}, "minecraft:iron_ingot", 1)
        self.assertEqual(result["skill"], "smelt_batch")
        self.assertEqual(plugin.approaches, [])

    async def test_sleep_without_bed_is_rejected(self):
        skills = skills_mod.VanillaSkills(FakePlugin())
        with self.assertRaisesRegex(skills_mod.SkillError, "床"):
            await skills.sleep()

    async def test_sleep_at_uses_block_context_and_confirms_sleep(self):
        plugin = FakePlugin()
        plugin.bed_context = {
            "bed": True,
            "bed_occupied": False,
            "block": "minecraft:red_bed",
            "stands": [{"x": 6, "y": 64, "z": 0, "feet_y": 64, "distance": 1}],
        }
        result = await skills_mod.VanillaSkills(plugin).sleep_at(5, 64, 0)
        self.assertTrue(result["sleeping"])
        self.assertEqual(plugin.approaches, [(6, 64, 0)])
        self.assertIn(("interact_block", {"x": 5, "y": 64, "z": 0}), plugin.runs)

    async def test_eat_until_full_skips_when_already_full(self):
        result = await skills_mod.VanillaSkills(FakePlugin()).eat_until_full()
        self.assertEqual(result["eaten"], 0)
        self.assertGreaterEqual(result["food"], 20)

    async def test_return_to_verifies_position(self):
        plugin = FakePlugin()
        result = await skills_mod.VanillaSkills(plugin).return_to([5, 64, 0], reach=2)
        self.assertTrue(result["arrived"])
        self.assertEqual(plugin.approaches, [(5, 64, 0)])

    async def test_harvest_block_approaches_stand_not_target_block(self):
        plugin = FakePlugin()
        plugin.bed_context = {"distance": 10, "stands": [{"x": 5, "y": 64, "z": 0, "feet_y": 64, "distance": 1}]}
        await skills_mod.VanillaSkills(plugin).harvest_block([1, 64, 2])
        self.assertEqual(plugin.approaches, [(5, 64, 0)])

    async def test_harvest_block_excavates_when_no_stand(self):
        plugin = FakePlugin()
        plugin.bed_context = {"distance": 10, "stands": []}
        await skills_mod.VanillaSkills(plugin).harvest_block([1, 64, 2])
        self.assertEqual(plugin.approaches, [(1, 64, 2)])

    async def test_gather_scan_fallback_uses_block_context(self):
        plugin = FakePlugin()
        plugin.beds = [{"x": 5, "y": 64, "z": 0, "distance": 1}]
        plugin.bed_context = {"stands": [{"x": 5, "y": 64, "z": 0, "feet_y": 64, "distance": 1}]}
        found = await skills_mod.VanillaSkills(plugin)._gather_scan_fallback("coal", 12)
        self.assertEqual(found[0]["x"], 5)
        self.assertEqual(found[0]["approaches"][0]["feet_y"], 64)

    async def test_return_to_uses_ladder_assisted_goto_first(self):
        class Plugin(FakePlugin):
            def __init__(self):
                super().__init__()
                self.moves = []

            async def _move_and_wait(self, x, y, z, seconds):
                self.moves.append((x, y, z, seconds))
                self.position = {"x": x, "y": y, "z": z}

        plugin = Plugin()
        result = await skills_mod.VanillaSkills(plugin).return_to([5, 64, 0], reach=2)
        self.assertTrue(result["arrived"])
        self.assertEqual(plugin.moves, [(5, 64, 0, 30)])

    async def test_place_line_requires_material_and_places_requested_count(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "minecraft:cobblestone", "count": 4, "slot": 20}]
        result = await skills_mod.VanillaSkills(plugin).place_line([1, 64, 1], "east", 2, "minecraft:cobblestone")
        self.assertEqual(result["placed"], 2)
        self.assertEqual([run[1] for run in plugin.runs], [
            {"x": 1, "y": 64, "z": 1, "item": "minecraft:cobblestone"},
            {"x": 2, "y": 64, "z": 1, "item": "minecraft:cobblestone"},
        ])

    async def test_craft_recipe_does_not_gather_missing_material(self):
        skills = skills_mod.VanillaSkills(FakePlugin())
        skills._recipes["minecraft:stick"] = {
            "output": {"id": "minecraft:stick", "count": 4},
            "ingredients": [{"alternatives": [{"id": "minecraft:oak_planks", "count": 1}]}],
            "width": 2,
            "height": 2,
        }
        with self.assertRaisesRegex(skills_mod.SkillError, "材料不足"):
            await skills.craft_recipe("minecraft:stick", 4)

    async def test_find_and_sleep_remembers_bed_only_after_success(self):
        plugin = FakePlugin()
        plugin.beds = [{"x": 5, "y": 64, "z": 0, "distance": 1, "block": "minecraft:red_bed"}]
        plugin.bed_context = {
            "bed": True,
            "bed_occupied": False,
            "block": "minecraft:red_bed",
            "stands": [{"x": 6, "y": 64, "z": 0, "feet_y": 64, "distance": 1}],
        }
        memory = FakeMemory()
        plugin._poi_memory_for = lambda: memory
        result = await skills_mod.VanillaSkills(plugin).find_and_sleep(remember=True)
        self.assertTrue(result["remembered"])
        self.assertEqual(memory.get("home_bed", "main")["x"], 5)
        self.assertEqual(memory.get("home_bed", "main")["world_key"], "test-world")

    async def test_ensure_table_places_and_opens_owned_table(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "minecraft:crafting_table", "count": 1, "slot": 20}]
        plugin.scan_extra = {"placements": [{"x": 8, "y": 64, "z": 0, "distance": 3}]}
        skills = skills_mod.VanillaSkills(plugin)
        self.assertTrue(await skills._ensure_table())
        self.assertEqual(plugin.runs[0][0], "workbench")
        self.assertTrue(any(call[0] == "container" for call in plugin.calls))

    async def test_ensure_table_falls_back_to_placing_when_existing_table_does_not_open(self):
        plugin = FakePlugin()
        plugin.inventory = [{"id": "minecraft:crafting_table", "count": 1, "slot": 20}]
        plugin.scan_extra = {
            "workbenches": [{"x": 7, "y": 64, "z": 0, "distance": 2,
                             "stands": [{"x": 7, "y": 64, "z": 1, "distance": 1}]}],
            "placements": [{"x": 8, "y": 64, "z": 0, "distance": 3}],
        }
        skills = skills_mod.VanillaSkills(plugin)
        self.assertTrue(await skills._ensure_table())
        self.assertEqual(plugin.runs[0][0], "workbench")

    async def test_place_station_falls_back_to_generic_place_at(self):
        plugin = FakePlugin()
        plugin.scan_extra = {"placements": [{"x": 8, "y": 64, "z": 0, "distance": 3}]}
        state = {"placed": False}
        original_run = plugin._run_operation

        async def run(action, params, seconds=10):
            if action == "station":
                raise RuntimeError("station timed out")
            result = await original_run(action, params, seconds)
            if action == "place_at":
                state["placed"] = True
            return result

        async def call(action, params=None, timeout=10):
            if action == "block_context":
                return {"block": "minecraft:furnace" if state["placed"] else "minecraft:stone"}
            return await FakePlugin._call(plugin, action, params, timeout)

        plugin._run_operation = run
        plugin._call = call
        skills = skills_mod.VanillaSkills(plugin)
        result = await skills._place_station("minecraft:furnace")
        self.assertEqual(result["x"], 8)
        self.assertIn(("place_at", {"x": 8, "y": 64, "z": 0, "item": "minecraft:furnace"}), plugin.runs)

    async def test_place_station_ignores_player_feet_site(self):
        plugin = FakePlugin()
        plugin.scan_extra = {"placements": [{"x": 5, "y": 64, "z": 0, "distance": 0}]}
        with self.assertRaisesRegex(skills_mod.SkillError, "附近没有可放置"):
            await skills_mod.VanillaSkills(plugin)._place_station("minecraft:furnace")

    async def test_combat_without_target_returns_explicitly(self):
        result = await skills_mod.VanillaSkills(FakePlugin()).combat(max_seconds=5)
        self.assertEqual(result["result"], "no_target")

    async def test_combat_holds_until_hard_floor(self):
        plugin = CombatPlugin([{
            "position": {"x": 0, "y": 64, "z": 0},
            "health": 5,
            "hostiles": [{"type": "minecraft:zombie", "name": "Zombie", "health": 18, "distance": 3.0,
                          "position": {"x": 1, "y": 64, "z": 0}}],
        }])
        result = await skills_mod.VanillaSkills(plugin).combat(
            max_seconds=5, retreat_health=4, hold_health=6)
        self.assertEqual(result["result"], "retreated")
        self.assertTrue(plugin.fled)

    async def test_combat_finishes_low_target_at_hold_health(self):
        plugin = CombatPlugin([{
            "position": {"x": 0, "y": 64, "z": 0},
            "health": 5,
            "hostiles": [{"type": "minecraft:zombie", "name": "Zombie", "health": 1, "distance": 3.0,
                          "position": {"x": 1, "y": 64, "z": 0}}],
        }])
        result = await skills_mod.VanillaSkills(plugin).combat(
            max_seconds=5, retreat_health=4, hold_health=6, finish_low_target=True)
        self.assertEqual(result["result"], "killed")
        self.assertTrue(plugin.attacks)

    async def test_combat_flees_multiple_targets(self):
        hostiles = [
            {"type": "minecraft:zombie", "name": "Zombie", "health": 12, "distance": 3.0,
             "position": {"x": 1, "y": 64, "z": 0}},
            {"type": "minecraft:skeleton", "name": "Skeleton", "health": 12, "distance": 4.0,
             "position": {"x": 2, "y": 64, "z": 0}},
            {"type": "minecraft:spider", "name": "Spider", "health": 12, "distance": 5.0,
             "position": {"x": 3, "y": 64, "z": 0}},
        ]
        plugin = CombatPlugin([{"position": {"x": 0, "y": 64, "z": 0}, "health": 16, "hostiles": hostiles}])
        result = await skills_mod.VanillaSkills(plugin).combat(
            max_seconds=5, retreat_health=4, hold_health=6, max_targets=2)
        self.assertEqual(result["result"], "retreated")
        self.assertTrue(plugin.fled)

    async def test_combat_leash_blocks_chasing(self):
        plugin = CombatPlugin([{
            "position": {"x": 0, "y": 64, "z": 0},
            "health": 18,
            "hostiles": [{"type": "minecraft:zombie", "name": "Zombie", "health": 12, "distance": 5.0,
                          "position": {"x": 20, "y": 64, "z": 0}}],
        }])
        result = await skills_mod.VanillaSkills(plugin).combat(
            max_seconds=5, retreat_health=4, hold_health=6, leash_radius=12)
        self.assertEqual(result["result"], "leash_exceeded")
        self.assertFalse(plugin.attacks)

    async def test_combat_blacklist_returns_no_target(self):
        plugin = CombatPlugin([{
            "position": {"x": 0, "y": 64, "z": 0},
            "health": 18,
            "hostiles": [{"type": "minecraft:creeper", "name": "Creeper", "health": 20, "distance": 3.0,
                          "position": {"x": 1, "y": 64, "z": 0}}],
        }])
        result = await skills_mod.VanillaSkills(plugin).combat(
            max_seconds=5, retreat_health=4, hold_health=6, blacklist=("minecraft:creeper",))
        self.assertEqual(result["result"], "no_target")
        self.assertFalse(plugin.attacks)

    async def test_explore_records_resource_and_stops_without_frontier(self):
        result = await skills_mod.VanillaSkills(FakePlugin()).explore("generic", max_seconds=10)
        self.assertEqual(result["stopped"], "no_frontier")
        self.assertTrue(any(row["type"] in {"resource", "logs"} for row in result["found"]))

    async def test_resupply_reports_complete_for_zero_targets(self):
        result = await skills_mod.VanillaSkills(FakePlugin()).resupply(food=0, torches=0, blocks=0, tools=False, max_seconds=30)
        self.assertTrue(result["complete"])
        self.assertEqual(result["shortages"], [])

    def test_tool_rank_requires_iron_or_better(self):
        self.assertGreaterEqual(skills_mod.tool_rank("minecraft:iron_pickaxe"), 3)
        self.assertGreaterEqual(skills_mod.tool_rank("minecraft:diamond_sword"), 3)
        self.assertLess(skills_mod.tool_rank("minecraft:wooden_axe"), 3)
        self.assertLess(skills_mod.tool_rank("minecraft:stone_sword"), 3)


if __name__ == "__main__":
    unittest.main()
