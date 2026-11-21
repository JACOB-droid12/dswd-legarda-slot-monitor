"""Phase 2: 24/7 Cloud Slot Availability Monitor — DSWD NCR AICS (CIS Legarda Only).

Monitors the DSWD NCR AICS Appointment Form on Google AppSheet strictly for:
  - Mode   : CASH (overrideable via DSWD_MODE env var)
  - Address: Manila
  - Office : CIS Legarda Satellite Office

Hard Safety Rule:
  Alert-only. NEVER fills out client personal details and NEVER clicks Save/Submit.
  NEVER attempts automated Google password login.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from notifier import Notifier, now_pht_str

APP_ID = "345cf64b-1b68-45e2-a627-e905176c1faf"
ROOT_URL = f"https://www.appsheet.com/start/{APP_ID}"
TARGET_URL = (
    f"{ROOT_URL}"
    "#appName=DSWDCISAppointmentTool-298417170&view=Appointment%20Form"
)
LIVE_SYNC_API_FRAGMENT = f"/api/template/{APP_ID}"
MOBILE_VIEWPORT = {"width": 412, "height": 915}

TARGET_ADDRESS = "Manila"
TARGET_OFFICE = "CIS Legarda Satellite Office"
NO_SLOTS_BANNER = "NO SLOTS AVAILABLE/ALREADY SUBMITTED PENDING REQUEST"
UNAVAILABLE_ERROR_PHRASE = "appointments are currently unavailable at this office"

SETTLE_WAIT_MS = 3_500  # 3-4s wait for AppSheet conditional slot check
LOAD_TIMEOUT_SEC = 60

# Verified AppSheet React DOM selectors (confirmed in Phase 1)
SEL_SYNC_BUTTON = '[data-testid="SyncActionButton"]'
SEL_MODE_BUTTON = '.Attr_MODE [data-testid="button-select-button"]'
SEL_NEXT_BUTTON = '[data-testid="footer-button-next"]'
SEL_DETAILS_HEADER = '.Attr_Page_2[data-testid="form-page-header"]'
SEL_ADDRESS_DROPDOWN = '[data-testid="RESIDENTIAL ADDRESS OR SERVICE PROVIDER ADDRESS-list-select"]'
SEL_OFFICE_DROPDOWN = '[data-testid="DSWD OFFICE-list-select"]'
SEL_TIME_SLOT_DROPDOWN = '[data-testid="TIME SLOT-list-select"]'
SEL_POPUP = '[data-testid="list-select-popup"]'
SEL_POPUP_OPTION = ".ListSelectPopupOption"
SEL_POPUP_SEARCH = ".ListSelectPopup__input input"
SEL_HAS_AVAILABLE_ENTRY = '.Attr_Has_Available_Date[data-testid="form-entry"]'
SEL_HAS_AVAILABLE_NO = '.Attr_Has_Available_Date .BooleanTypeDisplay--no'
SEL_HAS_AVAILABLE_VALUE = '[data-testonly-column="Has_Available_Date"]'
SEL_ERROR_MESSAGE = '[data-testid="error-message-text"]'

FORBIDDEN_CLICK_PATTERN = re.compile(r"\b(save|submit|confirm\s+appointment)\b", re.I)


def load_dotenv_if_present(dotenv_path: Path = Path(".env")) -> None:
    """Lightweight .env loader (no external dependency); existing env vars win."""
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def load_storage_state(session_file: Path) -> tuple[dict[str, Any], str]:
    """Load Playwright storage_state from APPSHEET_SESSION_B64 env var or local session.json."""
    b64_raw = os.getenv("APPSHEET_SESSION_B64", "").strip()
    if b64_raw:
        try:
            decoded = base64.b64decode(b64_raw, validate=False)
            state = json.loads(decoded.decode("utf-8"))
            if not isinstance(state, dict) or "cookies" not in state:
                raise ValueError("Decoded JSON does not look like a Playwright storage_state object.")
            return state, "env:APPSHEET_SESSION_B64"
        except Exception as exc:
            raise RuntimeError(f"Failed to decode APPSHEET_SESSION_B64: {exc}") from exc

    resolved = session_file.resolve()
    if resolved.exists():
        state = json.loads(resolved.read_text(encoding="utf-8"))
        return state, f"file:{resolved}"

    raise FileNotFoundError(
        "No AppSheet session found. Set APPSHEET_SESSION_B64 in environment/secrets "
        f"or generate {resolved} using `python export_session.py`."
    )


def is_login_url(url: str) -> bool:
    lower = (url or "").lower()
    return "/account/login" in lower or "accounts.google.com" in lower


def safe_click(locator: Locator, label: str) -> None:
    """Enforce hard alert-only safety rule before clicking any element."""
    target = locator.first
    meta = target.evaluate(
        """(el) => ({
            text: (el.innerText || el.textContent || '').trim(),
            aria: el.getAttribute('aria-label') || '',
            testid: el.getAttribute('data-testid') || '',
        })"""
    )
    combined = f"{meta.get('text', '')} {meta.get('aria', '')} {meta.get('testid', '')}"
    if FORBIDDEN_CLICK_PATTERN.search(combined):
        raise RuntimeError(
            f"SAFETY BLOCK: Refused to click '{label}' because target matched forbidden pattern: {combined!r}"
        )
    target.scroll_into_view_if_needed(timeout=5_000)
    target.click(timeout=10_000)


def trigger_manual_sync(page: Page, live_syncs: list[dict[str, Any]], timeout_sec: int = 15) -> dict[str, Any]:
    """Click AppSheet's top-right Sync button (two circular arrows), wait for HTTP 200 sync, and close Sync status sheet."""
    sync_btn = page.locator(SEL_SYNC_BUTTON)
    sync_btn.wait_for(state="visible", timeout=10_000)
    before = len(live_syncs)
    safe_click(sync_btn, "AppSheet Sync button")

    synced: dict[str, Any] | None = None
    for _ in range(timeout_sec * 2):
        if len(live_syncs) > before:
            synced = live_syncs[-1]
            break
        page.wait_for_timeout(500)

    if synced is None:
        raise PlaywrightTimeoutError(
            f"Clicked Sync button ({SEL_SYNC_BUTTON}) but no HTTP 200 /api/template/ response arrived within {timeout_sec}s."
        )

    page.wait_for_timeout(600)

    # Dismiss AppSheet's "Sync status" mobile bottom sheet if it stays open
    sheet = page.locator('[data-testid="mobile-bottom-sheet"]')
    if sheet.count() > 0 and sheet.first.is_visible():
        page.keyboard.press("Escape")
        try:
            sheet.first.wait_for(state="hidden", timeout=3_000)
        except PlaywrightTimeoutError:
            page.mouse.click(200, 100)
            sheet.first.wait_for(state="hidden", timeout=3_000)
        page.wait_for_timeout(300)

    return synced


