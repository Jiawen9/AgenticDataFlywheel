"""Data locations. Importing this module never creates files or directories."""

import os
import json
import sqlite3
from contextlib import closing
from pathlib import Path

def resolved_path(path: Path) -> Path:
    resolved = str(Path(path).expanduser().resolve())
    # Windows can expose an extended-length prefix while another process is
    # creating a missing ancestor. Normalize that equivalent spelling before
    # containment checks; symlinks are still resolved above.
    if os.name == "nt" and resolved.startswith("\\\\?\\"):
        resolved = "\\\\" + resolved[8:] if resolved.startswith("\\\\?\\UNC\\") else resolved[4:]
    return Path(resolved)


PROJECT_ROOT = resolved_path(Path(__file__)).parents[2]
DATA_ROOT = resolved_path(Path(os.environ.get("ADF_DATA_ROOT") or PROJECT_ROOT / "backend_workspace"))


def contained_path(root: Path, *parts: str) -> Path:
    root = resolved_path(root)
    target = resolved_path(root.joinpath(*parts))
    if not target.is_relative_to(root):
        raise ValueError(f"Data path {target} must remain inside the configured data root {root}")
    return target


def data_path(*parts: str) -> Path:
    return contained_path(DATA_ROOT, *parts)



def rebase_data_path(value: str | Path, data_root: Path | None = None) -> Path:
    """Resolve persisted paths only beneath current or explicitly migrated roots.

    Read original JSON and verify its SHA before resolving any contained paths.
    No old directory is scanned, and a missing database is a read-only miss.
    """
    root = resolved_path(Path(data_root or DATA_ROOT))
    value = Path(value).expanduser()
    if not value.is_absolute():
        if value.drive or value.root:
            raise ValueError("Data path must be root-relative or fully absolute")
        return contained_path(root, str(value))
    candidate = resolved_path(value)
    if candidate.is_relative_to(root):
        return contained_path(root, str(candidate.relative_to(root)))
    database = root / "system" / "app.sqlite"
    matches = []
    if database.is_file():
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=30)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone():
                for (serialized,) in connection.execute(
                    "SELECT payload FROM records WHERE namespace=?", ("data_root_relocations",)
                ):
                    record = json.loads(serialized)
                    if record.get("status") not in {"applied", "finalized"}:
                        continue
                    old = resolved_path(Path(record["old_root"]))
                    if resolved_path(Path(record["new_root"])) == root and candidate.is_relative_to(old):
                        matches.append(contained_path(root, str(candidate.relative_to(old))))
    if matches and len(set(matches)) == 1:
        return matches[0]
    raise ValueError(f"Persisted path is outside the current or registered data roots: {value}")
