# artintele.ai — Bengaluru Kannada (Kanglish) Voice Tele-Caller

An outbound/inbound phone agent that speaks **casual Bangalore-city Kannada mixed with English**,
built on Twilio + Sarvam (STT & TTS) + Gemini, orchestrated by Pipecat.

```
Caller ⇄ Twilio PSTN ⇄ Media Stream (wss) ⇄ Pipecat
                                              │
                     Sarvam Saarika STT → Gemini 2.5 Flash → Sarvam Bulbul TTS
                        (kn-IN, code-mix)   (dialect prompt)   (kn-IN, locked speaker)
```

## Files
| File | What it is |
|---|---|
| `system_prompt_bengaluru_kannada_telecaller.md` | The agent's brain: dialect rules, guardrails, call flow. |
| `bangalore_kannada_lexicon.json` | Bangalore vocab injected into context (anti-drift anchor). |
| `bot.py` | Builds the system instruction + the Pipecat pipeline (+ live transcript). |
| `server.py` | FastAPI: demo dashboard, campaign API, live-events WS, TwiML, media stream. |
| `static/index.html` | **Demo console** — set call context, place the call, watch the live transcript. |
| `app_state.py` | Shared campaign context + pub/sub event bus (dashboard ⇄ call). |
| `STACK_and_ARCHITECTURE.md` | Design rationale, config knobs, compliance. |
| `.env.example` | Copy to `.env` and fill with your **rotated** keys. |

## ⚠️ Before anything: rotate the keys you pasted in chat
The Sarvam, Gemini, and Twilio credentials shared in chat are compromised. Regenerate all three,
then put the NEW values in `.env`. Never commit `.env` (it's git-ignored).

## Setup
```bash
cd "Sarvam kannada"
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # then edit .env with rotated keys + PUBLIC_HOST
```

## Run (local test)
```bash
# 1) start the server
uvicorn server:app --host 0.0.0.0 --port 8000

# 2) in another terminal, expose it publicly
ngrok http 8000
#    copy the https host (e.g. abc123.ngrok.app) into PUBLIC_HOST in .env, restart uvicorn
```

### Inbound calls
In the Twilio console, set your number's **Voice → "A call comes in"** webhook to:
`https://<PUBLIC_HOST>/twiml` (HTTP POST).

### Demo console (for client demos)
Open **`http://localhost:8000/`** in a browser. From there you can:
- fill in the **call context** (customer, goal, offer, key facts, what-not-to-say),
- pick the **Sarvam voice/model**,
- hit **Call now** to dial the customer,
- watch the **live conversation** stream in as the AI and customer speak.

The context you set drives what the AI actually says on that call. For a client demo,
put the client's own product facts in the "Key facts" box, save, and call your own phone.

### Outbound calls (API, if you prefer curl)
```bash
curl -X POST https://<PUBLIC_HOST>/api/call \
  -H "Content-Type: application/json" \
  -d '{"to": "+9198XXXXXXXX"}'
```

## Picking the voice (matching your reference WAVs)
Sarvam TTS has **fixed preset speakers — it does not clone your uploaded audio.** To match the
reference recordings:
1. Audition female `kn-IN` speakers (`bulbul:v2`: `anushka`, `manisha`, `vidya`; also try `bulbul:v3`).
2. Set the closest one in `.env` → `SARVAM_TTS_SPEAKER`.
3. Tune `pace` / `pitch` / `loudness` in `bot.py` until it matches, using the WAVs as A/B reference.

## Tuning the dialect
Edit `system_prompt_bengaluru_kannada_telecaller.md` (rules) and `bangalore_kannada_lexicon.json`
(vocab). Terms tagged `"speak": false` are understood-but-never-spoken (too street for a pro call).
No code change needed — both are loaded fresh at call start.

## Known caveats
- **Pipecat API drift:** imports target `pipecat-ai==0.0.95`. If an import errors after install,
  check the installed version's docs and adjust the `pipecat.services.*` paths.
- Twilio audio is 8kHz mu-law; sample rates are set accordingly in `bot.py`.
- This is a working scaffold — test on a real call and tune VAD/latency before production.
- **Compliance (India):** honor DND/opt-out, identify the company at call start, respect calling
  hours (TRAI), and never collect OTP/CVV over voice. The prompt enforces these; keep it that way.
```
