"""Filesystem access without leaking Windows extended paths into stored identities.

Callers retain ordinary paths for validation, manifests and URLs. Only the actual
filesystem boundary receives an extended-length path. This does not change the
Windows registry or relax any application's directory containment rules.
"""
from __future__ import annotations

import ntpath
import os
import stat
from pathlib import Path
from typing import Iterator


def logical_path(value: str | os.PathLike[str]) -> Path:
    """Remove the Windows I/O spelling; do not resolve or follow any links."""
    text = os.fspath(value)
    if os.name == "nt":
        if text[:8].lower() == "\\\\?\\unc\\":
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\"):
            text = text[4:]
    return Path(text)


def windows_io_path(value: str) -> str:
    """Convert an absolute Windows path, including UNC, without filesystem I/O."""
    text = value.replace("/", "\\")
    if text[:8].lower() == "\\\\?\\unc\\":
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    if text.startswith("\\\\.\\"):
        raise ValueError("Windows device paths are not file paths")
    text = ntpath.normpath(text)
    drive, tail = ntpath.splitdrive(text)
    if not drive or not tail.startswith("\\"):
        # A share root has an empty tail and is still an absolute UNC path.
        if not (drive.startswith("\\\\") and tail == ""):
            raise ValueError("Windows file access requires an absolute path")
    if text.startswith("\\\\"):
        share = drive[2:].split("\\")
        if len(share) != 2 or not all(share) or any(part in {".", "..", "?"} for part in share):
            raise ValueError("Windows UNC paths require a server and share")
        return "\\\\?\\UNC\\" + text[2:]
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isalpha():
        raise ValueError("Windows file access requires a drive or UNC path")
    return "\\\\?\\" + text


def io_path(value: str | os.PathLike[str]) -> Path:
    """Return the filesystem spelling, preserving the caller's logical path."""
    path = logical_path(value)
    if os.name != "nt":
        return path
    return Path(windows_io_path(os.path.abspath(os.fspath(path))))


def resolve_path(value: str | os.PathLike[str]) -> Path:
    """Resolve links with long-path access, then return a normal logical path."""
    return logical_path(io_path(Path(value).expanduser()).resolve())


def iterdir(value: str | os.PathLike[str]) -> Iterator[Path]:
    for path in io_path(value).iterdir():
        yield logical_path(path)


def rglob(value: str | os.PathLike[str], pattern: str = "*") -> Iterator[Path]:
    for path in io_path(value).rglob(pattern):
        yield logical_path(path)


def is_link_or_junction(value: str | os.PathLike[str]) -> bool:
    """Check the original entry (also broken links) rather than its target."""
    try:
        info = io_path(value).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
