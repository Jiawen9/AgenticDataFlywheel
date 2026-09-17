"""Filesystem locations and defaults for dataset publishing."""

from __future__ import annotations

from pathlib import Path
from ..data_store.paths import DATA_ROOT


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
WORKSPACE_DIR = DATA_ROOT
DATASET_RELEASE_DIR = DATA_ROOT / "system" / "dataset_release"
FROZEN_RELEASES_DIR = DATA_ROOT / "releases"
RELEASES_FILE = DATASET_RELEASE_DIR / "releases.json"
UPLOAD_JOBS_DIR = DATASET_RELEASE_DIR / "upload_jobs"


def ensure_release_dirs() -> None:
    DATASET_RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_JOBS_DIR.mkdir(parents=True, exist_ok=True)
