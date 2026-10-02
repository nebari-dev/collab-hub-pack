"""Feature flags: registered once, off until set to true, read through one accessor."""

import logging
import os

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from collab_hub_api import config as config_module
from collab_hub_api.config import Config, FeaturesConfig
from collab_hub_api.core import make_app
from collab_hub_api.dependencies import get_features

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
    assert features.enabled_names == ["cogs_ui"]


@pytest.mark.parametrize("value", [False, 0, "0", "false", "off", "no", "f", "n"])
def test_false_values_keep_a_flag_off(value):
    features = FeaturesConfig.model_validate({"cogs_ui": value})
    assert features.enabled("cogs_ui") is False
    assert features.enabled_names == []


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


@pytest.mark.parametrize("name", ["COGS_UI", "cogs-ui", " cogs_ui "])
def test_only_the_registered_spelling_is_accepted(name):
    with pytest.raises(ValidationError, match="unknown feature flag"):
        FeaturesConfig.model_validate({name: "1"})
    with pytest.raises(KeyError):
        FeaturesConfig().enabled(name)


def test_a_retired_name_is_ignored_and_recorded(monkeypatch):
    monkeypatch.setattr(config_module, "RETIRED_FEATURE_FLAGS", frozenset({"old_ui"}))
    features = FeaturesConfig.model_validate({"old_ui": "true", "cogs_ui": "true"})
    assert features.enabled_names == ["cogs_ui"]
    assert features.retired_names == ["old_ui"]
    with pytest.raises(KeyError, match="'old_ui' is not registered"):
        features.enabled("old_ui")


def test_an_unparseable_value_is_not_echoed_at_startup(monkeypatch):
    monkeypatch.setenv(f"{FLAG_ENV_PREFIX}COGS_UI", "s3cr3t")
    with pytest.raises(ValidationError, match="'cogs_ui' must be a boolean") as caught:
        Config()
    assert "s3cr3t" not in str(caught.value)


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
    assert features.enabled_names == []


def _app_config(tmp_path, features: dict) -> Config:
    return Config.parse(
        {
            "storage": {"frames_path": str(tmp_path / "frames")},
            "frames": {"active_state": {"backend": "memory"}, "mcp_session_manager_enabled": False},
            "features": features,
        }
    )


@pytest.mark.asyncio
async def test_a_route_reads_flags_through_the_dependency():
    app = FastAPI()
    app.state.features = FeaturesConfig.model_validate({"cogs_ui": True})

    @app.get("/flag-probe")
    def flag_probe(features: FeaturesConfig = Depends(get_features)):
        return {"cogs_ui": features.enabled("cogs_ui")}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/flag-probe")).json() == {"cogs_ui": True}


def test_the_app_logs_each_retired_flag_once_logging_is_up(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config_module, "RETIRED_FEATURE_FLAGS", frozenset({"old_ui"}))
    with caplog.at_level(logging.WARNING, logger="frames_server.core"):
        make_app(_app_config(tmp_path, {"old_ui": True}))
    retired = [r for r in caplog.records if r.getMessage() == "feature_flag_retired_ignored"]
    assert [r.flag for r in retired] == ["old_ui"]


def test_the_shipped_registry_is_well_formed():
    config_module.check_feature_flag_registry(config_module.FEATURE_FLAGS, config_module.RETIRED_FEATURE_FLAGS)


@pytest.mark.parametrize("name", ["cogsUI", "cogs-ui", "cogs__ui", "_cogs", "cogs_", "1cogs", "cogs ui"])
def test_a_registered_name_outside_snake_case_is_refused(name):
    with pytest.raises(ValueError, match="must be lowercase snake_case"):
        config_module.check_feature_flag_registry({name: "A flag."}, frozenset())


def test_a_retired_name_outside_snake_case_is_refused():
    with pytest.raises(ValueError, match="must be lowercase snake_case: Old-UI"):
        config_module.check_feature_flag_registry({}, frozenset({"Old-UI"}))


def test_a_name_both_registered_and_retired_is_refused():
    with pytest.raises(ValueError, match="both registered and retired: cogs_ui"):
        config_module.check_feature_flag_registry({"cogs_ui": "A flag."}, frozenset({"cogs_ui"}))
