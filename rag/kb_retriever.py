"""
Layer 2 — Business Knowledge Retriever (multilingual embeddings + reranker).

Implements the other two techniques from the RAG notes, applied to the layer that
actually needs a vector store — the *business* knowledge base (products, pricing,
EMI/foreclosure terms, policies, FAQs), which can be written in Kannada, English, or
Kanglish:
  - "Multilingual embeddings": BAAI/bge-m3 (strong on Indic + code-mixed text) instead
    of English-only models like text-embedding-3.
  - "Contextual re-ranking": BAAI/bge-reranker-v2-m3 cross-encoder re-scores the top
    candidates so localized/code-mixed queries land on the right doc. (Swap for
    CohereRerank if you prefer a hosted reranker.)

Query flow at call time:
    customer utterance
      -> DialectGlossary.expand_query()   # slang -> standard synonyms (recall boost)
      -> bge-m3 dense retrieve (top 20)
      -> bge-reranker rerank (top 3)
      -> inject the 3 snippets into Gemini's context for this turn

Install (heavy — do it in the venv): pip install FlagEmbedding faiss-cpu
This is a scaffold: it runs standalone to build+query an index. Wire retrieve() into
bot.py before the LLM turn (see RAG_DESIGN.md "Wiring").
"""

from pathlib import Path

try:
    from FlagEmbedding import BGEM3FlagModel, FlagReranker
except ImportError:  # keep the base pipeline importable without the heavy deps
    BGEM3FlagModel = None
    FlagReranker = None

from rag.glossary import DialectGlossary

KB_DIR = Path(__file__).parent.parent / "knowledge_base"


class KBRetriever:
    def __init__(self, kb_dir: Path = KB_DIR, use_reranker: bool = True):
        if BGEM3FlagModel is None:
            raise ImportError("pip install FlagEmbedding faiss-cpu to use KBRetriever")
        self.embedder = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True)
        self.reranker = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=True) if use_reranker else None
        self.glossary = DialectGlossary()
        self.chunks: list[str] = []
        self._embeddings = None
        self._load(kb_dir)

    def _load(self, kb_dir: Path):
        # One chunk per non-empty paragraph across all KB markdown files.
        for md in sorted(kb_dir.glob("*.md")):
            for para in md.read_text(encoding="utf-8").split("\n\n"):
                para = para.strip()
                if para and not para.startswith("#"):
                    self.chunks.append(para)
        if not self.chunks:
            return
        self._embeddings = self.embedder.encode(self.chunks, batch_size=12)["dense_vecs"]

    def retrieve(self, utterance: str, top_k: int = 3, pool: int = 20) -> list[str]:
        if not self.chunks:
            return []
        query = self.glossary.expand_query(utterance)  # Layer-1 slang expansion
        q_vec = self.embedder.encode([query])["dense_vecs"][0]

        # dense cosine scores
        import numpy as np
        scores = (self._embeddings @ q_vec)
        idx = np.argsort(scores)[::-1][:pool]
        candidates = [self.chunks[i] for i in idx]

        if self.reranker:
            pairs = [[query, c] for c in candidates]
            rr = self.reranker.compute_score(pairs, normalize=True)
            order = sorted(range(len(candidates)), key=lambda i: rr[i], reverse=True)
            candidates = [candidates[i] for i in order]

        return candidates[:top_k]


if __name__ == "__main__":
    r = KBRetriever()
    for snip in r.retrieve("EMI yeshtu, foreclosure charge idya?"):
        print("•", snip[:120])
