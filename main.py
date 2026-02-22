from __future__ import annotations

import asyncio
import os
import tempfile
import time
from typing import Optional

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

import manifest_api
from fallback_providers import load_providers_from_api_json
from utils import extract_steam_app_id, fmt_bytes, is_digits, safe_filename


def _env(name: str, *, default: Optional[str] = None, required: bool = False) -> str:
    v = os.getenv(name, default)
    if required and (v is None or not str(v).strip()):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return str(v) if v is not None else ""


COMMAND_PREFIX = _env("COMMAND_PREFIX", default=".")
MANIFEST_API_KEY = _env("MANIFEST_API_KEY", required=True)
DISCORD_TOKEN = _env("DISCORD_TOKEN", required=True)

# Discord attachment limits vary; free is commonly 8MB, but many servers/users have 25MB.
# Default to 25MB but allow overriding in Render env vars.
MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", default="25"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

# Optional denylist (comma-separated user IDs). Neutral response only.
BLOCKED_USER_IDS = {
    s.strip()
    for s in _env("BLOCKED_USER_IDS", default="").split(",")
    if s.strip()
}

# Load ltsteamplugin-style fallback manifest providers (if present).
FALLBACK_API_JSON_PATH = _env(
    "FALLBACK_API_JSON_PATH",
    default="api.json",
)
FALLBACK_PROVIDERS = load_providers_from_api_json(FALLBACK_API_JSON_PATH)

# Last-resort data source: ManifestHub depotkeys.json (large mapping).
DEPOTKEYS_FALLBACK_URL = (
    "https://raw.githubusercontent.com/SteamAutoCracks/ManifestHub/refs/heads/main/depotkeys.json"
)

# Simple in-memory cache so we don't re-download on every miss.
_DEPOTKEYS_CACHE: dict[str, object] = {"fetched_at": 0.0, "data": None}


async def _get_depotkeys_map(session: aiohttp.ClientSession) -> Optional[dict[str, str]]:
    """
    Fetch and cache ManifestHub depotkeys.json.
    Returns a mapping of string->string (IDs -> 64-hex key).
    """
    now = time.time()
    cached_at = float(_DEPOTKEYS_CACHE.get("fetched_at") or 0.0)
    cached = _DEPOTKEYS_CACHE.get("data")
    if isinstance(cached, dict) and (now - cached_at) < 6 * 60 * 60:
        # Cache for 6h; it's a big file and changes relatively slowly.
        return cached  # type: ignore[return-value]

    timeout = aiohttp.ClientTimeout(total=60.0)
    try:
        async with session.get(
            DEPOTKEYS_FALLBACK_URL,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://github.com/"},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    # Sanitize to dict[str, str] only.
    cleaned: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str):
            cleaned[k] = v

    _DEPOTKEYS_CACHE["data"] = cleaned
    _DEPOTKEYS_CACHE["fetched_at"] = now
    return cleaned


def _build_lua_for_appid(app_id: str, value: str) -> str:
    # Format requested by user. Keep it tiny and compatible with common Lua loaders.
    return f'addappid({int(app_id)})\naddappid({int(app_id)}, 1, "{value}")\n'


async def start_webserver() -> web.AppRunner:
    """
    Minimal HTTP server for Render + UptimeRobot pinging.
    """
    app = web.Application()

    async def root(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.add_routes([web.get("/", root), web.get("/healthz", health)])

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(_env("PORT", default="8080"))
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    return runner


intents = discord.Intents.default()
intents.message_content = True  # required for prefix commands like ".app"

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)


@bot.event
async def on_ready() -> None:
    # Avoid printing secrets; just basic info.
    print(f"Logged in as {bot.user} (id={bot.user.id})")


@bot.event
async def setup_hook() -> None:
    # Start a tiny web server so Render sees an open port.
    bot.http_runner = await start_webserver()  # type: ignore[attr-defined]


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Missing argument. Try `{COMMAND_PREFIX}app 400` or `{COMMAND_PREFIX}app Portal`.")
        return
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.BadArgument):
        await ctx.reply("Bad argument. Try an AppID or game name.")
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(f"Slow down. Try again in `{error.retry_after:.1f}s`.")
        return
    if isinstance(error, commands.MaxConcurrencyReached):
        await ctx.reply("You already have a request running. Wait for it to finish.")
        return
    # Fallback
    await ctx.reply(f"Error: {error}")


