"""Feature flags (#135): off unless the environment says on, one accessor."""

import pytest

from collab_hub_api.config import Config, FeaturesConfig


def test_unset_flag_is_off():
    features = FeaturesConfig()
    assert features.enabled("anything") is False


@pytest.mark.parametrize("value", ["1", "true", "True", "YES", "on", " On "])
def test_truthy_values_turn_a_flag_on(value):
    features = FeaturesConfig.model_validate({"cogs_ui": value})
    assert features.enabled("cogs_ui") is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "enabled", "2"])
def test_everything_else_stays_off(value):
    features = FeaturesConfig.model_validate({"cogs_ui": value})
    assert features.enabled("cogs_ui") is False


def test_name_lookup_normalizes_case_and_dashes():
    features = FeaturesConfig.model_validate({"cogs_ui": "1"})
    assert features.enabled("COGS-UI") is True
    assert features.enabled(" cogs_ui ") is True


def test_flag_arrives_through_the_environment(monkeypatch):
    monkeypatch.setenv("COLLAB_HUB_API__FEATURES__COGS_UI", "true")
    config = Config()
    assert config.features.enabled("cogs_ui") is True
    assert config.features.enabled("other_flag") is False


def test_default_config_has_no_flags_on():
    config = Config.parse()
    assert config.features.enabled("cogs_ui") is False
