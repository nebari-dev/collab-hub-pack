# Frames In Collab Hub

Frames are exposed by the Collab Hub API as a workspace-scoped contract for saved
Markdown context, suggestions, and active-frame selection. The canonical REST
prefix is `/v1`; unversioned aliases exist only for prototype compatibility and
are hidden from OpenAPI.

## Authentication

Frames routes use the same Hub API auth boundary as the rest of Collab Hub:

- a verified `IdToken-*` cookie,
- `Authorization: Bearer <token>` for native clients when bearer verification is
  configured,
- or `DEV_AUTH_USER` for local development only when unsafe auth is explicitly
  enabled with `FRAMES_UNSAFE_AUTH_ENABLED=true` and `DEV_AUTH_ENABLED=true`.

If multiple identities are present, the `IdToken-*` cookie wins over the bearer
header, and either token wins over `DEV_AUTH_USER`. This matches the Apollo
Desktop direction where the native shell owns the OAuth token and proxies Hub
requests with an `Authorization` header.

## Access model

Owners, readers, and `created_by` hold **identity strings**, and which claim
that string comes from is a per-deployment setting
(`frames.auth.identityClaim`; see
[frames-operations.md](frames-operations.md#acl-identity-framesauthidentityclaim)).
Deployments pinned to the OIDC `sub` reject owner/reader grants that name an
email address with a **422** rather than storing a grant that would match no
one; other subject formats are opaque and accepted. Display name and email are
carried alongside the caller's identity for presentation only and are never
principals.

Every Frame is an owned, access-controlled resource. Four concepts, evaluated
in order — `published` is the master gate:

- **owners** (`list[str]`, ≥1, ordered) — all owners are equal: read, modify,
  delete, publish, and manage the owner/reader lists, **published or not**.
  Management is **tenant-scoped** — an owner manages a frame only from its own
  `(org_id, workspace_id)`. (`public` grants cross-tenant *read* by id, never
  cross-tenant mutation.) `created_by` records the immutable original creator
  (an audit fact only).
- **published** (`bool`, default `false`) — until `true`, only owners have any
  access, regardless of visibility or readers.
- **visibility** (`private` | `internal` | `public`, default `private`) — the
  audience *once published*:
  - `private` — owners, **plus anyone on the `readers` list**.
  - `internal` — the whole of the frame's own `(org_id, workspace_id)`.
  - `public` — any authenticated user in **any** org/workspace (multi-tenant).
- **readers** (`list[str]`) — an ACL-lite grant that **expands a `private`
  frame** to the listed users. Inert for `internal`/`public`. **Invariant:** a
  non-empty `readers` list ⟹ `visibility == private` (enforced in the store):
  writing readers forces `private`; setting `internal`/`public` clears `readers`.

`can_read` owns the tenant check, so reads have **no scope pre-gate**: a `public`
frame is reachable by id cross-tenant, while `internal`/`private` stay scoped.
Reads that fail return **404** (existence is never leaked). Mutations apply
**read-then-manage** ordering: `can_read` first → **404** if the caller can't even
see the frame, then `can_manage` (owner **and** same tenant) → **403** if they can
see it but don't own it in-tenant. So a non-owner who can't read a `private` frame
gets 404, a reader/`public`/`internal` viewer who isn't an owner gets 403, and
cross-tenant management is denied even to an owner.

Single-frame `GET` is a cross-tenant lookup by id gated by `can_read`. The
`GET /frames` **list stays scoped to the caller's tenant** — cross-tenant
`public` frames are not listed (discovery is out of scope); `public` only
affects single-frame `GET`. `GET /frames/{id}/history` likewise reads
cross-tenant for `public` frames and queries history rows under the **frame's**
own tenant. The MCP `list_frames` tool mirrors REST (tenant-scoped); `get_frame`
and the `frame://` resource reach `public` frames cross-tenant by id.

A Frame may stay in a user's active set only while they can still read it. Any
narrowing mutation (unpublish, `public → internal/private`, `internal → private`,
adding/removing a reader, removing an owner) prunes the Frame from the active
sets of users who just lost access — including **cross-tenant** holders of a
`public` frame (the holder lookup is global).

## REST Contract

`GET /v1/frames?name=voice&tag=brand&owner=alice&visibility=internal&published=true`

Lists readable Frame metadata. Markdown bodies are omitted. Optional filters are
case-insensitive `name`, repeated `tag`, `owner` (membership), `visibility`,
`published`, and `group_id`.

`GET /v1/frames/{frame_id}`

Returns one Frame including its body, when the caller can read it.

`POST /v1/frames`

Creates a Frame owned by the authenticated caller. `description`, `visibility`,
and `owners` are optional; `owners` seeds co-owners and the caller is always
force-added. `owners`, `readers`, and `published` are otherwise managed only via
the dedicated endpoints below.

```json
{
  "name": "Brand Voice",
  "description": "How we sound.",
  "visibility": "private",
  "tags": ["brand", "sales"],
  "owners": ["teammate@example.com"],
  "body": "# Brand voice\nUse direct language."
}
```

`PUT /v1/frames/{frame_id}`

Updates mutable fields (name, description, visibility, tags, body). Owners only.
Preserves owners and published. Setting `visibility` to `internal`/`public`
**clears `readers`** (reader/visibility invariant).

`DELETE /v1/frames/{frame_id}`

Deletes a Frame and its Suggestions. Owners only. Deleted Frame ids are removed
from active-frame state and pruned from any Frame Groups they belong to; a group
whose only member was the deleted Frame is deleted too (see Frame Groups below).

`GET|PUT|POST /v1/frames/{frame_id}/owners` · `DELETE /v1/frames/{frame_id}/owners/{email}`

Manage the owner list. `GET` requires read access; the rest require ownership.
`PUT` replaces the list (≥1). `POST` adds `{ "email": "..." }`. Removing the
**last owner** is refused with `409` (`code: last_owner`).

`GET|PUT|POST /v1/frames/{frame_id}/readers` · `DELETE /v1/frames/{frame_id}/readers/{email}`

Manage the reader list (the ACL-lite grant that expands a **private** frame).
All require ownership. `PUT` replaces, `POST` adds `{ "email": "..." }`. Writing
a non-empty reader list **forces `visibility=private`** (invariant). Every reader
change reconciles active sets — both because removing a reader drops that user
and because the forced flip to `private` removes any previous tenant-wide/public
audience.

`POST /v1/frames/{frame_id}/publish` · `POST /v1/frames/{frame_id}/unpublish`

Toggle the published gate. Owners only. `unpublish` reconciles active sets.

`POST /v1/frames/{frame_id}/suggestions`

Creates a Suggestion. Any user who can read the Frame may submit.

`GET /v1/frames/{frame_id}/suggestions?status=open`

Lists Suggestions for a Frame. `status` may be `open` or `closed`.

`POST /v1/frames/{frame_id}/suggestions/{suggestion_id}/close`

Closes a Suggestion. Only a Frame owner or the Suggestion submitter may close it.

`GET /v1/frames/{frame_id}/history?limit=<1..200, default 50>&before=<cursor>`

Returns a frame's change history, newest-first. Requires read access (`can_read`;
unreadable → `404`). Each entry is `{id, event, actor, detail, created_at}` where
`actor` is the Hub user who made the change and `detail` is a compact, body-free
summary (changed scalar fields as `{from, to}`; list changes as `{added, removed}`).
Events: `created`, `updated`, `deleted`, `owners_changed`, `readers_changed`,
`visibility_changed`, `published`, `unpublished`. Pagination is cursor-based: the
opaque `next` cursor encodes the last row's `(created_at, id)`; pass it back as
`before` to fetch the next page, and `next` is `null` once exhausted. A malformed
cursor returns `400 invalid_cursor`. History is an event log, not document
versioning — the Frame body is never recorded. Recorded rows persist after a
Frame is deleted (the deletion itself is recorded), but because the endpoint
authorizes against the live Frame, a deleted Frame's history is **not** readable
through this endpoint — the durable rows back future admin/audit tooling only.
When no shared `frames.postgres` is configured the endpoint returns `503 history_unavailable`.

`GET /v1/active-frames`

Returns the authenticated caller's stored active Frame ids.

`PUT /v1/active-frames`

Replaces the authenticated caller's active Frame ids. Each id must be **readable
by the caller** (`can_read`) — which includes a cross-tenant `public` frame; an
unreadable id is rejected with `404`. Duplicates are removed while preserving
order. A Frame stays active only while the caller can still read it — narrowing
mutations reconcile it out of every affected user's active set (across tenants
for `public` frames).

