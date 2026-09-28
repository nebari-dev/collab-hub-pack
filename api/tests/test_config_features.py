"""Feature flags: registered once, off until set to true, read through one accessor."""

import os

import pytest
from pydantic import ValidationError

from collab_hub_api import config as config_module
from collab_hub_api.config import Config, FeaturesConfig

FLAG_ENV_PREFIX = "COLLAB_HUB_API__FEATURES__"


@pytest.fixture(autouse=True)
def registered_flag(monkeypatch):
    """Register one flag for these tests, and hide any flag set in the developer's environment."""
    for name in [name for name in os.environ if name.startswith(FLAG_ENV_PREFIX)]:
        monkeypatch.delenv(name)
    monkeypatch.setitem(config_module.FEATURE_FLAGS, "cogs_ui", "The Cogs screens (test flag).")


def test_unset_flag_is_off():
    assert FeaturesConfig().enabled("cogs_ui") is False


@pytest.mark.parametrize("value", [True, 1, "1", "true", "True", "YES", "on", "t", "y"])
def test_true_values_turn_a_flag_on(value):
    features = FeaturesConfig.model_validate({"cogs_ui": value})
    assert features.enabled("cogs_ui") is True
    assert features.enabled_names() == ["cogs_ui"]


@pytest.mark.parametrize("value", [False, 0, "0", "false", "off", "no", "f", "n"])
def test_false_values_keep_a_flag_off(value):
    features = FeaturesConfig.model_validate({"cogs_ui": value})
    assert features.enabled("cogs_ui") is False
    assert features.enabled_names() == []


@pytest.mark.parametrize("value", ["", "enabled", "2", "ture", " on "])
def test_an_unparseable_value_is_refused(value):
    with pytest.raises(ValidationError, match="feature flag 'cogs_ui' must be a boolean"):
        FeaturesConfig.model_validate({"cogs_ui": value})


def test_an_unregistered_name_in_configuration_is_refused():
    with pytest.raises(ValidationError, match="unknown feature flag 'cogs_iu'; registered flags: cogs_ui"):
        FeaturesConfig.model_validate({"cogs_iu": "true"})


def test_reading_an_unregistered_name_raises():
    with pytest.raises(KeyError, match="'cogs_iu' is not registered"):
        FeaturesConfig().enabled("cogs_iu")


def test_name_lookup_normalizes_case_and_dashes():
    features = FeaturesConfig.model_validate({"COGS-UI": "1"})
    assert features.enabled("cogs_ui") is True
    assert features.enabled("COGS-UI") is True
    assert features.enabled(" cogs_ui ") is True


def test_flag_arrives_through_the_environment(monkeypatch):
    monkeypatch.setenv(f"{FLAG_ENV_PREFIX}COGS_UI", "true")
    assert Config().features.enabled("cogs_ui") is True


def test_an_unknown_environment_flag_stops_startup(monkeypatch):
    monkeypatch.setenv(f"{FLAG_ENV_PREFIX}COGS_IU", "true")
    with pytest.raises(ValidationError, match="unknown feature flag"):
        Config()


def test_an_unparseable_environment_value_stops_startup(monkeypatch):
    monkeypatch.setenv(f"{FLAG_ENV_PREFIX}COGS_UI", "ture")
    with pytest.raises(ValidationError, match="must be a boolean"):
        Config()


def test_default_config_has_no_flags_on():
    features = Config.parse().features
    assert features.enabled("cogs_ui") is False
    assert features.enabled_names() == []
