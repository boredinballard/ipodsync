"""
network_utils.py — Network path handling utilities for ipodsync.

Centralizes path normalization, UNC detection, reachability checks,
and safe resolution to avoid pathlib quirks with mapped drives.
"""

import os
import time
from pathlib import Path, PureWindowsPath


def normalize_path(raw: str) -> Path:
    """Normalize a user-supplied path string into a clean Path.

    Handles:
      - UNC paths  (\\\\server\\share, //server/share)
      - Mapped drives (Z:\\Music)
      - Forward/back slash mixes
      - Trailing separators

    Does NOT call Path.resolve() — that can mangle mapped drives
    into their underlying UNC paths unexpectedly.
    """
    if not raw or not raw.strip():
        return Path("")

    cleaned = raw.strip().strip('"').strip("'")

    # Normalise forward slashes to backslashes for consistency on Windows
    cleaned = cleaned.replace("/", "\\")

    # Collapse redundant separators (but preserve leading \\\\ for UNC)
    if cleaned.startswith("\\\\"):
        # UNC path: preserve the leading \\, collapse the rest
        rest = cleaned[2:]
        rest = _collapse_seps(rest)
        cleaned = "\\\\" + rest
    else:
        cleaned = _collapse_seps(cleaned)

    # Strip trailing separator (unless it's a root like "Z:\")
    if len(cleaned) > 3 and cleaned.endswith("\\"):
        cleaned = cleaned.rstrip("\\")

    return Path(cleaned)


def _collapse_seps(s: str) -> str:
    """Collapse runs of backslashes into single separators."""
    while "\\\\" in s:
        s = s.replace("\\\\", "\\")
    return s


def is_network_path(p: Path) -> bool:
    """Return True if the path is a UNC share or a mapped network drive.

    Detection strategy:
      1. UNC paths start with \\\\
      2. Mapped drives are detected via win32 API when available,
         otherwise we check if the drive type is DRIVE_REMOTE.
    """
    path_str = str(p)

    # UNC paths
    if path_str.startswith("\\\\"):
        return True

    # Check if a drive letter maps to a network share
    drive = os.path.splitdrive(path_str)[0]
    if drive:
        try:
            import ctypes
            DRIVE_REMOTE = 4
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
            return drive_type == DRIVE_REMOTE
        except (AttributeError, OSError):
            pass

    return False


def validate_path(p: Path) -> dict:
    """Quick reachability check for a path.

    Returns:
        {
            "reachable": bool,
            "error": str | None,
            "latency_ms": int,
            "is_network": bool,
        }
    """
    result = {
        "reachable": False,
        "error": None,
        "latency_ms": 0,
        "is_network": is_network_path(p),
    }

    path_str = str(p)
    if not path_str or path_str == ".":
        result["error"] = "Empty path"
        return result

    t0 = time.perf_counter()
    try:
        entries = os.listdir(path_str)
        elapsed = time.perf_counter() - t0
        result["reachable"] = True
        result["latency_ms"] = int(elapsed * 1000)
    except FileNotFoundError:
        result["error"] = "Path not found"
    except PermissionError:
        result["error"] = "Access denied"
    except OSError as e:
        elapsed = time.perf_counter() - t0
        result["latency_ms"] = int(elapsed * 1000)
        result["error"] = f"OS error: {e.strerror}"

    return result


def safe_resolve(p: Path) -> Path:
    """Resolve symlinks for local paths only.

    Network/UNC paths are returned unchanged because
    Path.resolve() can silently convert mapped drives (Z:\\)
    into their underlying UNC paths.
    """
    if is_network_path(p):
        return p
    try:
        return p.resolve()
    except OSError:
        return p