## Frame Groups

A **Frame Group** bundles one or more Frames under its own owners and
visibility. Unlike a Frame it has **no document body**, so it is a pure
relational record — it does *not* use the Frame blob store.

### Group access model

- **owners** (`list[str]`, ≥1, ordered) — may read, update, delete, and manage
  the owner list and membership. Management is **tenant-scoped** (owner and same
  `(org_id, workspace_id)`), mirroring frames: a `public` group reads
  cross-tenant but is never managed cross-tenant. `created_by` records the
  original creator.
- **visibility** (`private` | `internal` | `public`, default `private`) — the
  group's *own* stored visibility.
- **all_published** (`bool`, derived, never stored) — computed at read time by
  AND-ing the `published` flag of every member Frame.
- **effective_visibility** (`Visibility`, derived, never stored) — the
  **least-broad** of the group's own visibility and every member's visibility
  (`private < internal < public`). A group is never more visible than its
  narrowest member — e.g. a `public` group containing a `private` member is
  effectively `private`. This is the visibility analogue of the all-published cap.

Read rule (PRD §2.3): owners always read a group (from any tenant). For
non-owners, a group is visible only when **all** member Frames are published;
any unpublished member keeps the group owner-only. Once all members are
published, the group's **`effective_visibility`** decides the audience: `public`
→ any authenticated user in **any** tenant, `internal` → same-tenant users,
`private` → owners only. Single-group `GET` is gated by
`can_read_group` with no scope pre-gate (so `public` groups read cross-tenant);
the `GET /v1/frame-groups` list stays scoped to the caller's tenant.
`GET /v1/frame-groups/{id}/history` reads cross-tenant for `public` groups and
queries rows under the **group's** own tenant. Failed reads return **404**;
mutations apply the same read-then-manage ordering as frames — `can_read_group`
→ **404** if the group isn't visible, then tenant-scoped `can_manage_group` →
**403** if visible but not owned in-tenant.

