"""
Keycloak JWT validation for the BFF.

Temporal never sees roles -- the BFF is the sole enforcement point.
Expects standard Keycloak realm roles in the token's realm_access.roles
claim (Operator, Manager). Adjust the extraction if you're using
client-scoped roles instead (resource_access.<client>.roles).

KEYCLOAK_ISSUER is read lazily (on first real JSON API call), not at
import time. That lets the app start and serve /ui/* (mock auth) fine
even with no Keycloak running -- only an actual call to a Keycloak-
protected JSON endpoint fails, with a clear 503, if it's unconfigured.
"""

import os

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

_bearer = HTTPBearer()
_jwk_client: PyJWKClient | None = None


def _keycloak_issuer() -> str:
    issuer = os.environ.get("KEYCLOAK_ISSUER")
    if not issuer:
        raise HTTPException(
            status_code=503,
            detail="Keycloak is not configured (KEYCLOAK_ISSUER unset) -- "
            "the JSON API requires it. The /ui/* mock-auth screens don't.",
        )
    return issuer


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        issuer = _keycloak_issuer()
        _jwk_client = PyJWKClient(f"{issuer}/protocol/openid-connect/certs")
    return _jwk_client


def _decode_token(token: str) -> dict:
    try:
        jwk_client = _get_jwk_client()
        signing_key = jwk_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=_keycloak_issuer(),
            options={"verify_aud": False},  # set an audience and verify it in production
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}")


def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    payload = _decode_token(creds.credentials)
    roles = payload.get("realm_access", {}).get("roles", [])
    username = payload.get("preferred_username", payload.get("sub"))
    return {"sub": username, "roles": roles}


def require_role(role: str):
    """FastAPI dependency factory: require_role("operator")"""

    def checker(user: dict = Depends(get_current_user)) -> dict:
        if role not in user["roles"]:
            raise HTTPException(status_code=403, detail=f"requires role: {role}")
        return user

    return checker
