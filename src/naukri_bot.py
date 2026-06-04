"""M3 - Naukri Recruiter / Resdex bot (Playwright).

Responsibilities:
  * Log in to Naukri via the public login page.
  * Navigate to Resdex (resdex.naukri.com/v3/search) and apply the boolean
    query, experience, location, CTC, and freshness filters.
  * Paginate through search results, opening each candidate profile.
  * Extract profile fields: name, current_company, experience_years, location,
    skills, notice_period_days, current_ctc_lacs, last_active_days, profile_url.
  * Deduplicate by (name, current_company) -- silently skip repeats.
  * Hard cap: stop once MAX_RESUMES_PER_JD resumes have been downloaded.
  * Anti-detection: random jitter, realistic viewport, sticky session cookies.
  * Apply HardFilter BEFORE downloading the resume. Rejected candidates leave
    no trace (no log, no PDF).

NOTE: Resdex DOM is owned by Naukri and changes often. All CSS / XPath
selectors are isolated in `SELECTORS` so they can be updated without
touching the pipeline logic.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from .config_loader import Settings, get_logger
from .criteria_builder import HardFilter
from .jd_loader import JobDescription

logger = get_logger(__name__)


@dataclass
class Candidate:
    candidate_id: str
    name: str
    experience_years: int | None
    current_company: str
    location: str
    skills: list[str]
    notice_period_days: int | None
    current_ctc_lacs: float | None
    last_active_days: int | None
    profile_url: str
    resume_path: Path | None = None


# --- Selectors. Update this map when Resdex DOM changes. --------------------
# These are best-guess placeholders that target the public Resdex search
# experience. The first real run should be HEADLESS=false so the team can
# inspect and refine them. Once a working set is confirmed, freeze it here.
SELECTORS = {
    "login_username": 'input[placeholder*="Email"], input[type="text"], input#usernameField',
    "login_password": 'input[type="password"], input#passwordField',
    "login_submit":   'button[type="submit"], input[type="submit"], button.loginButton',

    "search_input":   'input#FZ_KEYWORD_ANY, input[placeholder*="Skills"], input[placeholder*="Keywords"]',
    "exp_min_input":  'input#expMin, input[placeholder*="Min"], input[id*="expMin"]',
    "exp_max_input":  'input#expMax, input[placeholder*="Max"], input[id*="expMax"]',
    "loc_input":      'input#location, input[placeholder*="Location"], input[id*="location"]',
    "ctc_input":      'input#ctc, input[placeholder*="CTC"], input[id*="ctc"]',
    "active_in_dropdown": 'select#activeIn, select[id*="activeIn"]',
    "search_button":  'button:has-text("Search"), button#search, input[type="submit"][value*="Search"]',

    "candidate_card": '.tuple, .candidate-card, [class*="tuple"], [class*="candidate-card"], [data-testid*="result"]',
    "profile_name":   '.name, h1, .candidateName, [class*="name"]',
    "experience":     '.exp, .experience, [class*="exp"]:not([class*="experienceYears"])',
    "company":        '.comp-name, .company, .org, [class*="company"]',
    "location":       '.loc, .location, [class*="location"]',
    "ctc":            '.ctc, .salary, [class*="ctc"]',
    "notice_period":  '.noticePeriod, [class*="notice"]',
    "active_days":    '.active, .lastActive, [class*="active"]',
    "skills":         '.skill, .chip, .tag, [class*="skill"]',
    "resume_link":    'a:has-text("View Resume"), a:has-text("Download"), a[href*="resume"]',
    "pagination_next": 'a:has-text("Next"), button:has-text("Next"), [class*="next"]',
}


# ---------------------------------------------------------------------------

_YEARS_RE = re.compile(r"(\d+)\s*(?:yrs?|years?)", re.IGNORECASE)
_DAYS_RE = re.compile(r"(\d+)\s*(?:days?|d)\b", re.IGNORECASE)
_MONTHS_RE = re.compile(r"(\d+)\s*(?:months?|m)\b", re.IGNORECASE)
_CTC_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lacs?|lpa|l)\b", re.IGNORECASE)


def _to_int(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"\d+", value)
    return int(m.group(0)) if m else None


def _parse_experience_years(text: str) -> int | None:
    m = _YEARS_RE.search(text)
    return int(m.group(1)) if m else None


def _parse_notice_period_days(text: str) -> int | None:
    m = _DAYS_RE.search(text)
    if m:
        return int(m.group(1))
    m = _MONTHS_RE.search(text)
    if m:
        return int(m.group(1)) * 30
    return None


def _parse_ctc_lacs(text: str) -> float | None:
    m = _CTC_RE.search(text)
    return float(m.group(1)) if m else None


def _parse_active_days(text: str) -> int | None:
    if not text:
        return None
    low = text.lower()
    m = re.search(r"(\d+)\s*day", low)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*month", low)
    if m:
        return int(m.group(1)) * 30
    if "active" in low and "today" in low:
        return 0
    return None


def _skill_tokens(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[,/|;]", text) if s.strip()]


class NaukriBot:
    def __init__(self, settings: Settings, jd: JobDescription, hard_filter: HardFilter):
        self.settings = settings
        self.jd = jd
        self.filter = hard_filter
        self._browser = None
        self._context = None
        self._page = None
        self._seen: set[tuple[str, str]] = set()
        self._downloaded: int = 0

    # --- context manager ------------------------------------------------

    async def __aenter__(self) -> "NaukriBot":
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.settings.headless,
            slow_mo=self.settings.raw.get("playwright", {}).get("slow_mo_ms", 0),
        )
        
        state_path = self.settings.session_dir.parent / "naukri_state.json"
        
        viewport = self.settings.raw.get("playwright", {}).get("viewport", {"width": 1440, "height": 900})
        user_agent = self.settings.raw.get("playwright", {}).get("user_agent")
        
        if state_path.exists():
            logger.info(f"Reusing saved session from {state_path}")
            self._context = await self._browser.new_context(
                storage_state=str(state_path),
                viewport=viewport,
                user_agent=user_agent,
            )
        else:
            logger.info("No saved session found. You will need to log in.")
            self._context = await self._browser.new_context(
                viewport=viewport,
                user_agent=user_agent,
            )
            
        self._page = await self._context.new_page()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self._context:
                state_path = self.settings.session_dir.parent / "naukri_state.json"
                await self._context.storage_state(path=str(state_path))
                logger.info(f"Saved browser session to {state_path}")
                await self._context.close()
            if self._browser:
                await self._browser.close()
        finally:
            await self._playwright.stop()

    # --- anti-detection --------------------------------------------------

    async def _jitter(self) -> None:
        cfg = self.settings.raw.get("playwright", {})
        lo = int(cfg.get("action_jitter_min_ms", 2000))
        hi = int(cfg.get("action_jitter_max_ms", 5000))
        await asyncio.sleep(random.uniform(lo, hi) / 1000)

    # --- public steps ----------------------------------------------------

    async def login(self) -> None:
        if not self.settings.naukri_username or not self.settings.naukri_password:
            raise RuntimeError("NAUKRI_USERNAME / NAUKRI_PASSWORD missing in .env")

        login_url = self.settings.raw.get("naukri", {}).get(
            "login_url", "https://www.naukri.com/nlogin/login"
        )
        resdex_url = self.settings.raw.get("naukri", {}).get(
            "resdex_url", "https://resdex.naukri.com/v3/search"
        )

        logger.info("Checking if already logged in...")
        await self._page.goto(resdex_url, wait_until="domcontentloaded")
        await self._page.wait_for_load_state("networkidle")

        if "resdex.naukri.com" in self._page.url.lower() and "login" not in self._page.url.lower():
            logger.info("Already logged in! Reusing saved session.")
            return

        logger.warning("Not logged in. Navigating to login page...")
        await self._page.goto(login_url, wait_until="domcontentloaded")
        await self._jitter()

        try:
            # Auto-fill the email and password to save the recruiter time
            await self._page.fill(SELECTORS["login_username"], self.settings.naukri_username)
            await self._page.fill(SELECTORS["login_password"], self.settings.naukri_password)
        except Exception as exc:
            logger.debug("Could not auto-fill login: %s", exc)

        print("\n" + "!"*80)
        print("!!! ACTION REQUIRED: PLEASE LOG IN TO NAUKRI IN THE BROWSER WINDOW !!!")
        print("1. Enter the OTP from your phone.")
        print("2. Wait until you see the Naukri dashboard.")
        print("3. Come back to this terminal and press [ENTER] to continue!")
        print("!"*80 + "\n")

        # Pause the script and wait for the user to press Enter
        await asyncio.to_thread(input, "Press ENTER when you are successfully logged in: ")

        logger.info("Navigating to Resdex search...")
        await self._page.goto(resdex_url, wait_until="domcontentloaded")
        await self._page.wait_for_load_state("networkidle")

    async def search(self, boolean_query: str) -> AsyncIterator[Candidate]:
        """Yield Candidate objects. Applies HardFilter BEFORE downloading."""
        await self._fill_filters(boolean_query)
        await self._page.click(SELECTORS["search_button"])
        await self._page.wait_for_load_state("networkidle")
        await self._jitter()

        cap = self.settings.max_resumes_per_jd
        while self._downloaded < cap:
            cards = await self._page.query_selector_all(SELECTORS["candidate_card"])
            if not cards:
                logger.info("No more candidate cards on this page")
                break

            for card in cards:
                if self._downloaded >= cap:
                    break
                cand = await self._scrape_card(card)
                if cand is None:
                    continue

                if self._is_duplicate(cand):
                    continue

                profile_dict: dict[str, Any] = {
                    "skills": cand.skills,
                    "experience_years": cand.experience_years,
                    "location": cand.location,
                    "notice_period_days": cand.notice_period_days,
                    "current_ctc_lacs": cand.current_ctc_lacs,
                    "last_active_days": cand.last_active_days,
                }
                rejected, _reason = self.filter.rejects(profile_dict)
                if rejected:
                    # STRICT: do not download, do not log, do not retain
                    continue

                cand = await self._download_resume(cand)
                if cand.resume_path:
                    self._downloaded += 1
                yield cand

            nxt = await self._page.query_selector(SELECTORS["pagination_next"])
            if not nxt:
                break
            try:
                await nxt.click()
                await self._page.wait_for_load_state("networkidle")
                await self._jitter()
            except Exception:
                break

        if self._downloaded >= cap:
            logger.info("Hard cap reached (%d resumes) for %s", cap, self.jd.jd_id)

    # --- internals -------------------------------------------------------

    async def _fill_filters(self, boolean_query: str) -> None:
        try:
            await self._page.fill(SELECTORS["search_input"], boolean_query)
        except Exception as exc:
            logger.debug("search_input fill failed: %s", exc)

        try:
            await self._page.fill(SELECTORS["exp_min_input"], str(self.jd.experience_min))
            await self._page.fill(SELECTORS["exp_max_input"], str(self.jd.experience_max))
        except Exception as exc:
            logger.debug("experience filter fill failed: %s", exc)

        if self.filter.locations:
            try:
                await self._page.fill(SELECTORS["loc_input"], self.filter.locations[0])
            except Exception as exc:
                logger.debug("location filter fill failed: %s", exc)

        if self.filter.ctc_ceiling_lacs is not None:
            try:
                await self._page.fill(SELECTORS["ctc_input"], str(self.filter.ctc_ceiling_lacs))
            except Exception as exc:
                logger.debug("ctc filter fill failed: %s", exc)

    def _is_duplicate(self, cand: Candidate) -> bool:
        key = (cand.name.strip().lower(), cand.current_company.strip().lower())
        if not key[0]:
            return True
        if key in self._seen:
            return True
        self._seen.add(key)
        return False

    async def _scrape_card(self, card) -> Candidate | None:
        try:
            text = await card.inner_text()
            name_el = await card.query_selector(SELECTORS["profile_name"])
            name = (await name_el.inner_text()).strip() if name_el else ""
            if not name:
                first_line = text.splitlines()[0].strip() if text else ""
                name = first_line

            comp_el = await card.query_selector(SELECTORS["company"])
            company = (await comp_el.inner_text()).strip() if comp_el else ""

            loc_el = await card.query_selector(SELECTORS["location"])
            location = (await loc_el.inner_text()).strip() if loc_el else ""

            ctc_el = await card.query_selector(SELECTORS["ctc"])
            ctc_text = (await ctc_el.inner_text()).strip() if ctc_el else ""

            np_el = await card.query_selector(SELECTORS["notice_period"])
            np_text = (await np_el.inner_text()).strip() if np_el else ""

            act_el = await card.query_selector(SELECTORS["active_days"])
            act_text = (await act_el.inner_text()).strip() if act_el else ""

            skill_els = await card.query_selector_all(SELECTORS["skills"])
            skills: list[str] = []
            if skill_els:
                for s in skill_els:
                    txt = (await s.inner_text()).strip()
                    if txt:
                        skills.append(txt)
            else:
                skills = _skill_tokens(text)

            return Candidate(
                candidate_id=uuid.uuid4().hex[:12],
                name=name,
                experience_years=_parse_experience_years(text),
                current_company=company,
                location=location,
                skills=skills,
                notice_period_days=_parse_notice_period_days(np_text or text),
                current_ctc_lacs=_parse_ctc_lacs(ctc_text),
                last_active_days=_parse_active_days(act_text),
                profile_url=await self._extract_link(card),
            )
        except Exception as exc:
            logger.debug("Failed to scrape card: %s", exc)
            return None

    async def _extract_link(self, card) -> str:
        try:
            a = await card.query_selector("a")
            if a:
                return await a.get_attribute("href") or ""
        except Exception:
            pass
        return self._page.url

    async def _download_resume(self, cand: Candidate) -> Candidate:
        cand_dir = self.settings.resumes_dir / self.jd.jd_id
        cand_dir.mkdir(parents=True, exist_ok=True)
        target = cand_dir / f"{cand.candidate_id}.pdf"

        link = await self._page.query_selector(SELECTORS["resume_link"])
        if not link:
            logger.debug("No resume link for %s", cand.name)
            return cand

        try:
            async with self._page.expect_download(
                timeout=self.settings.download_timeout_sec * 1000
            ) as dl_info:
                await link.click()
            download = await dl_info.value
            await download.save_as(target)
            cand.resume_path = target
        except Exception as exc:
            logger.debug("Resume download failed for %s: %s", cand.name, exc)

        return cand
