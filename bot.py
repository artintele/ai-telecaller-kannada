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
import wave
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
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


def load_filler_clips() -> list[bytes]:
    """Load pre-generated 8kHz PCM filler clips (Aoede voice, via Gemini TTS)."""
    clips = []
    if FILLERS_DIR.is_dir():
        for path in sorted(FILLERS_DIR.glob("*.wav")):
            with wave.open(str(path), "rb") as w:
                clips.append(w.readframes(w.getnframes()))
    return clips


class FillerInjector(FrameProcessor):
    """Speak a short pre-recorded acknowledgment while Gemini is still thinking.

    Gemini Live's TTFB is bimodal (measured: 0.2-1s usually, but 6-12s outliers).
    The delay is deliberately generous: normal turns (even slow-ish ones) get the
    real answer with NO filler; only a genuine multi-second stall gets covered by
    a soft non-word backchannel ("mm", "hmm", "haan") in the bot's own voice.
    Cancelled instantly if the real answer arrives first, if the caller resumes
    speaking, or on interruption.
    """

    def __init__(self, clips: list[bytes], delay: float = 1.2, sample_rate: int = 8000):
        super().__init__()
        self._clips = clips
        self._delay = delay
        self._sample_rate = sample_rate
        self._filler_task = None
        self._last_clip = -1

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStoppedSpeakingFrame) and self._clips:
            await self._cancel_pending()
            self._filler_task = self.create_task(self._play_filler_after_delay())
        elif isinstance(
            frame, (TTSStartedFrame, TTSAudioRawFrame, VADUserStartedSpeakingFrame, InterruptionFrame)
        ):
            await self._cancel_pending()
        await self.push_frame(frame, direction)

    async def _cancel_pending(self):
        if self._filler_task:
            task, self._filler_task = self._filler_task, None
            await self.cancel_task(task)

    async def _play_filler_after_delay(self):
        await asyncio.sleep(self._delay)
        self._filler_task = None
        choices = [i for i in range(len(self._clips)) if i != self._last_clip]
        self._last_clip = random.choice(choices) if choices else 0
        pcm = self._clips[self._last_clip]
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


async def run_bot(websocket, stream_sid: str, call_sid: str, campaign: dict | None = None):
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
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.5, start_secs=0.12)),
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
                silence_duration_ms=600,
            ),
            # The default native-audio preview model thinks before speaking; a
            # tele-caller needs speed over deliberation.
            thinking=ThinkingConfig(thinking_budget=0),
            # Latency fix: without this, Gemini Live reprocesses the ENTIRE growing
            # audio conversation every turn — measured TTFB climbed 1s -> 14s over a
            # call. Compression keeps the reprocessed window small so latency stays flat.
            # NOTE: pipecat expects a plain dict here (it calls .get() on it), NOT the
            # ContextWindowCompressionParams object.
            context_window_compression={"enabled": True, "trigger_tokens": 8000},
        )
        if os.getenv("GEMINI_LIVE_MODEL"):
            settings_kwargs["model"] = os.getenv("GEMINI_LIVE_MODEL")
        llm = GeminiLiveLLMService(
            api_key=os.getenv("GOOGLE_API_KEY"),
            system_instruction=build_system_instruction(campaign) + gemini_voice_note,
            settings=GeminiLiveLLMService.Settings(**settings_kwargs),
        )
    else:
        llm = GoogleLLMService(
            api_key=os.getenv("GOOGLE_API_KEY"),
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            params=GoogleLLMService.InputParams(temperature=0.4),
        )
        speaker = (campaign or {}).get("SARVAM_TTS_SPEAKER") or os.getenv("SARVAM_TTS_SPEAKER", "kavya")
        tts_model = (campaign or {}).get("SARVAM_TTS_MODEL") or os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
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

    filler = FillerInjector(clips=load_filler_clips(), delay=1.2)

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
        stages = [
            transport.input(),      # Twilio -> frames
            stt,                    # speech -> text (Kannada/Kanglish)
            transcript.user(),      # capture customer turn
            aggregator.user(),      # add user turn to context
            llm,                    # Gemini brain (text->text, or text->audio in Live mode)
        ]
        if tts is not None:
            stages.append(tts)      # text -> speech (Sarvam Bulbul) — sarvam mode only
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

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
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
