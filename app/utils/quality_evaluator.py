from __future__ import annotations

import re
from dataclasses import dataclass


COMMON_WORDS = {
    "de",
    "do",
    "da",
    "para",
    "com",
    "sem",
    "documento",
    "ocr",
    "pdf",
    "the",
    "and",
    "for",
}


@dataclass
class QualityEvaluation:
    label: str
    valid_ratio: float
    character_count: int


def evaluate_quality(text: str, min_text: int, valid_ratio_threshold: float) -> QualityEvaluation:
    normalized = re.findall(r"[A-Za-zÀ-ÿ0-9]{2,}", text.lower())
    character_count = len(text.strip())
    if character_count < min_text or not normalized:
        return QualityEvaluation(label="LOW", valid_ratio=0.0, character_count=character_count)

    valid_tokens = sum(1 for token in normalized if token in COMMON_WORDS or token.isalpha())
    valid_ratio = valid_tokens / len(normalized)
    label = "HIGH" if valid_ratio >= valid_ratio_threshold else "LOW"
    return QualityEvaluation(label=label, valid_ratio=valid_ratio, character_count=character_count)
