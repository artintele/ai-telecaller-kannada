"""
Layer 1 — Dialect Knowledge Graph + Query Expansion (NO vector DB).

Implements two of the four techniques from the RAG notes, applied to the layer that
actually needs them for *dialect*:
  - "Knowledge Graph / glossary lookup": a slang -> canonical-meaning map built from
    bangalore_kannada_lexicon.json (regionally scoped to Bengaluru).
  - "Cross-lingual query expansion": when a customer utterance contains local slang,
    expand it with standard meanings BEFORE it hits the business-KB retriever, so a
    term like "chindi" also matches standard synonyms ("excellent", "great").

Why not embeddings here: the term set is small and closed. A dict lookup is O(1),
zero-latency, and never mis-retrieves. Reserve embeddings for the open-ended business KB
(see kb_retriever.py).
"""

import json
import re
from pathlib import Path

LEXICON_FILE = Path(__file__).parent.parent / "bangalore_kannada_lexicon.json"


class DialectGlossary:
    def __init__(self, lexicon_path: Path = LEXICON_FILE):
        data = json.loads(lexicon_path.read_text(encoding="utf-8"))
        # term (lowercased latin) -> {meaning, script, speak, region}
        self.terms: dict[str, dict] = {}
        for category, items in data["categories"].items():
            for it in items:
                key = it.get("latin") or it.get("phrase")
                if not key:
                    continue
                self.terms[key.lower()] = {
                    "meaning": it["meaning"],
                    "script": it.get("script", ""),
                    "category": category,
                    "speak_ok": it.get("speak", True),
                    "region": "bengaluru",
                }

    def lookup(self, term: str) -> dict | None:
        return self.terms.get(term.lower().strip())

    def expand_query(self, utterance: str) -> str:
        """Append standard-language glosses for any slang found, for better KB recall."""
        found = []
        low = utterance.lower()
        for term, info in self.terms.items():
            # word-ish boundary match, tolerant of the loose romanization callers use
            if re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", low):
                found.append(f"{term} ({info['meaning']})")
        if not found:
            return utterance
        return f"{utterance}\n[dialect expansion: {'; '.join(found)}]"

    def system_context_block(self) -> str:
        """Compact glossary string to inline into the LLM system prompt if desired."""
        lines = ["Bengaluru dialect glossary (understand these; only speak ones marked OK):"]
        for term, info in sorted(self.terms.items()):
            flag = "OK" if info["speak_ok"] else "NO-SPEAK"
            lines.append(f"  {term} = {info['meaning']} [{flag}]")
        return "\n".join(lines)


if __name__ == "__main__":
    g = DialectGlossary()
    print(g.expand_query("swalpa kadime maadi, offer chindi ide"))
    print("---")
    print(g.lookup("guru"))
