"""M1 - Job Description loader.

Reads a normal, hand-maintained Excel file from the TA team and returns a
list of normalized `JobDescription` objects. The loader is forgiving:

  * Column headers are matched case-insensitively, with surrounding
    whitespace stripped, and via a small alias map (e.g. "JD ID" -> "JD_ID",
    "Skills Required" -> "Skills").
  * The header row is auto-detected (it does not have to be row 1).
  * Extra columns are ignored.
  * Blank rows are dropped.
  * Multiple header candidates are tried (first sheet, by default).

Expected columns (exact or via alias):
  JD_ID | Role | Skills | Experience_Min_Years | Experience_Max_Years
  | Location | Active   (Notes is optional)

To use the company's own file, drop it at `jd_input/jds.xlsx` (default) or
pass `excel_path=` to point at any other file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .config_loader import Settings, get_logger

logger = get_logger(__name__)

CANONICAL_COLUMNS = (
    "JD_ID", "Role", "Skills",
    "Experience_Min_Years", "Experience_Max_Years",
    "Location", "Active", "Notes",
    "CTC_Ceiling_Lacs", "Freshness_Days",
)

# Aliases allow the company file to use human-friendly headers.
# Keys are lowercased + whitespace-stripped versions of what may appear in
# the Excel; values are the canonical names the rest of the pipeline uses.
COLUMN_ALIASES: dict[str, str] = {
    "jd_id": "JD_ID",
    "jd id": "JD_ID",
    "jd": "JD_ID",
    "id": "JD_ID",
    "role": "Role",
    "title": "Role",
    "position": "Role",
    "job role": "Role",
    "skills": "Skills",
    "required skills": "Skills",
    "skills required": "Skills",
    "tech stack": "Skills",
    "experience_min_years": "Experience_Min_Years",
    "min years": "Experience_Min_Years",
    "min_experience": "Experience_Min_Years",
    "min experience": "Experience_Min_Years",
    "experience_min": "Experience_Min_Years",
    "min exp": "Experience_Min_Years",
    "experience_max_years": "Experience_Max_Years",
    "max years": "Experience_Max_Years",
    "max_experience": "Experience_Max_Years",
    "max experience": "Experience_Max_Years",
    "experience_max": "Experience_Max_Years",
    "max exp": "Experience_Max_Years",
    "experience": "Experience_Min_Years",
    "location": "Location",
    "city": "Location",
    "locations": "Location",
    "active": "Active",
    "enabled": "Active",
    "status": "Active",
    "notes": "Notes",
    "comments": "Notes",
    "remarks": "Notes",
    "ctc_ceiling_lacs": "CTC_Ceiling_Lacs",
    "ctc ceiling": "CTC_Ceiling_Lacs",
    "ctc": "CTC_Ceiling_Lacs",
    "max ctc": "CTC_Ceiling_Lacs",
    "max_ctc": "CTC_Ceiling_Lacs",
    "salary cap": "CTC_Ceiling_Lacs",
    "ctc cap": "CTC_Ceiling_Lacs",
    "freshness_days": "Freshness_Days",
    "freshness": "Freshness_Days",
    "active in days": "Freshness_Days",
    "last active days": "Freshness_Days",
    "recency days": "Freshness_Days",
}

REQUIRED_CANONICAL = {"JD_ID", "Role", "Skills", "Active"}


@dataclass(frozen=True)
class JobDescription:
    jd_id: str
    role: str
    skills: list[str]
    experience_min: int
    experience_max: int
    locations: list[str]
    notes: str
    ctc_ceiling_lacs: float | None = None
    freshness_days: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "jd_id": self.jd_id,
            "role": self.role,
            "skills": list(self.skills),
            "experience_min": self.experience_min,
            "experience_max": self.experience_max,
            "locations": list(self.locations),
            "notes": self.notes,
            "ctc_ceiling_lacs": self.ctc_ceiling_lacs,
            "freshness_days": self.freshness_days,
        }


def _split(value: Any) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    text = str(value)
    parts = re.split(r"[,;|]", text)
    return [p.strip() for p in parts if p and p.strip()]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, float) and pd.isna(value):
        return False
    return str(value).strip().upper() in {"TRUE", "1", "YES", "Y", "ACTIVE", "OPEN"}


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, (int, float)) and not pd.isna(value):
        return int(value)
    s = str(value).strip()
    if not s:
        return default
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m else default


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "null", "-"}:
        return None
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


_RANGE_RE = re.compile(r"(\d+)\s*[-–to]+\s*(\d+)", re.IGNORECASE)


def _parse_experience(min_v: Any, max_v: Any, fallback: Any) -> tuple[int, int]:
    mn = _coerce_int(min_v, default=0)
    mx = _coerce_int(max_v, default=0)
    if mn or mx:
        return mn, mx
    if fallback is None:
        return 0, 0
    s = str(fallback)
    m = _RANGE_RE.search(s)
    if m:
        return int(m.group(1)), int(m.group(2))
    single = re.search(r"\d+", s)
    return (int(single.group(0)), int(single.group(0))) if single else (0, 0)


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Map every header to a canonical name. Unknown columns are kept as-is
    (the loader simply ignores them when building JobDescription)."""
    rename: dict[str, str] = {}
    for col in df.columns:
        key = str(col).strip().lower()
        canonical = COLUMN_ALIASES.get(key, str(col).strip())
        rename[col] = canonical
    return df.rename(columns=rename)


