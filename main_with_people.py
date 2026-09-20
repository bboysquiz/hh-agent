from __future__ import annotations

from typing import Any

import main as hh_main
import cover_letter_compat  # noqa: F401  # local-model cover-letter compatibility
import hh_dom_compat  # noqa: F401  # robust replay parser for current/archived HH DOM
import hh_replay_fallback  # noqa: F401  # API fallback after DOM compatibility fallback
from tg_bot import TelegramService


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


def install_runtime_compat() -> None:
    global _PATCHED
    if _PATCHED:
        return

    TelegramService._command_handler = _command_handler_with_long_diagnostics  # type: ignore[method-assign]
    _PATCHED = True


install_runtime_compat()


if __name__ == "__main__":
    raise SystemExit(hh_main.cli())
