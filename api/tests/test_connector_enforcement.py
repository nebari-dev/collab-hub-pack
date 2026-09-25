"""A switched-off connector is actually off, not merely labelled off.

The switch would be theatre if it only changed what the admin screen printed.
What makes it real is that ``get_connectors_config`` -- the single dependency
every connector route already takes -- hands those routes a configuration with
the disabled connector's credentials blanked, so it is indistinguishable from
one that was never set up.
"""

from __future__ import annotations

import pytest

from collab_hub_api.config import ConnectorsConfig
from collab_hub_api.routers.connectors import get_connectors_config


class FakeState:
    def __init__(self, connectors, store=None):
        self.connectors_config = connectors
        if store is not None:
            self.connector_store = store


class FakeApp:
    def __init__(self, state):
        self.state = state


class FakeRequest:
    def __init__(self, state):
        self.app = FakeApp(state)


class Store:
    def __init__(self, disabled=(), raises=False):
        self._disabled = set(disabled)
        self._raises = raises

    def disabled(self):
        if self._raises:
            raise RuntimeError("database is unreachable")
        return set(self._disabled)


def configured() -> ConnectorsConfig:
    return ConnectorsConfig.model_validate(
        {
            "slack": {"broker_token_url": "https://broker.example.com/token"},
            "github": {"static_access_token": "gh-not-a-real-token"},
        }
    )


def test_a_disabled_connector_reaches_routes_with_no_credentials():
    request = FakeRequest(FakeState(configured(), Store(disabled={"slack"})))

    served = get_connectors_config(request)

    assert served.slack.broker_token_url == ""
    assert served.github.static_access_token == "gh-not-a-real-token"


def test_an_enabled_connector_is_untouched():
    request = FakeRequest(FakeState(configured(), Store()))

    served = get_connectors_config(request)

    assert served.slack.broker_token_url == "https://broker.example.com/token"


def test_a_deployment_with_no_switch_store_behaves_exactly_as_before():
    request = FakeRequest(FakeState(configured()))

    served = get_connectors_config(request)

    assert served is request.app.state.connectors_config


def test_an_unreachable_switch_store_disables_nothing():
    """Failing closed here would take every connector down over a blip.

    The switch is an administrator's preference, not an authorization decision:
    losing sight of it briefly should leave connectors working, which is the
    state they were in a moment earlier.
    """

    request = FakeRequest(FakeState(configured(), Store(raises=True)))

    served = get_connectors_config(request)

    assert served.slack.broker_token_url == "https://broker.example.com/token"


@pytest.mark.parametrize("name", ["slack", "github"])
def test_switching_one_off_never_touches_another(name):
    request = FakeRequest(FakeState(configured(), Store(disabled={name})))

    served = get_connectors_config(request)
    others = {"slack", "github"} - {name}
    (other,) = others

    assert getattr(served, name).broker_token_url == ""
    assert getattr(served, name).static_access_token == ""
    section = getattr(served, other)
    assert section.broker_token_url or section.static_access_token
