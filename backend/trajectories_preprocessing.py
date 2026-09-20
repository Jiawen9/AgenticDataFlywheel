"""Export rollout trajectories to Excel and append reviewed action boxes."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import uuid
from copy import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

try:
    from .bounding_box.build_annotations import resolve_action_box
    from .bounding_box.qwen_reviewer import QwenBoxReviewer
    from .export_vla_trajectories import collect_rows, write_xlsx
    from .batch_operations import batch_operation, active_batch_lock
    from .data_store import DATA_ROOT, ArtifactStore
    from .stage_artifacts import publish_workbooks, store_root, workbook_payload, write_sidecar, assert_unmanaged_output, register_annotation_view
except ImportError:  # Keep direct `python backend/trajectories_preprocessing.py` usage working.
    from bounding_box.build_annotations import resolve_action_box
    from bounding_box.qwen_reviewer import QwenBoxReviewer
    from export_vla_trajectories import collect_rows, write_xlsx
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend.batch_operations import batch_operation, active_batch_lock
    from backend.data_store import DATA_ROOT, ArtifactStore
    from backend.stage_artifacts import publish_workbooks, store_root, workbook_payload, write_sidecar, assert_unmanaged_output, register_annotation_view


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
WORKSPACE_DIR = DATA_ROOT / "system" / "preprocessing"
DEFAULT_SOURCE = DATA_ROOT / "raw" / "rollout_trajectories"
DEFAULT_EXPORT_OUTPUT = WORKSPACE_DIR / "trajectories_to_excel.xlsx"
DEFAULT_ANNOTATED_OUTPUT = WORKSPACE_DIR / "annotated_trajectories.xlsx"
DEFAULT_ENV_FILE = BACKEND_DIR / ".env"
DEFAULT_CACHE_FILE = DATA_ROOT / "cache" / "bounding_box" / "qwen_review_cache.json"
REQUIRED_MODEL = "qwen3.8-max"
TARGET_ACTIONS = {"click", "swipe", "long_press"}
STEP_IMAGE_RE = re.compile(r"^step(?P<step>\d+)_vla_input\.jpg$", re.IGNORECASE)
REQUIRED_COLUMNS = ("文件夹名", "image", "xml", "action", "summary")


def read_env_file(path: Path) -> dict[str, str]:
    """Read the small KEY=VALUE configuration used by this backend."""
    if not path.is_file():
        raise FileNotFoundError(f"environment file does not exist: {path}")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            raise ValueError(f"invalid .env entry at {path}:{line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def configure_reviewer_environment(env_file: Path) -> str:
    """Load model settings and map them to the bounding-box review client."""
    values = read_env_file(env_file)
    missing = [name for name in ("YUNAI_API_KEY", "MODEL_URL", "MODEL_NAME") if not values.get(name)]
    if missing:
        raise ValueError(f"missing required .env settings: {', '.join(missing)}")

    model = values["MODEL_NAME"]
    if model != REQUIRED_MODEL:
        raise ValueError(f"MODEL_NAME must be {REQUIRED_MODEL!r}, got {model!r}")

    os.environ["TRAJECTORY_API_KEY"] = values["YUNAI_API_KEY"]
    os.environ["TRAJECTORY_API_BASE_URL"] = values["MODEL_URL"]
    os.environ["TRAJECTORY_MODEL"] = model
    return model


def export_trajectories(source_root: Path, output_path: Path) -> tuple[int, list[str]]:
    """Run the repository exporter against the complete rollout tree."""
    source_root = source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"trajectory source directory does not exist: {source_root}")
    rows, warnings = collect_rows(source_root)
    if not rows:
        raise ValueError(f"no final VLA trajectory steps found under {source_root}")
    write_xlsx(rows, output_path.expanduser().resolve())
    return len(rows), warnings


def parse_action_cell(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("action cell is empty")
    parsed = json.loads(value)
    if isinstance(parsed, list):
        if len(parsed) != 1:
            raise ValueError(f"expected one action, found {len(parsed)}")
        parsed = parsed[0]
    if not isinstance(parsed, dict):
        raise ValueError(f"action must be a JSON object, got {type(parsed).__name__}")
    return parsed


def step_from_image_path(image_path: Path) -> int:
    match = STEP_IMAGE_RE.match(image_path.name)
    if not match:
        raise ValueError(f"cannot determine step from image filename: {image_path.name}")
    return int(match.group("step"))


def load_executed_actions(run_dir: Path) -> dict[int, dict[str, Any]]:
    evaluation_path = run_dir / "_trajectory_for_evaluate.json"
    if not evaluation_path.is_file():
        raise FileNotFoundError(f"evaluation trajectory does not exist: {evaluation_path}")
    payload = json.loads(evaluation_path.read_text(encoding="utf-8-sig"))
    actions = payload.get("actions_flat")
    if not isinstance(actions, list):
        raise ValueError(f"actions_flat is missing or invalid: {evaluation_path}")

    result: dict[int, dict[str, Any]] = {}
    for item in actions:
        if not isinstance(item, dict) or "global_step" not in item:
            continue
        step = int(item["global_step"])
        action = item.get("action")
        if not isinstance(action, dict):
            raise ValueError(f"invalid executed action at {run_dir.name}/step{step:03d}")
        if step in result:
            raise ValueError(f"duplicate executed action at {run_dir.name}/step{step:03d}")
        result[step] = action
    return result


def swipe_direction(action: dict[str, Any]) -> str:
    start = action.get("start_coordinate")
    end = action.get("end_coordinate")
    if not isinstance(start, list) or len(start) < 2 or not isinstance(end, list) or len(end) < 2:
        raise ValueError("swipe action is missing start/end coordinates")
    dx = float(end[0]) - float(start[0])
    dy = float(end[1]) - float(start[1])
    if dx == 0 and dy == 0:
        raise ValueError("swipe start and end coordinates are identical")
    if abs(dx) > abs(dy):
        return "left" if dx < 0 else "right"
    return "up" if dy < 0 else "down"


def format_actions_box(action: dict[str, Any], bbox: tuple[int, int, int, int]) -> str:
    kind = str(action.get("action", "")).lower()
    bbox_text = "[" + ",".join(str(int(value)) for value in bbox) + "]"
    tagged_bbox = f"<bbox>{bbox_text}</bbox>"
    if kind == "click":
        return f"click(bbox={tagged_bbox})"
    if kind == "long_press":
        return f"long_press(bbox={tagged_bbox})"
    if kind == "swipe":
        return f"swipe_screen(bbox={tagged_bbox}, direction={swipe_direction(action)})"
    raise ValueError(f"unsupported boxed action: {kind!r}")


def _header_map(sheet: Any) -> dict[str, int]:
    return {
        str(cell.value): cell.column
        for cell in sheet[1]
        if cell.value is not None and str(cell.value).strip()
    }


def resolve_artifact_path(value: Any, trajectory_root: Path | None) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path.resolve()
    if trajectory_root is None:
        raise ValueError(f"relative artifact path requires trajectory_root: {path}")
    return (trajectory_root / path).resolve()


def annotate_trajectory_workbook(
    source_path: Path,
    output_path: Path,
    *,
    reviewer: Any,
    max_review_rounds: int = 4,
    trajectory_root: Path | None = None,
    allow_excel_import: bool = True,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Append actions_box values, publishing the output only after all rows succeed."""
    source_path = source_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if trajectory_root is not None:
        trajectory_root = trajectory_root.expanduser().resolve()
    if allow_excel_import:
        workbook = load_workbook(source_path)
    else:
        if __package__:
            from .stage_artifacts import payload_workbook, read_workbook_payload
        else:
            from backend.stage_artifacts import payload_workbook, read_workbook_payload
        workbook = payload_workbook(read_workbook_payload(source_path))
    sheet = workbook.active
    headers = _header_map(sheet)
    missing_columns = [name for name in REQUIRED_COLUMNS if name not in headers]
    if missing_columns:
        raise ValueError(f"workbook is missing columns: {', '.join(missing_columns)}")

    box_column = headers.get("actions_box")
    if box_column is None:
        box_column = sheet.max_column + 1
        box_header = sheet.cell(row=1, column=box_column, value="actions_box")
        source_header = sheet.cell(row=1, column=headers["action"])
        box_header._style = copy(source_header._style)
        box_header.font = copy(source_header.font)
        box_header.fill = copy(source_header.fill)
        box_header.border = copy(source_header.border)
        box_header.alignment = copy(source_header.alignment)
        box_header.number_format = source_header.number_format
        box_header.protection = copy(source_header.protection)
    sheet.column_dimensions[get_column_letter(box_column)].width = 70

    action_cache: dict[Path, dict[int, dict[str, Any]]] = {}
    counts = {"rows": 0, "annotated": 0, "blank": 0}
    for row_number in range(2, sheet.max_row + 1):
        counts["rows"] += 1
        row_label = f"Excel row {row_number}"
        if progress:
            progress({"phase": "start", "excel_row": row_number,
                      "completed_steps": row_number - 2, "total_steps": sheet.max_row - 1})
        try:
            raw_action = parse_action_cell(sheet.cell(row_number, headers["action"]).value)
            raw_kind = str(raw_action.get("action", "")).lower()
            box_cell = sheet.cell(row_number, box_column)
            if raw_kind not in TARGET_ACTIONS:
                box_cell.value = None
                counts["blank"] += 1
                if progress:
                    progress({"phase": "done", "excel_row": row_number, "actions_box": None,
                              "completed_steps": row_number - 1, "total_steps": sheet.max_row - 1})
                continue

            image_path = resolve_artifact_path(
                sheet.cell(row_number, headers["image"]).value,
                trajectory_root,
            )
            run_dir = image_path.parent
            step = step_from_image_path(image_path)
            row_label = f"{run_dir.name}/step{step:03d} (Excel row {row_number})"
            if run_dir not in action_cache:
                action_cache[run_dir] = load_executed_actions(run_dir)
            executed_action = action_cache[run_dir].get(step)
            if executed_action is None:
                raise ValueError("executed action is missing from _trajectory_for_evaluate.json")
            executed_kind = str(executed_action.get("action", "")).lower()
            if executed_kind != raw_kind:
                raise ValueError(
                    f"action mismatch: Excel has {raw_kind!r}, evaluation has {executed_kind!r}"
                )

            stability_path = run_dir / f"step{step:03d}_vla_input_stability.jpg"
            if not stability_path.is_file():
                raise FileNotFoundError(f"stability screenshot does not exist: {stability_path}")
            xml_path = resolve_artifact_path(
                sheet.cell(row_number, headers["xml"]).value,
                trajectory_root,
            )
            if not xml_path.is_file():
                raise FileNotFoundError(f"UI XML does not exist: {xml_path}")
            xml_text = xml_path.read_text(encoding="utf-8", errors="replace")
            summary_value = sheet.cell(row_number, headers["summary"]).value
            resolution = resolve_action_box(
                image_path=stability_path,
                xml_text=xml_text,
                action=executed_action,
                action_summary=str(summary_value or ""),
                reviewer=reviewer,
                max_review_rounds=max_review_rounds,
            )
            box_cell.value = format_actions_box(executed_action, resolution.result.bbox)
            counts["annotated"] += 1
            print(f"Annotated {row_label}: {box_cell.value}", flush=True)
            if progress:
                progress({"phase": "done", "excel_row": row_number, "actions_box": box_cell.value,
                          "completed_steps": row_number - 1, "total_steps": sheet.max_row - 1})
        except Exception as exc:
            raise RuntimeError(f"failed to annotate {row_label}: {exc}") from exc

    sheet.auto_filter.ref = f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f".{output_path.stem}.tmp{output_path.suffix}")
    try:
        workbook.save(temporary_path)
        temporary_path.replace(output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export rollout trajectories and add Qwen-reviewed action boxes."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--export-output", type=Path, help="Explicit initial workbook output; omitted outputs are temporary and removed after publication.")
    parser.add_argument("--annotated-output", type=Path, help="Explicit annotated workbook output; omitted outputs are temporary and removed after publication.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--max-review-rounds", type=int, default=4)
    parser.add_argument("--batch-id", help="Reuse a business batch ID and update its single current result.")
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    return parser.parse_args()


