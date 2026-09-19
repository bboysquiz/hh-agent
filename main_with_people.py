from __future__ import annotations

import logging
from typing import Any

import main as hh_main
from people_enrichment import PeopleEnricher, format_people_messages
from tg_bot import TelegramService


logger = logging.getLogger(__name__)

_ORIGINAL_SEND_PREVIEW = TelegramService.send_preview
_ORIGINAL_STOP = TelegramService.stop
_PATCHED = False


async def _send_preview_with_people(
    self: TelegramService,
    vacancy: Any,
    include_actions: bool,
) -> None:
    # Сначала отправляем штатную карточку вакансии без задержки.
    await _ORIGINAL_SEND_PREVIEW(self, vacancy, include_actions)

    enricher: PeopleEnricher | None = getattr(self, "_people_enricher", None)
    if enricher is None:
        enricher = PeopleEnricher(self.settings, self.database)
        setattr(self, "_people_enricher", enricher)

    if not enricher.available:
        if not getattr(self, "_people_enrichment_config_warned", False):
            logger.warning(
                "people_enrichment_disabled: "
                "set PEOPLE_ENRICHMENT_ENABLED=true"
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
        # Ошибка enrichment не должна ломать текущий HH -> Telegram workflow.
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
    TelegramService.send_preview = _send_preview_with_people  # type: ignore[method-assign]
    TelegramService.stop = _stop_with_people  # type: ignore[method-assign]
    _PATCHED = True


install_people_enrichment()


if __name__ == "__main__":
    raise SystemExit(hh_main.cli())
