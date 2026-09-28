"""
artintele.ai — Bengaluru Kannada (Kanglish) voice tele-caller.

Pipeline:  Twilio audio  ->  Sarvam STT  ->  Gemini LLM  ->  Sarvam TTS  ->  Twilio audio

This module builds the system instruction (dialect prompt + lexicon) and wires the
Pipecat pipeline. server.py handles the Twilio webhook + websocket and calls run_bot().

NOTE ON VERSIONS: Pipecat's service import paths change between releases. These imports
target pipecat-ai ~0.0.95. If an import fails after `pip install`, run
`python -c "import pipecat.services.sarvam.tts"` etc. and adjust to your installed version.
"""

import asyncio
import json
import os
import random
import time
import wave
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.transcript_processor import TranscriptProcessor
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from app_state import hub  # demo event bus: publishes transcript + status to the dashboard

load_dotenv()

BASE_DIR = Path(__file__).parent
SYSTEM_PROMPT_FILE = BASE_DIR / "system_prompt_bengaluru_kannada_telecaller.md"
LEXICON_FILE = BASE_DIR / "bangalore_kannada_lexicon.json"

# CALL_LANGUAGE picks the whole language profile for a call: which prompt the LLM gets,
# whether the Bangalore lexicon is appended, and the language Sarvam STT listens for and
# Bulbul speaks. "kn" = Bengaluru Kanglish (the original), "en" = Indian English.
# Set per call from the dashboard (campaign CALL_LANGUAGE) or globally in .env.
LANGUAGE_PROFILES = {
    "kn": {
        "prompt_file": SYSTEM_PROMPT_FILE,
        "lexicon": True,
        "language": Language.KN_IN,
        # codemix keeps English words in Latin script instead of forcing them into Kannada
        "stt_mode": "codemix",
        "kickoff": "The call just connected. Speak your FIRST LINE now, warmly, in Bengaluru Kanglish.",
    },
    "en": {
        "prompt_file": BASE_DIR / "system_prompt_english_telecaller.md",
        # Gemini Live re-reads the system prompt on EVERY turn and bills it each time;
        # the full rulebook is ~2.3k tokens, this spoken-call core is about a third.
        "live_prompt_file": BASE_DIR / "system_prompt_english_live.md",
        "lexicon": False,
        "language": Language.EN_IN,
        "stt_mode": "transcribe",
        "kickoff": "The call just connected. Speak your FIRST LINE now, warmly, in natural Indian English.",
    },
}


def call_language(campaign: dict | None) -> str:
    lang = ((campaign or {}).get("CALL_LANGUAGE") or os.getenv("CALL_LANGUAGE", "kn")).lower()
    return lang if lang in LANGUAGE_PROFILES else "kn"

# Twilio media streams are 8kHz mu-law, mono.
TWILIO_SAMPLE_RATE = 8000

# Per-call debug log: pipecat logs TTFB metrics + turn events here so latency
# can be measured from real timestamps instead of guessed.
logger.add(BASE_DIR / "logs" / "call_debug.log", level="DEBUG", rotation="20 MB", retention=3)

FILLERS_DIR = BASE_DIR / "assets" / "fillers"

# Ordered stall FLOWS: tier 0 (covers 0-3s) -> tier 1 (3-7s) -> tier 2 (7-10s).
# On disk each is "{flow}_{tier}.wav"; the quick-stall pool is "quick_{i}.wav".
# Kept in sync with generate_fillers.py (that file holds the actual phrasing).
FILLER_FLOW_NAMES = ("account", "transaction", "loan", "universal")
QUICK_STALL_COUNT = 3


def _load_wav(path: Path) -> tuple[bytes, float]:
    """Read an 8kHz mono PCM clip -> (raw pcm bytes, real duration in seconds)."""
    with wave.open(str(path), "rb") as w:
        pcm = w.readframes(w.getnframes())
        dur = w.getnframes() / float(w.getframerate())
    return pcm, dur


def load_filler_flows(lang: str | None = None) -> tuple[dict[str, list[tuple[bytes, float]]], list[tuple[bytes, float]]]:
    """Load the tiered stall flows + quick-stall pool from assets/fillers/.

    Returns (flows, quick) where flows[name] is the ordered tier list
    [(pcm, dur), ...] and quick is the shared short-opener pool. Missing files
    are skipped so a partial regenerate still runs.
    """
    # Per-language clips (assets/fillers/<lang>/, from generate_acks.py) win over the
    # legacy flat folder, so an English call never plays a Kannada acknowledgement.
    fdir = FILLERS_DIR / lang if lang and (FILLERS_DIR / lang).is_dir() else FILLERS_DIR
    flows: dict[str, list[tuple[bytes, float]]] = {}
    if fdir.is_dir():
        for name in FILLER_FLOW_NAMES:
            tiers = [
                _load_wav(fdir / f"{name}_{tier}.wav")
                for tier in range(3)
                if (fdir / f"{name}_{tier}.wav").exists()
            ]
            if tiers:
                flows[name] = tiers
    quick = [
        _load_wav(fdir / f"quick_{i}.wav")
        for i in range(QUICK_STALL_COUNT)
        if (fdir / f"quick_{i}.wav").exists()
    ]
    return flows, quick


