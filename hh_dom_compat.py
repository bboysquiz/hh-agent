from __future__ import annotations

"""DOM compatibility fallback for replaying old HH vacancies.

The regular HH client intentionally uses strict selectors for the live
application workflow. During a deliberate replay of vacancies that already
exist in the local DB, a page can be rendered with a different/late DOM and
the strict classifier may return PAGE_STRUCTURE_CHANGED.

This patch only relaxes reading for existing DISCOVERED vacancies with no LLM
decision yet. Fresh vacancies keep the stock behaviour.
"""

import logging
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
    """Wait for late client-side rendering without requiring one exact selector."""
    selector = ", ".join(DESCRIPTION_SELECTORS + TITLE_SELECTORS)
    try:
        await page.locator(selector).first.wait_for(
            state="visible",
            timeout=timeout_ms,
        )
    except Exception:
        # Diagnostics below will decide whether the page is usable.
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
            )).slice(0, 80)"""
        )
        if isinstance(values, list):
            data_qa = tuple(str(value) for value in values if value)
    except Exception:
        pass

    return url, title, data_qa


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

        description_locator, description_selector = await _first_visible(
            page,
            DESCRIPTION_SELECTORS,
        )
        if description_locator is None:
            url, page_title, data_qa = await _page_diagnostics(page)
            logger.error(
                "hh_dom_unknown job_id=%s url=%r title=%r data_qa=%s",
                summary.id,
                url,
                page_title,
                ",".join(data_qa),
            )
            return None

        try:
            description = (await description_locator.inner_text()).strip()
        except Exception:
            description = ""

        if not description:
            url, page_title, data_qa = await _page_diagnostics(page)
            logger.error(
                "hh_dom_empty_description job_id=%s selector=%s url=%r "
                "title=%r data_qa=%s",
                summary.id,
                description_selector,
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
                error=rejection,
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

        company_locator, company_selector = await _first_visible(
            page,
            COMPANY_SELECTORS,
        )
        company = ""
        company_url = ""
        if company_locator is not None:
            try:
                company = (await company_locator.inner_text()).strip()
            except Exception:
                company = ""
            try:
                href = await company_locator.get_attribute("href")
            except Exception:
                href = None
            if href:
                company_url = hh._company_url(
                    summary.url,
                    urljoin(summary.url, href),
                )

        logger.info(
            "hh_dom_compat_loaded job_id=%s description_selector=%s "
            "company_selector=%s company=%r",
            summary.id,
            description_selector,
            company_selector or "none",
            company,
        )

        return hh.VacancyDetails(
            summary,
            hh.PageState.VACANCY_LOADED,
            company=company,
            description=description,
            error="compat_dom_replay",
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
        "hh_dom_compat_fallback_used job_id=%s original_state=%s",
        summary.id,
        result.state.value,
    )
    return fallback


def install_hh_dom_compat() -> None:
    global _PATCHED
    if _PATCHED:
        return
    hh.HHClient.read_vacancy = _read_vacancy_with_dom_compat  # type: ignore[method-assign]
    _PATCHED = True


install_hh_dom_compat()
