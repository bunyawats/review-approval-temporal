"""
Keycloak JWT validation + permission-check dependency for the JSON API.

Temporal has zero concept of roles or permissions -- this is the sole
enforcement point for this front door. The app checks *permissions*
(Keycloak Resources: Create_Request, Update_Request, Cancel_Request,
Approve_Request, Reject_Request), never role names -- see
workflow/keycloak_auth.py for the actual JWT/UMA mechanics this wraps,
and CLAUDE.md's api/auth.py bullet + the keycloak-admin skill for the
general Resources/Policies/Permissions model and why a plain JWT claim
read isn't enough.

KEYCLOAK_ISSUER/KEYCLOAK_CLIENT_ID/KEYCLOAK_CLIENT_SECRET are all read
lazily (on first real call), not at import time -- see
workflow/keycloak_auth.py. That lets the app start fine even with no
Keycloak running -- only an actual call to a Keycloak-protected route
(any JSON API route, or /ui/* once someone tries to log in or hit a
permission-gated action) fails, with a clear 503, if it's unconfigured.
"""

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from review_approval.workflow import keycloak_auth

_bearer = HTTPBearer()


def _decode_token(token: str) -> dict:
    try:
        return keycloak_auth.decode_token(token)
    except RuntimeError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Keycloak is not configured. ({e})",
        )
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}")


def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    payload = _decode_token(creds.credentials)
    username = payload.get("preferred_username", payload.get("sub"))
    return {"sub": username, "access_token": creds.credentials}


async def check_permission(user: dict, permission: str) -> None:
    """Raise HTTPException if `user` doesn't have `permission`.

    Callable directly from inside a route body (for routes that need to
    pick the required permission based on the request body -- see
    submit_decision in routes.py, which needs Approve_Request or
    Reject_Request depending on the submitted decision, something a
    single Depends() can't express), or via the require_permission()
    dependency factory below for the common single-fixed-permission
    case.
    """
    try:
        granted = await keycloak_auth.get_permissions(user["access_token"])
    except keycloak_auth.TokenInvalid as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}")
    except keycloak_auth.PermissionCheckError as e:
        raise HTTPException(
            status_code=503, detail=f"permission check failed: {e}"
        )
    if permission not in granted:
        raise HTTPException(
            status_code=403, detail=f"requires permission: {permission}"
        )


def require_permission(permission: str):
    """FastAPI dependency factory: require_permission("Create_Request")"""

    async def checker(user: dict = Depends(get_current_user)) -> dict:
        await check_permission(user, permission)
        return user

    return checker
