from __future__ import annotations

"""Strict identity/contact validation layered on top of people_enrichment.

This module keeps the search/caching/crawling implementation in
``people_enrichment.py`` and replaces only the permissive matching pieces.
It exists so the production addon can be upgraded without duplicating the
whole enrichment implementation.
"""

import logging
import re
from urllib.parse import urljoin, urlparse

import people_enrichment as base

logger = logging.getLogger(__name__)

NAME_STOPWORDS = {
    "компания", "company", "хабр", "habr", "карьера", "карьере", "career",
    "linkedin", "github", "behance", "setka", "сетка", "product", "manager",
    "developer", "engineer", "designer", "frontend", "backend", "fullstack",
    "vacancy", "вакансия", "вакансии", "открытые", "работа", "работы",
    "recruiter", "hr", "head", "lead", "director", "teamlead", "team",
    "support", "design", "software", "mobile", "ios", "android",
}

GENERIC_EXTERNAL_HOSTS = {
    "itunes.apple.com", "apps.apple.com", "play.google.com",
    "github.blog", "docs.github.com", "support.github.com", "githubstatus.com",
    "about.gitlab.com", "help.behance.net", "adobe.com", "www.adobe.com",
    "www.facebook.com", "facebook.com",
}

GENERIC_SOCIAL_USERNAMES = {
    "habr", "habr_career", "career_habr", "habr_com", "github", "githubstatus",
    "behance", "behanceofficial", "adobe", "dribbble", "linkedin", "setka",
    "twitter", "x", "telegram",
}

GENERIC_RESOURCE_PATH_PREFIXES = {
    "github": {
        "topics", "features", "marketplace", "collections", "orgs",
        "organizations", "apps", "settings", "login", "join",
    },
    "behance": {"gallery", "search", "joblist", "galleries", "assets", "hire"},
    "habr_career": {
        "companies", "vacancies", "courses", "rating", "articles", "salary",
        "education",
    },
    "habr": {"ru", "en", "top", "flows", "hubs", "companies", "articles", "news"},
}


def _words(value: str) -> list[str]:
    return [x for x in base.normalize_name(value).split() if x]


def looks_like_name(value: str, company: str = "") -> bool:
    parts = value.split()
    if not (2 <= len(parts) <= 4):
        return False
    if not all(len(x.strip(".'’")) >= 2 for x in parts):
        return False

    normalized_parts = _words(value)
    if any(part in NAME_STOPWORDS for part in normalized_parts):
        return False

    company_parts = {x for x in _words(company) if len(x) >= 4}
    if company_parts and set(normalized_parts) & company_parts:
        return False

    return all(re.fullmatch(r"[A-Za-zА-Яа-яЁё'’.-]+", part) for part in parts)


def names_from_title(title: str, company: str = "") -> list[str]:
    cleaned = re.sub(
        r"\s*[|·]\s*(LinkedIn|GitHub|Behance|Сетка|Setka|Habr.*)$",
        "",
        title,
        flags=re.I,
    )
    first = re.split(r"\s+[—–-]\s+", cleaned, maxsplit=1)[0].strip()
    if not base.NAME_RE.fullmatch(first):
        return []
    return [first] if looks_like_name(first, company) else []


def _path_parts(url: str) -> list[str]:
    return [x for x in urlparse(url).path.split("/") if x]


