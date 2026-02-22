from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

BASE_URL = "https://manifest.morrenus.xyz/api/v1"


class ManifestApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class GameSearchResult:
    game_id: str
    game_name: str
    manifest_available: bool


def _safe_json_value(v: Any) -> Any:
    # Avoid surprising types coming from json decoding
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return v


async def _read_error_text(resp: aiohttp.ClientResponse) -> str:
    try:
        return (await resp.text())[:2000]
    except Exception:
        return "<failed to read error body>"


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, Any],
    timeout_s: float = 30.0,
    retries: int = 2,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with session.get(url, params=params, timeout=timeout) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json()
                    except Exception as e:
                        body = await _read_error_text(resp)
                        raise ManifestApiError(f"Failed to parse JSON: {e}; body={body}") from e
                    if not isinstance(data, dict):
                        raise ManifestApiError("Unexpected JSON shape (expected object).")
                    return data

                body = await _read_error_text(resp)

                # Retry transient errors only.
                if resp.status >= 500 and attempt < retries:
                    await asyncio.sleep(0.6 * (2**attempt))
                    continue

                raise ManifestApiError(f"HTTP {resp.status} from API: {body}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_err = e
            if attempt < retries:
                await asyncio.sleep(0.6 * (2**attempt))
                continue
            raise ManifestApiError(f"API request failed: {e}") from e

    # Should be unreachable, but keep mypy happy.
    raise ManifestApiError(f"API request failed: {last_err}")


async def get_user_stats(session: aiohttp.ClientSession, api_key: str) -> dict[str, Any]:
    url = f"{BASE_URL}/user/stats"
    return await _get_json(session, url, params={"api_key": api_key})


async def get_status(session: aiohttp.ClientSession, api_key: str, app_id: str) -> dict[str, Any]:
    url = f"{BASE_URL}/status/{app_id}"
    return await _get_json(session, url, params={"api_key": api_key})


async def search_games(
    session: aiohttp.ClientSession,
    api_key: str,
    query: str,
    *,
    limit: int = 10,
) -> list[GameSearchResult]:
    url = f"{BASE_URL}/search"
    data = await _get_json(session, url, params={"api_key": api_key, "q": query})

    results = data.get("results")
    if not isinstance(results, list):
        return []

    parsed: list[GameSearchResult] = []
    for item in results[: max(0, limit)]:
        if not isinstance(item, dict):
            continue
        game_id = item.get("game_id")
        game_name = item.get("game_name")
        manifest_available = item.get("manifest_available")
        if isinstance(game_id, str) and isinstance(game_name, str) and isinstance(manifest_available, bool):
            parsed.append(
                GameSearchResult(
                    game_id=game_id,
                    game_name=game_name,
                    manifest_available=manifest_available,
                )
            )
    return parsed


async def download_manifest_to_path(
    session: aiohttp.ClientSession,
    api_key: str,
    app_id: str,
    out_path: str,
    *,
    max_bytes: Optional[int] = None,
    timeout_s: float = 300.0,
    retries: int = 2,
) -> int:
    """
    Downloads /manifest/<app_id> to out_path.
    Returns bytes written.
    """
    url = f"{BASE_URL}/manifest/{app_id}"
    timeout = aiohttp.ClientTimeout(total=timeout_s)

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        bytes_written = 0
        try:
            async with session.get(url, params={"api_key": api_key}, timeout=timeout) as resp:
                if resp.status != 200:
                    body = await _read_error_text(resp)
                    # Retry only server errors for downloads.
                    if resp.status >= 500 and attempt < retries:
                        await asyncio.sleep(0.8 * (2**attempt))
                        continue
                    raise ManifestApiError(f"HTTP {resp.status} while downloading: {body}")

                # Stream to disk to avoid keeping the whole file in memory.
                with open(out_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        if not chunk:
                            continue
                        bytes_written += len(chunk)
                        if max_bytes is not None and bytes_written > max_bytes:
                            raise ManifestApiError(
                                f"Manifest is too large to upload (>{max_bytes} bytes)."
                            )
                        f.write(chunk)

            # Let the event loop breathe a little after a potentially big stream.
            await asyncio.sleep(0)
            return bytes_written
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_err = e
            if attempt < retries:
                await asyncio.sleep(0.8 * (2**attempt))
                continue
            raise ManifestApiError(f"Download failed: {e}") from e

    raise ManifestApiError(f"Download failed: {last_err}")

