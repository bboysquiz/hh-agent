from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import socket
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from ddgs import DDGS
from dotenv import dotenv_values
from pydantic import BaseModel, Field

from config import Settings
from database import Database
from llm.base import LLMProvider
from llm.factory import create_llm_provider
from llm.types import LLMRequest

logger = logging.getLogger(__name__)
EmploymentStatus = Literal["current", "former", "unknown"]
USER_AGENT = "hh-agent-public-profile-enrichment/2.0"

TELEGRAM_URL_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})",
    re.I,
)
TELEGRAM_TEXT_RE = re.compile(
    r"(?:telegram|телеграм|tg)\s*[:\-–—]?\s*@([A-Za-z0-9_]{5,32})",
    re.I,
)
NAME_RE = re.compile(
    r"\b([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё'’-]+(?:\s+[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё'’-]+){1,3})\b"
)

RESOURCE_DOMAINS = {
    "linkedin.com": "linkedin",
    "github.com": "github",
    "behance.net": "behance",
    "career.habr.com": "habr_career",
    "habr.com": "habr",
    "setka.ru": "setka",
    "dribbble.com": "dribbble",
    "stackoverflow.com": "stackoverflow",
    "vc.ru": "vc",
    "medium.com": "medium",
    "x.com": "x",
    "twitter.com": "x",
}
BLOCKED_CRAWL = {
    "linkedin.com",
    "www.linkedin.com",
    "ru.linkedin.com",
    "facebook.com",
    "www.facebook.com",
    "instagram.com",
    "www.instagram.com",
}
SEARCH_HOSTS = {
    "google.com",
    "www.google.com",
    "yandex.ru",
    "bing.com",
    "www.bing.com",
    "search.brave.com",
    "duckduckgo.com",
}

ROLE_HR = (
    " recruiter ", " рекрутер ", " talent acquisition ", " talent partner ",
    " hr ", " hrbp ", " human resources ", " people partner ",
)
ROLE_HEAD = (
    " head ", " director ", " руковод", " начальник", " cto ", " cio ",
    " cpo ", " ceo ", " team lead ", " teamlead ", " lead ",
)
ROLE_DESIGN = (" designer ", " design ", " дизайнер", " art director ", " ux ", " ui ")
ROLE_DEV = (
    " developer ", " engineer ", " software ", " разработчик", " инженер",
    " frontend ", " backend ", " fullstack ", " mobile ", " ios ", " android ",
)
FORMER_MARKERS = (
    "former ", "formerly ", "previously ", "ex-", "бывш", "ранее ",
    "работал в", "работала в", "worked at", "worked for",
)
CURRENT_MARKERS = (
    "works at", "working at", "currently", "present", "по настоящее время",
    "работает сейчас", "работает в", "работаю в", "в настоящее время",
)


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    description: str


@dataclass(frozen=True)
class ResourceLink:
    kind: str
    url: str


@dataclass(frozen=True)
class PersonProfile:
    full_name: str
    title: str
    employment_status: EmploymentStatus
    employment_note: str
    telegram: str
    resources: tuple[ResourceLink, ...]
    evidence_urls: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "PersonProfile":
        raw_status = str(value.get("employment_status", "unknown"))
        status: EmploymentStatus = (
            raw_status if raw_status in {"current", "former", "unknown"} else "unknown"
        )  # type: ignore[assignment]
        resources = tuple(
            ResourceLink(str(item.get("kind", "website")), str(item.get("url", "")))
            for item in value.get("resources", [])
            if isinstance(item, dict) and item.get("url")
        )
        return cls(
            full_name=str(value.get("full_name", "")),
            title=str(value.get("title", "")),
            employment_status=status,
            employment_note=str(value.get("employment_note", "")),
            telegram=str(value.get("telegram", "")),
            resources=resources,
            evidence_urls=tuple(
                str(x) for x in value.get("evidence_urls", []) if isinstance(x, str)
            ),
        )


