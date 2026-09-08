# Notion Connector

The Notion connector lets Collab Hub search and read a user's Notion pages and
databases with that user's linked Notion workspace. It is **read-only** — it only
ever calls Notion read endpoints and has no code path that writes to Notion.

## Requirements

> **Capability — the security fact that must be signed off, not discovered.**
> Notion sets read/write capability **on the integration, in Notion's developer
> portal**, not via per-request OAuth `scope` strings. "Read-only" means the
> public integration is configured with the **Read content** capability only and
> **Insert content / Update content are left disabled**. Read-only is therefore a
> *configuration* guarantee at the integration level, reinforced in Collab Hub
> client code (only read methods exist, no write verb is ever constructed, and
> tests assert this at every layer). Enabling any write capability on the
> integration "just in case" violates the standing read-only contract.

> **Notion is not an OIDC provider.** Unlike GitHub/Google/Slack it has no OIDC
> discovery document, no `id_token`, and no standard `userinfo`. It issues a
> **workspace-scoped bot token** (returned alongside `bot_id`, `workspace_id`,
> `workspace_name`, `owner`), historically **non-expiring with no refresh
> token**. This shapes the token-path decision below.

### Token path — Option A vs Option B (decide before deploying)

The per-user Notion token can be stored/retrieved two ways. **The connector code
downstream of the token provider is identical either way** — only
`notion_tokens.py` and the desktop login method differ.

| Option | How | Trade-off |
| --- | --- | --- |
| **A. Keycloak generic-OAuth broker** *(shipped default in code)* | Configure a Keycloak identity provider using the **generic OAuth2 / custom SPI** type (not the built-in OIDC social type), `Store Tokens` on, pointed at `https://api.notion.com/v1/oauth/authorize` + `/oauth/token`. `notion_tokens.py` brokers the token exactly like `github_tokens.py`. | Keycloak's stock social providers assume OIDC; brokering a non-OIDC OAuth2 provider may need a custom identity-provider extension. **Verify this works in your Keycloak build before committing.** |
| **B. Hub-side OAuth + token store** | The Hub owns the Notion OAuth start/callback, encrypts the bot token per user in its own store, and `notion_tokens.py` reads from there. | More hub code (an OAuth flow + a token table), but no dependency on Keycloak supporting non-OIDC brokering. Safer if (A) is uncertain. |

> **⚠️ Status of the Option-A spike.** The code in this repo targets **Option A**.
> Whether Keycloak can broker Notion's non-OIDC OAuth2 in your realm has **not yet
> been confirmed against a live integration**. Prove it against a real Notion
> integration in your Keycloak realm before rollout; if it cannot broker a
> non-OIDC provider, switch `notion_tokens.py` to Option B (the exceptions and
> return type are unchanged, so nothing else moves).

### Creating the public Notion integration

1. In Notion's developer portal create a **public** integration.
2. Under **Capabilities**, enable **Read content** only. Leave **Insert content**
   and **Update content** disabled.
3. Set the **redirect URI**:
   - **Option A:** the Keycloak broker endpoint,
     `https://keycloak.<hub-host>/realms/<realm>/broker/notion/endpoint`.
   - **Option B:** the Hub's own Notion OAuth callback URL.
4. Note that the integration only ever sees pages/databases a workspace member
   explicitly shares with it. The bot token is **workspace-scoped**, not
   user-scoped: it grants access to exactly the content that workspace shared.

### Pinned API version

Every Notion request carries `Notion-Version: 2022-06-28`. This is pinned in
config (`notionVersion`) and sent by `notion_client._request`; **do not float
it**. Newer versions (2025-09-03+) split databases into *data sources* and move
the query endpoint — bumping the pin is a code migration, not a config change.

## Hub Helm values

```yaml
connectors:
  notion:
    brokerTokenUrl: ""            # Option A: Keycloak broker token endpoint; empty under Option B
    apiBaseUrl: https://api.notion.com
    notionVersion: "2022-06-28"
    requestTimeoutSeconds: 10
```

These map to `COLLAB_HUB_API__CONNECTORS__NOTION__*` env vars in the API
deployment. Under **Option B**, the token store's encryption key must be provided
through `existingSecret` + `secretKeyRef`, never inline.

## Endpoints

All are mounted under `/v1/connectors/notion` and require a Hub bearer; the
desktop reaches them through the authenticated loopback proxy with no credentials.

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/status` | Capability probe: `GET /v1/users/me` **then** a bounded `POST /v1/search` (page_size 1). A token that brokers but cannot read reports `reconnect_required`, not `connected`. |
| POST | `/search` | Search pages + databases. Friendly date fields are resolved **server-side** (Notion search only *sorts* by `last_edited_time`). |
| POST | `/pages/{page_id}/read` | Read a page's title + assembled block text, bounded by `max_chars`. |
| POST | `/databases/{database_id}/query` | Query a database; friendly date fields become a **native** Notion timestamp filter on `last_edited_time`. |

Error mapping: `409` not connected / reconnect, `422` bad id / stale cursor /
bad params, `502` provider failure (timeout, non-2xx, bad JSON, cursor cycle),
`503` connector unconfigured. Provider error bodies are never forwarded verbatim.

## Runtime boundary

The provider token and raw Notion payloads never enter the desktop runtime. The
Hub exchanges the Hub bearer for the workspace bot token, calls Notion, and
returns a bounded, sanitized, untrusted-marked response. Every data-bearing
response carries `content_trust: "external_untrusted"`. Page/database `url` and
rich-text `href` fields are **deliberately dropped** — link-shaped text crashes
the Apollo chat renderer (apollo-desktop#365).

## Deployment checklist

**Workspace admin (Notion side)**
- [ ] Create the public integration with **Read content** capability only.
- [ ] Set the redirect URI (Option A: Keycloak broker endpoint; Option B: Hub callback).
- [ ] Share the pages/databases the integration should see with it.

**Hub admin (Collab side)**
- [ ] Decide and record **Option A or B** (see the callout above); prove A works
      in a live realm first.
- [ ] Option A: configure the Keycloak `notion` generic-OAuth IdP with `Store
      Tokens` on and `Hide on Login Page` on; grant normal users the broker
      `read-token` role. Set `connectors.notion.brokerTokenUrl`.
- [ ] Option B: deploy the Hub Notion OAuth flow + token store; wire the
      encryption key through `existingSecret`.
- [ ] Set `notionVersion` and `apiBaseUrl` (defaults are correct for prod).

## Verification recipe (run once with a real linked workspace)

1. Link a Notion workspace through the desktop **Connect** action.
2. `GET /v1/connectors/notion/status` → `connected: true`, `account` = workspace name.
3. `POST /v1/connectors/notion/search` `{"query": "<a shared page title>"}` → the page appears; response contains **no** `url`/`notion.so` text.
4. `POST /v1/connectors/notion/pages/{id}/read` → assembled text, no `href`.
5. Confirm the bot token string never appears in any response body.

## Rollback

Set `connectors.notion.brokerTokenUrl` to `""` (Option A) — the connector reports
`not_connected` and the desktop hides the Notion tools (fail-closed). No data
migration is required; the connector is read-only and stateless in the Hub.
