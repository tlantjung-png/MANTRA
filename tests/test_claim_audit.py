"""Claim audit: deterministic unsupported-claim detection with bounded output."""

from __future__ import annotations

import os

import pytest

from core.registry import TOOL_REGISTRY
from core.sandbox import LocalSandbox
from core.tools.claim_audit import ClaimAuditTool
from core.agent import skills


def _report(text: str, **kwargs) -> str:
    return ClaimAuditTool().execute(None, text=text, **kwargs)  # type: ignore[arg-type]


# --- rule detection -------------------------------------------------------


def test_plain_factual_prose_has_no_findings() -> None:
    out = _report("The build ran. The tests passed. The team shipped the change.")
    assert "no findings" in out


def test_unsourced_superlative() -> None:
    out = _report("We are the best team in the project.")
    assert "[C01 unsourced superlative]" in out


def test_unverifiable_absolute() -> None:
    out = _report("This approach delivers guaranteed results.")
    assert "[C02 unverifiable absolute]" in out


def test_statistic_without_citation_is_flagged() -> None:
    out = _report("Performance improved 30% after the change.")
    assert "[C03 unsourced statistic]" in out


def test_statistic_with_citation_is_not_flagged() -> None:
    out = _report("Performance improved 30% according to the run report.")
    assert "no findings" in out


def test_multiplier_without_citation_is_flagged() -> None:
    out = _report("The new process is 3x faster.")
    assert "[C03 unsourced statistic]" in out


def test_vanity_claim() -> None:
    out = _report("This is a revolutionary way to work.")
    assert "[C04 vanity claim]" in out


def test_proxy_authority() -> None:
    out = _report("Studies show that breaks help focus.")
    assert "[C05 proxy authority]" in out


def test_placeholder_content() -> None:
    out = _report("The section is TODO and must be written.")
    assert "[C06 placeholder content]" in out


def test_lowercase_todo_as_noun_is_not_flagged() -> None:
    out = _report("The checklist keeps the todo items.")
    assert "no findings" in out


def test_protocol_todo_add_done_is_not_flagged() -> None:
    out = _report("The agent reports TODO ADD lines and TODO DONE lines.")
    assert "no findings" in out


# --- weak-rule gating -----------------------------------------------------


def test_single_weak_tell_is_not_reported() -> None:
    out = _report("The result is significantly better now.")
    assert "no findings" in out


def test_three_weak_tells_in_one_paragraph_are_reported() -> None:
    out = _report("It is significantly better, vastly superior, clearly improved.")
    assert "[W01 vague magnitude]" in out
    assert "[W03 appeal to common knowledge]" in out


def test_weak_tells_in_separate_paragraphs_stay_quiet() -> None:
    out = _report("Significantly better here.\n\nVastly superior there.")
    assert "no findings" in out


# --- masking and suppression ---------------------------------------------


def test_frontmatter_and_fenced_code_are_skipped() -> None:
    text = (
        "---\n"
        "title: the best\n"
        "---\n\n"
        "```text\n"
        "We are the best team.\n"
        "```\n\n"
        "Real prose sits here.\n"
    )
    out = _report(text)
    assert "no findings" in out


def test_inline_code_is_skipped() -> None:
    out = _report("Use the tag `the best` when tagging.")
    assert "no findings" in out


def test_rule_can_be_suppressed_on_its_line() -> None:
    text = "We are the best team. <!-- claim-audit-disable C01 -->"
    assert "no findings" in _report(text)


def test_all_rules_can_be_suppressed_on_a_line() -> None:
    text = "We are the best team. Performance improved 30%. <!-- claim-audit-disable-all -->"
    assert "no findings" in _report(text)


def test_citation_word_alone_does_not_flag_statistic() -> None:
    assert "no findings" in _report("The report is attached.")


def test_report_counts_all_strong_rules() -> None:
    out = _report("plain prose")
    assert "7 strong / 3 weak rules scanned" in out


# --- safety, bounds, determinism -----------------------------------------


def test_non_string_arguments_do_not_raise() -> None:
    tool = ClaimAuditTool()
    assert tool.execute(None, text=123).startswith("claim_audit:")  # type: ignore[arg-type]
    assert tool.execute(None, text=["a"]).startswith("claim_audit:")  # type: ignore[arg-type]
    assert tool.execute(None, path=123).startswith("ERROR:")  # type: ignore[arg-type]


