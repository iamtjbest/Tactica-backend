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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import predict, lineup, chat, live, squad, form, nations, fpl

app = FastAPI(
    title="Tactica AI Engine",
    description="Football Tactical Intelligence API — powered by BSD, Gemini & ML",
    version="2.0.0",
)

# SINGLE, CORRECT CORS CONFIGURATION
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.tactica.com.ng",      # the engine (production)
        "https://tactica.com.ng",          # marketing site
        "https://www.tactica.com.ng",      # marketing site (www)
        "http://localhost:3000",           # local dev testing
    ],
    # Also allow any Vercel preview deployment URL (changes every push)
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
