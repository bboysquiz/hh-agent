from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from llm.errors import (
    LLMError,
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMTransientError,
    error_for_http_status,
)
from llm.types import LLMRequest, LLMResponse


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        url: str,
        session: Any = None,
    ):
        self.url = url
        self._session = session
        self._owns_session = (
            session is None
        )

    def _get_session(self) -> Any:
        if self._session is None:
            self._session = (
                aiohttp.ClientSession()
            )

        return self._session

    async def complete(
        self,
        request: LLMRequest,
    ) -> LLMResponse:
        options: dict[str, object] = {
            "temperature": (
                request.temperature
            ),
        }

        # Для обычных операций лимит вывода сохраняем.
        #
        # Для cover_letter num_predict намеренно
        # НЕ передаём вообще. Ollama сама завершает
        # генерацию, когда модель выдаёт естественный
        # конец ответа.
        if request.operation != "cover_letter":
            options["num_predict"] = (
                request.max_output_tokens
            )

        payload: dict[str, object] = {
            "model": request.model,
            "system": (
                request.system_instructions
            ),
            "prompt": (
                request.user_content
            ),
            "stream": False,

            # Для Qwen3 reasoning здесь не нужен.
            "think": False,

            "options": options,
        }

        if request.json_schema is not None:
            payload["format"] = (
                request.json_schema
            )

        started = time.perf_counter()

        try:
            async with (
                self._get_session().post(
                    self.url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(
                        total=request.timeout_seconds
                    ),
                )
            ) as response:
                if not (
                    200 <= response.status < 300
                ):
                    raise error_for_http_status(
                        response.status,
                        response.headers.get(
                            "Retry-After"
                        ),
                    )

                try:
                    data = (
                        await response.json()
                    )
                except (
                    TypeError,
                    ValueError,
                ) as exc:
                    raise (
                        LLMInvalidResponseError()
                    ) from exc

        except LLMError:
            raise

        except (
            asyncio.TimeoutError,
            TimeoutError,
        ) as exc:
            raise LLMTimeoutError() from exc

        except aiohttp.ClientError as exc:
            raise LLMTransientError() from exc

        if not isinstance(data, dict):
            raise LLMInvalidResponseError()

        response_text = data.get(
            "response"
        )

        if not isinstance(
            response_text,
            str,
        ):
            raise LLMInvalidResponseError()

        if not response_text.strip():
            raise LLMInvalidResponseError()

        return LLMResponse(
            text=response_text,
            provider=self.name,
            model=(
                data.get("model")
                if isinstance(
                    data.get("model"),
                    str,
                )
                else request.model
            ),
            latency_ms=round(
                (
                    time.perf_counter()
                    - started
                )
                * 1000
            ),
            input_tokens=(
                data.get(
                    "prompt_eval_count"
                )
                if isinstance(
                    data.get(
                        "prompt_eval_count"
                    ),
                    int,
                )
                else None
            ),
            output_tokens=(
                data.get("eval_count")
                if isinstance(
                    data.get(
                        "eval_count"
                    ),
                    int,
                )
                else None
            ),
            finish_reason=(
                data.get("done_reason")
                if isinstance(
                    data.get(
                        "done_reason"
                    ),
                    str,
                )
                else None
            ),
        )

    async def close(self) -> None:
        if (
            self._owns_session
            and self._session is not None
        ):
            await self._session.close()
            self._session = None