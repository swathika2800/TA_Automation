"""M5 - 6-Layer Resume Quality Gate (Claude Haiku via LiteLLM proxy).

Per the SCOUT spec, scoring is OPTIONAL and the TA makes the final call.
This module implements the 6-layer rubric:

  L1  Project R&R Match       25 pts   hard-reject (all 3 projects must cover JD R&R)
  L2  Role Ownership          -10 pen  flag title inflation
  L3  Environment Fit          10 pts  flag mismatch
  L4  Project Depth            20 pts  hard-reject (vague 5+ yr candidates)
  L5  (TBD per video)          ?? pts  hard-reject
  L6  (TBD per video)          ?? pts  flag only

The function returns a dict with per-layer scores, total, hard_reject flag,
reasons, and a "give_me_the_idea" sample string for TA review.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from .config_loader import Settings, get_logger

logger = get_logger(__name__)


# Maximum points per layer. L5 and L6 are placeholders; total is recomputed
# from the live values.
LAYER_WEIGHTS = {
    "L1_project_rr_match":   25,
    "L2_role_ownership":    -10,
    "L3_environment_fit":    10,
    "L4_project_depth":      20,
    "L5_tbd":                15,
    "L6_tbd":                20,
}
HARD_REJECT_LAYERS = {"L1_project_rr_match", "L4_project_depth", "L5_tbd"}


@dataclass
class QualityGateResult:
    jd_id: str
    candidate_name: str
    layer_scores: dict[str, int] = field(default_factory=dict)
    layer_notes: dict[str, str] = field(default_factory=dict)
    total_score: int = 0
    hard_reject: bool = False
    reject_reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    summary: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "L1_Project_RR":        self.layer_scores.get("L1_project_rr_match", 0),
            "L2_Role_Ownership":    self.layer_scores.get("L2_role_ownership", 0),
            "L3_Environment_Fit":   self.layer_scores.get("L3_environment_fit", 0),
            "L4_Project_Depth":     self.layer_scores.get("L4_project_depth", 0),
            "L5_TBD":               self.layer_scores.get("L5_tbd", 0),
            "L6_TBD":               self.layer_scores.get("L6_tbd", 0),
            "Total_Score":          self.total_score,
            "Hard_Reject":          "YES" if self.hard_reject else "no",
            "Reject_Reasons":       " | ".join(self.reject_reasons),
            "Flags":                " | ".join(self.flags),
            "Summary":              self.summary,
        }


SYSTEM_PROMPT = """You are a strict technical recruiter evaluating a candidate's
resume against a job description using a 6-Layer Quality Gate. Be evidence-based
and concise. Reply ONLY with a single JSON object matching the schema described
in the user message. No prose, no markdown, no code fences."""


def _build_user_prompt(jd: dict[str, Any], resume: dict[str, Any]) -> str:
    jd_text = (
        f"Job ID: {jd['jd_id']}\n"
        f"Role: {jd['role']}\n"
        f"Required skills: {', '.join(jd['skills'])}\n"
        f"Experience range: {jd['experience_min']}-{jd['experience_max']} years\n"
        f"Locations: {', '.join(jd['locations'])}\n"
    )
    # Include full JD description when available for much more accurate scoring
    desc = (jd.get("jd_description") or "").strip()
    if desc:
        jd_text += f"\nFull Job Description:\n{desc[:3000]}\n"
    resume_text = (
        f"Total experience (years): {resume.get('total_experience_years')}\n"
        f"Skills: {', '.join(resume.get('skills', []))}\n"
        f"Experience block: {' | '.join(resume.get('experience', []))[:2500]}\n"
        f"Projects: {' | '.join(resume.get('projects', []))[:1500]}\n"
    )
    schema = (
        "{\n"
        '  "L1_project_rr_match":   <0-25  pts; reject if all 3 projects lack JD R&R>,\n'
        '  "L1_note":               "<one short sentence>",\n'
        '  "L2_role_ownership":    <-10..0 pts; negative penalises title inflation>,\n'
        '  "L2_note":               "<one short sentence>",\n'
        '  "L3_environment_fit":   <0-10  pts; mismatch -> 0>,\n'
        '  "L3_note":               "<one short sentence>",\n'
        '  "L4_project_depth":     <0-20  pts; vague 5+yr -> 0 and reject>,\n'
        '  "L4_note":               "<one short sentence>",\n'
        '  "L5_tbd":               <0-15  pts; placeholder layer; reject if 0>,\n'
        '  "L5_note":               "<one short sentence>",\n'
        '  "L6_tbd":               <0-20  pts; placeholder layer; flag only>,\n'
        '  "L6_note":               "<one short sentence>",\n'
        '  "summary":              "<2 sentences for the recruiter>"\n'
        "}"
    )
    return (
        "Score this candidate on the 6-Layer Quality Gate.\n\n"
        "=== JOB DESCRIPTION ===\n" + jd_text +
        "\n=== RESUME (parsed) ===\n" + resume_text +
        "\n=== RESPONSE SCHEMA ===\n" + schema +
        "\nReturn only the JSON object."
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = _JSON_RE.search(content)
        if m:
            return json.loads(m.group(0))
        raise


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)
def _call_api(settings: Settings, messages: list[dict[str, str]]) -> str:
    from openai import OpenAI

    client = OpenAI(
        api_key=settings.litellm_api_key,
        base_url=settings.litellm_base_url,
    )
    cfg = settings.raw.get("ai", {})
    resp = client.chat.completions.create(
        model=settings.litellm_model,
        messages=messages,
        max_tokens=int(cfg.get("max_tokens", 1500)),
        temperature=float(cfg.get("temperature", 0.0)),
        timeout=int(cfg.get("request_timeout_sec", 60)),
    )
    return (resp.choices[0].message.content or "").strip()


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def score_candidate(
    settings: Settings,
    jd: dict[str, Any],
    candidate_name: str,
    resume: dict[str, Any],
) -> QualityGateResult:
    """Run the 6-Layer Quality Gate on one candidate."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": _build_user_prompt(jd, resume)},
    ]
    raw_text = _call_api(settings, messages)
    parsed = _extract_json(raw_text)

    result = QualityGateResult(jd_id=jd["jd_id"], candidate_name=candidate_name)

    layer_keys = list(LAYER_WEIGHTS.keys())
    for key in layer_keys:
        weight = LAYER_WEIGHTS[key]
        if weight < 0:
            lo, hi = weight, 0
        else:
            lo, hi = 0, weight
        try:
            val = int(parsed.get(key, 0))
        except (TypeError, ValueError):
            val = 0
        result.layer_scores[key] = _clamp(val, lo, hi)
        result.layer_notes[key] = str(parsed.get(f"{key.split('_', 1)[0]}_note", "")).strip()

    positive_sum = sum(
        v for k, v in result.layer_scores.items() if LAYER_WEIGHTS[k] > 0
    )
    result.total_score = positive_sum + min(0, result.layer_scores.get("L2_role_ownership", 0))

    reject_reasons: list[str] = []
    flags: list[str] = []
    for key in HARD_REJECT_LAYERS:
        if result.layer_scores[key] == 0:
            reject_reasons.append(f"{key}=0")
    for key, score in result.layer_scores.items():
        if LAYER_WEIGHTS[key] > 0 and score == 0 and key not in HARD_REJECT_LAYERS:
            flags.append(f"{key}=0")

    result.hard_reject = bool(reject_reasons)
    result.reject_reasons = reject_reasons
    result.flags = flags
    result.summary = str(parsed.get("summary", "")).strip()

    return result
