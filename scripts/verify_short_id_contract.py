"""Offline collection handoff fixture; creates a fresh isolated demo database only."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from backend.data_store.registry import utc_now
from backend.file_io import io_path
from backend.identifiers import allocate_id
from backend.short_paths import map_collection_manifest, validate_local_manifest
from backend.task_generation.collection_batches import payload_digest


def write_json(path, value):
    io_path(path.parent).mkdir(parents=True, exist_ok=True)
    io_path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def create_demo(output: Path) -> dict:
    """output must be new: never reuse a real workspace or overwrite its data."""
    io_path(output).mkdir(parents=True, exist_ok=False)
    root, remote = output / "platform", output / "remote"
    batch = allocate_id("b", root, key=["demo-upload", "request-demo"])
    task = allocate_id("t", root, key=["collection-case", batch, "CASE-中文-001"])
    run_id = allocate_id("r", root, key=["collection-run", batch, "dispatch-demo"])
    request = {"batch_id": batch, "request_id": "dispatch-demo", "run_mode": "generate",
               "collection_run_id": run_id, "vla": "mock-only", "phone_ids": ["phone-A", "phone-B"],
               "config": {"sampling_enabled": True, "temperature": 0.7, "top_p": 0.9,
                          "use_experience_lib": False},
               "tasks": [{"collection_case_id": "CASE-中文-001", "task_id": task,
                          "task": "打开设置", "app": "模拟 App"}]}
    run = {"batch_id": batch, "collection_run_id": run_id, "created_at": utc_now(),
           "request_digest": payload_digest(request),
           "batch_tasks": {"CASE-中文-001": {"task_id": task, "source_result_id": None,
                                            "task": "打开设置", "app": "模拟 App"}}}
    entries = []
    for phone in request["phone_ids"]:
        relative = "CASE-中文-001/" + phone + "/同名轨迹"
        directory = remote / relative
        io_path(directory).mkdir(parents=True)
        action = {"action": "click", "coordinate": [10, 10]}
        write_json(directory / "_trajectory_for_evaluate.json",
                   {"actions_flat": [{"global_step": 1, "action": action}]})
        write_json(directory / "step001_vla_model_response.json",
                   {"content": "<tool_call>" + json.dumps(action) + "</tool_call><summary>Tap</summary>"})
        io_path(directory / "step001_vla_input_ui.xml").write_text("<hierarchy/>", encoding="utf-8")
        for name in ("step001_vla_input.jpg", "step001_vla_input_stability.jpg", "step001_vla_done.jpg"):
            Image.new("RGB", (20, 30), "white").save(io_path(directory / name))
        files = [{"path": relative + "/" + file.name, "size": file.stat().st_size,
                  "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
                 for file in sorted(io_path(directory).iterdir())]
        entries.append({"collection_case_id": "CASE-中文-001", "phone_id": phone,
                        "source_trajectory_id": "同名轨迹", "relative_dir": relative,
                        "collected_at": run["created_at"], "files": files})
    source = {"batch_id": batch, "collection_run_id": run_id, "trajectories": entries, "errors": []}
    mapped = map_collection_manifest(root, run, source)
    final = root / "raw" / "collection_batches" / batch / "runs" / run_id
    # Production must retain its staging journal, batch lock and registration transaction.
    # This demo validates bytes and mapping only; it never registers a completed business run.
    for original, relative in mapped["mapping"]["files"].items():
        destination = final / relative
        io_path(destination.parent).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(io_path(remote / original), io_path(destination))
    validate_local_manifest(root, run, mapped, root_override=final)
    assert map_collection_manifest(root, run, source) == mapped
    assert len({item["trajectory_id"] for item in mapped["manifest"]["trajectories"]}) == 2
    assert allocate_id("r", root, key=["collection-run", batch, "dispatch-demo"]) == run_id
    for name, value in (("dispatch.json", request), ("run.json", run), ("remote-manifest.json", source),
                        ("local-layout.json", mapped)):
        write_json(output / name, value)
    result = {"success": True, "real_services_called": False, "batch_id": batch,
              "collection_run_id": run_id, "task_id": task, "trajectory_count": 2,
              "file_count": len(mapped["mapping"]["files"]), "local_root": str(final),
              "note": "隔离模拟样例；未登记业务运行，不用于真实采集下发"}
    write_json(output / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="必须尚不存在的输出目录；省略则使用系统临时目录")
    args = parser.parse_args()
    destination = args.output or Path(tempfile.mkdtemp(prefix="adf-short-contract-")) / "fixture"
    print(json.dumps({"output": str(destination), **create_demo(destination)}, ensure_ascii=False, indent=2))
