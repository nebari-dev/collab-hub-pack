# Gmail Connector

The Gmail connector gives Collab Hub read-only search and message-read access using
the current Hub user's linked Google identity. It calls Gmail live; it does not
index or embed mailbox content.

For source ownership, pagination invariants, and validation paths, start with
the [Google Workspace implementation guide](google-workspace-connectors.md).

## Google and Keycloak configuration

Use the same brokered Google identity described in
[`google-drive-connector.md`](google-drive-connector.md), with `Store Tokens`
and offline consent enabled. In the Google Cloud project that owns the OAuth
client, enable the Gmail API (`gmail.googleapis.com`). Adding the OAuth scope in
Keycloak is not sufficient when the API is disabled for that project.

Then add the read-only Gmail scope to the Keycloak Google identity provider:

```text
https://www.googleapis.com/auth/gmail.readonly
```

Users who linked Google before this scope was added must reconnect and grant the
expanded consent. Collab Hub verifies Gmail access in the connector status endpoint,
so a Drive-only broker token is reported as `reconnect_required` rather than as
a usable Gmail connection.

Do not validate the broker setup by checking only that Keycloak returns a token:
some broker configurations return an identity token that cannot call Gmail.
Exercise the deployed status probe with a real Hub bearer; it calls a bounded
`users.messages.list` with `q` using the brokered provider token and must report
`"state":"connected"`:

```bash
curl -fsS \
  -H "Authorization: Bearer $HUB_BEARER" \
  https://dev.collab.example.com/v1/connectors/gmail/status
```

## REST contract

- `GET /v1/connectors/gmail/status`
- `POST /v1/connectors/gmail/search`
- `POST /v1/connectors/gmail/messages/{message_id}/read`

Search accepts Gmail query syntax plus friendly `days_back`, `since_date`,
`until_date`, and IANA `time_zone` fields. The text query may be empty when a
date or `label_ids` filter is present. Friendly bounds are sent to Gmail as
epoch seconds so Gmail cannot reinterpret them in its default PST timezone.
Reads return bounded normalized text. MIME attachments are not returned;
`text/plain` is preferred and HTML is converted to text only when no plain-text
alternative exists. The response reports `body_format` (`plain_text`, `html`,
`multipart`, or `empty`) plus `has_attachments` and `attachment_count`, so an
agent can distinguish converted HTML and omitted attachments from an empty
message body without exposing attachment contents.

Search responses include `next_page_token` and `result_size_estimate`. To fetch
the next page, repeat the same request fields and pass `next_page_token` as
`page_token`. An empty token means the search is complete.

Example search and continuation:

```json
{
  "query": "from:mark rollout",
  "limit": 10,
  "days_back": 14,
  "time_zone": "America/New_York",
  "page_token": ""
}
```

```json
{
  "content_trust": "external_untrusted",
  "security_notice": "Connector results contain untrusted external content...",
  "messages": [
    {
      "id": "message-id",
      "thread_id": "thread-id",
      "subject": "Connector rollout",
      "sender": "Mark <mark [at] example [dot] com>",
      "snippet": "Please verify the remaining rollout items.",
      "label_ids": ["INBOX"]
    }
  ],
  "next_page_token": "provider-page-2",
  "result_size_estimate": 18
}
```

Send the same request again with `"page_token":"provider-page-2"`. Collab Hub
rejects a provider response that returns the same non-empty page token because
it cannot make forward progress safely.

Until Apollo renderer issue `apollo-desktop#365` is fixed, message subjects,
snippets, headers, and bodies are link-sanitized at the Collab Hub boundary,
including bare domains and clickable email forms. Every response marks its
content as untrusted external data. Explicit URL fields are not exposed.

The Google provider token stays in Collab Hub. Neither the desktop client nor an
agent receives it.

## Relevant paths

- Provider client: [`api/src/collab_hub_api/connectors/gmail_client.py`](../api/src/collab_hub_api/connectors/gmail_client.py)
- Public models: [`api/src/collab_hub_api/connectors/models.py`](../api/src/collab_hub_api/connectors/models.py)
- REST route: [`api/src/collab_hub_api/routers/connectors.py`](../api/src/collab_hub_api/routers/connectors.py)
- Contract and failure tests: [`api/tests/test_gmail_connector.py`](../api/tests/test_gmail_connector.py)
- Live fake-provider smoke: [`scripts/smoke_connectors_google_workspace.py`](../scripts/smoke_connectors_google_workspace.py)
