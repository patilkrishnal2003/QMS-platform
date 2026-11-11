"""Writing-standard check (AI-13): Good Documentation Practice - factual, objective, no speculation or blame.
A deterministic lexicon check, NOT a language model. It flags; a person decides. Stdlib only."""
import re

LEXICON = {
    "speculation": ["probably", "possibly", "perhaps", "presumably", "likely", "unlikely", "might have", "may have been",
                    "could have been", "seems", "seemed", "appears to have", "appeared to have", "i think", "we think",
                    "i believe", "we believe", "assume", "assumed", "guess"],
    "blame": ["operator error", "human error", "careless", "carelessly", "negligent", "negligence", "incompetent",
              "sloppy", "lazy", "blame", "fault of", "irresponsible", "deliberately ignored", "ignored the", "should have known"],
    "subjective": ["unfortunately", "obviously", "clearly", "very", "extremely", "terrible", "serious failure",
                   "unacceptable", "shocking", "disaster"],
}
_PATTERNS = [(kind, term, re.compile(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", re.I))
             for kind, terms in LEXICON.items() for term in terms]


def check(text: str) -> list[dict]:
    """Return [{type, term}] for every flagged term found (each term once)."""
    out = []
    for kind, term, pat in _PATTERNS:
        if pat.search(text or ""):
            out.append({"type": kind, "term": term})
    return out
