# lsteam_manifestbot

Discord bot that downloads Steam manifest ZIPs from `https://manifest.morrenus.xyz/`.

## Commands

- `.app <appid>`
  - Example: `.app 400`
  - Downloads + uploads the manifest ZIP (if it exists and fits Discord upload limits).
- `.app <app name>`
  - Example: `.app Portal`
  - Searches by name, makes you pick (if multiple), then **asks for confirmation** before downloading.
- `.stats`
  - Shows your `manifest.morrenus.xyz` API usage stats.

## Environment variables

Set these in Render (or your local shell):

- `DISCORD_TOKEN` (required)
- `MANIFEST_API_KEY` (required)
- `COMMAND_PREFIX` (default `.`)
- `MAX_UPLOAD_MB` (default `25`)
- `BLOCKED_USER_IDS` (optional, comma-separated user IDs)

Important: don’t hardcode your API key in code. If you already pasted it in chat anywhere public, rotate it.

## Run locally (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:DISCORD_TOKEN="YOUR_DISCORD_TOKEN"
$env:MANIFEST_API_KEY="YOUR_MANIFEST_API_KEY"
python .\main.py
```

In the Discord Developer Portal, enable **Message Content Intent** for your bot (required for prefix commands).

## Deploy on Render + UptimeRobot

This repo includes a `render.yaml` blueprint. You can also create the service manually.

1. Create a new Render service from this repo.
2. Add env vars:
   - `DISCORD_TOKEN`
   - `MANIFEST_API_KEY`
3. Start command: `python main.py`
4. UptimeRobot: monitor your Render URL (`GET /healthz`).

## Notes / limitations

- Discord has an upload size limit. If the manifest ZIP is bigger than `MAX_UPLOAD_MB`, the bot will refuse to download/upload it.
- If Morrenus download fails, the bot falls back to other providers listed in `api.json` (you can edit/disable providers there).

