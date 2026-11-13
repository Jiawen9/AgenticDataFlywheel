from __future__ import annotations
from agent.evaluator.resource_paths import resolve_context_artifact

import logging
import os
from pathlib import Path

from agent.evaluator.tools.registry import EvaluatorContext
from agent.evaluator.step_identity import describe_action, describe_step, file_step_index, ordinal_set, parse_file_step, resolve_ordinal

logger = logging.getLogger(__name__)


def fetch_step_detail(ctx: EvaluatorContext, args: dict) -> dict:
    detail_type = str(args.get("detail_type", ""))

    # ── B2: list_all_screenshots ─────────────────────────────────────────
    if detail_type == "list_all_screenshots":
        return _list_all_screenshots(ctx)

    # ── B2: flat lookup by global_step ───────────────────────────────────
    global_step = args.get("global_step")

    # Special: global_step="final" loads the final orchestrator screenshot
    if str(global_step).lower() == "final":
        final_path = ctx.trajectory.get("final_orch_screenshot", "")
        if final_path and os.path.exists(final_path):
            return {
                "global_step": "final",
                "detail_type": detail_type,
                "path": final_path,
                "exists": True,
                "description": (
                    "Final orchestrator screenshot — the page state when "
                    "agent decided the task was complete"
                ),
            }
        return {
            "error": "final_orch_screenshot not available",
            "suggestion": "Try detail_type='list_all_screenshots'",
        }

    if global_step is not None:
        try:
            gsv = int(global_step)
        except (TypeError, ValueError):
            # non-numeric, non-'final' label (e.g. an orchestrator 'turn021_orch') —
            # those are not action steps; never crash on them.
            return {
                "error": f"global_step={global_step!r} is not a numeric ordinal or 'final'",
                "suggestion": "Use detail_type='list_all_screenshots' to discover ordinal steps",
            }
        return _fetch_by_global_step(ctx, gsv, detail_type)

    # ── Legacy: nested plan_steps lookup ─────────────────────────────────
    plan_index = int(args.get("plan_index", 0))
    subtask_id = str(args.get("subtask_id", ""))
    subtask_index = args.get("subtask_index")
    action_index = int(args.get("action_index", 0))

    plan_steps = ctx.trajectory.get("plan_steps", [])
    if plan_index >= len(plan_steps):
        return {"error": f"plan_index {plan_index} out of range (max {len(plan_steps) - 1})",
                "suggestion": "Try a different plan_index, use global_step instead, or call detail_type='list_all_screenshots'"}

    subtasks = plan_steps[plan_index].get("subtasks", [])
    subtask = None

    if subtask_index is not None:
        si = int(subtask_index)
        if 0 <= si < len(subtasks):
            subtask = subtasks[si]
    else:
        matches = [(i, st) for i, st in enumerate(subtasks) if st.get("subtask_id") == subtask_id]
        if matches:
            subtask = matches[-1][1]
            subtask_index = matches[-1][0]

    if subtask is None:
        hint = "Use subtask_index (integer) from the Quick Reference."
        if subtask_id.isdigit():
            hint = f"You passed subtask_id='{subtask_id}' which is an integer string. No subtask has that ID. Use subtask_index={subtask_id} instead."
        return {
            "error": f"subtask_id '{subtask_id}' not found in plan step {plan_index}",
            "available_subtasks": [f"subtask_index={i} → {st.get('subtask_id')}" for i, st in enumerate(subtasks)],
            "hint": hint,
            "suggestion": "Try using global_step (integer) instead of plan_index+subtask_id, or use detail_type='list_all_screenshots'",
        }

    actions = subtask.get("actions", [])
    if action_index >= len(actions):
        return {"error": f"action_index {action_index} out of range (max {len(actions) - 1})",
                "suggestion": "Try a different action_index, or use global_step to bypass plan/subtask indexing"}

    action = actions[action_index]
    return _load_detail(ctx, action, detail_type, {
        "plan_index": plan_index,
        "subtask_id": subtask.get("subtask_id", subtask_id),
        "subtask_index": subtask_index if subtask_index is not None else -1,
        "subtask_desc": subtask.get("subtask_desc", "")[:100],
        "action_index": action_index,
    })


