# artintele.ai — Bengaluru-Kannada Voice Tele-Caller · Architecture & Cost

> Authoritative current-state doc (supersedes the early `STACK_and_ARCHITECTURE.md` planning notes).
> Last updated: 2026-07-03.

A production-oriented outbound/inbound phone agent that speaks **casual Bengaluru "Kanglish"**
(spoken Kannada naturally code-mixed with English). After extensive testing, the winning
configuration is **Gemini Live native audio** for the voice — it was judged (by ear, on live
calls) to speak *better* Bangalore Kannada than the India-specialised Sarvam TTS.

---

## 1. TL;DR

- **Voice brain + voice:** Google **Gemini Live API** (native audio model, speaks directly — no separate TTS).
- **Telephony:** Twilio today; **Exotel** recommended for India (roughly half the per-minute cost).
- **Orchestration:** **Pipecat 0.0.108** (pinned — 1.x is a breaking rewrite).
- **Demo UI:** FastAPI dashboard with live status + context editor.
- **Cost (India, Gemini + Exotel):** **≈ ₹2.5 / min (~$0.03)** for typical short calls. See §7.
- **Key latency fix:** disable Gemini "thinking" (`thinking_budget=0`) → response time dropped from **6–17 s to ~1 s**.

---

## 2. Runtime architecture

The system has a **`VOICE_ENGINE` toggle** (`.env`) selecting one of three pipelines:

### `gemini-direct` — CURRENT / RECOMMENDED (lowest latency, best voice)
```
Caller ⇄ Twilio/Exotel ⇄ Media Stream (wss, via cloudflared) ⇄ Pipecat
                                                                  │
                                            raw caller audio ─────┤
                                                                  ▼
                                                   Gemini Live (native audio)
                                                   hears Kannada + speaks Kannada
                                                                  │
                                            raw agent audio ◀─────┘
```
No STT, no TTS. Gemini Live hears the caller's audio directly and responds with its own
voice. Server-side VAD handles turn-taking. **This is the flawless-Kannada, ~1s-latency path.**

### `gemini` — Sarvam STT → Gemini Live (fallback for noisy lines)
Sarvam Saarika transcribes first, feeds text to Gemini Live. Slightly higher latency; more
robust if raw-audio understanding struggles on a bad connection.

### `sarvam` — Gemini text LLM → Sarvam Bulbul TTS (the original pipeline)
```
Caller → Twilio → Sarvam STT → Gemini 2.5 Flash (text) → Sarvam Bulbul TTS → Twilio
```
Kept for voice-character control (pick a Sarvam speaker) and as a fully-cascaded fallback.

---

## 3. Components

| Layer | Tech | Notes |
|---|---|---|
| Telephony | **Twilio** Programmable Voice + Media Streams | Migrate to **Exotel** for India — ~half the per-min cost. |
| Public ingress | **cloudflared** quick tunnel | Exposes local `:8000` to Twilio. ⚠️ Ephemeral URL — see gotchas. |
| Orchestration | **Pipecat 0.0.108** | VAD, barge-in, turn-taking, service glue. Pinned; do **not** bump to 1.x. |
| Voice engine | **Gemini Live** native audio (`gemini-2.5-flash-native-audio-preview-12-2025`) | Speaks directly. Voice: `Aoede`. Thinking disabled. |
| STT (other modes) | **Sarvam Saarika** (`saarika:v2.5`, kn-IN) | Code-mix aware. |
| TTS (sarvam mode) | **Sarvam Bulbul** (`bulbul:v3`, speaker `kavya`) | India-native voices. |
| Text LLM (sarvam mode) | **Gemini 2.5 Flash** | temp 0.4. |
| Dialect brain | System prompt + lexicon (in-context, not RAG) | See §5. |
| Latency masking | **Filler injector** | Tiered "please hold" flow (ack ~1.1 s → reassure ~4 s → close-out ~7.5 s) covering slow Gemini turns; flow picked by `FILLER_FLOW` (account/transaction/loan/universal). |
| Demo UI | **FastAPI** + static dashboard | Context editor, live status, transcript stream. |