def test_inline_text_is_capped() -> None:
    text = "The build ran. " * 2_000 + "We are the best team."
    out = _report(text)
    assert "[C01" not in out
    assert "claim_audit:" in out


def test_max_findings_is_honoured() -> None:
    text = "\n\n".join(f"We are the best team {i}." for i in range(12))
    out = _report(text, max_findings=3)
    assert out.count("[C01") == 3


def test_output_is_bounded_with_many_findings() -> None:
    text = "\n\n".join(f"We are the best team {i}." for i in range(50))
    out = _report(text, max_findings=50)
    assert len(out) < 8_200
    assert "[C01" in out


def test_report_is_deterministic() -> None:
    text = "We are the best team. Performance improved 30%."
    assert _report(text) == _report(text)


# --- workspace path mode --------------------------------------------------


def test_path_mode_reads_workspace_file(sandbox: LocalSandbox) -> None:
    with open(os.path.join(sandbox.root, "sample.md"), "w", encoding="utf-8") as handle:
        handle.write("We are the best team.\n")
    out = ClaimAuditTool().execute(sandbox, path="sample.md")
    assert "[C01 unsourced superlative]" in out


def test_broken_local_reference_is_flagged(sandbox: LocalSandbox) -> None:
    with open(os.path.join(sandbox.root, "sample.md"), "w", encoding="utf-8") as handle:
        handle.write("See [details](missing.md).\n")
    out = ClaimAuditTool().execute(sandbox, path="sample.md")
    assert "[C07 broken local reference]" in out


def test_existing_local_reference_is_not_flagged(sandbox: LocalSandbox) -> None:
    with open(os.path.join(sandbox.root, "sample.md"), "w", encoding="utf-8") as handle:
        handle.write("See [details](sample.md).\n")
    out = ClaimAuditTool().execute(sandbox, path="sample.md")
    assert "no findings" in out


def test_backtick_token_is_not_a_reference(sandbox: LocalSandbox) -> None:
    with open(os.path.join(sandbox.root, "sample.md"), "w", encoding="utf-8") as handle:
        handle.write("Run `python -m core.tui.harness --out DIR`.\n")
    out = ClaimAuditTool().execute(sandbox, path="sample.md")
    assert "no findings" in out


def test_text_mode_skips_broken_reference_rule() -> None:
    out = _report("See [details](missing.md).")
    assert "(no workspace root: broken-reference rule skipped)" in out
    assert "[C07" not in out


def test_path_cannot_escape_workspace(sandbox: LocalSandbox) -> None:
    out = ClaimAuditTool().execute(sandbox, path="../outside.txt")
    assert out.startswith("ERROR: path escapes workspace")


@pytest.mark.parametrize(
    "path",
    ["bad\x00path", "bad\npath", "x" * 1001, "missing.md"],
)
def test_invalid_paths_return_errors(sandbox: LocalSandbox, path: str) -> None:
    out = ClaimAuditTool().execute(sandbox, path=path)
    assert out.startswith("ERROR:")


def test_exactly_one_source_is_required() -> None:
    tool = ClaimAuditTool()
    assert tool.execute(None).startswith("ERROR:")  # type: ignore[arg-type]
    assert tool.execute(None, path="a.md", text="hello").startswith("ERROR:")  # type: ignore[arg-type]


# --- wiring and skill -----------------------------------------------------


def test_registered_with_expected_schema() -> None:
    assert TOOL_REGISTRY["claim_audit"] is ClaimAuditTool
    schema = ClaimAuditTool().schema()
    assert schema["function"]["name"] == "claim_audit"
    assert "path" in schema["function"]["parameters"]["properties"]


@pytest.mark.parametrize("filename", ["config.json", "config.meta.json"])
def test_shipped_configs_enable_the_tool(filename: str) -> None:
    """The source-hygiene skill must work with a default config."""
    import json

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "examples", filename), "r", encoding="utf-8") as handle:
        config = json.load(handle)
    assert "claim_audit" in config["tools"]


def test_skill_is_refreshed_without_engine_leftovers() -> None:
    skill = skills.get("source-hygiene")
    assert skill is not None
    assert skill.version == "1.0.0"
    for leftover in ("/goal", "/todo", "/tools", "/workflow"):
        assert leftover not in skill.body
