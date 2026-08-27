# Google Drive Connector

The Google Drive connector lets Collab Hub search and read a user's Drive files with
that user's linked Google identity. The connector is read-only and expects
Keycloak to broker Google tokens.

## Requirements

Configure a Google identity provider in the hub's Keycloak realm with:

- `Store Tokens` enabled.
- Google Drive read-only scope:
  `https://www.googleapis.com/auth/drive.readonly`.
- Offline access / consent prompting enabled so Google can issue refresh tokens.
- A redirect URI matching the Keycloak broker endpoint:
  `https://keycloak.<hub-host>/realms/<realm>/broker/google/endpoint`.

Configure the Collab Hub pack with the Keycloak broker token endpoint:

```yaml
connectors:
  google:
    brokerTokenUrl: http://<keycloak-service>.<namespace>.svc.cluster.local:8080/realms/<realm>/broker/google/token
```

Keycloak must allow normal hub users to read their own linked broker tokens.
Grant the `broker` client role `read-token` to the role or group that every
normal hub user receives. In a standard Keycloak realm this may be a default
realm role such as `default-roles-<realm>`; in another deployment it may be a
site-specific user role or group.

For example:

```sh
kcadm.sh add-roles \
  -r <realm> \
  --rname default-roles-<realm> \
  --cclientid broker \
  --rolename read-token
```

If the role is missing, Collab Hub cannot retrieve the user's linked Google token and
the connector status reports that Keycloak denied broker token access. Re-running
Google consent does not fix that condition; the hub's Keycloak role mapping must
be corrected.

## Runtime Boundary

The user's Google access token stays hub-side. Apollo Desktop sends the user's
Hub bearer token to Collab Hub through its local proxy; Collab Hub uses that token to ask
Keycloak for the current user's Google broker token and then calls the Drive API.
The model and desktop client do not receive the Google access token.

Search accepts an optional `mime_types` list. Collab Hub also normalizes a
JSON-encoded list nested inside that array for compatibility with older tool
adapters, preventing a serialized value such as
`["application/vnd.google-apps.document"]` from being treated as a literal MIME
type. Reads support Google Docs, Slides, Sheets exports and direct text formats.
Other formats return `unsupported: true` with an `unsupported_reason`; callers
must not interpret the accompanying empty text as a successful empty file.
