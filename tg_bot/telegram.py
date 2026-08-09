from typing import Any

import httpx

from tg_bot.text import truncate_text


class TelegramAPIError(RuntimeError):
    def __init__(
        self,
        method: str,
        status_code: int,
        description: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(
            f"Telegram API {method} failed with HTTP "
            f"{status_code}: {truncate_text(description, 300)}"
        )
        self.method = method
        self.status_code = status_code
        self.description = description
        self.retry_after = retry_after


async def request(
    client: httpx.AsyncClient | None,
    api_base: str,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=20) as temporary_client:
                response = await temporary_client.post(
                    f"{api_base}/{method}",
                    json=payload,
                )
        else:
            response = await client.post(f"{api_base}/{method}", json=payload)
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Telegram API {method} request failed ({type(exc).__name__})"
        ) from None

    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.is_error or not isinstance(data, dict) or not data.get("ok", False):
        description = (
            str(data.get("description", "unexpected response"))
            if isinstance(data, dict)
            else "unexpected response"
        )
        parameters = data.get("parameters") if isinstance(data, dict) else None
        raw_retry_after = (
            parameters.get("retry_after") if isinstance(parameters, dict) else None
        )
        retry_after = (
            int(raw_retry_after)
            if isinstance(raw_retry_after, int) and raw_retry_after >= 0
            else None
        )
        raise TelegramAPIError(
            method,
            response.status_code,
            description,
            retry_after=retry_after,
        ) from None
    return data
