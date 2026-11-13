"""Case-local evidence resolution shared by image, XML and response tools."""
from __future__ import annotations

from pathlib import Path, PureWindowsPath

ARTIFACT_KEYS = frozenset({
    "screenshot_before", "screenshot_after", "action_overlay_path", "model_response_path",
    "ui_tree_before", "ui_tree_after", "ui_tree_before_raw", "ui_tree_after_raw",
    "ui_tree_before_extracted", "ui_tree_after_extracted", "final_orch_screenshot",
    "screenshot", "screenshot_path", "image_path", "xml_path", "ui_tree_path",
    "ui_dump_before", "ui_dump_after", "ui_dump_path",
})


def _safe_local(root: Path, path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("evidence path escapes this trajectory snapshot")
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("evidence links are forbidden")
    return str(resolved)


def resolve_artifact(trajectory: dict, raw_path: object, *, declared: bool = False) -> str:
    raw = str(raw_path or "").strip()
    if not raw or raw.startswith("<"):
        return raw
    strict = bool(trajectory.get("_strict_artifacts"))
    root = Path(trajectory.get("_case_dir") or ".").resolve()
    native = Path(raw)
    if not strict:
        if native.is_file():
            return str(native)
        fallback = root / PureWindowsPath(raw).name
        return str(fallback) if fallback.is_file() else raw
    normalized = raw.replace("\\", "/")
    if ".." in normalized.split("/") or "://" in normalized:
        raise ValueError("evidence traversal or URL is forbidden")
    aliases = trajectory.get("_artifact_aliases", {})
    if not declared and raw in aliases:
        return _safe_local(root, Path(aliases[raw]))
    absolute = native.is_absolute() or PureWindowsPath(raw).is_absolute()
    if absolute:
        # Paths already inside the snapshot are canonical. An external absolute
        # path is accepted only if it came from this exact trajectory JSON.
        if native.is_absolute() and native.resolve().is_relative_to(root):
            candidate = native
        elif declared:
            candidate = root / PureWindowsPath(normalized).name
        else:
            raise ValueError("unregistered evidence path outside this trajectory snapshot")
    else:
        candidate = root.joinpath(*normalized.split("/"))
    result = _safe_local(root, candidate)
    if not Path(result).is_file():
        raise ValueError(f"snapshot evidence file missing: {candidate.name}")
    return result


def resolve_context_artifact(ctx, raw_path: object) -> str:
    trajectory = ctx.trajectory
    if not trajectory.get("_case_dir") and getattr(ctx, "_trajectory_path", ""):
        trajectory = {**trajectory, "_case_dir": str(Path(ctx._trajectory_path).resolve().parent)}
    return resolve_artifact(trajectory, raw_path)


def bind_snapshot_artifacts(trajectory: dict, trajectory_path: str | Path) -> dict:
    """Normalize the in-memory record only; never alter frozen evidence bytes."""
    trajectory["_case_dir"] = str(Path(trajectory_path).resolve().parent)
    trajectory["_strict_artifacts"] = True
    aliases = {}
    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for key, item in list(value.items()):
                if key in ARTIFACT_KEYS and isinstance(item, str) and item.strip():
                    local = resolve_artifact(trajectory, item, declared=True)
                    aliases[item] = local
                    value[key] = local
                elif not key.startswith("_"):
                    visit(item)
    visit(trajectory)
    trajectory["_artifact_aliases"] = aliases
    return trajectory