def _fetch_by_global_step(ctx: EvaluatorContext, global_step: int, detail_type: str) -> dict:
    """Look up a committed action by ``global_step`` (execution ordinal 1..N).

    ``global_step`` contract = ordinal of committed actions. When committed file
    indices have holes (cancelled prefetches) a raw *physical* index leaked from
    disk (e.g. ``step006`` belonging to ordinal 4) is resolved to its owning
    ordinal via resolve_ordinal, so the model still sees the exact screenshot it
    meant. On-disk files with no committed ordinal (prefetch debris) cannot be
    loaded through ``global_step`` — the error tells the model to use image_path.
    """
    flat = ctx.trajectory.get("actions_flat", [])
    ord = resolve_ordinal(flat, gs=global_step)
    action = None
    if flat:
        for a in flat:
            if a.get("global_step") == (ord if ord is not None else global_step):
                action = a
                break
    if action is None and ord is None:
        # Fallback: scan legacy plan_steps by the literal number
        for ps in ctx.trajectory.get("plan_steps", []):
            for st in ps.get("subtasks", []):
                for a in st.get("actions", []):
                    if a.get("global_step") == global_step:
                        action = a
                        break
                if action:
                    break
            if action:
                break
    if action is None:
        valid = sorted(ordinal_set(flat))
        return {
            "error": f"no committed action with ordinal global_step={global_step}",
            "available_ordinals": valid,
            "hint": ("global_step is the execution ordinal 1..N. On-disk files with a "
                     "non-ordinal stepNNN (cancelled-prefetch debris, e.g. step003 under "
                     "_prefetch_staging/) have NO ordinal — load them via image_path, not global_step."),
            "suggestion": "Try detail_type='list_all_screenshots' to see which on-disk file maps to which ordinal",
        }

    return _load_detail(ctx, action, detail_type, {
        "global_step": action.get("global_step", global_step),
        "subtask_id": action.get("subtask_id", ""),
        "subtask_desc": action.get("subtask_desc", "")[:100],
        "action_index": action.get("action_index", 0),
    })


def _paired_ui_tree_path(screenshot_path: str) -> str:
    """Recover a captured UI tree omitted from a flat trajectory record."""
    if not screenshot_path:
        return ""
    screenshot = Path(screenshot_path)
    candidate = screenshot.with_name(screenshot.stem + "_ui.xml")
    return str(candidate) if candidate.is_file() else ""


def _load_detail(ctx: EvaluatorContext, action: dict, detail_type: str,
                 meta: dict) -> dict:
    """Load and cache screenshot/UI-tree/overlay detail from an action record."""
    path = action.get(detail_type, "")
    if detail_type == "action_overlay":
        path = action.get("action_overlay_path", "")
    elif detail_type == "ui_tree_before_extracted":
        path = action.get("ui_tree_before_raw", "")
        if not path:
            path = _paired_ui_tree_path(action.get("screenshot_before", ""))
    elif detail_type == "ui_tree_after_extracted":
        path = action.get("ui_tree_after_raw", "")
        if not path:
            path = _paired_ui_tree_path(action.get("screenshot_after", ""))
    elif detail_type == "model_response":
        path = action.get("model_response_path", "")
        # Fallback: derive from screenshot_before path
        if not path:
            sb = action.get("screenshot_before", "")
            if sb:
                path = sb.replace("_vla_input.jpg", "_vla_model_response.json")

    if not path:
        return {**meta, "error": f"empty path for {detail_type}", "detail_type": detail_type}

    path = resolve_context_artifact(ctx, path)

    result = {**meta, "detail_type": detail_type, "path": path,
              "global_step": action.get("global_step", "?"),
              "file_step": parse_file_step(path),
              "label": describe_action(action)}

    if detail_type.startswith("screenshot"):
        return _load_screenshot(ctx, path, result)

    if detail_type.startswith("ui_tree") or detail_type == "ui_tree_before_extracted" or detail_type == "ui_tree_after_extracted":
        return _load_ui_tree(ctx, path, result, detail_type)

    if detail_type == "action_overlay":
        # Backward compat: old trajectories store .png paths but files are .jpg
        if not os.path.exists(path) and path.endswith(".png"):
            alt = path[:-4] + ".jpg"
            if os.path.exists(alt):
                logger.info("[fetch_step_detail] overlay fallback .png -> .jpg: %s", alt)
                path = alt
        if os.path.exists(path):
            file_size = os.path.getsize(path)
            try:
                from agent.models.llm_client import encode_image_base64
                b64 = encode_image_base64(path)
                _cache_on_ctx(ctx, path, b64)
                result["exists"] = True
                result["file_size_kb"] = round(file_size / 1024, 1)
                result["b64_chars"] = len(b64)
                result["cached"] = True
                logger.warning(
                    "[fetch_step_detail] loaded screenshot: %s (%s KB, b64=%s chars)",
                    os.path.basename(path), result["file_size_kb"], len(b64),
                )
            except Exception as e:
                result["exists"] = True
                result["file_size_kb"] = round(file_size / 1024, 1)
                result["load_error"] = str(e)
        else:
            result["exists"] = False
            result["error"] = f"file not found: {path}"
        return result

    if detail_type == "model_response":
        return _load_model_response(ctx, path, result)

    return {**result, "error": f"unknown detail_type: {detail_type}"}


