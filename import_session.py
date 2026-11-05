"""import_session.py - build session.json from a browser export instead of logging in.

DeepSeek's login page is protected by an AWS WAF captcha that blocks automated browsers.
Sign in normally in your own browser, export the session, and import it here:

    python import_session.py                       # newest files in ./cookies
    python import_session.py cookies/export.json   # a specific file
    python import_session.py --token-prompt        # paste the login token (hidden input)
    python import_session.py --dry-run             # show what would change, write nothing

Accepted files: the extension export (extension/deepseek-session-exporter), Cookie-Editor /
EditThisCookie JSON, Playwright storage_state, Netscape cookies.txt, a Cookie header, DevTools
"Copy as cURL" text and HAR files.  Nothing secret is printed.  Exit code 0 means the login works
(or was not checked), 1 means more is needed.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bridge import session_import  # noqa: E402


def _args(argv=None):
    parser = argparse.ArgumentParser(description="Import a browser session export into session.json")
    parser.add_argument("files", nargs="*", help="export files (default: every file in the cookies folder)")
    parser.add_argument("--session", metavar="PATH", help="session.json to write (default: next to this script)")
    parser.add_argument("--token-prompt", action="store_true", help="read the login token from a hidden prompt")
    parser.add_argument("--token-stdin", action="store_true", help="read the login token from standard input")
    parser.add_argument("--no-verify", action="store_true", help="do not ask DeepSeek whether the token works")
    parser.add_argument("--dry-run", action="store_true", help="parse and verify but do not write session.json")
    parser.add_argument("--connect", action="store_true",
                        help="listen on 127.0.0.1:47123 for the browser extension (same as /connect in the harness)")
    parser.add_argument("--code", action="store_true", help="with --connect: require a pairing code typed into the extension")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    args = _args(argv)
    session_path = Path(args.session) if args.session else session_import.default_session_path()
    if args.connect:
        from bridge import config, connect
        return connect.run_blocking(session_path, code=args.code, seconds=config.CONNECT_SECONDS, port=config.CONNECT_PORT)

    token = None
    if args.token_prompt:
        token = getpass.getpass("Paste the DeepSeek login token (hidden): ")
    elif args.token_stdin:
        token = sys.stdin.readline()
    elif os.getenv("DS_USER_TOKEN"):
        token = os.environ["DS_USER_TOKEN"]
    token = (token or "").strip() or None

    files = [Path(f) for f in args.files] or session_import.discover(session_import.cookie_dir(session_path))
    if not files and not token:
        folder = session_import.cookie_dir(session_path)
        print(f"No export files found in {folder}")
        print("Put a cookie/session export there (or pass a file, or use --token-prompt).")
        print(session_import.TOKEN_HELP)
        return 1
    try:
        report = session_import.import_files(files, session_path=session_path, token=token,
                                             verify=not args.no_verify, dry_run=args.dry_run)
    except session_import.ImportProblem as exc:
        print("Import failed:", exc)
        return 1
    print("\n".join(session_import.describe(report)))
    if report["ok"]:
        print("\nDone. Start the harness with:  python run.py   (a running harness picks it up with /refresh)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
