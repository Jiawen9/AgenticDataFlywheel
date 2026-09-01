"""Isolated browser acceptance server: copies KBs to temp and uses a fake model.

Run after `npm run build --prefix frontend`:
  python -m backend.tests.scene_tree_demo_server --port 8791
No production jobs or knowledge-base files are changed; Ctrl+C cleans temp data.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.task_generation.constants import KNOWLEDGE_BASE_DIR, KNOWLEDGE_BASE_FILES, PROJECT_ROOT
from backend.task_generation.jobs import TaskGenerationJobManager
from backend.task_generation.router import configure_job_manager, router
from backend.task_generation.service import run_augmentation, run_initial_generation
from backend.tests.test_scene_tree_editing import SimulatedModel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    dist = PROJECT_ROOT / "frontend" / "dist"
    with tempfile.TemporaryDirectory(prefix="scene-tree-browser-") as temporary:
        base = Path(temporary)
        kb = base / "kb"
        kb.mkdir()
        source = KNOWLEDGE_BASE_DIR
        if (source / "current.json").exists():
            version = json.loads((source / "current.json").read_text(encoding="utf-8"))["version"]
            source = source / "versions" / version
        for filename in KNOWLEDGE_BASE_FILES.values():
            shutil.copy2(source / filename, kb / filename)
        model = SimulatedModel()
        manager = TaskGenerationJobManager(
            base / "jobs", base / "runs", base / "exports", kb, base / "logs",
            initial_runner=lambda ids, count, **kwargs: run_initial_generation(ids, count, **kwargs, model=model),
            augmentation_runner=lambda path, count, **kwargs: run_augmentation(path, count, **kwargs, model=model),
        )
        configure_job_manager(manager)
        app = FastAPI(title="Scene tree acceptance — temporary data, simulated model")
        app.include_router(router)
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        def frontend(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(dist / "index.html")

        try:
            uvicorn.run(app, host="127.0.0.1", port=args.port)
        finally:
            manager._executor.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    main()
