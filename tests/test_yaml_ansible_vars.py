"""Regression tests for #285: YAML editor for Ansible default_vars."""
import pytest


def _parse(raw: str) -> dict:
    from app.presentation.routes.admin import _parse_default_vars
    return _parse_default_vars(raw)


def test_empty_string_returns_empty_dict():
    assert _parse("") == {}


def test_whitespace_only_returns_empty_dict():
    assert _parse("   \n  ") == {}


def test_valid_yaml_mapping():
    assert _parse("version: latest\ndebug: false") == {"version": "latest", "debug": False}


def test_valid_yaml_nested():
    result = _parse("packages:\n  - git\n  - curl\n")
    assert result == {"packages": ["git", "curl"]}


def test_explicit_null_yaml_returns_empty_dict():
    assert _parse("~") == {}


def test_yaml_list_raises():
    with pytest.raises(ValueError, match="mapping"):
        _parse("- item1\n- item2")


def test_yaml_scalar_raises():
    with pytest.raises(ValueError, match="mapping"):
        _parse("just a string")


def test_invalid_yaml_raises():
    with pytest.raises(ValueError, match="valid YAML"):
        _parse("key: [unclosed")


def test_json_is_valid_yaml():
    """JSON is a subset of YAML — old JSON payloads still parse correctly."""
    assert _parse('{"version": "1.0", "debug": false}') == {"version": "1.0", "debug": False}