async def _prompt_choice(ctx: commands.Context, options: list[str], *, timeout_s: float = 30.0) -> Optional[int]:
    """
    Ask the user to choose 1..N by replying with a number.
    Returns chosen index (0-based) or None on timeout/cancel.
    """
    prompt = "Reply with a number:\n" + "\n".join(options) + "\n\n(or `cancel`)"
    await ctx.reply(prompt)

    def check(m: discord.Message) -> bool:
        if m.author.id != ctx.author.id:
            return False
        if m.channel.id != ctx.channel.id:
            return False
        c = (m.content or "").strip().lower()
        return c == "cancel" or (c.isdigit() and 1 <= int(c) <= len(options))

    try:
        msg = await bot.wait_for("message", check=check, timeout=timeout_s)
    except asyncio.TimeoutError:
        return None

    content = (msg.content or "").strip().lower()
    if content == "cancel":
        return None
    return int(content) - 1


async def _prompt_confirm(ctx: commands.Context, text: str, *, timeout_s: float = 30.0) -> bool:
    """
    Confirmation gate for name-based lookups (per your requirement).
    """
    await ctx.reply(f"{text}\n\nReply `yes` to confirm or `no` to cancel.")

    def check(m: discord.Message) -> bool:
        if m.author.id != ctx.author.id:
            return False
        if m.channel.id != ctx.channel.id:
            return False
        c = (m.content or "").strip().lower()
        return c in {"yes", "y", "no", "n"}

    try:
        msg = await bot.wait_for("message", check=check, timeout=timeout_s)
    except asyncio.TimeoutError:
        return False

    c = (msg.content or "").strip().lower()
    return c in {"yes", "y"}


