from __future__ import annotations

"""DOM compatibility fallback for replaying old HH vacancies.

Fresh vacancies keep the strict browser workflow. Existing DISCOVERED rows
that are intentionally being replayed get a compatibility reader which knows
about HH's current active and archived vacancy layouts.
"""

import html as html_lib
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin

from database import VacancyStatus
import hh_client as hh

logger = logging.getLogger(__name__)

_ORIGINAL_READ_VACANCY = hh.HHClient.read_vacancy
_PATCHED = False

DESCRIPTION_SELECTORS = (
    '[data-qa="vacancy-description"]',
    '[data-qa*="vacancy-description"]',
    'div[class*="vacancy-description"]',
    'section[class*="vacancy-description"]',
    'main .g-user-content',
)

TITLE_SELECTORS = (
    '[data-qa="vacancy-title"]',
    '[data-qa*="vacancy-title"]',
    'h1',
)

COMPANY_SELECTORS = (
    '[data-qa="vacancy-company-name"]',
    '[data-qa*="vacancy-company-name"]',
    'a[href*="/employer/"]',
)

ARCHIVED_SELECTORS = (
    '[data-qa="vacancy-title-archived-text"]',
    '[data-qa="vacancy-archive-description"]',
)

CAPTCHA_SELECTORS = (
    'form[action*="captcha"]',
    '[data-qa="captcha"]',
    '[data-qa*="captcha"]',
)

ACCESS_DENIED_SELECTORS = (
    '[data-qa="access-denied"]',
    '[data-qa*="access-denied"]',
)


async def _first_visible(page: Any, selectors: tuple[str, ...]) -> tuple[Any | None, str]:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible():
                return locator, selector
        except Exception:
            continue
    return None, ""


async def _wait_for_vacancy_dom(page: Any, timeout_ms: int = 8_000) -> None:
    """Wait for either an active or an archived vacancy shell."""
    selector = ", ".join(
        DESCRIPTION_SELECTORS + TITLE_SELECTORS + ARCHIVED_SELECTORS
    )
    try:
        await page.locator(selector).first.wait_for(
            state="visible",
            timeout=timeout_ms,
        )
    except Exception:
        return


async def _page_diagnostics(page: Any) -> tuple[str, str, tuple[str, ...]]:
    url = str(getattr(page, "url", "") or "")
    try:
        title = str(await page.title())
    except Exception:
        title = ""

    data_qa: tuple[str, ...] = ()
    try:
        values = await page.locator("[data-qa]").evaluate_all(
            """els => Array.from(new Set(
                els.map(el => el.getAttribute('data-qa')).filter(Boolean)
            )).slice(0, 100)"""
        )
        if isinstance(values, list):
            data_qa = tuple(str(value) for value in values if value)
    except Exception:
        pass

    return url, title, data_qa


