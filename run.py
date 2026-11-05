"""
Entry point for the native coding harness (default) and retained legacy web chat:

    python run.py

Native startup runs terminal chat and the API against one AgentRuntime with
separate owner-scoped sessions. --no-api runs the terminal without binding a port;
--api-only runs the server, and --resume restores a native terminal session.
The native command reference is in docs/advanced-harness.md.

The following describes --legacy-chat. It starts two interfaces:
  1. Terminal chatbot  — interactive chat in this window (foreground)
  2. API server        — http://localhost:11434/v1  (background thread)
     OpenAI-compatible, point any chatbot app here.

One DeepSeek session is created when the program starts and reused for every
message, so the model remembers the full conversation.

Commands (prefix every command with /):
  /new                       — start a new conversation (new session)
  /sessions                  — list recent sessions
  /load <id>                 — load & continue an existing session
  /rename <title>            — rename the current session
  /model                     — show the current (single) model;
                               expert/vision args are deprecated aliases
  /search on|off             — toggle web search (independent toggle)
  /deepthink on|off          — toggle Deep Think mode (independent toggle)
  /upload                    — open GUI file picker
  /share                     — create a share link for the current session
  /api                       — create a new API key (prompts for name)
  /api-show                  — list all API keys with names & values
  /api-delete <name>         — delete an API key by name
  /edit <id> <text>          — edit a user message + regenerate
  /export                    — trigger history export + show URL
  /status                    — show model, search, thinking state, session ID
  /help                      — show the command reference box
  /q  /quit  /exit           — quit

File upload:
  /upload                    — open GUI multi-file picker
  Drop any file into  file_uploads/  — it will be uploaded automatically
  on your next message and moved to  file_uploads/Done/.
   Or reference a file inline with  @path/to/file.ext  in your message.
   Works with search and deep think combined (server allows all three).

Set BRIDGE_PORT=8080 before running to change the API port.
"""

import datetime
import logging
import os
import platform
import re
import shutil
import sys
import textwrap
import threading
import time

# ── ANSI colour support ────────────────────────────────────────────────────────
_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _e(code: str) -> str:
    """Return ANSI escape code when colors are enabled, else empty string."""
    return code if _USE_COLOR else ""


R   = _e("\033[0m")     # reset
B   = _e("\033[1m")     # bold
D   = _e("\033[2m")     # dim
IT  = _e("\033[3m")     # italic
RD  = _e("\033[91m")    # bright red
GN  = _e("\033[92m")    # bright green
YL  = _e("\033[93m")    # bright yellow
CY  = _e("\033[96m")    # bright cyan
WH  = _e("\033[97m")    # bright white
GR  = _e("\033[90m")    # dark gray

# Code-specific palette
BG_CODE = _e("\033[48;5;236m")   # dark bg  (inline code)
FG_CODE = _e("\033[38;5;120m")   # mint     (inline code)
FG_CBLK = _e("\033[38;5;187m")   # pale tan (code-block lines)

# ── Terminal helpers ───────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\033\[[0-9;]*[mK]")
NL = "\n"

# Windows consoles and redirected output often default to cp1252, which cannot
# encode the braille spinner, box-drawing or check marks this UI uses — every
# such write raised UnicodeEncodeError and killed the spinner thread mid-turn.
# Switch stdout/stderr to UTF-8 with replacement so rendering degrades instead
# of crashing.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is None:
        continue
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass  # already UTF-8, detached, or not reconfigurable


def _vlen(s: str) -> int:
    """Visible length — strips ANSI escape codes before measuring."""
    return len(_ANSI_RE.sub("", s))


def _term_width() -> int:
    """Current terminal column count (minimum 60)."""
    return max(60, shutil.get_terminal_size((80, 24)).columns)


def _now_str() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


sys.path.insert(0, os.path.dirname(__file__))

from bridge import config
from bridge import api_keys
from bridge.agent import HarnessError
from bridge.sse_parser import DeepSeekStreamParser
from bridge.bridge_server import app, PORT, client

# ── Model map ─────────────────────────────────────────────────────────────────
# Single-model truth (verified 2026-10-01 via GET /api/v0/client/settings
# scope=model + Playwright): the server enables ONLY "default" ("Instant").
# "expert"/"vision" are enabled:false / switchable:false and the web UI has
# no model picker — search and thinking are independent per-message toggles.
# The old names stay as deprecated aliases resolving to the same model so
# existing configs and muscle memory keep working.
_MODEL_NAMES = {
    "chat":   ("deepseek-chat",   "default"),
}
_DEPRECATED_MODEL_ALIASES = {
    "expert": ("deepseek-chat", "default"),  # was R1 — disabled server-side
    "vision": ("deepseek-chat", "default"),  # was vision — disabled server-side
}

# ── File-upload directories ────────────────────────────────────────────────────
_BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR  = os.path.join(_BASE_DIR, "file_uploads")
DONE_DIR    = os.path.join(UPLOAD_DIR, "Done")
LOG_DIR     = os.path.join(_BASE_DIR, "logs")

# ── Session log ────────────────────────────────────────────────────
_dlog = logging.getLogger("deepseek")


def _log(msg: str) -> None:
    """Write an INFO entry to the shared session logger."""
    from bridge.diagnostics import redact
    if any(tag in msg for tag in ("[USER]", "[DEEPSEEK]", "[RETRY]", "[ERROR]")):
        _dlog.info("Terminal event (%d characters)", len(msg))
    else:
        _dlog.info(redact(msg))