def wait_for_home_ready(page: Page) -> tuple[str, str]:
    """Wait for AppSheet Home view bottom nav to finish loading; detect login redirects early.

    Returns (status, active_url) where status is 'closed', 'open', 'login_expired', or 'timeout'.
    """
    home_btn = page.locator("button").filter(has_text=re.compile(r"^\s*Home\s*$", re.I))
    closed_btn = page.locator("button").filter(has_text=re.compile(r"\b(closed|maintenance)\b", re.I))
    my_appts_btn = page.locator("button").filter(has_text=re.compile(r"^\s*My\s+Appointments\s*$", re.I))

    for _ in range(LOAD_TIMEOUT_SEC):
        if is_login_url(page.url):
            return "login_expired", page.url

        try:
            sign_in_btn = page.get_by_role("button", name=re.compile(r"^\s*sign\s*in\b", re.I))
            if sign_in_btn.count() > 0 and sign_in_btn.first.is_visible():
                return "login_expired", page.url
        except Exception:
            pass

        try:
            if (home_btn.count() > 0 and home_btn.first.is_visible()) or (
                my_appts_btn.count() > 0 and my_appts_btn.first.is_visible()
            ):
                page.wait_for_timeout(800)
                is_closed = closed_btn.count() > 0 and closed_btn.first.is_visible()
                return ("closed" if is_closed else "open"), page.url
        except Exception:
            pass

        page.wait_for_timeout(1_000)

    return "timeout", page.url


