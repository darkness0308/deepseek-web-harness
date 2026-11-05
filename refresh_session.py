"""
refresh_session.py - get a working DeepSeek login into session.json without fighting the captcha.

Run it whenever you see "token was rejected" or "AWS WAF token expired or missing":

    python refresh_session.py                  # import ./cookies if it works, else sign in via your browser
    python refresh_session.py --mode launch    # old behaviour: Playwright starts the browser itself
    python refresh_session.py --no-import      # skip the cookies/ folder and go straight to the browser

Why the old script was blocked: DeepSeek's login endpoint is behind an AWS WAF challenge, and a browser
that Playwright *launches* carries automation markers the challenge rejects.  Order of attack now:

  1. Import a browser export from cookies/ (see import_session.py).  No browser opens when it works.
  2. "attach" mode: start your own installed Chrome/Edge as an ordinary program with a private,
     persistent profile (.harness/browser-profile), let YOU sign in there (solve any captcha by hand;
     the profile remembers it for next time), then read the session through the DevTools port.
     Playwright never loads the login page, so it has nothing for the challenge to flag.
  3. "launch" mode: Playwright-launched persistent profile, kept for machines where attach fails.

The token is read from localStorage (it is not a cookie) and checked against DeepSeek before it replaces
the saved login.  The stable deviceId in session.json is always preserved.

Requirements:  pip install playwright   (no browser download is needed for attach mode)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bridge import session_import  # noqa: E402

SESSION_FILE = str(session_import.default_session_path())
PROFILE_DIR = ROOT / ".harness" / "browser-profile"
LOGIN_URL = "https://chat.deepseek.com/"
LOGIN_WAIT_SECONDS = 300

# Fingerprint parity with deepseek_client._COMMON_HEADERS + HAR (only used in launch mode).
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/154.0.0.0 Safari/537.36"
)
_TOKEN_JS = "() => { try { return localStorage.getItem('userToken'); } catch (e) { return null; } }"


def _say(text: str = "") -> None:
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def _load_dotenv_minimal() -> dict:
    """Read DS_EMAIL/DS_PASSWORD (via the same .env loader the harness uses)."""
    from bridge import config
    return {"DS_EMAIL": config.DS_EMAIL, "DS_PASSWORD": config.DS_PASSWORD}


# ── step 1: the cookies/ folder ───────────────────────────────────────────────

def try_cookie_folder() -> bool:
    """Import every export in cookies/ (not only newer ones). True when DeepSeek accepts the login."""
    files = session_import.discover(session_import.cookie_dir(SESSION_FILE))
    if not files:
        return False
    _say(f"Found {len(files)} export file(s) in cookies/ - trying them first (no browser needed) ...")
    report = session_import.import_files(files, session_path=SESSION_FILE)
    _say("\n".join(session_import.describe(report)))
    return report["ok"] and report["verified"] == "valid"


# ── step 2: attach to the user's own browser ──────────────────────────────────

def _find_browser(preferred: str = "auto") -> str | None:
    override = os.getenv("DS_BROWSER_PATH", "").strip()
    if override and Path(override).is_file():
        return override
    env = os.environ
    roots = [env.get("ProgramFiles", r"C:\Program Files"), env.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             env.get("LOCALAPPDATA", "")]
    chrome = [os.path.join(r, "Google", "Chrome", "Application", "chrome.exe") for r in roots if r]
    edge = [os.path.join(r, "Microsoft", "Edge", "Application", "msedge.exe") for r in roots if r]
    chrome += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    edge += ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    names_chrome = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
    names_edge = ("microsoft-edge", "microsoft-edge-stable")
    groups = [(chrome, names_chrome), (edge, names_edge)]
    if preferred == "msedge":
        groups.reverse()
    for paths, names in groups:
        for candidate in paths:
            if os.path.isfile(candidate):
                return candidate
        for name in names:
            found = shutil.which(name)
            if found:
                return found
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_devtools(port: int, seconds: int = 25) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


async def _wait_for_token(context, seconds: int) -> str | None:
    """Poll every open DeepSeek tab until localStorage holds a login token."""
    deadline = time.monotonic() + seconds
    announced = 0
    while time.monotonic() < deadline:
        for page in list(context.pages):
            if "deepseek.com" not in page.url:
                continue
            try:
                token = session_import.clean_token(await page.evaluate(_TOKEN_JS))
            except Exception:  # noqa: BLE001 - the page is navigating; look again
                continue
            if token:
                return token
        waited = int(seconds - (deadline - time.monotonic()))
        if waited // 30 > announced:
            announced = waited // 30
            _say(f"  Still waiting for you to sign in ... ({waited}s)")
        await asyncio.sleep(2)
    return None


async def _save_from_context(context, token: str) -> bool:
    await asyncio.sleep(2)  # let the WAF cookie settle after the page finished loading
    cookies = await context.cookies()
    payload = {"userToken": token, "cookies": cookies}
    report = session_import.import_parsed([session_import.parse_payload(payload)], session_path=SESSION_FILE,
                                          sources=[{"file": "(browser)", "format": "playwright-cookies",
                                                    "cookies": len(cookies), "token": True,
                                                    "expired_skipped": 0, "other_sites_skipped": 0}])
    _say("\n".join(session_import.describe(report)))
    return report["ok"]


async def refresh_attach(preferred: str, seconds: int) -> bool | None:
    """True: session saved.  False: the login did not happen (do not open another browser).
    None: the browser could not be started or attached; a fallback may be tried."""
    from playwright.async_api import async_playwright
    exe = _find_browser(preferred)
    if exe is None:
        _say("No Chrome or Edge found (set DS_BROWSER_PATH to its executable).")
        return None
    port = _free_port()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _say(f"Starting your own browser ({Path(exe).name}) with a private profile in {PROFILE_DIR.relative_to(ROOT)} ...")
    process = subprocess.Popen([exe, f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE_DIR}",
                                "--no-first-run", "--no-default-browser-check", LOGIN_URL])
    try:
        if not _wait_for_devtools(port):
            _say("The browser did not open its DevTools port (is another window using the same profile?).")
            return None
        _say("Sign in to DeepSeek in the window that opened (solve any captcha yourself).")
        _say(f"This script notices the login on its own; waiting up to {seconds // 60} minutes.\n")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            token = await _wait_for_token(context, seconds)
            if not token:
                _say("\nTimed out waiting for the login. session.json was left unchanged.")
                return False
            _say("Login detected.")
            return await _save_from_context(context, token)
    finally:
        try:
            process.terminate()
        except OSError:
            pass


# ── step 3: Playwright-launched browser (previous behaviour) ──────────────────

async def _try_auto_login(page, email: str, password: str) -> bool:
    """Attempt credential autofill on the sign-in page. Returns True on success."""
    try:
        await page.goto("https://chat.deepseek.com/sign_in", wait_until="domcontentloaded", timeout=30_000)
    except Exception:  # noqa: BLE001
        return False
    try:
        email_sel = ('input[type="email"], input[name="email"], '
                     'input[placeholder*="mail" i], input[placeholder*="phone" i]')
        await page.wait_for_selector(email_sel, timeout=10_000)
        await page.fill(email_sel, email)
        await page.fill('input[type="password"]', password)
        try:
            await page.click('button[type="submit"]', timeout=5_000)
        except Exception:  # noqa: BLE001
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(4000)
        url = page.url
        return "/a/chat/" in url or ("deepseek.com" in url and "sign_in" not in url and "login" not in url)
    except Exception as exc:  # noqa: BLE001
        _say(f"  Auto-login attempt failed ({type(exc).__name__}), falling back to manual ...")
        return False


async def refresh_launch(channel, seconds: int) -> bool:
    from playwright.async_api import async_playwright
    env = _load_dotenv_minimal()
    email, password = env.get("DS_EMAIL", ""), env.get("DS_PASSWORD", "")
    _say(f"Launching browser ({channel or 'bundled chromium'}) via Playwright ...")
    profile = ROOT / ".harness" / "launch-profile"
    profile.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile), channel=channel, headless=False, user_agent=_BROWSER_UA)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            if email and password:
                _say("Credentials found in .env - trying automatic login (a captcha may still appear).")
                await _try_auto_login(page, email, password)
            else:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            _say("Sign in in the browser window; this script notices the login on its own.")
            token = await _wait_for_token(context, seconds)
            if not token:
                _say("\nTimed out waiting for the login. session.json was left unchanged.")
                return False
            return await _save_from_context(context, token)
        finally:
            await context.close()


async def run(args) -> int:
    if not args.no_import and try_cookie_folder():
        _say("\nLogin imported from cookies/ and accepted by DeepSeek. Start the bridge:  python run.py")
        return 0
    try:
        import playwright  # noqa: F401
    except ImportError:
        _say("Playwright is not installed, so the browser sign-in is unavailable.")
        _say("Use the browser extension or python import_session.py instead (see README), or: pip install playwright")
        return 1
    if args.mode == "attach":
        try:
            result = await refresh_attach(args.browser, args.timeout)
        except Exception as exc:  # noqa: BLE001 - Playwright/CDP errors vary
            _say(f"\nAttaching to the browser failed ({type(exc).__name__}).")
            result = None
        if result is not None:
            if result:
                _say("\nYou can now start the bridge:  python run.py")
            return 0 if result else 1
        _say("Falling back to a browser started by Playwright ...")
    channels = {"chrome": ("chrome", "msedge", None), "msedge": ("msedge", "chrome", None),
                "auto": ("chrome", "msedge", None)}[args.browser]
    for channel in channels:
        try:
            if await refresh_launch(channel, args.timeout):
                _say("\nYou can now start the bridge:  python run.py")
                return 0
            return 1  # the person did not sign in; do not open yet another window
        except Exception as exc:  # noqa: BLE001 - Playwright launch/crash errors vary
            _say(f"\nBrowser {channel or 'chromium'} failed ({type(exc).__name__}); trying the next one ...")
    _say("\nNo browser could complete the login. Use the extension / python import_session.py, or install Chrome or Edge.")
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Refresh the DeepSeek login in session.json")
    parser.add_argument("--mode", choices=("attach", "launch"), default="attach",
                        help="attach (default): open your own browser then read it; launch: Playwright starts it")
    parser.add_argument("--browser", choices=("auto", "chrome", "msedge"), default="auto")
    parser.add_argument("--no-import", action="store_true", help="skip the cookies/ folder")
    parser.add_argument("--timeout", type=int, default=LOGIN_WAIT_SECONDS, help="seconds to wait for you to sign in")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
