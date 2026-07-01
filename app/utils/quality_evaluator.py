from __future__ import annotations

import re
from dataclasses import dataclass


# Expanded common words for PT and EN to improve quality detection
COMMON_WORDS = {
    # Portuguese
    "de", "do", "da", "dos", "das", "para", "com", "sem", "por",
    "que", "uma", "um", "os", "as", "no", "na", "nos", "nas",
    "em", "ao", "se", "ou", "mais", "mas", "como", "sua", "seu",
    "este", "esta", "esse", "essa", "isso", "isto",
    "foi", "ser", "tem", "nao", "sim", "entre",
    "sobre", "apos", "ate", "cada", "quando", "desde",
    "documento", "ocr", "pdf", "pagina", "arquivo", "texto",
    "nome", "data", "valor", "total", "numero",
    # English
    "the", "and", "for", "are", "but", "not", "you", "all",
    "can", "had", "her", "was", "one", "our", "out",
    "has", "his", "how", "its", "may", "new", "now", "old",
    "see", "way", "who", "did", "get", "let", "say",
    "she", "too", "use", "with", "this", "that", "from",
    "have", "been", "will", "each", "make", "like",
    "page", "file", "text", "name", "date", "total",
}


@dataclass
class QualityEvaluation:
    label: str
    valid_ratio: float
    character_count: int


def evaluate_quality(text: str, min_text: int, valid_ratio_threshold: float) -> QualityEvaluation:
    # Extract tokens of 2+ alphanumeric characters
    normalized = re.findall(r"[A-Za-z\u00C0-\u00FF0-9]{2,}", text.lower())
    character_count = len(text.strip())
    if character_count < min_text or not normalized:
        return QualityEvaluation(label="LOW", valid_ratio=0.0, character_count=character_count)

    valid_tokens = sum(
        1 for token in normalized
        if token in COMMON_WORDS or _is_valid_alphanumeric(token)
    )
    valid_ratio = valid_tokens / len(normalized)
    label = "HIGH" if valid_ratio >= valid_ratio_threshold else "LOW"
    return QualityEvaluation(label=label, valid_ratio=valid_ratio, character_count=character_count)


def _is_valid_alphanumeric(token: str) -> bool:
    """A token is valid if it is purely alphabetic or contains both
    letters and digits in a pattern typical of identifiers/codes.
    Pure digit-only tokens of 2+ chars are also accepted (e.g. years, amounts).
    Reject tokens that look like OCR garbage (excessive consonant clusters
    with no vowels)."""
    if token.isdigit():
        return True
    if token.isalpha():
        # Reject likely garbage: 4+ chars with no vowel
        if len(token) >= 4 and not re.search(r"[aeiouAEIOU\u00E0-\u00FC]", token):
            return False
        return True
    # Mixed alphanumeric: accept if it has at least one letter
    return bool(re.search(r"[A-Za-z\u00C0-\u00FF]", token))