def _load_screenshot(ctx: EvaluatorContext, path: str, result: dict) -> dict:
    path = resolve_context_artifact(ctx, path)
    result["path"] = path
    if not os.path.exists(path):
        result["exists"] = False
        result["error"] = f"file not found: {path}"
        return result
    file_size = os.path.getsize(path)
    try:
        from agent.models.llm_client import encode_image_base64
        b64 = encode_image_base64(path)
        _cache_on_ctx(ctx, path, b64)
        result["exists"] = True
        result["file_size_kb"] = round(file_size / 1024, 1)
        result["b64_chars"] = len(b64)
        result["cached"] = True
        logger.warning(
            "[fetch_step_detail] loaded screenshot: %s (%s KB, b64=%s chars)",
            os.path.basename(path), result["file_size_kb"], len(b64),
        )
    except Exception as e:
        result["exists"] = True
        result["file_size_kb"] = round(file_size / 1024, 1)
        result["load_error"] = str(e)
    return result


def _load_ui_tree(ctx: EvaluatorContext, path: str, result: dict,
                  detail_type: str) -> dict:
    """Load and process UI tree.  ALL UI tree types go through
    filter_interactive + format_for_llm.  Raw XML is NEVER returned to the model.

    Handles three source formats:
      - File path to raw XML on disk
      - Legacy inline XML string (starts with "<")
      - Cached processed text in _ui_tree/ subdirectory
    """

    # ── Detect inline XML (legacy trajectory) ────────────────────────────
    if path.strip().startswith("<"):
        # Legacy trajectory stored the full XML string as the "path".
        # Process it directly — no file I/O.
        raw_xml = path
        processed = _process_ui_tree(raw_xml)
        result["exists"] = True
        result["filtered"] = True
        result["line_count"] = processed.count("\n") + 1
        result["chars"] = len(processed)
        result["path"] = "(inline legacy XML)"
        result["content"] = processed
        _cache_on_ctx(ctx, result["path"], processed)
        logger.warning("[fetch_step_detail] processed legacy inline UI tree (%s lines)",
                       result["line_count"])
        return result

    # ── File path: process and cache ─────────────────────────────────────
    path = resolve_context_artifact(ctx, path)
    raw_path = Path(path)
    if ctx.trajectory.get("_strict_artifacts"):
        # A snapshot is immutable evidence. Process in memory; old disk caches
        # are neither trusted nor regenerated beside original collection files.
        content = _process_ui_tree(raw_path.read_text(encoding="utf-8-sig"))
        result.update(exists=True, filtered=True, path=path, content=content,
                      line_count=content.count("\n") + 1, chars=len(content))
        _cache_on_ctx(ctx, path, content)
        return result
    ui_tree_dir = raw_path.parent / "_ui_tree"
    processed_name = raw_path.stem + "_processed.txt"
    processed_path = ui_tree_dir / processed_name

    # Check processed cache first
    if processed_path.exists():
        content = processed_path.read_text(encoding="utf-8")
        result["exists"] = True
        result["filtered"] = True
        result["line_count"] = content.count("\n") + 1
        result["chars"] = len(content)
        result["path"] = str(processed_path)
        result["content"] = content
        _cache_on_ctx(ctx, str(processed_path), content)
        logger.warning("[fetch_step_detail] loaded cached processed UI tree: %s (%s lines)",
                       processed_path.name, result["line_count"])
        return result

    # Read raw XML from disk
    if not raw_path.exists():
        result["exists"] = False
        result["error"] = f"UI tree file not found: {raw_path.name}"
        return result

    try:
        raw_xml = raw_path.read_text(encoding="utf-8")
    except Exception as e:
        result["exists"] = False
        result["error"] = f"failed to read UI tree: {type(e).__name__}"
        return result

    # Process: filter_interactive + format_for_llm
    processed = _process_ui_tree(raw_xml)

    # Save to _ui_tree/ cache
    ui_tree_dir.mkdir(parents=True, exist_ok=True)
    processed_path.write_text(processed, encoding="utf-8")
    logger.warning("[fetch_step_detail] processed and cached UI tree: %s (%s chars)",
                   processed_path.name, len(processed))

    result["exists"] = True
    result["filtered"] = True
    result["line_count"] = processed.count("\n") + 1
    result["chars"] = len(processed)
    result["path"] = str(processed_path)
    result["content"] = processed
    _cache_on_ctx(ctx, str(processed_path), processed)
    return result


