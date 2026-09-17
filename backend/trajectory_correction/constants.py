"""Paths and stable values for the isolated trajectory-correction module."""

from __future__ import annotations

from pathlib import Path
from ..data_store.paths import DATA_ROOT


BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
WORKSPACE_DIR = DATA_ROOT
FIXED_ANNOTATED_XLSX = DATA_ROOT / "system" / "preprocessing" / "annotated_trajectories.xlsx"
FIXED_TRAJECTORY_ROOT = DATA_ROOT / "raw" / "rollout_trajectories"
FIXED_SOURCE_ID = "project/annotated_trajectories.xlsx"
CORRECTION_DATA_DIR = DATA_ROOT / "system" / "trajectory_correction"
CORRECTION_INPUTS_DIR = CORRECTION_DATA_DIR / "inputs"
CORRECTION_SESSIONS_DIR = CORRECTION_DATA_DIR / "sessions"
CORRECTION_EXPORTS_DIR = CORRECTION_DATA_DIR / "exports"
CORRECTION_COT_JOBS_DIR = CORRECTION_DATA_DIR / "cot_jobs"
CORRECTION_COT_CACHE_DIR = DATA_ROOT / "cache" / "trajectory_correction" / "cot"
CORRECTION_BBOX_CACHE_DIR = DATA_ROOT / "cache" / "trajectory_correction" / "bbox"

WORKBOOK_SUFFIXES = {".xlsx", ".xlsm"}
UPLOAD_SUFFIXES = WORKBOOK_SUFFIXES | {".zip"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
ACTION_TYPES = (
    "click",
    "long_press",
    "type",
    "open",
    "swipe",
    "system_button",
    "wait",
    "terminate",
    "answer",
)


def ensure_correction_dirs() -> None:
    for path in (
        CORRECTION_INPUTS_DIR,
        CORRECTION_SESSIONS_DIR,
        CORRECTION_EXPORTS_DIR,
        CORRECTION_COT_JOBS_DIR,
        CORRECTION_COT_CACHE_DIR,
        CORRECTION_BBOX_CACHE_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
