# Local development

Two ways to run Collab Hub on a laptop. Pick by what you need to see.

| Mode | What runs | Good for |
|---|---|---|
| **API only** (`README.md` at the root) | The API process with unsafe dev auth, frames on disk, everything else in-memory | Endpoint work, tests, MCP |
| **Compose stack** (this directory) | Keycloak + Postgres + a fake SES v2 + Mailpit in Docker, the API on the host | The browser web surface: sign-in, operator and owner invitation pages, Keycloak self-registration, invitation acceptance |

A kind cluster is documented at the end for the cases that need real routing.

## Compose stack

### What is in it

| Service | Address | Role |
|---|---|---|
| Keycloak 26 | http://localhost:8080 (admin console `/admin/`, `admin` / `admin`) | OIDC realm `nebari`, imported from `keycloak/nebari-realm.json` on first start |
| Postgres 16 | `postgresql://collab:collab@localhost:5432/collab` | History, groups, orgs, invitations, usage, active-frame state |
| Fake SES v2 (`fake-ses/app.py`) | http://localhost:4566 | Amazon SES v2 stand-in. The API's only invitation-email provider is SES v2, boto3 is pointed here through `AWS_ENDPOINT_URL_SESV2`, and every `SendEmail` is relayed over SMTP into Mailpit. LocalStack's free tier does not emulate SES v2, hence the small stdlib server |
| Mailpit | http://localhost:8025 | Every email the stack sends lands here: Keycloak verification mail and the API's invitation mail |

The realm ships two clients and two users:

- `apollo-desktop` — public client the API's bearer axis trusts (audience `apollo-desktop`). Direct-access grants are on so you can mint a token with a password for `curl`.
- `collab-web` — confidential client for the web surface, redirect URI `http://localhost:8010/web/oidc/callback`, secret `collab-web-local-dev-secret`.
- Users `operator` and `owner`, both password `password`, both with verified emails at `@collab-hub.local`.

Self-registration is enabled with email verification on, so a freshly
registered user has to click the link Mailpit receives before an invitation
will accept them, which mirrors production.

### Why the API runs on the host

The web surface only accepts a plain-http OIDC issuer when its host is
loopback, and the browser and the API must agree on one issuer string. Both
therefore reach Keycloak at `http://localhost:8080/realms/nebari`. An API
inside the compose network would see `keycloak:8080`, a different issuer, and
be refused at startup. Running it on the host is also the faster loop.

Host ports are overridable with `KEYCLOAK_PORT`, `POSTGRES_PORT`,
`MAILPIT_PORT`, `MAILPIT_SMTP_PORT` and `SES_PORT` in the environment
when you run `make up`. If you move Keycloak off 8080 or the API off 8010, edit
`api.env` and the `collab-web` redirect URI in the realm file to match.

### Start it

```sh
make -C dev up      # pulls images on first run; waits for health checks
make -C dev api     # foreground; the API on http://localhost:8010
```

`make api` sources `dev/api.env`, syncs the `api/` virtualenv on Python 3.14,
and runs the API with auto-migration on, so the `collab_` tables exist after
the first start. Frames are written to `dev/.data/frames` (gitignored).

Check it:

```sh
curl -s http://localhost:8010/health
open http://localhost:8010/web            # landing page with a sign-in link
```

### Walk the invitation flow

1. **Bootstrap an operator** (once per database; the API must have started at
   least once so the table exists):

   ```sh
   make -C dev operator            # grants the seeded `operator` user
   make -C dev operator USER=jane  # or any other realm username
   ```

   This looks up the user's `sub` in Keycloak and inserts the
   `collab_platform_roles` row exactly as `docs/frames-operations.md`
   prescribes for a first operator.

2. **Sign in** at http://localhost:8010/web/signin as `operator` / `password`.
   Then open http://localhost:8010/admin/invitations and invite an address
   such as `newcomer@collab-hub.local`. The API sends the mail through
   the fake SES, which relays it into Mailpit.

3. **Open Mailpit** at http://localhost:8025 and follow the acceptance link.
   The page offers *Create an account* as the primary path, which sends the
   browser to Keycloak's registration form with the same address. Keycloak
   emails a verification link, which also lands in Mailpit.

4. **Accept.** Back on http://localhost:8010/invite/accept, click *Accept
   invitation*. That user is now the owner of a new organization and can open
   http://localhost:8010/web/org/invitations to name it and invite members.

Use a private browser window for the invitee so the operator's session cookie
does not get in the way.

### API bearer tokens for curl

```sh
TOKEN=$(curl -s -X POST http://localhost:8080/realms/nebari/protocol/openid-connect/token \
  -d grant_type=password -d client_id=apollo-desktop \
  -d username=owner -d password=password | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8010/v1/frames
```

A user with no organization membership gets the "no organization" answer from
`/v1/*`, which is the intended membership-mode behaviour. Accept an invitation
first, or use the unsafe dev-auth shortcut described in the root README for
API-only work.

### Stop, reset, inspect

```sh
make -C dev status   # container health
make -C dev logs     # follow logs
make -C dev down     # stop, keep Postgres data and the Keycloak realm state
make -C dev reset    # stop and drop the volumes and dev/.data
```

Keycloak imports the realm only when it does not already exist, so realm edits
in `keycloak/nebari-realm.json` need a `make reset` (or a manual change in the
admin console) to take effect.

## kind cluster (on request)

`make -C dev cluster` creates the software-pack-template kind cluster with the
full Nebari stack (MetalLB, Envoy Gateway, cert-manager, Keycloak,
nebari-operator). The chart itself is deployed by the smoke scripts under
`scripts/` (for example `scripts/smoke_local_collab_hub_features_kind.sh`),
which build the API image, load it into the cluster and install
`helm/collab-hub` against an in-cluster Postgres. The template's example
`up-*` targets reference charts this repo does not carry and are left only
for reference. Tear down with `make -C dev kind-down`.
