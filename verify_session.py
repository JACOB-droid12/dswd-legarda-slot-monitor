"""Phase 1: Headless session & AppSheet React selector verifier (Legarda only).

Loads `session.json` in headless Chromium (412x915 mobile viewport), navigates
to the DSWD NCR AICS Appointment Form, verifies whether Google/AppSheet login
survived, walks strictly to `CIS Legarda Satellite Office`, and dumps exact
AppSheet React DOM selectors so Phase 2 (`monitor.py`) uses verified selectors.

Safety: Alert/read-only verification. NEVER fills client personal details and
NEVER clicks Save/Submit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

TARGET_URL = (
    "https://www.appsheet.com/start/345cf64b-1b68-45e2-a627-e905176c1faf"
    "#appName=DSWDCISAppointmentTool-298417170&view=Appointment%20Form"
)
MOBILE_VIEWPORT = {"width": 412, "height": 915}

TARGET_ADDRESS = "Manila"
TARGET_OFFICE = "CIS Legarda Satellite Office"
NO_SLOTS_TEXT = "NO SLOTS AVAILABLE/ALREADY SUBMITTED PENDING REQUEST"

FORBIDDEN_CLICK_PATTERN = re.compile(r"\b(save|submit|confirm\s+appointment)\b", re.I)

INSPECT_ELEMENT_JS = """
(el) => {
  if (!el) return null;
  const pickAttrs = (node) => {
    const dataAttrs = {};
    for (const attr of node.attributes || []) {
      if (
        attr.name.startsWith('data-') ||
        attr.name.startsWith('aria-') ||
        ['id', 'class', 'role', 'name', 'type', 'placeholder', 'for', 'tabindex'].includes(attr.name)
      ) {
        dataAttrs[attr.name] = attr.value;
      }
    }
    return dataAttrs;
  };
  const ancestors = [];
  let cur = el.parentElement;
  for (let i = 0; i < 4 && cur; i++) {
    ancestors.push({
      tag: cur.tagName.toLowerCase(),
      attrs: pickAttrs(cur),
    });
    cur = cur.parentElement;
  }
  const rect = el.getBoundingClientRect();
  return {
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.textContent || '').trim().slice(0, 200),
    attrs: pickAttrs(el),
    visible: rect.width > 0 && rect.height > 0,
    rect: {
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
    },
    outerHTMLPreview: (el.outerHTML || '').slice(0, 400),
    ancestors,
  };
}
"""

DUMP_FORM_FIELDS_JS = """
() => {
  const results = [];
  const candidates = document.querySelectorAll(
    '[data-testid], [data-test-id], [data-field-name], [role="combobox"], [role="button"], [role="radio"], [role="radiogroup"], [role="dialog"], input, select, textarea, button, label'
  );
  const seen = new Set();
  for (const el of candidates) {
    if (seen.has(el)) continue;
    seen.add(el);
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const attrs = {};
    for (const attr of el.attributes || []) {
      if (
        attr.name.startsWith('data-') ||
        attr.name.startsWith('aria-') ||
        ['id', 'class', 'role', 'name', 'type', 'placeholder', 'value'].includes(attr.name)
      ) {
        attrs[attr.name] = attr.value.slice(0, 120);
      }
    }
    const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 140);
    results.push({
      tag: el.tagName.toLowerCase(),
      text,
      attrs,
      rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
    });
    if (results.length >= 120) break;
  }
  return results;
}
"""


def is_login_url(url: str) -> bool:
    lower = (url or "").lower()
    return "/account/login" in lower or "accounts.google.com" in lower


def inspect_locator(locator: Locator) -> dict[str, Any] | None:
    try:
        return locator.first.evaluate(INSPECT_ELEMENT_JS)
    except Exception:
        return None


def safe_click(locator: Locator, description: str) -> dict[str, Any] | None:
    """Inspect an element, enforce hard safety rule against Save/Submit, then click."""
    target = locator.first
    info = inspect_locator(target)
    text_blob = " ".join(
        filter(
            None,
            [
                (info or {}).get("text", ""),
                (info or {}).get("attrs", {}).get("aria-label", ""),
                (info or {}).get("attrs", {}).get("data-testid", ""),
            ],
        )
    )
    if FORBIDDEN_CLICK_PATTERN.search(text_blob):
        raise RuntimeError(
            f"SAFETY BLOCK: Refused to click '{description}' because element text/attrs matched forbidden Save/Submit pattern: {text_blob!r}"
        )
    target.scroll_into_view_if_needed(timeout=5_000)
    target.click(timeout=10_000)
    return info


def find_first_visible(
    candidates: list[tuple[str, Locator]],
    timeout_ms: int = 12_000,
) -> tuple[str, Locator]:
    """Return the first candidate locator that becomes visible within timeout_ms."""
    per_try_ms = max(1_000, timeout_ms // max(len(candidates), 1))
    # Fast pass first
    for desc, loc in candidates:
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return desc, loc.first
        except Exception:
            continue

    # Polling pass
    for desc, loc in candidates:
        try:
            loc.first.wait_for(state="visible", timeout=per_try_ms)
            return desc, loc.first
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue

    names = ", ".join(desc for desc, _ in candidates)
    raise PlaywrightTimeoutError(f"None of the candidate locators became visible: [{names}]")


def wait_for_appsheet_ready(page: Page, mode_value: str, timeout_ms: int = 60_000) -> dict[str, Any]:
    """Wait for AppSheet Syncing... splash to finish and check early for login redirects."""
    deadline_steps = timeout_ms // 1_000
    sync_seen = False

    for sec in range(deadline_steps):
        if is_login_url(page.url):
            return {"status": "login_redirect", "url": page.url, "sync_seen": sync_seen}

        # Check for visible Sign in prompt on AppSheet itself
        try:
            sign_in_btn = page.get_by_role("button", name=re.compile(r"sign\s*in", re.I))
            if sign_in_btn.count() > 0 and sign_in_btn.first.is_visible():
                return {"status": "login_prompt", "url": page.url, "sync_seen": sync_seen}
        except Exception:
            pass

        try:
            syncing = page.get_by_text(re.compile(r"syncing", re.I))
            if syncing.count() > 0 and syncing.first.is_visible():
                sync_seen = True
        except Exception:
            pass

        # Ready signal: MODE OF ASSISTANCE or the mode button (e.g., CASH) is visible
        try:
            mode_heading = page.get_by_text(re.compile(r"MODE\s+OF\s+ASSISTANCE", re.I))
            mode_btn = page.get_by_text(re.compile(rf"^\s*{re.escape(mode_value)}\s*$", re.I))
            if (mode_heading.count() > 0 and mode_heading.first.is_visible()) or (
                mode_btn.count() > 0 and mode_btn.first.is_visible()
            ):
                # Give React a short settle after splash disappears
                page.wait_for_timeout(800)
                return {
                    "status": "ready",
                    "url": page.url,
                    "sync_seen": sync_seen,
                    "ready_after_sec": sec,
                }
        except Exception:
            pass

        page.wait_for_timeout(1_000)

    return {"status": "timeout", "url": page.url, "sync_seen": sync_seen}


def open_appsheet_list_select(page: Page, test_id: str, field_name: str) -> dict[str, Any]:
    """Open an AppSheet ListSelect dropdown strictly via its verified data-testid."""
    trigger = page.locator(f'[data-testid="{test_id}"]')
    trigger.wait_for(state="visible", timeout=20_000)
    trigger_info = safe_click(trigger, f"open {field_name} ([data-testid={test_id!r}])")

    popup = page.locator('[data-testid="list-select-popup"]')
    popup.wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(400)

    return {
        "field_name": field_name,
        "selector": f'[data-testid="{test_id}"]',
        "trigger_info": trigger_info,
    }


def select_option_in_popup(page: Page, option_text: str, search_hint: str | None = None) -> dict[str, Any]:
    """Select an exact option strictly inside AppSheet's [data-testid="list-select-popup"]."""
    exact_re = re.compile(rf"^\s*{re.escape(option_text)}\s*$", re.I)

    popup = page.locator('[data-testid="list-select-popup"]')
    popup.wait_for(state="visible", timeout=10_000)
    popup_info = inspect_locator(popup)

    options_loc = popup.locator(".ListSelectPopupOption")
    visible_options: list[str] = []
    try:
        visible_options = [t.strip().replace("\n", " ") for t in options_loc.all_inner_texts() if t.strip()][:30]
    except Exception:
        pass

    search_input = popup.locator(".ListSelectPopup__input input")
    search_input_info = inspect_locator(search_input) if search_input.count() > 0 else None

    target_opt = options_loc.filter(has_text=exact_re)
    used_search = False
    if target_opt.count() == 0 or not target_opt.first.is_visible():
        if search_input.count() > 0 and search_input.first.is_visible():
            search_input.first.fill(search_hint or option_text)
            used_search = True
            page.wait_for_timeout(800)
            try:
                visible_options = [t.strip().replace("\n", " ") for t in options_loc.all_inner_texts() if t.strip()][:30]
            except Exception:
                pass

    target_opt.first.wait_for(state="visible", timeout=8_000)
    option_info = safe_click(target_opt.first, f"select popup option '{option_text}'")

    # Wait for single-select popup to auto-close
    modal_auto_closed = True
    try:
        popup.wait_for(state="hidden", timeout=5_000)
    except PlaywrightTimeoutError:
        modal_auto_closed = False

    return {
        "option_text": option_text,
        "popup_selector": '[data-testid="list-select-popup"]',
        "option_selector": '[data-testid="list-select-popup"] .ListSelectPopupOption',
        "popup_info": popup_info,
        "search_input_info": search_input_info,
        "used_search": used_search,
        "visible_options_sample": visible_options,
        "option_info": option_info,
        "modal_auto_closed": modal_auto_closed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Headless verification of AppSheet login persistence and Legarda DOM selectors."
    )
    parser.add_argument(
        "--session",
        type=Path,
        default=Path("session.json"),
        help="Path to Playwright storage_state JSON exported by export_session.py (default: session.json)",
    )
    parser.add_argument(
        "--mode",
        default=os.getenv("DSWD_MODE", "CASH"),
        help="Mode of assistance to select (default: DSWD_MODE env var or 'CASH')",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser window for debugging (default: headless).",
    )
    parser.add_argument(
        "--dump-file",
        type=Path,
        default=Path("verify_dom_dump.json"),
        help="Path to write full JSON DOM selector dump (default: verify_dom_dump.json)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session_path: Path = args.session.resolve()
    mode_value: str = args.mode.strip() or "CASH"

    if not session_path.exists():
        print(f"[ERROR] Session file not found: {session_path}")
        print("Run `python export_session.py` first to generate session.json.")
        return 1

    report: dict[str, Any] = {
        "target_url": TARGET_URL,
        "mode": mode_value,
        "address": TARGET_ADDRESS,
        "office": TARGET_OFFICE,
        "headless": not args.headed,
        "steps": {},
    }

    print("=" * 72)
    print("DSWD NCR AICS (CIS Legarda) — Phase 1 Headless Session & DOM Verifier")
    print("=" * 72)
    print(f"Session file : {session_path}")
    print(f"Mode         : {mode_value}")
    print(f"Target       : {TARGET_ADDRESS} -> {TARGET_OFFICE}")
    print(f"Browser mode : {'headed' if args.headed else 'headless'}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=str(session_path),
            viewport=MOBILE_VIEWPORT,
            locale="en-PH",
            timezone_id="Asia/Manila",
        )
        page = context.new_page()

        try:
            # Step 1: Open URL and verify login persistence
            print("[1/6] Opening AppSheet URL and waiting for Syncing... to finish...")
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
            ready = wait_for_appsheet_ready(page, mode_value=mode_value, timeout_ms=60_000)
            report["steps"]["load_and_auth"] = ready

            if ready["status"] in ("login_redirect", "login_prompt"):
                print(f"\nLOGIN_STATUS: EXPIRED_OR_REDIRECTED ({ready['status']})")
                print(f"Final URL   : {ready['url']}")
                page.screenshot(path="verify_login_redirect.png", full_page=True)
                print("Saved screenshot: verify_login_redirect.png")
                args.dump_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
                return 2

            if ready["status"] != "ready":
                print(f"\nLOGIN_STATUS: UNKNOWN_TIMEOUT (url={ready['url']})")
                page.screenshot(path="verify_timeout.png", full_page=True)
                report["steps"]["timeout_dom"] = page.evaluate(DUMP_FORM_FIELDS_JS)
                args.dump_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(f"Saved screenshot: verify_timeout.png and DOM dump: {args.dump_file}")
                return 3

            print(
                f"LOGIN_STATUS: SURVIVED (ready in {ready.get('ready_after_sec', '?')}s, "
                f"sync_seen={ready.get('sync_seen')})"
            )
            print(f"Active URL  : {page.url}")
            page.screenshot(path="verify_01_mode.png", full_page=True)
            report["steps"]["step1_visible_elements"] = page.evaluate(DUMP_FORM_FIELDS_JS)

            # Step 2: Select MODE OF ASSISTANCE (CASH) and click Next
            print(f"\n[2/6] Selecting MODE OF ASSISTANCE = '{mode_value}' and clicking 'Next'...")
            mode_re = re.compile(rf"^\s*{re.escape(mode_value)}\s*$", re.I)
            mode_loc = page.locator('.Attr_MODE [data-testid="button-select-button"]').filter(has_text=mode_re)
            mode_loc.first.wait_for(state="visible", timeout=10_000)
            mode_info = safe_click(mode_loc.first, f"mode {mode_value}")
            page.wait_for_timeout(500)

            next_loc = page.locator('[data-testid="footer-button-next"]')
            next_loc.wait_for(state="visible", timeout=10_000)
            next_info = safe_click(next_loc, "Next button")
            page.locator('[data-testid="RESIDENTIAL ADDRESS OR SERVICE PROVIDER ADDRESS-list-select"]').wait_for(
                state="visible", timeout=15_000
            )

            report["steps"]["mode_and_next"] = {
                "mode_selector": '.Attr_MODE [data-testid="button-select-button"]',
                "mode_info": mode_info,
                "next_selector": '[data-testid="footer-button-next"]',
                "next_info": next_info,
            }
            print(f"  Selected {mode_value} via: .Attr_MODE [data-testid=\"button-select-button\"]")
            print(f"  Clicked Next via    : [data-testid=\"footer-button-next\"]")

            # Step 3: Open RESIDENTIAL ADDRESS OR SERVICE PROVIDER ADDRESS -> select Manila
            print(f"\n[3/6] Opening 'RESIDENTIAL ADDRESS OR SERVICE PROVIDER ADDRESS' and selecting '{TARGET_ADDRESS}'...")
            report["steps"]["details_initial_elements"] = page.evaluate(DUMP_FORM_FIELDS_JS)
            addr_open = open_appsheet_list_select(
                page,
                "RESIDENTIAL ADDRESS OR SERVICE PROVIDER ADDRESS-list-select",
                "RESIDENTIAL ADDRESS",
            )
            page.screenshot(path="verify_02_address_modal.png", full_page=True)
            addr_select = select_option_in_popup(page, TARGET_ADDRESS, search_hint=TARGET_ADDRESS)
            report["steps"]["address"] = {"open": addr_open, "select": addr_select}
            print(f"  Opened address via : {addr_open['selector']}")
            print(f"  Selected Manila    : {addr_select['option_selector']} (auto_closed={addr_select['modal_auto_closed']})")

            # Step 4: Wait for conditional DSWD OFFICE dropdown -> select CIS Legarda Satellite Office
            print(f"\n[4/6] Waiting for conditional 'DSWD OFFICE' and selecting '{TARGET_OFFICE}'...")
            office_open = open_appsheet_list_select(
                page,
                "DSWD OFFICE-list-select",
                "DSWD OFFICE",
            )
            page.screenshot(path="verify_03_office_modal.png", full_page=True)
            office_select = select_option_in_popup(page, TARGET_OFFICE, search_hint="Legarda")
            report["steps"]["office"] = {"open": office_open, "select": office_select}
            print(f"  Opened office via  : {office_open['selector']}")
            print(f"  Options seen       : {office_select['visible_options_sample']}")
            print(f"  Selected Legarda   : {office_select['option_selector']} (auto_closed={office_select['modal_auto_closed']})")

            # Step 5: Wait 3.5 seconds for AppSheet conditional slot check
            print("\n[5/6] Waiting 3.5s for AppSheet conditional slot check to settle...")
            page.wait_for_timeout(3_500)
            page.screenshot(path="verify_04_legarda_result.png", full_page=True)

            # Step 6: Inspect Details, TIME SLOT, and NO SLOTS banner
            print("[6/6] Inspecting Details form, TIME SLOT field, and NO SLOTS banner...")
            details_loc = page.locator('.Attr_Page_2[data-testid="form-page-header"]')
            time_slot_loc = page.locator('[data-testid="TIME SLOT-list-select"]')
            no_slots_loc = page.get_by_text(re.compile(re.escape(NO_SLOTS_TEXT), re.I))

            details_visible = details_loc.count() > 0 and details_loc.first.is_visible()
            time_slot_visible = time_slot_loc.count() > 0 and time_slot_loc.first.is_visible()
            no_slots_visible = no_slots_loc.count() > 0 and no_slots_loc.first.is_visible()

            details_info = inspect_locator(details_loc) if details_visible else None
            time_slot_info = inspect_locator(time_slot_loc) if time_slot_visible else None
            no_slots_info = inspect_locator(no_slots_loc) if no_slots_visible else None

            # Also inspect TIME SLOT popup options safely (without selecting anything) if TIME SLOT is visible
            time_slot_popup_options: list[str] = []
            if time_slot_visible:
                try:
                    safe_click(time_slot_loc, "inspect TIME SLOT options")
                    popup = page.locator('[data-testid="list-select-popup"]')
                    popup.wait_for(state="visible", timeout=4_000)
                    page.wait_for_timeout(500)
                    time_slot_popup_options = [
                        t.strip().replace("\n", " ")
                        for t in popup.locator(".ListSelectPopupOption").all_inner_texts()
                        if t.strip()
                    ]
                    page.screenshot(path="verify_05_timeslot_popup.png", full_page=True)
                    # Close popup via Escape (never selecting)
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                except Exception as ts_exc:
                    time_slot_popup_options = [f"<popup_inspect_note: {ts_exc}>"]

            body_text = page.locator("body").inner_text()
            no_slots_in_body = NO_SLOTS_TEXT.lower() in body_text.lower()

            report["steps"]["final_inspection"] = {
                "details_selector": '.Attr_Page_2[data-testid="form-page-header"]',
                "details_visible": details_visible,
                "details_info": details_info,
                "time_slot_selector": '[data-testid="TIME SLOT-list-select"]',
                "time_slot_visible": time_slot_visible,
                "time_slot_info": time_slot_info,
                "time_slot_popup_options": time_slot_popup_options,
                "no_slots_visible": no_slots_visible,
                "no_slots_in_body_text": no_slots_in_body,
                "no_slots_info": no_slots_info,
                "final_visible_elements": page.evaluate(DUMP_FORM_FIELDS_JS),
                "visible_text_lines": [line.strip() for line in body_text.splitlines() if line.strip()][:80],
            }

            args.dump_file.write_text(json.dumps(report, indent=2), encoding="utf-8")

            print("\n" + "=" * 72)
            print("PHASE 1 VERIFICATION SUMMARY (CIS Legarda Satellite Office)")
            print("=" * 72)
            print(f"LOGIN_STATUS        : SURVIVED")
            print(f"Details visible     : {details_visible}")
            print(f"TIME SLOT visible   : {time_slot_visible}")
            print(f"NO_SLOTS banner     : visible={no_slots_visible} | in_body={no_slots_in_body}")
            if no_slots_visible or no_slots_in_body:
                print(f"Evaluated State     : NO_SLOTS ({TARGET_OFFICE})")
            elif details_visible and time_slot_visible:
                print(f"Evaluated State     : POTENTIAL_OPEN_SLOT ({TARGET_OFFICE}) — verify verify_04_legarda_result.png!")
            else:
                print("Evaluated State     : GUARD_TRIGGERED (Details/TIME SLOT not both visible)")
            print(f"\nDOM selector dump   : {args.dump_file.resolve()}")
            print("Screenshots saved   : verify_01_mode.png .. verify_04_legarda_result.png")
            print("=" * 72)
            return 0

        except Exception as exc:
            print(f"\n[ERROR] Verification failed: {exc}")
            try:
                page.screenshot(path="verify_error.png", full_page=True)
                report["error"] = str(exc)
                report["error_url"] = page.url
                report["error_elements"] = page.evaluate(DUMP_FORM_FIELDS_JS)
                args.dump_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(f"Saved debug screenshot: verify_error.png")
                print(f"Saved partial DOM dump: {args.dump_file.resolve()}")
            except Exception:
                pass
            return 1
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        sys.exit(130)
