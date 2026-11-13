"""detect_image_changes — pixel-level diff between before/after screenshots.

Uses traditional CV (absdiff + threshold + contours) to locate changed
regions.  Complements VLM-based inspection when icon/button state changes
are too small for the VLM to reliably distinguish.
"""
from __future__ import annotations

import logging
import os

import numpy as np

from agent.evaluator.tools.registry import EvaluatorContext
from agent.evaluator.resource_paths import resolve_context_artifact

logger = logging.getLogger(__name__)

MIN_AREA_PX = 50          # ignore noise below this area
FULL_WIDTH_RATIO = 0.9    # ignore regions wider than 90 % of image (scroll)


def detect_image_changes(ctx: EvaluatorContext, args: dict) -> dict:
    import cv2
    before_path = resolve_context_artifact(ctx, args.get("before_path", ""))
    after_path = resolve_context_artifact(ctx, args.get("after_path", ""))
    threshold = int(args.get("threshold", 15))

    if not os.path.exists(before_path):
        return {"error": f"before_path not found: {before_path}"}
    if not os.path.exists(after_path):
        return {"error": f"after_path not found: {after_path}"}

    img1 = cv2.imread(before_path)
    img2 = cv2.imread(after_path)
    if img1 is None:
        return {"error": f"cv2 failed to read {before_path}"}
    if img2 is None:
        return {"error": f"cv2 failed to read {after_path}"}

    h, w = img1.shape[:2]

    # ── Pixel-level diff ──────────────────────────────────────────────
    diff = cv2.absdiff(img1, img2)
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # ── Extract changed regions ───────────────────────────────────────
    regions = []
    annotated = img2.copy()
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        area = cw * ch
        if area < MIN_AREA_PX:
            continue
        if cw > w * FULL_WIDTH_RATIO:
            continue
        regions.append({
            "id": len(regions),
            "bbox": [int(x), int(y), int(cw), int(ch)],
            "area_px": int(area),
        })
        cv2.rectangle(annotated, (x, y), (x + cw, y + ch), (0, 0, 255), 2)

    # ── Save annotated image ──────────────────────────────────────────
    checkpoint_path = getattr(ctx, "_checkpoint_path", "")
    out_dir = (os.path.join(os.path.dirname(checkpoint_path), "diffs") if checkpoint_path
               else os.path.join(".evaluator", f"session_{ctx.session_id}", "diffs"))
    os.makedirs(out_dir, exist_ok=True)
    stem, _ = os.path.splitext(os.path.basename(before_path))
    basename = f"{stem}_diff.jpg"
    out_path = os.path.join(out_dir, basename)
    cv2.imwrite(out_path, annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 90])

    changed_px = int(np.sum(thresh == 255))
    total_px = h * w

    logger.warning(
        "[detect_image_changes] %s → %s: %s regions, %.2f%% changed, saved %s",
        os.path.basename(before_path), os.path.basename(after_path),
        len(regions), 100.0 * changed_px / max(total_px, 1), out_path,
    )

    return {
        "before_path": before_path,
        "after_path": after_path,
        "changed_regions": regions,
        "region_count": len(regions),
        "change_ratio": round(changed_px / max(total_px, 1), 4),
        "changed_pixels": changed_px,
        "image_size": [w, h],
        "annotated_image": out_path,
    }
