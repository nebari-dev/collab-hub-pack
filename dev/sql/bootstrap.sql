-- Seed a local organization, put a user in it as owner, and grant that same
-- user the platform `operator` role.
--
-- Run through `make seed-org SUB=<oidc-sub> EMAIL=<address>`, which supplies
-- the two psql variables. Requires the collab_ tables, so the API must have
-- started once against this database with autoMigrate on.
--
-- Platform roles are NOT Keycloak roles: Keycloak authenticates, this server
-- authorizes, and the operator grant lives in collab_platform_roles. That is
-- why becoming an operator locally is an INSERT and not a realm edit.

\set ON_ERROR_STOP on

BEGIN;

INSERT INTO collab_orgs (id, name, created_by)
VALUES ('dev-org', 'Local Development Org', :'sub')
ON CONFLICT (id) DO NOTHING;

-- One home organization per login; user_id is the OIDC sub and the primary key.
INSERT INTO collab_org_members (user_id, org_id, role, email, display_name, status)
VALUES (:'sub', 'dev-org', 'owner', :'email', :'email', 'active')
ON CONFLICT (user_id) DO UPDATE
    SET org_id = EXCLUDED.org_id,
        role   = EXCLUDED.role,
        status = 'active';

INSERT INTO collab_platform_roles (user_id, role, granted_by, status)
VALUES (:'sub', 'operator', NULL, 'active')
ON CONFLICT (user_id) DO UPDATE
    SET role = 'operator',
        status = 'active';

-- Every privileged action by hand gets its audit row in the same transaction.
INSERT INTO collab_audit_events (actor, actor_label, action, target_type, target_id, detail)
VALUES (:'sub', :'email', 'operator.manual', 'user', :'sub',
        '{"summary": "local dev bootstrap: owner of dev-org + platform operator"}');

COMMIT;

\echo 'Seeded dev-org, owner membership, and the operator grant.'
