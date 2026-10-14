"""
Firebase Admin init + optional token verification.

This is NOT an access-control gate — every module endpoint stays fully
usable with no sign-in. All it does is let usage logging attribute a
hit to a real user when a valid Firebase ID token is present, and fall
back to "anon" when it isn't.

Reads the service account JSON from the FIREBASE_SERVICE_ACCOUNT_JSON
env var (set on Render — never commit the JSON file itself).
"""
import os
import json
import threading
from typing import Optional

import firebase_admin
from firebase_admin import credentials, auth as fb_auth

_lock = threading.Lock()
_app = None
_init_attempted = False


def get_firebase_app():
    """Lazily initialise the Firebase Admin app. Returns None (and logs once)
    if FIREBASE_SERVICE_ACCOUNT_JSON isn't set or is invalid — usage tracking
    then silently no-ops rather than breaking anything."""
    global _app, _init_attempted
    if _app is not None:
        return _app
    with _lock:
        if _app is not None:
            return _app
        if _init_attempted:
            return None
        _init_attempted = True

        raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
        if not raw:
            print("[auth] FIREBASE_SERVICE_ACCOUNT_JSON not set — usage tracking disabled")
            return None
        try:
            cred_dict = json.loads(raw)
            cred = credentials.Certificate(cred_dict)
            _app = firebase_admin.initialize_app(cred)
            print("[auth] Firebase Admin initialised")
        except Exception as e:
            print(f"[auth] Firebase Admin init failed: {e}")
            _app = None
        return _app


def verify_token(authorization_header: Optional[str]) -> Optional[dict]:
    """Returns the decoded token (has 'uid', 'email', etc.) or None if the
    header is missing, malformed, the token is invalid/expired, or Firebase
    isn't configured in this environment. Never raises."""
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None
    app = get_firebase_app()
    if app is None:
        return None
    token = authorization_header.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        return fb_auth.verify_id_token(token, app=app)
    except Exception:
        return None
