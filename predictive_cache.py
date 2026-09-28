"""
Predictive answer cache — "think while the customer is talking", like a human agent.

The cascade can't start the LLM mid-utterance (Sarvam STT emits only FINAL transcripts,
no interims), so instead we anticipate BETWEEN turns: the moment the bot finishes
speaking, a background flash-lite call predicts the 2-3 most likely next customer
utterances (given the conversation trajectory + campaign) and pre-writes the answers in
the agent's register. When the customer's actual question arrives, we embed it and
cosine-match against the predictions:

    HIT  -> emit the pre-written answer straight to TTS (skip the live LLM turn
            entirely: saves the LLM TTFB + first-sentence time, ~1-1.5s)
    MISS -> pass through to the normal LLM path, nothing lost

Wiring (bot.py, sarvam/hybrid modes): placed between aggregator.user() and llm.
- BotStoppedSpeakingFrame (broadcast upstream by the output transport) = "bot turn
  done" -> fire the prediction task.
- OpenAILLMContextFrame flowing downstream = "user turn triggered the LLM" -> match;
  on a hit we CONSUME the frame (LLM never runs) and emit the canonical LLM trio
  (LLMFullResponseStartFrame -> LLMTextFrame -> LLMFullResponseEndFrame) so the TTS
  and the assistant context aggregator treat it exactly like a real LLM answer.

Every hit/miss is logged with similarity so the threshold can be tuned from
logs/call_debug.log (grep "PREDICT").
"""

import asyncio
import json
import math
import os

from loguru import logger

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContextFrame
from pipecat.processors.frame_processor import FrameProcessor

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768  # smaller = faster; plenty for short-utterance matching

PREDICT_PROMPT = """You are the anticipation brain of "Kavya", a Bengaluru Kanglish tele-caller.
Given the conversation so far, predict the {n} most likely things the CUSTOMER will say next,
and pre-write Kavya's answer for each.

{style}
- SHORT: 1-2 spoken sentences, ~12-25 words. One idea, then hand the turn back.
- Only use facts from the conversation/campaign below. NEVER invent prices, dates or details.

CAMPAIGN:
{campaign}

CONVERSATION (most recent last):
{convo}

Return ONLY JSON: [{{"q": "...", "a": "..."}}, ...]"""


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


STYLE = {
    "kn": """Rules for predicted customer utterances ("q"):
- Write them the way Sarvam STT would transcribe spoken Kannada: Kannada script, casual, short.
- Cover distinct likely directions (e.g. a detail question, an objection, an acceptance).

Rules for answers ("a") — must be indistinguishable from Kavya's real replies:
- Casual Bengaluru Kanglish in KANNADA SCRIPT (English words transliterated, e.g. ಆಫರ್, ಡಾಕ್ಯುಮೆಂಟ್ಸ್),
  matching the agent's style in the conversation below.""",
    "en": """Rules for predicted customer utterances ("q"):
- Short, casual spoken Indian English, the way a phone transcript reads.
- Cover distinct likely directions (e.g. a detail question, an objection, an acceptance).

Rules for answers ("a") — must be indistinguishable from Kavya's real replies:
- Natural, friendly Indian English, numbers written as words, no markdown,
  matching the agent's style in the conversation below.""",
}


def _dict_messages(context) -> list[dict]:
    # Universal LLMContext also holds LLMSpecificMessage objects (e.g. Gemini 3 thought
    # signatures) — only plain dict messages carry role/content.
    return [m for m in context.messages if isinstance(m, dict)]


