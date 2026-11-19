from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backend.file_io import io_path, iterdir, resolve_path

from typing import Any

from PIL import Image

try:
    from .action_box import BoxResult, annotate_image, infer_box, load_trajectory_actions
    from .qwen_reviewer import QwenBoxReviewer
except ImportError:  # Keep direct `python build_annotations.py` usage working.
    from action_box import BoxResult, annotate_image, infer_box, load_trajectory_actions
    from qwen_reviewer import QwenBoxReviewer


DEFAULT_SOURCE = Path(r"C:\Users\panda\Desktop\gui-trajectory-adarubric-project\trajectories\20260711_104625")
PROJECT_DIR = Path(__file__).resolve().parent
SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)


@dataclass
class ActionBoxResolution:
    result: BoxResult
    image_size: tuple[int, int]
    rule_box: dict[str, Any]
    reviews: list[dict[str, Any]]
    verified: bool


def xml_for_step(directory: Path, step: int, action_record: dict[str, Any]) -> tuple[str, str]:
    xml_path = directory / f"step{step:03d}_vla_input_stability_ui.xml"
    if not io_path(xml_path).exists():
        xml_path = directory / f"step{step:03d}_vla_input_ui.xml"
    if io_path(xml_path).exists():
        return io_path(xml_path).read_text(encoding="utf-8", errors="replace"), str(xml_path)
    raw = action_record.get("ui_tree_before_raw", "")
    if isinstance(raw, str) and raw.strip():
        return raw, "embedded:ui_tree_before_raw"
    return "", "missing"


def action_summary_for_step(directory: Path, step: int, action_record: dict[str, Any]) -> str:
    response_path = directory / f"step{step:03d}_vla_model_response.json"
    if io_path(response_path).exists():
        try:
            payload = json.loads(io_path(response_path).read_text(encoding="utf-8", errors="replace"))
            match = SUMMARY_RE.search(str(payload.get("content", "")))
            if match:
                return match.group(1).strip()
        except (json.JSONDecodeError, OSError):
            pass
    return str(action_record.get("subtask_desc", "")).strip()


def resolve_action_box(
    *,
    image_path: Path,
    xml_text: str,
    action: dict[str, Any],
    action_summary: str,
    reviewer: QwenBoxReviewer | None,
    max_review_rounds: int = 4,
) -> ActionBoxResolution:
    """Use the first usable decision. Older callers supply a review round limit."""
    with Image.open(io_path(image_path)) as image:
        image_size = image.size

    result = infer_box(action, xml_text, image_size)
    rule_box = result.to_dict()
    reviews: list[dict[str, Any]] = []
    verified = reviewer is None

    if reviewer is not None:
        review = reviewer.review(
            image_path=image_path,
            action=action,
            action_summary=action_summary,
            candidate_bbox=result.bbox,
            image_size=image_size,
            rule_context={
                "source": result.source,
                "confidence": result.confidence,
                "reason": result.reason,
                "target": result.target,
            },
            round_index=1,
        )
        reviews.append(review.to_dict())
        verified = review.decision == "accept"
        result.bbox = review.bbox
        result.source = "qwen_verified" if verified else "qwen_replaced_unverified"
        result.confidence = review.confidence
        result.reason = review.reason

    return ActionBoxResolution(
        result=result,
        image_size=image_size,
        rule_box=rule_box,
        reviews=reviews,
        verified=verified,
    )


