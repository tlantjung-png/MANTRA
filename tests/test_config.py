"""Config loader: defaults, deep merge, unknown-key and type validation."""

from __future__ import annotations

import json

import pytest

from core.config import DEFAULTS, load_config, merge_defaults
from core.agent.exceptions import ConfigError


def _write(tmp_path, data) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_empty_file_yields_defaults(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("", encoding="utf-8")
    config = load_config(str(path))
    assert config["max_steps"] == DEFAULTS["max_steps"]
    assert config["llm"]["provider"] == "openai"


def test_unknown_top_level_key_rejected(tmp_path) -> None:
    path = _write(tmp_path, {"bogus": 1})
    with pytest.raises(ConfigError, match="unknown config keys"):
        load_config(path)


def test_unknown_section_key_rejected(tmp_path) -> None:
    path = _write(tmp_path, {"context": {"max_messages": 10, "nope": 1}})
    with pytest.raises(ConfigError, match="unknown config keys in 'context'"):
        load_config(path)


def test_deep_merge_keeps_untouched_defaults(tmp_path) -> None:
    path = _write(tmp_path, {"llm": {"model": "m2"}, "max_steps": 5})
    config = load_config(path)
    assert config["llm"]["model"] == "m2"
    # Untouched default survives the merge.
    assert config["llm"]["api_key_env"] == "OPENAI_API_KEY"
    assert config["max_steps"] == 5


def test_discriminator_switch_drops_previous_type_keys(tmp_path) -> None:
    # Switching evaluator type must not carry over "test_cmd".
    path = _write(tmp_path, {"evaluator": {"type": "none"}})
    config = load_config(path)
    assert "test_cmd" not in config["evaluator"]


def test_non_object_config_rejected(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ConfigError, match="must be an object"):
        load_config(path)


def test_invalid_json_rejected(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        load_config(path)


def test_missing_file_rejected(tmp_path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "absent.json"))


def test_oversized_config_rejected(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("x" * 1_100_000, encoding="utf-8")
    with pytest.raises(ConfigError, match="too large"):
        load_config(str(path))


def test_max_steps_validation(tmp_path) -> None:
    for bad in (0, -1, "ten", True, 2.5):
        path = _write(tmp_path, {"max_steps": bad})
        with pytest.raises(ConfigError, match="max_steps"):
            load_config(path)


def test_tools_validation(tmp_path) -> None:
    path = _write(tmp_path, {"tools": []})
    with pytest.raises(ConfigError, match="at least one tool"):
        load_config(path)
    path = _write(tmp_path, {"tools": ["read_file", "  "]})
    with pytest.raises(ConfigError, match="non-empty names"):
        load_config(path)


def test_approvals_enum_validation(tmp_path) -> None:
    path = _write(tmp_path, {"approvals": "bogus"})
    with pytest.raises(ConfigError, match="approvals"):
        load_config(path)


def test_context_minima_enforced(tmp_path) -> None:
    path = _write(tmp_path, {"context": {"max_messages": 1}})
    with pytest.raises(ConfigError, match="max_messages"):
        load_config(path)
    path = _write(tmp_path, {"context": {"max_chars": 10}})
    with pytest.raises(ConfigError, match="max_chars"):
        load_config(path)


def test_component_value_types_enforced(tmp_path) -> None:
    path = _write(tmp_path, {"llm": {"temperature": "hot"}})
    with pytest.raises(ConfigError, match="temperature"):
        load_config(path)


def test_reasoning_effort_enum(tmp_path) -> None:
    path = _write(tmp_path, {"llm": {"reasoning_effort": "ultra"}})
    with pytest.raises(ConfigError, match="reasoning_effort"):
        load_config(path)


def test_merge_defaults_rejects_non_dict() -> None:
    with pytest.raises(ConfigError):
        merge_defaults(["nope"])  # type: ignore[arg-type]


def test_yaml_requires_optional_dependency(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("max_steps: 7\n", encoding="utf-8")
    try:
        config = load_config(str(path))
        assert config["max_steps"] == 7  # PyYAML present: must parse
    except ConfigError as exc:
        # Absent PyYAML is a clean, explained error, never a crash.
        assert "PyYAML" in str(exc) or "JSON config" in str(exc)


def test_defaults_are_not_mutated_by_merge() -> None:
    merge_defaults({"llm": {"model": "changed"}})
    assert DEFAULTS["llm"]["model"] == "gpt-4o"
