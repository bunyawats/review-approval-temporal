#!/bin/bash
# Lists which Keycloak Authorization Services permissions (and the
# Resource+Scope pairs they cover) are granted by each realm role, by
# walking the Policy -> dependentPolicies (Permissions) -> resources +
# scopes chain via the Admin REST API. This is a *static config* view
# (what the current Policy/Permission setup grants), not a live per-user
# check -- for that, do a real UMA ticket exchange for a specific user's
# token instead (see README.md's "Try it (JSON API)" section).
#
# The single "RequestApproval" resource carries all 5 scopes
# (Create/Update/Cancel/Approve/Reject); each scope-type Permission binds
# one scope to a role Policy, which is why this walks /scopes per
# permission (analogous to the old resource-type /resources call) rather
# than resolving distinct Resources per permission.
#
# Usage: ./list-permissions-by-role.sh [keycloak-base-url] [realm]
# Defaults match this project's local dev setup.
set -euo pipefail

KEYCLOAK_URL="${1:-http://localhost:8080}"
REALM="${2:-myrealm}"
ADMIN_USER="${KEYCLOAK_ADMIN_USER:-admin}"
ADMIN_PASS="${KEYCLOAK_ADMIN_PASSWORD:-admin}"
CLIENT_ID="review-approval"

ADMIN_TOKEN=$(curl -s -X POST "$KEYCLOAK_URL/realms/master/protocol/openid-connect/token" \
  -d "client_id=admin-cli" -d "grant_type=password" \
  -d "username=$ADMIN_USER" -d "password=$ADMIN_PASS" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

CLIENT_UUID=$(curl -s "$KEYCLOAK_URL/admin/realms/$REALM/clients?clientId=$CLIENT_ID" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

AUTHZ="$KEYCLOAK_URL/admin/realms/$REALM/clients/$CLIENT_UUID/authz/resource-server"

# All role-type policies (the "<Role> Policy" objects), and the specific
# realm role each one requires.
curl -s "$AUTHZ/policy?type=role" -H "Authorization: Bearer $ADMIN_TOKEN" | python3 -c "
import sys, json, subprocess, os

policies = json.load(sys.stdin)
authz = '$AUTHZ'
base = '$KEYCLOAK_URL/admin/realms/$REALM'
token = '$ADMIN_TOKEN'

def api(path, base_url=authz):
    out = subprocess.run(
        ['curl', '-s', f'{base_url}{path}', '-H', f'Authorization: Bearer {token}'],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)

for policy in policies:
    # config.roles stores each role by internal UUID, not name -- resolve
    # via roles-by-id to get something readable.
    role_ids = [r['id'] for r in json.loads(policy['config']['roles'])]
    role_names = [api(f'/roles-by-id/{rid}', base_url=base)['name'] for rid in role_ids]
    print(f\"{', '.join(role_names)}:\")
    permissions = api(f\"/policy/{policy['id']}/dependentPolicies\")
    if not permissions:
        print('  (no permissions currently apply this policy)')
    for perm in permissions:
        resources = api(f\"/policy/{perm['id']}/resources\")
        scopes = api(f\"/policy/{perm['id']}/scopes\")
        resource_names = ', '.join(r['name'] for r in resources) or '(none)'
        scope_names = ', '.join(s['name'] for s in scopes) or '(whole resource, no scope)'
        print(f\"  {resource_names} -> scope: {scope_names:10s} (via {perm['name']})\")
    print()
"