def run_pipeline(
    *, source: Path, export_output: Path, annotated_output: Path,
    env_file: Path, max_review_rounds: int, batch_id: str | None = None,
    data_root: Path | None = None, register_annotation_export: bool = True,
) -> dict[str, Any]:
    """Keep concurrent CLI model inputs private; publish only completed views."""
    batch_id = batch_id or uuid.uuid4().hex
    root = store_root(export_output, data_root)
    with batch_operation(batch_id, "preprocessing_cli", root):
        assert_unmanaged_output(export_output, root)
        assert_unmanaged_output(annotated_output, root)
        if export_output.resolve() == annotated_output.resolve():
            raise ValueError("转换和标框的导出路径必须不同")
        work_parent = root / "tmp" / "preprocessing-cli"
        work_parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="run-", dir=work_parent) as directory:
            temporary = Path(directory)
            conversion_path = temporary / "conversion" / export_output.name
            annotation_path = temporary / "annotation" / annotated_output.name
            result = _run_pipeline(source=source, export_output=conversion_path, annotated_output=annotation_path,
                env_file=env_file, max_review_rounds=max_review_rounds, batch_id=batch_id, data_root=root)
            store = ArtifactStore(root)
            with active_batch_lock(batch_id, root):
                if any((store.get(batch_id, ref["stage"]) or {}).get("version") != ref["version"]
                       for ref in result["artifacts"]):
                    raise ValueError("处理完成后批次已有更新，已保留最新结果；请重新导出")
                for source_path, target in ((conversion_path, export_output), (annotation_path, annotated_output)):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    staging = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".tmp")
                    try:
                        shutil.copyfile(source_path, staging)
                        staging.replace(target)
                    finally:
                        staging.unlink(missing_ok=True)
                conversion, annotation = result["artifacts"]
                write_sidecar(export_output, store.read_payload(conversion), source_ref=conversion)
                payload = store.read_payload(annotation)
                write_sidecar(annotated_output, payload, source_ref=annotation)
                if register_annotation_export:
                    register_annotation_view(annotated_output, payload, annotation, root)
            print(f"Current batch exports: {export_output.resolve()}; {annotated_output.resolve()}", flush=True)
            return result


