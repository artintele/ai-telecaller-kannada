"""
artintele.ai — Bengaluru Kannada (Kanglish) voice tele-caller.

Pipeline:  Twilio audio  ->  Sarvam STT  ->  Gemini LLM  ->  Sarvam TTS  ->  Twilio audio

This module builds the system instruction (dialect prompt + lexicon) and wires the
Pipecat pipeline. server.py handles the Twilio webhook + websocket and calls run_bot().

NOTE ON VERSIONS: Pipecat's service import paths change between releases. These imports
target pipecat-ai ~0.0.95. If an import fails after `pip install`, run
`python -c "import pipecat.services.sarvam.tts"` etc. and adjust to your installed version.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
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
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.5)),  # turn-taking + barge-in; 0.5s end-of-turn for snappier replies
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
        from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

        gemini_voice_note = (
            "\n\n## VOICE MODE NOTE\nYou are speaking with your OWN voice (native audio, no TTS). "
            "Ignore all script-formatting rules in section 3 about Kannada script vs Latin — just "
            "SPEAK naturally in casual Bengaluru Kanglish (spoken Kannada mixed with English words), "
            "warm and human, short turns."
        )
        llm = GeminiLiveLLMService(
            api_key=os.getenv("GOOGLE_API_KEY"),
            model=os.getenv("GEMINI_LIVE_MODEL") or None,  # None -> pipecat's default native-audio model
            voice_id=os.getenv("GEMINI_VOICE", "Aoede"),
            system_instruction=build_system_instruction(campaign) + gemini_voice_note,
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
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair

        context = LLMContext()
        aggregator = LLMContextAggregatorPair(context)
    else:
        from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext

        messages = [{"role": "system", "content": build_system_instruction(campaign)}]
        context = OpenAILLMContext(messages)
        aggregator = llm.create_context_aggregator(context)

    # Captures both sides of the conversation and streams it to the demo dashboard.
    transcript = TranscriptProcessor()

    if voice_engine == "gemini-direct":
        # Minimal path: raw audio -> Gemini Live -> raw audio. NO local turn detection —
        # Gemini's server-side VAD decides turn ends in ~ms. Our aggregator's turn
        # strategies expect STT transcriptions and add ~9s of timeout lag without them.
        pipeline = Pipeline([
            transport.input(),   # Twilio audio in
            llm,                 # Gemini Live: hears + thinks + speaks
            transport.output(),  # Twilio audio out
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
