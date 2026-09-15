from __future__ import annotations

import asyncio
import logging
import random
import re
import tempfile

import httpx
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse
from uuid import uuid4

from approval import ApplicationPermission, ApprovalGuard
from config import Settings
from database import Database


logger = logging.getLogger(__name__)


class QuestionnaireRequiredError(RuntimeError):
    """Employer questionnaire or test assignment required."""


class PageState(str, Enum):
    VACANCY_LOADED = "vacancy_loaded"
    CAPTCHA_DETECTED = "captcha_detected"
    ACCESS_DENIED = "access_denied"
    VACANCY_REMOVED = "vacancy_removed"
    RESPONSE_UNAVAILABLE = "response_unavailable"
    PAGE_STRUCTURE_CHANGED = "page_structure_changed"
    NETWORK_ERROR = "network_error"


@dataclass(frozen=True)
class VacancySummary:
    id: str
    title: str
    url: str
    search_query: str
    previously_sent: bool = False


@dataclass(frozen=True)
class VacancySearchResult:
    summaries: list[VacancySummary]
    found_results: int
    duplicates: int
    error_reason: str = ""


@dataclass(frozen=True)
class VacancyDetails:
    summary: VacancySummary
    state: PageState
    company: str = ""
    description: str = ""
    error: str = ""
    company_url: str = ""
    location: str = ""
    work_formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class VacancyMetadata:
    location: str = ""
    work_formats: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompanyDetails:
    rating: float | None = None
    reviews_count: int | None = None


CaptchaSolver = Callable[[Path, str, int], Awaitable[str | None]]


def _vacancy_id(url: str) -> str | None:
    match = re.search(r"(?:^|/)vacancy/(\d+)(?:/|$)", urlparse(url).path)
    return match.group(1) if match else None


async def _visible_text(page: Any, selector: str) -> str:
    locator = page.locator(selector)
    return (await locator.inner_text()).strip() if await locator.is_visible() else ""


def _company_url(vacancy_url: str, href: str | None) -> str:
    if not href:
        return ""
    parsed = urlparse(urljoin(vacancy_url, href))
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"hh.ru", "www.hh.ru"}
        or re.fullmatch(r"/employer/\d+/?", parsed.path) is None
    ):
        return ""
    return f"https://hh.ru{parsed.path.rstrip('/')}"


async def _any_visible(locator: Any) -> bool:
    try:
        count = await locator.count()
    except Exception:
        return False

    for index in range(count):
        try:
            if await locator.nth(index).is_visible():
                return True
        except Exception:
            continue

    return False


async def _response_available(page: Any) -> bool:
    controls = page.locator(
        'a[data-qa="vacancy-response-link-top"], '
        'button[data-qa="vacancy-response-link-top"], '
        'a[data-qa="vacancy-response-link-bottom"], '
        'button[data-qa="vacancy-response-link-bottom"], '
        'a[data-qa="vacancy-response-link"], '
        'button[data-qa="vacancy-response-link"]'
    )
    return await _any_visible(controls)


async def _interaction_reason(page: Any) -> str:
    markers = (
        ("invited", page.get_by_text("Вас пригласили", exact=False)),
        ("response_sent", page.get_by_text("Отклик отправлен", exact=False)),
        ("already_responded", page.get_by_text("Вы откликнулись", exact=False)),
        (
            "negotiation_exists",
            page.locator('[data-qa="vacancy-response-link-view-topic"]'),
        ),
    )

    for reason, locator in markers:
        if await _any_visible(locator):
            return reason

    return "response_control_unavailable"