@dataclass(frozen=True)
class Config:
    enabled: bool
    max_employees: int
    results_per_query: int
    cache_hours: int
    empty_cache_hours: int
    crawl_depth: int
    pages_per_person: int
    timeout: int
    max_page_bytes: int
    search_region: str
    search_backends: tuple[str, ...]
    use_llm: bool
    search_delay: float

    @classmethod
    def load(cls) -> "Config":
        values: dict[str, str] = {}
        base_dir = Path(__file__).resolve().parent
        for env_path in (base_dir / ".env", base_dir / "people.env"):
            if env_path.exists():
                values.update({k: v for k, v in dotenv_values(env_path).items() if v is not None})
        values.update(os.environ)

        def b(key: str, default: bool) -> bool:
            return values.get(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}

        def i(key: str, default: int, lo: int, hi: int) -> int:
            try:
                return max(lo, min(hi, int(values.get(key, default))))
            except (TypeError, ValueError):
                return default

        def f(key: str, default: float, lo: float, hi: float) -> float:
            try:
                return max(lo, min(hi, float(values.get(key, default))))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=b("PEOPLE_ENRICHMENT_ENABLED", True),
            max_employees=i("PEOPLE_MAX_EMPLOYEES", 6, 1, 15),
            results_per_query=i("PEOPLE_SEARCH_RESULTS", 10, 3, 20),
            cache_hours=i("PEOPLE_CACHE_HOURS", 72, 1, 720),
            empty_cache_hours=i("PEOPLE_EMPTY_CACHE_HOURS", 12, 1, 72),
            crawl_depth=i("PEOPLE_CRAWL_DEPTH", 2, 0, 3),
            pages_per_person=i("PEOPLE_MAX_PAGES_PER_PERSON", 8, 1, 20),
            timeout=i("PEOPLE_HTTP_TIMEOUT_SECONDS", 12, 3, 30),
            max_page_bytes=i("PEOPLE_MAX_PAGE_BYTES", 750_000, 100_000, 2_000_000),
            search_region=values.get("PEOPLE_SEARCH_REGION", "ru-ru").strip() or "ru-ru",
            search_backends=tuple(
                item.strip()
                for item in values.get(
                    "PEOPLE_SEARCH_BACKENDS", "yandex,bing"
                ).split(",")
                if item.strip()
            ) or ("yandex", "bing"),
            use_llm=b("PEOPLE_USE_LLM", True),
            search_delay=f("PEOPLE_SEARCH_DELAY_SECONDS", 0.35, 0.0, 3.0),
        )


class LLMCandidate(BaseModel):
    full_name: str
    title: str = ""
    employment_status: Literal["current", "former", "unknown"] = "unknown"
    evidence_urls: list[str] = Field(default_factory=list)


class LLMCandidates(BaseModel):
    people: list[LLMCandidate] = Field(default_factory=list)


@dataclass(frozen=True)
class Candidate:
    full_name: str
    title: str
    employment_status: EmploymentStatus
    evidence_urls: tuple[str, ...]


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.text: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1

    def handle_data(self, data: str) -> None:
        if not self.skip and (part := " ".join(data.split())):
            self.text.append(part)


