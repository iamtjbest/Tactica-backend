"""
POST /api/chat
Body: { my_team, opp_team, message, history[], live_context?, squad? }
Returns: { reply }
"""
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import google.generativeai as genai
from app.config import GEMINI_KEY

router = APIRouter()

if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
    _model = genai.GenerativeModel("gemini-2.5-flash")
else:
    _model = None

class ChatMessage(BaseModel):
    role:    str   # "user" | "assistant"
    content: str

class ChatRequest(BaseModel):
    my_team:      str
    opp_team:     str
    message:      str
    history:      list[ChatMessage] = []
    live_context: str | None = None
    squad:        list[dict] | None = None

@router.post("/chat")
def chat(body: ChatRequest):
    if not _model:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY not configured.")

    live_status = body.live_context or "No live match data. Provide pre-match tactical advice."
    squad_text  = json.dumps(body.squad or [], ensure_ascii=False) if body.squad else "Squad data unavailable."

    history_text = "\n".join(
        f"{'Coach' if m.role == 'user' else 'Assistant'}: {m.content}"
        for m in body.history
    ) or "Start of briefing."

    system = f"""You are an elite AI Assistant Football Manager.
You assist the Head Coach of {body.my_team}, currently facing {body.opp_team}.

LIVE MATCH STATUS:
{live_status}

OUR SQUAD (Name | Position | Minutes | Goals+Assists):
{squad_text}

CONVERSATION HISTORY:
{history_text}

INSTRUCTIONS:
- Address the Head Coach directly. Be concise, tactical, professional.
- If LIVE MATCH DATA is present, anchor ALL advice to the current score and minute.
- No live data → sharp pre-match tactical advice.
- Reference ONLY players from the squad above. Never invent player names.
- Use football terminology: press triggers, half-spaces, double pivot, low block, etc.
- 3–6 sentences unless a detailed breakdown is explicitly requested.
"""

    try:
        resp  = _model.generate_content(f"{system}\n\nCoach: {body.message}")
        reply = resp.text
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini error: {e}")

    return {"reply": reply}
