"""BM25 retrieval over Markdown runbooks, returning verbatim section excerpts.

Each `## ` section is one retrievable chunk. The returned `quote` is always an
exact, consecutive run of lines from the source file, so citations can be
verified with a plain substring check.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from src.models import RunbookCitation

K1 = 1.5
B = 0.75
QUOTE_MAX_CHARS = 500

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN = re.compile(r"[a-z0-9]+")
_SECTION_HEADING = re.compile(r"^##\s+(.+?)\s*$")

STOPWORDS = frozenset(
    {
        "a", "al", "con", "de", "del", "el", "en", "es", "la", "las", "lo", "los", "o",
        "para", "por", "que", "se", "su", "sus", "un", "una", "y", "the", "and", "of",
        "to", "in", "is", "on", "or", "for", "by", "as", "at", "be",
    }
)


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, splitting CamelCase and dropping stopwords."""
    spaced = _CAMEL_BOUNDARY.sub(" ", text)
    return [t for t in _TOKEN.findall(spaced.lower()) if len(t) > 1 and t not in STOPWORDS]


def _excerpt(body_lines: list[str]) -> str:
    """First paragraph of a section, capped in length, taken as consecutive file lines."""
    collected: list[str] = []
    total = 0
    for line in body_lines:
        if not line.strip():
            if collected:
                break
            continue
        if collected and total + len(line) > QUOTE_MAX_CHARS:
            break
        collected.append(line)
        total += len(line) + 1
    return "\n".join(collected)


@dataclass(frozen=True)
class _Chunk:
    file: str
    section: str
    quote: str
    term_freq: Counter[str]
    length: int


def _split_sections(path: Path, display_name: str) -> list[_Chunk]:
    lines = path.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if _SECTION_HEADING.match(line)]
    chunks: list[_Chunk] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        match = _SECTION_HEADING.match(lines[start])
        if match is None:  # pragma: no cover - guarded by the list comprehension above
            continue
        title = match.group(1)
        body = lines[start + 1 : end]
        quote = _excerpt(body)
        if not quote:
            continue
        tokens = tokenize(f"{title} {' '.join(body)}")
        chunks.append(
            _Chunk(
                file=display_name,
                section=title,
                quote=quote,
                term_freq=Counter(tokens),
                length=len(tokens),
            )
        )
    return chunks


class RunbookIndex:
    """In-memory BM25 index over runbook sections."""

    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks
        self._document_frequency: Counter[str] = Counter()
        for chunk in chunks:
            self._document_frequency.update(chunk.term_freq.keys())
        self._size = len(chunks)
        total_length = sum(chunk.length for chunk in chunks)
        self._avg_length = total_length / self._size if self._size else 1.0

    @classmethod
    def from_directory(cls, directory: Path) -> "RunbookIndex":
        chunks: list[_Chunk] = []
        for path in sorted(directory.glob("*.md")):
            chunks.extend(_split_sections(path, f"{directory.name}/{path.name}"))
        return cls(chunks)

    def __len__(self) -> int:
        return self._size

    def search(self, query: str, top_k: int = 3) -> list[RunbookCitation]:
        """Return up to `top_k` sections with positive BM25 score, best first."""
        terms = set(tokenize(query))
        scored = [
            (score, chunk)
            for chunk in self._chunks
            if (score := self._score(terms, chunk)) > 0
        ]
        scored.sort(key=lambda item: (-item[0], item[1].file, item[1].section))
        return [
            RunbookCitation(
                file=chunk.file,
                section=chunk.section,
                quote=chunk.quote,
                score=round(score, 4),
            )
            for score, chunk in scored[:top_k]
        ]

    def _score(self, terms: set[str], chunk: _Chunk) -> float:
        norm = K1 * (1 - B + B * chunk.length / self._avg_length)
        total = 0.0
        for term in terms:
            tf = chunk.term_freq.get(term, 0)
            if tf == 0:
                continue
            df = self._document_frequency[term]
            idf = math.log((self._size - df + 0.5) / (df + 0.5) + 1.0)
            total += idf * tf * (K1 + 1) / (tf + norm)
        return total


def build_query(terms: list[str]) -> str:
    """Join free-text fragments (component names, signal names, messages) into one query."""
    return " ".join(terms)
