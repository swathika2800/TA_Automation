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
    "login_username": 'input[placeholder*="registered email"], input[placeholder*="Email"], input[type="email"], input#usernameField',
    "login_password": 'input[type="password"], input#passwordField',
    "login_submit":   'button[type="submit"], button:has-text("Log in"), input[type="submit"], button.loginButton',
    "register_login_btn": 'button:has-text("Register/Log in")',

    "search_input":   'input[placeholder*="keywords"], input[placeholder*="Keywords"]',
    "exp_min_input":  'input[placeholder*="Min experience"]',
    "exp_max_input":  'input[placeholder*="Max experience"]',
    "loc_input":      'input[placeholder*="location"], input[placeholder*="Location"]',
    "ctc_input":      'input[placeholder*="Min salary"]',
    "active_in_dropdown": 'input#adv-active-in',
    "search_button":  'button:has-text("Search candidates")',

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

        # CDP mode (optional) — connects to an already-running Chrome with
        # --remote-debugging-port.  Turn on in settings.json if you can keep
        # a CDP-enabled Chrome running stably.  Otherwise the bot launches
        # its own browser (recommended).
        use_cdp = self.settings.raw.get("playwright", {}).get("use_cdp", False)
        self._connected_via_cdp = False
        if use_cdp:
            cdp_host = self.settings.raw.get("playwright", {}).get("cdp_host", "127.0.0.1")
            cdp_port = self.settings.raw.get("playwright", {}).get("cdp_port", 9222)
            cdp_url = f"http://{cdp_host}:{cdp_port}"
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
                self._connected_via_cdp = True
                logger.info("Connected to existing Chrome at %s", cdp_url)
            except Exception as exc:
                logger.info("CDP connect failed (%s). Falling back to fresh browser.", exc)
        else:
            logger.info("CDP mode disabled. Launching fresh browser.")

        if not self._connected_via_cdp:
            # If no saved session exists and we're launching fresh, the user
            # will need to log in interactively — force a visible browser so
            # they can see the login page and enter the OTP.
            effective_headless = self.settings.headless
            no_saved_session = not (self.settings.session_dir.parent / "naukri_state.json").exists()
            if effective_headless and no_saved_session:
                logger.info(
                    "No saved session — switching to visible browser so you "
                    "can log in.  (Set HEADLESS=true in .env after the first "
                    "successful run to hide the browser.)"
                )
                effective_headless = False

            # Use system Chrome (channel="chrome") for a real User-Agent and
            # proper networking.  Playwright's bundled Chromium often gets
            # blocked by CDNs because its UA contains "HeadlessChrome".
            self._browser = await self._playwright.chromium.launch(
                headless=effective_headless,
                slow_mo=self.settings.raw.get("playwright", {}).get("slow_mo_ms", 0),
                channel="chrome",
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-gpu",
                ],
            )

        state_path = self.settings.session_dir.parent / "naukri_state.json"

        if self._connected_via_cdp:
            # Collect ALL existing pages across ALL contexts — don't open
            # new tabs when the user already has Chrome tabs open.
            all_contexts = self._browser.contexts
            all_pages: list[tuple] = []   # (context, page)
            for ctx in all_contexts:
                for p in ctx.pages:
                    all_pages.append((ctx, p))

            logger.info(
                "CDP: %d context(s), %d page(s) total",
                len(all_contexts), len(all_pages),
            )
            for ctx, p in all_pages:
                logger.debug("  page: %s", p.url)

            # Try to find a page that already shows Naukri
            naukri_page = None
            naukri_ctx = None
            for ctx, p in all_pages:
                url = p.url.lower()
                if "naukri.com" in url:
                    naukri_page = p
                    naukri_ctx = ctx
                    logger.info("Found existing Naukri tab: %s", p.url)
                    break

            if naukri_page:
                self._context = naukri_ctx
                self._page = naukri_page
            elif all_pages:
                # Reuse the first available page (active tab) and navigate it
                self._context, self._page = all_pages[0]
                logger.info(
                    "No Naukri tab found; reusing page: %s",
                    self._page.url,
                )
            else:
                # Truly no pages — only now create a new one
                self._context = (
                    all_contexts[0]
                    if all_contexts
                    else await self._browser.new_context()
                )
                self._page = await self._context.new_page()
                logger.info("No existing pages — created a new tab")
        else:
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
            if self._connected_via_cdp:
                # Borrowed the user's Chrome — do NOT close it or save state
                logger.info("CDP mode: leaving your Chrome open")
            else:
                # We launched our own browser — clean up
                if self._context:
                    if exc_type is None:
                        state_path = self.settings.session_dir.parent / "naukri_state.json"
                        await self._context.storage_state(path=str(state_path))
                        logger.info(f"Saved browser session to {state_path}")
                    else:
                        logger.info("Skipping session save due to error")
                    await self._context.close()
                if self._browser:
                    await self._browser.close()
        finally:
            await self._playwright.stop()

    # --- helpers ---------------------------------------------------------

    async def _on_chrome_error_page(self) -> bool:
        """Return True if the current page is Chrome's internal error page."""
        return self._page.url.lower().startswith("chrome-error://")

    async def _jitter(self) -> None:
        cfg = self.settings.raw.get("playwright", {})
        lo = int(cfg.get("action_jitter_min_ms", 2000))
        hi = int(cfg.get("action_jitter_max_ms", 5000))
        await asyncio.sleep(random.uniform(lo, hi) / 1000)

    async def _safe_goto(
        self, url: str, *, timeout: int = 30000, retries: int = 2,
    ) -> bool:
        """Navigate to *url* and return True if we land on a real page (not
        chrome-error://).  Retries once if an error page appears."""
        for attempt in range(1, retries + 1):
            try:
                await self._page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                await asyncio.sleep(1)
                if not await self._on_chrome_error_page():
                    return True
                logger.debug("chrome-error page after goto (attempt %d/%d)", attempt, retries)
            except Exception as exc:
                logger.debug("goto %s failed (attempt %d/%d): %s", url, attempt, retries, exc)
        return False

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

        # --- Step 0: If we're already on Resdex (CDP mode), skip everything ---
        current_url = self._page.url.lower()
        if "resdex.naukri.com" in current_url and "login" not in current_url:
            logger.info("Already on Resdex — no login needed!")
            return

        # --- Step 1: Check session via the public homepage (safe) ---
        # Do NOT go directly to resdex — Naukri's servers reject
        # unauthenticated requests to it with ERR_HTTP2_PROTOCOL_ERROR.
        logger.info("Checking if already logged in...")
        session_valid = False
        ok = await self._safe_goto("https://www.naukri.com", timeout=15000)
        if ok:
            await asyncio.sleep(2)
            current_url = self._page.url.lower()
            # If we land on a page that is NOT the login page, session is likely valid
            if "naukri.com" in current_url and "login" not in current_url:
                # Now try accessing Resdex to confirm recruiter access
                ok2 = await self._safe_goto(resdex_url, timeout=15000)
                if ok2:
                    await asyncio.sleep(2)
                    if "resdex.naukri.com" in self._page.url.lower() and "login" not in self._page.url.lower():
                        logger.info("Already logged in with Resdex access! Reusing saved session.")
                        session_valid = True

        if session_valid:
            return

        # --- Step 2: Navigate to login page ---
        logger.warning("Not logged in. Navigating to login page...")
        ok = await self._safe_goto(login_url, timeout=20000)
        if not ok:
            # Navigation failed outright (chrome-error), likely a network / CDN issue.
            print("\n" + "!" * 80)
            print("!!! BROWSER NAVIGATION FAILED !!!")
            print("The browser cannot reach Naukri.com.")
            print("")
            print("Possible fixes (try in order):")
            print("  1. Use your real Chrome session (recommended):")
            print("     - Open chrome://inspect/#remote-debugging in Chrome")
            print("     - Enable \"Discover network targets\"")
            print("     - Re-run the bot — it will reuse your signed-in session.")
            print("")
            print("  2. Check if your network/firewall blocks Naukri.com.")
            print("")
            print("  3. Set HEADLESS=false in config/.env to see what the")
            print("     browser shows on screen.")
            print("!" * 80 + "\n")
            raise RuntimeError(
                "Cannot navigate to Naukri.com — the browser shows a "
                "chrome-error page.  See instructions above."
            )

        await self._jitter()

        # The recruiter login page first shows a "Register/Log in" button.
        # Click it to reveal the actual login form.
        try:
            reg_btn = self._page.locator(SELECTORS["register_login_btn"])
            if await reg_btn.is_visible(timeout=5000):
                await reg_btn.click()
                await asyncio.sleep(3)
                await self._jitter()
                logger.info("Clicked 'Register/Log in' button to show login form")
        except Exception as exc:
            logger.debug("Register/Log in button not found (already on login form?): %s", exc)

        try:
            # Wait for the email input to actually appear
            email_input = self._page.locator(SELECTORS["login_username"])
            await email_input.wait_for(state="visible", timeout=10000)
            # Auto-fill the email and password to save the recruiter time
            await email_input.fill(self.settings.naukri_username)
            await self._page.fill(SELECTORS["login_password"], self.settings.naukri_password)
            logger.info("Filled email and password")
        except Exception as exc:
            logger.debug("Could not auto-fill login: %s", exc)

        # Click the "Log in" button to trigger OTP send
        try:
            login_btn = self._page.locator(SELECTORS["login_submit"])
            if await login_btn.is_visible(timeout=5000):
                await login_btn.click()
                logger.info("Clicked 'Log in' button — OTP should be sent")
                await self._jitter()
        except Exception as exc:
            logger.debug("Could not click Log in button: %s", exc)

        print("\n" + "!"*80)
        print("!!! ACTION REQUIRED: PLEASE LOG IN TO NAUKRI IN THE BROWSER WINDOW !!!")
        print("1. Enter the OTP from your phone.")
        print("2. Wait until you see the Naukri dashboard.")
        print("3. Come back to this terminal and press [ENTER] to continue!")
        print("!"*80 + "\n")

        # Pause the script and wait for the user to press Enter
        await asyncio.to_thread(input, "Press ENTER when you are successfully logged in: ")

        logger.info("Login complete. You can keep the dashboard tab open.")

    async def _ensure_page(self) -> bool:
        """If the current page was closed, try to re-acquire a Naukri page.
        Returns True if we have a usable page."""
        try:
            url = self._page.url
            return True  # page is still alive
        except Exception:
            pass
        # Page is dead — try to find a replacement in CDP mode
        if self._connected_via_cdp and self._browser:
            for ctx in self._browser.contexts:
                for p in ctx.pages:
                    url = p.url.lower()
                    if "naukri.com" in url or "resdex.naukri.com" in url:
                        self._context = ctx
                        self._page = p
                        logger.info("Re-acquired Naukri page: %s", p.url)
                        return True
        return False

    async def search(self, boolean_query: str) -> AsyncIterator[Candidate]:
        """Yield Candidate objects. Applies HardFilter BEFORE downloading."""
        # Navigate to Resdex first (in case we're still on dashboard)
        resdex_url = self.settings.raw.get("naukri", {}).get(
            "resdex_url", "https://resdex.naukri.com/v3/search"
        )
        if not await self._ensure_page():
            raise RuntimeError("Naukri page was closed. Please re-run with a Naukri tab open.")
        logger.info("Navigating to Resdex search...")
        ok = await self._safe_goto(resdex_url, timeout=20000)
        if not ok:
            raise RuntimeError("Could not navigate to Resdex. Are you logged in?")
        await asyncio.sleep(3)

        await self._fill_filters(boolean_query)
        await self._page.click(SELECTORS["search_button"])
        await asyncio.sleep(3)
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
                await asyncio.sleep(4)
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
