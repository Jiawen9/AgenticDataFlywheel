from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "backend"
WORKSPACE_DIR = PROJECT_ROOT / "backend_workspace"
TASK_GENERATION_DIR = WORKSPACE_DIR / "task_generation"
KNOWLEDGE_BASE_DIR = TASK_GENERATION_DIR / "KnowledgeBase"
JOBS_DIR = TASK_GENERATION_DIR / "jobs"
RUNS_DIR = TASK_GENERATION_DIR / "runs"
EXPORTS_DIR = TASK_GENERATION_DIR / "exports"
LOGS_DIR = TASK_GENERATION_DIR / "logs"
ENV_FILE = BACKEND_DIR / ".env"

KNOWLEDGE_BASE_FILES = {
    "scene_tree": "VLA场景树.xlsx",
    "control_prior": "APP操控先验知识库.xlsx",
    "resource_prior": "APP资源先验知识库.xlsx",
}

ALLOWED_WORKBOOK_SUFFIXES = {".xlsx", ".xlsm"}
JOB_STATUSES = {"queued", "running", "succeeded", "partial", "failed", "interrupted"}

INITIAL_RESULT_COLUMNS = [
    "task_uuid",
    "pre_task_uuid",
    "pre_dependency",
    "status",
    "app",
    "scene",
    "capability",
    "sub_capability",
    "task",
]

AUGMENTATION_RESULT_COLUMNS = [
    "用例编号",
    "源失败任务",
    "app",
    "scene",
    "capability",
    "sub_capability",
    "生成的变体任务",
    "run",
    "审核状态",
]
