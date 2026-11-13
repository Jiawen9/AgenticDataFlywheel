from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET

from agent.evaluator.tools.registry import EvaluatorContext
from agent.evaluator.resource_paths import resolve_context_artifact

logger = logging.getLogger(__name__)


def query_ui_tree_diff(ctx: EvaluatorContext, args: dict) -> dict:
    if args.get("global_step") is not None:
        return _query_by_global_step(ctx, args)

    if "plan_index" not in args or "action_index" not in args or not (
        args.get("subtask_id") or args.get("subtask_index") is not None
    ):
        return {
            "error": "provide global_step, or plan_index plus subtask_id/subtask_index and action_index",
            "suggestion": "Use global_step=N for trajectories with actions_flat",
        }

    plan_index = int(args.get("plan_index", 0))
    subtask_id = str(args.get("subtask_id", ""))
    subtask_index = args.get("subtask_index")  # optional, disambiguation
    action_index = int(args.get("action_index", 0))
    keyword = str(args.get("keyword", ""))

    plan_steps = ctx.trajectory.get("plan_steps", [])
    if plan_index >= len(plan_steps):
        return {"error": f"plan_index {plan_index} out of range"}

    subtasks = plan_steps[plan_index].get("subtasks", [])
    subtask = None
    # subtask_index first
    if subtask_index is not None:
        si = int(subtask_index)
        if 0 <= si < len(subtasks):
            subtask = subtasks[si]
    else:
        for st in subtasks:
            if st.get("subtask_id") == subtask_id:
                subtask = st
                break
    if subtask is None:
        return {"error": f"subtask_id '{subtask_id}' not found"}

    actions = subtask.get("actions", [])
    if action_index >= len(actions):
        return {"error": f"action_index {action_index} out of range"}

    action = actions[action_index]
    before_path = resolve_context_artifact(ctx, action.get("ui_tree_before", ""))
    after_path = resolve_context_artifact(ctx, action.get("ui_tree_after", ""))

    before_matches = _search_nodes(before_path, keyword) if before_path and os.path.exists(before_path) else []
    after_matches = _search_nodes(after_path, keyword) if after_path and os.path.exists(after_path) else []

    diffs = []
    before_keys = {_node_key(m): m for m in before_matches}
    after_keys = {_node_key(m): m for m in after_matches}

    for key, bm in before_keys.items():
        am = after_keys.get(key)
        if am is None:
            diffs.append(f"REMOVED: {_node_summary(bm)}")
        elif _node_summary(bm) != _node_summary(am):
            diffs.append(f"CHANGED: {_node_summary(bm)} -> {_node_summary(am)}")

    for key, am in after_keys.items():
        if key not in before_keys:
            diffs.append(f"ADDED: {_node_summary(am)}")

    return {
        "keyword": keyword,
        "before_matches": [_node_summary(m) for m in before_matches],
        "after_matches": [_node_summary(m) for m in after_matches],
        "diff": "; ".join(diffs) if diffs else "no changes detected for keyword",
        "before_match_count": len(before_matches),
        "after_match_count": len(after_matches),
    }


