"""Phase 1: Interactive local session exporter for DSWD NCR AICS AppSheet.

Launches a visible Chromium browser (412x915 mobile viewport) so you can
manually complete Google/AppSheet sign-in if prompted, then exports Playwright's
storage_state to `session.json` and prints a single-line Base64 string
(`APPSHEET_SESSION_B64`) ready for GitHub Actions Secrets.

Safety: Alert/read-only tooling. Never automates Google password entry,
never fills client personal details, and never clicks Save.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Playwright, sync_playwright

TARGET_URL = (
    "https://www.appsheet.com/start/345cf64b-1b68-45e2-a627-e905176c1faf"
    "#appName=DSWDCISAppointmentTool-298417170&view=Appointment%20Form"
)
MOBILE_VIEWPORT = {"width": 412, "height": 915}
DEFAULT_SESSION_PATH = Path("session.json")
GITHUB_SECRET_LIMIT_BYTES = 48 * 1024  # 48 KB GitHub Actions secret limit


def is_login_url(url: str) -> bool:
    lower = (url or "").lower()
    return "/account/login" in lower or "accounts.google.com" in lower


def launch_visible_browser(p: Playwright, channel: str) -> tuple[Browser, str]:
    """Launch a visible Chromium-family browser compatible with Google OAuth.

    Google Sign-In frequently blocks Playwright's bundled Chromium when default
    automation flags are present. Using system Chrome/Edge (when available)
    with `--disable-blink-features=AutomationControlled` avoids this cleanly.
    """
    launch_kwargs: dict[str, Any] = {
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
    }

    if channel != "auto":
        if channel == "chromium":
            return p.chromium.launch(**launch_kwargs), "bundled chromium"
        return p.chromium.launch(channel=channel, **launch_kwargs), channel

    for candidate in ("chrome", "msedge"):
        try:
            browser = p.chromium.launch(channel=candidate, **launch_kwargs)
            return browser, candidate
        except Exception:
            continue

    browser = p.chromium.launch(**launch_kwargs)
    return browser, "bundled chromium"


def summarize_storage_state(state_bytes: bytes, b64_str: str) -> None:
    try:
        data = json.loads(state_bytes.decode("utf-8"))
    except Exception:
        data = {}

    cookies = data.get("cookies", [])
    origins = data.get("origins", [])
    total_ls_items = sum(len(o.get("localStorage", [])) for o in origins)

    print("\n--- Session Summary ---")
    print(f"Cookies captured       : {len(cookies)}")
    print(f"Origins captured       : {len(origins)} ({total_ls_items} localStorage entries)")
    print(f"Raw session.json size  : {len(state_bytes):,} bytes ({len(state_bytes) / 1024:.2f} KB)")
    print(f"APPSHEET_SESSION_B64   : {len(b64_str):,} chars ({len(b64_str) / 1024:.2f} KB)")

    if len(b64_str) > GITHUB_SECRET_LIMIT_BYTES:
        print(
            "\n[WARNING] APPSHEET_SESSION_B64 exceeds GitHub Actions' 48 KB secret limit! "
            "Largest localStorage entries:"
        )
        entries: list[tuple[int, str, str]] = []
        for origin_obj in origins:
            origin = origin_obj.get("origin", "<unknown>")
            for item in origin_obj.get("localStorage", []):
                val = item.get("value", "")
                entries.append((len(val), origin, item.get("name", "")))
        for size, origin, name in sorted(entries, reverse=True)[:8]:
            print(f"  - {size:>8,} chars | {origin} | {name}")
        print(
            "Report this output before Phase 2 so we can add gzip compression or "
            "safe non-auth cache trimming for GitHub Secrets."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export authenticated AppSheet Playwright storage_state to session.json and Base64."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_SESSION_PATH,
        help="Output path for Playwright storage_state JSON (default: session.json)",
    )
    parser.add_argument(
        "--channel",
        choices=("auto", "chrome", "msedge", "chromium"),
        default="auto",
        help="Browser channel to launch (default: auto -> chrome, msedge, bundled chromium)",
    )
    parser.add_argument(
        "--no-print-b64",
        action="store_true",
        help="Skip printing the raw APPSHEET_SESSION_B64 string to stdout.",
    )
    parser.add_argument(
        "--auto-wait",
        action="store_true",
        help="Automatically export session.json once manual login completes and the Appointment Form loads (no Enter press needed).",
    )
    parser.add_argument(
        "--auto-wait-timeout",
        type=int,
        default=300,
        help="Max seconds to wait in --auto-wait mode (default: 300).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path: Path = args.output.resolve()

    print("=" * 72)
    print("DSWD NCR AICS (CIS Legarda) — Phase 1 Session Exporter")
    print("=" * 72)
    print(f"Target URL : {TARGET_URL}")
    print(f"Viewport   : {MOBILE_VIEWPORT['width']}x{MOBILE_VIEWPORT['height']} (mobile)")
    print(f"Output file: {output_path}")
    print()

    with sync_playwright() as p:
        browser, used_channel = launch_visible_browser(p, args.channel)
        print(f"Launched visible browser via: {used_channel}")

        context = browser.new_context(
            viewport=MOBILE_VIEWPORT,
            locale="en-PH",
            timezone_id="Asia/Manila",
        )
        page = context.new_page()

        print("Opening AppSheet Appointment Form...")
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)

        print(
            "\nAction Required in the Browser Window:\n"
            "  1. If prompted, sign in manually with your Google / AppSheet account.\n"
            "  2. Wait for AppSheet's 'Syncing...' splash screen to finish.\n"
            "  3. Confirm the Appointment Form ('MODE OF ASSISTANCE' / 'CASH') is visible.\n"
            "  4. Do NOT fill in personal details or click Save.\n"
        )

        if args.auto_wait:
            print(
                f"Waiting up to {args.auto_wait_timeout}s for manual sign-in and "
                "AppSheet Appointment Form ('MODE OF ASSISTANCE' / 'CASH') to appear..."
            )
            detected = False
            for _ in range(args.auto_wait_timeout):
                if page.is_closed():
                    print("[ERROR] Browser window was closed before login completed.")
                    browser.close()
                    return 1
                current_url = page.url
                if "appsheet.com/start/" in current_url.lower() and not is_login_url(current_url):
                    try:
                        mode_heading = page.get_by_text(re.compile(r"MODE\s+OF\s+ASSISTANCE", re.I))
                        cash_btn = page.get_by_text(re.compile(r"^\s*CASH\s*$", re.I))
                        if (mode_heading.count() > 0 and mode_heading.first.is_visible()) or (
                            cash_btn.count() > 0 and cash_btn.first.is_visible()
                        ):
                            detected = True
                            break
                    except Exception:
                        pass
                page.wait_for_timeout(1_000)

            if not detected:
                print(
                    f"[ERROR] Timed out after {args.auto_wait_timeout}s waiting for Appointment Form "
                    f"(last URL: {page.url})."
                )
                browser.close()
                return 1

            print(f"Detected authenticated Appointment Form at: {page.url}")
            page.wait_for_timeout(2_000)
        else:
            while True:
                input("Press [Enter] here once the Appointment Form is loaded and ready to export... ")
                open_urls = [pg.url for pg in context.pages if not pg.is_closed()]
                active_url = open_urls[-1] if open_urls else page.url
                print(f"Open browser URL(s): {open_urls}")

                on_appsheet_app = any(
                    "appsheet.com/start/" in u.lower() and not is_login_url(u)
                    for u in open_urls
                )
                if not on_appsheet_app and is_login_url(active_url):
                    choice = input(
                        "[WARNING] Browser still appears to be on a login URL.\n"
                        "Press [Enter] to wait and retry after finishing login, or type 'save' to export anyway: "
                    ).strip().lower()
                    if choice != "save":
                        continue
                break

        # If login landed on a different AppSheet view, ensure we're on the clean Appointment Form URL
        if "appsheet.com" in page.url.lower() and "appointment" not in page.url.lower():
            print("Navigating to clean Appointment Form URL to capture complete AppSheet state...")
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3_000)

        context.storage_state(path=str(output_path))
        print(f"\nSaved Playwright storage_state to: {output_path}")

        browser.close()

    state_bytes = output_path.read_bytes()
    b64_str = base64.b64encode(state_bytes).decode("ascii")
    summarize_storage_state(state_bytes, b64_str)

    if not args.no_print_b64:
        print("\n" + "=" * 72)
        print("APPSHEET_SESSION_B64 (copy the single line below into GitHub Secrets):")
        print("=" * 72)
        print(b64_str)
        print("=" * 72)

    print(
        "\nIMPORTANT: Do NOT commit `session.json` to any public repository!\n"
        "Next step: Run `python verify_session.py` to test headless login persistence "
        "and dump the exact AppSheet React dropdown selectors."
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled by user.")
        sys.exit(130)
