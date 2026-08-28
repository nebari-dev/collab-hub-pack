"""KubernetesCogExecutor unit tests — no cluster; the API + worker HTTP are faked."""

import re

import pytest

from collab_hub_execution import (
    KubernetesCogExecutor,
    PauseRequest,
    cog_slug,
    label_value,
    resource_name,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakeK8sApi:
    """Records calls; returns created responses and a ready deployment."""

    def __init__(self, *, conflict_on_deployment: bool = False, ready_after: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self.conflict_on_deployment = conflict_on_deployment
        self._gets = 0
        self._ready_after = ready_after

    def post(self, path: str, *, json: dict) -> FakeResponse:
        self.calls.append(("POST", path))
        if self.conflict_on_deployment and "deployments" in path:
            return FakeResponse(409)
        return FakeResponse(201)

    def patch(self, path: str, *, json: dict, headers: dict) -> FakeResponse:
        self.calls.append(("PATCH", path))
        return FakeResponse(200)

    def delete(self, path: str) -> FakeResponse:
        self.calls.append(("DELETE", path))
        return FakeResponse(200)

    def get(self, path: str) -> FakeResponse:
        self.calls.append(("GET", path))
        ready = 1 if self._gets >= self._ready_after else 0
        self._gets += 1
        return FakeResponse(200, {"status": {"readyReplicas": ready}})


class FakeWorkerHttp:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict) -> FakeResponse:
        self.posts.append((url, json))
        return FakeResponse(200, {"output": f"handled:{json['entry_point']}"})


def _executor(api, worker_http):
    return KubernetesCogExecutor(
        runner_image="collab-hub/cog-runner:test",
        namespace="cogs",
        api=api,
        worker_http=worker_http,
        poll_interval=0,
    )


def test_materialize_creates_deployment_and_service_then_reaches_by_dns():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    worker = _executor(api, http).materialize("openteams/research-analyst", "run-1")

    assert ("POST", "/apis/apps/v1/namespaces/cogs/deployments") in api.calls
    assert ("POST", "/api/v1/namespaces/cogs/services") in api.calls
    # reached only over service DNS, at the per-(cog, run) name — never a port-forward
    assert worker.url == f"http://{worker.name}.cogs.svc.cluster.local:8080"
    assert worker.name.startswith("cog-research-analyst-")


def test_interact_posts_to_the_entry_point_with_idempotency_key():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    worker = _executor(api, http).materialize("openteams/draft-generator", "run-2")
    assert worker.interact("draft", {"topic": "x"}, idempotency_key="run-2:draft:0") == "handled:draft"
    url, body = http.posts[-1]
    assert url.endswith("/invoke")
    assert body["entry_point"] == "draft" and body["idempotency_key"] == "run-2:draft:0"


def test_existing_deployment_is_replaced_with_a_merge_patch_not_update():
    api = FakeK8sApi(conflict_on_deployment=True)
    worker = _executor(api, FakeWorkerHttp()).materialize("openteams/doc-formatter", "run-3")
    # 409 is resolved with PATCH (granted by RBAC), never PUT (update, not granted)
    assert ("PATCH", f"/apis/apps/v1/namespaces/cogs/deployments/{worker.name}") in api.calls
    assert not any(method == "PUT" for method, _ in api.calls)


def test_wait_ready_polls_until_ready():
    api = FakeK8sApi(ready_after=2)  # not ready on the first two GETs
    _executor(api, FakeWorkerHttp()).materialize("openteams/x", "run-4")
    assert sum(1 for method, _ in api.calls if method == "GET") >= 3


def test_teardown_deletes_deployment_and_service_by_worker_name():
    api, http = FakeK8sApi(), FakeWorkerHttp()
    ex = _executor(api, http)
    worker = ex.materialize("openteams/reviewer", "run-5")
    ex.teardown(worker)
    assert ("DELETE", f"/apis/apps/v1/namespaces/cogs/deployments/{worker.name}") in api.calls
    assert ("DELETE", f"/api/v1/namespaces/cogs/services/{worker.name}") in api.calls


class PausingWorkerHttp:
    def post(self, url: str, *, json: dict) -> FakeResponse:
        return FakeResponse(200, {"pause": True, "reason": "needs approval"})


def test_runner_pause_response_becomes_a_pause_request():
    worker = _executor(FakeK8sApi(), PausingWorkerHttp()).materialize("openteams/gated", "run-6")
    with pytest.raises(PauseRequest):
        worker.interact("review", {"draft": "v1"})


# --- resource naming (#2 concurrency, #6 DNS validity) ---


def test_cog_slug_is_dns_safe_bounded_and_nonempty():
    assert cog_slug("openteams/Research Analyst!") == "research-analyst"
    assert cog_slug("openteams/café") == "caf"  # non-ASCII dropped
    assert cog_slug("openteams/---") == "cog"  # empty -> fallback, never blank
    assert len(cog_slug("x/" + "a" * 200)) <= 40


def test_resource_name_is_valid_unique_per_run_and_collision_resistant():
    n_a = resource_name("openteams/reviewer", "run-A")
    n_b = resource_name("openteams/reviewer", "run-B")
    assert n_a != n_b  # concurrent runs of the same Cog never collide
    assert n_a == resource_name("openteams/reviewer", "run-A")  # deterministic (recovery-safe)
    assert len(n_a) <= 63
    assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", n_a)  # valid DNS-1123 label
    # slug-colliding Cogs still get distinct names (digest is over the full id)
    assert resource_name("a/reviewer", "r") != resource_name("b/reviewer", "r")


def test_label_value_is_a_safe_kubernetes_label(monkeypatch):
    # run ids with '/', spaces, or excess length must not break object creation
    assert label_value("org/ws/run 42") == "org-ws-run-42"
    assert len(label_value("x" * 200)) <= 63
    assert label_value("///") == "unknown"  # never blank
    assert re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", label_value("org/ws/run 42"))


def test_manifests_keep_the_exact_run_id_in_an_annotation():
    ex = _executor(FakeK8sApi(), FakeWorkerHttp())
    _deployment, service = ex._manifests("openteams/reviewer", "org/ws/run 42", "cog-reviewer-abcd1234")
    assert service["metadata"]["labels"]["collab-hub/run"] == "org-ws-run-42"  # sanitized label
    assert service["metadata"]["annotations"]["collab-hub/run-id"] == "org/ws/run 42"  # exact id preserved
