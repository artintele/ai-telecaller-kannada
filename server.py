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

import base64
import json
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from twilio.rest import Client

from app_state import hub
from bot import run_bot

load_dotenv()

app = FastAPI()
STATIC_DIR = Path(__file__).parent / "static"

# The dashboard can place real, billed calls to any number, so on a public host it
# must not be open. HTTP Basic on everything EXCEPT the paths the telephony provider
# itself hits (/ws media stream, /twiml) and /health. Browsers resend Basic creds on
# the same-origin /events websocket, so the live transcript keeps working.
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
_OPEN_PATHS = {"/ws", "/twiml", "/health"}


@app.middleware("http")
async def require_dashboard_login(request: Request, call_next):
    if not DASHBOARD_PASSWORD or request.url.path in _OPEN_PATHS:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            user, pwd = "", ""
        if secrets.compare_digest(user, DASHBOARD_USER) and secrets.compare_digest(pwd, DASHBOARD_PASSWORD):
            return await call_next(request)
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="telecaller"'})

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
    # The dashboard's "Call now" posts here; route to whichever provider is live.
    if os.getenv("TELEPHONY_PROVIDER", "twilio").lower() == "exotel":
        return await start_call_exotel(request)
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

    # Both Twilio and Exotel may open with {"event":"connected"}; skip anything until
    # "start". Twilio's start carries camelCase streamSid/callSid, Exotel's snake_case
    # stream_sid/call_sid (Exotel streams 8kHz PCM, Twilio 8kHz mu-law).
    msg = json.loads(await iterator.__anext__())
    while msg.get("event") != "start":
        msg = json.loads(await iterator.__anext__())
    start = msg.get("start", {})
    if "stream_sid" in start or "stream_sid" in msg:
        provider = "exotel"
        stream_sid = start.get("stream_sid") or msg.get("stream_sid")
        call_sid = start.get("call_sid")
    else:
        provider = "twilio"
        stream_sid = start["streamSid"]
        call_sid = start["callSid"]

    await run_bot(websocket, stream_sid, call_sid, campaign=dict(hub.campaign), provider=provider)


@app.post("/api/call-exotel")
async def start_call_exotel(request: Request):
    """Place an outbound call via Exotel (India). Body: {"to": "+9198XXXXXXXX"}

    Requires EXOTEL_FLOW_URL (the App/flow that contains a Voicebot bidirectional-
    streaming applet pointing at wss://<PUBLIC_HOST>/ws) and EXOTEL_CALLER_ID (your
    ExoPhone). Set both in .env after creating the flow in the Exotel dashboard.
    """
    import httpx

    body = await request.json()
    to_number = body.get("to") or hub.campaign.get("CUSTOMER", {}).get("phone")
    caller_id = os.getenv("EXOTEL_CALLER_ID")
    flow_url = os.getenv("EXOTEL_FLOW_URL")
    if not (to_number and caller_id and flow_url):
        return JSONResponse(
            {"ok": False, "error": "need 'to', EXOTEL_CALLER_ID and EXOTEL_FLOW_URL set"},
            status_code=400,
        )
    sid = os.getenv("EXOTEL_SID")
    subdomain = os.getenv("EXOTEL_SUBDOMAIN", "api.exotel.com")
    url = f"https://{subdomain}/v1/Accounts/{sid}/Calls/connect.json"
    auth = (os.getenv("EXOTEL_API_KEY"), os.getenv("EXOTEL_API_TOKEN"))
    data = {"From": to_number, "CallerId": caller_id, "Url": flow_url}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, data=data, auth=auth)
        ok = r.status_code < 300
        await hub.publish({"type": "status", "status": "dialing (exotel)" if ok else "exotel error"})
        return JSONResponse({"ok": ok, "status_code": r.status_code, "body": r.text[:500]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/health")
async def health():
    return {"ok": True}
