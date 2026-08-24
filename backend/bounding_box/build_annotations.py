from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
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


def _contains_point(bbox: tuple[int, int, int, int], point: list[Any]) -> bool:
    if not isinstance(point, list) or len(point) < 2:
        return True
    x1, y1, x2, y2 = bbox
    x, y = float(point[0]), float(point[1])
    return x1 <= x <= x2 and y1 <= y <= y2


def _point_fallback(point: list[Any], image_size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = image_size
    x, y = float(point[0]), float(point[1])
    radius_x = max(45, round(width * 0.045))
    radius_y = max(45, round(width * 0.045))
    return (
        max(0, round(x - radius_x)),
        max(0, round(y - radius_y)),
        min(width - 1, round(x + radius_x)),
        min(height - 1, round(y + radius_y)),
    )


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union else 0.0


def xml_for_step(directory: Path, step: int, action_record: dict[str, Any]) -> tuple[str, str]:
    xml_path = directory / f"step{step:03d}_vla_input_ui.xml"
    if xml_path.exists():
        return xml_path.read_text(encoding="utf-8", errors="replace"), str(xml_path)
    raw = action_record.get("ui_tree_before_raw", "")
    if isinstance(raw, str) and raw.strip():
        return raw, "embedded:ui_tree_before_raw"
    return "", "missing"


def action_summary_for_step(directory: Path, step: int, action_record: dict[str, Any]) -> str:
    response_path = directory / f"step{step:03d}_vla_model_response.json"
    if response_path.exists():
        try:
            payload = json.loads(response_path.read_text(encoding="utf-8", errors="replace"))
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
    """Infer and optionally review one executable GUI action bounding box."""
    with Image.open(image_path) as image:
        image_size = image.size

    result = infer_box(action, xml_text, image_size)
    rule_box = result.to_dict()
    final_bbox = result.bbox
    reviews: list[dict[str, Any]] = []
    verified = reviewer is None

    if reviewer is not None:
        for round_index in range(1, max(1, max_review_rounds) + 1):
            reviewed_candidate = final_bbox
            review = reviewer.review(
                image_path=image_path,
                action=action,
                action_summary=action_summary,
                candidate_bbox=final_bbox,
                image_size=image_size,
                rule_context={
                    "source": result.source,
                    "confidence": result.confidence,
                    "reason": result.reason,
                    "target": result.target,
                },
                round_index=round_index,
            )
            review_record = review.to_dict()
            final_bbox = review.bbox
            kind = str(action.get("action", "")).lower()
            if kind in {"click", "long_press"} and not _contains_point(
                final_bbox, action.get("coordinate", [])
            ):
                final_bbox = _point_fallback(action.get("coordinate", []), image_size)
                review_record["constraint_adjustment"] = (
                    "Qwen replacement excluded the observed click point; "
                    "next candidate reset around the successful point"
                )
            elif kind == "swipe" and not (
                _contains_point(final_bbox, action.get("start_coordinate", []))
                and _contains_point(final_bbox, action.get("end_coordinate", []))
            ):
                final_bbox = result.bbox
                review_record["constraint_adjustment"] = (
                    "Qwen replacement excluded a swipe endpoint; retained rule container"
                )
            reviews.append(review_record)
            if review.decision == "accept":
                verified = True
                break
            if final_bbox == review.bbox and _iou(reviewed_candidate, final_bbox) >= 0.985:
                review_record["converged_as_verified"] = True
                verified = True
                break

        result.bbox = final_bbox
        result.source = "qwen_verified" if verified else "qwen_replaced_unverified"
        result.confidence = reviews[-1]["confidence"]
        result.reason = reviews[-1]["reason"]

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
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    output_root.mkdir(parents=True, exist_ok=True)

    for directory in sorted(path for path in source_root.iterdir() if path.is_dir()):
        evaluation = directory / "_trajectory_for_evaluate.json"
        if not evaluation.exists():
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
            if not image_path.exists():
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
    (output_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate one action box for every stability screenshot.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=PROJECT_DIR / "annotated")
    parser.add_argument(
        "--model", default=os.environ.get("TRAJECTORY_MODEL", "qwen3.6-27b:floor")
    )
    parser.add_argument("--max-review-rounds", type=int, default=4)
    parser.add_argument(
        "--rules-only",
        action="store_true",
        help="Disable Qwen review explicitly; default builds require Qwen.",
    )
    args = parser.parse_args()
    reviewer = None
    if not args.rules_only:
        reviewer = QwenBoxReviewer(
            model=args.model,
            cache_path=PROJECT_DIR / "qwen_review_cache.json",
        )
    manifest = build(
        args.source.resolve(),
        args.output.resolve(),
        reviewer=reviewer,
        model=args.model,
        max_review_rounds=max(1, args.max_review_rounds),
    )
    print(f"Generated: {manifest['generated_count']}")
    print(f"Missing:   {manifest['missing_count']}")
    print(f"Skipped:   {manifest['skipped_count']}")
    print(f"Manifest:  {args.output.resolve() / 'manifest.json'}")


if __name__ == "__main__":
    main()
