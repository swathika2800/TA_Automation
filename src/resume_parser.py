"""M4 - Resume parser.

Converts a PDF resume into a structured JSON document:
  {
    "raw_text": "...",
    "skills": [...],
    "experience": [{"company": ..., "title": ..., "start": ..., "end": ..., "summary": ...}],
    "education":  [...],
    "certifications": [...],
    "projects": [...],
    "total_experience_years": int | None
  }

Strategy: PyMuPDF (`fitz`) for primary text extraction; `pdfplumber` as a
fallback for table-heavy or image-based resumes.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .config_loader import get_logger

logger = get_logger(__name__)


_SECTION_HEADERS = {
    "skills": re.compile(r"^\s*(skills?|technical skills|core skills)\s*:?\s*$", re.I),
    "experience": re.compile(r"^\s*(work\s+experience|experience|employment|professional experience)\s*:?\s*$", re.I),
    "education": re.compile(r"^\s*(education|academic)\s*:?\s*$", re.I),
    "certifications": re.compile(r"^\s*(certifications?|licenses?)\s*:?\s*$", re.I),
    "projects": re.compile(r"^\s*(projects|key projects)\s*:?\s*$", re.I),
}


def _extract_text_pymupdf(path: Path) -> str:
    import fitz
    doc = fitz.open(path)
    chunks: list[str] = []
    for page in doc:
        chunks.append(page.get_text("text"))
    doc.close()
    return "\n".join(chunks)


def _extract_text_pdfplumber(path: Path) -> str:
    import pdfplumber
    chunks: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            chunks.append(page.extract_text() or "")
    return "\n".join(chunks)


def extract_text(pdf_path: Path) -> str:
    try:
        text = _extract_text_pymupdf(pdf_path)
        if text.strip():
            return text
    except Exception as exc:
        logger.debug("PyMuPDF failed for %s: %s", pdf_path.name, exc)

    try:
        return _extract_text_pdfplumber(pdf_path)
    except Exception as exc:
        logger.warning("pdfplumber also failed for %s: %s", pdf_path.name, exc)
        return ""


def _split_sections(text: str) -> dict[str, list[str]]:
    """Group lines into named sections by detecting common header patterns."""
    sections: dict[str, list[str]] = {
        "skills": [], "experience": [], "education": [],
        "certifications": [], "projects": [], "other": [],
    }
    current = "other"
    for line in text.splitlines():
        matched = None
        for name, pat in _SECTION_HEADERS.items():
            if pat.match(line):
                matched = name
                break
        if matched:
            current = matched
            continue
        sections[current].append(line)
    return sections


def _split_bullets_or_commas(block: str) -> list[str]:
    items: list[str] = []
    for line in block:
        for piece in re.split(r"[,;|]", line):
            piece = piece.strip(" \t-•*·")
            if piece:
                items.append(piece)
    return items


_YEARS_RE = re.compile(
    r"(\b(?:19|20)\d{2}\b)\s*[-–to]+\s*(\b(?:19|20)\d{2}\b|present|current|now)",
    re.I,
)


def _total_experience_years(text: str) -> int | None:
    matches = _YEARS_RE.findall(text)
    if not matches:
        return None
    years = 0
    for start, end in matches:
        try:
            s = int(start)
            e = 2024 if end.lower() in {"present", "current", "now"} else int(end)
            years += max(0, e - s)
        except ValueError:
            continue
    return years or None


def parse(pdf_path: Path) -> dict[str, Any]:
    text = extract_text(pdf_path)
    if not text.strip():
        return {"raw_text": "", "skills": [], "experience": [],
                "education": [], "certifications": [], "projects": [],
                "total_experience_years": None}

    sections = _split_sections(text)
    return {
        "raw_text": text,
        "skills": _split_bullets_or_commas(sections["skills"]),
        "experience": _split_bullets_or_commas(sections["experience"]),
        "education": _split_bullets_or_commas(sections["education"]),
        "certifications": _split_bullets_or_commas(sections["certifications"]),
        "projects": _split_bullets_or_commas(sections["projects"]),
        "total_experience_years": _total_experience_years(text),
    }
