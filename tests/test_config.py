"""Config parsing (D9): zero-config works, overrides honored, malformed never crashes."""

from pr_sentinel.config import load_config
from pr_sentinel.models import AgentName, Severity


def test_empty_config_gives_defaults():
    config = load_config(None)
    assert config.provider.model == "gpt-5-mini"
    assert config.min_severity == Severity.MEDIUM
    assert config.limits.max_files == 35
    assert len(config.agents.enabled) == 4
    assert not config.dry_run
    assert config.warnings == []


def test_overrides_honored():
    config = load_config(
        """
provider:
  base_url: https://api.deepseek.com/v1
  model: deepseek-v4-flash
agents:
  enabled: [security, performance]
min_severity: low
ignore:
  - "migrations/**"
limits:
  max_files: 10
dry_run: true
"""
    )
    assert config.provider.base_url == "https://api.deepseek.com/v1"
    assert config.agents.enabled == [AgentName.SECURITY, AgentName.PERFORMANCE]
    assert config.min_severity == Severity.LOW
    assert config.ignore == ["migrations/**"]
    assert config.limits.max_files == 10
    # Unset sections keep their defaults.
    assert config.limits.max_input_tokens == 120_000
    assert config.dry_run is True


def test_malformed_yaml_falls_back_to_defaults_with_warning():
    config = load_config("provider: [unclosed")
    assert config.provider.model == "gpt-5-mini"
    assert any("could not be parsed" in w for w in config.warnings)


def test_invalid_values_fall_back_to_defaults_with_warning():
    config = load_config("min_severity: catastrophic")
    assert config.min_severity == Severity.MEDIUM
    assert any("failed validation" in w for w in config.warnings)


def test_unknown_keys_warn_but_do_not_fail():
    config = load_config("future_feature: true\nmin_severity: high")
    assert config.min_severity == Severity.HIGH
    assert any("future_feature" in w for w in config.warnings)


def test_non_mapping_yaml_falls_back():
    config = load_config("- just\n- a\n- list")
    assert config.provider.model == "gpt-5-mini"
    assert config.warnings
