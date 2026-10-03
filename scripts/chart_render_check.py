#!/usr/bin/env python3
"""Assertions over a rendered collab-hub release for the Cog registry block.

    helm template t helm/collab-hub -f <values> | scripts/chart_render_check.py <case>

Driven by scripts/chart_render_tests.sh. Each case parses the API Deployment
and checks the actual attributes — secretKeyRef name and key, readOnly on the
mount, the JSON's per-source content — rather than grepping for a line that
happens to contain the right name. Needs PyYAML.
"""

from __future__ import annotations

import json
import sys

import yaml

CA_MOUNT = "/etc/collab-hub/cogs-ca"
SOURCES_VAR = "COLLAB_HUB_API__COGS__REGISTRY_SOURCES"


def env_id(source_id: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in source_id.upper())


class Rendered:
    def __init__(self, docs: list[dict]) -> None:
        self.docs = docs
        deployment = next(d for d in docs if d.get("kind") == "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.env = {e["name"]: e for e in container["env"]}
        self.mounts = {m["name"]: m for m in container.get("volumeMounts", [])}
        self.volumes = {v["name"]: v for v in pod.get("volumes", [])}
        self.cogs_env = sorted(n for n in self.env if "COGS" in n)
        self.sources_json = self.env[SOURCES_VAR]["value"] if SOURCES_VAR in self.env else None
        self.sources = {s["id"]: s for s in json.loads(self.sources_json)} if self.sources_json else {}
        self.source_order = [s["id"] for s in json.loads(self.sources_json)] if self.sources_json else []

    def value(self, name: str) -> str:
        return self.env[name]["value"]

    def secret_ref(self, name: str, secret: str, key: str) -> None:
        entry = self.env.get(name)
        assert entry is not None, f"{name} not rendered"
        assert "value" not in entry, f"{name} rendered as a literal value"
        ref = entry["valueFrom"]["secretKeyRef"]
        assert ref == {"name": secret, "key": key}, f"{name} -> {ref}, expected {secret}/{key}"

    def credentials_attached(self, source_id: str, secret: str, *, username_key="username", password_key="password"):
        source = self.sources[source_id]
        user_var, pw_var = (f"COLLAB_HUB_COGS_SOURCE_{env_id(source_id)}_{s}" for s in ("USERNAME", "PASSWORD"))
        assert source["credentials"] == {"username_env": user_var, "password_env": pw_var}, source
        self.secret_ref(user_var, secret, username_key)
        self.secret_ref(pw_var, secret, password_key)
        assert secret not in self.sources_json, f"Secret name {secret} leaked into the JSON"

    def webhook_attached(self, source_id: str, secret: str, *, key="secret") -> None:
        var = f"COLLAB_HUB_COGS_SOURCE_{env_id(source_id)}_WEBHOOK_SECRET"
        assert self.sources[source_id]["webhook_secret_env"] == var
        self.secret_ref(var, secret, key)
        assert secret not in self.sources_json, f"Secret name {secret} leaked into the JSON"

    def no_credentials(self, source_id: str) -> None:
        assert "credentials" not in self.sources[source_id]
        assert "webhook_secret_env" not in self.sources[source_id]
        prefix = f"COLLAB_HUB_COGS_SOURCE_{env_id(source_id)}_"
        assert not [n for n in self.env if n.startswith(prefix)], f"{source_id} has Secret env vars but no Secret"

    def no_source_vars_for(self, source_id: str) -> None:
        prefix = f"COLLAB_HUB_COGS_SOURCE_{env_id(source_id)}_"
        assert not [n for n in self.env if n.startswith(prefix)], f"stale env vars for removed source {source_id}"
        assert source_id not in self.sources


def case_default(r: Rendered) -> None:
    assert r.cogs_env == ["COLLAB_HUB_API__COGS__INDEX__ENABLED"], r.cogs_env
    assert not [n for n in r.env if n.startswith("COLLAB_HUB_API__FEATURES__")], "flag vars rendered with no flags set"
    assert r.value("COLLAB_HUB_API__COGS__INDEX__ENABLED") == "false"
    assert "cogs-ca-bundle" not in r.mounts and "cogs-ca-bundle" not in r.volumes


def case_fixture(r: Rendered) -> None:
    assert r.value("COLLAB_HUB_API__COGS__INDEX__ENABLED") == "true"
    assert r.value("COLLAB_HUB_API__COGS__INDEX__INTERVAL_SECONDS") == "120"
    assert r.value("COLLAB_HUB_API__COGS__INDEX__RUN_ON_STARTUP") == "false"
    assert r.source_order == ["harbor-main", "public.mirror"]
    harbor = r.sources["harbor-main"]
    assert harbor["kind"] == "harbor"
    assert harbor["api_url"] == "http://harbor-core.harbor.svc.cluster.local:80"
    assert harbor["token_url"] == "http://harbor-core.harbor.svc.cluster.local:80/service/token"
    assert harbor["projects"] == ["cogs"]
    assert harbor["ca_bundle_path"] == f"{CA_MOUNT}/ca.crt", "shared CA bundle applies to a source without its own"
    assert "request_timeout_seconds" not in harbor, "unset timeout is omitted, not defaulted, in the JSON"
    r.credentials_attached("harbor-main", "collab-hub-harbor-robot")
    r.webhook_attached("harbor-main", "collab-hub-harbor-webhook")
    static = r.sources["public.mirror"]
    assert static["kind"] == "static"
    assert static["index_url"] == "https://registry.example.com/catalog.v1.json"
    assert static["repositories"] == ["cogs/alpha"]
    assert static["ca_bundle_path"] == "/etc/ssl/certs/ca-certificates.crt", "per-source caBundlePath wins"
    assert static["request_timeout_seconds"] == 20
    r.no_credentials("public.mirror")
    mount = r.mounts["cogs-ca-bundle"]
    assert mount["mountPath"] == CA_MOUNT and mount["readOnly"] is True, mount
    volume = r.volumes["cogs-ca-bundle"]["configMap"]
    assert volume["name"] == "collab-hub-cogs-ca"
    assert volume["items"] == [{"key": "ca.crt", "path": "ca.crt"}]


def case_static_only(r: Rendered) -> None:
    # Sources with the indexer off: the source JSON still renders (the read
    # API needs it), the two tuning vars do not.
    assert r.value("COLLAB_HUB_API__COGS__INDEX__ENABLED") == "false"
    assert "COLLAB_HUB_API__COGS__INDEX__INTERVAL_SECONDS" not in r.env
    assert "COLLAB_HUB_API__COGS__INDEX__RUN_ON_STARTUP" not in r.env
    assert r.source_order == ["public"]
    assert "ca_bundle_path" not in r.sources["public"], "no CA bundle configured, none passed"
    assert "cogs-ca-bundle" not in r.mounts


def case_three_sources(r: Rendered) -> None:
    assert r.source_order == ["harbor-main", "mid.one", "tail-two"]
    r.credentials_attached("harbor-main", "robot-a")
    assert "webhook_secret_env" not in r.sources["harbor-main"]
    r.credentials_attached("mid.one", "robot-b", username_key="user", password_key="pass")
    r.webhook_attached("tail-two", "hook-c", key="token")
    assert "credentials" not in r.sources["tail-two"]


def case_reordered(r: Rendered) -> None:
    assert r.source_order == ["tail-two", "harbor-main"]
    r.webhook_attached("tail-two", "hook-c", key="token")
    assert "credentials" not in r.sources["tail-two"]
    r.credentials_attached("harbor-main", "robot-a")
    assert "webhook_secret_env" not in r.sources["harbor-main"]
    r.no_source_vars_for("mid.one")


def case_features(r: Rendered) -> None:
    flags = {n: r.value(n) for n in r.env if n.startswith("COLLAB_HUB_API__FEATURES__")}
    assert flags == {
        "COLLAB_HUB_API__FEATURES__COGS_UI": "true",
        "COLLAB_HUB_API__FEATURES__NEW_THING": "false",
    }, flags


SERVE_PREFIX = "COLLAB_HUB_API__COGS__SERVE__"


def serve_env(r: Rendered) -> dict[str, str]:
    return {n[len(SERVE_PREFIX) :]: r.value(n) for n in r.env if n.startswith(SERVE_PREFIX)}


def kind(r: Rendered, name: str) -> dict | None:
    return next((d for d in r.docs if d.get("kind") == name), None)


def case_serve_off(r: Rendered) -> None:
    # Sources configured, serving left off: no trace of it anywhere.
    assert serve_env(r) == {}, serve_env(r)
    app = kind(r, "NebariApp")
    assert app is not None
    assert [route["pathPrefix"] for route in app["spec"]["routing"]["publicRoutes"]] == ["/v1", "/mcp", "/health"]


def case_serve_nebariapp(r: Rendered) -> None:
    # The public URL is derived from the NebariApp hostname, and /v2 joins the
    # routes the gateway leaves to the app's own authentication.
    assert serve_env(r) == {
        "ENABLED": "true",
        "PUBLIC_URL": "https://hub.example.com",
        "CREDENTIAL_TTL_SECONDS": "900",
        "TOKEN_TTL_SECONDS": "300",
        "MAX_BLOB_BYTES": "1073741824",
        "MAX_BLOB_SECONDS": "900",
    }, serve_env(r)
    public = kind(r, "NebariApp")["spec"]["routing"]["publicRoutes"]
    assert public[-1] == {"pathPrefix": "/v2", "pathType": "PathPrefix"}, public
    assert [route["pathPrefix"] for route in public] == ["/v1", "/mcp", "/health", "/v2"]
    assert kind(r, "HTTPRoute") is None


def case_serve_httproute(r: Rendered) -> None:
    # An explicit public URL adds a port to the ingress host, a 5 GiB limit renders
    # as an integer (not 5.36870912e+09), and /v2 gets its own rule, first,
    # with a request timeout that outlasts the blob time limit.
    env = serve_env(r)
    assert env["PUBLIC_URL"] == "https://hub.example.com:8443", env
    assert env["MAX_BLOB_BYTES"] == "5368709120", env
    assert env["MAX_BLOB_SECONDS"] == "1800" and env["CREDENTIAL_TTL_SECONDS"] == "600", env
    rules = kind(r, "HTTPRoute")["spec"]["rules"]
    assert rules[0]["matches"] == [{"path": {"type": "PathPrefix", "value": "/v2"}}], rules[0]
    assert rules[0]["timeouts"] == {"request": "1830s"}, rules[0]
    assert rules[0]["backendRefs"] == rules[1]["backendRefs"]
    assert rules[1]["matches"] == [{"path": {"type": "PathPrefix", "value": "/"}}] and "timeouts" not in rules[1]


def case_serve_httproute_no_timeout(r: Rendered) -> None:
    # routeTimeout=false (Gateway API CRDs without HTTPRoute timeouts): the
    # host's own "/" rule carries /v2, and no rule sets a timeout.
    assert serve_env(r)["PUBLIC_URL"] == "https://hub.example.com"
    rules = kind(r, "HTTPRoute")["spec"]["rules"]
    assert len(rules) == 1 and "timeouts" not in rules[0], rules


CASES = {
    "serve-off": case_serve_off,
    "serve-nebariapp": case_serve_nebariapp,
    "serve-httproute": case_serve_httproute,
    "serve-httproute-no-timeout": case_serve_httproute_no_timeout,
    "features": case_features,
    "default": case_default,
    "fixture": case_fixture,
    "static-only": case_static_only,
    "three-sources": case_three_sources,
    "reordered": case_reordered,
}


def main() -> int:
    case = sys.argv[1]
    rendered = Rendered([d for d in yaml.safe_load_all(sys.stdin) if d])
    try:
        CASES[case](rendered)
    except AssertionError as exc:
        print(f"{case}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