def is_personal_resource_url(kind: str, url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    parts = _path_parts(url)
    if not parts:
        return False

    if kind == "linkedin":
        return len(parts) >= 2 and parts[0].casefold() == "in"
    if kind == "github":
        return len(parts) == 1 and parts[0].casefold() not in GENERIC_RESOURCE_PATH_PREFIXES["github"]
    if kind == "behance":
        return len(parts) == 1 and parts[0].casefold() not in GENERIC_RESOURCE_PATH_PREFIXES["behance"]
    if kind == "habr_career":
        return len(parts) == 1 and parts[0].casefold() not in GENERIC_RESOURCE_PATH_PREFIXES["habr_career"]
    if kind == "habr":
        lowered = [x.casefold() for x in parts]
        if "users" in lowered:
            index = lowered.index("users")
            return len(parts) > index + 1
        return False
    if kind == "setka":
        lowered = [x.casefold() for x in parts]
        if lowered[0] in {"networks", "companies", "company", "search", "vacancies", "jobs"}:
            return False
        return True
    if kind in {"x", "dribbble", "medium", "vc"}:
        username = parts[0].lstrip("@").casefold()
        return username not in GENERIC_SOCIAL_USERNAMES
    if kind == "stackoverflow":
        return len(parts) >= 2 and parts[0].casefold() == "users"
    if kind == "telegram":
        return telegram_is_personal_candidate(url)
    return bool(host and parts)


def candidate_source_url_ok(url: str) -> bool:
    kind = base.resource_kind(url)
    return bool(kind and is_personal_resource_url(kind, url))


def is_generic_external_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return host in GENERIC_EXTERNAL_HOSTS or any(
        host.endswith("." + item) for item in GENERIC_EXTERNAL_HOSTS
    )


def is_generic_title(value: str) -> bool:
    text = base.normalize_name(value)
    if not text:
        return True
    words = set(text.split())
    generic = {
        "linkedin", "github", "behance", "хабр", "habr", "карьера", "career",
        "вакансия", "вакансии", "открытые", "company", "компания",
    }
    return words.issubset(generic) or text in {
        "linkedin", "github", "behance", "хабр карьера"
    }


def page_mentions_person(name: str, text: str) -> bool:
    name_tokens = [x for x in _words(name) if len(x) >= 2]
    haystack = base.normalize_name(text[:12000])
    return len(name_tokens) >= 2 and all(
        token in haystack for token in (name_tokens[0], name_tokens[-1])
    )


def result_has_merged_people_noise(result: base.SearchResult) -> bool:
    blob = f"{result.title} {result.description}"
    if blob.casefold().count("linkedin") >= 3:
        return True
    names = re.findall(
        r"\b[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё'’-]+\s+[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё'’-]+\b",
        blob,
    )
    return len(names) >= 5


def result_matches_name(name: str, result: base.SearchResult) -> bool:
    haystack = base.normalize_name(f"{result.title} {result.description}")
    tokens = [x for x in _words(name) if len(x) > 1]
    return len(tokens) >= 2 and tokens[0] in haystack and tokens[-1] in haystack


def result_is_clean_person_result(name: str, result: base.SearchResult) -> bool:
    if result_has_merged_people_noise(result):
        return False
    title_name = names_from_title(result.title)
    if title_name and base.normalize_name(title_name[0]) == base.normalize_name(name):
        return True
    return base.normalize_name(name) in base.normalize_name(
        f"{result.title} {result.description}"
    )


def result_is_personal(name: str, result: base.SearchResult) -> bool:
    kind = base.resource_kind(result.url)
    if result_has_merged_people_noise(result):
        title_names = names_from_title(result.title)
        return bool(
            kind
            and is_personal_resource_url(kind, result.url)
            and title_names
            and base.normalize_name(title_names[0]) == base.normalize_name(name)
        )
    if kind:
        return is_personal_resource_url(kind, result.url) and result_matches_name(name, result)
    return result_matches_name(name, result) and not is_generic_external_url(result.url)


def telegram_is_personal_candidate(url: str) -> bool:
    tg = base.telegram_from_url(url)
    if not tg:
        return False
    username = urlparse(tg).path.strip("/").casefold()
    return username not in GENERIC_SOCIAL_USERNAMES


def telegram_from_text(text: str) -> str:
    if tg := base.telegram_from_url(text):
        return tg if telegram_is_personal_candidate(tg) else ""
    match = base.TELEGRAM_TEXT_RE.search(text)
    if not match:
        return ""
    tg = f"https://t.me/{match.group(1)}"
    return tg if telegram_is_personal_candidate(tg) else ""


def resource_is_safe_for_output(kind: str, url: str) -> bool:
    if kind == "website":
        return not is_generic_external_url(url)
    return is_personal_resource_url(kind, url)


def company_key(value: str) -> str:
    return "ddgs-v3:" + " ".join(value.casefold().split())


def _strict_candidates_heuristic(
    self: base.PeopleEnricher,
    company: str,
    results: list[base.SearchResult],
) -> list[base.Candidate]:
    output: list[base.Candidate] = []
    for result in results:
        combined = f"{result.title} {result.description}"
        if company.casefold() not in combined.casefold():
            continue
        if not candidate_source_url_ok(result.url):
            continue

        for name in names_from_title(result.title, company):
            if not looks_like_name(name, company):
                continue
            status, _ = base.status_from_text(company, combined)
            title = base.title_from_result(name, company, result.title)
            if is_generic_title(title):
                title = ""
            output.append(base.Candidate(name, title, status, (result.url,)))
    return output


async def _strict_enrich_person(
    self: base.PeopleEnricher,
    company: str,
    candidate: base.Candidate,
) -> base.PersonProfile | None:
    queries = [
        f'{base.quote(candidate.full_name)} {base.quote(company)} '
        '(LinkedIn OR GitHub OR Behance OR Habr OR Сетка OR portfolio)',
        f'{base.quote(candidate.full_name)} '
        '(site:github.com OR site:behance.net OR site:linkedin.com/in OR '
        'site:setka.ru OR site:career.habr.com OR site:habr.com)',
    ]

    results: list[base.SearchResult] = []
    seen: set[str] = set()
    for query in queries:
        for item in await self.searcher.search(query):
            url = base.normalize_url(item.url)
            if not url or url in seen:
                continue
            if not result_matches_name(candidate.full_name, item):
                continue
            if not result_is_personal(candidate.full_name, item):
                continue
            seen.add(url)
            results.append(item)

    resources: dict[str, str] = {}
    evidence_urls = list(candidate.evidence_urls)
    evidence_texts: list[str] = []

    for raw_url in candidate.evidence_urls:
        kind = base.resource_kind(raw_url)
        if kind and is_personal_resource_url(kind, raw_url):
            resources.setdefault(kind, base.clean_url(raw_url))

    for item in results:
        kind = base.resource_kind(item.url)
        if kind:
            if is_personal_resource_url(kind, item.url):
                resources.setdefault(kind, base.clean_url(item.url))
        elif base.safe_url(item.url) and not is_generic_external_url(item.url):
            resources.setdefault("website", base.clean_url(item.url))
        evidence_urls.append(item.url)
        evidence_texts.append(f"{item.title}. {item.description}")

    telegram = ""
    seed_urls = list(resources.values())
    for raw_url in candidate.evidence_urls:
        kind = base.resource_kind(raw_url)
        if kind and is_personal_resource_url(kind, raw_url):
            seed_urls.append(raw_url)

    queue = [(url, 0) for url in seed_urls if base.should_crawl(url)]
    visited: set[str] = set()
    pages = 0

    while queue and pages < self.config.pages_per_person:
        url, depth = queue.pop(0)
        normalized = base.normalize_url(url)
        if not normalized or normalized in visited:
            continue
        visited.add(normalized)

        page = await self._fetch(url)
        if not page:
            continue
        final_url, text, links = page
        pages += 1

        if not page_mentions_person(candidate.full_name, text):
            logger.debug(
                "people_page_identity_mismatch person=%r url=%s",
                candidate.full_name,
                final_url,
            )
            continue

        evidence_urls.append(final_url)
        if company.casefold() in text.casefold():
            evidence_texts.append(text[:3500])

        if not telegram:
            tg = telegram_from_text(text)
            if tg:
                telegram = tg
                resources.setdefault("telegram", tg)

        for href in links:
            target = base.normalize_url(urljoin(final_url, href))
            if not target:
                continue

            if tg := base.telegram_from_url(target):
                if telegram_is_personal_candidate(tg):
                    telegram = telegram or tg
                    resources.setdefault("telegram", tg)
                continue

            kind = base.resource_kind(target)
            if kind:
                if is_personal_resource_url(kind, target):
                    resources.setdefault(kind, base.clean_url(target))
                    if depth < self.config.crawl_depth and base.should_crawl(target):
                        queue.append((target, depth + 1))
                continue

            if is_generic_external_url(target):
                continue

            if depth < self.config.crawl_depth and base.likely_contact_or_external(
                final_url, target
            ):
                resources.setdefault("website", base.clean_url(target))
                if base.should_crawl(target):
                    queue.append((target, depth + 1))

    if not telegram:
        queries = [
            f'{base.quote(candidate.full_name)} {base.quote(company)} Telegram',
            f'{base.quote(candidate.full_name)} {base.quote(company)} t.me',
        ]
        for query in queries:
            found = False
            for item in await self.searcher.search(query):
                if not result_matches_name(candidate.full_name, item):
                    continue
                if result_has_merged_people_noise(item):
                    continue

                tg = base.telegram_from_url(item.url)
                if not tg and result_is_clean_person_result(candidate.full_name, item):
                    tg = telegram_from_text(f"{item.title} {item.description}")
                if not tg or not telegram_is_personal_candidate(tg):
                    continue

                telegram = tg
                resources.setdefault("telegram", tg)
                evidence_urls.append(item.url)
                found = True
                break
            if found:
                break

    status, note = base.best_status(
        company,
        candidate.employment_status,
        evidence_texts,
    )
    title = candidate.title or base.first_title(candidate.full_name, company, results)
    if is_generic_title(title):
        title = ""

    resource_list = tuple(
        base.ResourceLink(kind, url)
        for kind, url in base.sorted_resources(resources)
        if resource_is_safe_for_output(kind, url)
    )

    if not [x for x in resource_list if x.kind != "telegram"] and not telegram:
        return None

    return base.PersonProfile(
        full_name=candidate.full_name,
        title=title or "не удалось определить",
        employment_status=status,
        employment_note=note,
        telegram=telegram,
        resources=resource_list,
        evidence_urls=tuple(base.dedupe_urls(evidence_urls)),
    )


def apply_strict_patch() -> None:
    if getattr(base, "_STRICT_PEOPLE_PATCH_APPLIED", False):
        return

    base.company_key = company_key
    base.looks_like_name = looks_like_name
    base.names_from_title = names_from_title
    base.result_matches_name = result_matches_name
    base.telegram_from_text = telegram_from_text

    base.PeopleEnricher._candidates_heuristic = _strict_candidates_heuristic  # type: ignore[method-assign]
    base.PeopleEnricher._enrich_person = _strict_enrich_person  # type: ignore[method-assign]

    setattr(base, "_STRICT_PEOPLE_PATCH_APPLIED", True)


apply_strict_patch()

PeopleEnricher = base.PeopleEnricher
PersonProfile = base.PersonProfile
format_people_messages = base.format_people_messages
