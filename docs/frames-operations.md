# Frames Operations

## Observability

The Collab Hub API emits structured access logs for every request and audit logs for
Frame mutations. The request ID comes from `x-request-id` when supplied;
otherwise the API generates one and returns it on the response.

Prometheus metrics are exposed at `/metrics`:

- `frames_server_http_requests_total{method,path,status}`
- `frames_server_http_request_duration_seconds{method,path}`
- `frames_server_audit_events_total{action}`

Audit actions currently include:

- `frame_create`
- `frame_update`
- `frame_delete`
- `suggestion_create`
- `suggestion_close`
- `active_frames_update`
- `group_create`
- `group_update`
- `group_delete`
- `group_owners_update`
- `group_frame_add`
- `group_frame_remove`

## CORS

CORS is configured under `COLLAB_HUB_API__SECURITY__CORS`. The defaults allow
`Authorization` and `Content-Type` headers so Apollo Desktop and browser clients
can use the Hub API auth boundary. Set `allow_credentials=true` only when
`allowed_origins` is a concrete origin list, not a wildcard.

Example:

```yaml
security:
  cors:
    allowed_origins:
      - https://desktop.example.com
    allowed_headers:
      - Authorization
      - Content-Type
    allow_credentials: true
```

## Storage And Active State

Frame content uses the configured Frames store:

- `local` for local development and single-pod installs,
- `s3` for shared production-style storage.

### One shared Postgres for the relational features

History and Frame Groups are **required relational features** with **no
per-feature `disabled` toggle**. They — and, by fallback, active-state — ride a
**single shared Postgres URL** configured at
`COLLAB_HUB_API__FRAMES__POSTGRES__URL` / `__AUTO_MIGRATE` (Helm:
`frames.postgres`). Setting that one URL lights up active-state, history, and
groups together; leaving it unset is the *only* off state. This replaces the
earlier per-feature `disabled`-by-default backends, which let a deploy enable
one feature and silently leave the others off.

- **Active-frame selection** (`frames.activeState.backend`: `disabled` (default),
  `memory`, `postgres`) is the one feature that may still be fully disabled.
  With `postgres` and no `activeState.postgres.url` of its own, it **falls back
  to the shared `frames.postgres.url`**.
- **Change history** (`GET /v1/frames/{id}/history`) persists to
  `frames_server_history` (created when `frames.postgres.auto_migrate` is set).
  In-memory is available only as a test/dev override
  (`COLLAB_HUB_API__FRAMES__HISTORY__BACKEND=memory`). With no shared DB the
  history endpoints return `503` and recording is a no-op. History writes are
  best-effort relative to the mutation: a failed write is logged and counted
  (`frames_server_history_write_failures_total`) but never fails or rolls back
  the mutation that already succeeded.
- **Frame Groups** (`/v1/frame-groups`) persist to `frames_server_groups`
  (relational; groups have no document body). Same model: `…__FRAMES__GROUPS__BACKEND=memory`
  test override; with no shared DB **every** group endpoint returns
  `503 groups_unavailable` (no silent empty/404 fallback). Group change events
  reuse the history store with `entity_type='group'`. Frame-side touches of the
  group store (delete-time reconciliation, the `?group_id=` list filter) tolerate
  the unavailable store so a missing groups DB never breaks frame operations.

## User Directory

Collab Hub can be configured with Keycloak user-directory access so Apollo clients can
look up users and groups for Frame sharing. This is disabled by default. A Hub
admin must create or reuse a confidential Keycloak client with service accounts
enabled and grant its service account these `realm-management` roles:

- `query-users`
- `view-users`
- `query-groups`

The directory API is a realm-global picker by design. It requires an
authenticated Hub request, but it does not filter results by the caller's
`org_id` or `workspace_id`. Do not enable it for a shared Keycloak realm unless
all authenticated users are allowed to discover the realm's user and group names
for sharing workflows.

Store the client credentials in a Kubernetes Secret in the Collab Hub namespace:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: nexus-user-directory
type: Opaque
stringData:
  client-id: nexus-user-directory
  client-secret: "<client secret>"
```

Then point the chart at that Secret:

```yaml
userDirectory:
  enabled: true
  provider: keycloak
  keycloak:
    issuerUrl: https://keycloak.example.com/realms/nebari
    # Optional; defaults to <issuerUrl>/protocol/openid-connect/token.
    tokenUrl: https://keycloak.example.com/realms/nebari/protocol/openid-connect/token
    adminApiBaseUrl: https://keycloak.example.com/admin/realms/nebari
    existingSecret: nexus-user-directory
```

## User Directory

Collab Hub can be configured with Keycloak user-directory access so Apollo clients can
look up users and groups for Frame sharing. This is disabled by default. A Hub
admin must create or reuse a confidential Keycloak client with service accounts
enabled and grant its service account these `realm-management` roles:

- `query-users`
- `view-users`
- `query-groups`

The directory API is a realm-global picker by design. It requires an
authenticated Hub request, but it does not filter results by the caller's
`org_id` or `workspace_id`. Do not enable it for a shared Keycloak realm unless
all authenticated users are allowed to discover the realm's user and group names
for sharing workflows.

Store the client credentials in a Kubernetes Secret in the Collab Hub namespace:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: nexus-user-directory
type: Opaque
stringData:
  client-id: nexus-user-directory
  client-secret: "<client secret>"
```

Then point the chart at that Secret:

```yaml
userDirectory:
  enabled: true
  provider: keycloak
  keycloak:
    issuerUrl: https://keycloak.example.com/realms/nebari
    # Optional; defaults to <issuerUrl>/protocol/openid-connect/token.
    tokenUrl: https://keycloak.example.com/realms/nebari/protocol/openid-connect/token
    adminApiBaseUrl: https://keycloak.example.com/admin/realms/nebari
    existingSecret: nexus-user-directory
```

## Smoke Validation

Run the API tests before merging Frames changes:

```bash
PYTHONPATH=api/src pytest api/tests
```

For a deployed or locally running Collab Hub API, create a Frame through REST, then
verify the MCP contract with:

```bash
python scripts/smoke_frames_mcp.py \
  --base-url http://127.0.0.1:8000 \
  --frame-id 0123456789abcdef0123456789abcdef \
  --expected-body "# Body"
```

The default smoke helper sends an unsigned test `IdToken-*` cookie. Use that
only against a local/test server started with `FRAMES_UNSAFE_AUTH_ENABLED=true`
and `FRAMES_IDTOKEN_ALLOW_UNSIGNED=true`.

For shared authenticated environments, pass a real Hub bearer token:

```bash
python scripts/smoke_frames_mcp.py \
  --base-url https://hub.example.com \
  --frame-id 0123456789abcdef0123456789abcdef \
  --expected-body "# Body" \
  --bearer-token "${HUB_ID_TOKEN}"
```

Add `--require-stored-active` after setting `/v1/active-frames` for the same
authenticated user.

The pack also includes Collab Hub deployment smokes for the operational cases that
matter for Frames:

- `scripts/smoke_frames_http.py` validates REST, MCP, metrics, and optional active state.
- `scripts/smoke_frames_observability.sh` checks metrics plus structured request/audit logs.
- `scripts/smoke_frames_minio_s3.sh` installs MinIO and validates S3-backed Frame content.
- `scripts/smoke_frames_postgres_active_state.sh` installs Postgres and validates stored active-frame state.
