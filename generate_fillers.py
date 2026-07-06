"""
Generate the "please hold" filler clips for the tele-caller — via Google Cloud TTS
(the same Aoede voice + service account the hybrid pipeline uses). Reliable, unlike the
old AI Studio free-tier path (which throttled to ~1-in-5 success).

Current design (kept deliberately simple per request): NO "hmm/haan" backchannels — just
short, natural "okay, one minute sir" hold phrases in the agent's own voice, played when a
turn stalls. These load into bot.py's FillerInjector as the "universal" flow.

Output: 8kHz mono 16-bit PCM wav (Twilio format) in assets/fillers/, named
"{flow}_{tier}.wav". Old *.wav are cleared first.

Run:  python generate_fillers.py
"""

import asyncio
import audioop
import os
import wave
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import texttospeech_v1
from google.oauth2 import service_account

load_dotenv()

BASE_DIR = Path(__file__).parent
FILLERS_DIR = BASE_DIR / "assets" / "fillers"
KEY_PATH = str(BASE_DIR / os.getenv("GCP_KEY_PATH", "gcp-key.json"))
VOICE = os.getenv("GEMINI_VOICE", "Aoede")
TTS_MODEL = "gemini-2.5-flash-tts"

# MEANINGFUL acknowledgments — these fire only after a LONG/complex question (gated in
# bot.py by how long the caller spoke), so they should sound like Kavya genuinely taking
# in the request and going to check, NOT a canned "hold please". tier 0 fires first;
# 1 and 2 are follow-ups only if the wait keeps going.
FILLER_FLOWS = {
    "universal": [
        "Haan sir, adanna check maadi heltini.",       # yes sir, let me check that and tell you
        "Sari, ondu sec — adanna nodta iddini.",        # okay, one sec, I'm looking at that
        "Ahaan, artha aaytu sir, ondu nimisha.",        # ah, understood sir, one minute
    ],
}

STYLE = (
    "Say this the way a warm, natural Bangalore tele-caller reacts when a customer asks a "
    "real question — like you just took in what they said and you're about to check it for "
    "them. Casual, engaged, reassuring, a little thoughtful. NOT a scripted 'hold please', "
    "not formal, not an announcer. Kannada+English code-mix, natural Indian accent."
)


async def synth(client, text: str) -> bytes:
    """Cloud TTS -> 8kHz mono PCM for one phrase (Aoede voice, casual style)."""
    voice = texttospeech_v1.VoiceSelectionParams(
        language_code="en-US", name=VOICE, model_name=TTS_MODEL
    )
    scfg = texttospeech_v1.StreamingSynthesizeConfig(
        voice=voice,
        streaming_audio_config=texttospeech_v1.StreamingAudioConfig(
            audio_encoding=texttospeech_v1.AudioEncoding.PCM,
            sample_rate_hertz=24000,
        ),
    )

    async def reqs():
        yield texttospeech_v1.StreamingSynthesizeRequest(streaming_config=scfg)
        yield texttospeech_v1.StreamingSynthesizeRequest(
            input=texttospeech_v1.StreamingSynthesisInput(text=text, prompt=STYLE)
        )

    pcm24 = b""
    async for r in await client.streaming_synthesize(reqs()):
        pcm24 += r.audio_content
    pcm8, _ = audioop.ratecv(pcm24, 2, 1, 24000, 8000, None)  # 24kHz -> 8kHz Twilio
    return pcm8


def write_wav(name: str, pcm8: bytes):
    path = FILLERS_DIR / f"{name}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(pcm8)
    print(f"  ✓ {name:14s} '{pcm8 and ''}' -> {path.name}  ({len(pcm8)/16000:.2f}s)")


async def main():
    FILLERS_DIR.mkdir(parents=True, exist_ok=True)
    for old in FILLERS_DIR.glob("*.wav"):  # clear stale clips so nothing outdated loads
        old.unlink()

    creds = service_account.Credentials.from_service_account_file(KEY_PATH)
    client = texttospeech_v1.TextToSpeechAsyncClient(credentials=creds)

    ok = 0
    total = 0
    for flow, tiers in FILLER_FLOWS.items():
        for i, phrase in enumerate(tiers):
            total += 1
            try:
                pcm8 = await synth(client, phrase)
                write_wav(f"{flow}_{i}", pcm8)
                ok += 1
            except Exception as e:
                print(f"  ✗ {flow}_{i} '{phrase}' -> {type(e).__name__}: {str(e)[:120]}")

    print(f"\nDone. {ok}/{total} clips in {FILLERS_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
