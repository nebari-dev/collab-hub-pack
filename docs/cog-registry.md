# Cog registry

The hub indexes Cogs from OCI registries. This page covers the `cogs:` block
of the Helm chart and the matching application settings: which registries are
read (**sources**), how the hub authenticates to them (**credentials**), how a
private CA is trusted (**CA bundle**), and how often the index is rebuilt
(**indexer**). The adapters themselves — what "Harbor" and "static" mean and
why the registry stays swappable — are documented in
`api/src/collab_hub_api/cogs/registry.py`; the configuration surface is
[#87](https://github.com/nebari-dev/collab-hub-pack/issues/87).

> **Status.** This page describes the configuration surface and the chart
> wiring. Consuming it at runtime — the indexer sweep ([#84]) and the Cog read
> API ([#85]) — lands separately; today the API validates the block at startup
> (and refuses to start on the errors described below) but no sweep runs and
> no registry is contacted.

[#84]: https://github.com/nebari-dev/collab-hub-pack/issues/84
[#85]: https://github.com/nebari-dev/collab-hub-pack/issues/85

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