def _html_to_text(value: str) -> str:
    if not value:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</(?:p|div|li|ul|ol|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    lines: list[str] = []
    for line in text.splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _job_posting_from_json(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        raw_type = value.get("@type")
        if raw_type == "JobPosting" or (
            isinstance(raw_type, list) and "JobPosting" in raw_type
        ):
            return value
        for child in value.values():
            found = _job_posting_from_json(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _job_posting_from_json(child)
            if found is not None:
                return found
    return None


async def _json_ld_job_posting(page: Any) -> dict[str, Any] | None:
    """Recover JobPosting data when HH no longer renders the description node."""
    try:
        scripts = await page.locator('script[type="application/ld+json"]').evaluate_all(
            "els => els.map(el => el.textContent || '')"
        )
    except Exception:
        return None

    if not isinstance(scripts, list):
        return None

    for raw in scripts:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        found = _job_posting_from_json(payload)
        if found is not None:
            return found
    return None


def _company_from_job_posting(posting: dict[str, Any] | None) -> str:
    if not posting:
        return ""
    organization = posting.get("hiringOrganization")
    if isinstance(organization, dict):
        return str(organization.get("name") or "").strip()
    return ""


async def _read_company(page: Any, summary: hh.VacancySummary) -> tuple[str, str, str]:
    locator, selector = await _first_visible(page, COMPANY_SELECTORS)
    if locator is None:
        return "", "", ""

    try:
        company = (await locator.inner_text()).strip()
    except Exception:
        company = ""

    try:
        href = await locator.get_attribute("href")
    except Exception:
        href = None

    company_url = ""
    if href:
        company_url = hh._company_url(
            summary.url,
            urljoin(summary.url, href),
        )

    return company, company_url, selector


async def _read_existing_vacancy_from_compat_dom(
    self: hh.HHClient,
    summary: hh.VacancySummary,
) -> hh.VacancyDetails | None:
    page = None
    try:
        page = await self.context.new_page()
        await self._delay()
        await page.goto(
            summary.url,
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await _wait_for_vacancy_dom(page)

        captcha, _ = await _first_visible(page, CAPTCHA_SELECTORS)
        if captcha is not None:
            return hh.VacancyDetails(
                summary,
                hh.PageState.CAPTCHA_DETECTED,
                error="captcha_detected_by_compat_dom",
            )

        denied, _ = await _first_visible(page, ACCESS_DENIED_SELECTORS)
        if denied is not None:
            return hh.VacancyDetails(
                summary,
                hh.PageState.ACCESS_DENIED,
                error="access_denied_by_compat_dom",
            )

        archived_locator, archived_selector = await _first_visible(
            page,
            ARCHIVED_SELECTORS,
        )
        is_archived = archived_locator is not None

        description_locator, description_selector = await _first_visible(
            page,
            DESCRIPTION_SELECTORS,
        )

        description = ""
        if description_locator is not None:
            try:
                description = (await description_locator.inner_text()).strip()
            except Exception:
                description = ""

        posting: dict[str, Any] | None = None
        if not description:
            posting = await _json_ld_job_posting(page)
            if posting is not None:
                description = _html_to_text(
                    str(posting.get("description") or "")
                )
                if description:
                    description_selector = "json-ld:JobPosting.description"

        company, company_url, company_selector = await _read_company(
            page,
            summary,
        )
        if not company:
            company = _company_from_job_posting(posting)
            if company:
                company_selector = "json-ld:JobPosting.hiringOrganization"

        if not description:
            url, page_title, data_qa = await _page_diagnostics(page)

            if is_archived:
                try:
                    archive_text = (await archived_locator.inner_text()).strip()
                except Exception:
                    archive_text = ""
                logger.info(
                    "hh_archived_description_unavailable job_id=%s selector=%s "
                    "archive_text=%r url=%r title=%r company=%r",
                    summary.id,
                    archived_selector,
                    archive_text,
                    url,
                    page_title,
                    company,
                )
                # This is a known current HH layout, not a structure failure.
                # Do not open the page-structure circuit breaker merely because
                # HH has removed the full body of an archived vacancy.
                return hh.VacancyDetails(
                    summary,
                    hh.PageState.VACANCY_REMOVED,
                    company=company,
                    error="archived_description_unavailable",
                    company_url=company_url,
                )

            logger.error(
                "hh_dom_unknown job_id=%s url=%r title=%r data_qa=%s",
                summary.id,
                url,
                page_title,
                ",".join(data_qa),
            )
            return None

        metadata = await self._read_vacancy_metadata(page, summary)
        rejection = hh._location_work_format_rejection(
            metadata.location,
            metadata.work_formats,
        )
        if rejection:
            return hh.VacancyDetails(
                summary,
                hh.PageState.RESPONSE_UNAVAILABLE,
                company=company,
                error=rejection,
                company_url=company_url,
                location=metadata.location,
                work_formats=metadata.work_formats,
            )

        metadata_lines: list[str] = []
        if metadata.location:
            metadata_lines.append(f"Локация HH: {metadata.location}")
        if metadata.work_formats:
            metadata_lines.append(
                "Формат работы HH: " + ", ".join(metadata.work_formats)
            )
        if metadata_lines:
            description = (
                "СТРУКТУРИРОВАННЫЕ ДАННЫЕ HH:\n"
                + "\n".join(metadata_lines)
                + "\n\nОПИСАНИЕ ВАКАНСИИ:\n"
                + description
            )

        logger.info(
            "hh_dom_compat_loaded job_id=%s archived=%s "
            "description_source=%s company_source=%s company=%r",
            summary.id,
            is_archived,
            description_selector or "unknown",
            company_selector or "none",
            company,
        )

        return hh.VacancyDetails(
            summary,
            hh.PageState.VACANCY_LOADED,
            company=company,
            description=description,
            error=("compat_dom_archived_replay" if is_archived else "compat_dom_replay"),
            company_url=company_url,
            location=metadata.location,
            work_formats=metadata.work_formats,
        )
    except Exception as exc:
        logger.warning(
            "hh_dom_compat_failed job_id=%s error_type=%s error=%s",
            summary.id,
            type(exc).__name__,
            exc,
        )
        return None
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def _read_vacancy_with_dom_compat(
    self: hh.HHClient,
    summary: hh.VacancySummary,
    captcha_solver=None,
) -> hh.VacancyDetails:
    result = await _ORIGINAL_READ_VACANCY(self, summary, captcha_solver)

    if result.state is not hh.PageState.PAGE_STRUCTURE_CHANGED:
        return result

    existing = self.database.get(summary.id)
    if (
        existing is None
        or existing.status is not VacancyStatus.DISCOVERED
        or existing.llm_decision is not None
    ):
        return result

    fallback = await _read_existing_vacancy_from_compat_dom(self, summary)
    if fallback is None:
        return result

    logger.info(
        "hh_dom_compat_fallback_used job_id=%s original_state=%s new_state=%s",
        summary.id,
        result.state.value,
        fallback.state.value,
    )
    return fallback


def install_hh_dom_compat() -> None:
    global _PATCHED
    if _PATCHED:
        return
    hh.HHClient.read_vacancy = _read_vacancy_with_dom_compat  # type: ignore[method-assign]
    _PATCHED = True


install_hh_dom_compat()