async def _fetch_and_send_manifest(ctx: commands.Context, app_id: str, *, display_name: Optional[str] = None) -> None:
    def fallback_headers_for(url: str) -> dict[str, str]:
        # Some third-party providers return 403 unless the request looks browser-ish.
        headers: dict[str, str] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

        u = (url or "").lower()
        if "twentytwocloud.com" in u:
            headers["Referer"] = "https://twentytwocloud.com/"
            headers["Origin"] = "https://twentytwocloud.com"
        elif "raw.githubusercontent.com" in u or "githubusercontent.com" in u:
            headers["Referer"] = "https://github.com/"
        else:
            # Generic Steam-ish referer for plain appid endpoints.
            headers["Referer"] = f"https://store.steampowered.com/app/{app_id}/"
        return headers

    async with aiohttp.ClientSession() as session:
        async def try_send_depotkeys_lua(*, reason: str) -> bool:
            depot_map = await _get_depotkeys_map(session)
            if not isinstance(depot_map, dict):
                return False

            val = depot_map.get(str(app_id))
            if not isinstance(val, str) or not val.strip():
                return False

            tmp_dir = tempfile.gettempdir()
            out_lua_path = os.path.join(tmp_dir, f"depotkeys_{app_id}.lua")
            try:
                with open(out_lua_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(_build_lua_for_appid(app_id, val.strip()))

                await ctx.reply(
                    content=(
                        f"No manifest zip available for `{display_name or app_id}` (`{app_id}`). "
                        f"Sending Lua fallback from `depotkeys.json` ({reason})."
                    ),
                    file=discord.File(out_lua_path, filename=f"{app_id}.lua"),
                )
                return True
            finally:
                try:
                    if os.path.exists(out_lua_path):
                        os.remove(out_lua_path)
                except Exception:
                    pass

        status = None
        try:
            status = await manifest_api.get_status(session, MANIFEST_API_KEY, app_id)
        except Exception:
            # Status API is helpful for file info, but not required for download.
            status = None

        manifest_exists = None
        game_name = None
        file_size = None
        file_age_days = None
        needs_update = None
        if isinstance(status, dict):
            manifest_exists = bool(status.get("manifest_file_exists"))
            game_name = status.get("game_name") if isinstance(status.get("game_name"), str) else None
            file_size = status.get("file_size") if isinstance(status.get("file_size"), int) else None
            file_age_days = status.get("file_age_days") if isinstance(status.get("file_age_days"), (int, float)) else None
            needs_update = status.get("needs_update") if isinstance(status.get("needs_update"), bool) else None

        resolved_name = display_name or game_name or f"App {app_id}"

        # If Morrenus status says unavailable, it might still exist on a fallback provider.
        # Only hard-stop when we have no fallback providers configured.
        if manifest_exists is False and not FALLBACK_PROVIDERS:
            if await try_send_depotkeys_lua(reason="Morrenus status unavailable"):
                return
            await ctx.reply(f"No manifest file available for `{resolved_name}` (`{app_id}`).")
            return

        if file_size is not None and file_size > MAX_UPLOAD_BYTES:
            await ctx.reply(
                f"Manifest exists for `{resolved_name}` (`{app_id}`) but it's `{fmt_bytes(file_size)}` "
                f"and Discord upload limit is `{MAX_UPLOAD_MB} MB`.\n"
                f"Try raising `MAX_UPLOAD_MB` (if your server supports bigger uploads) or download it outside Discord."
            )
            return

        info_bits = []
        if file_size is not None:
            info_bits.append(f"size: {fmt_bytes(file_size)}")
        if file_age_days is not None:
            info_bits.append(f"age: {file_age_days:.1f}d")
        if needs_update is not None:
            info_bits.append(f"needs_update: {needs_update}")
        info = ", ".join(info_bits) if info_bits else "status: available"

        downloading_msg = await ctx.reply(f"Downloading `{resolved_name}` (`{app_id}`) ({info}) ...")

        # Use a temp file; discord.py wants a file-like object/path.
        tmp_dir = tempfile.gettempdir()
        base = safe_filename(resolved_name, fallback=f"app_{app_id}")
        out_path = os.path.join(tmp_dir, f"{base}_{app_id}.zip")

        try:
            provider_used = "Morrenus"
            try:
                bytes_written = await manifest_api.download_manifest_to_path(
                    session,
                    MANIFEST_API_KEY,
                    app_id,
                    out_path,
                    max_bytes=MAX_UPLOAD_BYTES,
                )
            except Exception:
                # Fallback providers from ltsteamplugin api.json (if any).
                bytes_written = 0
                ok = False
                attempts: list[str] = []
                attempted = 0
                unavailable = 0
                for p in FALLBACK_PROVIDERS:
                    # Avoid retrying the same provider we already attempted.
                    if p.name.strip().lower() == "morrenus":
                        continue

                    url = p.build_url(app_id=app_id, moapikey=MANIFEST_API_KEY)
                    if "<moapikey>" in p.url_template and not MANIFEST_API_KEY:
                        continue

                    try:
                        attempted += 1
                        timeout = aiohttp.ClientTimeout(total=300.0)
                        async with session.get(
                            url,
                            timeout=timeout,
                            allow_redirects=True,
                            headers=fallback_headers_for(url),
                        ) as resp:
                            if resp.status == p.unavailable_code:
                                unavailable += 1
                                attempts.append(f"{p.name}: unavailable ({resp.status})")
                                continue
                            if resp.status != p.success_code:
                                attempts.append(f"{p.name}: HTTP {resp.status}")
                                continue

                            provider_used = p.name
                            bytes_written = 0
                            with open(out_path, "wb") as f:
                                async for chunk in resp.content.iter_chunked(1024 * 64):
                                    if not chunk:
                                        continue
                                    bytes_written += len(chunk)
                                    if bytes_written > MAX_UPLOAD_BYTES:
                                        raise manifest_api.ManifestApiError(
                                            f"Manifest is too large to upload (>{MAX_UPLOAD_BYTES} bytes)."
                                        )
                                    f.write(chunk)
                            ok = True
                            break
                    except manifest_api.ManifestApiError as e:
                        # Size limit / explicit API error: surface to user.
                        await downloading_msg.edit(content=f"Download failed: {e}")
                        return
                    except asyncio.TimeoutError:
                        attempts.append(f"{p.name}: timeout")
                        continue
                    except aiohttp.ClientError:
                        attempts.append(f"{p.name}: network error")
                        continue
                    except Exception:
                        attempts.append(f"{p.name}: error")
                        continue

                if not ok:
                    if attempted == 0:
                        if await try_send_depotkeys_lua(reason="no fallback providers"):
                            await downloading_msg.delete()
                            return
                        await downloading_msg.edit(content="Download failed: no fallback providers configured.")
                        return

                    if unavailable == attempted:
                        if await try_send_depotkeys_lua(reason="fallback providers unavailable"):
                            await downloading_msg.delete()
                            return
                        await downloading_msg.edit(
                            content="Download failed: manifest not available on fallback providers."
                        )
                        return

                    details = "; ".join(attempts[:6])
                    if len(attempts) > 6:
                        details += " ..."
                    if await try_send_depotkeys_lua(reason="all providers failed"):
                        await downloading_msg.delete()
                        return
                    await downloading_msg.edit(content=f"Download failed from all providers. Details: {details}")
                    return

            await ctx.reply(
                content=f"`{resolved_name}` (`{app_id}`) manifest: `{fmt_bytes(bytes_written)}` (source: `{provider_used}`)",
                file=discord.File(out_path, filename=f"{base}_{app_id}.zip"),
            )
            await downloading_msg.delete()
        finally:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except Exception:
                # Best-effort cleanup.
                pass


@bot.command(name="app")
@commands.cooldown(2, 20, commands.BucketType.user)
@commands.max_concurrency(1, per=commands.BucketType.user, wait=False)
async def app_cmd(ctx: commands.Context, *, query: str) -> None:
    """
    .app <appid>
    .app <app name>   (asks for confirmation)
    """
    q = (query or "").strip()
    if not q:
        await ctx.reply(f"Usage: `{COMMAND_PREFIX}app 400` or `{COMMAND_PREFIX}app Portal`")
        return

    if str(ctx.author.id) in BLOCKED_USER_IDS:
        await ctx.reply("Poshel Nahuy Alik Huyalik pidar vanuchi.")
        return

    # URL path (Steam store/community): treat as AppID request.
    # Example: .app https://store.steampowered.com/app/2129530/REANIMAL/
    appid_from_url = extract_steam_app_id(q)
    if appid_from_url is not None:
        q = appid_from_url

    # App ID path
    if is_digits(q):
        await _fetch_and_send_manifest(ctx, q)
        return

    # Name path: search + require confirmation
    async with aiohttp.ClientSession() as session:
        try:
            results = await manifest_api.search_games(session, MANIFEST_API_KEY, q, limit=8)
        except manifest_api.ManifestApiError:
            await ctx.reply(
                "Search is not available right now. Use an AppID or Steam URL instead.\n"
                f"Example: `{COMMAND_PREFIX}app 400` or `{COMMAND_PREFIX}app https://store.steampowered.com/app/400/`"
            )
            return
        except Exception:
            await ctx.reply(
                "Search request failed (network error). Use an AppID or Steam URL instead.\n"
                f"Example: `{COMMAND_PREFIX}app 400`"
            )
            return

    if not results:
        await ctx.reply(f"No matches for `{q}`.")
        return

    # If multiple results, ask user to pick one.
    chosen = results[0]
    if len(results) > 1:
        options = []
        for i, r in enumerate(results[:5], start=1):
            avail = "available" if r.manifest_available else "no manifest"
            options.append(f"{i}. {r.game_name} ({r.game_id}) - {avail}")
        idx = await _prompt_choice(ctx, options, timeout_s=30.0)
        if idx is None:
            await ctx.reply("Cancelled (or timed out).")
            return
        chosen = results[idx]

    ok = await _prompt_confirm(
        ctx,
        f"Download manifest for `{chosen.game_name}` (`{chosen.game_id}`)?",
        timeout_s=30.0,
    )
    if not ok:
        await ctx.reply("Cancelled.")
        return

    await _fetch_and_send_manifest(ctx, chosen.game_id, display_name=chosen.game_name)


@bot.command(name="stats")
@commands.cooldown(2, 10, commands.BucketType.user)
async def stats_cmd(ctx: commands.Context) -> None:
    """
    .stats
    """
    if str(ctx.author.id) in BLOCKED_USER_IDS:
        await ctx.reply("Poshel Nahuy Alik Huyalik pidar vanuchi.")
        return

    async with aiohttp.ClientSession() as session:
        try:
            data = await manifest_api.get_user_stats(session, MANIFEST_API_KEY)
        except manifest_api.ManifestApiError:
            await ctx.reply("Manifest API is not responding right now. Try again in a bit.")
            return
        except Exception:
            await ctx.reply("Request failed (network error). Try again in a bit.")
            return

    # Pretty-print without leaking anything sensitive.
    user_id = data.get("user_id")
    username = data.get("username")
    api_key_usage_count = data.get("api_key_usage_count")
    daily_usage = data.get("daily_usage")
    daily_limit = data.get("daily_limit")
    can_make_requests = data.get("can_make_requests")

    embed = discord.Embed(title="Manifest API stats", color=discord.Color.blurple())
    if isinstance(username, str):
        embed.add_field(name="Username", value=username, inline=True)
    if isinstance(user_id, str):
        embed.add_field(name="User ID", value=user_id, inline=True)
    if isinstance(api_key_usage_count, int):
        embed.add_field(name="Total usage", value=str(api_key_usage_count), inline=True)
    if isinstance(daily_usage, int) and isinstance(daily_limit, int):
        embed.add_field(name="Daily usage", value=f"{daily_usage} / {daily_limit}", inline=True)
    if isinstance(can_make_requests, bool):
        embed.add_field(name="Can make requests", value=str(can_make_requests), inline=True)

    await ctx.reply(embed=embed)


@bot.command(name="help")
async def help_cmd(ctx: commands.Context) -> None:
    await ctx.reply(
        "\n".join(
            [
                f"`{COMMAND_PREFIX}app <appid>` - download manifest zip for an AppID",
                f"`{COMMAND_PREFIX}app <name>` - search by name, then confirm download",
                f"`{COMMAND_PREFIX}stats` - show your API usage stats",
            ]
        )
    )


def main() -> None:
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

