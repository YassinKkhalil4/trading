from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


NEWS_CLASSIFIER_VERSION = "news_classifier_v1"
RUMOR_TERMS = {"rumor", "reportedly", "unconfirmed", "speculation", "sources say"}
HIGH_CONFIDENCE_SOURCES = {"sec.gov", "investor relations", "company press release"}


@dataclass(frozen=True)
class NewsClassification:
    normalized_headline_hash: str
    source_confidence_score: float
    duplicate_headline: bool
    rumor_flag: bool
    reason: str
    classifier_version: str = NEWS_CLASSIFIER_VERSION


def normalize_headline(headline: str) -> str:
    return re.sub(r"\s+", " ", headline.strip().lower())


def classify_news_headline(
    *,
    headline: str,
    source: str,
    seen_hashes: set[str] | None = None,
) -> NewsClassification:
    normalized = normalize_headline(headline)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    duplicate = digest in (seen_hashes or set())
    rumor = any(term in normalized for term in RUMOR_TERMS)
    lower_source = source.strip().lower()
    source_confidence = 90.0 if any(item in lower_source for item in HIGH_CONFIDENCE_SOURCES) else 60.0
    if rumor:
        source_confidence = min(source_confidence, 35.0)
    reason = "Duplicate headline detected." if duplicate else "Headline classified."
    if rumor:
        reason += " Rumor flag set."
    return NewsClassification(
        normalized_headline_hash=digest,
        source_confidence_score=source_confidence,
        duplicate_headline=duplicate,
        rumor_flag=rumor,
        reason=reason,
    )

