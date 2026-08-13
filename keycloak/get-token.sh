#!/bin/bash
# Fetches a Keycloak access token for one of this project's demo users, for
# quickly testing the JSON API (e.g. via the Swagger UI's Authorize button
# at http://localhost:8000/docs, or curl -- see README.md's "Try it (JSON
# API)" section).
#
# Usage:
#   ./get-token.sh -u operator1 [-p password] [-c review-approval] \
#       [-s dev-secret-change-me] [-i http://localhost:8080/realms/myrealm] [-D]
#
# All flags except -u are optional -- defaults match this project's local
# dev setup (docker-compose.yml / .env.example). Prints only the raw access
# token to stdout, so it composes with command substitution:
#   TOKEN=$(./get-token.sh -u manager1)
#
# -D / --docker: fetch the token from *inside* the `bff` container instead
# of from the host. Required when the whole stack is running via
# `docker compose up` (rather than the natively-run hybrid setup) --
# `bff` validates a token's `iss` claim against its own KEYCLOAK_ISSUER,
# which docker-compose.yml sets to the Docker-internal
# http://keycloak:8080/realms/myrealm, not http://localhost:8080/... A
# token requested from the host has iss=localhost:8080 and gets rejected
# with "Invalid issuer" even though it's otherwise valid -- fetching it
# from inside the `bff` container's network makes the iss match.
set -euo pipefail

PASSWORD="password"
CLIENT_ID="${KEYCLOAK_CLIENT_ID:-review-approval}"
CLIENT_SECRET="${KEYCLOAK_CLIENT_SECRET:-dev-secret-change-me}"
ISSUER="${KEYCLOAK_ISSUER:-http://localhost:8080/realms/myrealm}"
USERNAME=""
VIA_DOCKER=false

usage() {
  echo "Usage: $0 -u <username> [-p <password>] [-c <client_id>] [-s <client_secret>] [-i <issuer_url>] [-D]" >&2
  echo "  Demo users (password 'password'): operator1, operator2, manager1, manager2" >&2
  echo "  -D: fetch via the 'bff' container (needed when running the full docker compose stack)" >&2
  exit 1
}

ISSUER_SET=false
while getopts "u:p:c:s:i:Dh-:" opt; do
  case "$opt" in
    u) USERNAME="$OPTARG" ;;
    p) PASSWORD="$OPTARG" ;;
    c) CLIENT_ID="$OPTARG" ;;
    s) CLIENT_SECRET="$OPTARG" ;;
    i) ISSUER="$OPTARG"; ISSUER_SET=true ;;
    D) VIA_DOCKER=true ;;
    h) usage ;;
    -) [ "$OPTARG" = "docker" ] && VIA_DOCKER=true || usage ;;
    *) usage ;;
  esac
done

[ -z "$USERNAME" ] && usage

if [ "$VIA_DOCKER" = true ] && [ "$ISSUER_SET" = false ]; then
  ISSUER="http://keycloak:8080/realms/myrealm"
fi

if [ "$VIA_DOCKER" = true ]; then
  RESPONSE=$(docker compose exec -T bff python3 -c "
import urllib.request, urllib.parse, json
data = urllib.parse.urlencode({
    'client_id': '$CLIENT_ID',
    'client_secret': '$CLIENT_SECRET',
    'grant_type': 'password',
    'username': '$USERNAME',
    'password': '$PASSWORD',
}).encode()
req = urllib.request.Request('$ISSUER/protocol/openid-connect/token', data=data)
print(json.load(urllib.request.urlopen(req)).get('access_token', ''))
")
else
  RESPONSE=$(curl -s -X POST \
    "$ISSUER/protocol/openid-connect/token" \
    -d "client_id=$CLIENT_ID" \
    -d "client_secret=$CLIENT_SECRET" \
    -d "grant_type=password" \
    -d "username=$USERNAME" \
    -d "password=$PASSWORD")
fi

if [ "$VIA_DOCKER" = true ]; then
  TOKEN=$(echo "$RESPONSE" | tr -d '\r')
else
  TOKEN=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
fi

if [ -z "$TOKEN" ]; then
  echo "Failed to get token. Response:" >&2
  echo "$RESPONSE" >&2
  exit 1
fi

echo "$TOKEN"
