"""
FastAPI server for the artintele.ai Kannada tele-caller + demo dashboard.

Endpoints:
  GET  /              -> the demo control panel (static/index.html)
  POST /api/campaign  -> set the live campaign context the AI will speak on
  GET  /api/campaign  -> read current context
  POST /api/call      -> place an outbound call (JSON: {"to": "+91..."} )
  WS   /events        -> live transcript + status stream for the dashboard
  POST /twiml         -> TwiML that opens the Twilio Media Stream to /ws
  WS   /ws            -> Twilio Media Stream; handed to the Pipecat bot

Run:  uvicorn server:app --host 0.0.0.0 --port 8000
Then: ngrok http 8000  (put the https host in PUBLIC_HOST in .env)
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from twilio.rest import Client

from app_state import hub
from bot import run_bot

load_dotenv()

app = FastAPI()
STATIC_DIR = Path(__file__).parent / "static"

PUBLIC_HOST = os.getenv("PUBLIC_HOST", "").replace("https://", "").replace("http://", "").rstrip("/")


# ---------- Demo dashboard ----------
@app.get("/")
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/campaign")
async def get_campaign():
    return JSONResponse({"campaign": hub.campaign, "status": hub.status})


@app.post("/api/campaign")
async def set_campaign(request: Request):
    data = await request.json()
    hub.campaign.update(data)
    await hub.publish({"type": "status", "status": "context-updated"})
    return JSONResponse({"ok": True, "campaign": hub.campaign})


@app.post("/api/call")
async def start_call(request: Request):
    body = await request.json()
    to_number = body.get("to") or hub.campaign.get("CUSTOMER", {}).get("phone")
    if not to_number:
        return JSONResponse({"ok": False, "error": "no 'to' number"}, status_code=400)
    try:
        client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
        call = client.calls.create(
            to=to_number,
            from_=os.getenv("TWILIO_PHONE_NUMBER"),
            url=f"https://{PUBLIC_HOST}/twiml",
        )
        await hub.publish({"type": "status", "status": "dialing"})
        return JSONResponse({"ok": True, "sid": call.sid, "to": to_number})
    except Exception as e:  # surface Twilio errors to the demo UI
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.websocket("/events")
async def events(ws: WebSocket):
    """Stream transcript + status events to the dashboard."""
    await ws.accept()
    q = hub.subscribe()
    try:
        await ws.send_json({"type": "status", "status": hub.status})
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(q)


# ---------- Twilio voice ----------
@app.post("/twiml")
async def twiml(_request: Request):
    stream_url = f"wss://{PUBLIC_HOST}/ws"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{stream_url}" />
  </Connect>
</Response>"""
    return HTMLResponse(content=xml, media_type="application/xml")


@app.websocket("/ws")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    iterator = websocket.iter_text()
    await iterator.__anext__()                          # Twilio "connected"
    start_msg = json.loads(await iterator.__anext__())  # Twilio "start"
    stream_sid = start_msg["start"]["streamSid"]
    call_sid = start_msg["start"]["callSid"]

    # Use whatever context the demo operator set in the dashboard.
    await run_bot(websocket, stream_sid, call_sid, campaign=dict(hub.campaign))


@app.get("/health")
async def health():
    return {"ok": True}
