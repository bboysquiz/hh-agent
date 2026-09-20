from __future__ import annotations

"""Replay fallback for old discovered vacancies.

For vacancies that already exist in the local DB and are being deliberately
reprocessed, browser-side HH markup may no longer match the selectors used by
the normal workflow. In that narrow case we fall back to the official public
HH API to recover the vacancy description/company/metadata and let the normal
analysis pipeline continue.

Fresh vacancies keep the original browser behaviour, including the check that
an application response control is available.
"""

import html
import logging
import re
from typing import Any

import httpx

from database import VacancyStatus
from hh_client import (
    HHClient,
    PageState,
    VacancyDetails,
    VacancySummary,
    _company_url,
    _location_work_format_rejection,
    _parse_work_formats,
)

logger = logging.getLogger(__name__)

_ORIGINAL_READ_VACANCY = HHClient.read_vacancy
_PATCHED = False

_REPLAY_FALLBACK_STATES = {
    PageState.PAGE_STRUCTURE_CHANGED,
    PageState.RESPONSE_UNAVAILABLE,
    PageState.VACANCY_REMOVED,
    PageState.NETWORK_ERROR,
}


def _html_to_text(value: str) -> str:
    if not value:
        return ""

    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</(?:p|div|li|ul|ol|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)

    lines = []
    for line in text.splitlines():
        cleaned = " ".join(line.split())
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def _api_metadata(payload: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    address = payload.get("address")
    area = payload.get("area")

    address_city = (
        str(address.get("city") or "").strip()
        if isinstance(address, dict)
        else ""
    )
    area_name = (
        str(area.get("name") or "").strip()
        if isinstance(area, dict)
        else ""
    )
    location = address_city or area_name

    formats: set[str] = set()
    raw_formats = payload.get("work_format")
    if isinstance(raw_formats, list):
        for item in raw_formats:
            if not isinstance(item, dict):
                continue
            format_id = str(item.get("id") or "").strip().upper()
            format_name = str(item.get("name") or "").strip()
            if format_id in {"REMOTE", "HYBRID", "ON_SITE", "FIELD_WORK"}:
                formats.add(format_id)
            formats.update(_parse_work_formats(format_name))

    schedule = payload.get("schedule")
    if isinstance(schedule, dict):
        schedule_id = str(schedule.get("id") or "").strip().casefold()
        schedule_name = str(schedule.get("name") or "").strip()
        if schedule_id == "remote":
            formats.add("REMOTE")
        formats.update(_parse_work_formats(schedule_name))

    return location, tuple(sorted(formats))


async def _read_api_replay(
    self: HHClient,
    summary: VacancySummary,
) -> VacancyDetails | None:
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=True,
            headers={"HH-User-Agent": "hh-ai-agent-local/1.0"},
        ) as client:
            response = await client.get(
                f"https://api.hh.ru/vacancies/{summary.id}"
            )
    except Exception as exc:
        logger.warning(
            "vacancy_replay_api_failed job_id=%s error_type=%s",
            summary.id,
            type(exc).__name__,
        )
        return None

    if response.status_code != 200:
        logger.info(
            "vacancy_replay_api_unavailable job_id=%s status=%s",
            summary.id,
            response.status_code,
        )
        return None

    try:
        payload = response.json()
    except Exception as exc:
        logger.warning(
            "vacancy_replay_api_invalid_json job_id=%s error_type=%s",
            summary.id,
            type(exc).__name__,
        )
        return None

    if not isinstance(payload, dict):
        return None

    description = _html_to_text(str(payload.get("description") or ""))
    if not description:
        logger.info(
            "vacancy_replay_api_empty_description job_id=%s",
            summary.id,
        )
        return None

    location, work_formats = _api_metadata(payload)
    rejection = _location_work_format_rejection(location, work_formats)
    if rejection:
        return VacancyDetails(
            summary,
            PageState.RESPONSE_UNAVAILABLE,
            error=rejection,
            location=location,
            work_formats=work_formats,
        )

    metadata_lines: list[str] = []
    if location:
        metadata_lines.append(f"Локация HH: {location}")
    if work_formats:
        metadata_lines.append("Формат работы HH: " + ", ".join(work_formats))
    if metadata_lines:
        description = (
            "СТРУКТУРИРОВАННЫЕ ДАННЫЕ HH:\n"
            + "\n".join(metadata_lines)
            + "\n\nОПИСАНИЕ ВАКАНСИИ:\n"
            + description
        )

    employer = payload.get("employer")
    company = ""
    company_url = ""
    if isinstance(employer, dict):
        company = str(employer.get("name") or "").strip()
        raw_company_url = str(
            employer.get("alternate_url")
            or employer.get("url")
            or ""
        ).strip()
        company_url = _company_url(summary.url, raw_company_url)

    archived = bool(payload.get("archived"))
    logger.info(
        "vacancy_replay_api_loaded job_id=%s archived=%s company=%r",
        summary.id,
        archived,
        company,
    )

    return VacancyDetails(
        summary,
        PageState.VACANCY_LOADED,
        company=company,
        description=description,
        error="api_replay_archived" if archived else "api_replay",
        company_url=company_url,
        location=location,
        work_formats=work_formats,
    )


async def _read_vacancy_with_replay_fallback(
    self: HHClient,
    summary: VacancySummary,
    captcha_solver=None,
) -> VacancyDetails:
    result = await _ORIGINAL_READ_VACANCY(self, summary, captcha_solver)

    existing = self.database.get(summary.id)
    if (
        existing is None
        or existing.status is not VacancyStatus.DISCOVERED
        or existing.llm_decision is not None
    ):
        return result

    if result.state not in _REPLAY_FALLBACK_STATES:
        return result

    if (
        result.state is PageState.RESPONSE_UNAVAILABLE
        and result.error.startswith("location_work_format_mismatch:")
    ):
        return result

    fallback = await _read_api_replay(self, summary)
    if fallback is None:
        return result

    logger.info(
        "vacancy_replay_fallback_used job_id=%s original_state=%s",
        summary.id,
        result.state.value,
    )
    return fallback


def install_hh_replay_fallback() -> None:
    global _PATCHED
    if _PATCHED:
        return
    HHClient.read_vacancy = _read_vacancy_with_replay_fallback  # type: ignore[method-assign]
    _PATCHED = True


install_hh_replay_fallback()
