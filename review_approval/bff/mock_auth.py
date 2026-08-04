"""
Mock auth for the POC UI ONLY. Trusts whatever role/username the
browser's session cookie says -- no password, no identity verification
of any kind. This exists purely so you can demo the UI without standing
up Keycloak first.

DO NOT reuse this for the JSON API in main.py (that already uses real
Keycloak JWTs via auth.py) and DO NOT deploy this beyond a local POC.

Swap-out path to real auth later: replace `login()`'s "trust the
submitted role" with an actual Keycloak Authorization Code flow, keep
storing the same {"username", "role"} shape in the session so ui.py
doesn't need to change.
"""

from fastapi import HTTPException, Request

SESSION_KEY = "user"
VALID_ROLES = ("operator", "manager")


class RequireLoginRedirect(HTTPException):
    """Raised when no session exists; main.py registers a handler that
    turns this into a 303 redirect to the login page."""

    def __init__(self) -> None:
        super().__init__(status_code=303, headers={"Location": "/ui/login"})


def login(request: Request, username: str, role: str) -> None:
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES}")
    if not username.strip():
        raise ValueError("username is required")
    request.session[SESSION_KEY] = {"username": username.strip(), "role": role}


def logout(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)


def get_session_user(request: Request) -> dict:
    user = request.session.get(SESSION_KEY)
    if not user:
        raise RequireLoginRedirect()
    return user


def require_session_role(role: str):
    """FastAPI dependency factory: require_session_role("operator")"""

    def checker(request: Request) -> dict:
        user = get_session_user(request)
        if user["role"] != role:
            raise HTTPException(status_code=403, detail=f"requires role: {role}")
        return user

    return checker