class DdgsSearch:
    """Free web discovery through DDGS. No API key is required.

    Backends are tried one by one instead of using DDGS backend="auto".
    This makes one broken/rate-limited engine a fallback event rather than
    a failure of the whole enrichment pass.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.lock = asyncio.Lock()
        self.last_call = 0.0

    async def close(self) -> None:
        return None

    def _search_sync(self, query: str, backend: str) -> list[dict[str, object]]:
        return DDGS(timeout=self.config.timeout).text(
            query,
            region=self.config.search_region,
            safesearch="moderate",
            max_results=self.config.results_per_query,
            page=1,
            backend=backend,
        )

    async def search(self, query: str) -> list[SearchResult]:
        async with self.lock:
            loop = asyncio.get_running_loop()
            output: list[SearchResult] = []
            seen: set[str] = set()

            for backend in self.config.search_backends:
                wait = self.config.search_delay - (loop.time() - self.last_call)
                if wait > 0:
                    await asyncio.sleep(wait)

                try:
                    raw_results = await asyncio.to_thread(
                        self._search_sync, query, backend
                    )
                except Exception as exc:
                    self.last_call = loop.time()
                    logger.warning(
                        "people_search_backend_failed backend=%s query=%r error=%s",
                        backend,
                        query,
                        type(exc).__name__,
                    )
                    continue

                self.last_call = loop.time()

                for item in raw_results or []:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("href") or item.get("url") or "").strip()
                    normalized = normalize_url(url)
                    if not normalized or normalized in seen:
                        continue
                    seen.add(normalized)
                    output.append(
                        SearchResult(
                            title=str(item.get("title") or ""),
                            url=url,
                            description=str(item.get("body") or item.get("description") or ""),
                        )
                    )
                    if len(output) >= self.config.results_per_query:
                        return output

            if not output:
                logger.warning(
                    "people_search_no_results query=%r backends=%s",
                    query,
                    ",".join(self.config.search_backends),
                )
            return output


class PeopleEnricher:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self.config = Config.load()
        self.searcher = DdgsSearch(self.config)
        self.http = httpx.AsyncClient(
            timeout=self.config.timeout,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"},
        )
        self.llm: LLMProvider | None = None
        self.lock = asyncio.Lock()
        self._init_db()

    @property
    def available(self) -> bool:
        return self.config.enabled

    async def close(self) -> None:
        await self.searcher.close()
        await self.http.aclose()
        if self.llm:
            await self.llm.close()
            self.llm = None

    def _init_db(self) -> None:
        with sqlite3.connect(self.database.path) as con:
            con.execute(
                """CREATE TABLE IF NOT EXISTS people_enrichment_cache (
                    company_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS people_enrichment_reports (
                    job_id TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL
                )"""
            )

    def report_was_sent(self, job_id: str) -> bool:
        with sqlite3.connect(self.database.path) as con:
            return con.execute(
                "SELECT 1 FROM people_enrichment_reports WHERE job_id = ?", (job_id,)
            ).fetchone() is not None

    def mark_report_sent(self, job_id: str) -> None:
        with sqlite3.connect(self.database.path) as con:
            con.execute(
                "INSERT OR REPLACE INTO people_enrichment_reports VALUES (?, ?)",
                (job_id, datetime.now(UTC).isoformat()),
            )

    async def enrich_company(self, company: str) -> tuple[PersonProfile, ...]:
        company = " ".join(company.split())
        if not company or not self.available:
            return ()
        async with self.lock:
            cached = self._cache_get(company)
            if cached is not None:
                return cached

            results = await self._discover(company)
            candidates = await self._candidates(company, results)
            candidates.sort(key=self._candidate_score, reverse=True)

            people: list[PersonProfile] = []
            for candidate in candidates[: self.config.max_employees * 2]:
                person = await self._enrich_person(company, candidate)
                if person:
                    people.append(person)
                if len(people) >= self.config.max_employees:
                    break

            people.sort(key=self._profile_score, reverse=True)
            final = tuple(people[: self.config.max_employees])
            self._cache_put(company, final)
            return final

    async def _discover(self, company: str) -> list[SearchResult]:
        q = quote(company)
        queries = [
            f'site:linkedin.com/in {q} (recruiter OR "talent acquisition" OR HR OR HRBP OR "people partner")',
            f'site:linkedin.com/in {q} ("Head of" OR director OR lead OR CTO OR manager OR developer OR engineer OR designer)',
            f'site:setka.ru {q} (HR OR рекрутер OR руководитель OR разработчик OR дизайнер)',
            f'site:career.habr.com {q}',
            f'site:habr.com {q} (разработчик OR инженер OR дизайнер OR руководитель OR HR)',
            f'{q} (рекрутер OR HR OR руководитель OR developer OR engineer OR designer) (LinkedIn OR Habr OR Сетка)',
        ]
        output: list[SearchResult] = []
        seen: set[str] = set()
        for query in queries:
            for item in await self.searcher.search(query):
                url = normalize_url(item.url)
                if url and url not in seen:
                    seen.add(url)
                    output.append(item)
        return output

    async def _candidates(self, company: str, results: list[SearchResult]) -> list[Candidate]:
        if self.config.use_llm and results:
            try:
                found = await self._candidates_llm(company, results)
                if found:
                    return dedupe_candidates(found)
            except Exception as exc:
                logger.warning("people_candidate_llm_failed error=%s", type(exc).__name__)
        return dedupe_candidates(self._candidates_heuristic(company, results))

    async def _candidates_llm(self, company: str, results: list[SearchResult]) -> list[Candidate]:
        if self.llm is None:
            self.llm = create_llm_provider(self.settings, self.database)
        lines = [
            f"[{n}] TITLE: {x.title}\nURL: {x.url}\nSNIPPET: {x.description[:700]}"
            for n, x in enumerate(results[:50], 1)
        ]
        _, parsed = await self.llm.generate_structured(
            LLMRequest(
                system_instructions=(
                    "Extract only real people associated with the requested company from public search results. "
                    "Prioritize recruiters/HR, hiring managers/executives, developers/engineers and designers. "
                    "employment_status=current only when the evidence indicates a current role; former only for explicit past/ex/former roles; otherwise unknown. "
                    "Never invent names, roles, status or URLs."
                ),
                user_content=f"Company: {company}\n\n" + "\n\n".join(lines),
                model=self.settings.llm.model,
                temperature=0,
                max_output_tokens=min(self.settings.llm.max_output_tokens, 1800),
                timeout_seconds=self.settings.llm.timeout_seconds,
                operation="people_discovery",
            ),
            LLMCandidates,
        )
        known_urls = {normalize_url(x.url): x.url for x in results}
        output: list[Candidate] = []
        for person in parsed.people:
            name = " ".join(person.full_name.split())
            if not looks_like_name(name):
                continue
            evidence = tuple(
                known_urls[u]
                for raw in person.evidence_urls
                if (u := normalize_url(raw)) in known_urls
            )
            output.append(
                Candidate(name, " ".join(person.title.split()), person.employment_status, evidence)
            )
        return output

    def _candidates_heuristic(self, company: str, results: list[SearchResult]) -> list[Candidate]:
        output: list[Candidate] = []
        for result in results:
            combined = f"{result.title} {result.description}"
            if company.casefold() not in combined.casefold():
                continue
            names = names_from_title(result.title) or NAME_RE.findall(result.description[:500])[:2]
            for name in names:
                if not looks_like_name(name):
                    continue
                status, _ = status_from_text(company, combined)
                output.append(
                    Candidate(
                        name,
                        title_from_result(name, company, result.title),
                        status,
                        (result.url,),
                    )
                )
        return output

    async def _enrich_person(self, company: str, candidate: Candidate) -> PersonProfile | None:
        queries = [
            f'{quote(candidate.full_name)} {quote(company)} (LinkedIn OR GitHub OR Behance OR Habr OR Сетка OR Telegram OR portfolio)',
            f'{quote(candidate.full_name)} (site:github.com OR site:behance.net OR site:linkedin.com/in OR site:setka.ru OR site:career.habr.com OR site:habr.com)',
        ]
        results: list[SearchResult] = []
        seen: set[str] = set()
        for query in queries:
            for item in await self.searcher.search(query):
                url = normalize_url(item.url)
                if url and url not in seen and result_matches_name(candidate.full_name, item):
                    seen.add(url)
                    results.append(item)

        resources: dict[str, str] = {}
        evidence_urls = list(candidate.evidence_urls)
        evidence_texts: list[str] = []
        telegram = ""
        for item in results:
            kind = resource_kind(item.url)
            if kind:
                resources.setdefault(kind, clean_url(item.url))
            elif safe_url(item.url):
                resources.setdefault("website", clean_url(item.url))
            evidence_urls.append(item.url)
            evidence_texts.append(f"{item.title}. {item.description}")

            # Search engines sometimes merge snippets from several LinkedIn profiles.
            # Never attribute a Telegram handle from such a mixed snippet to one person.
            direct_tg = telegram_from_url(item.url)
            if direct_tg:
                telegram = telegram or direct_tg
                resources.setdefault("telegram", direct_tg)
            elif trustworthy_person_snippet(candidate.full_name, item):
                snippet_tg = telegram_from_text(
                    f"{item.title} {item.description} {item.url}"
                )
                if snippet_tg:
                    telegram = telegram or snippet_tg
                    resources.setdefault("telegram", snippet_tg)
        queue = [(url, 0) for url in [*resources.values(), *candidate.evidence_urls] if should_crawl(url)]
        visited: set[str] = set()
        pages = 0

        while queue and pages < self.config.pages_per_person:
            url, depth = queue.pop(0)
            normalized = normalize_url(url)
            if not normalized or normalized in visited:
                continue
            visited.add(normalized)
            page = await self._fetch(url)
            if not page:
                continue
            final_url, text, links = page
            pages += 1
            evidence_urls.append(final_url)
            if company.casefold() in text.casefold():
                evidence_texts.append(text[:2500])
            telegram = telegram or telegram_from_text(text)

            for href in links:
                target = normalize_url(urljoin(final_url, href))
                if not target:
                    continue
                if tg := telegram_from_url(target):
                    telegram = telegram or tg
                    resources.setdefault("telegram", tg)
                    continue
                if kind := resource_kind(target):
                    resources.setdefault(kind, clean_url(target))
                    continue
                if depth < self.config.crawl_depth and likely_contact_or_external(final_url, target):
                    resources.setdefault("website", clean_url(target))
                    if should_crawl(target):
                        queue.append((target, depth + 1))

        if not telegram:
            for item in await self.searcher.search(
                f'{quote(candidate.full_name)} (Telegram OR телеграм OR t.me)'
            ):
                if not result_matches_name(candidate.full_name, item):
                    continue
                direct_tg = telegram_from_url(item.url)
                snippet_tg = (
                    telegram_from_text(f"{item.title} {item.description} {item.url}")
                    if trustworthy_person_snippet(candidate.full_name, item)
                    else ""
                )
                telegram = direct_tg or snippet_tg
                if telegram:
                    resources.setdefault("telegram", telegram)
                    evidence_urls.append(item.url)
                    break

        status, note = best_status(company, candidate.employment_status, evidence_texts)
        title = candidate.title or first_title(candidate.full_name, company, results)
        resource_list = tuple(
            ResourceLink(kind, url) for kind, url in sorted_resources(resources)
        )
        return PersonProfile(
            full_name=candidate.full_name,
            title=title or "не удалось определить",
            employment_status=status,
            employment_note=note,
            telegram=telegram,
            resources=resource_list,
            evidence_urls=tuple(dedupe_urls(evidence_urls)),
        )

    async def _fetch(self, url: str) -> tuple[str, str, list[str]] | None:
        current = normalize_url(url)
        if not current or not safe_url(current):
            return None
        for _ in range(4):
            if not should_crawl(current):
                return None
            if not await asyncio.to_thread(resolves_public, current):
                return None
            try:
                async with self.http.stream("GET", current) as response:
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("location")
                        current = normalize_url(urljoin(current, location or ""))
                        if not current:
                            return None
                        continue
                    if response.status_code >= 400:
                        return None
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(x in content_type for x in ("text/html", "text/plain", "xhtml")):
                        return None
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.config.max_page_bytes:
                            break
                        chunks.append(chunk)
            except httpx.HTTPError:
                return None
            parser = PageParser()
            parser.feed(b"".join(chunks).decode("utf-8", errors="replace"))
            return current, " ".join(parser.text), parser.links
        return None

    def _cache_get(self, company: str) -> tuple[PersonProfile, ...] | None:
        with sqlite3.connect(self.database.path) as con:
            row = con.execute(
                "SELECT payload_json, updated_at FROM people_enrichment_cache WHERE company_key = ?",
                (company_key(company),),
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[0])
            updated = datetime.fromisoformat(row[1])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            ttl = self.config.cache_hours if payload else self.config.empty_cache_hours
            if datetime.now(UTC) - updated.astimezone(UTC) > timedelta(hours=ttl):
                return None
            return tuple(PersonProfile.from_dict(x) for x in payload if isinstance(x, dict))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _cache_put(self, company: str, people: tuple[PersonProfile, ...]) -> None:
        payload = json.dumps([asdict(x) for x in people], ensure_ascii=False, separators=(",", ":"))
        with sqlite3.connect(self.database.path) as con:
            con.execute(
                "INSERT OR REPLACE INTO people_enrichment_cache VALUES (?, ?, ?)",
                (company_key(company), payload, datetime.now(UTC).isoformat()),
            )

    @staticmethod
    def _candidate_score(person: Candidate) -> int:
        return role_score(person.title) + {"current": 25, "former": 5, "unknown": 0}[person.employment_status]

    @staticmethod
    def _profile_score(person: PersonProfile) -> int:
        return (
            role_score(person.title)
            + {"current": 30, "former": 5, "unknown": 0}[person.employment_status]
            + (25 if person.telegram else 0)
        )


def quote(value: str) -> str:
    return '"' + value.replace('"', " ") + '"'


def company_key(value: str) -> str:
    return "ddgs-v2:" + " ".join(value.casefold().split())


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", " ", value.casefold()).strip()


def looks_like_name(value: str) -> bool:
    parts = value.split()
    return 2 <= len(parts) <= 4 and all(len(x) >= 2 for x in parts)


def names_from_title(title: str) -> list[str]:
    title = re.sub(r"\s*[|·]\s*(LinkedIn|GitHub|Behance|Сетка|Habr.*)$", "", title, flags=re.I)
    first = re.split(r"\s+[—–-]\s+", title, maxsplit=1)[0].strip()
    return [first] if NAME_RE.fullmatch(first) else []


def title_from_result(name: str, company: str, title: str) -> str:
    title = re.sub(r"\s*[|·]\s*(LinkedIn|GitHub|Behance|Сетка|Habr.*)$", "", title, flags=re.I)
    parts = [x.strip() for x in re.split(r"\s+[—–-]\s+", title) if x.strip()]
    if parts and parts[0].casefold() == name.casefold():
        parts.pop(0)
    parts = [x for x in parts if x.casefold() != company.casefold()]
    return " — ".join(parts[:2])[:180]


def first_title(name: str, company: str, results: list[SearchResult]) -> str:
    for result in results:
        if title := title_from_result(name, company, result.title):
            return title
    return ""


def role_score(title: str) -> int:
    text = f" {title.casefold()} "
    if any(x in text for x in ROLE_HR):
        return 100
    if any(x in text for x in ROLE_HEAD):
        return 85
    if any(x in text for x in ROLE_DESIGN):
        return 65
    if any(x in text for x in ROLE_DEV):
        return 60
    return 20


def dedupe_candidates(items: list[Candidate]) -> list[Candidate]:
    best: dict[str, Candidate] = {}
    for item in items:
        key = normalize_name(item.full_name)
        if not key:
            continue
        old = best.get(key)
        if old is None or PeopleEnricher._candidate_score(item) > PeopleEnricher._candidate_score(old):
            best[key] = item
        elif old:
            status = old.employment_status if old.employment_status != "unknown" else item.employment_status
            best[key] = Candidate(
                old.full_name,
                old.title or item.title,
                status,
                tuple(dedupe_urls([*old.evidence_urls, *item.evidence_urls])),
            )
    return list(best.values())



def trustworthy_person_snippet(name: str, result: SearchResult) -> bool:
    """Return True only for a search snippet that looks dedicated to one person.

    Bing can concatenate several LinkedIn cards into one result. Those snippets are
    useful for discovery, but unsafe for attributing a Telegram handle to a person.
    """
    wanted = normalize_name(name)
    title = normalize_name(result.title)
    if not wanted or not title.startswith(wanted):
        return False

    raw_title = result.title.casefold()
    raw_body = result.description.casefold()

    # Multiple LinkedIn markers are a strong signal that the engine merged profiles.
    if raw_title.count("linkedin") > 1 or raw_body.count("linkedin") > 2:
        return False

    # A long chain of profile separators is another common merged-result pattern.
    if result.title.count("|") >= 3:
        return False

    return True

def result_matches_name(name: str, result: SearchResult) -> bool:
    haystack = normalize_name(f"{result.title} {result.description} {result.url}")
    tokens = [x for x in normalize_name(name).split() if len(x) > 2]
    return bool(tokens) and tokens[0] in haystack and tokens[-1] in haystack


def status_from_text(company: str, text: str) -> tuple[EmploymentStatus, str]:
    text = " ".join(text.casefold().split())
    company_cf = company.casefold()
    if company_cf not in text:
        return "unknown", "компания не упомянута в проверенном фрагменте"
    if any(x in text for x in FORMER_MARKERS):
        return "former", "есть явный признак прошлого места работы"
    if any(x in text for x in CURRENT_MARKERS):
        return "current", "есть явный признак текущего места работы"
    if any(x in text for x in (f" at {company_cf}", f" в {company_cf}", f" @ {company_cf}")):
        return "current", "публичный профиль указывает роль в этой компании"
    return "unknown", "публичных данных недостаточно для надёжного вывода"


def best_status(
    company: str,
    seed: EmploymentStatus,
    evidence: list[str],
) -> tuple[EmploymentStatus, str]:
    current = False
    former = False
    current_note = ""
    former_note = ""
    for text in evidence:
        status, note = status_from_text(company, text)
        if status == "current":
            current, current_note = True, note
        elif status == "former":
            former, former_note = True, note
    if current and not former:
        return "current", current_note
    if former and not current:
        return "former", former_note
    if current and former:
        return "unknown", "в публичных источниках есть противоречивые сведения"
    if seed != "unknown":
        return seed, "статус подтверждён исходным публичным профилем/сниппетом"
    return "unknown", "не удалось надёжно подтвердить текущий или прошлый статус"


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold().rstrip(".")
    if host in SEARCH_HOSTS:
        return ""
    netloc = host + (f":{parsed.port}" if parsed.port else "")
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), netloc, path, "", parsed.query, ""))


def clean_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def safe_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    )



def resolves_public(url: str) -> bool:
    if not safe_url(url):
        return False
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for *_, sockaddr in addresses:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified
        ):
            return False
    return bool(addresses)

def resource_kind(url: str) -> str:
    host = (urlparse(url).hostname or "").casefold()
    if host in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
        return "telegram"
    # career.habr.com must win before habr.com.
    for domain, kind in RESOURCE_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return kind
    return ""


def should_crawl(url: str) -> bool:
    url = normalize_url(url)
    if not url or not safe_url(url):
        return False
    return (urlparse(url).hostname or "").casefold() not in BLOCKED_CRAWL


def telegram_from_url(url: str) -> str:
    match = TELEGRAM_URL_RE.search(url)
    if not match or match.group(1).casefold() in {"share", "joinchat", "addstickers", "iv"}:
        return ""
    return f"https://t.me/{match.group(1)}"


def telegram_from_text(text: str) -> str:
    if tg := telegram_from_url(text):
        return tg
    match = TELEGRAM_TEXT_RE.search(text)
    return f"https://t.me/{match.group(1)}" if match else ""


def likely_contact_or_external(source: str, target: str) -> bool:
    src = urlparse(source)
    dst = urlparse(target)
    if not dst.hostname or dst.hostname.casefold() in SEARCH_HOSTS:
        return False
    if resource_kind(target):
        return True
    if dst.hostname.casefold() != (src.hostname or "").casefold():
        return not dst.path.casefold().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".zip"))
    return any(word in dst.path.casefold() for word in ("contact", "about", "links", "social", "bio"))


def dedupe_urls(urls: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = normalize_url(url)
        if normalized and normalized not in seen:
            seen.add(normalized)
            output.append(url)
    return output


def sorted_resources(resources: dict[str, str]) -> list[tuple[str, str]]:
    order = {
        "telegram": 0, "linkedin": 1, "setka": 2, "github": 3,
        "habr_career": 4, "habr": 5, "behance": 6, "dribbble": 7, "website": 8,
    }
    by_url: dict[str, tuple[str, str]] = {}
    for kind, url in resources.items():
        if normalized := normalize_url(url):
            by_url.setdefault(normalized, (kind, url))
    return sorted(by_url.values(), key=lambda x: (order.get(x[0], 50), x[0]))


RESOURCE_LABELS = {
    "linkedin": "LinkedIn", "setka": "Сетка", "github": "GitHub",
    "habr_career": "Хабр Карьера", "habr": "Хабр", "behance": "Behance",
    "dribbble": "Dribbble", "stackoverflow": "Stack Overflow", "vc": "vc.ru",
    "medium": "Medium", "x": "X/Twitter", "website": "Сайт/портфолио",
}
STATUS_LABELS = {
    "current": "🟢 работает сейчас",
    "former": "⚪ работал(а) раньше",
    "unknown": "🟡 текущий/прошлый статус не подтверждён",
}


def format_people_messages(
    vacancy_title: str,
    company: str,
    people: tuple[PersonProfile, ...],
    max_chars: int = 3800,
) -> list[str]:
    header = (
        f"<b>Люди из компании — {html.escape(company)}</b>\n"
        f"Вакансия: {html.escape(vacancy_title)}\n"
        "Только публичные профессиональные страницы и опубликованные самим человеком ссылки.\n"
    )
    if not people:
        return [header + "\nНадёжно сопоставленных сотрудников в публичных источниках не найдено."]

    blocks: list[str] = []
    for n, person in enumerate(people, 1):
        if person.telegram:
            username = urlparse(person.telegram).path.strip("/") or "Telegram"
            telegram = f'<a href="{html.escape(person.telegram, quote=True)}">@{html.escape(username)}</a>'
        else:
            telegram = "не найден публично"
        links = [
            f'<a href="{html.escape(x.url, quote=True)}">{html.escape(RESOURCE_LABELS.get(x.kind, x.kind))}</a>'
            for x in person.resources
            if x.kind != "telegram"
        ]
        blocks.append(
            f"\n<b>{n}. {html.escape(person.full_name)}</b>\n"
            f"Должность: {html.escape(person.title)}\n"
            f"Статус: {STATUS_LABELS[person.employment_status]}\n"
            f"Основание: {html.escape(person.employment_note)}\n"
            f"Telegram: {telegram}\n"
            f"Ресурсы: {' · '.join(links) if links else 'не найдено'}"
        )

    messages: list[str] = []
    current = header
    for block in blocks:
        if len(current) + len(block) > max_chars and current != header:
            messages.append(current)
            current = f"<b>Люди из компании — {html.escape(company)} (продолжение)</b>\n{block}"
        else:
            current += block
    messages.append(current)
    return messages
