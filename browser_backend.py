from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from config import Settings


Launcher = Callable[..., Awaitable[Any]]


class BrowserLaunchError(RuntimeError):
    pass


class BrowserBackend(Protocol):
    async def start(self) -> Any: ...

    async def close(self) -> None: ...


class CloakBrowserBackend:
    """
    Compatibility stub.

    CloakBrowser is intentionally disabled in this hardened branch.
    The class is kept only so the existing project structure and tests
    that inject a fake launcher do not break.
    """

    def __init__(
        self,
        profile_dir: Path,
        headless: bool,
        launcher: Launcher | None = None,
    ):
        self.profile_dir = profile_dir
        self.headless = headless
        self._launcher = launcher
        self._context: Any = None

    async def start(self) -> Any:
        if self._context is not None:
            return self._context

        if self._launcher is None:
            raise BrowserLaunchError(
                "CloakBrowser is disabled in this hardened branch. "
                "Set BROWSER_BACKEND=playwright."
            )

        try:
            self.profile_dir.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )

            if os.name != "nt":
                self.profile_dir.chmod(0o700)

            self._context = await self._launcher(
                self.profile_dir,
                headless=self.headless,
            )
            return self._context

        except BrowserLaunchError:
            raise

        except Exception as exc:
            raise BrowserLaunchError(
                "CloakBrowser failed to start: "
                f"{exc}. Set BROWSER_BACKEND=playwright; "
                "CloakBrowser is disabled in this hardened branch."
            ) from exc

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None


class PlaywrightBrowserBackend:
    """
    Hardened browser backend based only on the Chromium build
    supplied by Playwright.

    Security properties:
    - no CloakBrowser;
    - no third-party Chromium executable;
    - Chromium sandbox is explicitly enabled;
    - persistent profile is isolated in BROWSER_PROFILE_DIR;
    - no fallback to an unsandboxed browser;
    - no custom stealth browser command-line flags.
    """

    def __init__(
        self,
        profile_dir: Path,
        headless: bool,
        launcher: Launcher | None = None,
    ):
        self.profile_dir = profile_dir
        self.headless = headless
        self._launcher = launcher
        self._context: Any = None
        self._playwright: Any = None

    async def _launch(
        self,
        profile_dir: Path,
        *,
        headless: bool,
    ) -> Any:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()

        try:
            context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,

                # SECURITY:
                # Playwright disables Chromium sandbox by default.
                # We explicitly enable it and NEVER retry without it.
                chromium_sandbox=True,
            )

            return context

        except Exception:
            await self._playwright.stop()
            self._playwright = None
            raise

    async def start(self) -> Any:
        if self._context is not None:
            return self._context

        try:
            self.profile_dir.mkdir(
                parents=True,
                exist_ok=True,
                mode=0o700,
            )

            if os.name != "nt":
                self.profile_dir.chmod(0o700)

            launcher = self._launcher or self._launch

            self._context = await launcher(
                self.profile_dir,
                headless=self.headless,
            )

            return self._context

        except Exception as exc:
            raise BrowserLaunchError(
                "Secure Playwright Chromium failed to start. "
                "The browser was NOT restarted without sandbox. "
                f"Original error: {exc}"
            ) from exc

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None

        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


def create_browser_backend(settings: Settings) -> BrowserBackend:
    if settings.browser_backend == "playwright":
        return PlaywrightBrowserBackend(
            settings.browser_profile_dir,
            settings.browser_headless,
        )

    if settings.browser_backend == "cloakbrowser":
        return CloakBrowserBackend(
            settings.browser_profile_dir,
            settings.browser_headless,
        )

    raise ValueError(
        f"Unknown browser backend: {settings.browser_backend}"
    )