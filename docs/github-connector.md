# GitHub Connector

The GitHub connector lets Collab Hub search and read a user's GitHub issues, pull
requests, and repository files with that user's linked GitHub identity. It is
**read-only** — it only ever calls GitHub read endpoints and has no code path
that writes to GitHub.

## Requirements

> **Scope — the security fact that must be signed off, not discovered.**
> Keycloak brokers a **classic GitHub OAuth App** token, and GitHub has **no
> read-only scope for private repositories**: the `repo` scope is the floor and
> it is **read *and* write** capable. (Fine-grained read-only permissions like
> `contents:read` are not available to a **classic OAuth App** — they require a
> **GitHub App**; see "Why the classic OAuth App" below.) Read-only is therefore
> enforced **in Collab Hub client code** — only read endpoints are implemented, no
> write verb is ever constructed, and tests assert this at every layer — not by
> the token. If you only need public repositories, request `read:user read:org`
> and omit `repo` entirely; then the token has no write capability by
> construction, at the cost of no private-repo visibility.

> **Why the classic OAuth App, not a GitHub App?** A GitHub App with fine-grained
> read permissions would make read-only a *token-level* guarantee, and its
> user-to-server tokens use the same OAuth endpoints, so Keycloak could broker one
> the same way. We ship the classic OAuth App because it reuses the exact
> brokering path the other connectors already use — no signing key to manage and
> no per-org app installation — and enforce read-only in code instead. Migrating
> to a fine-grained GitHub App is the path if we later want token-level read-only.

Configure a GitHub identity provider in the hub's Keycloak realm with alias
`github` and:

- `Store Tokens` enabled (so `/broker/github/token` can return the stored token).
- **Read scopes.** In Keycloak the identity provider's **Scopes** field
  *replaces* the provider defaults rather than appending to them, so set it to
  the full space-separated list you want — for private-repo access plus project
  boards:

  ```
  user:email repo read:org read:project
  ```

  A bare `repo` would drop the default `user:email` and break the account-email
  mapper. `read:project` is the **read-only** scope for Projects V2 boards
  (`list_github_projects` / `read_github_project`, which use GitHub's GraphQL
  API); omit it if you don't need boards. There is deliberately no write/push
  scope in this list.

  > **Adding `read:project` to an existing IdP does not re-prompt already-linked
  > users** — their stored token keeps its old scopes. Every linked user must
  > **unlink and relink** GitHub to pick up board access. (Same for any scope
  > change; see the callout below.) For local static-token testing, the PAT you
  > use as `static_access_token` must also include `read:project`.
- **Hide on Login Page** enabled on the identity provider, so nobody can sign in
  to Collab *with* GitHub (which would trigger Keycloak first-broker-login and
  create a GitHub-primary account). GitHub is only ever linked as a *secondary*
  identity to an existing Hub account.
- A redirect URI matching the Keycloak broker endpoint:
  `https://keycloak.<hub-host>/realms/<realm>/broker/github/endpoint`.

Configure Collab Hub with the Keycloak broker token endpoint:

```yaml
connectors:
  github:
    brokerTokenUrl: http://<keycloak-service>.<namespace>.svc.cluster.local:8080/realms/<realm>/broker/github/token
```

Keycloak must allow normal hub users to read their own linked broker tokens.
Grant the `broker` client role `read-token` to the role or group that every
normal hub user receives, exactly as for the other connectors:

```sh
kcadm.sh add-roles -r <realm> --rname default-roles-<realm> \
  --cclientid broker --rolename read-token
```

> **Two things that will make the connector go green while seeing nothing.**
> 1. **Org owner approval.** For any org with OAuth App access restrictions, an
>    org owner must *approve* the Collab OAuth App (and, if the org uses SAML
>    SSO, the user must authorize their token for that org). Until then the token
>    brokers fine and `/status` can read the user's own repos, but org
>    repositories return `404`. Make org-owner approval a named prerequisite of
>    rollout, not an afterthought.
> 2. **Scope changes don't re-prompt existing links.** If you add or change
>    scopes on the IdP after users have already linked GitHub, GitHub does **not**
>    re-prompt them — their stored token keeps the old scopes. A scope change
>    requires each linked user to **unlink and relink** GitHub.

## Endpoints

- `GET  /v1/connectors/github/status`
- `POST /v1/connectors/github/search`
- `POST /v1/connectors/github/items/{number}/read`
- `POST /v1/connectors/github/files/read`
- `POST /v1/connectors/github/projects/list`
- `POST /v1/connectors/github/projects/{number}/read`

The two `projects` endpoints read **Projects V2** boards via GitHub's **GraphQL**
API (`read:project` scope). `projects/list` takes an `owner` (org *or* user
login) and returns each board's number/title/description/item count.
`projects/{number}/read` returns the board's items, each with its status column
and — when the item is a linked issue or PR — the `repo` (owner/name),
`number`, `assignees`, and `labels`, so a caller can triage by person or label
and chain `items/{number}/read` to read that issue/PR.

