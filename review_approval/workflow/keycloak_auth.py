"""
Low-level Keycloak integration shared by both front doors: JWT
validation and permission checks via a UMA ticket exchange. Neither
api/auth.py nor bff's real-auth module talk to Keycloak directly --
both go through here, so the two front doors can't drift apart on how a
token gets validated or a permission gets checked.

Deliberately framework-agnostic (no FastAPI imports, no HTTPException) --
each front door maps failures from here into its own response style
(api/auth.py -> HTTP status codes and JSON bodies; bff -> redirects to
login or a re-rendered error fragment).

The five permissions (Create, Update, Cancel, Approve, Reject) are
Authorization Services Scopes on a single "RequestApproval" Resource on
the "review-approval" client, each gated by a role-based Policy via its
own scope-type Permission -- not realm roles. They never appear in a
plain access token's realm_access.roles claim; get_permissions() below
is what actually checks them, via a UMA ticket exchange, a real (not
cached) call to Keycloak per check. See the keycloak-admin skill and
keycloak/import/myrealm-realm.json for the full structure.
"""

import os

import httpx
import jwt
from jwt import PyJWKClient

_jwk_client: PyJWKClient | None = None


def _keycloak_issuer() -> str:
    issuer = os.environ.get("KEYCLOAK_ISSUER")
    if not issuer:
        raise RuntimeError("KEYCLOAK_ISSUER is not set")
    return issuer


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(f"{_keycloak_issuer()}/protocol/openid-connect/certs")
    return _jwk_client


def decode_token(token: str) -> dict:
    """Validate a Keycloak-issued JWT's signature and issuer, returning
    its claims.

    Raises jwt.PyJWTError (or a subclass -- ExpiredSignatureError,
    InvalidSignatureError, InvalidIssuerError, etc.) on any validation
    failure. Callers catch jwt.PyJWTError, not a specific subclass,
    unless they need to distinguish reasons.
    """
    jwk_client = _get_jwk_client()
    signing_key = jwk_client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=_keycloak_issuer(),
        leeway=5,  # tolerate small clock skew between this host and Keycloak on iat/exp/nbf
        options={"verify_aud": False},  # set an audience and verify it in production
    )


class TokenInvalid(Exception):
    """The bearer token itself was rejected during a UMA ticket exchange
    (expired, malformed, wrong signature, etc.) -- an authentication
    failure, not an authorization one. Confirmed empirically: Keycloak
    returns 401 {"error": "invalid_grant", "error_description":
    "Invalid bearer token"} for this case."""


class PermissionCheckError(Exception):
    """The UMA ticket exchange failed for an infrastructure reason
    (Keycloak unreachable, misconfigured client, unexpected response
    shape) -- distinct from both TokenInvalid and a clean "granted:
    set()" empty-permissions result."""


async def get_permissions(access_token: str) -> set[str]:
    """Return the set of Scope names (e.g. "Create") this access token is
    currently granted on the "RequestApproval" resource, via a UMA
    ticket exchange against the "review-approval" resource server.

    Confirmed empirically (not assumed from docs) against a real
    instance: response_mode=permissions returns one entry per *resource*
    -- here always exactly one, since there's only one resource -- with a
    "scopes" list of every granted scope name on it, e.g.
    `[{"rsid": "...", "rsname": "RequestApproval", "scopes": ["Create",
    "Update"]}]`. Flattening every entry's "scopes" (rather than reading
    "rsname") is what makes this scope-level rather than resource-level.

    A token with zero granted permissions is NOT an error -- confirmed
    empirically against a real instance (not assumed from docs):
    Keycloak returns 403 {"error": "access_denied", "error_description":
    "not_authorized"} for that case, which this function turns into an
    empty set. An actually-invalid token gets a *different* response
    (401 {"error": "invalid_grant"}), raised here as TokenInvalid so
    callers can tell "not logged in" apart from "logged in, no
    permission" -- important, since api/auth.py and bff both need to
    react differently to the two (401-style vs 403-style responses).
    """
    client_id = os.environ.get("KEYCLOAK_CLIENT_ID")
    client_secret = os.environ.get("KEYCLOAK_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "KEYCLOAK_CLIENT_ID/KEYCLOAK_CLIENT_SECRET are not set"
        )

    url = f"{_keycloak_issuer()}/protocol/openid-connect/token"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:uma-ticket",
                    "audience": client_id,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "response_mode": "permissions",
                },
            )
    except httpx.HTTPError as e:
        raise PermissionCheckError(f"could not reach Keycloak: {e}") from e

    if response.status_code == 401:
        raise TokenInvalid(response.text)

    if response.status_code == 403:
        body = response.json()
        if body.get("error") == "access_denied":
            return set()
        raise PermissionCheckError(f"unexpected 403 from Keycloak: {body}")

    if response.status_code != 200:
        raise PermissionCheckError(
            f"unexpected {response.status_code} from Keycloak: {response.text}"
        )

    granted: set[str] = set()
    for perm in response.json():
        granted.update(perm.get("scopes", []))
    return granted
