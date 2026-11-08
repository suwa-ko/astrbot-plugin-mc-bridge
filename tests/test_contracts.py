import importlib
import unittest

from test_base import m

contracts = importlib.import_module(m.__name__ + ".contracts")


class L0ContractTests(unittest.TestCase):
    def test_every_opcode_has_a_spec(self):
        names = {opcode.value for opcode in contracts.Opcode}
        self.assertEqual(names, contracts.L0_OPCODE_NAMES)
        self.assertEqual(len(contracts.L0_SPECS), len(names))

    def test_l1_only_ops_are_not_l0(self):
        for name in ("dig_down", "sleep_at", "craft_recipe", "attack_target"):
            self.assertIn(name, contracts.L1_ONLY)
            self.assertNotIn(name, contracts.L0_OPCODE_NAMES)

    def test_mutating_action_requires_actor(self):
        request = contracts.L0Request(
            contracts.Opcode.MINE_BLOCK,
            {"x": 1, "y": 64, "z": 2},
            resources=frozenset({contracts.Resource.ACTOR}),
        )
        self.assertEqual(request.validation_errors(), [])

        missing = contracts.L0Request(
            contracts.Opcode.MINE_BLOCK,
            {"x": 1, "y": 64, "z": 2},
        )
        self.assertIn("actor resource is required", missing.validation_errors())

    def test_readonly_query_rejects_actor(self):
        request = contracts.L0Request(
            contracts.Opcode.GET_STATUS,
            resources=frozenset({contracts.Resource.ACTOR}),
        )
        self.assertIn("actor resource must not be declared", " ".join(request.validation_errors()))

    def test_camera_action_does_not_need_actor(self):
        request = contracts.L0Request(
            contracts.Opcode.LOOK_AT,
            {"x": 1, "y": 64, "z": 2},
            resources=frozenset({contracts.Resource.CAMERA}),
        )
        self.assertEqual(request.validation_errors(), [])

    def test_unknown_opcode_and_timeout_are_rejected(self):
        unknown = contracts.L0Request("not_an_opcode")
        self.assertTrue(any("unknown opcode" in item for item in unknown.validation_errors()))

        bad_timeout = contracts.L0Request(
            contracts.Opcode.GET_STATUS,
            timeout_ms=0,
        )
        self.assertIn("timeout_ms must be 1..300000", bad_timeout.validation_errors())

    def test_result_normalizes_error_code(self):
        result = contracts.L0Result(False, "PROTECTED", error="protected area")
        self.assertIs(result.code, contracts.ErrorCode.PROTECTED)
        self.assertFalse(result.ok)

    def test_l1_specs_cover_names_and_require_actor(self):
        self.assertEqual(contracts.L1_OPCODE_NAMES, {op.value for op in contracts.L1Opcode})
        self.assertEqual(contracts.L1_OPCODE_NAMES, {spec.opcode.value for spec in contracts.L1_SPECS.values()})
        for spec in contracts.L1_SPECS.values():
            self.assertIn(contracts.Resource.ACTOR, spec.resources)
            self.assertGreater(spec.max_seconds, 0)
            self.assertIn(spec.opcode.value, contracts.L1_ONLY)

    def test_l2_specs_cover_names_and_require_actor(self):
        self.assertEqual(contracts.L2_OPCODE_NAMES, {op.value for op in contracts.L2Opcode})
        self.assertEqual(contracts.L2_OPCODE_NAMES, {spec.opcode.value for spec in contracts.L2_SPECS.values()})
        for spec in contracts.L2_SPECS.values():
            self.assertIn(contracts.Resource.ACTOR, spec.resources)
            self.assertGreater(spec.max_seconds, 0)
            self.assertIn(spec.opcode.value, contracts.L2_ONLY)

    def test_layer_names_do_not_overlap(self):
        self.assertFalse(contracts.L0_OPCODE_NAMES & contracts.L1_OPCODE_NAMES)
        self.assertFalse(contracts.L0_OPCODE_NAMES & contracts.L2_OPCODE_NAMES)
        self.assertFalse(contracts.L1_OPCODE_NAMES & contracts.L2_OPCODE_NAMES)


if __name__ == "__main__":
    unittest.main()
