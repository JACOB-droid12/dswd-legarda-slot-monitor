"""Modular notification dispatcher for DSWD NCR AICS (CIS Legarda) Monitor.

Auto-detects active notification channels from environment variables:
  - Telegram: active when both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.
      * Open-slot alerts      -> sendPhoto (PNG screenshot + HTML caption w/ form link)
      * Session-expired alert -> sendMessage (normal priority)
  - Discord Webhook: active when DISCORD_WEBHOOK_URL is set.
      * Open-slot alerts      -> multipart/form-data (payload_json embed + PNG file,
                                 optional <@DISCORD_USER_ID> ping, <10 MB limit)
      * Session-expired alert -> plain JSON embed (normal priority)

If both channels are configured, notifications are sent to both.
"""

from __future__ import annotations

import html
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

PHT = timezone(timedelta(hours=8))
DISCORD_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB Discord free webhook limit
REQUEST_TIMEOUT_SEC = 25
MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class NotifierConfig:
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    discord_webhook_url: str | None
    discord_user_id: str | None

    @classmethod
    def from_env(cls) -> "NotifierConfig":
        def clean(key: str) -> str | None:
            val = os.getenv(key, "").strip()
            return val or None

        return cls(
            telegram_bot_token=clean("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=clean("TELEGRAM_CHAT_ID"),
            discord_webhook_url=clean("DISCORD_WEBHOOK_URL"),
            discord_user_id=clean("DISCORD_USER_ID"),
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_webhook_url)

    @property
    def active_channels(self) -> list[str]:
        channels: list[str] = []
        if self.telegram_enabled:
            channels.append("telegram")
        if self.discord_enabled:
            channels.append("discord")
        return channels


def now_pht_str() -> str:
    return datetime.now(PHT).strftime("%Y-%m-%d %I:%M:%S %p PHT")


def _post_with_retry(url: str, **kwargs: Any) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(url, timeout=REQUEST_TIMEOUT_SEC, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                retry_after = 2.0
                try:
                    retry_after = min(float(resp.headers.get("Retry-After", "2")), 10.0)
                except ValueError:
                    pass
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(2.0)
    assert last_exc is not None
    raise last_exc


class Notifier:
    def __init__(self, config: NotifierConfig | None = None) -> None:
        self.config = config or NotifierConfig.from_env()

    @property
    def active_channels(self) -> list[str]:
        return self.config.active_channels

    def send_slot_available_alert(
        self,
        *,
        screenshot_path: Path,
        form_url: str,
        office: str,
        address: str,
        mode: str,
        portal_url: str | None = None,
        details_note: str | None = None,
        is_test: bool = False,
    ) -> dict[str, bool]:
        """Send an urgent open-slot alert with PNG screenshot attached to all active channels."""
        if not self.active_channels:
            print(
                "[WARN] No notification channels configured "
                "(set TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID and/or DISCORD_WEBHOOK_URL)."
            )
            return {}

        if not screenshot_path.exists():
            raise FileNotFoundError(f"Alert screenshot not found: {screenshot_path}")

        png_bytes = screenshot_path.read_bytes()
        timestamp = now_pht_str()
        results: dict[str, bool] = {}

        if self.config.telegram_enabled:
            results["telegram"] = self._send_telegram_photo(
                png_bytes=png_bytes,
                filename=screenshot_path.name,
                form_url=form_url,
                portal_url=portal_url,
                office=office,
                address=address,
                mode=mode,
                timestamp=timestamp,
                details_note=details_note,
                is_test=is_test,
            )

        if self.config.discord_enabled:
            results["discord"] = self._send_discord_slot_alert(
                png_bytes=png_bytes,
                filename=screenshot_path.name,
                form_url=form_url,
                portal_url=portal_url,
                office=office,
                address=address,
                mode=mode,
                timestamp=timestamp,
                details_note=details_note,
                is_test=is_test,
            )

        return results

    def send_session_expired_alert(
        self,
        *,
        redirect_url: str,
        office: str,
        is_test: bool = False,
    ) -> dict[str, bool]:
        """Send a normal-priority Session Expired notice to all active channels."""
        if not self.active_channels:
            print(
                "[WARN] No notification channels configured for Session Expired alert "
                "(set TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID and/or DISCORD_WEBHOOK_URL)."
            )
            return {}

        timestamp = now_pht_str()
        results: dict[str, bool] = {}

        if self.config.telegram_enabled:
            results["telegram"] = self._send_telegram_session_expired(
                redirect_url=redirect_url,
                office=office,
                timestamp=timestamp,
                is_test=is_test,
            )

        if self.config.discord_enabled:
            results["discord"] = self._send_discord_session_expired(
                redirect_url=redirect_url,
                office=office,
                timestamp=timestamp,
                is_test=is_test,
            )

        return results

    def _send_telegram_photo(
        self,
        *,
        png_bytes: bytes,
        filename: str,
        form_url: str,
        portal_url: str | None,
        office: str,
        address: str,
        mode: str,
        timestamp: str,
        details_note: str | None,
        is_test: bool,
    ) -> bool:
        assert self.config.telegram_bot_token and self.config.telegram_chat_id
        prefix = "🧪 <b>[TEST ALERT]</b> " if is_test else "🚨 <b>URGENT: DSWD AICS SLOT OPEN!</b>\n"
        lines = [
            f"{prefix}" if not is_test else f"{prefix}DSWD AICS Slot Monitor Test\n",
            f"<b>Office:</b> {html.escape(office)}",
            f"<b>Address:</b> {html.escape(address)}",
            f"<b>Mode:</b> {html.escape(mode)}",
            f"<b>Checked:</b> {html.escape(timestamp)}",
        ]
        if details_note:
            lines.append(f"<b>Status:</b> {html.escape(details_note)}")
        if portal_url:
            lines.append(f'\n👉 <a href="{html.escape(portal_url, quote=True)}">Open DSWD AppSheet Portal</a>')
            lines.append(f'⚡ <a href="{html.escape(form_url, quote=True)}">Direct Appointment Form</a>')
        else:
            lines.append(f'\n👉 <a href="{html.escape(form_url, quote=True)}">Open DSWD Appointment Form Now</a>')
        caption = "\n".join(lines)[:1020]

        api_url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendPhoto"
        try:
            _post_with_retry(
                api_url,
                data={
                    "chat_id": self.config.telegram_chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                files={"photo": (filename, png_bytes, "image/png")},
            )
            print("[NOTIFY] Telegram sendPhoto succeeded.")
            return True
        except Exception as exc:
            print(f"[ERROR] Telegram sendPhoto failed: {exc}")
            return False

    def _send_telegram_session_expired(
        self,
        *,
        redirect_url: str,
        office: str,
        timestamp: str,
        is_test: bool,
    ) -> bool:
        assert self.config.telegram_bot_token and self.config.telegram_chat_id
        header = (
            "🧪 <b>[TEST] AppSheet Session Expired Notice</b>"
            if is_test
            else "⚠️ <b>DSWD Monitor: AppSheet Session Expired</b>"
        )
        short_url = redirect_url.split("?")[0]
        text = (
            f"{header}\n\n"
            f"<b>Office:</b> {html.escape(office)}\n"
            f"<b>Detected:</b> {html.escape(timestamp)}\n"
            f"<b>Redirect:</b> <code>{html.escape(short_url)}</code>\n\n"
            "Action required: run <code>python export_session.py</code> locally and update "
            "the <code>APPSHEET_SESSION_B64</code> GitHub Actions secret."
        )

        api_url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        try:
            _post_with_retry(
                api_url,
                json={
                    "chat_id": self.config.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            print("[NOTIFY] Telegram session-expired sendMessage succeeded.")
            return True
        except Exception as exc:
            print(f"[ERROR] Telegram sendMessage failed: {exc}")
            return False

    def _send_discord_slot_alert(
        self,
        *,
        png_bytes: bytes,
        filename: str,
        form_url: str,
        portal_url: str | None,
        office: str,
        address: str,
        mode: str,
        timestamp: str,
        details_note: str | None,
        is_test: bool,
    ) -> bool:
        assert self.config.discord_webhook_url
        attach_name = "legarda_slot.png"
        mention = f"<@{self.config.discord_user_id}> " if self.config.discord_user_id else ""
        content = (
            f"{mention}🧪 **[TEST ALERT]** DSWD AICS Legarda slot monitor test notification."
            if is_test
            else f"{mention}🚨 **DSWD AICS SLOT AVAILABLE at {office}!** Open the form now!"
        )

        fields = [
            {"name": "Office", "value": office, "inline": False},
            {"name": "Address", "value": address, "inline": True},
            {"name": "Mode", "value": mode, "inline": True},
            {"name": "Checked (PHT)", "value": timestamp, "inline": False},
        ]
        if details_note:
            fields.append({"name": "Detected State", "value": details_note[:1024], "inline": False})

        desc_links = (
            f"[Open DSWD AppSheet Portal]({portal_url}) • [Direct Appointment Form]({form_url})"
            if portal_url
            else f"[Open DSWD NCR AICS Appointment Form]({form_url})"
        )
        embed: dict[str, Any] = {
            "title": (
                f"[TEST] {office} — Slot Alert Verification"
                if is_test
                else f"SLOT OPEN: {office}"
            ),
            "url": portal_url or form_url,
            "description": desc_links,
            "color": 0x3B82F6 if is_test else 0x16A34A,
            "fields": fields,
            "footer": {"text": "DSWD NCR AICS Slot Monitor (Alert-Only • CIS Legarda)"},
        }

        payload: dict[str, Any] = {
            "content": content,
            "embeds": [embed],
        }
        if self.config.discord_user_id:
            payload["allowed_mentions"] = {"users": [self.config.discord_user_id]}

        try:
            if len(png_bytes) < DISCORD_MAX_FILE_BYTES:
                embed["image"] = {"url": f"attachment://{attach_name}"}
                _post_with_retry(
                    self.config.discord_webhook_url,
                    data={"payload_json": json.dumps(payload)},
                    files={"file": (attach_name, png_bytes, "image/png")},
                )
            else:
                print(
                    f"[WARN] Screenshot ({len(png_bytes):,} bytes) exceeds Discord 10 MB limit; "
                    "sending Discord embed without attachment."
                )
                _post_with_retry(self.config.discord_webhook_url, json=payload)

            print("[NOTIFY] Discord Webhook slot alert succeeded.")
            return True
        except Exception as exc:
            print(f"[ERROR] Discord Webhook slot alert failed: {exc}")
            return False

    def _send_discord_session_expired(
        self,
        *,
        redirect_url: str,
        office: str,
        timestamp: str,
        is_test: bool,
    ) -> bool:
        assert self.config.discord_webhook_url
        short_url = redirect_url.split("?")[0]
        embed = {
            "title": (
                "[TEST] AppSheet Session Expired"
                if is_test
                else "AppSheet Session Expired — Re-login Required"
            ),
            "description": (
                "The headless monitor was redirected to the AppSheet/Google login page.\n\n"
                "**Next step:** Run `python export_session.py` locally, sign in with Google, "
                "and update the `APPSHEET_SESSION_B64` secret in GitHub Actions."
            ),
            "color": 0xF59E0B,
            "fields": [
                {"name": "Office", "value": office, "inline": True},
                {"name": "Detected (PHT)", "value": timestamp, "inline": True},
                {"name": "Redirect Host/Path", "value": f"`{short_url}`", "inline": False},
            ],
            "footer": {"text": "DSWD NCR AICS Slot Monitor (Normal Priority Notice)"},
        }
        payload = {"embeds": [embed]}

        try:
            _post_with_retry(self.config.discord_webhook_url, json=payload)
            print("[NOTIFY] Discord Webhook session-expired notice succeeded.")
            return True
        except Exception as exc:
            print(f"[ERROR] Discord Webhook session-expired notice failed: {exc}")
            return False
