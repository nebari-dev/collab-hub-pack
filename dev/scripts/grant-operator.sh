#!/usr/bin/env bash
# Bootstrap (or re-activate) a platform operator on the local compose stack.
#
#   dev/scripts/grant-operator.sh [keycloak-username]   (default: operator)
#
# Looks the user up in the Keycloak `nebari` realm to get their OIDC `sub`,
# then inserts the collab_platform_roles row the way docs/frames-operations.md
# ("Bootstrapping the first operator") prescribes. The table is created by the
# API's auto-migration, so start the API once (`make -C dev api`) before this.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "${SCRIPT_DIR}/../docker-compose.yaml")
USERNAME="${1:-operator}"
KCADM=/opt/keycloak/bin/kcadm.sh

"${COMPOSE[@]}" exec -T keycloak "${KCADM}" config credentials \
    --server http://localhost:8080 --realm master --user admin --password admin >/dev/null

SUB="$("${COMPOSE[@]}" exec -T keycloak "${KCADM}" get users -r nebari \
    -q exact=true -q "username=${USERNAME}" --fields id --format csv --noquotes | tr -d '\r' | head -n1)"
if [ -z "${SUB}" ]; then
    echo "No user '${USERNAME}' in realm nebari" >&2
    exit 1
fi

"${COMPOSE[@]}" exec -T postgres psql -U collab -d collab -v ON_ERROR_STOP=1 -q <<SQL
INSERT INTO collab_platform_roles (user_id, role, granted_by, status)
VALUES ('${SUB}', 'operator', NULL, 'active')
ON CONFLICT (user_id) DO UPDATE SET status = 'active';
SQL

echo "operator granted: ${USERNAME} (sub ${SUB})"
echo "Sign in at http://localhost:8010/web/signin, then open http://localhost:8010/admin/invitations"