class FillerInjector(FrameProcessor):
    """Cover Gemini's occasional slow turns with a coherent, TIERED "please hold" flow.

    Gemini Live TTFB is bimodal: usually ~1s, but with 5-10s outliers. A single ack
    can't cover a 7s wait without sounding broken, so this plays an ORDERED flow that
    tells a story as the wait grows (times measured from when the caller stops):
        ~1.1s  tier 0  "ondu nimisha sir, account pull up maadtha iddini..."   (0-3s)
        ~4s    tier 1  "details screen mele load aagtha ide..."                (3-7s)
        ~7.5s  tier 2  "almost mugithu sir, one second..."                     (7-10s)
    Each tier is allowed to ACTUALLY PLAY OUT (duration-aware sleeps) before the next
    is considered, so tiers never stack/machine-gun (the old failure mode). The whole
    sequence is cancelled the instant the real answer's audio arrives, the caller
    speaks again, or on interruption.

    The flow is chosen per call (FILLER_FLOW: account|transaction|loan|universal).
    'universal' + the quick-stall pool are safe on ANY query; the domain flows name
    specific actions (statement fetch, loan eligibility) so only select them when the
    campaign is actually about that.
    """

    def __init__(
        self,
        flows: dict[str, list[tuple[bytes, float]]],
        quick: list[tuple[bytes, float]],
        flow_name: str = "universal",
        delay: float = 1.1,
        gap: float = 0.35,
        min_query_secs: float = 2.5,
        sample_rate: int = 8000,
        probability: float = 1.0,
    ):
        super().__init__()
        self._flows = flows
        self._quick = quick
        self._flow_name = flow_name if flow_name in flows else "universal"
        self._delay = delay      # wait before the first ack — long enough to skip fast turns
        self._gap = gap          # breath between tiers, on top of the clip's own playout
        # Only arm the filler after a LONG/complex question (the caller spoke this many
        # seconds). Short questions get an instant answer with no filler — the ack only
        # appears where a "let me check that" beat is genuinely earned.
        self._min_query_secs = min_query_secs
        # A real caller does not hear "Okay sir" before EVERY answer — on the 28 Sep call
        # an ack on nearly every turn made it sound scripted. Play it on a fraction of
        # the long questions only, and never twice in a row.
        self._probability = probability
        self._acked_last = False
        self._sample_rate = sample_rate
        self._filler_task = None
        self._last_quick = -1
        self._user_start_t = None  # monotonic time the current user utterance began
        self._ready = False  # no fillers until the greeting has been spoken
        self.bot_has_spoken = False  # read by the greeting guard in run_bot

    def _active_flow(self) -> list[tuple[bytes, float]] | None:
        return self._flows.get(self._flow_name) or self._flows.get("universal")

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStoppedSpeakingFrame):
            # The opening greeting has finished — fillers allowed from here on.
            self._ready = True
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._user_start_t = time.monotonic()
            await self._cancel_pending()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # Gate on how long they spoke: only a long/complex query earns a filler.
            dur = (time.monotonic() - self._user_start_t) if self._user_start_t else 0.0
            self._user_start_t = None
            if self._ready and self._active_flow() and dur >= self._min_query_secs:
                if self._acked_last or random.random() >= self._probability:
                    self._acked_last = False
                else:
                    self._acked_last = True
                    await self._cancel_pending()
                    self._filler_task = self.create_task(self._play_flow())
        elif isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, InterruptionFrame)):
            if not isinstance(frame, InterruptionFrame):
                self.bot_has_spoken = True
            await self._cancel_pending()
        await self.push_frame(frame, direction)

    async def _cancel_pending(self):
        if self._filler_task:
            task, self._filler_task = self._filler_task, None
            await self.cancel_task(task)

    async def _play_flow(self):
        tiers = list(self._active_flow())
        # tier 0: sometimes swap in a shorter generic quick-stall for variety — both are
        # safe openers. tiers 1-2 stay flow-specific (the domain reassurance/close-out).
        if self._quick and tiers and random.random() < 0.5:
            tiers = [self._pick_quick()] + tiers[1:]
        await asyncio.sleep(self._delay)
        for pcm, dur in tiers:
            await self._play_one(pcm)
            # Let the clip actually play out before considering the next tier, so a fast
            # real answer cancels us mid-gap instead of after a second clip is queued.
            await asyncio.sleep(dur + self._gap)

    def _pick_quick(self) -> tuple[bytes, float]:
        choices = [i for i in range(len(self._quick)) if i != self._last_quick] or [0]
        self._last_quick = random.choice(choices)
        return self._quick[self._last_quick]

    async def _play_one(self, pcm: bytes):
        chunk = int(self._sample_rate * 2 * 0.2)  # 200ms chunks so barge-in clears fast
        for i in range(0, len(pcm), chunk):
            await self.push_frame(
                TTSAudioRawFrame(
                    audio=pcm[i : i + chunk], sample_rate=self._sample_rate, num_channels=1
                )
            )


# ---- Per-call cost meter -------------------------------------------------------------
# Provider dashboards lag hours and mix in test traffic, so every call logs its own cost.
# Google is billed in USD + 18% GST. USD_INR=92 is calibrated to the user's real balance
# (a Live call the meter put at Rs 2.52 before tax/FX cost Rs 3.19 on the bill).
def _google_inr(usd: float) -> float:
    return usd * float(os.getenv("USD_INR", "92")) * (1 + float(os.getenv("GST_RATE", "0.18")))


