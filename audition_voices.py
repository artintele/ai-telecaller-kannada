"""
Speaker audition — generate the SAME Bengaluru-Kanglish line in every Sarvam voice so you
can pick the one that matches your reference recordings.

It calls Sarvam's TTS REST API directly (no Pipecat needed) and saves one .wav per speaker
into audition_output/. Play them back-to-back and lock your favourite into
.env -> SARVAM_TTS_SPEAKER.

IMPORTANT: this also proves the anti-halegannada point — the LINE below is deliberately
casual/code-mixed. Every voice will sound casual because the *text* is casual. If you feed
it a formal Kannada sentence instead, every voice will sound like halegannada. Register
lives in the text, not the voice.

Usage:
  pip install requests python-dotenv
  python audition_voices.py                 # bulbul:v2 voices
  python audition_voices.py --model bulbul:v3
  python audition_voices.py --text "your own kanglish line"
"""

import argparse
import base64
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"
OUT_DIR = Path(__file__).parent / "audition_output"

# A casual, code-mixed Bangalore line — the register test, not just a voice test.
DEFAULT_LINE = (
    "ನಮಸ್ಕಾರ ಸರ್, ನಾನು Kavya, artintele.ai inda maataadtaidini. "
    "Nimma loan offer bagge ondu chikka update ide, two minutes ideera?"
)

VOICES = {
    "bulbul:v2": ["anushka", "manisha", "vidya", "arya", "abhilash", "karun", "hitesh"],
    "bulbul:v3": ["anushka", "manisha", "vidya", "arya", "pavithra", "maitreyi",
                  "abhilash", "karun", "hitesh"],  # trim/extend to what your account exposes
}


def synth(api_key: str, text: str, speaker: str, model: str) -> bytes | None:
    payload = {
        "inputs": [text],
        "target_language_code": "kn-IN",
        "speaker": speaker,
        "model": model,
        "pitch": 0.0,
        "pace": 1.05,
        "loudness": 1.0,
        "speech_sample_rate": 22050,
        "enable_preprocessing": True,
    }
    headers = {"api-subscription-key": api_key, "Content-Type": "application/json"}
    r = requests.post(SARVAM_TTS_URL, json=payload, headers=headers, timeout=60)
    if r.status_code != 200:
        print(f"  ✗ {speaker}: HTTP {r.status_code} {r.text[:160]}")
        return None
    audios = r.json().get("audios") or []
    if not audios:
        print(f"  ✗ {speaker}: no audio in response")
        return None
    return base64.b64decode(audios[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="bulbul:v2", choices=list(VOICES.keys()))
    ap.add_argument("--text", default=DEFAULT_LINE)
    args = ap.parse_args()

    api_key = os.getenv("SARVAM_API_KEY")
    if not api_key:
        raise SystemExit("Set SARVAM_API_KEY in .env")

    OUT_DIR.mkdir(exist_ok=True)
    print(f"Model: {args.model}\nLine : {args.text}\nOut  : {OUT_DIR}\n")

    made = []
    for speaker in VOICES[args.model]:
        audio = synth(api_key, args.text, speaker, args.model)
        if audio:
            path = OUT_DIR / f"{args.model.replace(':', '_')}__{speaker}.wav"
            path.write_bytes(audio)
            print(f"  ✓ {speaker} -> {path.name}")
            made.append(path.name)

    print(f"\nDone. {len(made)} files in {OUT_DIR}.")
    print("Play them, pick the closest to your reference WAVs, set it in .env -> SARVAM_TTS_SPEAKER.")


if __name__ == "__main__":
    main()
