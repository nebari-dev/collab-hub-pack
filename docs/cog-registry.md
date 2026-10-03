# Cog registry

The hub indexes Cogs from OCI registries. This page covers the `cogs:` block
of the Helm chart and the matching application settings: which registries are
read (**sources**), how the hub authenticates to them (**credentials**), how a
private CA is trusted (**CA bundle**), how often the index is rebuilt
(**indexer**), and whether the hub serves pulls itself
([**pulls through the Hub**](#pulls-through-the-hub)). The adapters themselves — what "Harbor" and "static" mean and
why the registry stays swappable — are documented in
`api/src/collab_hub_api/cogs/registry.py`; the configuration surface is
[#87](https://github.com/nebari-dev/collab-hub-pack/issues/87).

> **Status.** This page describes the configuration surface, the chart
> wiring, and the [read API](#read-api) clients discover Cogs through. The
> indexer sweep ([#84]) consumes the block at runtime and the read API
> ([#85]) serves what it indexed; the webhook receiver ([#86]) lands
> separately. The API validates the block at startup and refuses to start on
> the errors described below.

[#84]: https://github.com/nebari-dev/collab-hub-pack/issues/84
[#85]: https://github.com/nebari-dev/collab-hub-pack/issues/85
[#86]: https://github.com/nebari-dev/collab-hub-pack/issues/86

Two rules shape everything below:

- **The chart and the values must land together.** `values.schema.json` has
  `additionalProperties: false` everywhere, so a values file that names a
  `cogs` key the installed chart does not know fails at `helm upgrade`, and a
  chart that expects one the values do not set fails at startup. Upgrade the
  chart and the values in one release.
- **Secrets are never rendered.** The source list reaches the API as one JSON
  environment variable; passwords and webhook secrets do not. They are mounted
  from Kubernetes Secrets you create, under variable names the chart derives
  from the source id, and the API reads those variables at startup.

## Sources

`cogs.registry.sources` is an ordered list. Each entry has an `id`, a `kind`,
and the external `url`; the rest depends on the kind.

| Key | Applies to | Meaning |
| --- | --- | --- |
| `id` | all | Stable identifier stored with every indexed row. Lowercase `[a-z0-9._-]`, at most 64 characters. Renaming it orphans the rows indexed under the old name. |
| `kind` | all | `harbor` or `static`. |
| `url` | all | External registry URL. Its host becomes the identity `<host>/<repo>@<digest>`; never an in-cluster address. |
| `apiUrl` | harbor | In-cluster URL for Harbor's REST API and OCI endpoints, so sweeps stay inside the cluster. Identity still derives from `url`. |
| `tokenUrl` | all | In-cluster bearer-token endpoint for the OCI client, when the registry's `WWW-Authenticate` realm points at an external host the pod cannot reach. |
| `projects` | harbor | Harbor projects to enumerate. At least one. |
| `repositories` | static | OCI repository paths to index (`project/name`). |
| `indexUrl` | static | URL of a `catalog.v1.json` listing repositories. A static source needs `repositories`, `indexUrl`, or both. |
| `caBundlePath` | all | Per-source CA bundle path inside the pod. Defaults to the shared bundle below when that is configured. |
| `requestTimeoutSeconds` | all | HTTP timeout, default 10, at most 60. |
| `credentials` | all | Where the robot/service credential lives — see the next section. Omit for anonymous access. |
| `webhook` | harbor | Where the shared webhook secret lives. A static source has no webhook and the render refuses the block. |

Field mismatches are refused rather than ignored: `repositories` on a Harbor
source, or `projects` on a static one, almost always means the kind is
mistyped, and the source would otherwise start and index nothing. The chart
fails the render (`templates/cogs-validations.yaml`) and the API fails
startup on the same rules, so a values mistake surfaces at `helm template`
rather than as a crash-looping pod.

## Credentials

Each source may name a Secret for its credential and, for Harbor, another for
its webhook:

```yaml
credentials:
  existingSecret: collab-hub-harbor-robot   # required when the block is present
  usernameKey: username                     # default
  passwordKey: password                     # default
webhook:
  existingSecret: collab-hub-harbor-webhook
  secretKey: secret                         # default
```

The chart mounts those keys as environment variables named from the source id:

```
COLLAB_HUB_COGS_SOURCE_<ID>_USERNAME
COLLAB_HUB_COGS_SOURCE_<ID>_PASSWORD
COLLAB_HUB_COGS_SOURCE_<ID>_WEBHOOK_SECRET
```

`<ID>` is the id upper-cased with every character outside `[A-Z0-9]` replaced
by `_`: `harbor-main` becomes `HARBOR_MAIN`, `public.mirror` becomes
`PUBLIC_MIRROR`. Two ids that collapse to the same `<ID>` fail the render. The
JSON source list carries only these *names* (`credentials.username_env`,
`credentials.password_env`, `webhook_secret_env`); at startup the API replaces
each name with the variable's value and then validates the source as if the
value had been given directly. A name whose variable is unset or empty — the
Secret is missing, or its key is spelled differently from `passwordKey` — is a
startup error that names the variable, so the fix is a values change, not a
hunt through 401s on the first sweep. A username without a password (or the
reverse) is refused for the same reason: it is the shape a half-mounted
Secret produces.

Surrounding whitespace is removed from every resolved value, on purpose: a
Secret created with `--from-file` carries the file's trailing newline, and
forwarding it verbatim fails authentication at the registry with an error
that names nothing useful. Inline values are treated the same way, so both
routes yield the same bytes. A whitespace-only value counts as empty. A
credential whose surrounding whitespace is significant is not supported.

Why indirection rather than an indexed override such as
`COLLAB_HUB_API__COGS__REGISTRY_SOURCES__0__CREDENTIALS__PASSWORD`: the
pydantic-settings release the API pins does not layer index-style variables
over a list that arrived as JSON — the override is silently ignored, and a
silently ignored credential is the one failure mode this design must not
have. `api/tests/test_config_cogs.py` pins that measurement so a future
library version that starts honoring it is noticed.

Outside the chart (a bare process, docker-compose), the same mechanism works
with any variable names: put `"password_env": "MY_VAR"` in the JSON and set
`MY_VAR`.

## CA bundle

Dev clusters front the registry with a certificate from a private CA. Put the
CA certificate in a ConfigMap and name it:

```yaml
cogs:
  caBundle:
    configMap: collab-hub-cogs-ca
    key: ca.crt        # default
```

The chart mounts it read-only at `/etc/collab-hub/cogs-ca/<key>` (the API
container runs with a read-only root filesystem, so this is a volume, not a
file written at startup) and passes that path as `ca_bundle_path` to every
source that does not set its own `caBundlePath`. Leave `configMap` empty to
use system trust only.

## Indexer

```yaml
cogs:
  index:
    enabled: false      # sweep the sources on a schedule
    intervalSeconds: 300
    runOnStartup: true
```

`enabled` is the switch the indexer (#84) will honor: off, no sweep is
scheduled, so the read API (#85) serves whatever the index already holds.
Sources may be configured while the indexer is off — and they are still
rendered and validated: the source JSON, the Secret-backed env vars and the
CA mount are emitted whenever `registry.sources` is non-empty, regardless of
`enabled`, and the API still resolves every named Secret at startup. Only
`intervalSeconds` and `runOnStartup` are suppressed when `enabled` is false.
`enabled: true` with zero sources is refused at render and at startup — an
indexer with nothing to index is a misconfiguration, not an idle worker. The
interval is bounded to 10 s..24 h because a sweep lists every repository of
every source.

With no sources and the indexer off, the chart renders only the `enabled`
flag, so a deployment without Cogs carries no other `cogs` environment.

## Read API

`api/src/collab_hub_api/routers/cogs.py` serves the catalog the indexer
fills. It is read-only and answers from what was captured at index time: it
never contacts a registry, and an install goes to whatever host the pinned
reference names. By default that is the backing registry; with
[`cogs.serve.enabled`](#pulls-through-the-hub) it is the Hub itself. The catalog is
registry-agnostic — the answers are the same whichever adapter indexed a row.

| Route | Answers |
| --- | --- |
| `GET /v1/cogs` | Current Cogs, one entry per `cog_id`: its newest present version, with a **trimmed** card (below). Filtered and paged (below). |
| `GET /v1/cogs/{cog_id}` | The Cog's current entry, with its full card, plus `versions`: every indexed location of every version (digest, version, tags, `pushed_at`, `indexed_at`, `source_id`, repository, reference, `removed_at`), newest first, removed ones included. |
| `GET /v1/cogs/{cog_id}/versions/{digest}` | The exact, full card indexed for that digest — what an install pins. |
| `GET /v1/cogs/{cog_id}/versions/{digest}/cog.md` | The Markdown body of the version's `COG.md` (after its frontmatter), `text/markdown`, as captured at index time. The frontmatter is on the card (`frontmatter`, `frontmatter_raw`). |
| `GET /v1/cogs/{cog_id}/versions/{digest}/reference` | The **install reference** `{"reference": "<host>/<repository>@sha256:…", "source_id", "repository", "digest", "present", "locations"}` — ready for `nebi import`. |
| `GET /v1/cogs/catalog.v1.json` | **Transitional** compatibility view, below. |

Every entry carries the digest, the location (`source_id`, `repository`,
`reference`), `tags`, `pushed_at`, `indexed_at`, `removed_at` and the `card`
— the bundle reader's output, served verbatim (trimmed on list items, and
redacted for anonymous callers; both below). The OpenAPI document (`/docs`)
lists and describes every top-level key the reader emits but types none of
them: the card is the Cog's own declarations, not a hub schema, and a
publisher's odd value (a numeric `id`, say) is served as declared rather than
failing the response. Clients must tolerate unexpected shapes.

**List items carry a trimmed card.** A `GET /v1/cogs` item's card is the
stored card without `body`, `profile_raw` and `frontmatter_raw` — the
verbatim documents, up to three per item — and is a separate OpenAPI schema
(`CogListEntry` / `CogListCard`) so a client cannot mistake it for a full
card. Everything else, including keys a newer reader adds, is kept. The full
card is at `GET /v1/cogs/{cog_id}` and `…/versions/{digest}`, and the body
also at `…/cog.md`. The omitted set is `LIST_CARD_OMITTED_KEYS` in
`cogs/models.py`; adding a key back later is compatible, removing one would
not be.

**Identity is the digest.** `cog_id` (`<publisher>/<name>`, so it contains a
`/` — send it as is or percent-encoded) and `name` are search keys. "Newest"
is by `pushed_at` (unknown last), then `indexed_at`.

**Removed artifacts** are hidden from `GET /v1/cogs` and from the current
entry of `GET /v1/cogs/{cog_id}`, but every `…/versions/{digest}` route still
answers for them with `removed_at` set: installs and runs pin digests, and
the row outlives the artifact. A Cog whose every version was removed is a 404
at `GET /v1/cogs/{cog_id}` and stays reachable by digest. Non-Cog artifacts,
failed reads and cards without an `id` never appear.

**Filters** (`GET /v1/cogs`, all optional, combined with AND):

| Parameter | Matches |
| --- | --- |
| `kind` | the card's `kind`, exactly |
| `publisher` | the card's `publisher`, exactly |
| `provides` | an entry of the card's `provides` |
| `requires` | a capability named in the card's `requires` |
| `accepts` / `produces` | an io type in the card's `io.accepts` / `io.produces` |
| `q` | a case-insensitive substring of the Cog's `name`, or of its `description` when that is a string (`%` and `_` are literal; case folding is guaranteed for ASCII, locale-dependent beyond it) |
| `source_id` | only this registry source (refused for anonymous callers, below) |

`source_id` scopes the choice: the newest version is picked among that
source's rows. Every other filter tests the Cog's **current** version, never
an older one — a Cog that dropped a capability in its latest release does not
list under it — so without `source_id` a listed entry is always the version
`GET /v1/cogs/{cog_id}` serves. With `source_id` it is the newest version *in
that source*, which may be older than the one the detail route shows. The card filters are `jsonb` containment over the stored card, which
the GIN index on `card` serves.

**Paging.** `limit` (default 50, at most 200) and `offset` (default 0), as
the task routes page. The page is `{"items": [...], "limit", "offset",
"next_offset"}`, ordered by `cog_id` in code-point order; `next_offset` is
the `offset` of the next page, or `null` on the last one.

**Errors** use the API envelope `{"error": {"code", "message"}}`:
`cog_not_found` and `cog_version_not_found` (404 — including a digest that is
indexed, but as another Cog's), `cog_catalog_unavailable` (503 — neither
Postgres nor the development memory backend is configured; an empty catalog
is never invented), `validation_error` (422 —
an out-of-range parameter, a digest that is not `sha256:` followed by 64
lowercase hex digits — a well-formed digest the catalog does not hold is a
404 — or a `source_id` filter from an anonymous caller), and `unauthorized`
(401).

**Auth.** Every route requires an authenticated caller by default, like the
frames routes, and `/v1/cogs` is an API prefix of the path-protection map, so
a refusal under the hardened map keeps the envelope. **Anonymous discovery is
a `security.paths` entry**, not a code change:

```yaml
security:
  paths:
    - path: /v1/cogs
      match: prefix
      access: public
```

The routes admit a request without credentials when the rule deciding its
path is `public` and sits at or below `/v1/cogs` — so an `exact` rule can
open just `/v1/cogs/catalog.v1.json`. A broader public rule (`/` or `/v1`
prefix) does not open the catalog, and neither does `defaultAccess: public`
on its own (the unconfigured default), so nothing becomes anonymous by
accident. These are the only API routes that honor a `public` entry this
way; every other `/v1` route requires a caller whatever the map says.

Under such a rule credentials are optional, not ignored. What a request
gets depends on what it presents:

| The request | Answer |
| --- | --- |
| No credentials: no `IdToken-*` cookie and no `Authorization` header | The **anonymous view** (below) |
| Credentials the API accepts, the same check the frames routes use | The full answer. That includes a platform operator with no organization, whom the check accepts. |
| Credentials the API rejects: malformed, expired, an unsupported scheme, or a verifier that cannot run (no JWKS configured, JWKS unreachable) | 401 `unauthorized`, as on any other route; never the anonymous view |
| A valid subject with no organization, where the check answers 403 `no_organization` | The anonymous view: a public page needs no organization |

Any other failure, an unavailable organization store for instance,
propagates as it would elsewhere. The anonymous view leaves out what
discovery does not need:

- `source_id`, everywhere it appears: on list items, the detail entry and
  its `versions`, the version entry, and the reference and its `locations`.
  Source ids name the hub's internal registry configuration.
- the card's reader diagnostics, `errors` and `warnings`, on list and full
  cards alike.

The pinned `reference` (`<host>/<repository>@<digest>`) and `repository`
stay: a client must know where to pull from. When the Hub serves pulls the
host in it is the Hub's own, so an anonymous answer names no backing
registry at all. An anonymous `source_id` filter
is refused with 422 `validation_error` rather than answered, since filtering
by a value the caller cannot see would let it probe for source ids. The
refusal is decided on the raw query string, before any other parameter is
validated, so an empty or over-long value is refused the same way and the
value is never echoed in the error details.
`catalog.v1.json` carries neither and is the same for every caller. The
OpenAPI schemas mark these fields "omitted for anonymous callers" and do not
list `source_id` as required.

### `catalog.v1.json` (transitional)

`GET /v1/cogs/catalog.v1.json` renders the index in the shape of the static
`catalog.v1.json` that clients read before the hub had a catalog, so a client
can switch its catalog URL before it grows new logic:

```json
{"schemaVersion": 1, "repositories": [
  {"namespace": "cogs", "name": "cog-audio-transcriber-1a2b", "description": "…"}
]}
```

One entry per repository path holding a present, indexed Cog: `namespace` is
the path's first segment, `name` the rest, `description` the newest
version's card description (empty when it has none). Entries are sorted by
path and deduplicated across sources — the shape carries no registry host
and no digest, so the same path in two sources is one entry, and a client
reading it still resolves repositories against the registry it already knows.
A single-segment repository path has no namespace and is left out. The hub's
own `static` source parses this document, so a hub can index another hub's
view.

It is **transitional**: it exists for the switch-over and will be removed
once clients read `GET /v1/cogs`. Build nothing new on it.

### Level 1

`make api` keeps the catalog in process memory
(`COLLAB_HUB_API__COGS__CATALOG__BACKEND=memory`, a development override
with no chart value), so the read API answers 200 with an empty catalog at
level 1; with a `static` source and `cogs.index.enabled` the in-memory
catalog can be filled by a real sweep. Every other level, and every
deployment, keeps the catalog in the shared `frames.postgres`. See
[`dev/README.md`](../dev/README.md#level-1--the-api-alone).

## Pulls through the Hub

Off by default. With `cogs.serve.enabled` the Hub serves the
[OCI Distribution](https://github.com/opencontainers/distribution-spec) read
API on its own host, in front of the configured sources, so installing a Cog
needs a Hub sign-in and nothing else: no client holds a credential for a
backing registry, no install record names one, and the registry behind the
Hub can be replaced without a client noticing.

```yaml
cogs:
  serve:
    enabled: false
    publicUrl: ""               # derived from the API's host; see below
    credentialTtlSeconds: 900   # 60..86400
    tokenTtlSeconds: 300        # 30..3600
    maxBlobBytes: 1073741824    # 1 GiB
    maxBlobSeconds: 900         # 10..21600
    routeTimeout: true          # HTTPRoute only
```

| Chart value | Setting (`COLLAB_HUB_API__COGS__SERVE__…`) | Meaning |
| --- | --- | --- |
| `enabled` | `ENABLED` | Mount `/v2/`, enable the credential exchange, and point catalog references at the Hub. Needs at least one source and a catalog store. |
| `publicUrl` | `PUBLIC_URL` | The Hub's external origin, `https://host[:port]`, no path. |
| `credentialTtlSeconds` | `CREDENTIAL_TTL_SECONDS` | Lifetime of a registry credential. |
| `tokenTtlSeconds` | `TOKEN_TTL_SECONDS` | Lifetime of a pull token. |
| `maxBlobBytes` | `MAX_BLOB_BYTES` | Largest blob the Hub relays. |
| `maxBlobSeconds` | `MAX_BLOB_SECONDS` | Wall-clock limit on one blob response. |
| `routeTimeout` | (chart only) | Give the HTTPRoute's `/v2` rule a matching request timeout. |

**Off changes nothing.** No route exists under `/v2/`, the exchange answers
404 `cog_registry_not_served`, and every `reference` names the backing
registry exactly as before, with no new keys in any answer. Clients released
before this existed pull from the backing registry and keep working across a
Hub upgrade; turn serving on once the clients that use it are out. The chart
renders no `serve` variable at all while it is off.

### One origin

The registry is **the Hub API's own origin**, not a hostname of its own. The
`registry` of an exchanged credential, the host of every catalog `reference`
and the host `/v2/` is served on are one value: the authority (`host[:port]`,
lowercase, the scheme's default port left out) of `publicUrl`. Clients
depend on it. Collab Desktop accepts a registry credential only when its
`registry` is exactly the authority of the origin it sent the exchange to,
and treats anything else as a Hub that does not serve installs.

So `publicUrl` is not a place to name a registry host. Left empty, the chart
derives `https://<host>` from `api.nebariapp.hostname`, else from
`api.ingress.host`. Set it only to add a port or to say `http`; a value
naming a different host than the one the chart routes fails the render, and
the API refuses to start when it disagrees with `web.public_base_url`. It is
configuration rather than the request's `Host` header because a value a
caller can influence has no business in a credential, a challenge or a
reference. A bare process must set it.

`/v2/` must sit at the **root** of that host: registry clients build
`https://<host>/v2/…` and have no notion of a path prefix. A Hub mounted
under `server.rootPath` needs `/v2` routed to it unprefixed.

### What a client does

```
GET  /v2/                                   401  WWW-Authenticate: Bearer realm="https://<hub>/v2/token",service="<hub>"
POST /v1/cogs/registry-credentials          201  {id, registry, username, secret, scope, expires_at}   (Hub sign-in)
GET  /v2/token?service=<hub>&scope=repository:<name>:pull     (Basic username:secret)
                                            200  {token, access_token, expires_in, issued_at}
GET  /v2/<name>/manifests/<tag|digest>      200  (Bearer token)
GET  /v2/<name>/blobs/<digest>              200
```

The middle three are what `oras`, `docker` and any other registry client do
by themselves once they hold the username and secret, so the only step a
client adds is the exchange:

```sh
oras login hub.example.com -u "$USERNAME" --password-stdin <<<"$SECRET"
oras pull hub.example.com/cogs/cog-audio-transcriber@sha256:…
```

| Route | Answers |
| --- | --- |
| `GET`/`HEAD /v2/` | `200 {}` with a pull token; the bearer challenge without one. |
| `GET`/`HEAD /v2/<name>/manifests/<reference>` | The manifest, byte for byte, by tag or digest, with `Docker-Content-Digest` and its own `Content-Type`. |
| `GET`/`HEAD /v2/<name>/blobs/<digest>` | The blob, streamed. `HEAD` answers the size from the manifest that references it. |
| `GET /v2/<name>/tags/list` | `{"name", "tags"}`, sorted; `n` and `last` page it, with a `Link: …; rel="next"`. |
| `GET /v2/token` | The distribution token endpoint. |
| any other method under `/v2/` | 405 `UNSUPPORTED`: the surface is read-only. |

Errors are the registry format, `{"errors": [{"code", "message", "detail"}]}`:
`UNAUTHORIZED` (401), `DENIED` (403: the account has no organization, or a
blob is over `maxBlobBytes`), `NAME_UNKNOWN`, `MANIFEST_UNKNOWN`,
`BLOB_UNKNOWN` (404), `UNSUPPORTED` (405), `UNAVAILABLE` (503: the source or
the database could not answer; retry).

### Registry credentials

A client never presents its Hub token to `/v2/` and never stores it as a
registry login. It exchanges its session for a **registry credential**:

| Route | Answers |
| --- | --- |
| `POST /v1/cogs/registry-credentials` | 201 `{"id", "registry", "username", "secret", "scope": "pull", "expires_at"}`. Optional body `{"scope": "pull"}`; any other scope is 422. |
| `DELETE /v1/cogs/registry-credentials/{id}` | 204. 404 `cog_registry_credential_not_found` for an id that is unknown, expired or someone else's. |
| `DELETE /v1/cogs/registry-credentials` | 204: every credential and pull token of the caller. Idempotent. |

All three need an ordinary Hub sign-in and answer 404
`cog_registry_not_served` when serving is off. What the credential is:

- **Opaque and stored as a digest.** `secret` is shown once; the Hub keeps
  its SHA-256 (`collab_cog_registry_credentials`, migration 13). There is no
  signing key to configure or rotate. `id` is one URL-safe path segment
  matching `[A-Za-z0-9][A-Za-z0-9_-]{0,127}` (today `crc-` and 24 hex
  digits), and `username` is the same string.
- **Pull-only.** It can be turned into pull tokens and nothing else; a token
  request that asks for `push` is granted `pull`.
- **Short-lived.** It stops at `expires_at` (`credentialTtlSeconds`, fifteen
  minutes by default), and so does every token minted from it, whatever
  `tokenTtlSeconds` says. Exchange a fresh one before each install, and size
  the lifetime to the longest single install.
- **Revocable, at once.** Revoking deletes the row and its tokens with it;
  the next request with one of those tokens is a 401. A user holds at most
  20 live credentials; exchanging past that drops the oldest.
- **Useless anywhere else.** It is not a JWT. Every Hub API outside `/v2/`
  answers 401 to the credential, to its secret presented as a bearer token,
  and to a pull token; the backing registry has never heard of it.

**What "the Hub session ends" means here.** The credential records the `sid`
of the session it was exchanged from, but the Hub has no channel to learn
that a Keycloak session ended (no introspection call, no back-channel
logout), and none was added for this. What bounds a credential after
sign-out is therefore: the client revoking it, by id (clients are expected
to revoke each credential they hold when they are done with it and at
sign-out, and not to rely on the revoke-all route), and its lifetime,
fifteen minutes by default. On a membership-resolving deployment
the owner's membership is re-read at every token mint, so a member removed
from their organization stops pulling at the next mint, at most
`tokenTtlSeconds` later.

The token endpoint also accepts a Hub credential the API already accepts (a
bearer access token, the gateway's cookie) in place of Basic auth, for a
client that attaches it per request and never stores it. A token minted that
way has no credential to be revoked with; it ends with `tokenTtlSeconds` or
with `DELETE /v1/cogs/registry-credentials`. The read API itself takes pull
tokens only.

### What is served

**Only what the catalog would show.** Authorization is the rule
`GET /v1/cogs` applies: a caller the Hub authenticates sees the whole
catalog, so may pull all of it. A pull always needs a credential, even where
a `security.paths` rule opens catalog discovery to anonymous callers. Every
answer starts from the catalog's rows for the repository: present, indexed
Cogs. A repository, tag or digest the catalog does not list is a 404 whether
or not a registry holds it, and the request never reaches a registry.
Removed versions, non-Cog artifacts and failed reads are not pullable.

**Repository names are the backing repository paths**, unchanged and not
qualified by source: `<hub>/cogs/cog-a@sha256:…`. A repository path carried
by two sources is one repository here. The same digest in both is the same
content, served from whichever source answers; a tag both carry resolves to
the newest push, as the catalog orders versions. Moving a repository to
another registry under the same path changes nothing a client sees.

**Tags come from the catalog**, never from a live listing. A tag exists here
when an indexed, present row carries it, and resolves to that row's digest.
A tag pushed since the last sweep is not served until it is indexed.

**A blob is served only if an indexed manifest of that repository references
it.** A blob request names a repository and a digest, so the Hub has to
establish the link itself. It reads the manifests: for each pullable
artifact, its manifest's config and layer digests (and, for an index, its
child manifests and theirs), fetched from the source, verified, and kept in
a bounded in-process cache keyed by digest. Content never changes under a
digest, so an entry is never stale. A client pulls the manifest first, which
warms the entry for its blob requests on that replica; a replica that has
not seen the manifest reads the repository's pullable manifests, newest
first and at most 256, until it finds the digest. The descriptor's `size`
comes from the same place, which is how a response declares its length, and
how an oversized blob is refused, before the registry is asked.

**Multi-platform indexes** are served as stored: the index, each child
manifest by digest, and their blobs. At most 32 children of one index are
followed, and an index nested in an index is not.

**Streaming.** Blobs are relayed in 64 KiB chunks and never buffered whole.
Each is hashed as it passes and its last chunk is held until the hash and
the length match the digest and the manifest's descriptor; a blob that fails
reaches the client short, with the connection dropped, never complete and
wrong. Digests are never rewritten. A redirect from the registry to object
storage is followed by the Hub, without the registry credential once it
leaves the registry's origin; the client is never redirected. (Redirecting
blobs to a signed URL would take that traffic off the Hub at the price of
showing clients the storage host; streaming is the default and the only
mode today.)

### What stays inside the Hub

No response, header or error from `/v2/` or the catalog carries a backing
registry's credential or host. Upstream error bodies are not read, upstream
headers are not copied, and `Location` headers are followed rather than
relayed. A platform operator additionally gets `backing_reference`
(`<backing host>/<repository>@<digest>`) on
`…/versions/{digest}/reference` and on each of its `locations`; the field is
absent for everyone else, and `source_id` names the source for any
authenticated caller. Logs are the operator's: the Hub's own lines name the
source id, and the HTTP client's request log names the URL it called, with
its query string removed so that a pre-signed storage URL is not logged with
its signature.

### Replicas and the indexer

Serving builds its own registry sources on every replica that serves `/v2/`,
separately from the indexer's, so an API replica that does not sweep (the
indexer off, or running as another workload) still reaches the sources. The
source list and its Secret-backed variables are already rendered whenever
`registry.sources` is non-empty. Each replica keeps its own manifest cache;
nothing is shared but the catalog and the credential tables.

### Gateway

`/v2` carries bodies up to `maxBlobBytes` for up to `maxBlobSeconds`, on the
same host as the API:

- **Authentication.** The app authenticates `/v2` itself, and its 401 is the
  challenge a client follows. The path-protection map does not apply to it,
  whatever `security.paths` says. Behind the Nebari gateway the chart adds
  `/v2` to the NebariApp's `publicRoutes` when serving is on, so the
  gateway's browser sign-in does not answer in the app's place.
- **Timeouts.** With `api.ingress.kind: HTTPRoute` the chart renders a `/v2`
  rule with `timeouts.request` of `maxBlobSeconds` plus 30 seconds. That
  field needs Gateway API v1.2 CRDs; on older ones set `routeTimeout: false`
  and raise the gateway's own request timeout for the route. With an
  Ingress, set the controller's read and send timeouts (for ingress-nginx,
  `proxy-read-timeout` and `proxy-send-timeout`) to at least
  `maxBlobSeconds`. Behind the Nebari gateway the route is the operator's;
  check its request timeout before serving large blobs.
- **Buffering.** Responses are streamed with a `Content-Length`; a proxy
  that buffers responses to disk (ingress-nginx's `proxy-buffering`, with
  `proxy-max-temp-file-size`) should have that turned off for `/v2`, or its
  temp-file limit raised past `maxBlobBytes`. Requests have no bodies.
- **Load.** Every byte of every install goes through the API pods. Size
  their network and replica count for it, and `frames.postgres.pool` for
  two short queries per `/v2` request.

### Trying it

`scripts/cog-serve-e2e/run.sh distribution` (or `zot`) starts a private
registry, publishes a Cog to it, starts the Hub in front of it as a `static`
source, and pulls the Cog from the Hub with `oras` over TLS. CI runs both
(`.github/workflows/test-cog-serve-e2e.yaml`); the same script against two
registries is the check that the registry behind the Hub is swappable.

## How the rules stay in step

The refusals above exist in three layers: `values.schema.json` (structure
and grammar), `templates/cogs-validations.yaml` (cross-field rules, at render
time) and the API's `config.py` / `cogs/registry.py` (at startup). One
fixture keeps them from drifting: `scripts/testdata/chart/cogs-negative-cases.yaml`
holds every refused configuration once, in two forms — the chart values and
the `cogs` settings the chart renders from them — and two consumers read it.

- `scripts/chart_rules_parity.py`, run by `scripts/chart_render_tests.sh` in
  the lint workflow, renders each case and asserts the chart refuses it with
  the expected message; renders it again with the schema skipped and the
  validations template removed and asserts the Deployment's settings equal the
  fixture's settings form, so the two forms are proven to describe one
  configuration; and asserts every `fail` in the validations template fired
  for some case.
- `api/tests/test_config_cogs.py` feeds the settings form to `Config` and
  asserts the API refuses it with the expected message — or, for the rules
  that can only live in the chart (an empty `existingSecret`, ids that
  collide once derived into variable names, `extraEnv`, the CA key), that the
  API *accepts* what the chart would have rendered, with the fixture recording
  why the rule cannot be mirrored.

The fixture also lists **accepted** boundary configurations (ports, IPv6
literals, in-cluster hostnames, dotted project names) that the full chart
must render and the API must accept, so the schema cannot drift stricter than
the API unnoticed.

What this does and does not guarantee: every `fail` in the validations
template must fire for some case, so a template rule added without a case
fails the run. Schema and API rules have no automatic inventory — the fixture
enumerates them, and a new one needs a case added by hand; a case without the
rule in every layer fails that layer's test. Checks the chart cannot express
(credentials both-or-neither, a `*_env` name that is blank, unset, or
conflicts with an inline value) are tested directly in
`api/tests/test_config_cogs.py`.

Two asymmetries are deliberate, both in the direction of the chart refusing
what the API would take. Normalization: the chart validates values as
written; the API normalizes first (surrounding whitespace is stripped, and the
URL scheme is case-folded by the parser), so `projects: [" cogs "]` or
`url: HTTPS://…` fails the render but would be accepted by a bare process.
Host grammar: the API accepts whatever its URL parser does — internationalized
hostnames, IPvFuture literals, hostnames with characters outside
`[A-Za-z0-9._-]` — and the chart does not. A values file is authored, and the
render refuses what the API would have silently corrected or tolerated.

## Worked example: one Harbor source

Create the Secrets and the CA ConfigMap out of band (a secrets operator, a
sealed secret, or `kubectl`):

```sh
kubectl -n collab-hub create secret generic collab-hub-harbor-robot \
  --from-literal=username='robot$cogs+indexer' \
  --from-literal=password="$ROBOT_SECRET"
kubectl -n collab-hub create secret generic collab-hub-harbor-webhook \
  --from-literal=secret="$WEBHOOK_SECRET"
kubectl -n collab-hub create configmap collab-hub-cogs-ca --from-file=ca.crt=./gateway-ca.pem
```

Then in values:

```yaml
cogs:
  registry:
    sources:
      - id: harbor-main
        kind: harbor
        url: https://harbor.example.com
        apiUrl: http://harbor-core.harbor.svc.cluster.local:80
        tokenUrl: http://harbor-core.harbor.svc.cluster.local:80/service/token
        projects: [cogs]
        credentials:
          existingSecret: collab-hub-harbor-robot
        webhook:
          existingSecret: collab-hub-harbor-webhook
  caBundle:
    configMap: collab-hub-cogs-ca
  index:
    enabled: true
    intervalSeconds: 300
```

The rendered Deployment carries `COLLAB_HUB_API__COGS__REGISTRY_SOURCES` (JSON,
no secrets), `COLLAB_HUB_COGS_SOURCE_HARBOR_MAIN_{USERNAME,PASSWORD,WEBHOOK_SECRET}`
from the two Secrets, and the CA bundle at `/etc/collab-hub/cogs-ca/ca.crt`.
Nothing inside the pod needs editing. `helm/collab-hub/ci/cogs-values.yaml`
is this example plus a static source, and `scripts/chart_render_tests.sh`
renders it (and every refusal above) in the lint workflow. Nothing in the
manifest is trusted implicitly: a URL that embeds `user:password@` is refused
by the schema and the render, because the JSON lands in the pod spec and the
release history.

## Bare process

Without the chart, set the same settings directly. pydantic-settings reads
list-valued settings as JSON:

```sh
export COLLAB_HUB_API__COGS__INDEX__ENABLED=true
export COLLAB_HUB_API__COGS__REGISTRY_SOURCES='[{"id":"harbor-main","kind":"harbor",
  "url":"https://harbor.example.com","projects":["cogs"],
  "credentials":{"username_env":"HARBOR_USER","password_env":"HARBOR_PASSWORD"}}]'
export HARBOR_USER='robot$cogs+indexer' HARBOR_PASSWORD=...
```

Inline `"password": "..."` in the JSON is accepted too, for local runs; a
source that sets both the inline value and the `_env` name is refused as
ambiguous.
