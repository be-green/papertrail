"""Similarity heuristics for the tag vocabulary guardrail.

Used by the `find_similar_tags` tool and by `add_tags` to warn when a
candidate name looks like a near-duplicate of something already in the
vocabulary.
"""
from __future__ import annotations

# Generic kebab-case tokens that appear in many unrelated tag names and
# therefore shouldn't count as evidence of similarity on their own. Sharing
# "methods" between "bayesian-methods" and "computational-methods" is not
# informative; sharing "bayesian" or "computational" is.
_STOPWORD_TOKENS = frozenset(
    {
        "methods",
        "method",
        "analysis",
        "theory",
        "models",
        "model",
        "modeling",
        "modelling",
        "estimation",
        "framework",
        "approach",
        "approaches",
        "data",
        "paper",
        "papers",
    }
)


def _tokens(name: str) -> set[str]:
    return {
        tok
        for tok in name.lower().split("-")
        if tok and tok not in _STOPWORD_TOKENS
    }


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            add = previous[j] + 1
            remove = current[-1] + 1
            substitute = previous[j - 1] + (ca != cb)
            current.append(min(add, remove, substitute))
        previous = current
    return previous[-1]


def similar_tags(
    candidate: str,
    vocabulary: list[str],
    *,
    max_edit_distance: int = 3,
) -> list[str]:
    """Return existing tag names that look similar to `candidate`.

    A vocabulary entry is flagged when it shares at least one non-stopword
    kebab-case token with the candidate, or when its Levenshtein distance to
    the candidate is within `max_edit_distance`. Token overlap is treated as a
    stronger signal than edit distance and sorts earlier in the result.
    Exact matches to the candidate are excluded.
    """
    candidate_tokens = _tokens(candidate)
    ranked: list[tuple[int, int, str]] = []
    for existing in vocabulary:
        if existing == candidate:
            continue
        existing_tokens = _tokens(existing)
        shared = candidate_tokens & existing_tokens
        distance = _levenshtein(candidate, existing)
        if shared:
            ranked.append((0, -len(shared), existing))
        elif distance <= max_edit_distance:
            ranked.append((1, distance, existing))
    ranked.sort()
    return [name for _, _, name in ranked]
