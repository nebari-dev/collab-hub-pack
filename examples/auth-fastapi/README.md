# Example 4: Auth-Aware Helm Pack (FastAPI)

A custom FastAPI application that reads the authenticated user's identity from
the IdToken cookie set by Envoy Gateway after Keycloak OIDC authentication.

## What This Example Shows

- Building a custom container image for a Nebari pack
- Reading the IdToken cookie to extract user claims (username, email, groups)
- How Envoy Gateway handles the OIDC flow before requests reach your app
- Full chart structure with Deployment, Service, and NebariApp

## How Authentication Works

When deployed on Nebari with auth enabled:

1. User visits `my-pack.nebari.example.com`
2. Envoy Gateway intercepts the request (no valid session cookie)
3. User is redirected to Keycloak for login
4. After login, Keycloak redirects back with an authorization code
5. Envoy Gateway exchanges the code for tokens and sets an `IdToken-*` cookie
6. The request (now with the cookie) is forwarded to the FastAPI app
7. The app decodes the JWT from the cookie and displays user info

Your app never handles the login flow - Envoy Gateway does it all. You just
read the resulting cookie.

## Deploying to Nebari

### ArgoCD Application (recommended)

The container image is automatically built and published to GHCR by CI when
changes are pushed to `examples/auth-fastapi/app/` or the `Dockerfile`. Create
an ArgoCD Application:

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
    path: examples/auth-fastapi/chart
    helm:
      valuesObject:
        nebariapp:
          enabled: true
          hostname: my-pack.nebari.example.com
          auth:
            enabled: true
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

### Helm install

```bash
helm install my-pack ./chart/ \
  --set nebariapp.enabled=true \
  --set nebariapp.hostname=my-pack.nebari.example.com \
  --set nebariapp.auth.enabled=true
```

## Local development

The container image is pre-built and published to GHCR by CI. You can run it
locally without building:

```bash
docker run -p 8000:8000 ghcr.io/nebari-dev/software-pack-template/auth-fastapi-example:latest
# Open http://localhost:8000 - shows "Not Authenticated" page
```

To build locally from the Dockerfile instead:

```bash
cd examples/auth-fastapi
docker build -t my-pack-fastapi:latest .
docker run -p 8000:8000 my-pack-fastapi:latest
```

## Code Walkthrough

### `app/main.py`

The key function is `get_id_token()` which extracts the JWT from Envoy Gateway's
`IdToken-<suffix>` cookie. The JWT payload is base64-decoded (no signature
verification needed since Envoy Gateway already verified it) to extract claims:

- `preferred_username` - the Keycloak username
- `email` - user's email address
- `name` - display name
- `groups` - Keycloak group memberships
- `realm_access.roles` - Keycloak realm roles

### `app/templates/index.html`

A simple Jinja2 template that renders user information when authenticated, or
an explanatory message when no IdToken is present.

## Files

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI application reading IdToken cookies |
| `app/requirements.txt` | Python dependencies |
| `app/templates/index.html` | HTML template for user info display |
| `Dockerfile` | Multi-stage build for the FastAPI image |
| `chart/Chart.yaml` | Helm chart metadata |
| `chart/values.yaml` | Default config with auth enabled |
| `chart/templates/_helpers.tpl` | Name, label, and selector helpers |
| `chart/templates/nebariapp.yaml` | NebariApp CRD with auth configuration |
| `chart/templates/deployment.yaml` | Kubernetes Deployment |
| `chart/templates/service.yaml` | ClusterIP Service |
| `chart/templates/NOTES.txt` | Post-install instructions |

See the [main README](../../README.md) for the full customization guide.
