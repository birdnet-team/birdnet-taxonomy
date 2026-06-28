"""Shared helpers for description quality checks."""

from __future__ import annotations

import re


def word_count(text: str) -> int:
    """Count prose units for quality checks, including CJK text without spaces."""
    text = text or ""
    words = re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE)
    cjk_chars = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text)
    return len(words) + len(cjk_chars)


def normalize_space(text: str) -> str:
    """Collapse whitespace in text."""
    return re.sub(r"\s+", " ", (text or "").strip())


def contains_species_identity(text: str, names: list[str] | tuple[str, ...] | set[str]) -> bool:
    """Return True when text contains one of the accepted scientific names."""
    haystack = normalize_space(text).lower()
    if not haystack:
        return False
    for name in names:
        clean = normalize_space(str(name)).lower()
        if clean and re.search(rf"\b{re.escape(clean)}\b", haystack):
            return True
    return False


def select_description_text(full_text: str, min_words: int, target_words: int,
                            extra_sections: int = 2,
                            min_paragraphs: int = 1) -> str:
    """Select a concise early-article description from a full Wikipedia extract."""
    text = (full_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    chunks = [
        normalize_space(chunk)
        for chunk in re.split(r"\n{2,}", text)
        if normalize_space(chunk)
    ]
    if not chunks:
        chunks = [normalize_space(text)]

    selected: list[str] = []
    max_chunks = max(1, 1 + int(extra_sections or 0))
    min_chunks = max(1, int(min_paragraphs or 1))
    for chunk in chunks[:max_chunks]:
        if re.match(r"^(references|external links|further reading|see also)\b", chunk, re.I):
            break
        selected.append(chunk)
        if (
            len(selected) >= min_chunks
            and word_count(" ".join(selected)) >= max(min_words, target_words)
        ):
            break

    candidate = "\n\n".join(selected).strip()
    if not candidate:
        return ""
    if word_count(candidate) <= max(target_words * 2, min_words):
        return candidate

    sentences = re.split(r"(?<=[.!?])\s+", candidate)
    out: list[str] = []
    for sentence in sentences:
        if not sentence:
            continue
        out.append(sentence)
        if word_count(" ".join(out)) >= target_words and len(selected) <= min_chunks:
            break
    shortened = " ".join(out).strip()
    if len(selected) >= min_chunks:
        return candidate
    return shortened or candidate