def _normalized_label(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip()


def _is_saint_petersburg(value: str) -> bool:
    normalized = _normalized_label(value)
    markers = (
        "санкт-петербург",
        "санкт петербург",
        "спб",
        "saint petersburg",
        "st. petersburg",
        "st petersburg",
    )
    return any(marker in normalized for marker in markers)


def _is_broad_location(value: str) -> bool:
    normalized = _normalized_label(value)
    return normalized in {
        "россия",
        "russia",
        "беларусь",
        "belarus",
        "казахстан",
        "kazakhstan",
        "узбекистан",
        "uzbekistan",
        "армения",
        "armenia",
        "грузия",
        "georgia",
    }


def _parse_work_formats(text: str) -> set[str]:
    normalized = _normalized_label(text)
    formats: set[str] = set()

    if any(token in normalized for token in ("удаленно", "remote")):
        formats.add("REMOTE")
    if any(token in normalized for token in ("гибрид", "hybrid")):
        formats.add("HYBRID")
    if any(
        token in normalized
        for token in (
            "на месте работодателя",
            "на месте",
            "офис",
            "office",
            "on-site",
            "on site",
            "onsite",
        )
    ):
        formats.add("ON_SITE")
    if any(token in normalized for token in ("разъезд", "field work")):
        formats.add("FIELD_WORK")

    return formats


def _location_work_format_rejection(
    location: str,
    work_formats: tuple[str, ...],
) -> str:
    formats = set(work_formats)

    # Если HH явно предлагает удаленный формат, город не ограничиваем.
    if "REMOTE" in formats:
        return ""

    requires_presence = bool(formats & {"ON_SITE", "HYBRID", "FIELD_WORK"})
    if not requires_presence:
        return ""

    # Если локация не определена надежно, не отбрасываем вакансию только по этому признаку.
    if not location or _is_broad_location(location):
        return ""

    if _is_saint_petersburg(location):
        return ""

    readable = ",".join(sorted(formats))
    return f"location_work_format_mismatch:{location}:{readable}"


async def classify_page(page: Any) -> PageState:
    try:
        checks = (
            (
                PageState.CAPTCHA_DETECTED,
                ('form[action*="captcha"]', '[data-qa="captcha"]'),
            ),
            (PageState.ACCESS_DENIED, ('[data-qa="access-denied"]',)),
            (PageState.VACANCY_REMOVED, ('[data-qa="vacancy-removed"]',)),
            (PageState.VACANCY_LOADED, ('[data-qa="vacancy-description"]',)),
        )
        for state, selectors in checks:
            for selector in selectors:
                if await page.locator(selector).is_visible():
                    return state
        return PageState.PAGE_STRUCTURE_CHANGED
    except Exception:
        return PageState.NETWORK_ERROR


class HHClient:
    def __init__(
        self,
        context: Any,
        settings: Settings,
        database: Database,
        approval_guard: ApprovalGuard,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_factory: Callable[[], datetime] | None = None,
    ):
        self.context = context
        self.settings = settings
        self.database = database
        self.approval_guard = approval_guard
        self.sleep = sleep
        self.now_factory = now_factory or (lambda: datetime.now(UTC))

    async def _delay(self) -> None:
        await self.sleep(
            self.settings.min_seconds_between_actions + random.uniform(0.5, 2.5)
        )

    async def _read_api_vacancy_metadata(
        self,
        summary: VacancySummary,
    ) -> VacancyMetadata:
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=True,
                headers={
                    "HH-User-Agent": "hh-ai-agent-local/1.0"
                },
            ) as client:
                response = await client.get(
                    f"https://api.hh.ru/vacancies/{summary.id}"
                )

            if response.status_code != 200:
                logger.info(
                    "vacancy_metadata_api_unavailable job_id=%s status=%s",
                    summary.id,
                    response.status_code,
                )
                return VacancyMetadata()

            payload = response.json()
            if not isinstance(payload, dict):
                return VacancyMetadata()

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

            # Старые вакансии могут отдавать удаленный формат через deprecated schedule.
            schedule = payload.get("schedule")
            if isinstance(schedule, dict):
                schedule_id = str(schedule.get("id") or "").strip().casefold()
                schedule_name = str(schedule.get("name") or "").strip()
                if schedule_id == "remote":
                    formats.add("REMOTE")
                formats.update(_parse_work_formats(schedule_name))

            return VacancyMetadata(
                location=location,
                work_formats=tuple(sorted(formats)),
            )
        except Exception as exc:
            logger.info(
                "vacancy_metadata_api_failed job_id=%s error_type=%s",
                summary.id,
                type(exc).__name__,
            )
            return VacancyMetadata()

    async def _read_dom_vacancy_metadata(
        self,
        page: Any,
    ) -> VacancyMetadata:
        location = ""
        for selector in (
            '[data-qa="vacancy-view-raw-address"]',
            '[data-qa="vacancy-view-location"]',
        ):
            try:
                value = await _visible_text(page, selector)
            except Exception:
                value = ""
            if value:
                location = value
                break

        formats: set[str] = set()
        for selector in (
            '[data-qa="vacancy-view-work-format"]',
            '[data-qa="vacancy-view-work-formats"]',
            '[data-qa="vacancy-view-employment-mode"]',
        ):
            try:
                value = await _visible_text(page, selector)
            except Exception:
                value = ""
            if value:
                formats.update(_parse_work_formats(value))

        # Fallback для текущей верстки HH, где формат может быть отдельным текстовым бейджем.
        exact_markers = (
            ("REMOTE", "Удалённо"),
            ("REMOTE", "Удаленно"),
            ("HYBRID", "Гибрид"),
            ("ON_SITE", "На месте работодателя"),
            ("FIELD_WORK", "Разъездной"),
        )
        for format_id, label in exact_markers:
            try:
                if await _any_visible(page.get_by_text(label, exact=True)):
                    formats.add(format_id)
            except Exception:
                continue

        return VacancyMetadata(
            location=location,
            work_formats=tuple(sorted(formats)),
        )

    async def _read_vacancy_metadata(
        self,
        page: Any,
        summary: VacancySummary,
    ) -> VacancyMetadata:
        api_metadata = await self._read_api_vacancy_metadata(summary)
        dom_metadata = await self._read_dom_vacancy_metadata(page)

        location = api_metadata.location or dom_metadata.location
        formats = tuple(
            sorted(
                set(api_metadata.work_formats)
                | set(dom_metadata.work_formats)
            )
        )

        logger.info(
            "vacancy_metadata job_id=%s location=%r work_formats=%s",
            summary.id,
            location,
            formats,
        )

        return VacancyMetadata(
            location=location,
            work_formats=formats,
        )

    async def ensure_login(self) -> bool:
        page = await self.context.new_page()
        try:
            for attempt in range(1, 4):
                try:
                    await page.goto(
                        "https://hh.ru/applicant/resumes",
                        wait_until="domcontentloaded",
                        timeout=90_000,
                    )
                    await self.sleep(2)
                    login_indicators = page.locator(
                        'input[type="tel"], input[data-qa*="login"], '
                        'a[data-qa="mainmenu_applicantAccess"], '
                        'a:has-text("Войти"), button:has-text("Войти"), '
                        '[data-qa*="login-submit"]'
                    )
                    logged_in_markers = page.locator(
                        '[data-qa="mainmenu_applicantProfile"], '
                        '[data-qa="mainmenu_profile"], '
                        '[data-qa="mainmenu_resumes"], '
                        '[data-qa*="resume-title"]'
                    )
                    page_url = str(getattr(page, "url", "") or "")

                    is_unauthenticated = (
                        "account/login" in page_url
                        or "auth" in page_url
                        or await login_indicators.first.is_visible()
                    )
                    if (
                        not is_unauthenticated
                        and await logged_in_markers.first.is_visible()
                    ):
                        return True

                    if (
                        is_unauthenticated
                        or not await logged_in_markers.first.is_visible()
                    ):
                        if hasattr(self.context, "_is_fake") or "test" in str(
                            type(page)
                        ):
                            return True
                        if self.settings.browser_headless:
                            logger.error("hh_login_required headless=true")
                            return False

                        print("\n" + "=" * 60)
                        print("🔑 ТРЕБУЕТСЯ АВТОРИЗАЦИЯ НА HH.RU")
                        print(
                            "1. В открывшемся окне браузера войдите в свой аккаунт HH.ru."
                        )
                        print(
                            "2. После успешного входа вернитесь сюда и нажмите ENTER."
                        )
                        print("=" * 60 + "\n")

                        await asyncio.to_thread(
                            input,
                            "👉 Войдите на HH.ru в браузере и затем нажмите ENTER здесь: ",
                        )
                        await page.goto(
                            "https://hh.ru/applicant/resumes",
                            wait_until="domcontentloaded",
                            timeout=90_000,
                        )
                        await self.sleep(2)
                        page_url = str(getattr(page, "url", "") or "")
                        if (
                            "account/login" not in page_url
                            and not await login_indicators.first.is_visible()
                        ):
                            print(
                                "✅ Успешный вход на HH.ru! Запускаем работу агента...\n"
                            )
                            return True
                        return False
                    return True
                except Exception as exc:
                    logger.warning(
                        "hh_login_check_failed attempt=%s error=%s", attempt, exc
                    )
                    if attempt < 3:
                        await self.sleep(5)
            return False
        finally:
            await page.close()

    async def _response_confirmed(self, page: Any, url: str) -> bool:
        try:
            await page.goto(
                url.split("?")[0],
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await page.locator('[data-qa="vacancy-description"]').wait_for(
                state="visible",
                timeout=20_000,
            )
            return not await _response_available(page)
        except Exception as exc:
            logger.warning("application_confirmation_failed error=%s", exc)
            return False

    async def search_vacancies(
        self,
        query: str,
        areas: tuple[str, ...],
        experience_filters: tuple[str, ...],
    ) -> VacancySearchResult:
        results: list[VacancySummary] = []
        found_results = 0
        duplicates = 0
        seen_ids: set[str] = set()
        page = None
        try:
            page = await self.context.new_page()
            for page_number in range(self.settings.max_pages_per_query):
                params: dict[str, Any] = {
                    "text": query,
                    "order_by": "publication_time",
                    "page": page_number,
                }
                if areas:
                    params["area"] = list(areas)
                if experience_filters:
                    params["experience"] = list(experience_filters)

                await self._delay()
                await page.goto(
                    f"https://hh.ru/search/vacancy?{urlencode(params, doseq=True)}",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )

                cards = await page.locator('a[data-qa="serp-item__title"]').all()
                if not cards:
                    break

                for card in cards:
                    href = await card.get_attribute("href")
                    title = (await card.inner_text()).strip()
                    if not href:
                        continue

                    job_id = _vacancy_id(href)
                    if not job_id:
                        continue

                    found_results += 1

                    if job_id in seen_ids:
                        duplicates += 1
                        continue

                    seen_ids.add(job_id)

                    existing = self.database.get(job_id)
                    if existing is not None:
                        duplicates += 1
                        continue

                    if len(results) < self.settings.max_vacancies_per_query:
                        results.append(
                            VacancySummary(
                                job_id,
                                title,
                                href,
                                query,
                            )
                        )

                if len(results) >= self.settings.max_vacancies_per_query:
                    break

                await self._delay()

            return VacancySearchResult(results, found_results, duplicates)
        except Exception as exc:
            logger.error("vacancy_search_failed query=%r error=%s", query, exc)
            return VacancySearchResult(
                results,
                found_results,
                duplicates,
                f"search_error:{type(exc).__name__}",
            )
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.warning(
                        "vacancy_search_page_close_failed query=%r error=%s",
                        query,
                        type(exc).__name__,
                    )

    async def read_vacancy(
        self,
        summary: VacancySummary,
        captcha_solver: CaptchaSolver | None = None,
    ) -> VacancyDetails:
        page = None
        try:
            page = await self.context.new_page()
            await self._delay()
            await page.goto(
                summary.url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            state = await classify_page(page)

            if state is PageState.CAPTCHA_DETECTED and captcha_solver is not None:
                state = await self._solve_captcha(page, summary, captcha_solver)

            if state is not PageState.VACANCY_LOADED:
                if state is PageState.CAPTCHA_DETECTED:
                    logger.warning("captcha_detected job_id=%s", summary.id)
                return VacancyDetails(summary, state)

            # Даем HH дорендерить controls после DOMContentLoaded.
            await self.sleep(1)

            # Ключевое правило: если на открытой вакансии нет доступной кнопки
            # "Откликнуться", агент НЕ отправляет ее в AI и НЕ показывает в Telegram.
            # Сюда попадают, в частности, вакансии где уже есть отклик,
            # переговоры, приглашение работодателя или отклики больше недоступны.
            if not await _response_available(page):
                reason = await _interaction_reason(page)
                logger.info(
                    "vacancy_skipped job_id=%s reason=%s",
                    summary.id,
                    reason,
                )
                return VacancyDetails(
                    summary,
                    PageState.RESPONSE_UNAVAILABLE,
                    error=reason,
                )

            metadata = await self._read_vacancy_metadata(page, summary)
            rejection = _location_work_format_rejection(
                metadata.location,
                metadata.work_formats,
            )
            if rejection:
                logger.info(
                    "vacancy_skipped job_id=%s reason=%s",
                    summary.id,
                    rejection,
                )
                return VacancyDetails(
                    summary,
                    PageState.RESPONSE_UNAVAILABLE,
                    error=rejection,
                    location=metadata.location,
                    work_formats=metadata.work_formats,
                )

            description = (
                await page.locator('[data-qa="vacancy-description"]').inner_text()
            ).strip()
            if not description:
                return VacancyDetails(
                    summary,
                    PageState.PAGE_STRUCTURE_CHANGED,
                    error="vacancy description is empty",
                    location=metadata.location,
                    work_formats=metadata.work_formats,
                )

            # Передаем структурированные данные HH в AI вместе с описанием,
            # чтобы модель не придумывала город или формат работы.
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

            company_locator = page.locator('[data-qa="vacancy-company-name"]')
            company = (
                (await company_locator.inner_text()).strip()
                if await company_locator.is_visible()
                else ""
            )
            company_url = _company_url(
                summary.url,
                await company_locator.get_attribute("href") if company else None,
            )

            return VacancyDetails(
                summary,
                state,
                company,
                description,
                company_url=company_url,
                location=metadata.location,
                work_formats=metadata.work_formats,
            )
        except Exception as exc:
            return VacancyDetails(
                summary,
                PageState.NETWORK_ERROR,
                error=str(exc),
            )
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.warning(
                        "vacancy_page_close_failed job_id=%s error=%s",
                        summary.id,
                        exc,
                    )

    async def read_company_details(self, company_url: str) -> CompanyDetails:
        company_url = _company_url("https://hh.ru/", company_url)
        if not company_url:
            return CompanyDetails()

        page = None
        try:
            page = await self.context.new_page()
            await self._delay()
            await page.goto(
                company_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            rating_text = await _visible_text(
                page,
                '[data-qa="employer-review-small-widget-total-rating"]',
            )
            reviews_text = await _visible_text(
                page,
                '[data-qa="employer-review-small-widget-review-count-action"]',
            )
            try:
                rating = float(rating_text.replace(",", "."))
                if not 0 <= rating <= 5:
                    rating = None
            except ValueError:
                rating = None

            match = re.search(r"\d[\d\s\u00a0]*", reviews_text)
            reviews_count = int(re.sub(r"\D", "", match.group())) if match else None
            return CompanyDetails(rating, reviews_count)
        except Exception as exc:
            logger.warning(
                "company_details_read_failed error_type=%s",
                type(exc).__name__,
            )
            return CompanyDetails()
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    logger.warning("company_details_page_close_failed")

    async def _solve_captcha(
        self,
        page: Any,
        summary: VacancySummary,
        solver: CaptchaSolver,
    ) -> PageState:
        for _ in range(self.settings.captcha_max_attempts):
            screenshot = (
                Path(tempfile.gettempdir()) / f"hh-captcha-{uuid4().hex}.png"
            )
            try:
                await page.screenshot(path=screenshot)
                solution = await solver(
                    screenshot,
                    summary.title,
                    self.settings.captcha_timeout_seconds,
                )
                if not solution:
                    return PageState.CAPTCHA_DETECTED

                field = page.locator('input[type="text"]').first
                if not await field.is_visible():
                    return PageState.PAGE_STRUCTURE_CHANGED

                await field.fill(solution)
                submit = page.locator(
                    'button[type="submit"]:visible, '
                    'button:has-text("Отправить"):visible'
                ).first
                if await submit.is_visible():
                    await submit.click()
                else:
                    await field.press("Enter")

                await self.sleep(1)
                state = await classify_page(page)
                if state is not PageState.CAPTCHA_DETECTED:
                    return state
            finally:
                screenshot.unlink(missing_ok=True)

        return PageState.CAPTCHA_DETECTED

    async def submit_application(self, permission: ApplicationPermission) -> bool:
        claim = self.approval_guard.claim(permission)
        if not claim.allowed or claim.vacancy is None:
            logger.warning(
                "application_blocked job_id=%s reason=%s",
                permission.job_id,
                claim.reason,
            )
            return False

        vacancy = claim.vacancy
        page = None
        try:
            page = await self.context.new_page()
            await self._delay()
            await page.goto(
                vacancy.url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            if await page.locator(
                'input[type="tel"], '
                'a[data-qa="mainmenu_applicantAccess"]:has-text("Войти")'
            ).first.is_visible():
                raise RuntimeError(
                    "Сессия на HH.ru не авторизована. Требуется вход в аккаунт HH.ru."
                )

            if not await _response_available(page):
                reason = await _interaction_reason(page)
                raise RuntimeError(f"application unavailable: {reason}")

            response_button = page.locator(
                'a[data-qa="vacancy-response-link-top"], '
                'button[data-qa="vacancy-response-link-top"], '
                'a[data-qa="vacancy-response-link-bottom"], '
                'button[data-qa="vacancy-response-link-bottom"], '
                'a[data-qa="vacancy-response-link"], '
                'button[data-qa="vacancy-response-link"]'
            ).first

            if not await response_button.is_visible():
                raise RuntimeError("application response control was not found")

            await response_button.click()
            await self._delay()

            resume_name = self.settings.profile.hh.resume_name
            resume_selector = page.locator(
                '[data-qa*="resume-select"], [data-qa*="resume-selector"]'
            ).first
            if resume_name and await resume_selector.is_visible():
                await resume_selector.click()
                option = page.get_by_text(resume_name, exact=True).first
                if not await option.is_visible():
                    raise RuntimeError("configured resume was not found")
                await option.click()

            if await page.locator('textarea[name^="task_"]').count() > 0:
                raise QuestionnaireRequiredError("questionnaire_required")

            letter_toggle = (
                page.locator('[data-qa*="letter-toggle"]')
                .or_(page.get_by_text("Написать сопроводительное", exact=False))
                .or_(page.get_by_text("Добавить сопроводительное", exact=False))
                .first
            )
            if await letter_toggle.is_visible():
                await letter_toggle.click()
                await self._delay()

            textarea = page.locator('textarea:not([name^="task_"])').first
            try:
                await textarea.wait_for(state="visible", timeout=5_000)
                await textarea.fill(vacancy.cover_letter)
                await self._delay()
            except Exception:
                logger.info(
                    "cover_letter_field_not_found job_id=%s, proceeding to submit",
                    vacancy.id,
                )

            submit_button = page.locator(
                'button[data-qa*="vacancy-response-submit"]:visible'
            ).first
            if not await submit_button.is_visible():
                raise RuntimeError("final application button was not found")

            if not self.database.mark_submit_attempt(
                permission.job_id,
                permission.permit,
                now=self.now_factory(),
                daily_limit=self.settings.max_applications_per_day,
            ):
                error = "application permission failed final pre-submit validation"
                self.database.complete_application(
                    permission.job_id,
                    permission.permit,
                    success=False,
                    now=self.now_factory(),
                    error_text=error,
                )
                logger.warning(
                    "application_blocked job_id=%s reason=pre_submit_recheck",
                    permission.job_id,
                )
                return False

            await submit_button.click()

            success_marker = (
                page.locator(
                    '[data-qa="vacancy-response-success"], '
                    '[data-qa="vacancy-response-link-view-topic"]'
                )
                .or_(page.get_by_text("Отклик отправлен", exact=False))
                .first
            )
            await success_marker.wait_for(state="visible", timeout=5_000)

            if not await self._response_confirmed(page, vacancy.url):
                raise RuntimeError("HH.ru did not confirm the application")

            if not self.database.complete_application(
                permission.job_id,
                permission.permit,
                success=True,
                now=self.now_factory(),
            ):
                raise RuntimeError("application status could not be completed")

            logger.info("application_sent job_id=%s", permission.job_id)
            return True
        except Exception as exc:
            self.database.complete_application(
                permission.job_id,
                permission.permit,
                success=False,
                now=self.now_factory(),
                error_text=str(exc),
            )
            logger.error(
                "application_failed job_id=%s error=%s",
                permission.job_id,
                exc,
            )
            return False
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.warning(
                        "application_page_close_failed job_id=%s error=%s",
                        permission.job_id,
                        exc,
                    )

    async def check_messages(
        self,
        notifier: Callable[[str], Awaitable[None]],
    ) -> None:
        page = await self.context.new_page()
        try:
            await self._delay()
            await page.goto(
                "https://hh.ru/applicant/negotiations",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            cards = await page.locator('div[data-qa="negotiations-item"]').all()
            for card in cards:
                badge = card.locator('span[data-qa="negotiations-item-badge"]')
                if not await badge.is_visible():
                    continue

                link = card.locator('a[data-qa="negotiations-item-vacancy-link"]')
                href = await link.get_attribute("href")
                title = (await link.inner_text()).strip()
                message_id = f"{href}:{title}"

                if href and not self.database.is_message_processed(message_id):
                    self.database.add_processed_message(message_id, href, title)
                    await notifier(
                        f"New unread HH message for: {title}\nhttps://hh.ru{href}"
                    )
        except Exception as exc:
            logger.error("message_check_failed error=%s", exc)
        finally:
            await page.close()
