# artintele.ai — Bengaluru Kannada Tele-Caller: Stack & Architecture

## Recommended stack (your stack + one upgrade)

| Layer | You said | Recommendation | Why |
|---|---|---|---|
| Telephony | Twilio | **Twilio Media Streams** (bidirectional WebSocket over the call) | Correct. Use `<Connect><Stream>` TwiML to pipe raw audio to your server. Exotel/Plivo are India-local alternatives with cheaper local DIDs if you scale in-India volume. |
| STT | Sarvam | **Sarvam Saarika (kn-IN)** | Best-in-class for Kannada + code-mixed "Kanglish". Turn on code-mixing. |
| LLM brain | Gemini API | **Gemini 2.5 Flash** | Low latency, strong multilingual, cheap enough for real-time turns. Keep temperature low (~0.4) for guardrail stability. |
| TTS | Sarvam | **Sarvam Bulbul (v2 or v3, kn-IN)** | Preset speakers — pick the female voice closest to your reference WAVs. |
| **Orchestration** | *(hand-wired?)* | **➜ Pipecat** (open-source, Python) | **This is the upgrade.** Pipecat has first-party plug-ins for Twilio transport + Sarvam STT + Sarvam TTS + Gemini LLM. It handles VAD, barge-in/interruptions, turn-taking, and streaming glue so you don't hand-roll WebSocket plumbing. LiveKit Agents is the main alternative. |

### Why Pipecat specifically
Sarvam publishes an official "Build a Voice Agent with Pipecat" guide, and Pipecat ships `SarvamTTSService`, `SarvamSTTService`, a Twilio serializer, and a Gemini LLM service. That means your whole pipeline is ~one config file: `Twilio audio → Sarvam STT → Gemini (your system prompt) → Sarvam TTS → Twilio audio`, with interruption handling built in.

## Data flow
```
 Caller ⇄ Twilio PSTN ⇄ <Stream> WebSocket ⇄ Pipecat
                                              │
        ┌─────────────────────────────────────┤
        ▼                 ▼                    ▼
   Sarvam Saarika    Gemini 2.5 Flash     Sarvam Bulbul
      (STT kn-IN)   (system prompt +      (TTS kn-IN,
   code-mix ON       tools/CRM)           locked speaker)
```

## About the two reference WAV files
- Sarvam Bulbul **does not clone** an uploaded voice — it uses fixed preset speakers. So you can't feed the WAVs in to "become" that voice.
- **Use the WAVs as your target/eval reference:** audition Sarvam's female kn-IN speakers, pick the closest, then tune `pitch` / `pace` / `loudness` until it matches. Keep the WAVs as A/B ground truth for QA.
- If you genuinely need *that exact* voice cloned, that's a different tool class (ElevenLabs multilingual / Sarvam's custom-voice enterprise offering if available) — but you lose Sarvam's Kannada-native quality. Recommendation: match with a preset; don't chase a clone.

## Key config knobs
- **Sarvam STT:** `language_code: kn-IN`, enable code-mixing/translit; stream partials for low-latency turn detection.
- **Sarvam TTS:** `target_language_code: kn-IN`, `speaker: <locked id>`, `pace ~1.0`, `pitch ~0`, `model: bulbul:v2` (audition v3). Feed it text per the "How to write text for the TTS" rules in the system prompt (Kannada script for Kannada words, Latin for English words, numbers as words, no markdown).
- **Gemini:** load `system_prompt_bengaluru_kannada_telecaller.md` as the system instruction; temperature ~0.3–0.5; short max output tokens to force short spoken turns; wire CRM/booking as tools/function-calls.
- **Twilio:** `<Connect><Stream url="wss://.../ws"/>`; handle DTMF; set caller ID; respect India TRAI calling-time / DND rules.

## Compliance checklist (India)
- DPDP Act 2023: identify company + purpose at call start; honor opt-out immediately; log consent.
- TRAI DND / calling windows for outbound.
- Never collect CVV/passwords/full OTP over voice (enforced in the prompt guardrails).

## Sources
- Sarvam TTS / Bulbul: https://www.sarvam.ai/apis/text-to-speech · https://docs.sarvam.ai/api-reference-docs/getting-started/models/bulbul
- Sarvam STT (Kannada, code-mix): https://www.sarvam.ai/apis/speech-to-text
- Sarvam + Pipecat guide: https://docs.sarvam.ai/api-reference-docs/integration/build-voice-agent-with-pipecat
- Pipecat: https://github.com/pipecat-ai/pipecat · https://docs.pipecat.ai/api-reference/server/services/tts/sarvam
