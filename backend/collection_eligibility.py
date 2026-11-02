"""Use-specific eligibility never changes immutable source execution status."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

from .data_store.paths import contained_path
from .file_io import io_path


def inspect_eligibility(runs, run, entry, base, kind):
    result = {"version": 1, "evaluation": False, "preprocessing": False, "issues": []}
    if kind == "outcome":
        result["issues"].append({"code": "no_file_evidence", "scope": "evaluation"})
        return result
    directory = contained_path(base, entry["relative_dir"])
    metadata = directory / "_trajectory_for_evaluate.json"
    try:
        data = json.loads(io_path(metadata).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("task"), str) or not data["task"].strip() or not isinstance(data.get("actions_flat"), list):
            raise ValueError("invalid metadata")
        # Partial images are reported; at least one readable visual evidence
        # item admits evaluation. Unsafe references never admit evaluation.
        references = []
        for action in data["actions_flat"]:
            if not isinstance(action, dict):
                raise ValueError("invalid action")
            references.extend(action.get(key) for key in ("screenshot_before", "screenshot_after", "action_overlay_path") if action.get(key))
        if data.get("final_orch_screenshot"):
            references.append(data["final_orch_screenshot"])
        references.extend(path.name for path in directory.glob("step*_vla_input.jpg"))
        from PIL import Image
        readable, unsafe = 0, False
        for name in set(references):
            if not isinstance(name, str):
                unsafe = True
                continue
            raw = name.replace("\\", "/")
            if ".." in PurePosixPath(raw).parts or raw.startswith(("http://", "https://", "data:")):
                unsafe = True
                continue
            if Path(name).is_absolute() or PureWindowsPath(name).is_absolute():
                raw = PurePosixPath(raw).name
            try:
                path = contained_path(directory, raw)
                with Image.open(io_path(path)) as image:
                    image.verify()
                readable += 1
            except (OSError, ValueError, SyntaxError):
                if len(result["issues"]) < 100:
                    result["issues"].append({"code": "image_unavailable", "scope": "image", "path": raw[:240]})
        result["evaluation"] = bool(readable and not unsafe)
        if unsafe:
            result["issues"].append({"code": "unsafe_evidence_reference", "scope": "image"})
        if not readable:
            result["issues"].append({"code": "image_evidence_missing", "scope": "evaluation"})
    except (OSError, ValueError, TypeError, AttributeError):
        result["issues"].append({"code": "evaluation_evidence_invalid", "scope": "evaluation"})
    if kind == "trajectory":
        try:
            # This is the unchanged authoritative training contract, not the
            # collector's advisory quality declaration.
            runs._validate_trajectory_content(directory, entry["relative_dir"],
                agent_type=run.get("request", {}).get("metadata", {}).get("agent_type"))
            result["preprocessing"] = True
        except (OSError, ValueError, TypeError, AttributeError):
            result["issues"].append({"code": "preprocessing_evidence_invalid", "scope": "preprocessing"})
    return result