`search` runs against GitHub's issue-and-PR search (`/search/issues`, which
returns both issues and pull requests) and is **page/`per_page`-paginated with a
hard 1000-result cap**. Collab Hub returns an opaque `next_page_token` that encodes
the page number plus a fingerprint of the query/filters; feed it back unchanged
to page. Changing the query or `repo` and reusing an old token is a stale cursor
and returns `422`. `incomplete_results` is surfaced when GitHub's search times
out before scanning every candidate. GitHub's search rate limit (30 req/min; 10
for code search, which this connector does not use) is normalized to `429` with
`Retry-After` echoed.

`items/{number}/read` reads one issue or pull request (body plus recent comments,
capped at `max_chars`). Both issues and PRs carry their `assignees` and `labels`;
pull requests additionally carry `requested_reviewers` and submitted `reviews`
(user + review state), fetched from the pulls endpoint. Changed files and diffs
are out of scope. `files/read` reads a repository file by `repo`, `path`,
and optional `ref` (branch/tag/SHA; default branch when omitted). The contents
API only returns files up to 1 MB, so larger files come back with `too_large`;
non-UTF-8 files come back with `binary`; git-lfs pointers and directories come
back with an `unsupported_reason`. In every case `content` is empty rather than
garbage. `path` and `ref` are validated against traversal (`..`, absolute paths,
control characters) before they are ever placed in an upstream URL.

No endpoint returns a GitHub URL of any kind: the Collab chat renderer crashes on
link-shaped text anywhere in tool output (apollo-desktop#365), so all provider
text is link-sanitized and `repo`/`number` (not URLs) are what a follow-up read
needs.

## Runtime Boundary

The user's GitHub access token stays hub-side. Apollo Desktop sends the user's
Hub bearer token to Collab Hub through its local proxy; Collab Hub uses that token to ask
Keycloak for the current user's GitHub broker token and then calls the GitHub
API. The model and desktop client never receive the GitHub access token.

## Deployment Checklist

The connector is code-complete and tested (unit tests over the full
router→client→GitHub path via a mock transport: happy paths, every token-error
state, static/broker config precedence, 429/`Retry-After`, pagination plus
stale-cursor `422`, `incomplete_results`, oversized/binary/lfs
file handling, path/ref traversal rejection, link sanitization, and an assertion
that the token never appears in any response body — see
`api/tests/test_github_connector.py` — plus Helm schema validation). Cutting it
over to a real org is a configuration exercise. Two roles are involved.

### From whoever owns the GitHub org(s)

1. Ensure the Collab OAuth App is **approved** for each org whose repositories
   should be searchable (Org Settings → Third-party Access / OAuth App access).
2. If the org enforces SAML SSO, tell users they must authorize their linked
   token for the org after linking.
3. Confirm the read scopes above are acceptable for the org's data policy — note
   the `repo`-is-write-capable caveat and that read-only is enforced in Collab
   Hub code.

### From whoever administers the Hub (Keycloak + Collab Hub deploy)

1. In the hub's Keycloak realm, add a GitHub identity provider with **alias
   `github`** (must match exactly — Apollo's `GitHubConnectorLogin` hardcodes
   this alias). Enable `Store Tokens` and **Hide on Login Page**, set the
   **Scopes** field to `user:email repo read:org read:project`, and register the redirect URI
   above. **Reviewable artifact:** `docs/keycloak-github-idp-partial-import.json`
   is a ready-to-import version of exactly this IdP — fill in the two `REPLACE_`
   values (GitHub OAuth App client id/secret; prefer injecting the secret from
   your secret store) and import it via **Realm settings → Action → Partial
   import**, or:
   ```sh
   kcadm.sh create partialImport -r <realm> -s ifResourceExists=SKIP \
     -f docs/keycloak-github-idp-partial-import.json
   ```
   It sets Store Tokens, Hide on Login Page, the read scopes, and
   `addReadTokenRoleOnCreate`, so step 2 is handled for users created through it.
2. Grant the `broker` `read-token` role to normal users (the `kcadm.sh` line
   above) — belt-and-suspenders alongside the partial import's
   `addReadTokenRoleOnCreate`.
3. Set `connectors.github.brokerTokenUrl` in the Collab Hub Helm values and deploy.
4. Run the verification recipe below once with a real linked user.

### Verifying the connection

With a linked user's Hub bearer token:

```sh
curl -s -H "Authorization: Bearer $HUB_TOKEN" \
  https://<nexus-host>/v1/connectors/github/status | jq
```

A healthy connection reports `"state": "connected"` and an `"account"` matching
the linked GitHub login. `"reconnect_required"` means the token brokered but
cannot read repositories (bad/expired token, or org SSO not authorized).
`"unavailable"` means the connector is unconfigured or the broker `read-token`
role is missing. Then confirm a search returns results the user expects:

```sh
curl -s -H "Authorization: Bearer $HUB_TOKEN" -H 'Content-Type: application/json' \
  -d '{"query":"is:open","repo":"your-org/your-repo"}' \
  https://<nexus-host>/v1/connectors/github/search | jq '.hits[0]'
```

If `status` is `connected` but org searches are empty or a known item `read`
returns `404`, the most likely cause is missing **org owner approval** or **SSO
authorization** — not a Collab Hub problem.

## Rollback

Remove or blank `connectors.github.brokerTokenUrl` and redeploy: `/status`
reports `unavailable` and the tools disappear from Apollo. To fully remove
access, delete the GitHub identity provider from the Keycloak realm; existing
users' stored tokens are dropped with it.