def _run_pipeline(
    *,
    source: Path,
    export_output: Path,
    annotated_output: Path,
    env_file: Path,
    max_review_rounds: int,
    batch_id: str | None = None,
    data_root: Path | None = None,
) -> dict[str, Any]:
    batch_id = batch_id or uuid.uuid4().hex
    root = store_root(export_output, data_root)
    with batch_operation(batch_id, "preprocessing_cli", root):
        assert_unmanaged_output(export_output, root)
        assert_unmanaged_output(annotated_output, root)
        store = ArtifactStore(root)
        base_annotation = store.get(batch_id, "02_annotation")
        row_count, warnings = export_trajectories(source, export_output)
        converted = workbook_payload(export_output)
        # Model input is a worker snapshot; public 01/02 remain unchanged on failure.
        write_sidecar(export_output, converted)
        print(f"Exported {row_count} steps to: {export_output.expanduser().resolve()}", flush=True)
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        model = configure_reviewer_environment(env_file.expanduser().resolve())
        from backend.preprocessing_service import processing_config
        configuration = {**processing_config(env_file), "model": model,
                         "base_url": os.environ.get("TRAJECTORY_VLA_API_BASE_URL")
                         or os.environ.get("TRAJECTORY_API_BASE_URL", ""),
                         "max_review_rounds": max(1, max_review_rounds)}
        current_conversion = store.get(batch_id, "01_conversion")
        if (base_annotation and current_conversion and store.read_payload(current_conversion) == converted
                and base_annotation.get("metadata", {}).get("cli_configuration") == configuration):
            previous = store.read_payload(base_annotation)
            from backend.stage_artifacts import write_payload_workbook
            write_payload_workbook(annotated_output, previous)
            write_sidecar(export_output, converted, source_ref=current_conversion)
            write_sidecar(annotated_output, previous, source_ref=base_annotation)
            return {"exported_rows": row_count, "warnings": warnings, "batch_id": batch_id,
                    "artifacts": [current_conversion, base_annotation], "reused": True,
                    **{key: base_annotation.get("metadata", {}).get(key, 0) for key in ("rows", "annotated", "blank")}}
        reviewer = QwenBoxReviewer(model=model, cache_path=root / "cache" / "bounding_box" / "qwen_review_cache.json")
        counts = annotate_trajectory_workbook(export_output, annotated_output, reviewer=reviewer,
            max_review_rounds=max(1, max_review_rounds), trajectory_root=source, allow_excel_import=False)
        converted_ref, annotation = publish_workbooks([
            {"stage": "01_conversion", "payload": converted, "workbooks": {export_output.name: export_output},
             "source_refs": [{"kind": "raw_trajectories", "path": str(source.resolve())}], "metadata": {"warnings": warnings}},
            {"stage": "02_annotation", "payload": workbook_payload(annotated_output),
             "workbooks": {annotated_output.name: annotated_output}, "source_stages": ["01_conversion"],
             "metadata": {"model": model, "cli_configuration": configuration, **counts}},
        ], batch_id=batch_id, data_root=root, expected_annotation_version=(base_annotation or {}).get("version"),
           check_annotation_version=True)
        conversion = converted_ref
        write_sidecar(export_output, converted, source_ref=conversion)
        annotation_payload = workbook_payload(annotated_output)
        write_sidecar(annotated_output, annotation_payload, source_ref=annotation)
        print(f"Annotated workbook: {annotated_output.expanduser().resolve()}", flush=True)
        print(
            f"Rows={counts['rows']} annotated={counts['annotated']} blank={counts['blank']}",
            flush=True,
        )
        return {"exported_rows": row_count, "warnings": warnings, "batch_id": batch_id,
                "artifacts": [conversion, annotation], **counts}


def main() -> int:
    args = parse_args()
    try:
        args.batch_id = args.batch_id or uuid.uuid4().hex
        args.data_root = args.data_root.expanduser().resolve()
        ArtifactStore(args.data_root).list(batch_id=args.batch_id)
        if args.source == DEFAULT_SOURCE:
            args.source = args.data_root / "raw" / "rollout_trajectories"
        work_parent = args.data_root / "tmp" / "preprocessing-cli-views"
        work_parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="run-", dir=work_parent) as directory:
            temporary = Path(directory)
            run_pipeline(
                source=args.source,
                export_output=args.export_output or temporary / DEFAULT_EXPORT_OUTPUT.name,
                annotated_output=args.annotated_output or temporary / DEFAULT_ANNOTATED_OUTPUT.name,
                env_file=args.env_file,
                max_review_rounds=args.max_review_rounds,
                batch_id=args.batch_id,
                data_root=args.data_root,
                register_annotation_export=args.annotated_output is not None,
            )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
