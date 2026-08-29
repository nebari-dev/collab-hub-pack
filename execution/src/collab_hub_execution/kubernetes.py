"""Kubernetes CogExecutor: materialize a Cog worker as a Deployment + Service.

Talks to the API server with the pod ServiceAccount token over httpx (no
kubernetes-client dependency). This is the only place K8s primitives are touched
(ADR invariant 2: materialization behind the pluggable executor). The API client
and the worker HTTP client are injectable so the executor is unit-testable
without a cluster; the defaults are the in-cluster SA-token client and httpx.

Workers are reached over in-cluster service DNS (never port-forwards). Each
materialization gets a name that is a valid DNS label and unique per (Cog, run),
so concurrent runs never share or tear down each other's workloads.

Security boundary: a worker's ``/invoke`` Service is unauthenticated, so any
workload that can reach it can call arbitrary entry points with arbitrary input.
This executor therefore assumes a trusted, single-tenant namespace. It reduces
blast radius (workers carry no ServiceAccount token — automountServiceAccountToken
is false) but does not itself authenticate callers or restrict traffic. In a
shared or multi-tenant cluster it must be paired with a NetworkPolicy that admits
only the hub and bounds worker egress (#11), and, longer term, request
authentication on the worker endpoint.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Protocol

_log = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "collab-hub"
RUNNER_PORT = 8080
MAX_DNS_LABEL = 63
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105 - path, not a secret
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


def cog_slug(cog: str) -> str:
    """Reduce a Cog id's last path segment to a DNS-label-safe token.

    Lowercased, restricted to ``[a-z0-9-]`` (non-ASCII/alnum -> ``-``), stripped
    of leading/trailing hyphens, capped, and never empty.
    """
    raw = cog.split("/")[-1].lower()
    cleaned = "".join(ch if (ch.isascii() and ch.isalnum()) or ch == "-" else "-" for ch in raw)
    cleaned = cleaned.strip("-")
    return cleaned[:40] or "cog"


DIGEST_HEX = 16  # 64-bit suffix: birthday collisions stay negligible at realistic run volumes


def resource_name(cog: str, run_id: str) -> str:
    """A valid, unique DNS-1123 label for one (Cog, run) materialization.

    Deterministic (recovery re-derives the same name), unique per run (concurrent
    runs of the same Cog don't collide), and collision-resistant across Cogs whose
    slugs would otherwise coincide (the digest is over the full Cog id + run id).
    The suffix is ``DIGEST_HEX`` hex chars (64 bits), so the name stays within the
    63-char DNS-label limit while keeping collisions negligible at realistic scale.
    """
    digest = hashlib.sha1(f"{cog}\x00{run_id}".encode()).hexdigest()[:DIGEST_HEX]  # noqa: S324 - name uniqueness, not security
    return f"cog-{cog_slug(cog)}-{digest}"[:MAX_DNS_LABEL].rstrip("-")


def label_value(value: str) -> str:
    """A safe Kubernetes label value: ``[A-Za-z0-9._-]``, <=63, alnum-bounded.

    Arbitrary ids (run ids with ``/``, spaces, or length) would make object
    creation fail, so the value is normalized for the label and the exact id is
    kept in an annotation instead (see ``_manifests``).
    """
    cleaned = "".join(ch if (ch.isascii() and ch.isalnum()) or ch in "-_." else "-" for ch in value)
    cleaned = cleaned.strip("-_.")[:63].strip("-_.")
    return cleaned or "unknown"


class K8sApi(Protocol):
    """Minimal shape of the Kubernetes API client the executor needs."""

    def post(self, path: str, *, json: Any) -> Any: ...
    def patch(self, path: str, *, json: Any, headers: dict[str, str]) -> Any: ...
    def delete(self, path: str) -> Any: ...
    def get(self, path: str) -> Any: ...


class WorkerHttp(Protocol):
    def post(self, url: str, *, json: Any) -> Any: ...


def _in_cluster_api() -> Any:
    import httpx

    with open(TOKEN_PATH) as handle:
        token = handle.read().strip()
    # Prefer the injected service env (an IP:port) so the API server needs no DNS.
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS") or os.environ.get("KUBERNETES_SERVICE_PORT") or "443"
    base_url = f"https://{host}:{port}" if host else "https://kubernetes.default.svc"
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        verify=CA_PATH,
        timeout=30,
    )


def _default_worker_http() -> Any:
    import httpx

    return httpx.Client(timeout=60)


class KubernetesCogExecutor:
    """Materialize a Cog worker as a Deployment + Service and reach it by DNS."""

    name = "kubernetes"

    def __init__(
        self,
        *,
        runner_image: str,
        namespace: str = DEFAULT_NAMESPACE,
        api: K8sApi | None = None,
        worker_http: WorkerHttp | None = None,
        ready_timeout: float = 60.0,
        poll_interval: float = 1.0,
    ) -> None:
        self.runner_image = runner_image
        self.namespace = namespace
        self._api = api
        self._worker_http = worker_http
        self.ready_timeout = ready_timeout
        self.poll_interval = poll_interval

    @property
    def api(self) -> K8sApi:
        if self._api is None:
            self._api = _in_cluster_api()
        return self._api

    @property
    def worker_http(self) -> WorkerHttp:
        if self._worker_http is None:
            self._worker_http = _default_worker_http()
        return self._worker_http

    def _service_url(self, name: str) -> str:
        return f"http://{name}.{self.namespace}.svc.cluster.local:{RUNNER_PORT}"

    def _manifests(self, cog: str, run_id: str, name: str) -> tuple[dict, dict]:
        labels = {
            "app": name,
            "collab-hub/cog": cog_slug(cog),
            "collab-hub/run": label_value(run_id),
            "app.kubernetes.io/managed-by": "collab-hub-executor",
        }
        # Label values are charset/length-constrained; keep the exact run id in an
        # annotation (unconstrained) so nothing is lost and creation never fails.
        annotations = {"collab-hub/run-id": run_id}
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": self.namespace, "labels": labels, "annotations": annotations},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": labels, "annotations": annotations},
                    "spec": {
                        # A Cog worker talks to the hub, not the Kubernetes API, so
                        # it should not carry a ServiceAccount token it could use
                        # (or leak) against the API server.
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "runner",
                                "image": self.runner_image,
                                "imagePullPolicy": "IfNotPresent",
                                "ports": [{"containerPort": RUNNER_PORT}],
                                "env": [{"name": "COG_ID", "value": cog}],
                                "readinessProbe": {
                                    "httpGet": {"path": "/healthz", "port": RUNNER_PORT},
                                    "initialDelaySeconds": 1,
                                    "periodSeconds": 1,
                                },
                                "resources": {
                                    "requests": {"cpu": "50m", "memory": "64Mi"},
                                    "limits": {"cpu": "500m", "memory": "256Mi"},
                                },
                            }
                        ]
                    },
                },
            },
        }
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": self.namespace, "labels": labels, "annotations": annotations},
            "spec": {
                "selector": {"app": name},
                "ports": [{"port": RUNNER_PORT, "targetPort": RUNNER_PORT}],
            },
        }
        return deployment, service

    def _apply(self, collection_path: str, name: str, body: dict, *, replaceable: bool) -> None:
        response = self.api.post(collection_path, json=body)
        if response.status_code == 409:  # already exists
            if not replaceable:
                return
            # merge-patch updates the existing object's spec; RBAC grants `patch`
            # (not `update`), so this stays within least privilege.
            response = self.api.patch(
                f"{collection_path}/{name}",
                json=body,
                headers={"Content-Type": "application/merge-patch+json"},
            )
        response.raise_for_status()

    def _wait_ready(self, name: str) -> None:
        path = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}"
        deadline = time.monotonic() + self.ready_timeout
        while True:
            response = self.api.get(path)
            response.raise_for_status()
            ready = (response.json().get("status") or {}).get("readyReplicas") or 0
            if ready >= 1:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"deployment {name} not ready within {self.ready_timeout}s")
            time.sleep(self.poll_interval)

    def _resource_paths(self, name: str) -> tuple[str, str]:
        return (
            f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}",
            f"/api/v1/namespaces/{self.namespace}/services/{name}",
        )

    def materialize(self, cog: str, run_id: str) -> "_KubernetesWorker":
        name = resource_name(cog, run_id)
        deployment, service = self._manifests(cog, run_id, name)
        try:
            self._apply(
                f"/apis/apps/v1/namespaces/{self.namespace}/deployments", name, deployment, replaceable=True
            )
            self._apply(
                f"/api/v1/namespaces/{self.namespace}/services", name, service, replaceable=False
            )
            self._wait_ready(name)
        except Exception:
            # A partial materialization (e.g. Deployment created, then Service or
            # readiness failed) would leak a workload the engine can't tear down —
            # it never got a worker handle. Delete by the deterministic name,
            # best-effort, then re-raise the original failure.
            self._cleanup(name)
            raise
        return _KubernetesWorker(cog, name, self._service_url(name), self.worker_http)

    def _cleanup(self, name: str) -> None:
        """Best-effort delete of a (possibly partial) materialization by name.

        Runs while a materialize() failure is already propagating, so it never
        raises (that would mask the original cause). It still validates each delete
        the way teardown() does — an HTTP error response does not raise on its own —
        and logs any resource it could not remove, so an orphan is visible for
        remediation instead of being silently swallowed.
        """
        for path in self._resource_paths(name):
            try:
                response = self.api.delete(path)
                status = getattr(response, "status_code", None)
                if status is not None and status not in (200, 202, 404):
                    _log.warning("cleanup of %s returned HTTP %s; resource may be orphaned", path, status)
            except Exception as exc:  # noqa: BLE001 - cleanup runs during failure handling
                _log.warning("cleanup of %s failed: %r; resource may be orphaned", path, exc)

    def teardown(self, worker: "_KubernetesWorker") -> None:
        for path in self._resource_paths(worker.name):
            response = self.api.delete(path)
            if response.status_code not in (200, 202, 404):
                response.raise_for_status()


class _KubernetesWorker:
    """A materialized Cog reached over its service DNS via its declared entry point."""

    def __init__(self, cog: str, name: str, url: str, http: WorkerHttp) -> None:
        self.cog = cog
        self.name = name
        self.url = url
        self.http = http

    def interact(self, entry_point: str, input: Any = None, idempotency_key: str | None = None) -> Any:
        response = self._post_with_retry(
            f"{self.url}/invoke",
            {"entry_point": entry_point, "input": input, "idempotency_key": idempotency_key},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("pause"):
            # The Cog asked to pause for a decision — surface it as the engine's
            # PauseRequest so a Gate/human can approve, reject, or send back.
            from .orchestration import PauseRequest

            raise PauseRequest(body.get("reason", "cog requested a pause"))
        return body.get("output")

    def _post_with_retry(self, url: str, payload: Any, *, attempts: int = 40, delay: float = 0.5) -> Any:
        # A freshly materialized Service can be briefly unroutable (endpoints /
        # kube-proxy still programming) even after the pod is Ready. Retry the
        # connection only — HTTP responses (including errors) are returned as-is.
        last: Exception | None = None
        for _ in range(attempts):
            try:
                return self.http.post(url, json=payload)
            except Exception as exc:  # noqa: BLE001 - connection-level failures only
                last = exc
                time.sleep(delay)
        raise last  # type: ignore[misc]
