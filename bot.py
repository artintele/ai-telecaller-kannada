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


def load_filler_flows() -> tuple[dict[str, list[tuple[bytes, float]]], list[tuple[bytes, float]]]:
    """Load the tiered stall flows + quick-stall pool from assets/fillers/.

    Returns (flows, quick) where flows[name] is the ordered tier list
    [(pcm, dur), ...] and quick is the shared short-opener pool. Missing files
    are skipped so a partial regenerate still runs.
    """
    flows: dict[str, list[tuple[bytes, float]]] = {}
    if FILLERS_DIR.is_dir():
        for name in FILLER_FLOW_NAMES:
            tiers = [
                _load_wav(FILLERS_DIR / f"{name}_{tier}.wav")
                for tier in range(3)
                if (FILLERS_DIR / f"{name}_{tier}.wav").exists()
            ]
            if tiers:
                flows[name] = tiers
    quick = [
        _load_wav(FILLERS_DIR / f"quick_{i}.wav")
        for i in range(QUICK_STALL_COUNT)
        if (FILLERS_DIR / f"quick_{i}.wav").exists()
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
        self._sample_rate = sample_rate
        self._filler_task = None
        self._last_quick = -1
        self._user_start_t = None  # monotonic time the current user utterance began
        self._ready = False  # no fillers until the greeting has been spoken

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
                await self._cancel_pending()
                self._filler_task = self.create_task(self._play_flow())
        elif isinstance(frame, (TTSStartedFrame, TTSAudioRawFrame, InterruptionFrame)):
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


def build_system_instruction(campaign: dict | None = None) -> str:
    """Combine the dialect system prompt + the Bangalore lexicon into one instruction."""
    prompt = SYSTEM_PROMPT_FILE.read_text(encoding="utf-8")

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

    stt = SarvamSTTService(
        api_key=os.getenv("SARVAM_API_KEY"),
        model=os.getenv("SARVAM_STT_MODEL", "saarika:v2.5"),
        # Kannada; Sarvam handles code-mixed "Kanglish". language goes inside InputParams.
        params=SarvamSTTService.InputParams(language=Language.KN_IN),
    )

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

        gemini_voice_note = (
            "\n\n## VOICE MODE NOTE\nYou are speaking with your OWN voice (native audio, no TTS). "
            "Ignore all script-formatting rules in section 3 about Kannada script vs Latin — just "
            "SPEAK naturally in casual Bengaluru Kanglish (spoken Kannada mixed with English words), "
            "warm and human, short turns. The phone system sometimes plays a short 'Hmm'/'Okay' "
            "acknowledgment in your voice before your reply reaches the caller — so do NOT open "
            "with long filler phrases yourself; get to the substance quickly and naturally."
        )
        settings_kwargs = dict(
            voice=os.getenv("GEMINI_VOICE", "Aoede"),
            # NOTE: do NOT set language here. Native-audio Live models auto-detect the
            # spoken language and hard-reject an explicit code (websocket 1007
            # "Unsupported language code 'kn-IN'" — verified on a live call 2026-07-03).
            # Server-side VAD owns end-of-turn in Live mode; make it call turns fast.
            vad=GeminiVADParams(
                end_sensitivity=EndSensitivity.END_SENSITIVITY_HIGH,
                silence_duration_ms=400,
            ),
            # The default native-audio preview model thinks before speaking; a
            # tele-caller needs speed over deliberation.
            thinking=ThinkingConfig(thinking_budget=0),
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
                "trigger_tokens": int(os.getenv("GEMINI_CWC_TRIGGER", "1024")),
            },
        )
        if os.getenv("GEMINI_LIVE_MODEL"):
            settings_kwargs["model"] = os.getenv("GEMINI_LIVE_MODEL")
        llm = GeminiLiveLLMService(
            api_key=os.getenv("GOOGLE_API_KEY"),
            system_instruction=build_system_instruction(campaign) + gemini_voice_note,
            settings=GeminiLiveLLMService.Settings(**settings_kwargs),
        )
    else:
        # Cascade: Sarvam STT -> Gemini TEXT LLM -> {Sarvam TTS | Gemini TTS}.
        # "hybrid" keeps Gemini's Aoede voice (via Vertex, streamed); "sarvam" uses
        # Sarvam Bulbul (fastest voice). Gemini text is fast (~0.5s); the voice choice
        # is the latency/quality tradeoff.
        llm = GoogleLLMService(
            api_key=os.getenv("GOOGLE_API_KEY"),
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            # HARD CAP on output length. Default 4096 let the model dump a long "here are
            # all the steps/documents" answer -> the Kannada block took 16s to synthesize
            # (pipecat's sentence splitter doesn't chunk Kannada script, so the whole reply
            # goes to TTS at once). A phone turn is 1-2 short sentences (~40-80 tokens);
            # 160 keeps replies short so TTS stays ~1s. Prompt also tells it to chunk lists.
            params=GoogleLLMService.InputParams(
                temperature=0.4,
                max_tokens=int(os.getenv("GEMINI_MAX_TOKENS", "160")),
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
        else:
            # .env is authoritative for the voice — a stale dashboard campaign value can
            # otherwise force an incompatible speaker/model combo (e.g. kavya on v2) and
            # every TTS call 400s -> dead call. Model+speaker MUST be a valid Sarvam pair.
            tts_model = os.getenv("SARVAM_TTS_MODEL") or (campaign or {}).get("SARVAM_TTS_MODEL") or "bulbul:v2"
            speaker = os.getenv("SARVAM_TTS_SPEAKER") or (campaign or {}).get("SARVAM_TTS_SPEAKER") or "anushka"
            tts = SarvamTTSService(
                api_key=os.getenv("SARVAM_API_KEY"),
                model=tts_model,
                voice_id=speaker,
                sample_rate=TWILIO_SAMPLE_RATE,
                params=SarvamTTSService.InputParams(
                    language=Language.KN_IN,
                    pace=1.05,
                    pitch=0.0,
                    loudness=1.0,
                ),
            )

    # Conversation context. Gemini Live uses the universal context API (its old-style
    # aggregator path is broken in pipecat 0.0.108); the big system prompt goes in via
    # system_instruction. Sarvam mode keeps the proven OpenAILLMContext path.
    messages = []
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
                "content": "(Call connected. Speak your FIRST LINE now — warm, short, Bengaluru Kanglish.)",
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
        from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext

        messages = [{"role": "system", "content": build_system_instruction(campaign)}]
        context = OpenAILLMContext(messages)
        aggregator = llm.create_context_aggregator(context)

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
        filler_flows, filler_quick = load_filler_flows()
    flow_name = (campaign or {}).get("FILLER_FLOW") or os.getenv("FILLER_FLOW", "universal")
    # delay = how long a turn must stall before the filler fires. Keep it above a normal
    # snappy turn so it only covers genuinely slow ones (env FILLER_DELAY).
    filler = FillerInjector(
        filler_flows, filler_quick, flow_name=flow_name,
        delay=float(os.getenv("FILLER_DELAY", "1.3")),
        # Only long/complex questions (caller spoke >= this many seconds) earn a filler.
        min_query_secs=float(os.getenv("FILLER_MIN_QUERY_SECS", "2.5")),
    )

    if voice_engine == "gemini-direct":
        # Raw audio -> Gemini Live -> raw audio. The aggregator MUST be in the path (it
        # converts LLMRunFrame/turn-ends into generation triggers — without it Gemini
        # never responds), but it runs the fast VAD-timeout strategies configured above.
        pipeline = Pipeline([
            transport.input(),      # Twilio audio in
            aggregator.user(),      # fast VAD-based turn trigger
            llm,                    # Gemini Live: hears + thinks + speaks
            filler,                 # covers slow-TTFB turns with a spoken "Hmm"/"Okay"
            transport.output(),     # Twilio audio out
            aggregator.assistant(), # context bookkeeping
        ])
    else:
        # Predictive answer cache (hybrid/sarvam only): while the customer is speaking,
        # a background flash-lite call pre-writes answers to their most likely next
        # questions; a semantic hit skips the live LLM turn (~1-1.5s faster). Miss =
        # normal path. Tune from logs: grep PREDICT logs/call_debug.log
        stages = [
            transport.input(),      # Twilio -> frames
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
        ),
    )

    # Speak the opening line the moment the call connects.
    @transport.event_handler("on_client_connected")
    async def _on_connect(_transport, _client):
        await hub.publish({"type": "status", "status": f"in-call ({voice_engine} voice)"})
        if voice_engine.startswith("gemini"):
            from pipecat.frames.frames import LLMRunFrame

            await task.queue_frames([LLMRunFrame()])
        else:
            messages.append({
                "role": "system",
                "content": "The call just connected. Speak your FIRST LINE now, warmly, in Bengaluru Kanglish.",
            })
            await task.queue_frames([aggregator.user().get_context_frame()])

    @transport.event_handler("on_client_disconnected")
    async def _on_disconnect(_transport, _client):
        await hub.publish({"type": "status", "status": "ended"})
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
