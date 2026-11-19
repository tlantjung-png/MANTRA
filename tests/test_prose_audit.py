"""Prose audit: deterministic writing-tell detection with bounded output."""

from __future__ import annotations

import os

import pytest

from core.registry import TOOL_REGISTRY
from core.sandbox import LocalSandbox
from core.tools.prose_audit import ProseAuditTool
from core.agent import skills


def _report(text: str, **kwargs) -> str:
    return ProseAuditTool().execute(None, text=text, **kwargs)  # type: ignore[arg-type]


# --- rule detection -------------------------------------------------------


def test_plain_prose_has_no_findings() -> None:
    out = _report("The build ran. The tests passed. The team shipped the change.")
    assert "no findings" in out


def test_not_x_but_y() -> None:
    out = _report("It's not just a feature, it's a shift in how we work.")
    assert "[P01 not-X-but-Y]" in out


def test_one_line_closer() -> None:
    out = _report("Here is the result.\n\nLet that sink in.\n")
    assert "[P02 one-line closer]" in out


def test_deep_saying_and_overused_word() -> None:
    out = _report("At its core, we must delve into this problem.")
    assert "[P03 saying that sounds deep]" in out
    assert "[P12 overused AI word]" in out


def test_staged_runup_and_arguing_with_no_one() -> None:
    out = _report("Here's the thing. I'm not saying it is broken, but it feels off.")
    assert "[P04 staged run-up]" in out
    assert "[P05 arguing with no one]" in out


def test_chatbot_residue() -> None:
    out = _report("Great question! The answer is in the docs.")
    assert "[P22 chatbot residue]" in out


def test_repeated_sentence_openings() -> None:
    out = _report("She noted the delay. She noted the cause. She filed the report.")
    assert "[P07 repeated sentence openings]" in out


def test_bold_as_decoration() -> None:
    out = _report("**OKRs** and **KPIs** and **SLAs** are tracked here.")
    assert "[P19 bold as decoration]" in out


def test_heading_restated_in_first_sentence() -> None:
    out = _report("## Performance\n\nPerformance improved after the change.")
    assert "[P24 heading restated in first sentence]" in out


# --- weak-rule gating -----------------------------------------------------


def test_single_weak_tell_is_not_reported() -> None:
    out = _report("One thing — just one thing matters here.")
    assert "no findings" in out


def test_three_weak_tells_in_one_paragraph_are_reported() -> None:
    out = _report("One — two — three — done.")
    assert "[P08 dashes as universal connector]" in out


def test_weak_tells_in_separate_paragraphs_stay_quiet() -> None:
    out = _report("One — two here.\n\nThree — four there.")
    assert "no findings" in out


# --- masking and suppression ---------------------------------------------


def test_frontmatter_and_fenced_code_are_skipped() -> None:
    text = (
        "---\n"
        "title: not just a test\n"
        "---\n\n"
        "```text\n"
        "It's not just a feature, it's a shift.\n"
        "```\n\n"
        "Real prose sits here.\n"
    )
    out = _report(text)
    assert "no findings" in out


def test_inline_code_is_skipped() -> None:
    out = _report("Use the phrase `not just a feature` when editing.")
    assert "no findings" in out


def test_rule_can_be_suppressed_on_its_line() -> None:
    text = "It's not just a feature, it's a shift. <!-- prose-audit-disable P01 -->"
    assert "no findings" in _report(text)


def test_all_rules_can_be_suppressed_on_a_line() -> None:
    text = "At its core, we must delve. <!-- prose-audit-disable-all -->"
    assert "no findings" in _report(text)


def test_possessive_its_is_not_a_not_x_but_y_tell() -> None:
    assert "no findings" in _report("It is not just one of its many features.")


def test_ordinary_this_is_sentence_is_not_a_closer() -> None:
    assert "no findings" in _report("This is a test.")


def test_arrow_in_heading_is_detected() -> None:
    assert "[P20 decorative heading]" in _report("## Next \u2192 done")


