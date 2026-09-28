"""
Gemini TTS (AI Studio / google-genai) as a Pipecat TTS service.

Why this exists: pipecat 0.0.108's GeminiTTSService goes through the Cloud TTS API and
only knows the older gemini-2.5 TTS models. The Gemini 3.8 TTS models (priced per audio
second — about a third of Chirp 3 HD per call on 28 Sep 2026) are only reachable via
the Gemini API, with the same GOOGLE_API_KEY the LLM uses.

Measured from the box on 28 Sep 2026: first audio ~1.1s vs Chirp 3 HD's ~0.19s, so this
is the cheaper voice, not the faster one.

Do NOT put style instructions in the text ("Say this warmly: ..."): the model read one
aloud, making a 3.8s line 10.3s long.
"""

import os
from typing import AsyncGenerator

from loguru import logger
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.tts_service import TTSService

GEMINI_TTS_RATE = 24000  # audio/l16; rate=24000; channels=1


class GeminiGenAITTSService(TTSService):
    def __init__(self, *, api_key: str, model: str, voice: str, sample_rate: int = 8000, **kwargs):
        super().__init__(sample_rate=sample_rate, **kwargs)
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._voice = voice
        self._resampler = create_stream_resampler()
        self.audio_secs = 0.0  # billed per second of generated audio — read by CostMeter

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        from google.genai import types

        logger.debug(f"{self}: Generating TTS [{text}]")
        cfg = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice)
                )
            ),
        )
        try:
            await self.start_tts_usage_metrics(text)
            stream = await self._client.aio.models.generate_content_stream(
                model=self._model, contents=text, config=cfg
            )
            async for chunk in stream:
                cand = chunk.candidates[0] if chunk.candidates else None
                for part in (cand.content.parts if cand and cand.content else []) or []:
                    data = part.inline_data.data if part.inline_data else None
                    if not data:
                        continue
                    self.audio_secs += len(data) / (GEMINI_TTS_RATE * 2)
                    pcm = await self._resampler.resample(data, GEMINI_TTS_RATE, self.sample_rate)
                    if pcm:
                        await self.stop_ttfb_metrics()
                        yield TTSAudioRawFrame(pcm, self.sample_rate, 1, context_id=context_id)
        except Exception as e:
            yield ErrorFrame(error=f"Gemini TTS error: {e}")
