from __future__ import annotations

"""Compatibility patch for local-model cover letter generation.

Some local models (notably Qwen variants) may echo contact placeholders even
when exact candidate contacts are present in the prompt. The application
already appends a canonical signature in Python, so the model should generate
only the body of the letter.

This patch strengthens the cover-letter instruction and safely resolves only
known contact placeholders before the stock validator runs. Unknown/template
placeholders are still rejected by the original validator.
"""

import re

from ai_analyzer import (
    CANDIDATE_EMAIL,
    CANDIDATE_FULL_NAME,
    CANDIDATE_PHONE,
    CANDIDATE_TELEGRAM,
    VacancyAnalyzer,
)

_ORIGINAL_REQUEST = VacancyAnalyzer._request
_ORIGINAL_SAFE_LETTER = VacancyAnalyzer._safe_letter
_PATCHED = False


_CONTACT_PLACEHOLDER_SUBSTITUTIONS: tuple[tuple[str, str], ...] = (
    (r"\[\s*(?:ваше\s+имя|имя|фио)\s*\]", CANDIDATE_FULL_NAME),
    (
        r"\[\s*(?:телефон|номер\s+телефона|ваш\s+телефон)\s*\]",
        CANDIDATE_PHONE,
    ),
    (r"\[\s*(?:email|e-mail|почта|ваш\s+email|ваш\s+e-mail)\s*\]", CANDIDATE_EMAIL),
    (r"\[\s*(?:telegram|телеграм|ваш\s+telegram|ваш\s+телеграм)\s*\]", CANDIDATE_TELEGRAM),
)


def _request_body_only(
    self: VacancyAnalyzer,
    *,
    system_instructions: str,
    payload: dict[str, object],
    operation: str,
    structured: bool,
):
    if operation == "cover_letter":
        system_instructions = (
            system_instructions
            + "\n\nCRITICAL SIGNATURE RULE: Generate ONLY the body of the cover letter. "
            "Do not output a signature, candidate name, phone, email, Telegram, "
            "portfolio URL, contact block, labels for contact fields, or any "
            "contact placeholders. End after the final closing paragraph. "
            "The application will append the exact verified signature and "
            "contact details programmatically after validation."
        )

    return _ORIGINAL_REQUEST(
        self,
        system_instructions=system_instructions,
        payload=payload,
        operation=operation,
        structured=structured,
    )


def _safe_letter_with_known_contact_placeholders(
    self: VacancyAnalyzer,
    raw: str,
) -> str:
    cleaned = raw

    # Remove a generic placeholder-only contact line. The application appends
    # the canonical contact block itself, so no information is lost.
    cleaned = re.sub(
        r"(?im)^\s*(?:контакты\s*:\s*)?\[\s*ваши\s+данные\s*\]\s*$\n?",
        "",
        cleaned,
    )

    for pattern, replacement in _CONTACT_PLACEHOLDER_SUBSTITUTIONS:
        cleaned = re.sub(
            pattern,
            lambda _match, value=replacement: value,
            cleaned,
            flags=re.IGNORECASE,
        )

    return _ORIGINAL_SAFE_LETTER(self, cleaned)


def install_cover_letter_compat() -> None:
    global _PATCHED
    if _PATCHED:
        return

    VacancyAnalyzer._request = _request_body_only  # type: ignore[method-assign]
    VacancyAnalyzer._safe_letter = _safe_letter_with_known_contact_placeholders  # type: ignore[method-assign]
    _PATCHED = True


install_cover_letter_compat()
