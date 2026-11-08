import unittest
from types import SimpleNamespace

from test_base import m, make_plugin


class ToolProfileTests(unittest.TestCase):
    def test_simple_profile_removes_advanced_tools(self):
        plugin = make_plugin()
        names = ["mc_brief", "mc_task_start", "mc_gather", "mc_protect", "mc_wait", "mc_dig_down", "mc_set_respawn"]
        manager = SimpleNamespace(
            func_list=[SimpleNamespace(name=name) for name in names],
            remove_func=lambda name: manager.func_list.__setitem__(
                slice(None), [tool for tool in manager.func_list if tool.name != name]
            ),
        )
        plugin.context = SimpleNamespace(get_llm_tool_manager=lambda: manager)
        plugin.config["tool_profile"] = "simple"
        plugin._apply_tool_profile()
        remaining = {tool.name for tool in manager.func_list}
        self.assertEqual(remaining, {"mc_brief", "mc_task_start", "mc_protect", "mc_dig_down", "mc_set_respawn"})

    def test_full_profile_keeps_all_tools(self):
        plugin = make_plugin()
        manager = SimpleNamespace(
            func_list=[SimpleNamespace(name=name) for name in ["mc_brief", "mc_gather"]],
            remove_func=lambda name: None,
        )
        plugin.context = SimpleNamespace(get_llm_tool_manager=lambda: manager)
        plugin.config["tool_profile"] = "full"
        plugin._apply_tool_profile()
        self.assertEqual(len(manager.func_list), 2)


if __name__ == "__main__":
    unittest.main()
