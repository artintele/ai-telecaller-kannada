"""
Generate the instant-acknowledgement clips ("Okay sir", "Sure") in the SAME Google Cloud
TTS voice the calls use, one set per call language.

bot.py's FillerInjector plays one of these ~0.35s after the caller stops a longer
utterance, so the line is never silent while Gemini writes the real reply (~1s). The
real answer's first audio cancels anything still pending.

Output: assets/fillers/<lang>/quick_<i>.wav and universal_0.wav — 8 kHz mono 16-bit PCM,
the phone-line format, so nothing is resampled at play time.

Auth: same as the call path — GOOGLE_APPLICATION_CREDENTIALS (Workload Identity
Federation config) or a service-account key.

Run on the box:  python generate_acks.py
"""

import os
import wave
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import texttospeech_v1 as tts

load_dotenv()

BASE_DIR = Path(__file__).parent
FILLERS_DIR = BASE_DIR / "assets" / "fillers"

# Neutral acknowledgements only — they must fit ANY question, because they play before
# the answer is known. Nothing that promises an action ("let me check") or an outcome.
ACKS = {
    "en": {
        "voice": os.getenv("GOOGLE_TTS_VOICE_EN", "en-IN-Wavenet-A"),
        "language_code": "en-IN",
        "universal": "Okay sir.",
        "quick": ["Sure.", "Right, okay.", "Hmm, okay."],
    },
    "kn": {
        "voice": os.getenv("GOOGLE_TTS_VOICE_KN", "kn-IN-Wavenet-A"),
        "language_code": "kn-IN",
        "universal": "ಸರಿ ಸರ್.",
        "quick": ["ಹಾ, ಓಕೆ.", "ಹೂಂ, ಸರಿ.", "ಓಕೆ ಸರ್."],
    },
}


def synth(client, text: str, voice: str, language_code: str) -> bytes:
    r = client.synthesize_speech(
        input=tts.SynthesisInput(text=text),
        voice=tts.VoiceSelectionParams(language_code=language_code, name=voice),
        audio_config=tts.AudioConfig(
            audio_encoding=tts.AudioEncoding.LINEAR16, sample_rate_hertz=8000
        ),
    )
    # LINEAR16 responses carry a 44-byte WAV header; keep only the PCM samples.
    return trim_silence(r.audio_content[44:])


def trim_silence(pcm: bytes, threshold: int = 500, pad_ms: int = 40) -> bytes:
    """Cut the leading/trailing near-silence Chirp 3 HD pads clips with.

    An "Okay sir" that is 1.4s long is ~0.6s of speech and ~0.8s of padding; every
    padded millisecond pushes the real answer later, since queued ack audio plays out.
    """
    import array

    samples = array.array("h", pcm)
    loud = [i for i, v in enumerate(samples) if abs(v) > threshold]
    if not loud:
        return pcm
    pad = 8 * pad_ms  # samples at 8 kHz
    start, end = max(0, loud[0] - pad), min(len(samples), loud[-1] + pad)
    return samples[start:end].tobytes()


def write_wav(path: Path, pcm: bytes) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(pcm)
    return len(pcm) / 16000


def main():
    client = tts.TextToSpeechClient()
    for lang, spec in ACKS.items():
        out = FILLERS_DIR / lang
        for old in out.glob("*.wav"):
            old.unlink()
        clips = [("universal_0", spec["universal"])] + [
            (f"quick_{i}", t) for i, t in enumerate(spec["quick"])
        ]
        for name, text in clips:
            pcm = synth(client, text, spec["voice"], spec["language_code"])
            dur = write_wav(out / f"{name}.wav", pcm)
            # Refuse silently-broken output: a real "Okay sir" is well over 0.3s.
            if dur < 0.3:
                raise SystemExit(f"{lang}/{name}: only {dur:.2f}s of audio — synthesis failed")
            print(f"{lang}/{name}.wav  {dur:.2f}s  {text}")


if __name__ == "__main__":
    main()
