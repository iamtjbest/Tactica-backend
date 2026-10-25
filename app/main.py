"""
Tactica AI Engine — FastAPI Backend
====================================
All endpoints the web app and mobile app consume.
Hosted on Railway (free tier). No Streamlit dependency.

Endpoints:
  POST /api/predict         → ML formation prediction
  POST /api/lineup          → Starting XI selection
  POST /api/chat            → Gemini AI tactical chat
  GET  /api/live            → BSD live match proxy + cache
  GET  /api/squad           → On-demand BSD squad fetch
  GET  /api/form            → Last 5 matches + dynamic ratings
  GET  /api/nations/squads  → World Cup national team squads
  GET  /api/nations/predict → Formation prediction for national teams
  GET  /api/health          → Health check
"""

import os
import asyncio
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from app.routers import predict, lineup, chat, live, squad, form, nations, fpl
from app.config import clear_cache
from app.usage import log_usage

app = FastAPI(
    title="Tactica AI Engine",
    description="Football Tactical Intelligence API — powered by BSD, Gemini & ML",
    version="2.0.0",
)

# SINGLE, CORRECT CORS CONFIGURATION
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.tacticaengine.com",   # the engine (production)
        "https://tacticaengine.com",       # marketing site
        "https://www.tacticaengine.com",   # marketing site (www)
        "https://tactica-backend-hdbd.onrender.com",
    ],
    # Also allow any Vercel preview deployment URL (changes every push)
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Usage tracking — runs after every request, logs to Firestore in the
# background (via a thread), never touches the actual response. If
# FIREBASE_SERVICE_ACCOUNT_JSON isn't set, log_usage no-ops silently, so
# this is safe to deploy even before that env var exists.
@app.middleware("http")
async def usage_tracking_middleware(request: Request, call_next):
    response = await call_next(request)
    asyncio.create_task(asyncio.to_thread(
        log_usage,
        request.url.path,
        request.method,
        response.status_code,
        request.headers.get("authorization"),
    ))
    return response


app.include_router(predict.router,    prefix="/api")
app.include_router(lineup.router,     prefix="/api")
app.include_router(chat.router,       prefix="/api")
app.include_router(live.router,       prefix="/api")
app.include_router(squad.router,      prefix="/api")
app.include_router(form.router,       prefix="/api")
app.include_router(nations.router,    prefix="/api")
app.include_router(fpl.router,        prefix="/api")

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Tactica AI Engine v2"}

@app.delete("/api/admin/cache")
def clear_cache_endpoint(
    key: str = Query(..., description="Must match the ADMIN_KEY env var"),
    prefix: str = Query("", description="Only clear keys starting with this, e.g. 'form_v7__'. Empty = clear everything."),
):
    """Manually bust the file cache without waiting out the TTL or redeploying.
    Set ADMIN_KEY in Render's env vars, then call:
    DELETE /api/admin/cache?key=YOUR_ADMIN_KEY
    DELETE /api/admin/cache?key=YOUR_ADMIN_KEY&prefix=form_v7__   (just /form entries)
    """
    admin_key = os.environ.get("ADMIN_KEY", "")
    if not admin_key or key != admin_key:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key.")
    deleted = clear_cache(prefix)
    return {"status": "ok", "deleted": deleted, "prefix": prefix or "(all)"}