def wait_for_appsheet_ready(page: Page, mode_value: str) -> tuple[str, str]:
    """Wait for AppSheet Syncing... splash to finish; detect login redirects early.

    Returns (status, active_url) where status is 'ready', 'login_expired', or 'timeout'.
    """
    mode_re = re.compile(rf"^\s*{re.escape(mode_value)}\s*$", re.I)

    for _ in range(LOAD_TIMEOUT_SEC):
        if is_login_url(page.url):
            return "login_expired", page.url

        try:
            sign_in_btn = page.get_by_role("button", name=re.compile(r"^\s*sign\s*in\b", re.I))
            if sign_in_btn.count() > 0 and sign_in_btn.first.is_visible():
                return "login_expired", page.url
        except Exception:
            pass

        try:
            mode_btn = page.locator(SEL_MODE_BUTTON).filter(has_text=mode_re)
            next_btn = page.locator(SEL_NEXT_BUTTON)
            if (
                mode_btn.count() > 0
                and mode_btn.first.is_visible()
                and next_btn.count() > 0
                and next_btn.first.is_visible()
            ):
                page.wait_for_timeout(500)
                return "ready", page.url
        except Exception:
            pass

        page.wait_for_timeout(1_000)

    return "timeout", page.url


def select_from_list_popup(
    page: Page,
    dropdown_selector: str,
    option_text: str,
    field_label: str,
    search_hint: str | None = None,
) -> None:
    """Open a verified AppSheet ListSelect dropdown and click an exact option inside its popup."""
    dropdown = page.locator(dropdown_selector)
    dropdown.wait_for(state="visible", timeout=20_000)
    safe_click(dropdown, f"open {field_label}")

    popup = page.locator(SEL_POPUP)
    popup.wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(300)

    exact_re = re.compile(rf"^\s*{re.escape(option_text)}\s*$", re.I)
    option_loc = popup.locator(SEL_POPUP_OPTION).filter(has_text=exact_re)

    if option_loc.count() == 0 or not option_loc.first.is_visible():
        search_box = popup.locator(SEL_POPUP_SEARCH)
        if search_box.count() > 0 and search_box.first.is_visible():
            search_box.first.fill(search_hint or option_text)
            page.wait_for_timeout(700)

    option_loc.first.wait_for(state="visible", timeout=8_000)
    safe_click(option_loc.first, f"select '{option_text}' in {field_label}")
    popup.wait_for(state="hidden", timeout=8_000)