def _load_model_response(ctx: EvaluatorContext, path: str, result: dict) -> dict:
    """Load and parse a VLA model_response JSON file.
    Extracts thought, tool_call, and summary from the XML-tagged content field."""
    import json, re
    path = resolve_context_artifact(ctx, path)
    result["path"] = path
    if not os.path.exists(path):
        result["exists"] = False
        result["error"] = f"model_response file not found: {os.path.basename(path) if path else 'N/A'}"
        return result
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        result["exists"] = False
        result["error"] = f"failed to read model_response: {e}"
        return result

    content = data.get("content", "")
    thought = ""
    tool_call = ""
    summary = ""
    m = re.search(r"<thought>(.*?)</thought>", content, re.DOTALL)
    if m:
        thought = m.group(1).strip()
    m = re.search(r"<tool_call>(.*?)</tool_call>", content, re.DOTALL)
    if m:
        tool_call = m.group(1).strip()
    m = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL)
    if m:
        summary = m.group(1).strip()

    result["exists"] = True
    result["thought"] = thought
    result["tool_call"] = tool_call
    result["summary"] = summary
    result["intent_text"] = f"意图: {thought} 操作: {tool_call} 总结: {summary}" if thought or summary else ""
    logger.warning("[fetch_step_detail] loaded model_response: step=%s summary=%s",
                   result.get("global_step", "?"), summary[:60])
    return result


def _process_ui_tree(xml_text: str) -> str:
    """Apply filter_interactive() + format_for_llm() from ui_tree_extracted."""
    try:
        from ui_tree_extracted import UITreeExtractor
        elements = UITreeExtractor.parse_ui_xml(xml_text)
        filtered = UITreeExtractor.filter_interactive(elements)
        return UITreeExtractor.format_for_llm(filtered)
    except Exception:
        # Fallback: return raw text
        return xml_text