def test_report_counts_all_strong_rules() -> None:
    out = _report("plain prose")
    assert "13 strong / 5 weak rules scanned" in out


def test_weak_rule_disable_honoured_above_threshold() -> None:
    text = (
        "One \u2014 two \u2014 three \u2014 done. "
        "alpha-beta gamma-delta epsilon-zeta. "
        "<!-- prose-audit-disable P10 -->"
    )
    out = _report(text)
    assert "[P10" not in out


def test_non_string_arguments_do_not_raise() -> None:
    tool = ProseAuditTool()
    assert tool.execute(None, text=123).startswith("prose_audit:")  # type: ignore[arg-type]
    assert tool.execute(None, text=["a"]).startswith("prose_audit:")  # type: ignore[arg-type]
    assert tool.execute(None, path=123).startswith("ERROR:")  # type: ignore[arg-type]


# --- safety, bounds, determinism -----------------------------------------


def test_inline_text_is_capped() -> None:
    text = "The build ran. " * 2_000 + "It's not just a feature, it's a shift."
    out = _report(text)
    assert "[P01" not in out
    assert "prose_audit:" in out


def test_max_findings_is_honoured() -> None:
    text = "\n\n".join(f"not just item {i}, it's a shift" for i in range(12))
    out = _report(text, max_findings=3)
    assert out.count("[P01") == 3


def test_output_is_bounded_with_many_findings() -> None:
    text = "\n\n".join(f"not just item {i}, it's a shift" for i in range(50))
    out = _report(text, max_findings=50)
    assert len(out) < 8_200
    assert "[P01" in out


def test_report_is_deterministic() -> None:
    text = "At its core, we must delve. Let that sink in."
    assert _report(text) == _report(text)


# --- workspace path mode --------------------------------------------------


def test_path_mode_reads_workspace_file(sandbox: LocalSandbox) -> None:
    with open(os.path.join(sandbox.root, "sample.md"), "w", encoding="utf-8") as handle:
        handle.write("It's not just a file, it's a record.\n")
    out = ProseAuditTool().execute(sandbox, path="sample.md")
    assert "[P01 not-X-but-Y]" in out


def test_path_cannot_escape_workspace(sandbox: LocalSandbox) -> None:
    out = ProseAuditTool().execute(sandbox, path="../outside.txt")
    assert out.startswith("ERROR: path escapes workspace")


@pytest.mark.parametrize(
    "path",
    ["bad\x00path", "bad\npath", "x" * 1001, "missing.md"],
)
def test_invalid_paths_return_errors(sandbox: LocalSandbox, path: str) -> None:
    out = ProseAuditTool().execute(sandbox, path=path)
    assert out.startswith("ERROR:")


def test_exactly_one_source_is_required() -> None:
    tool = ProseAuditTool()
    assert tool.execute(None).startswith("ERROR:")  # type: ignore[arg-type]
    assert tool.execute(None, path="a.md", text="hello").startswith("ERROR:")  # type: ignore[arg-type]


# --- wiring and skill -----------------------------------------------------


def test_registered_with_expected_schema() -> None:
    assert TOOL_REGISTRY["prose_audit"] is ProseAuditTool
    schema = ProseAuditTool().schema()
    assert schema["function"]["name"] == "prose_audit"
    assert "path" in schema["function"]["parameters"]["properties"]


@pytest.mark.parametrize("filename", ["config.json", "config.meta.json"])
def test_shipped_configs_enable_the_tool(filename: str) -> None:
    """The humanize skill's first step must work with a default config."""
    import json

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "examples", filename), "r", encoding="utf-8") as handle:
        config = json.load(handle)
    assert "prose_audit" in config["tools"]


def test_skill_is_refreshed_without_engine_leftovers() -> None:
    skill = skills.get("humanize")
    assert skill is not None
    assert skill.version == "1.1.0"
    for leftover in ("ai-constants.ts", "generateFixPrompt", "BURSTINESS", "Levenshtein"):
        assert leftover not in skill.body