def build(
    source_root: Path,
    output_root: Path,
    reviewer: QwenBoxReviewer | None,
    model: str,
    max_review_rounds: int,
    *, batch_id: str | None = None, data_root: Path | None = None,
) -> dict[str, Any]:
    if not __package__:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.batch_operations import batch_operation, batch_from_path
    from backend.data_store import DATA_ROOT
    from backend.stage_artifacts import assert_unmanaged_output
    root = data_root or DATA_ROOT
    operation_batch = batch_id or batch_from_path(source_root, root) or batch_from_path(output_root, root)
    with batch_operation(operation_batch, "bounding_box_cli", root):
        assert_unmanaged_output(output_root, root)
        records: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        io_path(output_root).mkdir(parents=True, exist_ok=True)

        for directory in sorted(path for path in iterdir(source_root) if io_path(path).is_dir()):
            evaluation = directory / "_trajectory_for_evaluate.json"
            if not io_path(evaluation).exists():
                continue
            for item in load_trajectory_actions(evaluation):
                step = int(item["global_step"])
                action = item.get("action", {})
                if str(action.get("action", "")).lower() == "type":
                    skipped.append({
                        "trajectory": directory.name,
                        "step": step,
                        "action": action,
                        "reason": "type actions do not require a bounding box",
                    })
                    continue
                image_path = directory / f"step{step:03d}_vla_input_stability.jpg"
                if not io_path(image_path).exists():
                    missing.append({
                        "trajectory": directory.name,
                        "step": step,
                        "action": action,
                        "reason": "missing stability screenshot",
                        "expected_path": str(image_path),
                    })
                    continue

                xml_text, xml_source = xml_for_step(directory, step, item)
                action_summary = action_summary_for_step(directory, step, item)
                resolution = resolve_action_box(
                    image_path=image_path,
                    xml_text=xml_text,
                    action=action,
                    action_summary=action_summary,
                    reviewer=reviewer,
                    max_review_rounds=max_review_rounds,
                )
                result = resolution.result
                image_size = resolution.image_size
                rule_box = resolution.rule_box
                reviews = resolution.reviews
                verified = resolution.verified
                relative_output = Path(directory.name) / f"step{step:03d}_boxed.jpg"
                destination = output_root / relative_output
                annotate_image(image_path, destination, action, result)
                records.append({
                    "trajectory": directory.name,
                    "step": step,
                    "action": action,
                    "action_summary": action_summary,
                    "original": str(image_path),
                    "annotated": relative_output.as_posix(),
                    "xml_source": xml_source,
                    "image_size": list(image_size),
                    "box": result.to_dict(),
                    "rule_box": rule_box,
                    "qwen": {
                        "enabled": reviewer is not None,
                        "model": model if reviewer is not None else None,
                        "verified": verified,
                        "rounds": reviews,
                    },
                })

        manifest = {
            "source_root": str(source_root),
            "output_root": str(output_root),
            "generated_count": len(records),
            "missing_count": len(missing),
            "skipped_count": len(skipped),
            "qwen_enabled": reviewer is not None,
            "qwen_model": model if reviewer is not None else None,
            "records": records,
            "missing": missing,
            "skipped": skipped,
        }
        (io_path(output_root / "manifest.json")).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one action box for every stability screenshot.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=PROJECT_DIR / "annotated")
    parser.add_argument("--batch-id")
    parser.add_argument("--data-root", type=Path)
    parser.add_argument(
        "--model", default=os.environ.get("TRAJECTORY_MODEL", "qwen3.8-max")
    )
    parser.add_argument("--max-review-rounds", type=int, default=4)
    parser.add_argument(
        "--rules-only",
        action="store_true",
        help="Disable Qwen review explicitly; default builds require Qwen.",
    )
    args = parser.parse_args()
    if not __package__:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.batch_operations import active_batch_lock, batch_from_path
    operation_batch = args.batch_id or batch_from_path(args.source, args.data_root)
    with active_batch_lock(operation_batch, args.data_root):
        pass
    reviewer = None
    if not args.rules_only:
        reviewer = QwenBoxReviewer(
            model=args.model,
            cache_path=PROJECT_DIR / "qwen_review_cache.json",
        )
    manifest = build(
        resolve_path(args.source),
        resolve_path(args.output),
        reviewer=reviewer,
        model=args.model,
        max_review_rounds=max(1, args.max_review_rounds),
        batch_id=args.batch_id, data_root=args.data_root,
    )
    print(f"Generated: {manifest['generated_count']}")
    print(f"Missing:   {manifest['missing_count']}")
    print(f"Skipped:   {manifest['skipped_count']}")
    print(f"Manifest:  {resolve_path(args.output) / 'manifest.json'}")


if __name__ == "__main__":
    main()
