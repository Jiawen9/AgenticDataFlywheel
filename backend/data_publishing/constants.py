"""Filesystem locations and defaults for dataset publishing."""

from __future__ import annotations

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
WORKSPACE_DIR = PROJECT_ROOT / "backend_workspace"
DATASET_RELEASE_DIR = WORKSPACE_DIR / "dataset_release"
RELEASES_FILE = DATASET_RELEASE_DIR / "releases.json"
UPLOAD_JOBS_DIR = DATASET_RELEASE_DIR / "upload_jobs"


def ensure_release_dirs() -> None:
    DATASET_RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_JOBS_DIR.mkdir(parents=True, exist_ok=True)