---

## 4. File map

| File | Role |
|---|---|
| `bot.py` | Builds the system instruction + the Pipecat pipeline. Houses the `VOICE_ENGINE` toggle, the Gemini Live wiring (thinking off, fast VAD turn strategy), and the `FillerInjector`. |
| `server.py` | FastAPI: demo dashboard, `/api/campaign`, `/api/call`, `/events` (live WS), `/twiml`, `/ws` (Twilio media stream). |
| `app_state.py` | In-memory campaign context + pub/sub event bus (dashboard ⇄ call). |
| `system_prompt_bengaluru_kannada_telecaller.md` | The dialect brain: identity, Kanglish rules, anti-halegannada, guardrails, call flow. |
| `bangalore_kannada_lexicon.json` | Bangalore slang/verb lexicon, injected into context. |
| `generate_fillers.py` | Regenerates the Indian-accent English filler clips (Gemini TTS, Aoede voice → 8 kHz). |
| `assets/fillers/*.wav` | Runtime filler clips (git-ignored; regenerate via the script). |
| `audition_voices.py` | Auditions Sarvam speakers (for `sarvam` mode voice selection). |
| `rag/`, `knowledge_base/` | Optional business-KB retrieval (BGE-m3 + reranker) — see `RAG_DESIGN.md`. |
| `static/index.html` | Demo control panel. |
| `.env` | Secrets + config (git-ignored). |

---

## 5. The dialect approach (why it sounds local)

- **Register lives in the text/model, not the voice.** The system prompt forces *casual spoken*
  Bangalore Kanglish and bans **halegannada** (formal/literary Kannada) — the #1 failure mode.
- **Lexicon is injected in-context, not vector-retrieved** — the term set is small and bounded,
  so a glossary in the prompt is faster and never mis-retrieves. (Vector RAG is reserved for the
  *business* knowledge base — see `RAG_DESIGN.md`.)
- In `gemini-direct`/`gemini` modes, a voice-mode note tells the model to **speak naturally**
  and ignore the script-formatting rules (those only matter for Sarvam TTS text).

---

## 6. The latency story (how we got to ~1 s)

Measured, not guessed (isolated Gemini Live tests):

| Change | Effect |
|---|---|
| Native audio with **thinking ON** (default) | first-audio **6 s → 11 s → 17 s**, growing every turn |
| **`thinking_budget=0`** (disable thinking) | **~1.0–1.1 s, flat** — the single biggest fix |
| Removed redundant STT stage (`gemini-direct`) | cut a full STT round-trip |
| VAD `stop_secs` 0.8 → 0.5, fast VAD turn-stop strategy | trimmed end-of-turn wait |
| Filler injector (>1.2 s stall) | masks the occasional slow turn with a spoken "hmm/okay" |

Net user-perceived latency target: **~1.2–1.7 s** per turn.

---

## 7. Cost (verified 2026-07-03)

