"""M2 - Criteria builder.

Parses a JD + its `screening_guidelines/<JD_ID>_*.md` file and produces:
  * a Naukri boolean search query
  * a structured HardFilter object used by the bot to reject candidates
    BEFORE downloading their resume.

Per the strict data-minimization policy, rejected candidates leave no trace:
no log, no PDF, no profile JSON. The HardFilter is therefore in-memory only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config_loader import Settings, get_logger
from .jd_loader import JobDescription

logger = get_logger(__name__)


@dataclass
class HardFilter:
    """In-memory rules; rejected candidates are never persisted."""
    mandatory_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    experience_min: int = 0
    experience_max: int = 99
    locations: list[str] = field(default_factory=list)
    max_notice_period_days: int | None = None
    ctc_ceiling_lacs: float | None = None
    freshness_days: int = 45
    min_ai_score: int = 60

    def rejects(self, profile: dict[str, Any]) -> tuple[bool, str]:
        """Return (rejected, reason). reason is empty when accepted.

        `profile` is a dict with optional keys: skills (list[str]),
        experience_years (int|None), location (str|None),
        notice_period_days (int|None), current_ctc_lacs (float|None),
        last_active_days (int|None).
        """
        skills_lower = {s.lower() for s in profile.get("skills", []) or []}

        missing = [
            m for m in self.mandatory_skills
            if m.lower() not in skills_lower
            and not any(m.lower() in s for s in skills_lower)
        ]
        if missing:
            return True, "missing_mandatory_skill"

        exp = profile.get("experience_years")
        if exp is not None:
            if exp < self.experience_min:
                return True, "experience_below_min"
            if exp > self.experience_max:
                return True, "experience_above_max"

        if self.locations:
            loc = (profile.get("location") or "").lower()
            if loc and not any(
                allowed.lower() in loc or loc in allowed.lower()
                for allowed in self.locations
            ):
                return True, "location_mismatch"

        if self.max_notice_period_days is not None:
            np_days = profile.get("notice_period_days")
            if np_days is not None and np_days > self.max_notice_period_days:
                return True, "notice_period_too_long"

        if self.ctc_ceiling_lacs is not None:
            ctc = profile.get("current_ctc_lacs")
            if ctc is not None and ctc > self.ctc_ceiling_lacs:
                return True, "ctc_above_ceiling"

        freshness = profile.get("last_active_days")
        if freshness is not None and freshness > self.freshness_days:
            return True, "stale_profile"

        return False, ""


def build_boolean_query(jd: JobDescription) -> str:
    """Return a Naukri-style AND-joined query like:  "Python" AND "FastAPI" AND "AWS"."""
    if not jd.skills:
        return ""
    quoted = [f'"{s}"' for s in jd.skills]
    return " AND ".join(quoted)


_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*-\s+(.+?)\s*$", re.MULTILINE)
_KV_RE = re.compile(
    r"^\s*(?:-\s*)?([a-zA-Z_]+)\s*:\s*(.+?)\s*$", re.MULTILINE
)


def _section_block(text: str, name: str) -> str:
    m = re.search(
        rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s+|\Z)",
        text, re.MULTILINE | re.DOTALL,
    )
    return m.group(1) if m else ""


def _parse_bullets(block: str) -> list[str]:
    return [m.group(1).strip() for m in _BULLET_RE.finditer(block)]


def _parse_kv(block: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2).strip() for m in _KV_RE.finditer(block)}


def load_guidelines(settings: Settings, jd: JobDescription) -> HardFilter:
    """Read `screening_guidelines/<JD_ID>_*.md` and return HardFilter.

    Falls back to JD-only defaults (skills become mandatory, experience/location
    from JD, AI min_score from settings.json) when the file is absent.
    """
    pattern = f"{jd.jd_id}_*.md"
    matches = sorted(settings.guidelines_dir.glob(pattern))
    if not matches:
        logger.warning(
            "No guidelines file for %s; using JD defaults", jd.jd_id,
        )
        return _defaults_from_jd(settings, jd)

    path = matches[0]
    text = path.read_text(encoding="utf-8")

    mandatory = _parse_bullets(_section_block(text, "Mandatory Skills"))
    preferred = _parse_bullets(_section_block(text, "Preferred Skills"))

    exp_kv = _parse_kv(_section_block(text, "Experience"))
    exp_min = int(exp_kv.get("min_years", jd.experience_min))
    exp_max = int(exp_kv.get("max_years", jd.experience_max))

    locations = _parse_bullets(_section_block(text, "Location"))
    if not locations:
        locations = list(jd.locations)

    rejection_text = _section_block(text, "Rejection Criteria")
    max_np: int | None = None
    for line in rejection_text.splitlines():
        m = re.search(r"notice_period_days\s*>\s*(\d+)", line)
        if m:
            max_np = int(m.group(1))
            break

    ai_kv = _parse_kv(_section_block(text, "AI Screening"))
    default_min_score = settings.raw.get("filters", {}).get("default_min_score", 60)
    min_score = int(ai_kv.get("min_score", default_min_score))

    filter_kv = _parse_kv(_section_block(text, "Filters"))
    ctc_ceiling = _safe_float(filter_kv.get("ctc_ceiling_lacs")) or jd.ctc_ceiling_lacs
    freshness = _safe_int(filter_kv.get("freshness_days")) or jd.freshness_days
    if freshness is None:
        freshness = settings.raw.get("filters", {}).get("freshness_days", 45)

    if not mandatory:
        mandatory = list(jd.skills)

    logger.info(
        "Guidelines for %s: mandatory=%s preferred=%s exp=[%d-%d] locs=%s np<=%s ctc<=%s fresh<=%dd score>=%d",
        jd.jd_id, mandatory, preferred, exp_min, exp_max, locations, max_np, ctc_ceiling, freshness, min_score,
    )

    return HardFilter(
        mandatory_skills=mandatory,
        preferred_skills=preferred,
        experience_min=exp_min,
        experience_max=exp_max,
        locations=locations,
        max_notice_period_days=max_np,
        ctc_ceiling_lacs=ctc_ceiling,
        freshness_days=int(freshness),
        min_ai_score=min_score,
    )


def _defaults_from_jd(settings: Settings, jd: JobDescription) -> HardFilter:
    default_min_score = settings.raw.get("filters", {}).get("default_min_score", 60)
    default_freshness = settings.raw.get("filters", {}).get("freshness_days", 45)
    return HardFilter(
        mandatory_skills=list(jd.skills),
        experience_min=jd.experience_min,
        experience_max=jd.experience_max,
        locations=list(jd.locations),
        ctc_ceiling_lacs=jd.ctc_ceiling_lacs,
        freshness_days=int(jd.freshness_days or default_freshness),
        min_ai_score=int(default_min_score),
    )


def _safe_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Boolean query generation (Claude-powered with static fallback)
# ---------------------------------------------------------------------------

_BOOLEAN_SYSTEM = """You generate Naukri Resdex boolean search queries.
Return ONLY the query string, no prose, no markdown fences."""


def _build_boolean_prompt(jd_dict: dict[str, Any], hint: str | None) -> str:
    hint_block = f"\nTA refinement hint: {hint}\n" if hint else ""
    skills_list = ", ".join(jd_dict.get("skills", []))
    return (
        "Generate a single Naukri Resdex boolean query for this JD.\n\n"
        f"Role: {jd_dict.get('role')}\n"
        f"Skills: {skills_list}\n"
        f"Experience: {jd_dict.get('experience_min')}-{jd_dict.get('experience_max')} years\n"
        f"Locations: {', '.join(jd_dict.get('locations', []))}\n"
        f"{hint_block}\n"
        "Rules:\n"
        "- Use AND for mandatory skills, OR for alternatives\n"
        "- Quote multi-word terms with double quotes\n"
        "- Keep it under 200 characters\n"
        "- Do NOT include experience or location (those are applied as Resdex filters)\n"
        "- Return ONLY the boolean query string\n"
    )


def generate_boolean_query(
    settings: Settings,
    jd_dict: dict[str, Any],
    hint: str | None = None,
    attempt: int = 1,
) -> str:
    """Generate a Naukri boolean query.

    Uses Claude via the LiteLLM proxy. The TA can pass a refinement `hint`
    and re-run (max 2 regenerations per spec).

    Falls back to the static `build_boolean_query` output on any error.
    """
    max_attempts = settings.raw.get("scout", {}).get("max_boolean_regenerations", 2)
    if attempt > max_attempts:
        raise ValueError(
            f"Max boolean regenerations ({max_attempts}) exceeded"
        )

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=settings.litellm_api_key,
            base_url=settings.litellm_base_url,
        )
        cfg = settings.raw.get("ai", {})
        resp = client.chat.completions.create(
            model=settings.litellm_model,
            messages=[
                {"role": "system", "content": _BOOLEAN_SYSTEM},
                {"role": "user",   "content": _build_boolean_prompt(jd_dict, hint)},
            ],
            max_tokens=int(cfg.get("max_tokens", 200)),
            temperature=float(cfg.get("temperature", 0.0)),
            timeout=int(cfg.get("request_timeout_sec", 30)),
        )
        text = (resp.choices[0].message.content or "").strip()
        text = text.strip('"').strip("'").strip()
        if text:
            return text
        raise RuntimeError("empty response from Claude")
    except Exception as exc:
        logger.warning(
            "Claude boolean generation failed (attempt %d/%d): %s; using static fallback",
            attempt, max_attempts, exc,
        )
        return build_boolean_query_dict(jd_dict)


def build_boolean_query_dict(jd_dict: dict[str, Any]) -> str:
    """Static fallback: AND-join all skills."""
    skills = jd_dict.get("skills", []) or []
    if not skills:
        return ""
    return " AND ".join(f'"{s}"' for s in skills)
