# Example: Kustomize-Based Pack (Nginx)

A Nebari Software Pack using [Kustomize](https://kustomize.io/) overlays to
manage environment-specific configuration without Helm templating.

## What's in this example

```
kustomize-nginx/
  base/
    kustomization.yaml     # References all base resources
    deployment.yaml        # nginx Deployment
    service.yaml           # ClusterIP Service
    nebariapp.yaml         # NebariApp with placeholder hostname
  overlays/
    dev/
      kustomization.yaml   # Patches base for dev environment
      nebariapp-patch.yaml # Dev hostname, auth disabled
    production/
      kustomization.yaml   # Patches base for production
      nebariapp-patch.yaml # Production hostname, auth enabled with groups
```

## When to use this approach

- You need different configuration per environment (dev, staging, production)
- You prefer Kustomize's patch-based approach over Helm's templating
- You want to keep a readable base that overlays modify
- Your team already uses Kustomize for other deployments

## How it works

The `base/` directory contains complete, valid Kubernetes manifests including
the NebariApp CRD resource. Each overlay in `overlays/` patches the base
to customize values for a specific environment - typically the hostname and
auth settings on the NebariApp.

## Deploying to Nebari

### ArgoCD Application (recommended)

Point an ArgoCD Application at an overlay directory. ArgoCD auto-detects the
`kustomization.yaml` and renders the overlay before applying:

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
    path: examples/kustomize-nginx/overlays/production
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

### kubectl apply -k

```bash
# Dev environment (auth disabled)
kubectl apply -k examples/kustomize-nginx/overlays/dev/

# Production environment (auth enabled, restricted to groups)
kubectl apply -k examples/kustomize-nginx/overlays/production/
```

### Preview rendered manifests

```bash
# See what the dev overlay produces
kubectl kustomize examples/kustomize-nginx/overlays/dev/

# See what the production overlay produces
kubectl kustomize examples/kustomize-nginx/overlays/production/
```

## Local development (standalone, no Nebari)

Deploy only the base Deployment and Service:

```bash
kubectl apply -f examples/kustomize-nginx/base/deployment.yaml \
              -f examples/kustomize-nginx/base/service.yaml
kubectl port-forward svc/my-pack 8080:80
# Open http://localhost:8080
```

## Customizing

### Adding a new environment

1. Create a new overlay directory:
   ```bash
   mkdir -p overlays/staging
   ```

2. Create `overlays/staging/kustomization.yaml`:
   ```yaml
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization

   resources:
     - ../../base

   patches:
     - path: nebariapp-patch.yaml
   ```

3. Create `overlays/staging/nebariapp-patch.yaml` with your staging values:
   ```yaml
   apiVersion: reconcilers.nebari.dev/v1
   kind: NebariApp
   metadata:
     name: my-pack
   spec:
     hostname: my-pack.staging.nebari.example.com
     auth:
       enabled: true
   ```

### Patching other resources

You can patch any base resource. For example, to increase replicas in
production, add a deployment patch:

```yaml
# overlays/production/deployment-patch.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-pack
spec:
  replicas: 3
```

Then reference it in `overlays/production/kustomization.yaml`:

```yaml
patches:
  - path: nebariapp-patch.yaml
  - path: deployment-patch.yaml
```
