"""
Usage-event logging to Firestore.

Fire-and-forget by design: this must never affect an actual API response.
Every failure is caught and printed, never raised. The caller (the
middleware in main.py) runs this on a background thread so a Firestore
round-trip never adds latency to a module request.
"""
import time
from typing import Optional

from app.auth import get_firebase_app, verify_token

_db = None
_db_init_attempted = False


def _get_db():
    global _db, _db_init_attempted
    if _db is not None:
        return _db
    if _db_init_attempted:
        return None
    _db_init_attempted = True

    app = get_firebase_app()
    if app is None:
        return None
    try:
        from firebase_admin import firestore
        _db = firestore.client(app)
    except Exception as e:
        print(f"[usage] Firestore client init failed: {e}")
        _db = None
    return _db


# Maps a request path prefix to the module name, mirroring the frontend's
# trackModuleAction() module names so web + API usage line up in Firestore.
_MODULE_BY_PREFIX = {
    "/api/predict": "tactics",
    "/api/lineup": "tactics",
    "/api/form": "opponent",
    "/api/chat": "chat",
    "/api/live": "chat",
    "/api/squad": "sandbox",
    "/api/fpl": "fpl",
    "/api/nations": "nations",
}


def _module_for_path(path: str) -> Optional[str]:
    for prefix, module in _MODULE_BY_PREFIX.items():
        if path.startswith(prefix):
            return module
    return None


def log_usage(path: str, method: str, status_code: int, authorization: Optional[str]) -> None:
    """Best-effort usage log for one request. Never raises — any failure
    here must never surface to the client, which has already gotten its
    response by the time this runs."""
    try:
        if status_code >= 400:
            return  # only count successful module hits
        module = _module_for_path(path)
        if module is None:
            return  # not a tracked module endpoint (e.g. /api/health, /api/admin)

        db = _get_db()
        if db is None:
            return  # Firebase not configured in this environment — no-op

        user = verify_token(authorization)
        db.collection("usage_events").add({
            "module": module,
            "path": path,
            "method": method,
            "user_id": user["uid"] if user else "anon",
            "email": (user or {}).get("email"),
            "timestamp": time.time(),
        })
    except Exception as e:
        print(f"[usage] log_usage failed: {e}")
