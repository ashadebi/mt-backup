import os
import secrets
import bcrypt
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse


def _truncate(password: str) -> bytes:
    """bcrypt has a 72-byte limit; truncate deterministically."""
    return password.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_truncate(password), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_truncate(password), hashed.encode("utf-8"))
    except Exception:
        return False


def verify_login(username: str, password: str) -> dict | None:
    """Verify credentials. Try DB users first, fallback to env admin.

    Returns user dict {id, username, role} or None.
    Env admin has no id (id=None) and role='admin'.
    """
    from app import database
    # Try DB users first
    try:
        u = database.get_user_by_username(username)
        if u and u.get("enabled"):
            if database.verify_user_password(u, password):
                return {"id": u["id"], "username": u["username"], "role": u["role"]}
        elif u and not u.get("enabled"):
            return None  # explicitly disabled
    except Exception:
        pass
    # Fallback to env admin
    expected_user = os.environ.get("MT_ADMIN_USERNAME", "admin")
    if username != expected_user:
        return None
    expected_hash = os.environ.get("MT_ADMIN_PASSWORD_HASH", "")
    if not expected_hash or not verify_password(password, expected_hash):
        return None
    return {"id": None, "username": expected_user, "role": "admin"}


def verify_admin(username: str, password: str) -> bool:
    """Legacy: single admin via env. Returns True if matches env admin."""
    expected_user = os.environ.get("MT_ADMIN_USERNAME", "admin")
    expected_hash = os.environ.get("MT_ADMIN_PASSWORD_HASH", "")
    if not expected_hash:
        return False
    if username != expected_user:
        return False
    return verify_password(password, expected_hash)


def current_user(request: Request) -> dict | None:
    return request.session.get("user")


def require_login(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def login_response(request: Request, user: dict) -> RedirectResponse:
    """Set session user (dict with id, username, role) and redirect to /."""
    request.session["user"] = user
    return RedirectResponse(url="/", status_code=303)


def logout_response(request: Request) -> RedirectResponse:
    request.session.clear()
    response = RedirectResponse(url="/login", status_code=303)
    return response
