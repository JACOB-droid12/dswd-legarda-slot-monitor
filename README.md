# DSWD NCR AICS Slot Monitor — CIS Legarda Satellite Office (Manila)

A 24/7 cloud-based, **alert-only** slot availability monitor for the **DSWD NCR AICS Appointment Form (Google AppSheet)**, locked strictly to:

- **Mode:** `CASH` *(optional override via `DSWD_MODE` secret/env)*
- **Address:** `Manila`
- **Office:** `CIS Legarda Satellite Office`

Runs free 24/7 on **GitHub Actions (`ubuntu-latest`)** in a public repository, sending real-time alerts with full-page mobile screenshots (`412x915`) to **Telegram**, **Discord**, or **both**.

> **Safety & Freshness Guarantees**
> - **Alert-only:** Never fills out client personal details and never clicks `Save` / `Submit`.
> - **No automated Google login:** Uses an exported Playwright `storage_state` (`APPSHEET_SESSION_B64`). If AppSheet redirects to `/Account/Login` or `accounts.google.com`, the monitor sends a normal-priority **Session Expired** notice and exits cleanly (`0`).
> - **Verified live data (never stale/preview):** Every run starts in a fresh ephemeral browser context (zero IndexedDB/disk cache) and requires an HTTP `200` `POST /api/template/345cf64b-1b68-45e2-a627-e905176c1faf/` live sync response from AppSheet's servers (logging the live server `Date`).
> - **Maintenance & anti-false-positive guards:** Opens the base link first to check AppSheet's **`Closed`** (Maintenance) tab (`PORTAL_CLOSED_MAINTENANCE`, overrideable with `--skip-maintenance-check`), and on `Appointment Form` confirms `Details`, `TIME SLOT`, and `DSWD OFFICE = CIS Legarda Satellite Office` before evaluating `Has_Available_Date` (`NO SLOTS AVAILABLE/ALREADY SUBMITTED PENDING REQUEST`).

---

## 1. Local Setup & Export `APPSHEET_SESSION_B64`

Requires Python 3.11+ (Windows PowerShell example):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

Export your authenticated AppSheet session using a visible browser:

```powershell
python export_session.py
```

1. Sign in manually with Google in the opened browser window.
2. Wait until AppSheet's `Syncing...` screen finishes and `MODE OF ASSISTANCE` (`CASH`) appears.
3. Press **Enter** in the terminal to save `session.json` and print `APPSHEET_SESSION_B64`.

**Quick Windows tip — copy `APPSHEET_SESSION_B64` directly to your clipboard from `session.json`:**

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("session.json")) | Set-Clipboard
```

*(Optional)* Verify headless login persistence locally before pushing:

```powershell
python verify_session.py
```

> **Never commit `session.json`, `.env`, or `*.png`.** They are blocked by `.gitignore`.

---

## 2. Notification Setup (Telegram, Discord, or Both)

The monitor auto-detects channels from environment variables / GitHub Secrets. If both are configured, alerts are sent to **both**.

### Option A — Telegram (`@BotFather`)

1. Open Telegram, search for **@BotFather**, and send `/newbot`.
2. Follow the prompts and copy the **HTTP API token** → this is your `TELEGRAM_BOT_TOKEN`.
3. Open a chat with your new bot and send `/start` (or `"hi"`).
4. Open `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates` in a browser and copy `"chat": {"id": 123456789}` → this is your `TELEGRAM_CHAT_ID`.
5. **On-demand remote status check:** Send `/status` (or `/check`) to your bot anytime from your phone. On the next scheduled run, the monitor replies with a live status summary (`PORTAL CLOSED / MAINTENANCE` or `NO SLOTS`) + a fresh screenshot (only authorized messages from `TELEGRAM_CHAT_ID` are honored).

### Option B — Discord Webhook

1. In your Discord server, go to **Server Settings → Integrations → Webhooks → New Webhook**.
2. Choose the target alert channel, click **Copy Webhook URL** → this is your `DISCORD_WEBHOOK_URL`.
3. *(Optional phone ping mention)* Enable **User Settings → Advanced → Developer Mode**, right-click your username → **Copy User ID** → this is your `DISCORD_USER_ID` (pings `<@ID>` on open-slot alerts).

---

## 3. Test Real-Time Phone Delivery Locally

Create a local `.env` file (ignored by Git):

```env
# Configure Telegram, Discord, or both:
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789

DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_USER_ID=123456789012345678
```

Send a simulated screenshot alert to verify your phone rings immediately:

```powershell
python monitor.py --test-alert
```

*(Optional)* Test the normal-priority Session Expired alert:

```powershell
python monitor.py --test-expired
```

Run a real Legarda check locally:

```powershell
python monitor.py
```

---

## 4. Deploy 24/7 on GitHub Actions (Public Repo — Free Unlimited Minutes)

1. Create a **public** GitHub repository and push the project files:
   - Confirm `git status` does **not** show `session.json`, `.env`, or any `.png` files.
2. Go to **Repository Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret Name | Required? | Description |
|---|---|---|
| `APPSHEET_SESSION_B64` | **Yes** | Single-line Base64 string of `session.json` |
| `TELEGRAM_BOT_TOKEN` | If using Telegram | Bot token from `@BotFather` |
| `TELEGRAM_CHAT_ID` | If using Telegram | Your numeric Telegram chat/user ID |
| `DISCORD_WEBHOOK_URL` | If using Discord | Discord channel Webhook URL |
| `DISCORD_USER_ID` | Optional (Discord) | Numeric Discord user ID for `<@ID>` urgent pings |
| `DSWD_MODE` | Optional | Override assistance mode (defaults to `CASH`) |

3. Open the **Actions** tab → **Check DSWD Legarda Slots** → click **Run workflow**:
   - First run with **`test_alert` = `true`** to verify cloud delivery from GitHub Actions.
   - Then run with **`test_alert` = `false`** to verify a live Legarda check (`NO_SLOTS (CIS Legarda Satellite Office)`).

The built-in schedule runs every **10 minutes** (`*/10 * * * *`), caches pip + Playwright Chromium binaries, prevents overlapping runs (`cancel-in-progress: false`), and uploads `debug_*.png` artifacts **only on unexpected failures**.

---

## 5. Optional: Strict 5-Minute Runs via `cron-job.org`

GitHub's native `schedule` cron can occasionally jitter during peak hours. For strict 5-minute polling:

1. Create a GitHub **Fine-Grained Personal Access Token** scoped **only** to this repository with permission **Actions: Read and write**.
2. In [cron-job.org](https://cron-job.org), create a cronjob every **5 minutes**:
   - **URL:** `https://api.github.com/repos/<OWNER>/<REPO>/actions/workflows/check-legarda.yml/dispatches`
   - **Method:** `POST`
   - **Headers:**
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <YOUR_FINE_GRAINED_PAT>`
     - `X-GitHub-Api-Version: 2022-11-28`
     - `User-Agent: dswd-legarda-monitor`
   - **Body:** `{"ref": "main"}`

---

## 6. Refreshing an Expired Session

When you receive a **Session Expired** alert:

1. Run `python export_session.py` locally and sign in with Google again.
2. Copy the new Base64 string:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("session.json")) | Set-Clipboard
   ```
3. Paste it into **Settings → Secrets and variables → Actions → `APPSHEET_SESSION_B64`** on GitHub.