def _create_session_log(session_id: str) -> None:
    """Rotate the per-session .log file in logs/ and re-attach the handler."""
    for _h in list(_dlog.handlers):
        _h.close()
        _dlog.removeHandler(_h)
    os.makedirs(LOG_DIR, exist_ok=True)
    ts    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    short = session_id[-8:] if session_id else "unknown"
    path  = os.path.join(LOG_DIR, f"session_{ts}_{short}.log")
    _fh = logging.FileHandler(path, encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _dlog.addHandler(_fh)
    from bridge.diagnostics import RedactingFilter
    _fh.addFilter(RedactingFilter())
    _dlog.setLevel(config.LOG_LEVEL)
    _dlog.propagate = False
    _dlog.info(f"=== Session started: {session_id} ===")
    print(f"  {GR}·{R}  {D}logs/session_{ts}_{short}.log{R}")


def _ensure_upload_dirs():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(DONE_DIR,   exist_ok=True)


# ── Start API server in daemon thread ─────────────────────────────────────────

def _start_api_server():
    try:
        from waitress import serve
        serve(app, host=config.BRIDGE_HOST, port=PORT,
              threads=config.BRIDGE_THREADS)
    except ImportError:
        app.run(host=config.BRIDGE_HOST, port=PORT, threaded=True, debug=False)


# ── File-upload helpers ────────────────────────────────────────────────────────

def _scan_and_upload(model_type: str, thinking: bool) -> list[str]:
    """
    Upload every file in file_uploads/ (non-recursively), move each one to
    file_uploads/Done/, and return the list of DeepSeek file IDs.
    """
    file_ids = []
    for fname in sorted(os.listdir(UPLOAD_DIR)):
        fpath = os.path.join(UPLOAD_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            fid = client.upload_file(fpath, model_type=model_type,
                                     thinking=thinking)
            final_status = client._last_upload_statuses.get(fid, "SUCCESS")
            file_ids.append(fid)
            dest = os.path.join(DONE_DIR, fname)
            # Avoid overwriting an existing file with the same name
            if os.path.exists(dest):
                base, ext = os.path.splitext(fname)
                dest = os.path.join(DONE_DIR,
                                    f"{base}_{int(os.path.getmtime(fpath))}{ext}")
            shutil.move(fpath, dest)
            if final_status not in ("SUCCESS", "TIMEOUT"):
                print(
                    f"  {YL}\u26a0  {B}{fname}{R}  [{YL}{final_status}{R}]\n"
                    f"     DeepSeek could not extract readable text from this file.\n"
                    f"     It was still attached — results may vary."
                )
            else:
                sc = GN if final_status == "SUCCESS" else YL
                print(f"  {GN}\u2191{R}  {fname}  {GR}\u2192{R}  "
                      f"{GR}{fid[:20]}\u2026{R}  [{sc}{final_status}{R}]")
            _log(f"[UPLOAD-SCAN] {fname} \u2192 {fid}")
        except Exception as exc:
            print(f"  {RD}\u2717{R}  {fname}: {exc}")
            _log(f"[UPLOAD-ERROR] {fname}: {exc}")
    return file_ids


# Matches:
#   @"path with spaces"   (double-quoted)
#   @'path with spaces'   (single-quoted)
#   @path_without_spaces  (unquoted, stops at whitespace)
_FILE_REF_RE = re.compile(r'@"([^"]+)"|@\'([^\']+)\'|@(\S+)')


def _parse_file_refs(text: str) -> tuple[str, list[str]]:
    """
    Extract @filepath tokens from user input (including paths with spaces
    when wrapped in quotes).  Returns (cleaned_text, list_of_paths).
    """
    paths: list[str] = []
    for m in _FILE_REF_RE.finditer(text):
        # exactly one of the three capture groups will be non-None
        paths.append(m.group(1) or m.group(2) or m.group(3))
    cleaned = _FILE_REF_RE.sub('', text).strip()
    return cleaned, paths


def _upload_ref_files(paths: list[str], model_type: str,
                      thinking: bool) -> list[str]:
    """Upload @filepath references and return file IDs."""
    file_ids = []
    for raw_path in paths:
        fpath = os.path.abspath(raw_path)
        if not os.path.isfile(fpath):
            print(f"  {YL}\u26a0{R}  File not found: {raw_path}")
            continue
        fname = os.path.basename(fpath)
        try:
            fid = client.upload_file(fpath, model_type=model_type,
                                     thinking=thinking)
            final_status = client._last_upload_statuses.get(fid, "SUCCESS")
            file_ids.append(fid)
            if final_status not in ("SUCCESS", "TIMEOUT"):
                print(
                    f"\n  {YL}\u26a0  {B}{fname}{R}  [{YL}{final_status}{R}]\n"
                    f"     DeepSeek could not extract readable text from this file.\n"
                    f"     It was still attached — results may vary.\n"
                )
            else:
                print(f"  {GN}\u2191{R}  {fname}  {GR}\u2192{R}  {GR}{fid[:20]}\u2026{R}")
            _log(f"[UPLOAD] {fname} \u2192 {fid}")
        except Exception as exc:
            print(f"  {RD}\u2717{R}  {fname}: {exc}")
            _log(f"[UPLOAD-ERROR] {os.path.basename(raw_path)}: {exc}")
    return file_ids

# \u2500\u2500 GUI file picker \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

def _pick_files_dialog(model_name: str, model_type: str) -> list[str]:
    """
    Open the native Windows file picker for multi-file selection.
    Returns a list of absolute file paths (empty if cancelled or unavailable).
    Single Instant model: one broad filter covering documents + images
    (server support_file_exts is a ~400-entry allow-list; the dialog shows
    the common ones plus All files).
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        print("[GUI] tkinter is not available. "
              "Use @filepath or drop files into file_uploads/ instead.")
        return []

    title  = "Select files to attach — DeepSeek Instant"
    ftypes = [
        ("Documents & Images",
         "*.pdf *.txt *.md *.docx *.doc *.xlsx *.xls *.csv *.pptx "
         "*.py *.js *.ts *.json *.html *.xml *.yaml *.yml "
         "*.png *.jpg *.jpeg *.gif *.webp *.bmp *.svg"),
        ("Documents",
         "*.pdf *.txt *.md *.docx *.doc *.xlsx *.xls *.csv "
         "*.py *.js *.ts *.json *.html *.xml"),
        ("Images",    "*.png *.jpg *.jpeg *.gif *.webp *.bmp *.svg"),
        ("All files", "*.*"),
    ]

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    paths = filedialog.askopenfilenames(title=title, filetypes=ftypes,
                                        parent=root)
    root.destroy()
    return list(paths)


# ══════════════════════════════════════════════════════════════════════════════
# CHAT UI — Spinner · Markdown renderer · Bubbles
# ══════════════════════════════════════════════════════════════════════════════

# ── Spinner ────────────────────────────────────────────────────────────────────
class Spinner:
    """Thread-safe braille spinner rendered on a single overwritten line."""

    _FRAMES = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"

    def __init__(self) -> None:
        self._stop   = threading.Event()
        self._thread: threading.Thread | None = None
        self._label  = ""
        self._alive  = False

    def start(self, label: str = "Thinking") -> None:
        self._label = label
        self._stop.clear()
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._alive:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        clear = " " * (_vlen(self._label) + 18)
        sys.stdout.write(f"\r{clear}\r")
        sys.stdout.flush()
        self._alive = False

    def _run(self) -> None:
        i = 0
        while not self._stop.wait(0.09):
            frame = self._FRAMES[i % len(self._FRAMES)]
            sys.stdout.write(
                f"\r  {CY}{frame}{R}  {D}{self._label}\u2026{R}   "
            )
            sys.stdout.flush()
            i += 1


# ── Inline markdown renderer ───────────────────────────────────────────────────
def _render_inline(text: str) -> str:
    """
    Apply inline markdown to a single line:
      **bold**   *italic*   ***bold+italic***   `code`   ~~strikethrough~~
    Backtick spans are protected before other patterns are applied.
    """
    # Phase 1 — extract inline code spans to protect their content
    parts: list[str] = []
    seg     = ""
    in_code = False
    for ch in text:
        if ch == "`":
            if in_code:
                parts.append(f"{BG_CODE}{FG_CODE}{seg}{R}")
                seg     = ""
                in_code = False
            else:
                parts.append(seg)
                seg     = ""
                in_code = True
        else:
            seg += ch
    parts.append(("`" + seg) if in_code else seg)
    text = "".join(parts)

    # Phase 2 — remaining inline patterns
    text = re.sub(r"\*\*\*(.+?)\*\*\*",
                  lambda m: f"{B}{IT}{WH}{m.group(1)}{R}", text)
    text = re.sub(r"\*\*(.+?)\*\*",
                  lambda m: f"{B}{WH}{m.group(1)}{R}", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)",
                  lambda m: f"{IT}{m.group(1)}{R}", text)
    text = re.sub(r"~~(.+?)~~",
                  lambda m: f"{D}{m.group(1)}{R}", text)
    return text


def _render_table(rows: list[str], indent: str) -> list[str]:
    """
    Render buffered markdown-table rows as a formatted box table with
    column-width-aware cell wrapping.  Returns pre-formatted output lines.
    """
    tw = _term_width()

    # ── Strip inline markdown markers and citation refs ────────────────────
    def _strip_md(text: str) -> str:
        text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
        text = re.sub(r"~~(.+?)~~",            r"\1", text)
        text = re.sub(r"`(.+?)`",              r"\1", text)
        text = re.sub(r"\[citation:\d+\]",     "",    text)
        return text.strip()

    # ── Parse: skip separator rows (|---|:---:|), split cells ─────────────
    data_rows: list[list[str]] = []
    for row in rows:
        stripped = row.strip()
        if re.match(r"^\|[\s\-:|]+\|$", stripped):
            continue
        cells = [_strip_md(c) for c in stripped.strip("|").split("|")]
        data_rows.append(cells)

    if not data_rows:
        return [f"{indent}{r.strip()}" for r in rows]   # fallback: plain

    num_cols = max(len(c) for c in data_rows)
    for row in data_rows:
        while len(row) < num_cols:
            row.append("")

    # ── Column widths (based on clean text) ───────────────────────────────
    nat   = [max(len(row[c]) for row in data_rows) for c in range(num_cols)]
    avail = tw - len(indent) - (num_cols + 1) - (num_cols * 2)
    if sum(nat) <= avail:
        col_w = nat[:]
    else:
        ratio = avail / max(sum(nat), 1)
        col_w = [max(6, int(n * ratio)) for n in nat[:-1]]
        used  = sum(w + 2 for w in col_w) + num_cols
        col_w.append(max(6, tw - len(indent) - used - 3))
    col_w = [max(w, 3) for w in col_w]

    # ── Helpers ────────────────────────────────────────────────────────────
    def _hbar(lc: str, mc_c: str, rc: str) -> str:
        return indent + GR + lc + mc_c.join("\u2500" * (w + 2) for w in col_w) + rc + R

    def _wcell(text: str, width: int) -> list[str]:
        wrapped = textwrap.wrap(text, width,
                                break_long_words=True, break_on_hyphens=False)
        return wrapped or ([""] if not text else [text[:width]])

    # ── Render ────────────────────────────────────────────────────────────
    out: list[str] = []
    out.append(_hbar("\u250c", "\u252c", "\u2510"))

    for ri, cells in enumerate(data_rows):
        wrapped = [_wcell(cells[c], col_w[c]) for c in range(num_cols)]
        n_lines = max(len(wc) for wc in wrapped)
        for li in range(n_lines):
            parts: list[str] = []
            for c in range(num_cols):
                txt = wrapped[c][li] if li < len(wrapped[c]) else ""
                pad = " " * max(0, col_w[c] - len(txt))
                if ri == 0:
                    parts.append(f" {B}{CY}{txt}{pad}{R} ")
                else:
                    parts.append(f" {txt}{pad} ")
            out.append(
                indent + GR + "\u2502" + R
                + (GR + "\u2502" + R).join(parts)
                + GR + "\u2502" + R
            )
        # separator after every row except the last
        if ri < len(data_rows) - 1:
            out.append(_hbar("\u251c", "\u253c", "\u2524"))

    out.append(_hbar("\u2514", "\u2534", "\u2518"))
    return out


# ── Streaming line-buffered markdown renderer ──────────────────────────────────
class _StreamRenderer:
    """
    Accumulates SSE tokens, buffers by newline, then emits each complete
    line rendered with markdown to stdout.
    """

    def __init__(self, indent: str = "  ") -> None:
        self._indent      = indent
        self._buf         = ""
        self._in_code_blk = False
        self._code_lang   = ""
        self.full_text    = ""
        self._table_buf: list[str] = []

    def feed(self, chunk: str) -> None:
        self._buf      += chunk
        self.full_text += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)

    def flush(self) -> None:
        self._flush_table()
        if self._buf:
            self._emit(self._buf)
            self._buf = ""

    def _flush_table(self) -> None:
        if not self._table_buf:
            return
        for tline in _render_table(self._table_buf, self._indent):
            sys.stdout.write(tline + "\n")
        sys.stdout.flush()
        self._table_buf = []

    def _emit(self, line: str) -> None:
        # Inside a fenced code block, never treat | lines as table rows
        if not self._in_code_blk and re.match(r"^\s*\|", line):
            self._table_buf.append(re.sub(r"\[citation:\d+\]", "", line))
            return
        # Flush any buffered table before rendering this non-table line
        self._flush_table()
        sys.stdout.write(self._render(line) + "\n")
        sys.stdout.flush()

    def _render(self, line: str) -> str:  # noqa: C901
        ind   = self._indent
        tw    = _term_width()
        bar_w = min(tw - len(ind) - 4, 60)

        # ── Inside a fenced code block ────────────────────────────────────
        if self._in_code_blk:
            if line.strip().startswith("```"):
                self._in_code_blk = False
                return f"{ind}{GR}\u2514{'─' * bar_w}\u2518{R}"
            return f"{ind}{GR}\u2502{R}  {FG_CBLK}{line}{R}"

        # Strip DeepSeek web-search citation markers from all non-code output
        line = re.sub(r"\[citation:\d+\]", "", line).rstrip()

        # ── Opening fence ─────────────────────────────────────────────────
        cb = re.match(r"^```(\w*)", line.strip())
        if cb:
            self._in_code_blk = True
            self._code_lang   = cb.group(1)
            lang_lbl = f" {self._code_lang} " if self._code_lang else "\u2500\u2500"
            fill     = max(0, bar_w - len(lang_lbl) - 1)
            return f"{ind}{GR}\u250c{YL}{lang_lbl}{GR}{'─' * fill}\u2510{R}"

        # ── ATX headings ──────────────────────────────────────────────────
        hm = re.match(r"^(#{1,6})\s+(.*)", line)
        if hm:
            lvl, txt = len(hm.group(1)), hm.group(2)
            if lvl == 1:
                bar = "\u2501" * min(len(txt) + 2, bar_w)
                return f"\n{ind}{B}{CY}{txt}{R}\n{ind}{CY}{bar}{R}"
            if lvl == 2:
                return f"\n{ind}{B}{WH}{txt}{R}"
            if lvl == 3:
                return f"\n{ind}{B}{GN}{txt}{R}"
            return f"{ind}{B}{D}{txt}{R}"

        # ── Horizontal rule ───────────────────────────────────────────────
        if re.match(r"^[-*_]{3,}\s*$", line):
            return f"{ind}{GR}{'─' * bar_w}{R}"

        # ── Blockquote ────────────────────────────────────────────────────
        if line.startswith("> "):
            return f"{ind}{CY}\u258c{R} {D}{_render_inline(line[2:])}{R}"

        # ── Unordered list ────────────────────────────────────────────────
        bm = re.match(r"^(\s{0,6})[*\-+]\s+(.*)", line)
        if bm:
            depth   = len(bm.group(1)) // 2
            bullets = ["\u25cf", "\u25e6", "\u25b8"]   # ● ◦ ▸
            colors  = [CY, GN, YL]
            return (f"{ind}{'  ' * depth}"
                    f"{colors[min(depth, 2)]}{bullets[min(depth, 2)]}{R}"
                    f"  {_render_inline(bm.group(2))}")

        # ── Ordered list ──────────────────────────────────────────────────
        nm = re.match(r"^(\d+)\.\s+(.*)", line)
        if nm:
            return f"{ind}{D}{nm.group(1)}.{R}  {_render_inline(nm.group(2))}"

        # ── Empty line ────────────────────────────────────────────────────
        if not line.strip():
            return ""

        # ── Regular paragraph ─────────────────────────────────────────────
        return f"{ind}{_render_inline(line)}"


# ── Chat bubble — user side ────────────────────────────────────────────────────
def _print_user_bubble(text: str, ts: str) -> None:
    """Print the user's message inside a green rounded-corner box."""
    tw        = _term_width()
    max_inner = min(tw - 4, 74)

    raw_lines: list[str] = []
    for para in text.split("\n"):
        if para.strip():
            raw_lines.extend(textwrap.wrap(para, max_inner) or [""])
        else:
            raw_lines.append("")
    if not raw_lines:
        raw_lines = [text[:max_inner]]

    inner_w = min(max(len(l) for l in raw_lines), max_inner)
    inner_w = max(inner_w, 20)

    you_str = "\u2500 You "     # "─ You "
    ts_str  = f" {ts} "
    fill    = max(0, inner_w + 2 - len(you_str) - len(ts_str))

    print()
    print(f"{GN}\u256d{you_str}{'─' * fill}{ts_str}\u256e{R}")
    for line in raw_lines:
        pad = inner_w - len(line)
        print(f"{GN}\u2502{R} {WH}{line}{R}{' ' * pad} {GN}\u2502{R}")
    print(f"{GN}\u2570{'─' * (inner_w + 2)}\u256f{R}")


# ── Chat section — AI side ─────────────────────────────────────────────────────
def _print_ai_header(model_name: str, model_type: str, ts: str) -> None:
    """Print DeepSeek turn header: icon + model + right-aligned timestamp + rule."""
    mc  = GN
    tw  = _term_width()
    tag = f"  {mc}\u25c6{R}  {B}DeepSeek{R}  {D}{model_name}{R}"
    gap = max(2, tw - _vlen(tag) - len(ts) - 2)
    print(f"\n{tag}{' ' * gap}{GR}{ts}{R}")
    print(f"  {GR}{'─' * min(tw - 4, 60)}{R}")


def _print_ai_footer(elapsed: float, char_count: int) -> None:
    """Print response metrics (char count + elapsed seconds + rate) after a turn."""
    tw   = _term_width()
    rate = char_count / max(elapsed, 0.1)
    stats = f" {char_count:,} chars \u00b7 {elapsed:.1f}s \u00b7 {rate:.0f} c/s "
    fill  = max(0, min(tw - len(stats) - 6, 56))
    print(f"\n  {GR}{'─' * fill}{D}{stats}{GR}\u2500{R}")


_THINK_OPEN = False
_THINK_RULE = f"  {YL}{chr(0x2504)}{chr(0x2500) * 58}{chr(0x2504)}{R}"
_STREAM_MESSAGE_ID = 0   # assistant message id of the last streamed turn


def _print_thinking_chunk(chunk: str) -> None:
    """
    Render Deep Think reasoning as it streams, dimmed and italic, framed by
    rules. Opens the block on first use; _close_thinking() ends it just
    before the answer starts.
    """
    global _THINK_OPEN
    if not _THINK_OPEN:
        if not chunk.strip():
            return
        _THINK_OPEN = True
        sys.stdout.write(f"{NL}{_THINK_RULE}{NL}  {D}{IT}reasoning:{R}{NL}  ")
        sys.stdout.flush()
    sys.stdout.write(f"{D}{IT}{chunk}{R}")
    sys.stdout.flush()


def _close_thinking() -> None:
    """Close the reasoning block, if one is open."""
    global _THINK_OPEN
    if _THINK_OPEN:
        _THINK_OPEN = False
        sys.stdout.write(f"{NL}{_THINK_RULE}{NL}")
        sys.stdout.flush()



# ── SSE → terminal with spinner + markdown ─────────────────────────────────────
def _stream_to_terminal(user_input: str, model_name: str, model_type: str,
                        search: bool, thinking: bool,
                        file_ids: list[str]) -> str:
    """
    Stream one user message through DeepSeek.
    Shows a braille spinner until the first token arrives, then renders the
    response incrementally with markdown.  Returns the full assistant reply.
    """
    global _STREAM_MESSAGE_ID
    spinner  = Spinner()
    renderer = _StreamRenderer(indent="  ")
    t_start  = time.monotonic()
    first    = True

    # Dynamic spinner label based on the active independent toggles
    if thinking and search:
        spinner_label = "Deep thinking \u00b7 web search"
    elif thinking:
        spinner_label = "Deep thinking"
    elif search:
        spinner_label = "Searching the web"
    else:
        spinner_label = "Thinking"

    spinner.start(spinner_label)
    parser = DeepSeekStreamParser()
    shown_reasoning = 0
    try:
        for line in client.chat_stream(
            user_input,
            model_type=model_type,
            thinking=thinking,
            search=search,
            file_ids=file_ids,
        ):
            # Same parser the HTTP server uses -- it understands the real
            # wire protocol, where the answer arrives as an APPEND to
            # response/fragments carrying typed RESPONSE objects.  The old
            # hand-rolled p/o scan here only matched patches whose path
            # contained "content", so replies never reached the screen.
            parser.feed_line(line)

            reasoning = parser.reasoning
            if len(reasoning) > shown_reasoning:
                if first:
                    spinner.stop()
                    _print_ai_header(model_name, model_type, _now_str())
                    first = False
                new_reasoning = reasoning[shown_reasoning:]
                shown_reasoning = len(reasoning)
                _print_thinking_chunk(new_reasoning)

            answer = parser.answer
            if len(answer) > len(renderer.full_text):
                if first:
                    spinner.stop()
                    _print_ai_header(model_name, model_type, _now_str())
                    first = False
                # Reasoning is done — close its block before the answer.
                _close_thinking()
                renderer.feed(answer[len(renderer.full_text):])

    except KeyboardInterrupt:
        # Ctrl+C mid-stream: keep whatever arrived, then stop cleanly.
        spinner.stop()
        _close_thinking()
        renderer.flush()
        print()
        print(f"  {YL}⚠{R}  Interrupted.")
        _log("[INTERRUPT] user cancelled stream")
        return renderer.full_text
    finally:
        if first:
            spinner.stop()   # stopped without any response tokens
        # Message id of the assistant turn, for /stop.
        _STREAM_MESSAGE_ID = parser.response_message_id or 0

    _close_thinking()
    renderer.flush()
    elapsed = time.monotonic() - t_start
    _print_ai_footer(elapsed, len(renderer.full_text))
    return renderer.full_text


# ── Helpers ───────────────────────────────────────────────────────────────────

def _short_id(session_id: str | None) -> str:
    if not session_id:
        return "none"
    return "…" + session_id[-8:]


def _print_status(model_name: str, model_type: str,
                  search: bool, thinking: bool) -> None:
    sid  = _short_id(client._chat_session_id)
    s_part  = f"search {B}on{R}" if search else f"search {D}off{R}"
    tk_part = f"think {B}on{R}"  if thinking else f"think {D}off{R}"
    up_part = f"files {B}yes{R}"
    sep  = f"  {GR}\u2502{R}  "
    print(f"  {GN}\u25cf  {B}{model_name}{R}{sep}{s_part}{sep}{tk_part}{sep}{up_part}{sep}{GR}{sid}{R}")


def _account_line() -> str:
    """
    Masked account label for /status and /whoami.  Falls back to the live
    profile endpoint because the stored email is usually None after a
    Playwright login.
    """
    email = getattr(client, "email", "") or ""
    if not email:
        try:
            email = (client.get_current_user() or {}).get("email") or ""
        except Exception:
            email = ""
    if not email:
        return ""
    if len(email) <= 8:
        return email
    return f"{email[:3]}{chr(0x2026)}{email[-6:]}"


def _print_status_detail() -> None:
    """
    The expanded /status panel: auth mode, endpoint, account and API-key
    state -- the things that decide whether a request will actually succeed.
    """
    print()
    print(f"  {B}{WH}Status{R}")
    print(f"  {GR}{'─' * 56}{R}")

    # The live Bearer token lives on _token; email is often None because the
    # web login only keeps credentials locally, so fall back to the API.
    if getattr(client, "_token", ""):
        mode, mcol = "authenticated", GN
    else:
        mode, mcol = "not signed in -- run /login", RD
    print(f"  {D}Auth{R}       {mcol}{mode}{R}")

    acct = _account_line()
    if acct:
        print(f"  {D}Account{R}    {acct}")

    print(f"  {D}API{R}        http://localhost:{PORT}/v1")
    print(f"  {D}Health{R}     http://localhost:{PORT}/health")

    try:
        n = len(api_keys.list_keys())
    except Exception:
        n = 0
    # Mirror bridge_server._check_api_key: any named key (even an unreadable key file) or
    # BRIDGE_API_KEY turns authentication on; REQUIRE_API_KEY alone only refuses to serve.
    single = bool(config.BRIDGE_API_KEY)
    if n or single or api_keys.key_exists():
        # Named keys take precedence: once any exists, BRIDGE_API_KEY is not consulted.
        what = f"{n} named key(s)" if n else ("BRIDGE_API_KEY" if single else "key file unreadable")
        gate = f"{what} · {B}auth required{R}"
    elif getattr(config, "REQUIRE_API_KEY", False):
        gate = f"{RD}auth required but no keys exist (requests are refused){R}"
    else:
        gate = f"no keys · {D}auth not required (API is open){R}"
    print(f"  {D}API keys{R}   {gate}")

    try:
        blob = (client.get_client_settings() or {}).get("settings") or {}
        parts = []
        for label, key in (("completion", "completion_request_timeout_ms"),
                           ("auto-resume", "auto_resume_request_timeout_ms")):
            raw = blob.get(key)
            # Settings arrive wrapped: {"id": ..., "value": ...}
            if isinstance(raw, dict):
                raw = raw.get("value")
            if raw:
                parts.append(f"{label} {raw}ms")
        if parts:
            print(f"  {D}Timeouts{R}   {' · '.join(parts)}")
    except Exception:
        pass
    print()


# ── Help box (also callable via /help command) ────────────────────────────────

def _print_help_box() -> None:
    """Print the command reference box. Called on startup and via /help."""
    tw = _term_width()
    bw = min(tw - 4, 72)

    def _hrow(cmd: str, desc: str) -> str:
        cmd_w   = 28
        content = f"  {B}{CY}{cmd:<{cmd_w}}{R}{GR}{desc}{R}"
        pad     = bw - _vlen(content)
        return f"  {CY}\u2502{R}{content}{' ' * max(0, pad)}{CY}\u2502{R}"

    def _srow(txt: str) -> str:
        pad = bw - len(txt)
        return f"  {CY}\u2502{R}  {D}{txt}{R}{' ' * max(0, pad - 2)}{CY}\u2502{R}"

    def _cat(label: str) -> str:
        pad = bw - len(label)
        return (f"  {CY}\u2502{R}  {B}{YL}{label}{R}"
                f"{' ' * max(0, pad - 2)}{CY}\u2502{R}")

    sub = f"API  \u00b7  http://localhost:{PORT}/v1  \u00b7  OpenAI-compatible"
    print()
    print(f"  {CY}\u256d{chr(0x2500) * bw}\u256e{R}")
    print(f"  {CY}\u2502{R}  {B}{WH}\u25c6  DeepSeek Bridge  "
          f"{D}v{config.VERSION}{R}{' ' * max(0, bw - 23 - len(config.VERSION))}{CY}\u2502{R}")
    print(f"  {CY}\u2502{R}  {D}{sub:<{bw - 2}}{R}{CY}\u2502{R}")
    print(f"  {CY}\u251c{chr(0x2500) * bw}\u2524{R}")
    print(_cat("Session"))
    for ln in [
        _hrow("/new",               "start a new conversation"),
        _hrow("/sessions",          "list recent sessions (then /load 3)"),
        _hrow("/load <id|num>",     "load & continue an existing session"),
        _hrow("/rename <title>",    "rename the current session"),
        _hrow("/pin  /unpin",       "pin or unpin the current session"),
        _hrow("/delete",            "delete the current session"),
        _hrow("/share",             "create a share link"),
    ]:
        print(ln)
    print(f"  {CY}\u251c{chr(0x2500) * bw}\u2524{R}")
    print(_cat("Model & Features"))
    for ln in [
        _hrow("/model",              "show model (single Instant — no picker)"),
        _hrow("/search on|off",     "toggle web search (independent)"),
        _hrow("/deepthink on|off",  "toggle deep think (independent)"),
    ]:
        print(ln)
    print(f"  {CY}\u251c{chr(0x2500) * bw}\u2524{R}")
    print(_cat("API Keys"))
    for ln in [
        _hrow("/api [name]",         "create an API key (prompts if no name)"),
        _hrow("/api-show",          "list all API keys with names & values"),
        _hrow("/api-delete <name>", "delete an API key by name"),
    ]:
        print(ln)
    print(f"  {CY}├{chr(0x2500) * bw}┤{R}")
    print(_cat("Account"))
    for ln in [
        _hrow("/status",            "model, features, auth & API keys"),
        _hrow("/whoami",            "show the signed-in DeepSeek account"),
        _hrow("/limits",            "server-side client settings & timeouts"),
        _hrow("/voices",            "list TTS voices for this account"),
    ]:
        print(ln)
    print(f"  {CY}├{chr(0x2500) * bw}┤{R}")
    print(_cat("Files & Tools"))
    for ln in [
        _hrow("/upload",            "open file picker GUI"),
        _hrow("/history",           "show this session message history"),
        _hrow("/retry",             "resend the last message + attachments"),
        _hrow("/edit <id> <text>",  "edit a message + regenerate"),
        _hrow("/export",            "trigger history export + poll URL (NEW)"),
        _hrow("/stop",              "stop the last streamed reply"),
        _hrow("/clear",             "clear the terminal screen"),
        _hrow("/help",              "show this help"),
        _hrow("/q  /quit  /exit",   "quit"),
    ]:
        print(ln)
    print(f"  {CY}\u251c{chr(0x2500) * bw}\u2524{R}")
    print(_srow('Attach:  @"path/to/file"  or drop files into  file_uploads/'))
    print(f"  {CY}\u2570{chr(0x2500) * bw}\u256f{R}")


# ── Terminal chatbot ───────────────────────────────────────────────────────────

def run_chatbot() -> None:
    model_key  = "chat"
    model_name, model_type = _MODEL_NAMES[model_key]
    search         = True    # server default is on (search_state_on_launch=on)
    thinking       = False   # deep think defaults off; independent toggle
    _pending_files: list[str] = []
    _last_prompt:   str       = ""        # for /retry
    _last_files:    list[str] = []        # attachments from the last turn
    _last_sessions: list[dict] = []       # for numbered /load
    _turn          = 0                    # conversation turn counter

    # ── Help banner ────────────────────────────────────────────────────────
    _print_help_box()
    print()
    _print_status(model_name, model_type, search, thinking)
    print()

    while True:
        try:
            user_input = input(
                f"\n  {B}{WH}You{R}  {D}{model_name}{R}  {GN}\u203a{R} "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n  {GR}Goodbye!{R}")
            break

        if not user_input:
            continue

        lower = user_input.lower()

        # ── Commands ──────────────────────────────────────────────────────
        if lower in ("/q", "/quit", "/exit"):
            print(f"\n  {GR}Goodbye!{R}")
            break

        if lower == "/new":
            _log("[CMD] /new — starting fresh session")
            session_id = client.new_chat()
            _create_session_log(session_id)
            print(f"  {GN}●{R}  New session  {D}#{session_id[-8:]}{R}")
            _print_status(model_name, model_type, search, thinking)
            continue

        if lower == "/status":
            print()
            _print_status(model_name, model_type, search, thinking)
            waiting = [f for f in os.listdir(UPLOAD_DIR)
                       if os.path.isfile(os.path.join(UPLOAD_DIR, f))]
            if waiting:
                print(f"  {D}Pending in file_uploads/:  {', '.join(waiting)}{R}")
            if _pending_files:
                print(f"  {D}Queued via /upload ({len(_pending_files)}):{R}")
                for p in _pending_files:
                    print(f"    {D}·  {os.path.basename(p)}{R}")
            _print_status_detail()
            continue

        if lower == "/model" or lower.startswith("/model "):
            # Single-model backend: no picker.  Bare /model shows status;
            # expert/vision are accepted as deprecated aliases (still the
            # same Instant model) so old muscle memory doesn't error out.
            arg = lower.split(None, 1)[1].lstrip("/") if " " in lower else ""
            if not arg or arg == "chat":
                print(f"  {GN}\u25cf{R}  Model: {B}{model_name}{R}  {D}(single Instant model — "
                      f"no picker on the website){R}")
                _print_status(model_name, model_type, search, thinking)
            elif arg in _DEPRECATED_MODEL_ALIASES:
                print(f"  {YL}\u26a0{R}  '{arg}' was removed from the website "
                      f"(server: enabled=false).")
                print(f"     Staying on {B}{model_name}{R} — search/deepthink "
                      f"are independent toggles now.")
                _log(f"[CMD] /model {arg} (deprecated alias, no-op)")
                _print_status(model_name, model_type, search, thinking)
            else:
                print(f"  {YL}\u26a0{R}  Unknown model '{arg}'.  "
                      f"Only {B}deepseek-chat{R} exists now.")
            continue

        if lower in ("/search on", "/search off"):
            # Independent toggle on the single model (server default: on).
            search = lower.endswith("on")
            print(f"  {GN}\u2713{R}  Web search {'enabled' if search else 'disabled'}")
            _log(f"[CMD] /search {'on' if search else 'off'}")
            _print_status(model_name, model_type, search, thinking)
            continue

        if lower in ("/deepthink on", "/deepthink off"):
            # Independent toggle on the single model (defaults off).
            # Combines freely with /search (conflict_with_search=false).
            thinking = lower.endswith("on")
            print(f"  {GN}\u2713{R}  Deep Think {'enabled' if thinking else 'disabled'}")
            _log(f"[CMD] /deepthink {'on' if thinking else 'off'}")
            _print_status(model_name, model_type, search, thinking)
            continue

        if lower == "/upload":
            paths = _pick_files_dialog(model_name, model_type)
            if paths:
                _pending_files.extend(paths)
                print(f"  {GN}✓{R}  {len(paths)} file(s) queued — will upload with your next message")
                for p in paths:
                    print(f"    {D}·  {os.path.basename(p)}{R}")
                _log(f"[CMD] /upload — queued: {', '.join(os.path.basename(p) for p in paths)}")
            else:
                print(f"  {D}·  No files selected{R}")
            continue

        if lower == "/share":
            sid = client._chat_session_id
            if not sid:
                print(f"  {YL}⚠{R}  No active session — send a message first.")
                continue
            try:
                share_id = client.create_share(sid, [])
                url = f"https://chat.deepseek.com/a/chat/s/{share_id}"
                print(f"  {GN}✓{R}  Share link created:")
                print(f"     {CY}{url}{R}")
                _log(f"[CMD] /share → {url}")
            except Exception as exc:
                print(f"  {RD}✗{R}  Could not create share: {exc}")
            continue

        if lower == "/sessions":
            spinner = Spinner()
            spinner.start("Loading sessions")
            try:
                sessions = client.list_sessions(count=20)
                spinner.stop()
                _last_sessions = sessions
                if not sessions:
                    print(f"  {D}No sessions found.{R}")
                else:
                    print(f"\n  {B}{CY}Recent sessions  {GR}(latest first){R}")
                    print(f"  {GR}{'─' * 56}{R}")
                    for i, s in enumerate(sessions[:20], 1):
                        sid    = s.get("id", "")
                        title  = (s.get("title") or "(untitled)")[:40]
                        short  = "\u2026" + sid[-8:] if len(sid) > 8 else sid
                        pin    = f"  {YL}\u25c6{R}" if s.get("pinned") else ""
                        active = f"  {GN}\u25cf{R}" if sid == client._chat_session_id else ""
                        print(f"  {GR}{i:>2}.{R}  {D}{short}{R}  {title}{pin}{active}")
                    print(f"\n  {D}Use  /load <number>  or  /load <full-id>{R}")
                    print()
            except Exception as exc:
                spinner.stop()
                print(f"  {RD}\u2717{R}  {exc}")
            continue

        if lower.startswith("/load "):
            sid_arg = user_input[6:].strip()
            if not sid_arg:
                print(f"  {YL}\u26a0{R}  Usage:  /load <session_id or list number>")
                continue
            # Support number-based selection from the last /sessions list
            if sid_arg.isdigit():
                idx = int(sid_arg) - 1
                if not _last_sessions:
                    print(f"  {YL}\u26a0{R}  Run  /sessions  first to load by number.")
                    continue
                if idx < 0 or idx >= len(_last_sessions):
                    print(f"  {YL}\u26a0{R}  Number out of range. "
                          f"Run  /sessions  to see the list.")
                    continue
                sid_arg = _last_sessions[idx]["id"]
            try:
                client.load_and_resume_session(sid_arg)
                _last_prompt = ""
                _last_files  = []
                _create_session_log(sid_arg)
                print(f"  {GN}\u25cf{R}  Loaded session  {D}\u2026{sid_arg[-8:]}{R}")
                _print_status(model_name, model_type, search, thinking)
            except Exception as exc:
                print(f"  {RD}\u2717{R}  Could not load session: {exc}")
            continue

        if lower.startswith("/rename "):
            title_arg = user_input[8:].strip()
            if not title_arg:
                print(f"  {YL}\u26a0{R}  Usage:  /rename <new title>")
                continue
            sid = client._chat_session_id
            if not sid:
                print(f"  {YL}\u26a0{R}  No active session — use /new first.")
                continue
            try:
                client.rename_session(sid, title_arg)
                print(f"  {GN}\u2713{R}  Session renamed to  {B}{title_arg}{R}")
                _log(f"[CMD] /rename → '{title_arg}'")
            except Exception as exc:
                print(f"  {RD}\u2717{R}  {exc}")
            continue

        if lower in ("/pin", "/unpin"):
            sid = client._chat_session_id
            if not sid:
                print(f"  {YL}\u26a0{R}  No active session — use /new first.")
                continue
            pin_val = (lower == "/pin")
            try:
                client.pin_session(sid, pin_val)
                action = "Pinned" if pin_val else "Unpinned"
                icon   = YL + "\u25c6" + R if pin_val else GR + "\u25c7" + R
                print(f"  {icon}  {action}  {D}\u2026{sid[-8:]}{R}")
                _log(f"[CMD] {'pin' if pin_val else 'unpin'} session {sid}")
            except Exception as exc:
                print(f"  {RD}\u2717{R}  {exc}")
            continue

        if lower == "/delete":
            sid = client._chat_session_id
            if not sid:
                print(f"  {YL}\u26a0{R}  No active session.")
                continue
            try:
                confirm = input(
                    f"  {YL}\u26a0  Delete session  {D}\u2026{sid[-8:]}{R}?  "
                    f"Type  {B}yes{R}  to confirm:  "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(f"  {D}Cancelled.{R}")
                continue
            if confirm != "yes":
                print(f"  {D}Cancelled.{R}")
                continue
            try:
                client.delete_session(sid)
                client._chat_session_id = None
                client._last_parent_id  = None
                _last_prompt = ""
                _last_files  = []
                print(f"  {GN}\u2713{R}  Session deleted.  "
                      f"Use  {B}/new{R}  to start a fresh session.")
                _log(f"[CMD] /delete — deleted session {sid}")
            except Exception as exc:
                print(f"  {RD}\u2717{R}  {exc}")
            continue

        if lower == "/history":
            sid = client._chat_session_id
            if not sid:
                print(f"  {YL}\u26a0{R}  No active session — use /new first.")
                continue
            spinner = Spinner()
            spinner.start("Loading history")
            try:
                data = client.get_history(sid)
                spinner.stop()
                # History messages carry NO top-level "content" -- the text
                # lives in fragments[].content, typed REQUEST / RESPONSE /
                # THINK / SEARCH.  Filtering on m["content"] matched nothing,
                # which is why /history always came back empty.
                msgs = []
                for _m in (data.get("chat_messages") or []):
                    _role = (_m.get("role") or "").upper()
                    for _frag in (_m.get("fragments") or []):
                        _ftype = _frag.get("type") or ""
                        _text  = (_frag.get("content") or "").strip()
                        if not _text or _ftype not in ("REQUEST", "RESPONSE",
                                                        "THINK"):
                            continue
                        msgs.append({"role": _role, "type": _ftype,
                                     "text": _text})
                if not msgs:
                    print(f"  {D}No messages in this session yet.{R}")
                else:
                    print(f"\n  {B}{CY}Conversation history  "
                          f"{GR}({len(msgs)} fragments){R}")
                    print(f"  {GR}{'─' * 56}{R}")
                    for m in msgs:
                        content = m["text"]
                        preview = content[:90].replace("\n", " ")
                        if len(content) > 90:
                            preview += "\u2026"
                        if m["type"] == "REQUEST":
                            print(f"  {GN}You:  {R}  {preview}")
                        elif m["type"] == "THINK":
                            print(f"  {YL}think:{R} {D}{preview}{R}")
                        else:
                            print(f"  {CY}AI:   {R}  {D}{preview}{R}")
                    print()
            except Exception as exc:
                spinner.stop()
                print(f"  {RD}\u2717{R}  {exc}")
            continue

        if lower == "/retry":
            if not _last_prompt:
                print(f"  {YL}\u26a0{R}  No previous message to retry.")
                continue
            preview = _last_prompt[:60] + ("\u2026" if len(_last_prompt) > 60 else "")
            print(f"  {D}\u2935  Retrying:  {preview}{R}")
            _print_user_bubble(_last_prompt, _now_str())
            try:
                response = _stream_to_terminal(
                    _last_prompt,
                    model_name=model_name,
                    model_type=model_type,
                    search=search,
                    thinking=thinking,
                    file_ids=list(_last_files),
                )
                _log(f"[RETRY] {response}")
            except Exception as exc:
                print(f"\n  {RD}\u2717  Error:{R}  {exc}")
                _log(f"[ERROR] {exc}")
            continue

        if lower.startswith("/edit "):
            # /edit <message_id> <new text> — edit a USER message in place.
            # Upstream POST /api/v0/chat/edit_message (new bridge route
            # POST /v1/sessions/<id>/edit). Not in HAR window — unverified.
            parts = user_input.split(None, 2)
            if len(parts) < 3 or not parts[1].isdigit():
                print(f"  {YL}\u26a0{R}  Usage:  /edit <message_id> <new text>")
                print(f"     Find IDs via  {CY}/history{R}  (USER message numbers).")
                continue
            sid = client._chat_session_id
            if not sid:
                print(f"  {YL}\u26a0{R}  No active session — use /new first.")
                continue
            try:
                from bridge.sse_parser import DeepSeekStreamParser as _P
                _parser = _P()
                _hdr_printed = False
                for line in client.edit_message_stream(
                        sid, int(parts[1]), parts[2],
                        thinking=thinking, search=search):
                    _parser.feed_line(line)
                    if _parser.answer and not _hdr_printed:
                        _print_ai_header(model_name, model_type, _now_str())
                        _hdr_printed = True
                        print(f"  {_parser.answer}")
                    elif _parser.answer:
                        pass
                print(f"  {GN}\u2713{R}  Edit applied (msg {parts[1]}).")
                _log(f"[CMD] /edit {parts[1]}")
            except Exception as exc:
                print(f"  {RD}\u2717{R}  {exc}")
            continue

        if lower == "/export":
            try:
                info = client.trigger_export()
                print(f"  {GN}\u2713{R}  Export triggered.")
                url = client.get_export_download()
                if url:
                    print(f"     {CY}{url}{R}")
                else:
                    print(f"  {D}·  Not ready yet (TASK_EXPIRED/p pending) — "
                          f"poll  GET /v1/export/download{R}")
                _log(f"[CMD] /export → {info}")
            except Exception as exc:
                print(f"  {RD}\u2717{R}  {exc}")
            continue

        # ── Account / session info ────────────────────────────────────────
        if lower == "/whoami":
            spinner = Spinner()
            spinner.start("Fetching account")
            try:
                user = client.get_current_user()
                spinner.stop()
                email = user.get("email") or "(hidden)"
                name  = (user.get("name") or "").strip()
                print(f"  {D}Email{R}    {B}{email}{R}")
                if name:
                    print(f"  {D}Name{R}     {B}{name}{R}")
                print(f"  {D}User ID{R}  {D}{user.get('id', 'n/a')}{R}")
                if user.get("is_mainland") is not None:
                    region = "CN" if user.get("is_mainland") else "INTL"
                    print(f"  {D}Region{R}   {region}")
                _log("[CMD] /whoami")
            except Exception as exc:
                spinner.stop()
                print(f"  {RD}✗{R}  {exc}")
            continue

        if lower == "/limits":
            spinner = Spinner()
            spinner.start("Reading client settings")
            try:
                data = client.get_client_settings() or {}
                spinner.stop()
                blob = data.get("settings") or {}
                print(f"  {D}{'─' * 56}{R}")
                shown = 0
                for k in sorted(blob):
                    val = blob[k]
                    if isinstance(val, dict) and "value" in val:
                        val = val["value"]
                    print(f"  {D}{k}{R}  {val}")
                    shown += 1
                if not shown:
                    print(f"  {D}No settings returned.{R}")
                print(f"  {D}{'─' * 56}{R}")
                _log("[CMD] /limits")
            except Exception as exc:
                spinner.stop()
                print(f"  {RD}✗{R}  {exc}")
            continue

        if lower == "/voices":
            spinner = Spinner()
            spinner.start("Fetching TTS voices")
            try:
                voices = client.get_tts_voices() or []
                spinner.stop()
                if not voices:
                    print(f"  {D}No TTS voices available for this account.{R}")
                else:
                    print(f"  {B}{WH}TTS voices{R}  {D}({len(voices)}){R}")
                    print(f"  {GR}{'─' * 56}{R}")
                    for v in voices:
                        vid = v.get("voice_id") or v.get("id") or "?"
                        nm  = v.get("name") or v.get("voice_name") or vid
                        lang = v.get("lang") or v.get("language") or ""
                        print(f"  {CY}{vid}{R}  {nm}{D}  {lang}{R}")
                    print()
                _log("[CMD] /voices")
            except Exception as exc:
                spinner.stop()
                print(f"  {RD}✗{R}  {exc}")
            continue

        if lower == "/stop":
            sid = client._chat_session_id
            mid = _STREAM_MESSAGE_ID
            if not sid or not mid:
                print(f"  {YL}⚠{R}  No completed response to stop — "
                      f"DeepSeek streams are stopped from the web UI.")
                continue
            try:
                client.stop_stream(sid, mid)
                print(f"  {GN}✓{R}  Stop requested for message {mid}.")
            except Exception as exc:
                print(f"  {RD}✗{R}  {exc}")
            continue

        # ── API Key Management ──────────────────────────────────────────
        if lower == "/api" or lower.startswith("/api "):
            # Create a new API key.  "/api <name>" names it inline;
            # a bare "/api" prompts for the name.
            # Take the name from the ORIGINAL input, not `lower` — key names
            # are user-chosen labels and lowercasing them made /api-delete
            # unable to find a key created as "/api MyKey".
            inline_name = user_input[5:].strip() if lower.startswith("/api ") else ""
            try:
                name = (inline_name or input(
                    f"  {D}Enter a name for this API key: {R}").strip())
            except (EOFError, KeyboardInterrupt):
                print(f"\n  {GR}Cancelled.{R}")
                continue
            if not name:
                print(f"  {YL}⚠{R}  Name cannot be empty.")
                continue
            try:
                key_value = api_keys.create_key(name)
                print(f"\n  {GN}✓{R}  API key created!")
                print(f"     {D}Name:{R}  {B}{name}{R}")
                # No ANSI inside the key itself — a stray escape sequence
                # copied out of the terminal silently breaks the key.
                print(f"     {D}Key:{R}   {B}{key_value}{R}")
                print(f"     {D}Use:{R}   Authorization: Bearer <key>")
                print(f"  {YL}⚠{R}  Save this key — use {CY}/api-show{R} to list it later.\n")
                _log(f"[CMD] /api — created key '{name}'")
            except ValueError as exc:
                print(f"  {RD}✗{R}  {exc}")
            continue

        if lower == "/api-show":
            # List all API keys with names and full values
            keys = api_keys.list_keys()
            if not keys:
                print(f"  {D}No API keys found. Use {CY}/api{R} {D}to create one.{R}")
            else:
                print(f"\n  {B}{WH}API Keys{R}  ({len(keys)} total)")
                print(f"  {GR}{'─' * 60}{R}")
                for i, k in enumerate(keys, 1):
                    created = ""
                    if k.get("created_at"):
                        created = datetime.datetime.fromtimestamp(
                            k["created_at"]
                        ).strftime("%Y-%m-%d %H:%M")
                    last = ""
                    if k.get("last_used"):
                        last = datetime.datetime.fromtimestamp(
                            k["last_used"]
                        ).strftime("%Y-%m-%d %H:%M")
                    print(f"  {B}{i}.{R}  {CY}{k['name']}{R}")
                    # Key printed bare -- an ANSI escape copied along with
                    # it would corrupt the key the user pastes into curl.
                    print(f"     {D}Key:{R}     {k['key']}")
                    if created:
                        print(f"     {D}Created:{R}  {created}")
                    if last:
                        print(f"     {D}Last used:{R} {last}")
                    print()
                print(f"  {GR}{'─' * 60}{R}")
                print(f"  {D}Delete with  {CY}/api-delete <name>{R}\n")
                _log(f"[CMD] /api-show — listed {len(keys)} key(s)")
            continue

        if lower.startswith("/api-delete "):
            # Delete an API key by name
            name = user_input.split(None, 1)[1].strip()
            if not name:
                print(f"  {YL}⚠{R}  Usage: {CY}/api-delete <name>{R}")
                continue
            if api_keys.delete_key(name):
                print(f"  {GN}✓{R}  API key '{name}' deleted.")
                _log(f"[CMD] /api-delete — deleted key '{name}'")
            else:
                print(f"  {YL}⚠{R}  No API key named '{name}' found.")
            continue

        if lower == "/clear":
            os.system("cls" if os.name == "nt" else "clear")
            _print_help_box()
            print()
            _print_status(model_name, model_type, search, thinking)
            print()
            continue

        if lower == "/help":
            _print_help_box()
            continue

        if lower.startswith("/"):
            print(f"  {YL}\u26a0{R}  Unknown command. Type  {B}/help{R}  to see all commands.")
            continue

        # ── Resolve file attachments ──────────────────────────────────────
        # 1. @filepath tokens in the message
        prompt, ref_paths = _parse_file_refs(user_input)
        if not prompt:
            prompt = user_input  # fallback: keep original if nothing left

        file_ids: list[str] = []

        if ref_paths:
            file_ids.extend(
                _upload_ref_files(ref_paths, model_type, thinking)
            )

        # 2. Files queued via /upload
        if _pending_files:
            file_ids.extend(
                _upload_ref_files(_pending_files, model_type, thinking)
            )
            _pending_files.clear()

        # 3. Auto-scan file_uploads/ folder
        auto_ids = _scan_and_upload(model_type, thinking)
        if auto_ids:
            file_ids.extend(auto_ids)

        # ── Chat turn ─────────────────────────────────────────────────────
        _turn += 1
        _last_prompt = prompt
        _last_files = list(file_ids)
        _log(f"[TURN {_turn}] [USER] {prompt}")
        if file_ids:
            _log(f"[ATTACHED] {len(file_ids)} file(s): {', '.join(file_ids)}")

        _print_user_bubble(prompt, _now_str())

        try:
            response = _stream_to_terminal(
                prompt,
                model_name=model_name,
                model_type=model_type,
                search=search,
                thinking=thinking,
                file_ids=file_ids,
            )
            _log(f"[TURN {_turn}] [DEEPSEEK] {response}")
        except Exception as exc:
            print(f"\n  {RD}\u2717  Error:{R}  {exc}")
            _log(f"[ERROR] {exc}")
            continue


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from bridge.diagnostics import configure_logging
    parser = argparse.ArgumentParser(description="Standalone AI coding harness and API")
    parser.add_argument("--legacy-chat", action="store_true", help="Use the original DeepSeek web chat UI")
    parser.add_argument("--api-only", action="store_true", help="Run API without terminal input")
    parser.add_argument("--no-api", action="store_true", help="Run the native terminal without binding an API port")
    parser.add_argument("--resume", metavar="SESSION_ID", help="Resume an existing native terminal session")
    parser.add_argument("-c", "--continue", dest="continue_latest", action="store_true",
                        help="Resume the most recent native terminal session")
    parser.add_argument("-p", "--prompt", metavar="TEXT", help="Run one prompt non-interactively, print the result and exit")
    parser.add_argument("--output-format", choices=("text", "json", "stream-json"), default="text",
                        help="Output format for --prompt (default: text)")
    parser.add_argument("--auto-approve", action="store_true",
                        help="Approve command/web actions without asking (use in trusted workspaces only)")
    parser.add_argument("--mode", choices=("plan", "build"), help="Start in plan (read-only) or build mode")
    parser.add_argument("--max-turns", type=int, metavar="N", help="Limit model turns per run (overrides HARNESS_MAX_TURNS)")
    parser.add_argument("--doctor", action="store_true", help="Diagnose configuration and dependencies, then exit")
    parser.add_argument("--live", action="store_true", help="With --doctor: also make one real provider request")
    args = parser.parse_args()
    if args.no_api and (args.api_only or args.legacy_chat):
        parser.error("--no-api is available only for the native terminal")
    if args.continue_latest and (args.resume or args.api_only or args.legacy_chat):
        parser.error("--continue cannot be combined with --resume, --api-only or --legacy-chat")
    if args.resume and (args.api_only or args.legacy_chat):
        parser.error("--resume is available only for the native terminal")
    if (args.prompt or args.doctor) and (args.api_only or args.legacy_chat):
        parser.error("--prompt and --doctor are available only for the native harness")
    if args.max_turns is not None:
        if args.max_turns < 1:
            parser.error("--max-turns must be at least 1")
        config.MAX_AGENT_TURNS = args.max_turns
    configure_logging()
    if config.PROVIDER == "deepseek-web" and not args.doctor:
        # A browser export dropped into cookies/ becomes the login without any captcha-prone sign-in.
        from bridge import session_import
        try:
            _imported = session_import.auto_import()
        except Exception as exc:  # noqa: BLE001 - a bad export must never stop the harness starting
            _imported = None
            print(f"Cookie import skipped ({type(exc).__name__}).", file=sys.stderr)
        if _imported is not None:
            print("Imported a browser session from cookies/:", file=sys.stderr)
            print("\n".join(session_import.describe(_imported)), file=sys.stderr)
            client.reload_session()
    if not (args.doctor or args.prompt is not None or args.no_api):
        from bridge.doctor import exposure_warning
        _exposed = exposure_warning()
        if _exposed:
            print("WARNING: " + _exposed, file=sys.stderr)
    if args.doctor:
        from bridge.bridge_server import runtime
        from bridge.doctor import collect, format_report
        items = collect(runtime, live=args.live, check_port=True)
        print(format_report(items, color=sys.stdout.isatty()))
        sys.exit(1 if any(level == "fail" for level, _, _ in items) else 0)
    if args.continue_latest:
        from bridge.bridge_server import runtime
        args.resume = runtime.latest("terminal")
        if args.resume is None:
            print("No saved terminal session to continue.", file=sys.stderr)
            sys.exit(1)
    if args.prompt is not None:
        from bridge.bridge_server import runtime
        from bridge.terminal import run_once
        sys.exit(run_once(runtime, args.prompt, session_id=args.resume, output_format=args.output_format,
                          auto_approve=args.auto_approve, mode=args.mode))
    if args.api_only:
        _start_api_server()
        sys.exit(0)
    if not args.legacy_chat:
        from bridge.bridge_server import runtime
        from bridge.terminal import run_terminal
        api_server = None
        if not args.no_api:
            try:
                from waitress import create_server
                api_server = create_server(app, host=config.BRIDGE_HOST, port=PORT,
                                           threads=config.BRIDGE_THREADS)
            except Exception as exc:
                print(f"API startup failed ({type(exc).__name__}). Check BRIDGE_HOST, BRIDGE_PORT, and installed requirements; use --no-api for terminal only.")
                sys.exit(1)
            threading.Thread(target=api_server.run, daemon=True).start()
            print(f"API: http://localhost:{api_server.effective_port}/v1")
        exit_code = 0
        try:
            run_terminal(runtime, session_id=args.resume, auto_approve=args.auto_approve, mode=args.mode)
        except HarnessError as exc:  # e.g. --resume with an unknown or foreign session id
            print(f"Error: {exc}", file=sys.stderr)
            exit_code = 1
        finally:
            if api_server is not None:
                api_server.close()
        sys.exit(exit_code)
    _ensure_upload_dirs()

    # ── Startup banner ─────────────────────────────────────────────────────
    tw = _term_width()
    bw = min(tw - 4, 64)

    py_ver  = platform.python_version()
    os_name = f"{platform.system()} {platform.release()}"
    api_url = f"http://localhost:{PORT}/v1"

    # Backslash escapes are not allowed inside f-string expressions before Python
    # 3.12 (the README promises 3.10+), so the pieces are built first.
    rule = "\u2500" * bw
    tagline = "OpenAI-compatible  \u00b7  streaming  \u00b7  file upload"
    runtime_line = f"Python {py_ver}  \u00b7  {os_name}"
    title_pad = " " * max(0, bw - 23 - len(config.VERSION))

    print()
    print(f"  {CY}\u256d{rule}\u256e{R}")
    print(f"  {CY}\u2502{R}  {B}{WH}\u25c6  DeepSeek Bridge  "
          f"{D}v{config.VERSION}{R}"
          f"{title_pad}{CY}\u2502{R}")
    print(f"  {CY}\u2502{R}  {D}{api_url:<{bw - 2}}{R}{CY}\u2502{R}")
    print(f"  {CY}\u2502{R}  {D}{tagline:<{bw - 2}}{R}{CY}\u2502{R}")
    print(f"  {CY}\u2502{R}  {D}{runtime_line:<{bw - 2}}{R}{CY}\u2502{R}")
    print(f"  {CY}\u2570{rule}\u256f{R}")

    # ── Start API server ───────────────────────────────────────────────────
    t = threading.Thread(target=_start_api_server, daemon=True)
    t.start()
    print(f"\n  {GN}\u2713{R}  API server  {GR}\u2192{R}  {D}{api_url}{R}")

    # ── Pre-flight auth check ──────────────────────────────────────────────
    sys.stdout.write(f"  {D}\u00b7  Checking session\u2026{R}")
    sys.stdout.flush()
    try:
        client.ensure_authenticated()
        sys.stdout.write(f"\r  {GN}\u2713{R}  Session authenticated           \n")
    except RuntimeError as exc:
        sys.stdout.write(f"\r  {YL}\u26a0{R}  {str(exc).strip()}                \n")
        sys.stdout.write(
            f"     Run:  {B}{CY}python refresh_session.py{R}  then restart.\n\n"
        )
    sys.stdout.flush()

    # ── Create persistent chat session ────────────────────────────────────
    session_id = client.new_chat()
    _create_session_log(session_id)
    print(f"  {GN}\u2713{R}  Session  {D}#{session_id[-8:]}{R}")
    print()

    # Hand off to the interactive chatbot (blocks until user exits)
    run_chatbot()

