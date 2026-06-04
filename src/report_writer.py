"""M6 - Report writer (local Excel).

The SCOUT spec calls for Google Sheets; the TA team will provide the
service-account credentials later. Until then we write a local Excel with
two tabs:

  * Candidates  - one row per accepted candidate, with 6-Layer Quality Gate
                  scores inline so the TA can see the score idea at a glance.
  * JD_Summary  - one row per JD, showing boolean query, counts, and run info.

Reverted from previous design:
  * No more _tmp/ purger
  * No more 'shortlisted/' promotion
  * No more ranker dependency
  All accepted candidates (those that pass hard filters + the Quality Gate
  if enabled) are surfaced in the Excel. The TA reviews the file, not the bot.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .ai_screener import QualityGateResult
from .config_loader import Settings, get_logger
from .naukri_bot import Candidate

logger = get_logger(__name__)


@dataclass
class CandidateRow:
    jd_id: str
    candidate: Candidate
    quality: QualityGateResult | None = None
    boolean_query: str = ""
    boolean_attempt: int = 1
    fallback_skill: bool = False

    def to_dict(self) -> dict[str, Any]:
        cand = self.candidate
        row: dict[str, Any] = {
            "JD_ID":                self.jd_id,
            "Candidate_Name":       cand.name,
            "Experience_Years":     cand.experience_years,
            "Current_Company":      cand.current_company,
            "Location":             cand.location,
            "Skills":               " | ".join(cand.skills),
            "Notice_Period_Days":   cand.notice_period_days,
            "Current_CTC_Lacs":     cand.current_ctc_lacs,
            "Last_Active_Days":     cand.last_active_days,
            "Profile_URL":          cand.profile_url,
            "Resume_Path":          str(cand.resume_path) if cand.resume_path else "",
            "Boolean_Query":        self.boolean_query,
            "Boolean_Attempt":      self.boolean_attempt,
            "Fallback_Skill":       "YES" if self.fallback_skill else "no",
        }
        if self.quality is not None:
            row.update(self.quality.to_row())
        else:
            row.update({
                "L1_Project_RR": "", "L2_Role_Ownership": "", "L3_Environment_Fit": "",
                "L4_Project_Depth": "", "L5_TBD": "", "L6_TBD": "", "Total_Score": "",
                "Hard_Reject": "n/a", "Reject_Reasons": "", "Flags": "", "Summary": "",
            })
        return row


def _jd_summary_row(
    jd_id: str,
    role: str,
    boolean_query: str,
    profiles_seen: int,
    resumes_downloaded: int,
    hard_rejects: int,
    ai_rejects: int,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    return {
        "JD_ID": jd_id,
        "Role": role,
        "Boolean_Query": boolean_query,
        "Profiles_Seen": profiles_seen,
        "Resumes_Downloaded": resumes_downloaded,
        "Hard_Rejects": hard_rejects,
        "AI_Rejects": ai_rejects,
        "Run_Started_UTC": started_at,
        "Run_Finished_UTC": finished_at,
    }


def write_report(
    settings: Settings,
    jd_id: str,
    role: str,
    boolean_query: str,
    rows: list[CandidateRow],
    counts: dict[str, int],
    started_at: datetime,
) -> Path:
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    date_str = started_at.strftime("%Y%m%d_%H%M")
    out_path = settings.outputs_dir / f"scout_{jd_id}_{date_str}.xlsx"

    candidates_df = pd.DataFrame([r.to_dict() for r in rows]) if rows else pd.DataFrame()

    summary_df = pd.DataFrame([_jd_summary_row(
        jd_id=jd_id, role=role, boolean_query=boolean_query,
        profiles_seen=counts.get("profiles_seen", 0),
        resumes_downloaded=counts.get("resumes_downloaded", 0),
        hard_rejects=counts.get("rejected_pre_ai", 0),
        ai_rejects=counts.get("rejected_post_ai", 0),
        started_at=started_at.isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat(),
    )])

    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        candidates_df.to_excel(xw, index=False, sheet_name="Candidates")
        summary_df.to_excel(xw, index=False, sheet_name="JD_Summary")

        for sheet_name, widths in [
            ("Candidates",  [10, 24, 8, 22, 22, 40, 8, 10, 8, 40, 40, 40, 8, 8,
                             10, 10, 10, 10, 10, 10, 8, 10, 30, 6, 30, 40, 50]),
            ("JD_Summary",  [10, 28, 50, 12, 16, 12, 12, 22, 22]),
        ]:
            ws = xw.sheets[sheet_name]
            from openpyxl.utils import get_column_letter
            for i, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(i)].width = w
            ws.freeze_panes = "A2"

    logger.info("Wrote scout report: %s (%d candidates)", out_path, len(rows))
    return out_path
