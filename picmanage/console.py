"""Make the CLI's Chinese output render correctly on any Windows console.

Python picks its stdout encoding from the *locale* (``cp936`` on a Chinese
Windows), but what actually decides how the terminal renders bytes is the
*console output code page*.  Those disagree often enough -- Windows Terminal
running with UTF-8 while the locale is still GBK -- that the CLI would print
mojibake.

The fix is to match the console code page rather than the locale.  When stdout
is redirected to a file or pipe there is no console, and UTF-8 is the better
default for anything that will read the log later.
"""

from __future__ import annotations

import sys

#: Console code page -> Python codec name.
_CP_TO_CODEC = {
    65001: "utf-8",
    932: "cp932",      # Japanese
    936: "gbk",        # Simplified Chinese
    949: "cp949",      # Korean
    950: "cp950",      # Traditional Chinese
    1250: "cp1250",
    1251: "cp1251",
    1252: "cp1252",
    1253: "cp1253",
    1254: "cp1254",
    1255: "cp1255",
    1256: "cp1256",
    1257: "cp1257",
    1258: "cp1258",
}


def console_code_page() -> int | None:
    """Active console output code page, or ``None`` when there is no console."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP())
    except Exception:
        return None


def desired_encoding() -> str:
    # Redirected to a file or pipe: no terminal will render it, and UTF-8 is
    # the encoding anything reading the log later is most likely to expect.
    try:
        if sys.stdout is not None and not sys.stdout.isatty():
            return "utf-8"
    except Exception:
        pass
    cp = console_code_page()
    if cp is None:
        return "utf-8"
    return _CP_TO_CODEC.get(cp, f"cp{cp}")


def configure_stdio() -> None:
    """Re-point stdout/stderr at the encoding the console actually expects."""
    if sys.platform != "win32":
        return
    target = desired_encoding()
    for stream in (sys.stdout, sys.stderr):
        try:
            current = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        except Exception:
            continue
        if current == target.lower().replace("-", ""):
            continue
        try:
            stream.reconfigure(encoding=target, errors="replace")
        except (AttributeError, ValueError, OSError):
            # A replaced or wrapped stream: leave it alone rather than break it.
            pass


__all__ = ["configure_stdio", "console_code_page", "desired_encoding"]
