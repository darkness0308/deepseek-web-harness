"""Metadata-only diagnostics; provider bodies, prompts and credentials stay out."""
import logging
import logging.handlers
from pathlib import Path
import re

from . import config


def redact(value):
    text = str(value)
    for secret in (config.DS_PASSWORD, config.DS_EMAIL, config.BRIDGE_API_KEY, config.PROVIDER_API_KEY):
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[redacted]", text)
    text = re.sub(r"\b(?:sk-|dsb_)[A-Za-z0-9_-]+", "[redacted]", text)
    return text


def safe_message(exc):
    from .agent import HarnessError
    if isinstance(exc, HarnessError):
        return redact(str(exc))
    return f"{type(exc).__name__}: request failed; see local diagnostics"


class RedactingFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        # A traceback may contain upstream bodies/URLs.
        record.exc_info = None
        record.exc_text = None
        return True


def configure_logging():
    directory = Path(__file__).parent.parent / "logs"
    directory.mkdir(exist_ok=True)
    # Rotate instead of growing forever: 5 MB x 3 files keeps diagnostics useful.
    handler = logging.handlers.RotatingFileHandler(directory / "harness.log", maxBytes=5_000_000,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactingFilter())
    for name in ("harness", "deepseek", "bridge.bridge_server"):
        logger = logging.getLogger(name)
        for previous in logger.handlers:
            previous.close()
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(config.LOG_LEVEL)
        logger.propagate = False
