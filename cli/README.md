# collab-hub

A command-line client for the Collab Hub. It is a client, not a second implementation: every command is an HTTP call to the hub's REST API, the same endpoints the Collab desktop uses, and the package imports nothing from the hub's own packages.

Today it signs in and lists the Cogs the hub offers. Launching a Cog, listing what was launched and terminating it come next, then deciding Gates, reading payloads and retrying runs; each phase of [`COG_EXECUTION.md`](../COG_EXECUTION.md) that adds a hub surface adds its commands here.

## Installing

Python 3.11 or later.

```sh
uv tool install ./cli        # from a checkout of this repository
collab-hub --help
```

In the dev environment, `make -C dev cli` installs it and prints the sign-in line for the level in use.

## Signing in

```sh
collab-hub login --hub https://hub.example.org
```

The CLI signs in the way the Collab desktop does, against the same Keycloak realm and the same public client (`apollo-desktop`): the authorization code flow with PKCE (S256) and a loopback redirect (RFC 8252). `login` opens the realm's sign-in page in a browser on this machine, which returns to a listener on `127.0.0.1` and a port the system picks. The hub's URL is all you type: the hub names its issuer and client at `GET /v1/auth/cli`.

- `--no-browser` prints the sign-in URL instead of opening it. The browser still has to run on the same machine, since the redirect goes to its loopback address; on a remote host, forward the port with SSH or use `--with-token`.
- `--with-token` reads a bearer token from stdin, for CI or a script that already holds one: `make -C dev token | collab-hub login --with-token`. Such a token is used until it expires and never renewed.

The session is kept in `credentials/<profile>.json` in the configuration directory, readable only by you (the directory is `0700`, the file `0600`), and is sent only to the hub it was obtained for. It is renewed with its refresh token before it expires.

```sh
collab-hub whoami            # the user, organization and roles the hub resolved, and the token's expiry
collab-hub logout            # ends the realm session and deletes the stored token
```

`whoami` asks the hub (`GET /v1/me`), so it reports what the hub will act on rather than what the token claims. Against a hub running dev auth, as at dev level 1, it says the session is unauthenticated: the hub answers every request as its development user, and there is nothing to sign in to.

## Profiles

Several hubs are one flag apart. `login` records its hub under the profile it ran with, and the first profile recorded becomes the default:

```toml
# ~/.config/collab-hub/config.toml
default_profile = "default"

[profiles.default]
hub = "http://127.0.0.1:8000"

[profiles."work"]
hub = "https://hub.example.org"
```

A command's hub is `--hub`, else `COLLAB_HUB_URL`, else its profile's `hub`. Its profile is `--profile`, else `COLLAB_HUB_PROFILE`, else `default_profile`, else `default`. The configuration directory is `COLLAB_HUB_CONFIG_DIR`, else `$XDG_CONFIG_HOME/collab-hub`, else `~/.config/collab-hub`.

## The Cogs the hub offers

```sh
collab-hub cog list                        # every Cog in the catalog, at its newest version
collab-hub cog list --kind context -q review
collab-hub cog show acme/reviewer          # one Cog's card and every indexed version
```

`cog list` follows every page of `GET /v1/cogs` and takes its filters: `--kind`, `--publisher`, `--provides`, `--requires` and `--query`.

## Output and exit codes

Tables for people by default; `--json` prints one JSON document for scripts, shaped for `jq`. Results go to stdout, messages to stderr.

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the hub refused or failed the request, or could not be reached |
| 2 | a usage error: a bad option, a missing hub |
| 5 | not signed in, or the session has expired and could not be renewed |

## Development

```sh
cd cli
uv run --group test pytest
uv run --with ruff ruff check src tests
```

The sign-in is tested against a stub realm, including a real redirect to the loopback listener. An import-boundary test keeps the CLI from importing `collab_hub_api` or `collab_hub_execution`.
