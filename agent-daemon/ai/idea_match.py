"""Match a proposed tool against ideas the user already declined.

The user asked (2026-11-09 live checks) that a declined tool idea is never
asked again, even reworded: "send_imessage" denied should also stop
"send_text_message" or "reply to the iMessage sender".

Word overlap (``IdeaFeedback.resembles_rejected``) misses rewordings, and
embeddings alone can't tell *reading* messages from *sending* them (both
score ~0.6 against a declined "send an iMessage"). So, per proposal:

1. Same name or word overlap  → declined (``IdeaFeedback``).
2. Embedding cosine ≥ ``high``  → declined, no model call.
3. ``low`` ≤ cosine < ``high``  → one judge call: "is this the same tool?"
4. Below ``low``                → not declined.

Embeddings come from model2vec (``minishlab/potion-base-8M``, ~30 MB, local,
numpy only), loaded on first use. Without it, only step 1 runs.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "minishlab/potion-base-8M"
HIGH = 0.80
LOW = 0.55
JUDGE_MAX_TOKENS = 600

SAME_IDEA_SYSTEM_PROMPT = (
    "A user declined a proposed helper tool. Decide whether a NEW proposal "
    "is essentially the same tool: the same action on the same kind of "
    "thing, even if worded differently (e.g. 'send a text message' is the "
    "same as 'send an iMessage'; 'read my messages' is NOT the same as "
    "'send a message'). Reply with exactly one JSON object and nothing "
    'else: {"same": true} or {"same": false}'
)

Embed = Callable[[Sequence[str]], Any]
Judge = Callable[[str], str]


def idea_text(name: str, description: str) -> str:
    return f"{name.replace('_', ' ')}: {description}".strip()


def _parse_same(raw: str) -> Optional[bool]:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if match:
        try:
            value = json.loads(match.group(0)).get("same")
            if isinstance(value, bool):
                return value
        except (ValueError, AttributeError):
            pass
    low = (raw or "").lower()
    if '"same": true' in low or "same: true" in low:
        return True
    if '"same": false' in low or "same: false" in low:
        return False
    return None


class DeclinedIdeaMatcher:
    """Finds the declined idea a proposal repeats, if any."""

    def __init__(
        self,
        feedback: Any,
        *,
        judge: Optional[Judge] = None,
        embed: Optional[Embed] = None,
        high: float = HIGH,
        low: float = LOW,
        model_name: str = DEFAULT_MODEL,
    ) -> None:
        self.feedback = feedback
        self.judge = judge
        self._embed = embed
        self.high = high
        self.low = low
        self.model_name = model_name
        self._lock = threading.Lock()
        self._load_failed = False
        self._cache: dict[str, Any] = {}

    # ---- embeddings ---------------------------------------------------

    def _embedder(self) -> Optional[Embed]:
        if self._embed is not None or self._load_failed:
            return self._embed
        with self._lock:
            if self._embed is None and not self._load_failed:
                try:
                    from model2vec import StaticModel  # type: ignore

                    model = StaticModel.from_pretrained(self.model_name)
                    self._embed = model.encode
                    logger.info("Declined-idea matching uses %s", self.model_name)
                except Exception as exc:
                    self._load_failed = True
                    logger.warning(
                        "Declined-idea matching without embeddings (%s); "
                        "only exact names and word overlap are caught", exc,
                    )
        return self._embed

    def _vectors(self, texts: list[str]) -> Optional[list[Any]]:
        embed = self._embedder()
        if embed is None:
            return None
        missing = [t for t in texts if t not in self._cache]
        if missing:
            import numpy as np

            for text, vec in zip(missing, embed(missing)):
                arr = np.asarray(vec, dtype=float)
                norm = float(np.linalg.norm(arr)) or 1.0
                self._cache[text] = arr / norm
        return [self._cache[t] for t in texts]

    def forget(self, entries: Sequence[dict[str, Any]]) -> int:
        """Drop cached vectors of removed (un-declined) ideas (#56).

        Returns how many cache entries were dropped.
        """
        dropped = 0
        with self._lock:
            for entry in entries:
                text = idea_text(str(entry.get("tool_name") or ""),
                                 str(entry.get("description") or ""))
                if self._cache.pop(text, None) is not None:
                    dropped += 1
        return dropped

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ---- matching -----------------------------------------------------

    def _declined(self) -> list[dict[str, Any]]:
        return [e for e in getattr(self.feedback, "entries", []) if not e.get("approved")]

    def match(self, name: str, description: str) -> str:
        """Name of the declined idea this proposal repeats, else ``""``."""
        if not name:
            return ""
        try:
            if self.feedback.resembles_rejected(name, description):
                return name
        except Exception:
            logger.exception("resembles_rejected failed")
        declined = self._declined()
        if not declined:
            return ""
        proposal = idea_text(name, description)
        texts = [idea_text(e.get("tool_name", ""), e.get("description", "")) for e in declined]
        try:
            vectors = self._vectors([proposal, *texts])
        except Exception:
            logger.exception("embedding declined ideas failed")
            return ""
        if vectors is None:
            return ""
        query, rest = vectors[0], vectors[1:]
        scored = sorted(
            ((float(query @ vec), entry) for vec, entry in zip(rest, declined)),
            key=lambda pair: pair[0], reverse=True,
        )
        best_score, best = scored[0]
        if best_score >= self.high:
            logger.info("Proposal %r repeats declined %r (similarity %.2f)",
                        name, best.get("tool_name"), best_score)
            return str(best.get("tool_name") or name)
        if best_score < self.low or self.judge is None:
            return ""
        # Borderline: ask once about the closest few declined ideas.
        candidates = [e for s, e in scored[:3] if s >= self.low]
        listing = "\n".join(
            f"- {e.get('tool_name')}: {e.get('description')}" for e in candidates
        )
        prompt = (
            f"Declined tools:\n{listing}\n\n"
            f"NEW proposal: {name}: {description}\n\n"
            "Is the NEW proposal essentially the same tool as any declined one?"
        )
        try:
            same = _parse_same(self.judge(prompt))
        except Exception:
            logger.exception("same-idea judge failed")
            return ""
        logger.info("Proposal %r vs declined %r (similarity %.2f): judge says same=%s",
                    name, best.get("tool_name"), best_score, same)
        return str(best.get("tool_name") or name) if same else ""


def make_sync_judge(engine: Any, max_tokens: int = JUDGE_MAX_TOKENS) -> Judge:
    """A blocking judge over ``engine.generate_chat_sync`` (tool calls run in
    a worker thread, so blocking is fine there)."""

    def judge(prompt: str) -> str:
        raw = engine.generate_chat_sync(SAME_IDEA_SYSTEM_PROMPT, prompt, max_tokens)
        return re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL).strip()

    return judge
