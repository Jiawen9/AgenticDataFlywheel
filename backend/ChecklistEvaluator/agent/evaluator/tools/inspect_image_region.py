from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from pathlib import Path

from agent.evaluator.tools.registry import EvaluatorContext
from agent.evaluator.step_identity import describe_step, parse_file_step, resolve_ordinal
from agent.evaluator.resource_paths import resolve_context_artifact

logger = logging.getLogger(__name__)


def _resolve_case_artifact_path(ctx: EvaluatorContext, raw_path: str) -> str:
    """Resolve stale absolute artifact paths after a run directory is moved.

    Trajectory JSONs store absolute paths, so organizing ``.runs`` can leave
    those references pointing at the old location.  The trajectory's own
    directory is the authoritative case directory; only fall back to a file
    with the same basename there (never search unrelated runs).
    """
    return resolve_context_artifact(ctx, raw_path)


def _lookup_action_context(ctx: EvaluatorContext, global_step: int) -> dict:
    """Harness looks up the action record from actions_flat and returns all
    file paths + intent_text.  LLM never needs to pass individual file paths.

    NOTE: ``global_step`` here is the *ordinal* from actions_flat (resolve it via
    agent.evaluator.step_identity.resolve_ordinal first) — do NOT pass a raw on-disk
    file index, which differs from the ordinal whenever prefetch cancellations left
    holes in the committed screenshot numbering."""
    def _derive_path(base_path: str, from_suffix: str, to_suffix: str) -> str:
        """Derive related file path by swapping suffixes (e.g. _done.jpg → _overlay.jpg)."""
        if not base_path:
            return ""
        derived = base_path.replace(from_suffix, to_suffix)
        if derived != base_path and os.path.exists(derived):
            return derived
        return ""

    flat = ctx.trajectory.get("actions_flat", [])
    for a in flat:
        if a.get("global_step") != global_step:
            continue
        before = _resolve_case_artifact_path(ctx, a.get("screenshot_before", ""))
        after = _resolve_case_artifact_path(ctx, a.get("screenshot_after", ""))
        overlay = _resolve_case_artifact_path(ctx, a.get("action_overlay_path", ""))

        # Fallback: derive overlay from screenshot path naming convention
        if not overlay or not os.path.exists(overlay):
            derived = _derive_path(after, "_vla_done.jpg", "_vla_overlay.jpg")
            if not derived:
                derived = _derive_path(before, "_vla_input.jpg", "_vla_overlay.jpg")
            if derived:
                overlay = derived

        # Load intent from model_response
        intent = ""
        mr_path = _resolve_case_artifact_path(ctx, a.get("model_response_path", ""))
        # Fallback: derive model_response path
        if not mr_path or not os.path.exists(mr_path):
            derived = _derive_path(after, "_vla_done.jpg", "_vla_model_response.json")
            if not derived:
                derived = _derive_path(before, "_vla_input.jpg", "_vla_model_response.json")
            if derived:
                mr_path = derived
        if mr_path and os.path.exists(mr_path):
            try:
                data = json.loads(open(mr_path, encoding="utf-8").read())
                content = data.get("content", "")
                tm = re.search(r"<thought>(.*?)</thought>", content, re.DOTALL)
                sm = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL)
                if tm:
                    intent += tm.group(1).strip()
                if sm:
                    intent = (intent + " " + sm.group(1).strip()).strip()
            except Exception:
                pass

        return {
            "image_path": after,
            "before_path": before,
            "overlay_path": overlay,
            "intent_text": intent,
            "action_text": str(a.get("action_desc", "") or a.get("subtask_desc", "") or ""),
        }
    return {}


