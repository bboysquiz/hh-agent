from __future__ import annotations

"""Compatibility layer for DDGS search backends used by people enrichment.

DDGS backend names change between releases. Older configuration used
``yandex,bing``; current DDGS releases may reject those names and silently
fall back to ``auto``, which fans out to rate-limited/captcha-prone engines.

This patch sanitizes configured backends before DdgsSearch starts and prefers
only free backends that are currently useful for public-profile discovery.
"""

import logging
from dataclasses import replace

import people_enrichment as base

logger = logging.getLogger(__name__)

# Backends advertised by the currently supported DDGS generation. We prefer
# DuckDuckGo and Yahoo because the others commonly require API keys or return
# CAPTCHA/rate-limit pages in this workflow.
SUPPORTED_BACKENDS = {
    "brave",
    "duckduckgo",
    "google",
    "grokipedia",
    "mojeek",
    "startpage",
    "wikipedia",
    "yahoo",
}
PREFERRED_BACKENDS = ("duckduckgo", "yahoo")

_ORIGINAL_INIT = base.DdgsSearch.__init__
_PATCHED = False


def _ddgs_init_with_supported_backends(
    self: base.DdgsSearch,
    config: base.Config,
) -> None:
    requested = tuple(
        item.strip().casefold()
        for item in config.search_backends
        if item and item.strip()
    )
    valid = tuple(item for item in requested if item in SUPPORTED_BACKENDS)
    invalid = tuple(item for item in requested if item not in SUPPORTED_BACKENDS)

    if invalid:
        logger.warning(
            "people_search_unsupported_backends ignored=%s supported=%s",
            ",".join(invalid),
            ",".join(sorted(SUPPORTED_BACKENDS)),
        )

    if not valid:
        valid = PREFERRED_BACKENDS
        logger.warning(
            "people_search_backends_fallback configured=%s fallback=%s",
            ",".join(requested) or "<empty>",
            ",".join(valid),
        )

    if valid != config.search_backends:
        config = replace(config, search_backends=valid)

    _ORIGINAL_INIT(self, config)
    logger.info(
        "people_search_backends active=%s",
        ",".join(self.config.search_backends),
    )


def install_people_search_compat() -> None:
    global _PATCHED
    if _PATCHED:
        return

    base.DdgsSearch.__init__ = _ddgs_init_with_supported_backends  # type: ignore[method-assign]

    # primp logs every low-level HTTP request at INFO and makes the normal
    # agent log unreadable. Warnings/errors remain visible.
    logging.getLogger("primp").setLevel(logging.WARNING)

    _PATCHED = True


install_people_search_compat()