def _list_all_screenshots(ctx: EvaluatorContext) -> dict:
    """Scan the session directory for all available screenshots.

    Every entry is self-locating: a ``label`` (``step#N`` or ``step#N (file
    stepNNN)`` when the ordinal and on-disk index differ) plus, for on-disk files
    not referenced by the trajectory, an explicit ``global_step`` mapping or a
    ``loadable_via_global_step=False`` flag so the model never feeds a physical
    index back as an ordinal and silently resolves to a *different* action.
    """
    from pathlib import Path as _Path

    flat = ctx.trajectory.get("actions_flat", [])
    fsi = file_step_index(flat)

    all_files = []
    referenced_paths: set[str] = set()
    # Try to find screenshots from trajectory file locations
    for a in flat:
        gs = a.get("global_step")
        for key in ("screenshot_before", "screenshot_after"):
            p = a.get(key, "")
            if not p:
                continue
            referenced_paths.add(os.path.normpath(p))
            fs = parse_file_step(p)
            entry = {"global_step": gs, "type": key, "path": p, "exists": os.path.exists(p),
                     "file_step": fs, "label": describe_step(gs, fs)}
            all_files.append(entry)

    # On-disk files not referenced by any committed action's before/after fields
    # (action overlays, orphan captures, cancelled-prefetch debris). Map each to
    # its committed ordinal when one owns it. loadable_via_global_step means
    # fetch_step_detail(global_step=ordinal, ...) actually RETURNS this file —
    # i.e. the owning action record references it. Aux files that merely share the
    # index (overlays not stored on the record) must be viewed via image_path.
    extra: list[dict] = []
    if flat:
        first_shot = flat[0].get("screenshot_before", "")
        if first_shot:
            scan_dir = _Path(first_shot).parent
            for f in sorted(scan_dir.glob("step*_vla_*.jpg")):
                if "_input_stability" in f.name:
                    continue  # internal stability re-captures, not action screenshots
                norm = os.path.normpath(str(f))
                if norm in referenced_paths:
                    continue  # already listed above as a trajectory screenshot
                fs = parse_file_step(f)
                owner = fsi.get(fs)  # ordinal owning this on-disk file index, or None
                if owner is not None:
                    owner_action = next((a for a in flat
                                         if a.get("global_step") == owner), {})
                    stored = {os.path.normpath(str(p)) for p in (
                        owner_action.get("screenshot_before"),
                        owner_action.get("screenshot_after"),
                        owner_action.get("action_overlay_path"),
                    ) if p}
                    loadable = norm in stored
                    extra.append({
                        "path": str(f), "file_step": fs, "global_step": owner,
                        "label": describe_step(owner, fs),
                        "loadable_via_global_step": loadable,
                        "note": ("" if loadable else
                                 f"owned by committed action ordinal {owner}, but not "
                                 "referenced by the action record — load this exact "
                                 "file via image_path, NOT fetch_step_detail"),
                    })
                else:
                    extra.append({
                        "path": str(f), "file_step": fs, "global_step": None,
                        "label": describe_step(None, fs),
                        "loadable_via_global_step": False,
                        "note": ("no committed action (cancelled prefetch / uncommitted). "
                                 "load via image_path, NOT global_step"),
                    })

    # Also include the final orchestrator screenshot (the LAST one only)
    final_orch = ctx.trajectory.get("final_orch_screenshot", "")
    final_orch_list = []
    if final_orch and os.path.exists(final_orch):
        final_orch_list.append({
            "path": final_orch, "type": "final_orch_screenshot",
            "description": "FINAL page state when agent declared task complete",
        })

    return {"screenshots": all_files, "extra_on_disk": extra,
            "final_orch_screenshot": final_orch_list,
            "total_from_trajectory": len(all_files),
            "total_on_disk": len(extra)}


def _cache_on_ctx(ctx: EvaluatorContext, key: str, value: object) -> None:
    if not hasattr(ctx, "_file_cache"):
        ctx._file_cache = {}
    norm = os.path.normpath(key)
    ctx._file_cache[norm] = value
    # Also index by basename for fuzzy lookup
    ctx._file_cache[os.path.basename(key)] = value


def get_cached(ctx: EvaluatorContext, key: str) -> object | None:
    if not hasattr(ctx, "_file_cache"):
        return None
    norm = os.path.normpath(key)
    if norm in ctx._file_cache:
        return ctx._file_cache[norm]
    # Fallback: lookup by basename
    return ctx._file_cache.get(os.path.basename(key))
