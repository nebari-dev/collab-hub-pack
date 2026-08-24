# NebariApp CRD Reference

Complete field-by-field reference for the NebariApp custom resource.

**API Version:** `reconcilers.nebari.dev/v1`
**Kind:** `NebariApp`
**Source:** [nebari-operator/api/v1/nebariapp_types.go](https://github.com/nebari-dev/nebari-operator/blob/v0.1.0-alpha.19/api/v1/nebariapp_types.go)
**Operator version this doc tracks:** `v0.1.0-alpha.19`

## Full Example

```yaml
apiVersion: reconcilers.nebari.dev/v1
kind: NebariApp
metadata:
  name: my-pack
  namespace: my-pack
spec:
  hostname: my-pack.nebari.example.com
  serviceAccountName: my-pack
  service:
    name: my-pack
    port: 80
  routing:
    routes:
      - pathPrefix: /
        pathType: PathPrefix
    publicRoutes:
      - pathPrefix: /healthz
        pathType: Exact
    tls:
      enabled: true
    annotations:
      argocd.argoproj.io/tracking-id: my-pack
  auth:
    enabled: true
    provider: keycloak
    provisionClient: true
    enforceAtGateway: true
    forwardAccessToken: false
    # redirectURI defaults to /oauth2/callback - omit to use the operator default
    scopes:
      - openid
      - profile
      - email
    groups:
      - admin
  gateway: public
  landingPage:
    enabled: true
    displayName: My Pack
    description: A short description for the landing page card.
    icon: jupyter
    category: Development
    priority: 100
```

## spec

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `hostname` | string | Yes | - | FQDN where the app will be accessible. Used to generate the HTTPRoute and TLS certificate. Must match pattern `^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`. |
| `service` | [ServiceReference](#specservice) | Yes | - | The backend Kubernetes Service that receives traffic. |
| `routing` | [RoutingConfig](#specrouting) | No | - | Routing behavior including path rules, TLS, and HTTPRoute annotations. **Omitting `routing` disables operator-managed routing entirely** - the operator skips HTTPRoute creation and cleans up any existing HTTPRoute. TLS is also considered disabled in that case. |
| `auth` | [AuthConfig](#specauth) | No | - | Authentication/authorization configuration. |
| `gateway` | string | No | `"public"` | Which shared Gateway to use. Valid values: `public`, `internal`. |
| `serviceAccountName` | string | No | NebariApp name | Name of the ServiceAccount used by the app's pods. The operator scopes RBAC on the OIDC client Secret to this ServiceAccount, so only the app's pods can read its credentials. |
| `landingPage` | [LandingPageConfig](#speclandingpage) | No | - | Controls how this service appears on the Nebari landing page. |

## spec.service

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | Yes | - | Name of the Kubernetes Service. |
| `port` | int32 | Yes | - | Port number on the Service to route traffic to. Range: 1-65535. |
| `namespace` | string | No | NebariApp's namespace | Namespace of the Service. Allows referencing services in other namespaces for centralized service architectures. The operator has cluster-scoped read permission on Services. |

## spec.routing

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `routes` | [][RouteMatch](#routematch) | No | - | Path-based routing rules. If omitted, all traffic to the hostname is routed to the service. |
| `publicRoutes` | [][RouteMatch](#routematch) | No | - | Paths that bypass OIDC authentication. When `auth.enabled=true` and `auth.enforceAtGateway=true`, these paths are routed via a separate HTTPRoute that is not protected by the SecurityPolicy. Default `pathType` is `Exact` here (vs `PathPrefix` for `routes`) - safer for auth bypass. |
| `tls` | [RoutingTLSConfig](#specroutingtls) | No | - | TLS certificate management configuration. |
| `annotations` | map[string]string | No | - | Additional annotations merged onto the generated HTTPRoute. Useful for tools like ArgoCD that track resources via annotations (e.g. `argocd.argoproj.io/tracking-id`). Operator-managed annotations always take precedence. |

### RouteMatch

Used in both `spec.routing.routes[]` and `spec.routing.publicRoutes[]`.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `pathPrefix` | string | Yes | - | Path to match. Must start with `/`. Examples: `/`, `/api/v1`, `/dashboard`. |
| `pathType` | string | No | `PathPrefix` (in `routes`), `Exact` (in `publicRoutes`) | How the path is matched. Values: `PathPrefix`, `Exact`. |

### spec.routing.tls

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | *bool | No | `true` | Whether to provision a TLS certificate via cert-manager and configure an HTTPS listener on the Gateway. When `false`, only HTTP listeners are used. |

## spec.auth

> **Validation rule:** `forwardAccessToken: true` requires `enforceAtGateway: true`.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | bool | No | `false` | Whether to enforce OIDC authentication. |
| `provider` | string | No | `"keycloak"` | OIDC provider. Values: `keycloak`, `generic-oidc`. |
| `provisionClient` | *bool | No | `true` | Auto-provision an OIDC client in the provider. Only supported for `keycloak`. The operator creates the client and stores credentials in a Secret named `<name>-oidc-client`. The client ID follows the convention `<namespace>-<nebariapp-name>`. See [auth-flow.md](auth-flow.md#2-kubernetes-secret) for the full secret structure. |
| `enforceAtGateway` | *bool | No | `true` | Create an Envoy Gateway SecurityPolicy for gateway-level auth. When `false`, the operator provisions the client and Secret but does NOT create a SecurityPolicy - the app handles OAuth natively. See [auth-flow.md](auth-flow.md#app-native-oauth) for wiring guidance. |
| `forwardAccessToken` | *bool | No | `false` | When `enforceAtGateway: true`, forward the user's OAuth access token to the upstream service via the `Authorization: Bearer <token>` header. Use when the app needs to read the JWT itself (e.g., to inspect the `groups` claim for per-user authorization). Without this, the gateway only stores the token in an encrypted session cookie that the backend cannot decode. |
| `denyRedirect` | [][DenyRedirectHeader](#denyredirectheader) | No | - | Headers that, when matched, prevent the OIDC filter from redirecting to the IdP and instead return 401. Helps avoid PKCE race conditions when SPAs fire multiple parallel requests on page load. Only applies when `enforceAtGateway: true`. |
| `redirectURI` | string | No | `"/oauth2/callback"` | OAuth2 callback path. The full URL is `https://<hostname><redirectURI>`. |
| `clientSecretRef` | string | No | - | Name (string) of a Secret in the same namespace containing keys `client-id` and `client-secret`. **Note:** the spec field is a plain string (the Secret name), not the `{name, namespace}` object reference used by `status.clientSecretRef`. If omitted and `provisionClient` is true, the operator creates a Secret named `<nebariapp-name>-oidc-client` with keys: `client-id`, `client-secret`, and `issuer-url`. |
| `scopes` | []string | No | `["openid", "profile", "email"]` | OIDC scopes to request during authentication. |
| `groups` | []string | No | - | Groups that have access. When specified, only users in these groups are authorized. Case-sensitive. |
| `issuerURL` | string | No | - | OIDC issuer URL. Required when `provider=generic-oidc`, ignored for `keycloak`. Example: `https://accounts.google.com`. |
| `spaClient` | [SPAClientConfig](#specauthspaclient) | No | - | Provisions a public Keycloak client for browser-based PKCE flows (e.g., React apps using `keycloak-js`). Distinct from the confidential client used by gateway-enforced auth. |
| `deviceFlowClient` | [DeviceFlowClientConfig](#specauthdeviceflowclient) | No | - | Provisions a public Keycloak client for the OAuth2 Device Authorization Grant (RFC 8628), for CLI/native apps. |
| `keycloakConfig` | [KeycloakClientConfig](#specauthkeycloakconfig) | No | - | Keycloak-specific configuration: realm groups (with optional member assignments) and client-level protocol mappers. Only applied when `provider=keycloak` and `provisionClient=true`. |
| `tokenExchange` | [TokenExchangeConfig](#specauthtokenexchange) | No | - | OAuth 2.0 Token Exchange (RFC 8693). When enabled, other NebariApp clients in the same Keycloak realm can exchange their access tokens for tokens with this client's audience. Requires `KC_FEATURES=token-exchange` on the Keycloak server. |

### DenyRedirectHeader

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | Yes | - | Header name to match against. |
| `value` | string | Yes | - | Header value to match. |
| `type` | string | No | `Exact` | Match type. Values: `Exact`, `Prefix`, `Suffix`, `RegularExpression`. |

### spec.auth.spaClient

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | bool | No | `false` | Provision a public OIDC client for SPA use (PKCE, no client secret). The public client ID is written to the OIDC Secret as `spa-client-id`. |
| `clientId` | string | No | `<namespace>-<name>-spa` | Override the generated client ID. |

### spec.auth.deviceFlowClient

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | bool | No | `false` | Provision a public OIDC client configured for the Device Authorization Grant. The client ID is written to the OIDC Secret as `device-client-id`. |

### spec.auth.keycloakConfig

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `groups` | [][KeycloakGroup](#keycloakgroup) | No | - | Groups to ensure exist in the realm, with optional user membership assignments. |
| `protocolMappers` | [][KeycloakProtocolMapperConfig](#keycloakprotocolmapperconfig) | No | - | Client-level protocol mappers applied directly to the OIDC client. When specified, the operator's default mappers (e.g., group-membership) are not auto-created. |

#### KeycloakGroup

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | Yes | - | Group name to create in Keycloak. |
| `members` | []string | No | - | Keycloak usernames to add to the group. Membership sync is **additive-only** - users not in this list are not removed. |

#### KeycloakProtocolMapperConfig

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | Yes | - | Protocol mapper name. |
| `protocolMapper` | string | Yes | - | Mapper type identifier (e.g., `oidc-group-membership-mapper`). |
| `config` | map[string]string | No | - | Mapper configuration as arbitrary key-value pairs. Keys/values are mapper-type specific. |

### spec.auth.tokenExchange

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | bool | No | `false` | Enable authorization services on the Keycloak client and create policies allowing other NebariApp clients in the same realm to exchange tokens for this client's audience. |

## spec.landingPage

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | bool | No | `false` | Whether this service appears on the Nebari landing page. Set to `true` to opt in. |
| `displayName` | string | No (required when enabled) | - | Human-readable name on the landing page. Max 64 chars. |
| `description` | string | No | - | Supplementary text on the service card. Max 256 chars. |
| `icon` | string | No | - | Icon identifier or URL. Built-in icons: `jupyter`, `grafana`, `prometheus`, `keycloak`, `argocd`, `kubernetes`. |
| `category` | string | No | - | Group services together. Common categories: `Development`, `Monitoring`, `Platform`, `Data Science`. |
| `priority` | *int | No | `100` | Sort order within a category (lower = higher priority). Range: 0-1000. |
| `externalUrl` | string | No | `https://<hostname>` | Override the default URL. |
| `healthCheck` | [HealthCheckConfig](#speclandingpagehealthcheck) | No | - | Health-status monitoring for the service card. |

### spec.landingPage.healthCheck

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | bool | No | `false` | Whether health checks are performed. |
| `path` | string | No | `/health` | HTTP path to check. |
| `port` | *int32 | No | `spec.service.port` | Port to use for health checks. Range: 1-65535. |
| `intervalSeconds` | *int | No | `30` | Health check interval. Range: 10-300. |
| `timeoutSeconds` | *int | No | `5` | Per-check request timeout. Range: 1-30. |

## Status

The operator writes status conditions and several status fields for downstream consumers (the webapi watcher, ArgoCD, etc.).

### Conditions

| Condition | Description |
|-----------|-------------|
| `RoutingReady` | HTTPRoute has been created and the Gateway is routing traffic. |
| `TLSReady` | TLS certificate is provisioned and the HTTPS listener is configured. |
| `AuthReady` | SecurityPolicy is created and the OIDC client is available. Only set when `auth.enabled=true`. |
| `Ready` | Aggregate condition - all components are ready. |

### Condition Reasons

| Reason | Description |
|--------|-------------|
| `Available` | Resource is functioning correctly. |
| `Reconciling` | Reconciliation is in progress. |
| `ReconcileSuccess` | Reconciliation completed successfully. |
| `ValidationSuccess` | Validation passed successfully. |
| `Failed` | Reconciliation failed. |
| `NamespaceNotOptedIn` | Namespace is missing the `nebari.dev/managed=true` label. |
| `ServiceNotFound` | The referenced Service does not exist. |
| `SecretNotFound` | The referenced Secret does not exist. |
| `GatewayNotFound` | The target Gateway does not exist. |
| `GatewayListenerConflict` | The per-app Gateway listener conflicts with another NebariApp on the same hostname. |
| `CertificateNotReady` | The cert-manager Certificate is not yet ready. |

### Status Fields

| Field | Type | Description |
|-------|------|-------------|
| `observedGeneration` | int64 | Most recent `metadata.generation` observed by the controller. |
| `hostname` | string | Mirror of `spec.hostname` for easy reference. |
| `gatewayRef` | object `{name, namespace}` | The Gateway resource routing traffic to this app. |
| `clientSecretRef` | object `{name, namespace}` | The Secret containing OIDC client credentials. |
| `authConfigHash` | string | SHA-256 hash of the last successfully provisioned OIDC client config. Used to skip re-provisioning when the spec is unchanged. To force re-provisioning, set the `nebari.dev/force-reprovision` annotation; the operator removes it once the forced re-provision completes. |
| `serviceDiscovery` | object | URL-resolved view of `spec.landingPage` for the webapi/landing-page watcher. Includes `enabled`, `displayName`, `description`, `url`, `icon`, `category`, `priority`, `visibility`, `requiredGroups`. |

## Namespace Opt-In

The namespace containing the NebariApp must be labeled for the operator to process it:

```bash
kubectl label namespace my-pack nebari.dev/managed=true
```

Without this label, the NebariApp will show `NamespaceNotOptedIn` and no resources will be created.

## Deployment Patterns

The NebariApp resource can be included in your pack using any deployment method.

### Plain YAML

The NebariApp is just another manifest file alongside your Deployment and Service:

```yaml
# nebariapp.yaml
apiVersion: reconcilers.nebari.dev/v1
kind: NebariApp
metadata:
  name: my-pack
spec:
  hostname: my-pack.nebari.example.com
  service:
    name: my-pack
    port: 80
```

When deploying standalone (without Nebari), skip this file in your `kubectl apply`.

### Kustomize

Include the NebariApp in your base `kustomization.yaml` and use overlays to
patch environment-specific values like `hostname` and `auth`:

```yaml
# overlays/production/nebariapp-patch.yaml
apiVersion: reconcilers.nebari.dev/v1
kind: NebariApp
metadata:
  name: my-pack
spec:
  hostname: my-pack.nebari.example.com
  auth:
    enabled: true
    groups:
      - admin
```

### Helm

In Helm charts, you can make the NebariApp conditional so the chart works both
standalone and on Nebari:

```yaml
{{- if .Values.nebariapp.enabled }}
apiVersion: reconcilers.nebari.dev/v1
kind: NebariApp
metadata:
  name: {{ include "my-pack.fullname" . }}
  namespace: {{ .Release.Namespace }}
  labels:
    {{- include "my-pack.labels" . | nindent 4 }}
spec:
  hostname: {{ required "nebariapp.hostname is required" .Values.nebariapp.hostname }}
  service:
    name: {{ .Values.nebariapp.service.name | default (include "my-pack.fullname" .) }}
    port: {{ .Values.nebariapp.service.port | default 80 }}
  {{- with .Values.nebariapp.auth }}
  auth:
    enabled: {{ .enabled | default false }}
    provider: {{ .provider | default "keycloak" }}
    provisionClient: {{ .provisionClient | default true }}
    {{- with .scopes }}
    scopes:
      {{- toYaml . | nindent 6 }}
    {{- end }}
  {{- end }}
{{- end }}
```

The corresponding `values.yaml` section:

```yaml
nebariapp:
  enabled: false
  # hostname: my-pack.nebari.example.com  # Required when enabled
  service:
    name: ""   # Defaults to release fullname
    port: 80
  auth:
    enabled: false
    provider: keycloak
    provisionClient: true
    scopes:
      - openid
      - profile
      - email
  gateway: public
```