class PredictiveCache(FrameProcessor):
    """Anticipates the customer's next question during their speaking time."""

    def __init__(
        self,
        context,                    # OpenAILLMContext — read-only, for trajectory
        api_key: str,
        campaign: dict | None = None,
        language: str = "kn",
        model: str = "gemini-3.1-flash-lite",
        n_predictions: int = 3,
        threshold: float = 0.80,
        enabled: bool = True,
    ):
        super().__init__()
        self._context = context
        self._campaign = campaign or {}
        self._style = STYLE.get(language, STYLE["kn"])
        self._model = model
        self._n = n_predictions
        self._threshold = threshold
        self._enabled = enabled
        self._client = None         # lazy: google-genai async client
        self._predict_task = None
        self._predictions: list[dict] = []  # [{"q","a","vec"}]

    def _genai(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
        return self._client

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if not self._enabled:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (InterruptionFrame,)):
            await self._cancel_predict()
            self._predictions = []
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Bot's turn just finished -> the customer is about to speak. Use their
            # speaking time to pre-generate likely answers.
            await self._cancel_predict()
            self._predict_task = self.create_task(self._predict())
        elif isinstance(frame, (OpenAILLMContextFrame, LLMContextFrame)):
            # A user turn is about to run the LLM. Try the cache first.
            answer = await self._match(frame)
            if answer is not None:
                self._predictions = []  # single-use
                await self.push_frame(LLMFullResponseStartFrame())
                await self.push_frame(LLMTextFrame(answer))
                await self.push_frame(LLMFullResponseEndFrame())
                return  # consume: the live LLM never sees this turn

        await self.push_frame(frame, direction)

    async def _cancel_predict(self):
        if self._predict_task:
            task, self._predict_task = self._predict_task, None
            await self.cancel_task(task)

    def _convo_tail(self, max_msgs: int = 8) -> list[dict]:
        msgs = [m for m in _dict_messages(self._context) if m.get("role") in ("user", "assistant")]
        return msgs[-max_msgs:]

    async def _predict(self):
        try:
            tail = self._convo_tail()
            if not tail:
                return
            convo = "\n".join(
                f"{'CUSTOMER' if m['role'] == 'user' else 'KAVYA'}: {m.get('content', '')}"
                for m in tail
                if isinstance(m.get("content"), str)
            )
            from google.genai import types

            prompt = PREDICT_PROMPT.format(
                n=self._n,
                style=self._style,
                campaign=json.dumps(self._campaign, ensure_ascii=False),
                convo=convo,
            )
            r = await self._genai().aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=0.6
                ),
            )
            preds = json.loads(r.text or "[]")
            preds = [p for p in preds if p.get("q") and p.get("a")][: self._n]
            if not preds:
                return
            emb = await self._genai().aio.models.embed_content(
                model=EMBED_MODEL,
                contents=[p["q"] for p in preds],
                config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
            )
            for p, e in zip(preds, emb.embeddings):
                p["vec"] = list(e.values)
            self._predictions = preds
            logger.info(f"PREDICT ready: {[p['q'][:40] for p in preds]}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"PREDICT generation failed (harmless, falls back to LLM): {e}")

    async def _match(self, frame) -> str | None:
        if not self._predictions:
            return None
        try:
            msgs = _dict_messages(frame.context)
            last = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
            query = last.get("content") if last else None
            if not isinstance(query, str) or not query.strip():
                return None
            from google.genai import types

            emb = await asyncio.wait_for(
                self._genai().aio.models.embed_content(
                    model=EMBED_MODEL,
                    contents=[query],
                    config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
                ),
                timeout=0.6,  # caps the miss penalty; a hit saves ~1.2s so this stays net-positive
            )
            qvec = list(emb.embeddings[0].values)
            best, best_p = 0.0, None
            for p in self._predictions:
                s = _cos(qvec, p["vec"])
                if s > best:
                    best, best_p = s, p
            hit = best >= self._threshold
            logger.info(
                f"PREDICT {'HIT' if hit else 'miss'} sim={best:.3f} "
                f"q='{query[:40]}' pred='{(best_p or {}).get('q', '')[:40]}'"
            )
            return best_p["a"] if hit else None
        except Exception as e:
            logger.warning(f"PREDICT match skipped ({e}); using live LLM")
            return None