def run_test_alert(notifier: Notifier, mode_value: str, test_expired: bool = False) -> int:
    """Send a sample screenshot + message through all configured notification channels."""
    channels = notifier.active_channels
    if not channels:
        print(
            "[ERROR] Cannot run --test-alert: no notification channels are configured.\n"
            "Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or DISCORD_WEBHOOK_URL "
            "(via environment variables or a local .env file)."
        )
        return 1

    print(f"[{now_pht_str()}] Running test notification to active channel(s): {', '.join(channels)}")

    if test_expired:
        results = notifier.send_session_expired_alert(
            redirect_url="https://www.appsheet.com/Account/Login?provider=google",
            office=TARGET_OFFICE,
            is_test=True,
        )
        return 0 if results and all(results.values()) else 1

    with tempfile.TemporaryDirectory() as tmpdir:
        sample_png = Path(tmpdir) / "test_alert_legarda.png"
        html_card = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
  body {{
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0f172a; color: #f8fafc; width: 412px; height: 640px;
    display: flex; align-items: center; justify-content: center;
  }}
  .card {{
    width: 356px; background: #1e293b; border: 2px solid #22c55e; border-radius: 16px;
    padding: 24px; box-sizing: border-box; box-shadow: 0 20px 40px rgba(0,0,0,0.4);
  }}
  .badge {{
    display: inline-block; background: #22c55e; color: #052e16; font-weight: 800;
    padding: 6px 12px; border-radius: 999px; font-size: 13px; letter-spacing: 0.5px;
  }}
  h1 {{ margin: 16px 0 8px; font-size: 22px; line-height: 1.25; }}
  p {{ margin: 6px 0; font-size: 14px; color: #cbd5e1; }}
  .kv {{ margin-top: 16px; padding-top: 12px; border-top: 1px solid #334155; }}
  .kv div {{ display: flex; justify-content: space-between; margin: 8px 0; font-size: 14px; }}
  .k {{ color: #94a3b8; }}
  .v {{ font-weight: 600; color: #f8fafc; text-align: right; }}
</style>
</head>
<body>
  <div class="card">
    <span class="badge">TEST ALERT • MONITOR OK</span>
    <h1>DSWD NCR AICS Slot Monitor</h1>
    <p>This is a simulated alert verifying real-time screenshot delivery.</p>
    <div class="kv">
      <div><span class="k">Office</span><span class="v">{TARGET_OFFICE}</span></div>
      <div><span class="k">Address</span><span class="v">{TARGET_ADDRESS}</span></div>
      <div><span class="k">Mode</span><span class="v">{mode_value}</span></div>
      <div><span class="k">Timestamp</span><span class="v">{now_pht_str()}</span></div>
      <div><span class="k">Safety Mode</span><span class="v">Alert-Only (No Save)</span></div>
    </div>
  </div>
</body>
</html>"""

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 412, "height": 640})
            page.set_content(html_card)
            page.screenshot(path=str(sample_png), full_page=True)
            browser.close()

        results = notifier.send_slot_available_alert(
            screenshot_path=sample_png,
            form_url=TARGET_URL,
            portal_url=ROOT_URL,
            office=TARGET_OFFICE,
            address=TARGET_ADDRESS,
            mode=mode_value,
            details_note="Simulated test alert (--test-alert). Real alerts only fire when 'Closed' is gone AND Legarda slots are open.",
            is_test=True,
        )

    print(f"Test alert results: {results}")
    return 0 if results and all(results.values()) else 1


def run_monitor(
    *,
    session_file: Path,
    mode_value: str,
    headless: bool,
    debug_screenshot: Path,
    notifier: Notifier,
    skip_maintenance_check: bool = False,
) -> int:
    storage_state, source_desc = load_storage_state(session_file)
    print(
        f"[{now_pht_str()}] Checking {TARGET_OFFICE} "
        f"(Address={TARGET_ADDRESS}, Mode={mode_value}, Session={source_desc}, "
        f"Channels={notifier.active_channels or ['none']})"
    )

    live_syncs: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=storage_state,
            viewport=MOBILE_VIEWPORT,
            locale="en-PH",
            timezone_id="Asia/Manila",
        )
        page = context.new_page()

        def on_response(resp: Any) -> None:
            try:
                if (
                    resp.request.method == "POST"
                    and LIVE_SYNC_API_FRAGMENT in resp.url
                    and resp.status == 200
                ):
                    headers = resp.headers
                    live_syncs.append(
                        {
                            "status": resp.status,
                            "content_length": headers.get("content-length", "chunked"),
                            "server_date": headers.get("date", "unknown"),
                        }
                    )
            except Exception:
                pass

        page.on("response", on_response)

        try:
            # 1. Check Home view first for global Closed / Maintenance mode (unless skipped)
            if not skip_maintenance_check:
                page.goto(ROOT_URL, wait_until="domcontentloaded", timeout=60_000)
                home_status, active_url = wait_for_home_ready(page)

                if home_status == "login_expired":
                    print(f"SESSION_EXPIRED ({TARGET_OFFICE}) — redirected to: {active_url}")
                    notifier.send_session_expired_alert(
                        redirect_url=active_url,
                        office=TARGET_OFFICE,
                    )
                    return 0

                if home_status == "timeout":
                    raise PlaywrightTimeoutError(
                        f"Timed out waiting {LOAD_TIMEOUT_SEC}s for AppSheet Home view to load (url={active_url})."
                    )

                if not live_syncs:
                    raise RuntimeError(
                        "Live freshness check failed: no HTTP 200 AppSheet /api/template/ sync observed."
                    )

                # Force an explicit manual Sync click (top-right circular arrows) and re-check Closed tab
                latest_sync = trigger_manual_sync(page, live_syncs)
                print(
                    f"LIVE_SYNC_OK (initial + manual Sync click): POST /api/template/ HTTP 200 "
                    f"(sync_count={len(live_syncs)}, Server-Date: {latest_sync['server_date']})"
                )

                closed_btn = page.locator("button").filter(
                    has_text=re.compile(r"\b(closed|maintenance)\b", re.I)
                )
                if closed_btn.count() > 0 and closed_btn.first.is_visible():
                    print(f"PORTAL_CLOSED_MAINTENANCE ({TARGET_OFFICE}) — AppSheet 'Closed' tab active")
                    return 0

                # Portal is open -> switch to Appointment Form view
                page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
            else:
                page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)

            # 2. Wait for Appointment Form ready & early login redirect detection
            status, active_url = wait_for_appsheet_ready(page, mode_value=mode_value)

            if status == "login_expired":
                print(f"SESSION_EXPIRED ({TARGET_OFFICE}) — redirected to: {active_url}")
                notifier.send_session_expired_alert(
                    redirect_url=active_url,
                    office=TARGET_OFFICE,
                )
                return 0

            if status != "ready":
                raise PlaywrightTimeoutError(
                    f"Timed out waiting {LOAD_TIMEOUT_SEC}s for AppSheet Appointment Form to load (url={active_url})."
                )

            if not live_syncs:
                raise RuntimeError(
                    "Live freshness check failed: no HTTP 200 AppSheet /api/template/ sync observed."
                )

            if skip_maintenance_check:
                latest_sync = trigger_manual_sync(page, live_syncs)
                print(
                    f"LIVE_SYNC_OK (initial + manual Sync click): POST /api/template/ HTTP 200 "
                    f"(sync_count={len(live_syncs)}, Server-Date: {latest_sync['server_date']})"
                )
            else:
                latest_sync = live_syncs[-1]

            # 3. MODE OF ASSISTANCE -> CASH (or DSWD_MODE) -> Next
            mode_re = re.compile(rf"^\s*{re.escape(mode_value)}\s*$", re.I)
            mode_btn = page.locator(SEL_MODE_BUTTON).filter(has_text=mode_re)
            mode_btn.first.wait_for(state="visible", timeout=10_000)
            safe_click(mode_btn.first, f"MODE OF ASSISTANCE = {mode_value}")
            page.wait_for_timeout(400)

            next_btn = page.locator(SEL_NEXT_BUTTON)
            next_btn.wait_for(state="visible", timeout=10_000)
            safe_click(next_btn, "Next")

            # 4. Details -> RESIDENTIAL ADDRESS OR SERVICE PROVIDER ADDRESS -> Manila
            select_from_list_popup(
                page,
                dropdown_selector=SEL_ADDRESS_DROPDOWN,
                option_text=TARGET_ADDRESS,
                field_label="RESIDENTIAL ADDRESS OR SERVICE PROVIDER ADDRESS",
                search_hint=TARGET_ADDRESS,
            )

            # 5. Conditional DSWD OFFICE -> CIS Legarda Satellite Office
            select_from_list_popup(
                page,
                dropdown_selector=SEL_OFFICE_DROPDOWN,
                option_text=TARGET_OFFICE,
                field_label="DSWD OFFICE",
                search_hint="Legarda",
            )

            # 6. Wait 3.5s for AppSheet's conditional slot check (Has_Available_Date) to evaluate
            page.wait_for_timeout(SETTLE_WAIT_MS)

            # 7. Anti-false-positive guard: confirm Details, TIME SLOT, and selected office
            details_visible = (
                page.locator(SEL_DETAILS_HEADER).count() > 0
                and page.locator(SEL_DETAILS_HEADER).first.is_visible()
            )
            time_slot_visible = (
                page.locator(SEL_TIME_SLOT_DROPDOWN).count() > 0
                and page.locator(SEL_TIME_SLOT_DROPDOWN).first.is_visible()
            )
            office_selected_text = ""
            if page.locator(SEL_OFFICE_DROPDOWN).count() > 0:
                office_selected_text = page.locator(SEL_OFFICE_DROPDOWN).first.inner_text().strip()
            office_confirmed = TARGET_OFFICE.lower() in office_selected_text.lower()

            if not (details_visible and time_slot_visible and office_confirmed):
                raise RuntimeError(
                    "Anti-false-positive guard failed before slot evaluation: "
                    f"details_visible={details_visible}, time_slot_visible={time_slot_visible}, "
                    f"office_confirmed={office_confirmed} (displayed={office_selected_text!r})"
                )

            # 8. Evaluate slot availability via Has_Available_Date / NO SLOTS banner
            no_slots_banner_visible = (
                page.get_by_text(re.compile(re.escape(NO_SLOTS_BANNER), re.I)).count() > 0
                and page.get_by_text(re.compile(re.escape(NO_SLOTS_BANNER), re.I)).first.is_visible()
            )
            has_available_no_visible = (
                page.locator(SEL_HAS_AVAILABLE_NO).count() > 0
                and page.locator(SEL_HAS_AVAILABLE_NO).first.is_visible()
            )
            unavailable_error_visible = False
            error_loc = page.locator(SEL_ERROR_MESSAGE)
            if error_loc.count() > 0 and error_loc.first.is_visible():
                err_txt = error_loc.first.inner_text().lower()
                unavailable_error_visible = UNAVAILABLE_ERROR_PHRASE in err_txt

            body_text = page.locator("body").inner_text()
            no_slots_in_body = NO_SLOTS_BANNER.lower() in body_text.lower()

            if (
                no_slots_banner_visible
                or has_available_no_visible
                or unavailable_error_visible
                or no_slots_in_body
            ):
                print(f"NO_SLOTS ({TARGET_OFFICE})")
                return 0

            # Banner absent & guards passed -> SLOTS OPEN! Capture full-page PNG and send urgent alert.
            portal_gate_note = (
                "Portal OPEN ('Closed' tab gone)"
                if not skip_maintenance_check
                else "Direct form check"
            )
            status_summary = f"{portal_gate_note} • NO SLOTS banner absent (Live Sync: {latest_sync['server_date']})"
            val_loc = page.locator(SEL_HAS_AVAILABLE_VALUE)
            if val_loc.count() > 0 and val_loc.first.is_visible():
                val_text = val_loc.first.inner_text().strip().replace("\n", " ")
                if val_text:
                    status_summary = (
                        f"{portal_gate_note} • Has_Available_Date = {val_text} "
                        f"(Live Sync: {latest_sync['server_date']})"
                    )

            print(f"SLOTS_AVAILABLE ({TARGET_OFFICE}) — {status_summary}")

            with tempfile.TemporaryDirectory() as tmpdir:
                alert_png = Path(tmpdir) / "legarda_slot_available.png"
                page.screenshot(path=str(alert_png), full_page=True)
                results = notifier.send_slot_available_alert(
                    screenshot_path=alert_png,
                    form_url=TARGET_URL,
                    portal_url=ROOT_URL,
                    office=TARGET_OFFICE,
                    address=TARGET_ADDRESS,
                    mode=mode_value,
                    details_note=status_summary,
                )
                print(f"Alert dispatch results: {results}")

            return 0

        except Exception as exc:
            print(f"[ERROR] Unexpected monitor failure: {exc}")
            try:
                page.screenshot(path=str(debug_screenshot), full_page=True)
                print(f"Saved debug screenshot: {debug_screenshot.resolve()}")
            except Exception as shot_exc:
                print(f"[WARN] Could not save debug screenshot: {shot_exc}")
            return 1
        finally:
            browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="24/7 Slot Availability Monitor for DSWD NCR AICS (CIS Legarda Satellite Office)."
    )
    parser.add_argument(
        "--session",
        type=Path,
        default=Path("session.json"),
        help="Fallback local Playwright storage_state JSON when APPSHEET_SESSION_B64 is unset (default: session.json).",
    )
    parser.add_argument(
        "--mode",
        default=None,
        help="Mode of assistance override (default: DSWD_MODE env var or 'CASH').",
    )
    parser.add_argument(
        "--test-alert",
        action="store_true",
        help="Send a sample urgent screenshot alert to all configured channels and exit.",
    )
    parser.add_argument(
        "--test-expired",
        action="store_true",
        help="Send a sample Session Expired notice to all configured channels and exit.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser window for local debugging.",
    )
    parser.add_argument(
        "--skip-maintenance-check",
        action="store_true",
        help="Jump straight to Appointment Form without checking Home's 'Closed' (Maintenance) tab first.",
    )
    parser.add_argument(
        "--debug-screenshot",
        type=Path,
        default=Path("debug_error.png"),
        help="Path for unexpected-error debug screenshot (default: debug_error.png).",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv_if_present()
    args = parse_args()
    mode_value = (args.mode or os.getenv("DSWD_MODE") or "CASH").strip() or "CASH"
    notifier = Notifier()

    if args.test_alert or args.test_expired:
        return run_test_alert(
            notifier=notifier,
            mode_value=mode_value,
            test_expired=args.test_expired,
        )

    return run_monitor(
        session_file=args.session,
        mode_value=mode_value,
        headless=not args.headed,
        debug_screenshot=args.debug_screenshot,
        notifier=notifier,
        skip_maintenance_check=args.skip_maintenance_check,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        sys.exit(130)
