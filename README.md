# Tactica AI Engine — FastAPI Backend

## Stack
- **FastAPI** — REST API framework
- **Railway** — hosting (free tier, persistent storage)
- **BSD API** — football data (live scores, squads, formations, World Cup)
- **Gemini API** — AI tactical chat
- **scikit-learn** — ML formation prediction

## Project Structure
```
tactica-backend/
├── app/
│   ├── main.py              # FastAPI app + CORS
│   ├── config.py            # BSD client, position maps, cache helpers
│   ├── ml_model.py          # Model loader + formation scorer
│   ├── xi_selector.py       # Starting XI selection logic
│   ├── national_ratings.py  # World Cup player/team scoring engine
│   └── routers/
│       ├── predict.py       # POST /api/predict
│       ├── lineup.py        # POST /api/lineup
│       ├── chat.py          # POST /api/chat
│       ├── live.py          # GET  /api/live
│       ├── squad.py         # GET  /api/squad
│       ├── form.py          # GET  /api/form
│       └── nations.py       # GET/POST /api/nations/*
├── requirements.txt
├── Procfile
└── railway.json
```

## API Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/api/health` | Health check |
| POST | `/api/predict` | Formation recommendation |
| POST | `/api/lineup` | Starting XI selection |
| POST | `/api/chat` | Gemini AI tactical chat |
| GET | `/api/live?home=X&away=Y` | BSD live match proxy |
| GET | `/api/squad?team=X` | On-demand squad fetch + cache |
| GET | `/api/form?team=X` | Last 5 matches + dynamic ratings |
| GET | `/api/nations/squads` | World Cup squads |
| GET | `/api/nations/squads/{id}` | One nation's squad |
| POST | `/api/nations/predict` | National team formation prediction |

## Deploy to Railway

1. Push this folder to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Add environment variables:
   ```
   BSD_API_KEY=your_bsd_key
   GEMINI_API_KEY=your_gemini_key
   MODEL_PATH=tactical_model.pkl
   TEAMS_PATH=teams.json
   PLAYERS_PATH=players.json
   CACHE_DIR=/tmp/tactica_cache
   ```
4. Copy `tactical_model.pkl`, `teams.json`, `players.json` to the repo root
5. Railway auto-deploys on every push

## Local Development
```bash
pip install -r requirements.txt
BSD_API_KEY=xxx GEMINI_API_KEY=xxx uvicorn app.main:app --reload
# Docs at: http://localhost:8000/docs
```

## National Team Rating Formula
```
Player Score = (Form × 0.35) + (Quality × 0.30) + (Experience × 0.20) + (Age × 0.15)

Form       = goals+assists per 90 from last club season (scaled 0-100)
Quality    = BSD average match rating last 10 games (scaled 0-100)
Experience = international caps (0-100, capped at 100)
Age        = peak factor: 26-29 = 1.0, scales down from there

Final score × league_weight (Premier League = 1.0, down to 0.74 for outside Europe)

Team Attack  = avg top 4 FW/MF player scores × league_weight
Team Defence = avg top 4 DF player scores    × league_weight
```
