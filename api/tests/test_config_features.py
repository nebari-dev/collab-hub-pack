"""Feature flags: every flag is off until set to true, read through one accessor."""

import pytest
from pydantic import ValidationError

from collab_hub_api.config import Config, FeaturesConfig


def test_unset_flag_is_off():
    features = FeaturesConfig()
    assert features.enabled("anything") is False


@pytest.mark.parametrize("value", [True, 1, "1", "true", "True", "YES", "on", "t", "y"])
def test_true_values_turn_a_flag_on(value):
    features = FeaturesConfig.model_validate({"cogs_ui": value})
    assert features.enabled("cogs_ui") is True


@pytest.mark.parametrize("value", [False, 0, "0", "false", "off", "no", "f", "n"])
def test_false_values_keep_a_flag_off(value):
    features = FeaturesConfig.model_validate({"cogs_ui": value})
    assert features.enabled("cogs_ui") is False


@pytest.mark.parametrize("value", ["", "enabled", "2", "ture", " on "])
def test_an_unparseable_value_is_refused(value):
    with pytest.raises(ValidationError, match="feature flag 'cogs_ui' must be a boolean"):
        FeaturesConfig.model_validate({"cogs_ui": value})


def test_name_lookup_normalizes_case_and_dashes():
    features = FeaturesConfig.model_validate({"COGS-UI": "1"})
    assert features.enabled("cogs_ui") is True
    assert features.enabled("COGS-UI") is True
    assert features.enabled(" cogs_ui ") is True


def test_flag_arrives_through_the_environment(monkeypatch):
    monkeypatch.setenv("COLLAB_HUB_API__FEATURES__COGS_UI", "true")
    config = Config()
    assert config.features.enabled("cogs_ui") is True
    assert config.features.enabled("other_flag") is False


def test_an_unparseable_environment_value_stops_startup(monkeypatch):
    monkeypatch.setenv("COLLAB_HUB_API__FEATURES__COGS_UI", "ture")
    with pytest.raises(ValidationError, match="feature flag"):
        Config()


def test_default_config_has_no_flags_on():
    config = Config.parse()
    assert config.features.enabled("cogs_ui") is False
