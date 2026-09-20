from __future__ import annotations

import logging
from typing import Any

import main as hh_main
from people_enrichment_strict import PeopleEnricher, format_people_messages
from tg_bot import TelegramService


logger = logging.getLogger(__name__)

_ORIGINAL_SEND_PREVIEW = TelegramService.send_preview
_ORIGINAL_STOP = TelegramService.stop
_ORIGINAL_COMMAND_HANDLER = TelegramService._command_handler
_PATCHED = False


def _split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    """Split long plain-text Telegram responses without cutting normal lines."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            for start in range(0, len(line), limit):
                part = line[start : start + limit].rstrip("\n")
                if part:
                    chunks.append(part)
            continue

        if current and len(current) + len(line) > limit:
            chunks.append(current.rstrip("\n"))
            current = line
        else:
            current += line

    if current:
        chunks.append(current.rstrip("\n"))

    return chunks or [text[:limit]]


async def _command_handler_with_long_diagnostics(
    self: TelegramService,
    message: Any,
) -> None:
    """Keep stock command handling, but split an oversized /diagnostics reply."""
    name = (message.text or "").split()[0].lstrip("/").split("@")[0]
    if name != "diagnostics":
        await _ORIGINAL_COMMAND_HANDLER(self, message)
        return

    if not self.authorized(message.from_user.id):
        await message.answer("This bot is private.")
        return

    text = self.command(name, message.from_user.id)
    for chunk in _split_telegram_text(text):
        await message.answer(chunk)


async def _send_preview_with_people(
    self: TelegramService,
    vacancy: Any,
    include_actions: bool,
) -> None:
    # First send the normal vacancy card immediately.
    await _ORIGINAL_SEND_PREVIEW(self, vacancy, include_actions)

    enricher: PeopleEnricher | None = getattr(self, "_people_enricher", None)
    if enricher is None:
        enricher = PeopleEnricher(self.settings, self.database)
        setattr(self, "_people_enricher", enricher)

    if not enricher.available:
        if not getattr(self, "_people_enrichment_config_warned", False):
            logger.warning(
                "people_enrichment_disabled: set PEOPLE_ENRICHMENT_ENABLED=true"
            )
            setattr(self, "_people_enrichment_config_warned", True)
        return

    if enricher.report_was_sent(vacancy.id):
        return

    try:
        people = await enricher.enrich_company(vacancy.company)
        messages = format_people_messages(vacancy.title, vacancy.company, people)
        for text in messages:
            await self.bot.send_message(
                chat_id=self.settings.tg_user_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        enricher.mark_report_sent(vacancy.id)
    except Exception as exc:
        # Enrichment failures must never break the existing HH -> Telegram flow.
        logger.exception(
            "people_enrichment_failed job_id=%s company=%r error=%s",
            vacancy.id,
            vacancy.company,
            type(exc).__name__,
        )


async def _stop_with_people(self: TelegramService) -> None:
    enricher: PeopleEnricher | None = getattr(self, "_people_enricher", None)
    if enricher is not None:
        try:
            await enricher.close()
        except Exception as exc:
            logger.warning("people_enrichment_close_failed error=%s", type(exc).__name__)
    await _ORIGINAL_STOP(self)


def install_people_enrichment() -> None:
    global _PATCHED
    if _PATCHED:
        return

    TelegramService._command_handler = _command_handler_with_long_diagnostics  # type: ignore[method-assign]
    TelegramService.send_preview = _send_preview_with_people  # type: ignore[method-assign]
    TelegramService.stop = _stop_with_people  # type: ignore[method-assign]
    _PATCHED = True


install_people_enrichment()


if __name__ == "__main__":
    raise SystemExit(hh_main.cli())