def inspect_image_region(ctx: EvaluatorContext, args: dict) -> dict:
    global_step = args.get("global_step")
    image_path = str(args.get("image_path", ""))
    target = str(args.get("target_keyword", ""))
    bbox = args.get("bbox")
    overlay_path = str(args.get("overlay_path", "") or "").strip()
    before_path = str(args.get("before_path", "") or "").strip()
    intent_text = str(args.get("intent_text", "") or "").strip()
    action_text = str(args.get("action_text", "") or "").strip()
    step_summary_mode = bool(args.get("_step_summary_mode", False))

    # ── Harness auto-enrich: canonicalize the step handle, then load the action
    #    record so before/overlay/intent auto-populate. ──
    # global_step may be an ordinal, a physical file index leaked from disk, or a
    # NON-numeric orchestrator label ('final', 'turn021_orch', …). Only committed
    # actions have an ordinal; cancelled-prefetch debris and orchestrator shots
    # enrich from nothing. resolve_ordinal keeps ordinal↔file consistent even when
    # committed file indices have holes (prefetch cancellations) — it must NOT
    # blindly treat a numeric global_step as a physical file index.
    flat = ctx.trajectory.get("actions_flat", []) if ctx.trajectory else []
    ordinal = resolve_ordinal(flat, gs=global_step, path=image_path)
    if ordinal is not None:
        enriched = _lookup_action_context(ctx, ordinal)
        if not image_path:
            image_path = enriched.get("image_path", "")
        if not before_path:
            before_path = enriched.get("before_path", "")
        if not overlay_path:
            overlay_path = enriched.get("overlay_path", "")
        if not intent_text:
            intent_text = enriched.get("intent_text", "")
        if not action_text:
            action_text = enriched.get("action_text", "")

    image_path = _resolve_case_artifact_path(ctx, image_path)
    before_path = _resolve_case_artifact_path(ctx, before_path)
    overlay_path = _resolve_case_artifact_path(ctx, overlay_path)
    if not image_path or not os.path.exists(image_path):
        return {"error": f"image not found: {image_path}",
                "global_step": global_step}

    import config

    # Vision images are sent RESIZED to a bounded token budget — the same Qwen
    # smart-resize rule the framework's orche/VLA paths use. Raw full-size
    # 1176x2400 screenshots make each VLM request huge/slow (verified: resizing to
    # config.EVALUATOR_IMAGE_TOKENS keeps 6 images ≈12k prompt tokens).  Sent by
    # prepare_vision_image which memoizes per path.
    from agent.evaluator.tools.image_prep import max_images_per_call, prepare_vision_image

    b64 = prepare_vision_image(image_path)[0]

    # Load the BEFORE image for paired objective description.
    before_b64 = ""
    if before_path and os.path.exists(before_path):
        before_b64 = prepare_vision_image(before_path)[0]
    elif before_path:
        logger.warning("[inspect_image_region] before_path NOT FOUND: %s", before_path)
    # Normalize legacy .png overlay references before loading the image.
    if overlay_path and not os.path.exists(overlay_path) and overlay_path.endswith(".png"):
        alt = overlay_path[:-4] + ".jpg"
        if os.path.exists(alt):
            overlay_path = alt
    overlay_b64 = ""
    if overlay_path and os.path.exists(overlay_path):
        overlay_b64 = prepare_vision_image(overlay_path)[0]
    elif overlay_path:
        logger.warning("[inspect_image_region] overlay_path NOT FOUND: %s", overlay_path)

    # The VLM always performs evidence extraction only. With an action handle we
    # send the before/after pair; a final/orphan screenshot is described alone.
    paired_mode = bool(before_b64)

    logger.warning(
        "[inspect_image_region] mode=%s has_before=%s has_intent=%s | done=%s",
        "step-triplet" if step_summary_mode and overlay_b64 else "paired(before+after)" if paired_mode else "single",
        bool(before_b64), bool(intent_text),
        os.path.basename(image_path),
    )

    if bbox and isinstance(bbox, dict):
        try:
            from PIL import Image
            img = Image.open(image_path)
            x = int(bbox.get("x", 0))
            y = int(bbox.get("y", 0))
            w = int(bbox.get("w", 0))
            h = int(bbox.get("h", 0))
            cropped = img.crop((x, y, x + w, y + h))
            buf = io.BytesIO()
            cropped.convert("RGB").save(buf, format="JPEG", quality=90, optimize=True)
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as e:
            logger.warning("[inspect_image_region] crop failed: %s, using full image", e)

    if step_summary_mode and paired_mode:
        image_order_note = (
            "Images are BEFORE, ACTION OVERLAY, AFTER."
            if overlay_b64 else
            "The ACTION OVERLAY image is unavailable; the images are BEFORE and AFTER only. "
            "Leave overlay_target uncertain/empty rather than guessing."
        )
        system = (
            "You are an objective visual transition recorder for a GUI evaluator. You receive images "
            "from exactly ONE executed action step. " + image_order_note + " "
            "The ACTION OVERLAY, when present, is the annotation marking the action target on the "
            "before/action screen. "
            "The overlay is an aid for locating the intended control; use the before screenshot to read "
            "its actual label and context. Do not infer task success, action correctness, usefulness, "
            "or causality beyond visible change. Do not invent unreadable labels.\n\n"
            "OUTPUT CONTRACT — MUST FOLLOW EXACTLY:\n"
            "- Return exactly ONE syntactically valid JSON object, and nothing else.\n"
            "- Do not use Markdown fences, prose, headings, comments, or trailing commas.\n"
            "- Use exactly the keys and value types shown below; do not omit keys or add keys.\n"
            "- Summary fields must contain plain text, never serialized/nested JSON.\n"
            "- Escape any double quote or newline occurring inside a JSON string.\n"
            "- Use empty strings/lists for unknown information; uncertainty must be an array of strings.\n"
            "Required JSON schema (both states must use exactly these structured fields):\n"
            "{\n"
            '  "before_state_summary": "concise objective screen description",\n'
            '  "before_page": "app/page identity if visible, else uncertain",\n'
            '  "before_visible_entities": ["visible relevant titles, controls, labels"],\n'
            '  "overlay_target": {"description": "control/entity under annotation", '
            '"visible_label": "readable label or empty", "certainty": "high|medium|low"},\n'
            '  "after_state_summary": "concise objective screen description",\n'
            '  "after_page": "app/page identity if visible, else uncertain",\n'
            '  "after_visible_entities": ["visible relevant titles, controls, labels"],\n'
            '  "before_state": {"app_id":"literal visible app name or unknown", "page_type":"short normalized UI function or unknown", "page_title":"literal visible title or unknown", "navigation_path":["visible navigation labels in order"], "active_tab":"literal selected tab or unknown", "selected_filters":{"visible filter name":"literal selected value"}, "task_relevant_entities":[{"type":"content/control/rank/other", "value":"literal visible text", "rank":"visible ordinal or unknown", "task_relation":"directly relevant/possibly relevant/unknown"}], "modal_or_ad":{"kind":"none/ad/modal/loading/unknown", "blocking":"yes/no/unknown", "dismiss_controls":["visible labels or controls"]}, "media_state":"playing/paused/loading/not_applicable/unknown", "volatile_content":["recommendation or rotating content that may change without changing page identity"], "visible_controls":["literal visible actionable labels"], "uncertainty":["unknown or unreadable fields"], "evidence_refs":["before"]},\n'
            '  "after_state": {"app_id":"literal visible app name or unknown", "page_type":"short normalized UI function or unknown", "page_title":"literal visible title or unknown", "navigation_path":["visible navigation labels in order"], "active_tab":"literal selected tab or unknown", "selected_filters":{"visible filter name":"literal selected value"}, "task_relevant_entities":[{"type":"content/control/rank/other", "value":"literal visible text", "rank":"visible ordinal or unknown", "task_relation":"directly relevant/possibly relevant/unknown"}], "modal_or_ad":{"kind":"none/ad/modal/loading/unknown", "blocking":"yes/no/unknown", "dismiss_controls":["visible labels or controls"]}, "media_state":"playing/paused/loading/not_applicable/unknown", "volatile_content":["recommendation or rotating content that may change without changing page identity"], "visible_controls":["literal visible actionable labels"], "uncertainty":["unknown or unreadable fields"], "evidence_refs":["after"]},\n'
            '  "observed_transition": "only visible before-to-after change",\n'
            '  "uncertainty": ["ambiguous or unreadable details"]\n'
            "}\n"
            "Before responding, verify that the whole response parses as one JSON object and that "
            "all keys and value types match this schema. If uncertain about a visual fact, keep the "
            "schema valid and describe the uncertainty in the uncertainty array.\n"
            "For free-text fields, describe the screen in Chinese by default. If a title or UI label is "
            "clearly written in English in the SCREENSHOT, preserve that literal English text for that "
            "specific title/label; an English app brand or isolated control does not switch the whole "
            "description to English. MANDATORY: page_type must be a short Chinese UI-function phrase, "
            "not an English taxonomy code or snake_case label. This applies even when page_title is "
            "a visible English word. Keep fixed JSON keys/enumerations exactly as specified. "
            "The screenshot is primary evidence for page_title; UI/XML may supplement other fields "
            "but a missing or stale UI/XML title must not determine or overwrite page_title. "
            "Prefer literal words visible in the screenshot for titles, tabs, filters, entities, ranks and controls; "
            "also give a short normalized page_type without replacing the literal title with a guessed synonym. "
            "Keep task-relevant entities separate from volatile recommendations. Record selected date/category/sort "
            "values, ranking name and item ordinal, ad/modal blocking and dismissibility, and playback state. "
            "Use unknown for absent/unreadable facts; never infer them from task, action intent, checklist, or verdict. "
            "evidence_refs may contain only before, overlay, and/or after, matching the image role that supports the fact. "
            "Unknown collections are empty; do not guess."
        )
        user_text = (
            f"Task: {ctx.trajectory.get('task', '')}\n"
            f"Action record: {action_text or '(unavailable)'}\n"
            f"Recorded model intent (context only): {intent_text or '(unavailable)'}\n"
            f"Focus hint: {target or '(none)'}\n\n"
            "Analyze the three images for this single step only. Identify what the overlay marks, but "
            "report the control/entity based on visible UI context. Describe before and after separately. "
            "Do not output pass/fail, task success, or action-quality judgments."
        )
    elif paired_mode:
        system = (
            "You are a visual evidence extractor for a GUI evaluator. You receive TWO images:\n"
            "  Image 1: BEFORE — the screen state before the agent's action.\n"
            "  Image 2: AFTER — the screen state after the action was executed.\n"
            "Extract observable information only. Do NOT decide task success, evidence sufficiency, "
            "or whether the step was beneficial/neutral/harmful; the text model makes all judgments.\n\n"
            "OUTPUT CONTRACT — MUST FOLLOW EXACTLY: Return exactly ONE syntactically valid JSON object "
            "and nothing else. No Markdown fences, prose, comments, or trailing commas. Use exactly "
            "the keys and value types shown; do not omit or add keys. Escape quotes/newlines inside "
            "strings. Before responding, verify the entire response parses as JSON.\n"
            'Format: {"before_description": "<Chinese objective description>", '
            '"after_description": "<Chinese objective description>", '
            '"transition_description": "<only observable before→after changes>", '
            '"before_visible_text": "<readable text, comma-separated>", '
            '"after_visible_text": "<readable text, comma-separated>", '
            '"uncertainty": "<unreadable/ambiguous visual details, or empty>"}\n'
            "Include app/page identity, layout, salient controls, media state, dialogs, selection "
            "indicators, and text relevant to the requested focus. Describe uncertainty instead of guessing."
        )
        user_text = (
            f"Requested focus (for attention only; do not judge): {target or 'describe the full screen'}\n"
            f"Executed action (observable context): {action_text or '(unavailable)'}\n"
            f"Recorded action intent (context only; do not judge): {intent_text or '(unavailable)'}\n\n"
            f"Image 1 (BEFORE): The screen state BEFORE the agent's action.\n"
            f"Image 2 (AFTER): The screen state AFTER the action.\n\n"
            "Describe each image separately, then describe only the observable transition. "
            "Do not output found/missing, pass/fail, effectiveness, or recommendations."
        )
    else:
        system = (
            "You are a visual evidence extractor for a GUI evaluator. Analyze the provided screenshot.\n"
            "Extract observable information only. Do NOT decide task success, evidence sufficiency, "
            "or step effectiveness; the text model makes all judgments.\n\n"
            "OUTPUT CONTRACT — MUST FOLLOW EXACTLY: Return exactly ONE syntactically valid JSON object "
            "and nothing else. No Markdown fences, prose, comments, or trailing commas. Use exactly "
            "the keys and value types shown; do not omit or add keys. Escape quotes/newlines inside "
            "strings. Before responding, verify the entire response parses as JSON.\n"
            'Format: {"before_description": "", "after_description": "<Chinese objective description>", '
            '"transition_description": "", "before_visible_text": "", '
            '"after_visible_text": "<readable text, comma-separated>", '
            '"uncertainty": "<unreadable/ambiguous details, or empty>"}\n'
            "Include app/page identity, layout, salient controls, media state, dialogs, selection "
            "indicators, and text relevant to the requested focus. Describe uncertainty instead of guessing."
        )
        user_text = (
            f"Requested focus (for attention only; do not judge): {target or 'describe the full screen'}\n"
            "Describe the screenshot objectively. Do not output found/missing, pass/fail, "
            "effectiveness, or recommendations."
        )

    # Assemble ordered, LABELED image slots. Each image is prefixed by its own
    # caption [图N] <step / kind> so any multi-image request is unambiguous about
    # which screenshot it refers to.  Cap the request at EVALUATOR_MAX_IMAGES_PER_CALL.
    if paired_mode:
        slots = [(_describe_slot(before_path, ordinal=ordinal, role="操作前(before)"), before_b64, "before")]
        if overlay_b64:
            slots.append((_describe_slot(overlay_path, ordinal=ordinal, role="动作标注(action overlay)"), overlay_b64, "overlay"))
        slots.append((_describe_slot(image_path, ordinal=ordinal, role="操作后(after)"), b64, "after"))
    else:
        slots = [(_describe_slot(image_path, ordinal=ordinal), b64, "after")]

    _cap = max(3, max_images_per_call()) if step_summary_mode else max_images_per_call()
    if len(slots) > _cap:
        logger.warning("[inspect_image_region] %d images exceeds cap=%d, truncating to first %d",
                       len(slots), _cap, _cap)
        slots = slots[:_cap]

    from agent.models.llm_client import make_image_content
    content: list[dict] = [{"type": "text", "text": user_text}]
    for idx, (label, bb, _role) in enumerate(slots, 1):
        content.append({"type": "text",
                        "text": f"[图{idx}/{len(slots)}] 这张图是: {label} —— 检查对象见上方文字描述。"})
        content.append(make_image_content(bb))
    result_paired = len(slots) >= 2
    image_roles_sent = [role for _label, _bb, role in slots]
    messages = [{"role": "user", "content": content}]

    vision_llm = ctx._vision_llm
    if vision_llm is None:
        return {"error": "vision_llm not set on context"}

    model = getattr(config, "EVALUATOR_VISION_MODEL", "qwen3.8-flash")
    try:
        resp = vision_llm.chat_with_vision(
            model=model,
            system=system,
            messages=messages,
            temperature=0.0,
            role="evaluator",
            phase="vision",
            max_tokens=2400 if step_summary_mode else 4096,
        )
        content = resp.get("content", "").strip()
        try:
            result = json.loads(content)
            result.setdefault("before_description", "")
            result.setdefault("after_description", "")
            result.setdefault("transition_description", "")
            result.setdefault("before_visible_text", "")
            result.setdefault("after_visible_text", "")
            result.setdefault("uncertainty", "")
            result.setdefault("before_state_summary", result.get("before_description", ""))
            result.setdefault("before_page", "")
            result.setdefault("before_visible_entities", [])
            result.setdefault("overlay_target", {})
            result.setdefault("after_state_summary", result.get("after_description", ""))
            result.setdefault("after_page", "")
            result.setdefault("after_visible_entities", [])
            result.setdefault("observed_transition", result.get("transition_description", ""))
            for role in ("before", "after"):
                result.setdefault(f"{role}_state", {})
        except json.JSONDecodeError:
            result = {
                "before_description": content[:300] if paired_mode else "",
                "after_description": content[:300],
                "transition_description": "",
                "before_visible_text": "",
                "after_visible_text": "",
                "uncertainty": "VLM returned non-JSON text; raw response retained",
                "before_state_summary": content[:300] if paired_mode else "",
                "before_page": "",
                "before_visible_entities": [],
                "overlay_target": {},
                "after_state_summary": content[:300],
                "after_page": "",
                "after_visible_entities": [],
                "observed_transition": "",
                "before_state": {},
                "after_state": {},
            }
        # Judgment fields are intentionally not accepted from the VLM. Strip any
        # model habit outputs so they cannot leak into the text evaluator context.
        for field_name in (
            "found", "target_visible", "step_effect", "step_effect_reason", "scene",
            "detected_text", "description", "verdict", "reasoning",
        ):
            result.pop(field_name, None)
        result["step_index"] = ordinal
        result["query"] = target
        result["action_text"] = action_text
        result["before_path"] = before_path if paired_mode else ""
        result["overlay_path"] = overlay_path if overlay_b64 else ""
        result["after_path"] = image_path
        result["image_roles_sent"] = image_roles_sent
        result["step_triplet_complete"] = image_roles_sent == ["before", "overlay", "after"]
        result["cropped"] = bbox is not None
        result["paired"] = result_paired  # whether before/after were compared
        journal_step = (
            -1 if str(global_step).lower() == "final"
            else (ordinal if ordinal is not None else 0)
        )
        if args.get("_record_observation", True):
            ctx.record_vision_observation(
                journal_step,
                {k: v for k, v in result.items() if k not in {"cropped", "paired"}},
            )

        # E3: write tool trace for full VLM call traceability
        _fh = getattr(ctx, "_dialog_fh", None)
        if _fh is not None and args.get("_write_tool_trace", True):
            try:
                from agent.evaluator.evaluator_agent import _write_tool_trace
                _write_tool_trace(_fh, -1, "inspect_image_region", model,
                                  system, str(args), result, resp.get("usage"))
            except Exception:
                pass

        return result
    except Exception as e:
        return {"error": str(e)}


def _describe_slot(path: str, *, ordinal=None, role: str = "") -> str:
    """Human label for one screenshot in a multi-image prompt.

    Uses the canonical step identity: the resolved ordinal plus the physical
    ``stepNNN`` parsed from the file name, e.g. ``step#3 (file step004)`` when they
    diverge (committed file indices can have holes after prefetch cancellations).
    Non-step artifacts (orchestrator turn screenshots, …) fall back to their
    basename so the VLM is never shown a bare ``step?``.
    """
    label = describe_step(ordinal, parse_file_step(path))
    parts: list[str] = []
    if label != "step?":
        parts.append(label)
    if role:
        parts.append(role)
    if not parts:
        parts.append(os.path.splitext(os.path.basename(str(path)))[0])
    return " ".join(parts)


def _get_cached(ctx: EvaluatorContext, key: str) -> str | None:
    if not hasattr(ctx, "_file_cache"):
        return None
    cache = ctx._file_cache
    norm = os.path.normpath(key)
    if norm in cache:
        return cache[norm]
    # Fallback by basename
    return cache.get(os.path.basename(key))