### Membership projection (`group_ids`) — deferred

The reciprocal `group_ids` projection on Frame reads is **not** populated for
A3: `FrameMetadata.group_ids` always returns `[]`. Membership lives only on the
group row (`frame_ids`); populating the reverse projection would require a
per-Frame group lookup plus an all-published/access computation on the hot Frame
read path. Membership is instead queryable from the group side via
`GET /v1/frame-groups` and the `GET /v1/frames?group_id=` filter — the latter
resolves the group's stored membership and only returns members when the caller
**can read that group** (an unknown, cross-scope, or unreadable group yields an
empty result).

### REST contract

`GET /v1/frame-groups?name=&visibility=&published=` — lists readable groups
(`published` filters on the derived `all_published`).

`POST /v1/frame-groups` — creates a group. Body
`{name, description?, visibility?, frame_ids[≥1]}`; every `frame_id` must be
**readable** by the caller (owner, reader on a private Frame, or any
authenticated user on a published `internal`/`public` Frame) — ownership of the
member Frame is not required, else `403`/`404`. The creator becomes the sole
owner of the group. Returns `201`.

`GET /v1/frame-groups/{id}` — group detail including the computed `all_published`.

`PUT /v1/frame-groups/{id}` — updates `name`, `description`, `visibility`. Owners only.

`DELETE /v1/frame-groups/{id}` — deletes the group only; member Frames are never
touched. Owners only.

**Membership lifecycle — non-destructive on narrowing, cascade on deletion.**
Group membership is **not** pruned when a member Frame later becomes unreadable
to the group's owners (unpublish, visibility change, or reader/owner changes on
either the Frame or the group). The member is retained; clients render an
inaccessible member as inaccessible ("Frame you can't access"), and if access is
later restored — e.g. the Frame is republished — the member recovers
automatically with no reconciliation. There is deliberately **no** "a group
never contains an unreadable member" invariant.