def _find_header_row(raw: pd.DataFrame) -> int:
    """Find the row that looks like a header line (contains 'JD' and 'Role')."""
    for i, row in raw.iterrows():
        cells = [str(v).strip().lower() for v in row.tolist() if pd.notna(v)]
        joined = " ".join(cells)
        if "jd" in joined and ("role" in joined or "title" in joined or "position" in joined):
            return i
    return 0


def _read_excel(path: Path) -> pd.DataFrame:
    """Read an Excel sheet, auto-detecting the header row and normalizing
    column names. Supports .xlsx and .xls."""
    raw = pd.read_excel(path, header=None, dtype=object)
    if raw.empty:
        return pd.DataFrame()

    header_row = _find_header_row(raw)
    df = pd.read_excel(path, header=header_row, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")
    df = _normalize_columns(df)

    if "JD_ID" in df.columns:
        df["JD_ID"] = df["JD_ID"].astype(str).str.strip()
        df = df[df["JD_ID"].astype(bool) & (df["JD_ID"].str.lower() != "nan")]

    return df


def _row_to_jd(row: pd.Series) -> JobDescription:
    exp_min, exp_max = _parse_experience(
        row.get("Experience_Min_Years"),
        row.get("Experience_Max_Years"),
        row.get("Experience_Min_Years") or row.get("Experience_Max_Years"),
    )
    return JobDescription(
        jd_id=str(row["JD_ID"]).strip(),
        role=str(row["Role"]).strip(),
        skills=_split(row["Skills"]),
        experience_min=exp_min,
        experience_max=exp_max,
        locations=_split(row.get("Location", "")),
        notes=str(row.get("Notes", "") or "").strip(),
        ctc_ceiling_lacs=_coerce_float(row.get("CTC_Ceiling_Lacs")),
        freshness_days=_coerce_int(row.get("Freshness_Days")) or None,
    )


def load_jds(
    settings: Settings,
    jd_id_filter: str | None = None,
    excel_path: Path | None = None,
) -> list[JobDescription]:
    path = excel_path or (settings.jd_input_dir / "jds.xlsx")
    if not path.exists():
        raise FileNotFoundError(
            f"JD Excel not found: {path}\n"
            f"Place the company-maintained JD sheet at this location "
            f"(see jd_input/jds_sample.xlsx for the expected schema)."
        )

    logger.info("Loading JDs from %s", path)
    df = _read_excel(path)

    if df.empty:
        logger.warning("JD sheet is empty: %s", path)
        return []

    missing = REQUIRED_CANONICAL - set(df.columns)
    if missing:
        raise ValueError(
            f"JD Excel is missing required columns: {sorted(missing)}.\n"
            f"Found columns: {list(df.columns)}\n"
            f"See jd_input/jds_sample.xlsx for the expected header layout."
        )

    if "Active" in df.columns:
        df = df[df["Active"].apply(_truthy)]
    if jd_id_filter and "JD_ID" in df.columns:
        df = df[df["JD_ID"].astype(str).str.upper() == jd_id_filter.upper()]

    jds: list[JobDescription] = []
    for _, row in df.iterrows():
        try:
            jds.append(_row_to_jd(row))
        except Exception as exc:
            logger.warning("Skipping invalid JD row %s: %s", row.get("JD_ID"), exc)

    logger.info("Loaded %d active JD(s) from %s", len(jds), path.name)
    return jds
