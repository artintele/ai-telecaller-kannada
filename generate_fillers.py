"""
Generate short English-but-Indian-accent filler/backchannel clips for the tele-caller.

These play while Gemini is (occasionally) slow to respond, so they must sound like the
SAME warm Bangalore agent — English words ("hmm", "okay", "alright") in a natural Indian
accent, never foreign. Generated with Gemini TTS using the Aoede voice (the same voice
family the Live agent speaks with) and an accent-steering instruction.

Output: 8kHz mono 16-bit PCM wav (Twilio format) in assets/fillers/.

Run:  python generate_fillers.py
"""

import audioop
import os
import wave
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

FILLERS_DIR = Path(__file__).parent / "assets" / "fillers"
VOICE = os.getenv("GEMINI_VOICE", "Aoede")

# name -> acknowledging "please hold on" fillers a real Bengaluru tele-caller says while
# looking something up. Kanglish (Kannada + a little English), reassuring, so the caller
# knows the agent is still there and working — not awkward dead air.
FILLERS = {
    "line_alli_iri": "ಒಂದು ನಿಮಿಷ ಸರ್, line ಅಲ್ಲಿ ಇರಿ.",
    "ok_ond_nimsha": "ok ಸರ್, ಒಂದು ನಿಮಿಷ!",
    "ok_sir": "ok ಸರ್.",
    "sure_sir": "ಹ್ಂ, sure ಸರ್.",
    "check_maadtini": "ಸರಿ ಸರ್, ಒಂದು ಸೆಕೆಂಡ್ check ಮಾಡ್ತಿನಿ.",
    "haan_ondu_second": "ಹಾಂ ಹಾಂ, ಒಂದು ಸೆಕೆಂಡ್ ಸರ್.",
}

STYLE = (
    "Speak this as a warm, reassuring Bengaluru Kannada tele-caller politely asking the "
    "customer to hold on for a moment while you check something — natural, calm, and "
    "acknowledging, like you are still with them on the line. Kanglish: "
)


def main():
    FILLERS_DIR.mkdir(parents=True, exist_ok=True)
    client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

    import time

    ok = 0
    for name, word in FILLERS.items():
        pcm24 = None
        for attempt in range(3):
            try:
                r = client.models.generate_content(
                    model="gemini-2.5-flash-preview-tts",
                    contents=STYLE + word,
                    config=types.GenerateContentConfig(
                        response_modalities=["AUDIO"],
                        speech_config=types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
                            )
                        ),
                    ),
                )
                cand = r.candidates[0] if r.candidates else None
                if cand and cand.content and cand.content.parts:
                    pcm24 = cand.content.parts[0].inline_data.data
                    break
            except Exception as e:
                print(f"    {name} attempt {attempt+1} error: {str(e)[:80]}")
            time.sleep(1.5)

        if not pcm24:
            print(f"  ✗ {name:12s} '{word}' -> FAILED after retries, skipping")
            continue

        pcm8, _ = audioop.ratecv(pcm24, 2, 1, 24000, 8000, None)   # -> 8kHz for Twilio
        path = FILLERS_DIR / f"{name}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(pcm8)
        ok += 1
        print(f"  ✓ {name:12s} '{word}' -> {path.name}  ({len(pcm8)/16000:.2f}s)")

    print(f"\nDone. {ok}/{len(FILLERS)} clips in {FILLERS_DIR}")


if __name__ == "__main__":
    main()
