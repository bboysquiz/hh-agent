from __future__ import annotations

import re


DEFAULT_STOP_WORDS = (
    "lead",
    "архитектор",
    "руководитель",
    "стажер",
    "intern",
    "trainee",
    "менеджер",
    "дизайнер",
    "маркетолог",
    "риелтор",
)

# These roles/stacks are deterministically outside the user's target search.
# They are checked before the LLM so a model cannot promote a Golang/Product/
# Backend vacancy merely because Vue/JavaScript is mentioned somewhere in the
# description (for example as another team's stack).
NON_TARGET_TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("golang", re.compile(r"\bgolang\b|\bgo\s*[-/]?\s*(?:developer|engineer|разработчик)\b", re.I)),
    ("backend", re.compile(r"\bback\s*[- ]?end\b|\bbackend\b|\bбэкенд\b|\bбекенд\b", re.I)),
    ("fullstack", re.compile(r"\bfull\s*[- ]?stack\b|\bфуллст[еэ]к\b", re.I)),
    ("php", re.compile(r"\bphp\b", re.I)),
    ("python", re.compile(r"\bpython\b", re.I)),
    ("java", re.compile(r"\bjava\b", re.I)),
    ("kotlin", re.compile(r"\bkotlin\b", re.I)),
    ("dotnet", re.compile(r"\b\.net\b|\bdotnet\b|\bc#\b", re.I)),
    ("ruby", re.compile(r"\bruby\b|\brails\b", re.I)),
    ("1c", re.compile(r"\b1[сc]\b|\b1[сc][\s:-]", re.I)),
    ("bitrix", re.compile(r"\bbitrix\b|\bбитрикс\b", re.I)),
    ("devops", re.compile(r"\bdevops\b|\bsre\b", re.I)),
    ("qa", re.compile(r"\bqa\b|\bquality assurance\b|\bтестировщик\b|\btesting engineer\b", re.I)),
    ("mobile", re.compile(r"\bandroid\b|\bios\b|\bflutter\b|\breact native\b|\bmobile developer\b|\bмобильн\w*\s+разработ", re.I)),
    ("product owner", re.compile(r"\bproduct\s+(?:area\s+)?owner\b|\bproduct\s+manager\b|\bпродакт\b|\bвладелец\s+продукта\b", re.I)),
    ("project manager", re.compile(r"\bproject\s+(?:owner|manager)\b|\bproject\s+lead\b|\bпроектн\w*\s+менеджер\b", re.I)),
    ("analyst", re.compile(r"\banalyst\b|\bаналитик\b", re.I)),
    ("data", re.compile(r"\bdata\s+(?:engineer|scientist|analyst)\b|\bmachine learning\b|\bml engineer\b", re.I)),
    ("support", re.compile(r"\bsupport\s+(?:engineer|specialist)\b|\bтехподдержк\w*\b", re.I)),
)

# A title must itself look like a frontend/web role in the user's target stack.
# Description-level checks in VacancyAnalyzer remain a second gate.
TARGET_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bfront\s*[- ]?end\b|\bfrontend\b|\bфронт\s*[- ]?енд\b|\bфронтенд\b|\bфронтэнд\b", re.I),
    re.compile(r"\bvue(?:\.js|js)?\b", re.I),
    re.compile(r"\bnuxt(?:\.js|js)?\b", re.I),
    re.compile(r"\bquasar\b", re.I),
    re.compile(r"\bjavascript\b|\btypescript\b", re.I),
    re.compile(r"(?:^|[^A-Za-z0-9])(?:js|ts)(?:[^A-Za-z0-9]|$)", re.I),
    re.compile(r"\bweb\s*[- ]?(?:developer|engineer|разработчик)\b|\bвеб\s*[- ]?разработчик\b", re.I),
    re.compile(r"\b(?:ui|интерфейс\w*)\s*[- ]?(?:developer|engineer|разработчик)\b", re.I),
)


def title_rejection_reason(
    title: str, excluded_positions: tuple[str, ...]
) -> str | None:
    normalized = " ".join(title.casefold().split())

    configured_or_default = next(
        (
            word
            for word in (*DEFAULT_STOP_WORDS, *excluded_positions)
            if word.casefold() in normalized
        ),
        None,
    )
    if configured_or_default:
        return configured_or_default

    for reason, pattern in NON_TARGET_TITLE_PATTERNS:
        if pattern.search(title):
            return reason

    if not any(pattern.search(title) for pattern in TARGET_TITLE_PATTERNS):
        return "not_vue_frontend_role"

    return None
