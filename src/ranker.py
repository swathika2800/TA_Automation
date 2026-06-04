"""M6 - Ranker.

Given a list of evaluated candidates (those that PASSED the AI threshold),
sort them by `match_score` descending and assign rank numbers. The list passed
in is the only one allowed to leave the pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RankedCandidate:
    rank: int
    name: str
    match_score: int
    strengths: list[str]
    gaps: list[str]
    profile_url: str
    resume_path: str
    reasoning: str

    def to_row(self) -> dict[str, Any]:
        return {
            "Rank": self.rank,
            "Name": self.name,
            "Match Score": self.match_score,
            "Strengths": " | ".join(self.strengths),
            "Gaps": " | ".join(self.gaps),
            "Profile URL": self.profile_url,
            "Resume Path": self.resume_path,
            "Reasoning": self.reasoning,
        }


def rank(evaluated: list[dict[str, Any]]) -> list[RankedCandidate]:
    evaluated_sorted = sorted(
        evaluated, key=lambda c: c["ai"]["match_score"], reverse=True
    )
    out: list[RankedCandidate] = []
    for i, c in enumerate(evaluated_sorted, start=1):
        ai = c["ai"]
        out.append(
            RankedCandidate(
                rank=i,
                name=c["name"],
                match_score=int(ai["match_score"]),
                strengths=list(ai.get("strengths", [])),
                gaps=list(ai.get("gaps", [])),
                profile_url=c["profile_url"],
                resume_path=str(c["resume_path"]),
                reasoning=ai.get("reasoning", ""),
            )
        )
    return out
