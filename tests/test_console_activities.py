"""Themed chip-word mapping: one word per activity, on the MANTRA theme."""

from __future__ import annotations

import unittest

from core.console import _activity_for_tool, _TOOL_ACTIVITY


class ActivityMappingTest(unittest.TestCase):
    def test_default_tools_have_themed_words(self):
        expected = {
            "read_file": "Scrying",
            "list_dir": "Scrying",
            "find_file": "Divining",
            "search_code": "Divining",
            "write_file": "Inscribing",
            "edit_file": "Refining",
            "run_command": "Casting",
            "shell_output": "Casting",
            "kill_shell": "Casting",
            "git_diff": "Chronicling",
            "git_reset": "Chronicling",
            "extract_document": "Unveiling",
            "web_fetch": "Summoning",
        }
        for tool, word in expected.items():
            self.assertEqual(_activity_for_tool(tool), word, tool)
            self.assertIn(tool, _TOOL_ACTIVITY)

    def test_unknown_tool_falls_back_to_enchanting(self):
        self.assertEqual(_activity_for_tool("mcp_custom_tool"), "Enchanting")
        self.assertEqual(_activity_for_tool(""), "Enchanting")

    def test_closely_related_tools_share_a_word(self):
        self.assertEqual(_activity_for_tool("read_file"), _activity_for_tool("list_dir"))
        self.assertEqual(_activity_for_tool("find_file"), _activity_for_tool("search_code"))
        self.assertEqual(_activity_for_tool("run_command"), _activity_for_tool("shell_output"))
        self.assertEqual(_activity_for_tool("git_diff"), _activity_for_tool("git_reset"))


if __name__ == "__main__":
    unittest.main()
