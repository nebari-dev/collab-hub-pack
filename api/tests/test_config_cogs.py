"""The ``cogs:`` config block (#87): cross-source validation and Secret indirection.

Per-source rules are covered in test_cog_registry.py against the model itself;
here every case goes through ``Config`` so the assertion is "this fails at
settings load", which is the whole point of validating in config.py.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from collab_hub_api.config import Config, resolve_cogs_source_secrets

HARBOR_URL = "https://harbor.example.com"
STATIC_URL = "https://registry.example.com"


def harbor_source(**overrides) -> dict:
    return {"id": "harbor-main", "kind": "harbor", "url": HARBOR_URL, "projects": ["cogs"], **overrides}


def static_source(**overrides) -> dict:
    return {"id": "public", "kind": "static", "url": STATIC_URL, "repositories": ["cogs/alpha"], **overrides}


def parse_cogs(**cogs) -> Config:
    return Config.parse({"cogs": cogs})


# --- Defaults and the enabled/sources coupling -------------------------------


def test_defaults_are_off_and_empty():
    cogs = Config.parse().cogs

    assert cogs.registry_sources == []
    assert cogs.index.enabled is False
    assert cogs.index.interval_seconds == 300
    assert cogs.index.run_on_startup is True


def test_sources_with_index_disabled_is_valid():
    # The read API can serve a static index: sources configured, no sweeps.
    cogs = parse_cogs(registry_sources=[harbor_source(), static_source()], index={"enabled": False}).cogs

    assert [source.id for source in cogs.registry_sources] == ["harbor-main", "public"]
    assert cogs.index.enabled is False


def test_index_enabled_requires_at_least_one_source():
    with pytest.raises(ValidationError, match="cogs.index.enabled is true but cogs.registry_sources is empty"):
        parse_cogs(index={"enabled": True})


def test_index_enabled_with_a_source_is_valid():
    cogs = parse_cogs(registry_sources=[static_source()], index={"enabled": True, "interval_seconds": 60}).cogs

    assert cogs.index.enabled is True
    assert cogs.index.interval_seconds == 60


@pytest.mark.parametrize("interval", [0, 9, 24 * 3600 + 1])
def test_index_interval_is_bounded(interval):
    with pytest.raises(ValidationError, match="interval_seconds"):
        parse_cogs(registry_sources=[static_source()], index={"interval_seconds": interval})


# --- Each validation failure, observed at settings load ----------------------


def test_unknown_kind_fails():
    with pytest.raises(ValidationError, match="kind"):
        parse_cogs(registry_sources=[harbor_source(kind="quay")])


def test_duplicate_id_fails_and_names_both_positions():
    with pytest.raises(ValidationError, match=r"registry_sources\[1\] reuses id 'main'.*registry_sources\[0\]"):
        parse_cogs(registry_sources=[harbor_source(id="main"), static_source(id="main")])


def test_harbor_without_projects_fails():
    with pytest.raises(ValidationError, match="requires at least one entry in projects"):
        parse_cogs(registry_sources=[harbor_source(projects=[])])


def test_static_without_repositories_or_index_url_fails():
    with pytest.raises(ValidationError, match="requires repositories and/or index_url"):
        parse_cogs(registry_sources=[static_source(repositories=[])])


def test_unknown_source_key_fails_rather_than_being_ignored():
    # A typo in a secret indirection (``pasword_env``) must not leave the
    # source silently unauthenticated.
    with pytest.raises(ValidationError, match="pasword_env"):
        parse_cogs(registry_sources=[harbor_source(credentials={"username": "robot", "pasword_env": "X"})])


# --- Secret indirection: *_env names a variable the chart mounts -------------


def test_credentials_and_webhook_resolve_from_named_env_vars(monkeypatch):
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_USERNAME", "robot$cogs+indexer")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD", " robot-secret-value ")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_WEBHOOK_SECRET", "hook-secret-value")

    [source] = parse_cogs(
        registry_sources=[
            harbor_source(
                credentials={
                    "username_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_USERNAME",
                    "password_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD",
                },
                webhook_secret_env="COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_WEBHOOK_SECRET",
            )
        ]
    ).cogs.registry_sources

    assert source.credentials.configured
    assert source.credentials.username == "robot$cogs+indexer"
    # The model's own strip applies to the resolved value too.
    assert source.credentials.password.get_secret_value() == "robot-secret-value"
    assert source.webhook_secret.get_secret_value() == "hook-secret-value"
    # Neither the model nor its error text leaks the secret.
    assert "robot-secret-value" not in repr(source)
    assert "hook-secret-value" not in repr(source)


def test_inline_username_with_password_from_env(monkeypatch):
    monkeypatch.setenv("PW", "hunter2-secret")

    [source] = parse_cogs(
        registry_sources=[harbor_source(credentials={"username": "robot", "password_env": "PW"})]
    ).cogs.registry_sources

    assert source.credentials.password.get_secret_value() == "hunter2-secret"


def test_missing_env_var_fails_at_startup_and_names_it(monkeypatch):
    monkeypatch.delenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD", raising=False)

    with pytest.raises(ValidationError) as excinfo:
        parse_cogs(
            registry_sources=[
                harbor_source(
                    credentials={"username": "robot", "password_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD"}
                )
            ]
        )

    message = str(excinfo.value)
    assert "registry_sources[0] ('harbor-main').credentials.password_env" in message
    assert "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD, which is not set" in message


def test_empty_env_var_is_treated_as_missing(monkeypatch):
    # An empty Secret value is what a wrong `key:` under existingSecret yields.
    monkeypatch.setenv("WH", "   ")

    with pytest.raises(ValidationError, match="names WH, which is empty"):
        parse_cogs(registry_sources=[harbor_source(webhook_secret_env="WH")])


def test_blank_env_name_is_refused():
    with pytest.raises(ValidationError, match="password_env must name an environment variable"):
        parse_cogs(registry_sources=[harbor_source(credentials={"username": "robot", "password_env": " "})])


def test_inline_value_and_env_indirection_together_are_refused(monkeypatch):
    monkeypatch.setenv("PW", "from-env")

    with pytest.raises(ValidationError, match="sets both password and password_env"):
        credentials = {"username": "robot", "password": "inline", "password_env": "PW"}
        parse_cogs(registry_sources=[harbor_source(credentials=credentials)])


def test_env_password_without_username_still_trips_both_or_neither(monkeypatch):
    # Resolution happens before the model's own rules, which then still apply.
    monkeypatch.setenv("PW", "secret-value")

    with pytest.raises(ValidationError, match="must be set together"):
        parse_cogs(registry_sources=[harbor_source(credentials={"password_env": "PW"})])


def test_resolver_passes_non_mappings_through():
    sentinel = object()

    assert resolve_cogs_source_secrets(sentinel, 0, {}) is sentinel


def test_resolver_does_not_mutate_its_input():
    raw = {"id": "x", "credentials": {"username": "u", "password_env": "PW"}}

    resolved = resolve_cogs_source_secrets(raw, 0, {"PW": "pw"})

    assert raw["credentials"] == {"username": "u", "password_env": "PW"}
    assert resolved["credentials"] == {"username": "u", "password": "pw"}


# --- The chart's contract: one JSON env var for the list, plus named Secrets --


def test_env_round_trip_as_the_chart_renders_it(monkeypatch):
    """The exact shape api-deployment.yaml produces: JSON list + per-source Secret env vars."""

    sources = [
        {
            "id": "harbor-main",
            "kind": "harbor",
            "url": HARBOR_URL,
            "api_url": "http://harbor-core.harbor.svc:80",
            "token_url": "http://harbor-core.harbor.svc:80/service/token",
            "projects": ["cogs"],
            "ca_bundle_path": "/etc/collab-hub/cogs-ca/ca.crt",
            "credentials": {
                "username_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_USERNAME",
                "password_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD",
            },
            "webhook_secret_env": "COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_WEBHOOK_SECRET",
        },
        {
            "id": "public.mirror",
            "kind": "static",
            "url": STATIC_URL,
            "index_url": f"{STATIC_URL}/catalog.v1.json",
            "credentials": {
                "username_env": "COLLAB_HUB_COGS_SOURCE_PUBLIC_MIRROR_USERNAME",
                "password_env": "COLLAB_HUB_COGS_SOURCE_PUBLIC_MIRROR_PASSWORD",
            },
        },
    ]
    monkeypatch.setenv("COLLAB_HUB_API__COGS__REGISTRY_SOURCES", json.dumps(sources))
    monkeypatch.setenv("COLLAB_HUB_API__COGS__INDEX__ENABLED", "true")
    monkeypatch.setenv("COLLAB_HUB_API__COGS__INDEX__INTERVAL_SECONDS", "120")
    monkeypatch.setenv("COLLAB_HUB_API__COGS__INDEX__RUN_ON_STARTUP", "false")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_USERNAME", "robot$cogs+indexer")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_PASSWORD", "robot-secret-value")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_WEBHOOK_SECRET", "hook-secret-value")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_PUBLIC_MIRROR_USERNAME", "reader")
    monkeypatch.setenv("COLLAB_HUB_COGS_SOURCE_PUBLIC_MIRROR_PASSWORD", "reader-secret")

    cogs = Config().cogs

    assert cogs.index.enabled is True
    assert cogs.index.interval_seconds == 120
    assert cogs.index.run_on_startup is False
    harbor, static = cogs.registry_sources
    assert harbor.kind == "harbor"
    assert harbor.api_url == "http://harbor-core.harbor.svc:80"
    assert harbor.ca_bundle_path == "/etc/collab-hub/cogs-ca/ca.crt"
    assert harbor.credentials.username == "robot$cogs+indexer"
    assert harbor.credentials.password.get_secret_value() == "robot-secret-value"
    assert harbor.webhook_secret.get_secret_value() == "hook-secret-value"
    assert static.kind == "static"
    assert static.credentials.password.get_secret_value() == "reader-secret"
    assert static.webhook_secret.get_secret_value() == ""


def test_env_round_trip_missing_secret_names_the_variable(monkeypatch):
    monkeypatch.setenv(
        "COLLAB_HUB_API__COGS__REGISTRY_SOURCES",
        json.dumps([harbor_source(credentials={"username_env": "U", "password_env": "P"})]),
    )
    monkeypatch.setenv("U", "robot")
    monkeypatch.delenv("P", raising=False)

    with pytest.raises(ValidationError, match="password_env names P, which is not set"):
        Config()


def test_index_style_env_override_is_not_a_supported_route(monkeypatch):
    """Pins the measurement behind the *_env design (pydantic-settings 2.14).

    Layered over a JSON list, ``..._SOURCES__0__CREDENTIALS__PASSWORD`` is
    silently ignored. If a future pydantic-settings starts honoring it this
    test fails, which is the cue to revisit the indirection — not a bug.
    """

    monkeypatch.setenv(
        "COLLAB_HUB_API__COGS__REGISTRY_SOURCES",
        json.dumps([static_source(credentials={"username": "reader", "password": "inline-secret"})]),
    )
    monkeypatch.setenv("COLLAB_HUB_API__COGS__REGISTRY_SOURCES__0__CREDENTIALS__PASSWORD", "from-index-var")

    [source] = Config().cogs.registry_sources

    assert source.credentials.password.get_secret_value() == "inline-secret"
