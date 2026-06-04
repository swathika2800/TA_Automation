"""M7 - SCOUT orchestrator.

End-to-end pipeline per JD:

  1. Load JD from Excel
  2. Load screening guidelines (for hard filters)
  3. Generate boolean query via Claude (or static fallback)
  4. Launch Playwright -> Login -> Navigate to Resdex
  5. Apply boolean query + filters (exp, location, NP, CTC, freshness)
  6. Paginate results, extract profiles
  7. Dedup (name + current_company) -- in bot
  8. Hard cap at MAX_RESUMES_PER_JD
  9. Parse resumes
 10. Run 6-Layer Quality Gate (Claude) per candidate
 11. Write Candidates + JD_Summary to outputs/scout_<JD_ID>_<timestamp>.xlsx
 12. Write per-JD run-metric to logs/run_<date>.json (counts only)

CLI flags:
  --jd-id JD001       process a single JD
  --all               process every active JD
  --limit N           override MAX_RESUMES_PER_JD for this run
  --hint "text"       TA hint to refine the boolean query
  --regenerate        regenerate the boolean (max 2 total per spec)
  --dry-run           run M1+M2+M3 prompt build only; no browser, no Excel
  --no-score          skip the Quality Gate (sourcing only)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai_screener import score_candidate
from src.config_loader import Settings, get_logger, load_settings
from src.criteria_builder import (
    generate_boolean_query, load_guidelines,
)
from src.jd_loader import load_jds
from src.naukri_bot import NaukriBot
from src.report_writer import CandidateRow, write_report
from src.resume_parser import parse as parse_resume

logger = get_logger("main")


def _write_run_metric(settings: Settings, jd_id: str, m: dict) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = settings.logs_dir / f"run_{now.strftime('%Y%m%d')}.json"
    record = {
        "timestamp_utc": now.isoformat().replace("+00:00", "Z"),
        "jd_id": jd_id,
        **m,
    }
    existing: list[dict] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except json.JSONDecodeError:
            existing = []
    existing.append(record)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _safe_delete(path: Path | None) -> None:
    if path and path.exists():
        try:
            path.unlink()
        except OSError:
            pass


async def process_jd(
    settings: Settings,
    jd,
    hint: str | None,
    regenerate: bool,
    dry_run: bool,
    do_score: bool,
) -> dict:
    started = datetime.now(timezone.utc)
    logger.info("=== %s | %s ===", jd.jd_id, jd.role)

    guidelines = load_guidelines(settings, jd)
    jd_dict = jd.to_dict()

    attempt = 2 if regenerate else 1
    boolean_query = generate_boolean_query(settings, jd_dict, hint=hint, attempt=attempt)
    boolean_query_fallback = '"' + '" AND "'.join(jd.skills) + '"'
    fallback_skill = boolean_query == boolean_query_fallback
    logger.info("Boolean query (attempt %d): %s%s",
                attempt, boolean_query, "  [FALLBACK]" if fallback_skill else "")

    if dry_run:
        return {
            "status": "dry_run",
            "jd_id": jd.jd_id,
            "boolean_query": boolean_query,
            "fallback": fallback_skill,
            "guidelines": {
                "mandatory": guidelines.mandatory_skills,
                "min_score": guidelines.min_ai_score,
                "ctc_ceiling": guidelines.ctc_ceiling_lacs,
                "freshness_days": guidelines.freshness_days,
            },
        }

    counts = {
        "status": "ok",
        "profiles_seen": 0,
        "rejected_pre_ai": 0,
        "rejected_post_ai": 0,
        "resumes_downloaded": 0,
    }
    rows: list[CandidateRow] = []

    try:
        async with NaukriBot(settings, jd, guidelines) as bot:
            await bot.login()
            async for cand in bot.search(boolean_query):
                counts["profiles_seen"] += 1
                counts["resumes_downloaded"] += 1 if cand.resume_path else 0

                if cand.resume_path is None or not cand.resume_path.exists():
                    counts["rejected_post_ai"] += 1
                    _safe_delete(cand.resume_path)
                    continue

                try:
                    resume = parse_resume(cand.resume_path)
                except Exception as exc:
                    logger.debug("Parse failed for %s: %s", cand.name, exc)
                    counts["rejected_post_ai"] += 1
                    _safe_delete(cand.resume_path)
                    continue

                quality = None
                if do_score:
                    try:
                        quality = score_candidate(settings, jd_dict, cand.name, resume)
                        if quality.hard_reject:
                            counts["rejected_post_ai"] += 1
                            _safe_delete(cand.resume_path)
                            continue
                    except Exception as exc:
                        logger.warning("Quality Gate failed for %s: %s", cand.name, exc)

                rows.append(CandidateRow(
                    jd_id=jd.jd_id,
                    candidate=cand,
                    quality=quality,
                    boolean_query=boolean_query,
                    boolean_attempt=attempt,
                    fallback_skill=fallback_skill,
                ))
    except Exception as exc:
        logger.exception("Bot failed for %s: %s", jd.jd_id, exc)
        counts["status"] = "error"
        counts["error"] = str(exc)

    out = write_report(
        settings,
        jd_id=jd.jd_id,
        role=jd.role,
        boolean_query=boolean_query,
        rows=rows,
        counts=counts,
        started_at=started,
    )
    try:
        counts["output_file"] = str(out.relative_to(Path.cwd()))
    except ValueError:
        counts["output_file"] = str(out)

    counts["jd_id"] = jd.jd_id
    return counts


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SCOUT - Candidate Sourcing Pipeline")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--jd-id", help="Process a single JD by ID, e.g. JD001")
    g.add_argument("--all", action="store_true", help="Process every active JD")
    p.add_argument("--limit", type=int, default=None,
                   help="Override MAX_RESUMES_PER_JD for this run")
    p.add_argument("--hint", type=str, default=None,
                   help="TA hint to refine the Claude-generated boolean query")
    p.add_argument("--regenerate", action="store_true",
                   help="Regenerate boolean query (max 2 attempts total)")
    p.add_argument("--dry-run", action="store_true",
                   help="M1+M2 only; no browser, no Excel")
    p.add_argument("--no-score", action="store_true",
                   help="Skip the 6-Layer Quality Gate (sourcing only)")
    return p.parse_args()


async def main_async() -> int:
    args = parse_args()
    settings = load_settings()
    if args.limit:
        settings = Settings(
            **{**settings.__dict__, "max_resumes_per_jd": args.limit}
        )

    jds = load_jds(settings, jd_id_filter=args.jd_id)
    if not jds:
        logger.error("No active JDs to process (filter=%s)", args.jd_id)
        return 1

    failures = 0
    for jd in jds:
        try:
            metric = await process_jd(
                settings, jd,
                hint=args.hint,
                regenerate=args.regenerate,
                dry_run=args.dry_run,
                do_score=not args.no_score,
            )
            _write_run_metric(settings, jd.jd_id, metric)
        except Exception as exc:
            logger.exception("JD %s failed: %s", jd.jd_id, exc)
            _write_run_metric(settings, jd.jd_id, {
                "status": "error", "jd_id": jd.jd_id, "error": str(exc),
            })
            failures += 1

    return 0 if failures == 0 else 2


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