Rates: Exotel [exotel.com/pricing](https://exotel.com/pricing) · Gemini [ai.google.dev/gemini-api/docs/pricing](https://ai.google.dev/gemini-api/docs/pricing). FX $1 ≈ ₹86.

### Rate card
- **Exotel** outbound to Indian numbers: **₹0.80–1.00/min + 18% GST** ≈ ₹0.95–1.18/min.
- **Gemini 2.5 Flash Native Audio (Live API):** audio **in $3 / 1M**, audio **out $12 / 1M**, text in $0.50/1M.
  Audio tokenises at **32 tokens/sec (in)**, **25 tokens/sec (out)**.

### Per-minute (balanced minute: bot ~30 s, caller ~30 s)
| Piece | Tokens | Rate | Cost |
|---|---|---|---|
| Bot audio out | 30 × 25 = 750 | $12/1M | $0.0090 |
| Caller audio in | 30 × 32 = 960 | $3/1M | $0.0029 |
| System-prompt reprocessing | ~2k–5k | $0.50/1M | $0.001–0.006 |
| **Gemini subtotal** | | | **≈ $0.013–0.018 → ₹1.1–1.6** |
| **Exotel** (India + GST) | | | **≈ $0.012 → ₹0.95–1.18** |
| **TOTAL** | | | **≈ ₹2.0–2.8 / min (~$0.024–0.033)** |

**Plan with ~₹2.5 / min (~$0.03).**

### Scale
| Volume | Approx cost |
|---|---|
| 1 call × 3 min | ~₹7.5 |
| 1,000 calls × 3 min | ~₹7,500/mo (~$90) |
| 10,000 calls × 3 min | ~₹75,000/mo (~$875) |

### ⚠️ Cost risks
1. **Context accumulation** — Live API reprocesses the growing conversation each turn, so a
   **10-min call can average ₹4–6/min** (vs ₹2.5 for a 1-min call). Mitigate with Live
   **context-window compression** + keeping calls short.
2. **No cheap Live tier** — `gemini-2.0-flash-live` was **shut down 2026-06-01**; native audio
   (premium) is the only option, so you can't downgrade the model to save money.
3. **Telephony dominates if you stay on Twilio** — Twilio→India was ~₹9–13/min; Exotel ≈ halves it.

---

## 8. Setup & run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in rotated keys + set VOICE_ENGINE=gemini-direct

# macOS Python.org gotcha: install SSL certs or Sarvam/Gemini sockets fail
/Applications/Python\ 3.11/Install\ Certificates.command   # (once)

# run
export SSL_CERT_FILE=$(python -m certifi)
uvicorn server:app --host 0.0.0.0 --port 8000

# expose to telephony
cloudflared tunnel --url http://localhost:8000     # copy the https host into .env PUBLIC_HOST, restart

# generate filler clips (once)
python generate_fillers.py
```

Dashboard: `http://localhost:8000`. Place a call from the UI or `POST /api/call {"to":"+91…"}`.

---

## 9. Gotchas (hard-won)

- **cloudflared quick-tunnels are unstable** — they die and the URL changes (Cloudflare error 1033
  = tunnel down → Twilio plays "application error"). For real demos use a **named/persistent tunnel**
  or restart + update `PUBLIC_HOST` each time.
- **macOS SSL certs** — python.org Python doesn't link system certs; run `Install Certificates.command`
  or set `SSL_CERT_FILE=$(python -m certifi)`, else Sarvam/Gemini websockets throw `CERTIFICATE_VERIFY_FAILED`.
- **Sarvam TTS credits** — a `402 No credits available` = silent call in `sarvam` mode.
- **Sarvam script quirk (sarvam mode only):** Bulbul renders Kannada script cleanly but garbles
  romanized Latin ("Namaskara"→"vamaskara"). Irrelevant in Gemini voice modes.
- **Pipecat is pinned to 0.0.108** — 1.x drops `OpenAILLMContext`/`create_context_aggregator`/`TranscriptProcessor`.
- **Gemini Live language code:** do NOT set an explicit `kn-IN` on the native-audio model — it
  auto-detects and hard-rejects an explicit code (websocket 1007).

---

## 10. Version history (git tags)

| Tag | State |
|---|---|
| `v1-first-call` | First good Sarvam-TTS version. |
| `v2-stable-romanized` | Sarvam mode, romanized output. |
| `v3-script-output` | Sarvam mode, Kannada-script output (fixed garble). |
| `v4-gemini-voice` | Pivot to Gemini Live native audio (current lineage). |

---

## 11. Roadmap / open items

- Exotel migration (telephony cost + India compliance).
- Live **context-window compression** to cap long-call cost.
- Persistent named tunnel (or cloud host) for stable demos.
- Dashboard transcript in Gemini modes (wire Live transcription events).
- ABC School (or client) campaign content + guardrail probes.
- Few-shot dialogue examples in the prompt for tighter register.
