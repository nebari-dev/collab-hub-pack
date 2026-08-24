# Example: Vanilla YAML (Plain Kubernetes Manifests)

The simplest possible Nebari Software Pack - plain Kubernetes manifests with no
tooling dependencies beyond `kubectl`.

## What's in this example

| File | Purpose |
|------|---------|
| `deployment.yaml` | nginx Deployment |
| `service.yaml` | ClusterIP Service |
| `nebariapp.yaml` | NebariApp CRD resource for Nebari integration |

## When to use this approach

- You want the lowest barrier to entry
- Your configuration is simple and doesn't vary across environments
- You don't need Helm templating or Kustomize overlays
- You want to commit manifests that are readable without any tooling

## How it works

The NebariApp resource is just another YAML file alongside your Deployment and
Service. No templating, no conditionals. The nebari-operator watches for
NebariApp resources and auto-configures routing, TLS, and authentication.

## Deploying to Nebari

### ArgoCD Application (recommended)

Point an ArgoCD Application at this directory using the `directory` source type:

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
    path: examples/vanilla-yaml
    directory:
      recurse: false
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

Before deploying, edit `nebariapp.yaml` and set `hostname` to your actual
hostname:

```yaml
spec:
  hostname: my-pack.your-nebari-domain.com
```

### kubectl apply

1. Edit `nebariapp.yaml` and set `hostname` to your actual hostname.

2. Apply all manifests:
   ```bash
   kubectl apply -f examples/vanilla-yaml/
   ```

3. Verify the NebariApp is ready:
   ```bash
   kubectl get nebariapp my-pack
   kubectl describe nebariapp my-pack
   ```

## Local development (standalone, no Nebari)

Deploy only the Deployment and Service (skip the NebariApp since the CRD
doesn't exist outside Nebari):

```bash
kubectl apply -f deployment.yaml -f service.yaml
kubectl port-forward svc/my-pack 8080:80
# Open http://localhost:8080
```

## Enabling authentication

Edit `nebariapp.yaml` and set `auth.enabled` to `true`:

```yaml
auth:
  enabled: true
```

To restrict access to specific groups, add a `groups` list:

```yaml
auth:
  enabled: true
  groups:
    - admin
    - data-science-team
```

## Limitations

- **No variable substitution** - Hostnames and other values must be edited
  directly in the YAML files. For environment-specific configuration, consider
  the [Kustomize example](../kustomize-nginx/) instead.
- **No conditional rendering** - The NebariApp manifest is always present. When
  deploying standalone, simply skip that file (don't `kubectl apply` it).
