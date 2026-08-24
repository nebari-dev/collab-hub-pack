---
title: Concepts
---

This section explains the key concepts behind Nebari Software Packs and the
deployment methods the template supports.

## The NebariApp integration point

Every software pack has exactly one integration point with the Nebari platform: the
**NebariApp** custom resource. When you create a NebariApp, the
[nebari-operator](https://github.com/nebari-dev/nebari-operator) watches for it and
automatically configures routing, TLS, and authentication.

The NebariApp is just a Kubernetes resource - it can live in a plain YAML file, a
Kustomize base, or a Helm template. In Helm charts, you typically make it conditional
so the chart works both standalone and on Nebari:

```yaml
{{- if .Values.nebariapp.enabled }}
apiVersion: reconcilers.nebari.dev/v1
kind: NebariApp
metadata:
  name: {{ include "my-pack.fullname" . }}
spec:
  hostname: {{ required "nebariapp.hostname is required" .Values.nebariapp.hostname }}
  service:
    name: {{ include "my-pack.fullname" . }}
    port: 80
{{- end }}
```

With plain YAML or Kustomize, the NebariApp manifest is always present. When deploying
standalone, skip that file or exclude it from your apply command.

## Deployment methods

All three Kubernetes deployment methods are first-class in the template and in ArgoCD.

### Plain YAML

The lowest barrier to entry. Your pack is a set of `.yaml` files - `deployment.yaml`,
`service.yaml`, and `nebariapp.yaml`. Users run `kubectl apply -f .`.

Best for: packs with no configuration variability, or internal tools where simplicity
trumps flexibility.

### Kustomize

Kustomize overlays let you patch environment-specific values (hostname, auth settings,
resource limits) on top of a shared base without duplicating the base manifests.

```
base/
  kustomization.yaml
  deployment.yaml
  service.yaml
  nebariapp.yaml
overlays/
  dev/
    kustomization.yaml
    nebariapp-patch.yaml      # dev hostname, no auth
  production/
    kustomization.yaml
    nebariapp-patch.yaml      # prod hostname, auth + groups
```

Best for: packs deployed to multiple environments with known configuration differences.

### Helm

Helm is the most common choice for packs that wrap existing upstream software. You add
the upstream chart as a dependency and add a NebariApp template that points to its
service - you do not rewrite the app.

```yaml
# Chart.yaml - add the upstream chart as a dependency
dependencies:
  - name: podinfo
    version: 6.10.1
    repository: oci://ghcr.io/stefanprodan/charts
```

```yaml
# templates/nebariapp.yaml - the only template you write
{{- if .Values.nebariapp.enabled }}
apiVersion: reconcilers.nebari.dev/v1
kind: NebariApp
spec:
  hostname: {{ .Values.nebariapp.hostname }}
  service:
    name: {{ .Release.Name }}-podinfo   # upstream service name
    port: 9898
{{- end }}
```

Best for: wrapping existing Helm charts and for packs with rich configuration needs.

## Template examples

The template repo ships five examples of increasing complexity.

### Example 1: Vanilla YAML

Plain Kubernetes manifests. `kubectl apply -f examples/vanilla-yaml/`. No tooling
beyond `kubectl`. The NebariApp sits alongside `deployment.yaml` and `service.yaml`
as a peer file.

### Example 2: Kustomize (Nginx)

Same nginx app as the vanilla example, structured with Kustomize overlays for dev and
production. Demonstrates patching hostname and auth settings per environment without
duplicating the base.

### Example 3: Helm - Basic Pack (Nginx)

The simplest possible Helm chart - nginx with a conditional NebariApp template and a
`nebariapp.enabled` toggle. Shows the `standalone / Nebari` mode switch.

### Example 4: Helm - Auth-Aware FastAPI

A custom Python app that reads the `IdToken-*` cookie set by Envoy Gateway after
Keycloak authentication. The key snippet:

```python
def get_id_token(request: Request) -> str | None:
    for name, value in request.cookies.items():
        if name.startswith("IdToken-"):
            return value
    return None
```

Shows how to extract and decode the JWT to get `preferred_username`, `email`, and
`groups`.

### Example 5: Helm - Wrapping an Existing Chart (Podinfo)

**This is the most realistic use case.** Most Helm-based packs wrap existing software.
You add the upstream chart as a dependency, override values, and add a NebariApp that
points to the upstream service. You do not write a Deployment or Service of your own.

## Local development

The `dev/` directory provides a Makefile for local development with
[kind](https://kind.sigs.k8s.io/). Running any `up-*` target automatically creates a
kind cluster with the full Nebari infrastructure stack - MetalLB, Envoy Gateway,
cert-manager, Keycloak, and the nebari-operator.

```bash
cd dev

make up-vanilla    # deploy vanilla YAML example
make up-kustomize  # deploy kustomize example (dev overlay)
make up-basic      # deploy Helm nginx example
make up-fastapi    # deploy FastAPI Helm example (auth enabled)
make update-hosts  # update /etc/hosts with NebariApp hostnames
make down          # delete the kind cluster
```

The first `make up-*` run takes 5-10 minutes (cluster and infrastructure setup).
Subsequent runs reuse the existing cluster and are fast.

## Pack metadata

Every tracked pack has a `pack-metadata.yaml` at its repo root. Key fields:

```yaml
level: experimental          # experimental | alpha | beta | ga
owner: github-username
nebariapp_integration: full  # none | partial | full | na
scope:
  standalone-supported: yes
```

The [software pack dashboard](https://github.com/nebari-dev/software-pack-dashboard)
aggregates these files and renders a single view of every tracked pack. See the
[Release Readiness](/release-readiness/) page for the full field reference and the
promotion checklist.
