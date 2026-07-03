# RAG Design — Bangalore Kannada Tele-Caller

The retrieval notes you shared are correct, but they describe a **document-retrieval**
problem. A tele-caller has *two* different Kannada-understanding needs, and mixing them
into one vector DB is the classic mistake. Here's how each of the four techniques is
applied to the layer that actually needs it.

## The two layers

### Layer 1 — Dialect / slang understanding  →  `rag/glossary.py`
Small, closed vocabulary (Bengaluru slang + Kanglish). Implemented as a **glossary /
knowledge-graph lookup**, NOT a vector DB.
- **Knowledge Graph Integration** (their point #3): `bangalore_kannada_lexicon.json` is the
  graph — each term carries meaning, script, region (`bengaluru`), and a `speak_ok` flag.
  `DialectGlossary.lookup()` is an O(1) dict hit with zero latency and zero mis-retrieval.
- **Query Expansion** (their point #1): `expand_query()` detects slang in the customer's
  utterance and appends standard-language glosses (e.g. `chindi (mind-blowing)`) so the
  Layer-2 retriever gets better recall. This is where a BHASHINI translate/transliterate
  call can slot in if you want Kannada-script normalization (see "Optional: BHASHINI").
- **Why not embeddings here:** the set is bounded and exact; vector search would add
  latency and can *miss* rare slang. Dialect is a lookup problem, not a semantic-search one.

### Layer 2 — Business knowledge (products, EMI, policies, FAQs)  →  `rag/kb_retriever.py`
Large, changing, semantic, and multilingual (Kannada / English / Kanglish). THIS is what
their BGE-m3 + reranker advice is actually for.
- **Multilingual Embeddings** (their point #2): `BAAI/bge-m3` — strong on Indic scripts and
  code-mixed text — instead of English-only `text-embedding-3`. Preserves the semantics of
  native Kannada and Kanglish chunks in vector space.
- **Contextual Re-ranking** (their point #4): `BAAI/bge-reranker-v2-m3` cross-encoder
  re-scores the top-20 dense hits down to the top-3, so localized/code-mixed queries land
  on the right doc. Swap in `CohereRerank` if you prefer a hosted API.

## Runtime flow (per customer turn)
```
customer utterance
   │
   ├─ Layer 1: glossary.expand_query()        # slang → standard synonyms
   │
   ├─ Layer 2: bge-m3 dense retrieve (top 20)
   │           → bge-reranker rerank (top 3)
   │
   └─ inject the 3 business snippets into Gemini's context for THIS turn only
                → Gemini answers in Bengaluru Kanglish, grounded, no hallucination
```

## Wiring into the call (bot.py)
Layer 2 is optional and heavy, so the base pipeline runs without it. To enable grounded
answers, add a processor before the LLM that, on each user turn, calls
`KBRetriever.retrieve(utterance)` and prepends the snippets as a system message like:
`"Use ONLY these facts to answer; if not here, say you'll send details on WhatsApp:\n<snippets>"`.
In Pipecat this is a small custom `FrameProcessor` placed between `aggregator.user()` and
`llm`, or a function-call tool the LLM invokes. Keep `retrieve()` fast (pre-built index in
memory) so it doesn't blow the turn latency budget (<~300ms ideal).

## Optional: BHASHINI
For heavier Kannada-script normalization or Kannada⇄English translation of queries, call
the Govt. of India **BHASHINI** ULCA APIs inside `expand_query()` (transliterate Kanglish →
Kannada script, or translate for synonym expansion). Only add it if you see recall misses —
for a bounded glossary the local lookup is usually enough and far faster.

## Install (Layer 2 only — heavy)
```bash
pip install FlagEmbedding faiss-cpu numpy
python -m rag.kb_retriever   # builds index from knowledge_base/*.md and runs a test query
```

## Rule that stays sacred
Retrieval **grounds** the agent; it never loosens the guardrails. If a fact isn't in the KB,
the agent does not invent it — it offers to send details on WhatsApp. (Enforced in the
system prompt.)