Only **actual Frame deletion** mutates membership: the deleted Frame id is
pruned from every group it belonged to, looked up **globally** across tenants
(not scoped to the Frame's own org/workspace, since a readable-not-owned member
— including a cross-tenant `public` Frame — can live in a group outside the
Frame's tenant). A group left with no members is deleted as well, preserving the
≥1-member invariant rather than leaving a stale id that would silently force the
group owner-only. This is system reconciliation — it runs regardless of who owns
the group — and is recorded in group history as `frame_removed`
(`reason: member_frame_deleted`), or `deleted` (`reason: last_member_deleted`)
for the sole-member cascade.

The **actor** recorded for these deletion-driven group-history events is the
Frame owner who triggered the deletion, who may belong to a **different tenant**
than the group. This is a conscious, audit-correct choice: history records the
identity that actually caused the change rather than a synthetic system actor.

`POST /v1/frame-groups/{id}/frames` `{frame_id}` — adds a member; the Frame must
be **readable** by the caller (same rule as creation; ownership of the group is
still required to call this endpoint at all). `DELETE
/v1/frame-groups/{id}/frames/{frame_id}` removes a member, refused with `409`
(`code: last_frame`) if it would empty the group.

`GET|PUT|POST /v1/frame-groups/{id}/owners` · `DELETE …/owners/{email}` — same
contract as Frame owners, including last-owner → `409 last_owner`.

`GET /v1/frame-groups/{id}/history` — group change history, newest-first, with
the same cursor contract as Frame history and gated by group read access. Rows
are stored in the shared history table with `entity_type='group'`; events are
`created`, `updated`, `deleted`, `owners_changed`, `frame_added`, `frame_removed`.

All group endpoints return `503 groups_unavailable` when no shared
`frames.postgres` is configured; an unknown group id returns `404 group_not_found`.

## MCP Contract

The original Frames MCP contract is mounted by the Collab Hub API at `/mcp`. It uses
the same auth context as the REST API, so tools are scoped to the authenticated
caller's org and workspace and honor the same `can_read` access model:
`list_frames` returns only readable Frames, `get_frame` raises not-found for
Frames the caller cannot read, and `get_active_frames` skips any stale id the
caller can no longer read.

Tools:

- `list_frames(name?: str, tags?: list[str], owner?: str)`
- `get_frame(id: str)`
- `get_active_frames(ids?: list[str])`

When `ids` is provided, `get_active_frames` returns those Frame bodies. When
`ids` is omitted, it falls back to the authenticated caller's stored active
Frame ids.

Resources:

- `frame://{frame_id}`

## User Directory

Apollo clients can search Hub users and groups through Collab Hub when the user
directory is configured. These routes require the same authenticated Hub request
as Frames routes. Any authenticated Hub user may search so they can choose users
and groups when sharing Frames.

Directory lookup is intentionally realm-global: results are not filtered by the
caller's `org_id` or `workspace_id`. This matches the current Frame sharing
picker requirement, where authenticated Hub users need to discover share
recipients. Deployments that share one Keycloak realm across mutually untrusted
organizations should account for that visibility before enabling the directory.

`GET /v1/user-directory/users?q=alice&limit=20`

Returns user picker records from the configured Hub directory:

```json
[
  {
    "id": "user-1",
    "username": "alice",
    "email": "alice@example.com",
    "first_name": "Alice",
    "last_name": "Ng",
    "enabled": true
  }
]
```

`GET /v1/user-directory/groups?q=sales&limit=20`

Returns group picker records:

```json
[
  {
    "id": "group-1",
    "name": "sales",
    "path": "/sales"
  }
]
```

`q` is optional. `limit` defaults to `50` and may be between `1` and `200`;
there is no offset/page cursor in this first version. When the directory is not
configured or Keycloak is unavailable, Collab Hub returns `503` with
`code: user_directory_unavailable`.

## Error Shape

```json
{
  "error": {
    "code": "forbidden",
    "message": "Only an owner may manage a Frame"
  }
}
```

Validation errors use `code: validation_error` and include `details`.