class CostMeter(FrameProcessor):
    """Sum usage MetricsFrames (LLM tokens, TTS characters) for the cascade pipeline."""

    def __init__(self):
        super().__init__()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tts_chars = 0

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        from pipecat.frames.frames import MetricsFrame
        from pipecat.metrics.metrics import LLMUsageMetricsData, TTSUsageMetricsData

        if isinstance(frame, MetricsFrame):
            for d in frame.data:
                if isinstance(d, LLMUsageMetricsData):
                    self.prompt_tokens += d.value.prompt_tokens or 0
                    self.completion_tokens += d.value.completion_tokens or 0
                elif isinstance(d, TTSUsageMetricsData):
                    self.tts_chars += d.value or 0
        await self.push_frame(frame, direction)

    def report(self, voice: str, stt_secs: float, call_secs: float, tts_audio_secs=None) -> str:
        # $ per 1M — Gemini 3.1 Flash-Lite (ai.google.dev pricing, 28 Sep 2026); Google Cloud
        # TTS list prices (Chirp 3 HD vs WaveNet/Standard). Sarvam Saaras is billed in INR;
        # 0.50/min is the user's own reference figure — override with SARVAM_STT_INR_PER_MIN.
        llm = _google_inr(self.prompt_tokens * float(os.getenv("LLM_IN_USD_PER_M", "0.25")) / 1e6
                          + self.completion_tokens * float(os.getenv("LLM_OUT_USD_PER_M", "1.50")) / 1e6)
        tts_rate = float(os.getenv("CHIRP_USD_PER_M_CHARS", "30")) if "chirp" in voice.lower() \
            else float(os.getenv("WAVENET_USD_PER_M_CHARS", "4"))
        tts = _google_inr(self.tts_chars * tts_rate / 1e6)
        if tts_audio_secs is not None:
            # Gemini TTS bills generated audio time: $0.0015 per 10s for 3.8 Flash-Lite
            # (introductory price to 31 Dec 2026, then double).
            tts = _google_inr(tts_audio_secs * float(os.getenv("GEMINI_TTS_USD_PER_SEC", "0.00015")))
            voice = f"{voice} {tts_audio_secs:.0f}s audio"
        stt = stt_secs / 60 * float(os.getenv("SARVAM_STT_INR_PER_MIN", "0.50"))
        # Exotel bills per STARTED minute.
        billed_min = max(1, -(-int(call_secs) // 60))
        tel = billed_min * float(os.getenv("EXOTEL_INR_PER_MIN", "0.63"))
        total = llm + tts + stt + tel
        per_min = total / (call_secs / 60) if call_secs else 0
        return (f"CALL COST ~Rs {total:.2f} for {call_secs:.0f}s (~Rs {per_min:.2f}/min talk time): "
                f"Gemini {self.prompt_tokens}+{self.completion_tokens} tok Rs {llm:.2f} | "
                f"TTS {self.tts_chars} chars ({voice}) Rs {tts:.2f} | "
                f"Sarvam STT {stt_secs:.0f}s Rs {stt:.2f} | Exotel {billed_min} min Rs {tel:.2f}")


class LiveTranscriptLogger(FrameProcessor):
    """Log + publish both sides of a Gemini Live call (there is no STT/TTS stage to do it).

    Gemini Live transcribes the caller itself and pushes that TranscriptionFrame
    UPSTREAM from the LLM service; its own speech comes DOWNSTREAM as TTSTextFrame. So
    one instance sits just before the LLM (catches the caller) and one just after it
    (catches the bot).
    """

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        from pipecat.frames.frames import TranscriptionFrame, TTSTextFrame

        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            logger.info(f"LIVE CALLER: {frame.text.strip()}")
            await hub.publish({"type": "transcript", "role": "customer", "content": frame.text})
        elif isinstance(frame, TTSTextFrame) and frame.text.strip():
            logger.info(f"LIVE KAVYA: {frame.text.strip()}")
            await hub.publish({"type": "transcript", "role": "agent", "content": frame.text})
        await self.push_frame(frame, direction)


class STTAudioGate(FrameProcessor):
    """Send caller audio to the STT only while the caller is actually speaking.

    Sarvam bills streaming STT by audio time, and the pipeline used to stream the whole
    call — including the ~half of it when the bot is talking and the line is silent.
    Local Silero VAD (in the transport, upstream of this gate) decides what is speech.
    A short pre-roll is replayed on VAD start because VAD confirms speech only after
    start_secs, so the first syllable would otherwise be clipped. Nothing is needed
    after VAD stop: pipecat's SarvamSTTService sends a flush on
    VADUserStoppedSpeakingFrame, which makes Sarvam finalize without hearing silence.
    Barge-in is unaffected — it runs on the local VAD, not on STT.
    """

    def __init__(self, preroll_secs: float = 0.4, sample_rate: int = 8000):
        super().__init__()
        self._max_preroll = int(preroll_secs * sample_rate * 2)  # 16-bit mono bytes
        self._bytes_per_sec = sample_rate * 2
        self._preroll: list = []
        self._preroll_bytes = 0
        self._speaking = False
        self._sent = 0
        self._total = 0

    def stats(self) -> tuple[float, float]:
        return self._sent / self._bytes_per_sec, self._total / self._bytes_per_sec

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        from pipecat.frames.frames import InputAudioRawFrame

        if isinstance(frame, InputAudioRawFrame):
            self._total += len(frame.audio)
            if self._speaking:
                self._sent += len(frame.audio)
                await self.push_frame(frame, direction)
            else:
                self._preroll.append(frame)
                self._preroll_bytes += len(frame.audio)
                while self._preroll and self._preroll_bytes > self._max_preroll:
                    self._preroll_bytes -= len(self._preroll.pop(0).audio)
            return
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._speaking = True
            await self.push_frame(frame, direction)
            for f in self._preroll:
                self._sent += len(f.audio)
                await self.push_frame(f, direction)
            self._preroll, self._preroll_bytes = [], 0
            return
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._speaking = False
        await self.push_frame(frame, direction)


def build_system_instruction(campaign: dict | None = None, live: bool = False) -> str:
    """Combine the language's system prompt (+ the Bangalore lexicon for Kannada)."""
    profile = LANGUAGE_PROFILES[call_language(campaign)]
    if live and profile.get("live_prompt_file") and profile["live_prompt_file"].exists():
        prompt = profile["live_prompt_file"].read_text(encoding="utf-8")
        if campaign:
            # Only what the agent may say — voice/model settings are not facts, and every
            # extra token here is billed again on every turn.
            facts = {k: v for k, v in campaign.items()
                     if k in ("CALL_TYPE", "GOAL", "CUSTOMER", "OFFER", "KEY_FACTS", "DO_NOT_SAY")}
            prompt += "\n\nLIVE CAMPAIGN\n" + json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        return prompt
    prompt = profile["prompt_file"].read_text(encoding="utf-8")
    if not profile["lexicon"]:
        if campaign:
            prompt += "\n\n## LIVE CAMPAIGN\n" + json.dumps(campaign, ensure_ascii=False, indent=2)
        return prompt

    lex = json.loads(LEXICON_FILE.read_text(encoding="utf-8"))
    lines = ["\n\n## APPENDED DIALECT LEXICON (anchor to this; do not drift to formal Kannada)"]
    lines.append(lex["meta"]["register_guardrail"])
    for cat, items in lex["categories"].items():
        lines.append(f"\n### {cat}")
        for it in items:
            if "phrase" in it:
                lines.append(f"- {it['phrase']} = {it['meaning']}")
            else:
                speak = "SPEAK-OK" if it.get("speak", True) else "COMPREHEND-ONLY-DO-NOT-SPEAK"
                lines.append(
                    f"- {it['latin']} ({it['script']}) = {it['meaning']} [{speak}] e.g. {it.get('example','')}"
                )
    instruction = prompt + "\n".join(lines)

    if campaign:
        instruction += "\n\n## LIVE CAMPAIGN\n" + json.dumps(campaign, ensure_ascii=False, indent=2)
    return instruction


async def run_bot(
    websocket,
    stream_sid: str,
    call_sid: str,
    campaign: dict | None = None,
    provider: str = "twilio",
):
    if provider == "exotel":
        from pipecat.serializers.exotel import ExotelFrameSerializer

        # Exotel streams 8kHz PCM (not mulaw); no auth token needed by the serializer.
        serializer = ExotelFrameSerializer(stream_sid=stream_sid, call_sid=call_sid)
    else:
        serializer = TwilioFrameSerializer(
            stream_sid=stream_sid,
            call_sid=call_sid,
            account_sid=os.getenv("TWILIO_ACCOUNT_SID"),
            auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
        )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            # stop_secs: end-of-turn wait; start_secs 0.2->0.12 so barge-in
            # (interruption broadcast + Twilio buffer clear) fires near-instantly.
            # stop_secs 0.4->0.3 shaves 100ms off EVERY turn; raise via VAD_STOP_SECS
            # if it starts cutting off slow/pausing speakers.
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(
                    stop_secs=float(os.getenv("VAD_STOP_SECS", "0.3")),
                    start_secs=0.12,
                )
            ),
            serializer=serializer,
        ),
    )

    # saaras:v3 in "codemix" mode returns Kanglish as spoken (Kannada script + English
    # words in Latin) instead of forcing English words into Kannada script. `mode` is
    # only valid on saaras:v3 — pipecat raises on saarika, so it is dropped there.
    lang = call_language(campaign)
    profile = LANGUAGE_PROFILES[lang]
    stt_model = os.getenv("SARVAM_STT_MODEL", "saaras:v3")
    stt_kwargs = {}
    if stt_model == "saaras:v3":
        stt_kwargs["mode"] = os.getenv("SARVAM_STT_MODE") if lang == "kn" and os.getenv(
            "SARVAM_STT_MODE") else profile["stt_mode"]
    stt = SarvamSTTService(
        api_key=os.getenv("SARVAM_API_KEY"),
        settings=SarvamSTTService.Settings(model=stt_model, language=profile["language"]),
        # STTAudioGate stops audio while the bot talks; keepalive sends a little silence
        # after 10s of no audio so Sarvam does not drop the socket mid-call.
        keepalive_timeout=float(os.getenv("STT_KEEPALIVE_SECS", "10")),
        **stt_kwargs,
    )
    logger.info(f"Call language: {lang} (STT {stt_model} {stt_kwargs.get('mode', '')})")

    # VOICE_ENGINE toggle:
    #   "sarvam"        -> Gemini text LLM + Sarvam Bulbul TTS
    #   "gemini"        -> Sarvam STT -> Gemini Live native audio (no TTS stage)
    #   "gemini-direct" -> caller audio straight to Gemini Live (no STT, no TTS — lowest
    #                      latency; Gemini hears and speaks Kannada natively)
    voice_engine = (campaign or {}).get("VOICE_ENGINE") or os.getenv("VOICE_ENGINE", "sarvam")

    tts = None
    if voice_engine.startswith("gemini"):
        from google.genai.types import EndSensitivity, ThinkingConfig
        from pipecat.services.google.gemini_live.llm import (
            GeminiLiveLLMService,
            GeminiVADParams,
        )

        spoken = {
            "kn": "casual Bengaluru Kanglish (spoken Kannada mixed with English words)",
            "en": "natural, friendly Indian English",
        }[lang]
        gemini_voice_note = (
            "\n\n## VOICE MODE NOTE\nYou are speaking with your OWN voice (native audio, no TTS). "
            "Ignore every rule about how to WRITE text for a text-to-speech engine (scripts, "
            f"spelling numbers out) — just SPEAK naturally in {spoken}, warm and human, one "
            "short sentence per turn. Do not open replies with filler ('Sure', 'Okay', "
            "'Perfect', 'Great'); get to the substance at once. Keep every turn under about "
            "eight seconds of speech; never give a speech.\n"
            "GREET ONLY ONCE PER CALL. If you have already said your first line and the "
            "caller says 'hello' again, reply only 'Yes sir, I'm here' and carry on from "
            "where you were — never restart the introduction.\n"
            "SAY ONLY FACTS WRITTEN IN ## LIVE CAMPAIGN. Do not describe the company, its "
            "business, processes, timelines or amounts beyond what is written there — say "
            "'Let me have our team confirm that for you, sir.'"
        )
        settings_kwargs = dict(
            voice=os.getenv("GEMINI_VOICE", "Aoede"),
            # NOTE: do NOT set language here. Native-audio Live models auto-detect the
            # spoken language and hard-reject an explicit code (websocket 1007
            # "Unsupported language code 'kn-IN'" — verified on a live call 2026-07-03).
            # Server-side VAD owns end-of-turn in Live mode; make it call turns fast.
            # With the audio gate on, Gemini's server VAD never sees the caller stop (the
            # stream just goes quiet), so it never answered — verified 28 Sep. Disable it
            # and let pipecat send ActivityStart/ActivityEnd from the local Silero VAD
            # (VADUserStarted/StoppedSpeakingFrame), so Gemini only needs the speech.
            vad=(
                GeminiVADParams(disabled=True)
                if os.getenv("LIVE_AUDIO_GATE", "true").lower() != "false"
                else GeminiVADParams(
                    end_sensitivity=EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=400,
                )
            ),
            # The default native-audio preview model thinks before speaking; a
            # tele-caller needs speed over deliberation.
            # Gemini 2.5 takes thinking_budget=0; Gemini 3.x rejects budgets and takes a
            # level instead — "minimal" is the fastest it allows.
            thinking=(
                ThinkingConfig(thinking_level="minimal")
                if os.getenv("GEMINI_LIVE_MODEL", "").startswith("gemini-3")
                else ThinkingConfig(thinking_budget=0)
            ),
            # Latency fix: without this, Gemini Live reprocesses the ENTIRE growing
            # audio conversation every turn — measured TTFB climbed 1s -> 34s over 6
            # turns. Compression should cap the reprocessed window so latency stays flat.
            # NOTE: pipecat expects a plain dict here (it calls .get() on it), NOT the
            # ContextWindowCompressionParams object.
            # trigger_tokens must be LOW enough to fire on a SHORT call: a turn is only
            # ~400-450 audio tokens, so trigger=2500 didn't activate until ~turn 6 and
            # the first 5 turns grew unbounded (1->17s). 1024 fires after ~2 turns so the
            # window is capped early. If latency STILL climbs, native audio isn't honoring
            # compression -> fall back to a periodic session reset (reconnect + re-seed).
            context_window_compression={
                "enabled": True,
                # 1024 was tuned for 2.5 native audio. On 3.1 Flash Live it fired inside
                # a 2-minute call, dropped the start of the conversation, and Kavya
                # re-introduced herself mid-call (28 Sep). A 2-minute call is ~6k audio
                # tokens + ~2.5k prompt, so 12k keeps short calls intact.
                "trigger_tokens": int(os.getenv("GEMINI_CWC_TRIGGER", "12000")),
            },
        )
        # Same controls the cascade got after the invented "CIBIL" line: low temperature,
        # and a hard output cap. Live output tokens are AUDIO tokens (~25/s), so 300 is
        # ~12s — a backstop for the 15-20s monologues seen on the 28 Sep call.
        settings_kwargs["temperature"] = float(os.getenv("GEMINI_TEMPERATURE", "0.2"))
        settings_kwargs["max_tokens"] = int(os.getenv("GEMINI_LIVE_MAX_TOKENS", "300"))
        if os.getenv("GEMINI_LIVE_MODEL"):
            settings_kwargs["model"] = os.getenv("GEMINI_LIVE_MODEL")
        class MeteredGeminiLive(GeminiLiveLLMService):
            """Sum Gemini Live's own per-turn token counts, split by modality.

            Live bills audio and text at different rates and re-bills the whole context
            every turn; dashboards lag hours and mix in other traffic. This gives an
            exact per-call figure in the log instead.
            """

            usage = {"in_text": 0, "in_audio": 0, "out_text": 0, "out_audio": 0, "turns": 0}

            async def _handle_msg_usage_metadata(self, message):
                u = message.usage_metadata
                if u:
                    self.usage["turns"] += 1
                    for side, details in (("in", u.prompt_tokens_details), ("out", u.response_tokens_details)):
                        for d in details or []:
                            kind = "audio" if "AUDIO" in str(d.modality) else "text"
                            self.usage[f"{side}_{kind}"] += d.token_count or 0
                await super()._handle_msg_usage_metadata(message)

            def cost_inr(self) -> float:
                # $/1M tokens, Gemini 3.1 Flash Live list price (ai.google.dev, 28 Sep 2026)
                rate = {"in_text": 0.75, "in_audio": 3.0, "out_text": 4.5, "out_audio": 12.0}
                usd = sum(self.usage[k] * rate[k] / 1e6 for k in rate)
                return _google_inr(usd)

        llm = MeteredGeminiLive(
            api_key=os.getenv("GOOGLE_API_KEY"),
            # Seed the conversation WITHOUT asking for a reply. pipecat only starts
            # forwarding caller audio after this initial seed, so the seed must go out
            # at connect — but if it also triggers a reply, Kavya greets over the
            # callee's "Hello" and then greets again (28 Sep). With this off she listens
            # from the first second and answers the "Hello" with one greeting.
            inference_on_context_initialization=False,
            system_instruction=(
                build_system_instruction(campaign, live=True)
                # the compact Live prompt already carries the voice-mode rules
                if (LANGUAGE_PROFILES[lang].get("live_prompt_file") or Path("/nonexistent")).exists()
                else build_system_instruction(campaign) + gemini_voice_note
            ),
            settings=GeminiLiveLLMService.Settings(**settings_kwargs),
        )
    else:
        # Cascade: Sarvam STT -> Gemini TEXT LLM -> {Sarvam TTS | Gemini TTS}.
        # "hybrid" keeps Gemini's Aoede voice (via Vertex, streamed); "sarvam" uses
        # Sarvam Bulbul (fastest voice). Gemini text is fast (~0.5s); the voice choice
        # is the latency/quality tradeoff.
        llm = GoogleLLMService(
            api_key=os.getenv("GOOGLE_API_KEY"),
            model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"),
            # HARD CAP on output length. Default 4096 let the model dump a long "here are
            # all the steps/documents" answer -> the Kannada block took 16s to synthesize
            # (pipecat's sentence splitter doesn't chunk Kannada script, so the whole reply
            # goes to TTS at once). A phone turn is 1-2 short sentences (~40-80 tokens);
            # 160 keeps replies short so TTS stays ~1s. Prompt also tells it to chunk lists.
            params=GoogleLLMService.InputParams(
                # 0.2 not 0.4: at 0.4 Flash-Lite invented "our system checks your CIBIL
                # score automatically" on a real call — a fact nowhere in the campaign.
                temperature=float(os.getenv("GEMINI_TEMPERATURE", "0.2")),
                # 80 not 160: the prompt's "one sentence" rule alone still produced
                # 3-sentence, 12-second turns. ~80 tokens is two short sentences at most.
                max_tokens=int(os.getenv("GEMINI_MAX_TOKENS", "80")),
            ),
        )
        if voice_engine == "hybrid":
            from pipecat.services.google.tts import GeminiTTSService

            # location must be a real regional prefix or omitted. Pipecat builds
            # "{location}-texttospeech.googleapis.com"; "global" -> 404. Leave it unset
            # to use the default endpoint (verified working at 0.94s first-byte).
            # Delivery/tone lives in the TTS `prompt` (natural-language style steering),
            # NOT the system prompt (that controls wording). Default steers Aoede away
            # from the polished "news anchor" read toward relaxed phone-chat. Override
            # via GEMINI_TTS_PROMPT to retune the vibe without code edits.
            # NOTE: Gemini voices have no numeric speaking_rate (Chirp/Journey only) —
            # pace is steered by wording here. "relaxed pace" made it audibly SLOW;
            # ask for normal brisk phone speed explicitly.
            tts_style = os.getenv(
                "GEMINI_TTS_PROMPT",
                "Speak like a real, natural tele-caller on a live customer phone call — a "
                "warm, friendly, confident Bangalore call-center agent. Speak at a NORMAL, "
                "energetic phone-conversation speed — the brisk pace of a busy call-center "
                "agent, never slow, never dragging, no long pauses between words. "
                "Conversational and human, normal everyday intonation, polite and genuinely "
                "engaged. NOT reading a script, NOT a formal news anchor or announcer, not "
                "over-enunciated, not dramatic.",
            )
            tts = GeminiTTSService(
                credentials_path=str(BASE_DIR / os.getenv("GCP_KEY_PATH", "gcp-key.json")),
                location=os.getenv("GCP_LOCATION") or None,
                voice_id=os.getenv("GEMINI_VOICE", "Aoede"),
                model="gemini-2.5-flash-tts",  # outputs 24kHz; pipeline downsamples to 8kHz
                params=GeminiTTSService.InputParams(prompt=tts_style),
            )
        elif os.getenv("TTS_PROVIDER", "sarvam").lower() == "gemini-tts":
            from gemini_genai_tts import GeminiGenAITTSService

            # Gemini 3.8 TTS via the Gemini API: ~1/3 of Chirp 3 HD per call, but first
            # audio ~1.1s vs ~0.19s (measured 28 Sep). Same Aoede voice family.
            tts = GeminiGenAITTSService(
                api_key=os.getenv("GOOGLE_API_KEY"),
                model=os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-lite-tts"),
                voice=os.getenv("GEMINI_TTS_VOICE", "Aoede"),
                sample_rate=TWILIO_SAMPLE_RATE,
            )
        elif os.getenv("TTS_PROVIDER", "sarvam").lower() == "google":
            from pipecat.services.google.tts import GoogleHttpTTSService

            # Google Cloud TTS Standard/WaveNet: ~$4 per 1M chars vs Bulbul v3's ~₹3 per
            # 1K — the voice was over half of the per-minute cost. Needs a service-account
            # JSON (Cloud TTS rejects API keys). Rendered straight at 8 kHz LINEAR16 so
            # nothing is resampled for the phone line. Voice per language, from .env.
            voice = os.getenv(f"GOOGLE_TTS_VOICE_{lang.upper()}") or {
                "en": "en-IN-Wavenet-A",
                "kn": "kn-IN-Wavenet-A",
            }[lang]
            # Two ways to authenticate. Preferred: Workload Identity Federation — the box's
            # AWS role is exchanged for Google credentials, no secret on disk (and the org
            # blocks SA keys via iam.disableServiceAccountKeyCreation). Point
            # GOOGLE_APPLICATION_CREDENTIALS at the non-secret external_account config and
            # pipecat's default() path picks it up. Fallback: a service-account key file.
            key_file = BASE_DIR / os.getenv("GCP_KEY_PATH", "gcp-key.json")
            # Chirp 3 HD voices (natural, user-picked "Aoede" on 28 Sep — WaveNet sounded
            # robotic on a real call) support STREAMING synthesis, so audio starts before
            # the sentence is fully rendered. Standard/WaveNet only work over HTTP.
            if "chirp3" in voice.lower():
                from pipecat.services.google.tts import GoogleTTSService as GoogleTTSClass
            else:
                GoogleTTSClass = GoogleHttpTTSService
            tts = GoogleTTSClass(
                credentials_path=None if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") else str(key_file),
                voice_id=voice,
                sample_rate=TWILIO_SAMPLE_RATE,
                params=GoogleTTSClass.InputParams(language=profile["language"]),
            )
        else:
            # .env is authoritative for the voice — a stale dashboard campaign value can
            # otherwise force an incompatible speaker/model combo (e.g. kavya on v2) and
            # every TTS call 400s -> dead call. Model+speaker MUST be a valid Sarvam pair.
            tts_model = os.getenv("SARVAM_TTS_MODEL") or (campaign or {}).get("SARVAM_TTS_MODEL") or "bulbul:v3"
            speaker = os.getenv("SARVAM_TTS_SPEAKER") or (campaign or {}).get("SARVAM_TTS_SPEAKER") or "kavya"
            tts = SarvamTTSService(
                api_key=os.getenv("SARVAM_API_KEY"),
                model=tts_model,
                voice_id=speaker,
                sample_rate=TWILIO_SAMPLE_RATE,
                params=SarvamTTSService.InputParams(
                    language=profile["language"],
                    pace=1.05,
                    pitch=0.0,
                    loudness=1.0,
                ),
            )

    # Conversation context. Both paths use the universal LLMContext API. The cascade
    # (sarvam/hybrid) used to run on OpenAILLMContext, but Gemini 3.x returns thought
    # signatures that pipecat appends as LLMSpecificMessage objects, and the legacy
    # GoogleAssistantContextAggregator crashes on them ("'LLMSpecificMessage' object is
    # not subscriptable") at the end of the first bot turn — every later turn is lost.
    if voice_engine.startswith("gemini"):
        from pipecat.frames.frames import VADUserStoppedSpeakingFrame
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
            LLMUserAggregatorParams,
        )
        from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
        from pipecat.turns.user_stop.base_user_turn_stop_strategy import (
            BaseUserTurnStopStrategy,
        )
        from pipecat.turns.user_turn_strategies import UserTurnStrategies

        class VADImmediateUserTurnStopStrategy(BaseUserTurnStopStrategy):
            """Close the user turn the moment local VAD reports silence.

            Every stop strategy pipecat ships requires STT transcription text to
            fire — there is no STT in gemini/gemini-direct mode, so they never
            trigger and every turn falls to the aggregator's stop timeout
            (default 5s of dead air). Gemini Live does its own server-side turn
            handling; locally we only need the turn closed for context/transcript
            bookkeeping, so plain VAD silence is the right signal.
            """

            async def process_frame(self, frame):
                if isinstance(frame, VADUserStoppedSpeakingFrame):
                    await self.trigger_user_turn_stopped()

        # Seed one short kickoff message: with an empty context the service re-sends
        # the entire multi-KB system instruction as the first turn to coax a greeting.
        context = LLMContext(
            messages=[{
                "role": "user",
                "content": "(The call just connected. Wait for the callee to say hello, then "
                           "greet them ONCE with your first line.)",
            }]
        )
        aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=UserTurnStrategies(
                    start=[VADUserTurnStartStrategy()],
                    stop=[VADImmediateUserTurnStopStrategy()],
                ),
                user_turn_stop_timeout=1.5,  # safety net only; VAD strategy fires first
            ),
        )
    else:
        # Import the VAD frames HERE too: the gemini branch above imports
        # VADUserStoppedSpeakingFrame locally, which makes the name local to all of
        # run_bot — the module-level import is then invisible in this branch.
        from pipecat.frames.frames import (
            TranscriptionFrame,
            VADUserStartedSpeakingFrame,
            VADUserStoppedSpeakingFrame,
        )
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
            LLMUserAggregatorParams,
        )
        from pipecat.turns.types import ProcessFrameResult
        from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
        from pipecat.turns.user_stop.base_user_turn_stop_strategy import (
            BaseUserTurnStopStrategy,
        )
        from pipecat.turns.user_turn_strategies import UserTurnStrategies

        class TranscriptAfterVADStopStrategy(BaseUserTurnStopStrategy):
            """End the user turn the moment STT delivers text after local VAD silence.

            The default stop strategy (smart-turn + STT P99 timer) waited a FIXED
            ~1.3s after VAD silence on every turn — sized for Sarvam's worst case,
            while on real Exotel calls Sarvam returned the final segment in
            0.42-0.49s. Triggering on that transcript cuts ~0.85s off every reply.
            The fallback covers a slow/missing final transcript: it fires only if
            some text already exists, else the next transcript still triggers.
            """

            def __init__(self, fallback_secs: float, **kwargs):
                super().__init__(**kwargs)
                self._fallback_secs = fallback_secs
                self._text = ""
                self._vad_stopped = False
                self._task = None

            async def _cancel(self):
                if self._task:
                    await self.task_manager.cancel_task(self._task)
                    self._task = None

            async def reset(self):
                await super().reset()
                self._text = ""
                self._vad_stopped = False
                await self._cancel()

            async def cleanup(self):
                await super().cleanup()
                await self._cancel()

            async def _fallback(self):
                try:
                    await asyncio.sleep(self._fallback_secs)
                except asyncio.CancelledError:
                    return
                self._task = None
                if self._text:
                    await self.trigger_user_turn_stopped()

            async def process_frame(self, frame):
                if isinstance(frame, VADUserStartedSpeakingFrame):
                    self._vad_stopped = False
                    await self._cancel()
                elif isinstance(frame, VADUserStoppedSpeakingFrame):
                    self._vad_stopped = True
                    await self._cancel()
                    self._task = self.task_manager.create_task(
                        self._fallback(), f"{self}::fallback"
                    )
                elif isinstance(frame, TranscriptionFrame) and frame.text.strip():
                    self._text += frame.text
                    if self._vad_stopped:
                        await self._cancel()
                        await self.trigger_user_turn_stopped()
                return ProcessFrameResult.CONTINUE

        # The compact prompt (written for Live) is ~1/3 the size of the full rulebook:
        # measured ~0.15s faster Gemini first-token and cheaper per turn, same rules.
        instruction = build_system_instruction(
            campaign, live=os.getenv("COMPACT_PROMPT", "true").lower() != "false"
        )
        if os.getenv("FILLERS_ENABLED", "true").lower() != "false" and (FILLERS_DIR / lang).is_dir():
            # The phone line already says "Okay sir"/"Sure" before longer answers —
            # without this Gemini opens with its own "Sure, sir" and the caller hears
            # the acknowledgement twice.
            instruction += (
                "\n\n## ACKNOWLEDGEMENT NOTE\nOn longer questions the phone system plays a "
                "short acknowledgement like 'Okay sir' or 'Sure' in your voice just before "
                "your reply. So NEVER begin a reply with an acknowledgement or filler — not "
                "'Sure', 'Okay', 'Got it', 'Great', 'That's great', 'That's perfect', "
                "'Perfect', 'Thank you sir', 'Great question'. Start with the substance."
            )
        context = LLMContext(messages=[{"role": "system", "content": instruction}])
        aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=UserTurnStrategies(
                    start=[VADUserTurnStartStrategy()],
                    stop=[TranscriptAfterVADStopStrategy(
                        fallback_secs=float(os.getenv("TURN_STOP_FALLBACK_SECS", "0.9"))
                    )],
                ),
            ),
        )

    # Captures both sides of the conversation and streams it to the demo dashboard.
    transcript = TranscriptProcessor()

    # Tiered "please hold" flow that covers up to ~7-10s of Gemini slow-TTFB: an ack at
    # ~1.1s, mid-wait reassurance at ~4s, close-out at ~7.5s — each tier plays out fully
    # before the next, cancelled the instant the real answer arrives. FILLER_FLOW picks
    # the domain (account|transaction|loan|universal); universal is the safe default.
    # FILLERS_ENABLED=false disables them entirely (empty flows -> inert pass-through).
    if os.getenv("FILLERS_ENABLED", "true").lower() == "false":
        filler_flows, filler_quick = {}, []
    else:
        filler_flows, filler_quick = load_filler_flows(lang)
    flow_name = (campaign or {}).get("FILLER_FLOW") or os.getenv("FILLER_FLOW", "universal")
    # delay = how long a turn must stall before the filler fires. Keep it above a normal
    # snappy turn so it only covers genuinely slow ones (env FILLER_DELAY).
    filler = FillerInjector(
        filler_flows, filler_quick, flow_name=flow_name,
        # 0.35s: the ack lands while Gemini is still writing (~1s), so the caller hears
        # "Okay sir" almost at once instead of ~2s of dead air before the real answer.
        delay=float(os.getenv("FILLER_DELAY", "0.35")),
        # Only long/complex questions (caller spoke >= this many seconds) earn a filler.
        min_query_secs=float(os.getenv("FILLER_MIN_QUERY_SECS", "2.5")),
        probability=float(os.getenv("FILLER_PROBABILITY", "0.5")),
    )

    stt_gate = None  # set in the cascade branch; read by the disconnect handler
    cost_meter = CostMeter()
    call_started = time.monotonic()
    if voice_engine == "gemini-direct":
        # Raw audio -> Gemini Live -> raw audio. The aggregator MUST be in the path (it
        # converts LLMRunFrame/turn-ends into generation triggers — without it Gemini
        # never responds), but it runs the fast VAD-timeout strategies configured above.
        live_stages = [transport.input()]  # Twilio audio in
        if os.getenv("LIVE_AUDIO_GATE", "true").lower() != "false":
            # Same gate as the STT path. Every streamed second — silence and the time the
            # bot is talking included — is billed as input AND kept in the context Live
            # re-bills on every turn. The gate passes speech plus the VAD's own trailing
            # silence (stop_secs), which is longer than Gemini's silence_duration_ms, so
            # its server-side turn detection still sees the caller stop.
            stt_gate = STTAudioGate(
                preroll_secs=float(os.getenv("STT_PREROLL_SECS", "0.4")),
                sample_rate=TWILIO_SAMPLE_RATE,
            )
            live_stages.append(stt_gate)
        pipeline = Pipeline(live_stages + [
            aggregator.user(),      # fast VAD-based turn trigger
            LiveTranscriptLogger(), # caller's words (pushed upstream by Gemini Live)
            llm,                    # Gemini Live: hears + thinks + speaks
            LiveTranscriptLogger(), # Kavya's words (downstream)
            filler,                 # covers slow-TTFB turns with a spoken "Hmm"/"Okay"
            transport.output(),     # Twilio audio out
            aggregator.assistant(), # context bookkeeping
        ])
    else:
        # Predictive answer cache (hybrid/sarvam only): while the customer is speaking,
        # a background flash-lite call pre-writes answers to their most likely next
        # questions; a semantic hit skips the live LLM turn (~1-1.5s faster). Miss =
        # normal path. Tune from logs: grep PREDICT logs/call_debug.log
        stages = [transport.input()]  # Twilio -> frames
        if os.getenv("STT_GATE", "true").lower() != "false":
            stt_gate = STTAudioGate(
                preroll_secs=float(os.getenv("STT_PREROLL_SECS", "0.4")),
                sample_rate=TWILIO_SAMPLE_RATE,
            )
            stages.append(stt_gate)  # only the caller's speech reaches (paid) STT
        stages += [
            stt,                    # speech -> text (Kannada/Kanglish)
            transcript.user(),      # capture customer turn
            aggregator.user(),      # add user turn to context
        ]
        if voice_engine in ("hybrid", "sarvam") and os.getenv(
            "PREDICTIVE_CACHE", "true"
        ).lower() != "false":
            from predictive_cache import PredictiveCache

            stages.append(
                PredictiveCache(
                    context=context,
                    api_key=os.getenv("GOOGLE_API_KEY"),
                    campaign=campaign,
                    language=lang,
                    model=os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"),
                    threshold=float(os.getenv("PREDICT_THRESHOLD", "0.80")),
                )
            )
        stages.append(llm)          # Gemini brain (only runs on cache misses)
        if tts is not None:
            stages.append(tts)      # text -> speech (Sarvam Bulbul / Gemini Cloud TTS)
            stages.append(filler)   # cover slow TTS turns with a spoken "one minute sir"
        else:
            stages.append(filler)   # gemini mode: cover slow Live TTFB turns
        stages += [
            transport.output(),     # frames -> Twilio
            aggregator.assistant(), # add bot turn to context
            transcript.assistant(), # capture agent turn
            cost_meter,             # sums LLM-token / TTS-char usage metrics
        ]
        pipeline = Pipeline(stages)

    @transcript.event_handler("on_transcript_update")
    async def _on_transcript(_proc, frame):
        for msg in frame.messages:
            await hub.publish({
                "type": "transcript",
                "role": "agent" if msg.role == "assistant" else "customer",
                "content": msg.content,
            })

    # Gemini Live expects 16kHz PCM input; Twilio delivers 8kHz. Run the pipeline input
    # at 16kHz so the transport upsamples the phone audio before Gemini hears it —
    # otherwise Gemini gets half-rate audio and cannot understand the caller (it falls
    # back to "network problem, say again"). Output stays 8kHz for Twilio.
    # Silero VAD is also happier at 16kHz.
    # Sarvam STT (used in gemini/hybrid) prefers 16kHz; pure-sarvam mode ran at 8kHz fine.
    audio_in_rate = TWILIO_SAMPLE_RATE if voice_engine == "sarvam" else 16000
    # Gemini TTS ONLY outputs 24kHz. Forcing the pipeline output to 8kHz made Cloud TTS
    # produce 24kHz audio tagged as 8kHz -> played 3x slow ("naaammaasstee"). Run hybrid
    # output at 24kHz; the Twilio serializer resamples 24kHz -> 8kHz mu-law for the call.
    audio_out_rate = 24000 if voice_engine == "hybrid" else TWILIO_SAMPLE_RATE
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=audio_in_rate,
            audio_out_sample_rate=audio_out_rate,
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,  # LLM tokens + TTS chars for the per-call cost log
        ),
    )

    callee_spoke = {"yes": False}

    @aggregator.user().event_handler("on_user_turn_started")
    async def _on_callee_turn(*_args):
        callee_spoke["yes"] = True

    # Speak the opening line the moment the call connects.
    @transport.event_handler("on_client_connected")
    async def _on_connect(_transport, _client):
        await hub.publish({"type": "status", "status": f"in-call ({voice_engine} voice)"})
        from pipecat.frames.frames import LLMRunFrame

        # Outbound callees answer with "Hello". Greeting at once AND answering that
        # "Hello" ran the LLM twice and Kavya said her greeting twice (28 Sep call).
        # So: prime the context, give the callee a moment, and let their "Hello" turn
        # trigger the one greeting; greet unprompted only if they stay silent.
        # (Gemini Live hears the "Hello" itself and answers it with the greeting, so it
        # only needs the silent-callee fallback; the cascade also needs the kickoff line.)
        if not voice_engine.startswith("gemini"):
            context.add_message({"role": "user", "content": f"({profile['kickoff']})"})

        if voice_engine.startswith("gemini"):
            # Send the seed now (no reply requested) so Gemini hears the caller at once.
            await task.queue_frames([LLMRunFrame()])

        async def _greet_if_silent():
            # Two cases end in Kavya greeting first: the callee stays silent, OR the callee
            # spoke but nothing came of it. The second happened in a test on 28 Sep: a
            # "Hello?" 0.8s after pickup got no transcript (STT socket still opening), the
            # old check saw "callee spoke", skipped the greeting, and she never said a word.
            await asyncio.sleep(float(os.getenv("GREETING_WAIT_SECS", "1.5")))
            if callee_spoke["yes"]:
                await asyncio.sleep(float(os.getenv("GREETING_GRACE_SECS", "3.0")))
            if filler.bot_has_spoken:
                return
            if voice_engine.startswith("gemini"):
                # Callee said nothing: ask Live for the greeting explicitly. This is
                # pipecat's own one-shot "create a response from these messages" path.
                await llm._create_single_response([{
                    "role": "user",
                    "content": "(The callee is silent. Say your first line now.)",
                }])
            else:
                await task.queue_frames([LLMRunFrame()])

        asyncio.create_task(_greet_if_silent())

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnect(_transport, _client):
        if hasattr(llm, "cost_inr"):
            logger.info(f"GEMINI LIVE usage: {llm.usage} -> ~Rs {llm.cost_inr():.2f} this call")
        if stt_gate is not None:
            sent, total = stt_gate.stats()
            logger.info(f"STT audio billed: {sent:.1f}s of {total:.1f}s call audio "
                        f"({(sent / total * 100) if total else 0:.0f}%)")
            if not voice_engine.startswith("gemini"):
                provider = os.getenv("TTS_PROVIDER", "sarvam").lower()
                voice_name = {
                    "google": os.getenv(f"GOOGLE_TTS_VOICE_{lang.upper()}", ""),
                    "gemini-tts": f"{os.getenv('GEMINI_TTS_MODEL', 'gemini-3.8-flash-lite-tts')}/"
                                  f"{os.getenv('GEMINI_TTS_VOICE', 'Aoede')}",
                }.get(provider, "sarvam-bulbul")
                logger.info(cost_meter.report(voice_name, sent, time.monotonic() - call_started,
                                              tts_audio_secs=getattr(tts, "audio_secs", None)))
        await hub.publish({"type": "status", "status": "ended"})
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
