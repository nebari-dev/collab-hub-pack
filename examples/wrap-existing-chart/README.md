# Example 5: Wrapping an Existing Chart (Podinfo)

This is the most realistic use case for a Helm-based Nebari Software Pack.
Instead of writing your own Deployment and Service templates, you add an
existing Helm chart as a dependency and create a NebariApp resource that points
to its service.

**Key lesson: You don't rewrite the app - you just connect it to Nebari.**

## What This Example Shows

- Adding an upstream Helm chart as a `Chart.yaml` dependency
- Overriding upstream values via your own `values.yaml`
- Creating a NebariApp that points to the upstream chart's service
- No custom Deployment or Service templates needed

## How It Works

```
Chart.yaml
  dependencies:
    - name: podinfo          # Upstream chart handles pods + services
      version: 6.10.1
      repository: oci://ghcr.io/stefanprodan/charts

templates/
  nebariapp.yaml             # Points to podinfo's service -> Nebari routes to it
  (no deployment.yaml!)      # Podinfo chart handles this
  (no service.yaml!)         # Podinfo chart handles this
```

The only template you write is `nebariapp.yaml`. Everything else comes from the
upstream chart.

## Deploying to Nebari

### ArgoCD Application (recommended)

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: my-pack
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/YOUR-ORG/YOUR-REPO.git
    targetRevision: main
    path: examples/wrap-existing-chart/chart
    helm:
      valuesObject:
        nebariapp:
          enabled: true
          hostname: my-pack.nebari.example.com
  destination:
    server: https://kubernetes.default.svc
    namespace: my-pack
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

To override upstream values via ArgoCD:

```yaml
    helm:
      valuesObject:
        nebariapp:
          enabled: true
          hostname: my-pack.nebari.example.com
        podinfo:
          ui:
            message: "Custom greeting!"
```

### Helm install

```bash
# Build dependencies first
helm dependency update ./chart/

# Deploy on Nebari
helm install my-pack ./chart/ \
  --set nebariapp.enabled=true \
  --set nebariapp.hostname=my-pack.nebari.example.com

# Override upstream values
helm install my-pack ./chart/ \
  --set nebariapp.enabled=true \
  --set nebariapp.hostname=my-pack.nebari.example.com \
  --set podinfo.ui.message="Custom greeting!"
```

## Local development (standalone, no Nebari)

```bash
helm dependency update ./chart/
helm install test-wrap ./chart/

# Access via port-forward
kubectl port-forward svc/test-wrap-podinfo 9898:9898
# Open http://localhost:9898
```

## Files

| File | Purpose |
|------|---------|
| `chart/Chart.yaml` | Helm chart metadata with podinfo dependency |
| `chart/values.yaml` | NebariApp config + podinfo overrides |
| `chart/templates/_helpers.tpl` | Name, label, and selector helpers |
| `chart/templates/nebariapp.yaml` | NebariApp CRD pointing to podinfo's service |
| `chart/templates/NOTES.txt` | Post-install instructions |

## Adapting for Your Own Chart

To wrap a different upstream chart:

1. Replace the `dependencies` entry in `Chart.yaml` with your chart
2. Update `nebariapp.service.port` to match the upstream service port
3. **Discover the upstream service name** (see below)
4. Update the service name helper in `_helpers.tpl` to match
5. Add any value overrides under the dependency's key in `values.yaml`
6. Run `helm dependency update ./chart/`

### Discovering the upstream service name

The NebariApp must reference the upstream chart's Service by its exact name.
Different charts use different naming conventions. **Always verify:**

```bash
helm template myrelease ./chart/ | grep "kind: Service" -A2
```

Common patterns:
- `<release>-<chartname>` (e.g., `myrelease-podinfo`) - most charts
- `<release>` only (e.g., `myrelease`) - when the release name contains the
  chart name, the fullname template collapses (common with charts like Superset)

Don't assume - always check with `helm template`.

### Wrapping charts with Bitnami sub-dependencies

Many popular Helm charts (Superset, Airflow, etc.) use Bitnami PostgreSQL and
Redis as sub-chart dependencies. Since August 2025, Bitnami images are behind a
paywall. The `bitnamilegacy/*` Docker Hub mirrors exist but are frozen and receive
no security updates.

If your upstream chart depends on Bitnami sub-charts:
- Override image repositories (e.g., `postgresql.image.repository: bitnamilegacy/postgresql`)
- Consider alternatives: [CloudNativePG](https://cloudnative-pg.io/) for PostgreSQL,
  [Valkey](https://valkey.io/) for Redis
- See [nebari-dev/nebari-operator#96](https://github.com/nebari-dev/nebari-operator/issues/96)
  for the longer-term platform solution

### Sub-chart values are static

Helm sub-chart values do not support template expressions (`{{ }}`). You cannot
dynamically generate configuration for the upstream app from your wrapper chart's
values.yaml. If you need templated config (e.g., generating Python OAuth config
from structured values), put it in example values files that deployers customize,
or create a ConfigMap template in your wrapper chart and mount it via the upstream
chart's volume support.

### App-native OAuth for RBAC

If your wrapped application needs role-based access control (not just
"authenticated or not"), see the [App-Native OAuth](../../docs/auth-flow.md#app-native-oauth)
section for how to wire operator-provisioned Keycloak credentials into your app.

See the [main README](../../README.md) for the full customization guide.