def _query_by_global_step(ctx: EvaluatorContext, args: dict) -> dict:
    """Query the same processed UI-tree cache used by fetch_step_detail."""
    from agent.evaluator.tools.fetch_step_detail import fetch_step_detail

    try:
        global_step = int(args["global_step"])
    except (TypeError, ValueError):
        return {"error": "global_step must be an integer execution ordinal"}
    if global_step < 1:
        return {"error": "global_step must be a positive execution ordinal"}
    keyword = str(args.get("keyword", "")).strip()
    if not keyword:
        return {"error": "keyword must be nonempty"}

    before = fetch_step_detail(ctx, {
        "global_step": global_step, "detail_type": "ui_tree_before_extracted",
    })
    after = fetch_step_detail(ctx, {
        "global_step": global_step, "detail_type": "ui_tree_after_extracted",
    })
    if "available_ordinals" in before or "available_ordinals" in after:
        return before if "available_ordinals" in before else after

    before_available = bool(before.get("exists"))
    after_available = bool(after.get("exists"))
    before_matches = _search_processed_tree(before.get("content", ""), keyword) if before_available else []
    after_matches = _search_processed_tree(after.get("content", ""), keyword) if after_available else []
    if before_available and after_available:
        removed = sorted(set(before_matches) - set(after_matches))
        added = sorted(set(after_matches) - set(before_matches))
        changes = [f"REMOVED: {item}" for item in removed]
        changes.extend(f"ADDED: {item}" for item in added)
        diff = "; ".join(changes) if changes else "no changes detected for keyword"
    else:
        diff = "comparison unavailable: before or after UI tree is missing"

    return {
        "global_step": before.get("global_step", after.get("global_step", global_step)),
        "keyword": keyword,
        "before_available": before_available,
        "after_available": after_available,
        "before_path": before.get("path", "") if before_available else "",
        "after_path": after.get("path", "") if after_available else "",
        "before_error": before.get("error", "") if not before_available else "",
        "after_error": after.get("error", "") if not after_available else "",
        "before_matches": before_matches,
        "after_matches": after_matches,
        "before_match_count": len(before_matches),
        "after_match_count": len(after_matches),
        "diff": diff,
        "evidence_note": "A missing keyword in one UI tree is not proof that the page is not the target; inspect the screenshots and other available evidence.",
    }


def _search_processed_tree(content: str, keyword: str) -> list[str]:
    """Search cached formatted text, including older caches containing XML."""
    if content.lstrip().startswith("<"):
        nodes = _get_filtered_elements(content)
        return [_node_summary(node) for node in nodes if keyword.lower() in (
            f"{node.get('text', '')} {node.get('content_desc', '')} "
            f"{node.get('class', '')} {node.get('resource_id', '')}"
        ).lower()]
    return [line.strip() for line in content.splitlines()
            if keyword.lower() in line.lower()]


def _search_nodes(xml_path: str, keyword: str) -> list[dict]:
    """Search UI tree for nodes matching keyword.  Uses filter_interactive
    output only — never reads raw XML directly.  selected/checked are
    intentionally excluded because they are unreliable for Android custom views."""
    elements = _get_filtered_elements(xml_path)
    matches = []
    kw = keyword.lower()
    for el in elements:
        combined = f"{el.get('text','')} {el.get('class','')} {el.get('resource_id','')} {el.get('content_desc','')}".lower()
        if kw in combined:
            matches.append({
                "class": el.get("class", "").split(".")[-1],
                "text": el.get("text", ""),
                "resource_id": el.get("resource_id", ""),
                "bounds": str(el.get("bounds", "")),
                # selected/checked intentionally excluded — unreliable for Chinese apps
            })
    return matches


def _get_filtered_elements(xml_path: str) -> list[dict]:
    """Read XML from disk or inline, return filter_interactive() result."""
    # Detect inline XML (legacy trajectory format)
    if xml_path.strip().startswith("<"):
        raw_xml = xml_path
    else:
        try:
            with open(xml_path, "r", encoding="utf-8") as f:
                raw_xml = f.read()
        except Exception:
            return []

    try:
        from ui_tree_extracted import UITreeExtractor
        elements = UITreeExtractor.parse_ui_xml(raw_xml)
        return UITreeExtractor.filter_interactive(elements)
    except Exception:
        # Fallback for corrupted/malformed XML: try direct ET parse
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(raw_xml)
            return [
                {"class": n.get("class", ""), "text": n.get("text", ""),
                 "resource_id": n.get("resource-id", ""), "content_desc": n.get("content-desc", ""),
                 "bounds": n.get("bounds", "")}
                for n in root.iter("node")
                if (n.get("text") or n.get("content-desc") or n.get("clickable") == "true")
            ]
        except Exception:
            return []


def _node_key(m: dict) -> str:
    return f"{m.get('class','')}|{m.get('text','')}|{m.get('resource_id','')}"


def _node_summary(m: dict) -> str:
    """Build a human-readable node summary.  selected/checked are intentionally
    omitted because Android accessibility attributes are unreliable for apps
    with custom views (most Chinese apps — 腾讯视频, 抖音, etc.)."""
    parts = [m.get("class", ""), f"text='{m.get('text','')}'"]
    if m.get("resource_id"):
        parts.append(f"id={m['resource_id'].split('/')[-1]}")
    return " ".join(parts)
