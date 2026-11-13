from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import local

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config
from agent.evaluator.tools.registry import (
    EvaluatorContext,
    EvaluatorToolRegistry,
    build_default_registry,
)
from agent.evaluator.eval_types import EvaluationReport
from agent.evaluator.gold_checklist import load_gold_checklist
from agent.evaluator.resource_paths import bind_snapshot_artifacts, resolve_artifact
from evaluation_profiles import current_identity, load_rules
from file_access import io_path
from agent.evaluator.report_state import (
    latest_report,
    read_report,
    report_checklist_state,
    sha256_file,
)
from agent.evaluator.step_identity import (
    describe_action,
    file_step_index,
    parse_file_step,
    resolve_ordinal,
)
from agent.models.llm_client import LLMClient

logger = logging.getLogger(__name__)

# Module-level toggle: when True, skip vision extraction for steps not referenced
# by any checklist verdict. Significantly reduces VLM calls but produces sparser
# key_steps_identified / redundant_steps_identified in the report.
_SKIP_UNREFERENCED_STEPS: bool = False
_STEP_SUMMARY_SCHEMA = "evaluator.step_visual_summaries.v2"
_STEP_SUMMARY_PROMPT_VERSION = "before_overlay_after.visual_state_v4.chinese_page_type"
_EXCLUDED_EVALUATION_RULE_IDS: set[str] = set()


def _step_vision_concurrency() -> int:
    configured = os.environ.get(
        "GUI_AGENT_EVALUATOR_STEP_VISION_CONCURRENCY",
        str(getattr(config, "EVALUATOR_STEP_VISION_CONCURRENCY", 3)),
    )
    try:
        return max(1, int(configured))
    except (TypeError, ValueError):
        logger.warning("Invalid evaluator step vision concurrency %r; using 3", configured)
        return 3
# Compact tool catalog embedded in the system prompt (no OpenAI function-calling).
# The LLM outputs JSON blocks; the harness parses and dispatches.
_TOOL_CATALOG = """Available tools (call via JSON block):
- generate_verification_checklist(task) → returns checklist JSON
- locate_key_steps() → returns key steps with scores
- fetch_step_detail(global_step, detail_type) → loads screenshot / UI tree / action_overlay / model_response. Use global_step="final" to load the FINAL orchestrator screenshot (page state when agent declared completion). Use 'action_overlay' to see the red-marked tap/target position on the screenshot — helpful for verifying playback, toggles, and other ambiguous actions.
- query_ui_tree_diff(global_step, keyword) → searches and compares processed UI trees before/after the committed action; global_step is the execution ordinal, not the stepNNN file index
- inspect_image_region(image_path, target_keyword) → VLM inspects the image at the exact file path. Prefer image_path with a canonical before/after/overlay/final file path. global_step is only a fallback lookup coordinate and must never be used as the evidence_ref value.
- detect_image_changes(before_path, after_path, threshold=15) → pixel-diff between two screenshots. Use when VLM cannot tell if a tap/click actually toggled a small icon/button state — compare the action's before/after screenshots.
- finish(verdict) → submit final verdict (success | partial_success | fail)"""


def evaluate_trajectory(trajectory_path: str, session_id: str | None = None,
                         output_dir: str = "", key_elements: str = "",
                         skip_unreferenced_steps: bool = False,
                         resume_from: str = "", resume_auto: bool = False,
                         evaluation_mode: str = "checklist",
                          case_id: str = "", checklist_policy: str = "auto") -> EvaluationReport:
    global _SKIP_UNREFERENCED_STEPS
    _SKIP_UNREFERENCED_STEPS = skip_unreferenced_steps

    with open(trajectory_path, "r", encoding="utf-8") as f:
        trajectory = json.load(f)

    # A run archive may carry the benchmark id only in its directory name,
    # e.g. ``<run-root>/<date>/AT-YYSP-HG-11``.  The batch adapter passes that
    # path-derived id explicitly.  It is authoritative for gold lookup and is
    # injected into the in-memory trajectory without modifying the source file.
    # Keeping both fields populated also supports older consumers that inspect
    # either spelling.
    explicit_case_id = str(case_id or "").strip()
    if explicit_case_id:
        trajectory["task_id"] = explicit_case_id
        trajectory["case_id"] = explicit_case_id
    # Keep the authoritative run directory available to prompt construction so
    # stale absolute artifact paths can be repaired to the local case archive.
    trajectory["_case_dir"] = str(Path(trajectory_path).resolve().parent)
    from readonly_inputs import REFERENCE_ENV, load_reference, adapt_reference
    reference_path = os.environ.get(REFERENCE_ENV)
    if reference_path:
        trajectory = adapt_reference(trajectory, trajectory_path, load_reference(reference_path, trajectory=trajectory_path))
    elif current_identity():
        bind_snapshot_artifacts(trajectory, trajectory_path)

    sid = session_id or trajectory.get("session_id", "unknown")

    traj_json_chars = len(json.dumps(trajectory, ensure_ascii=False))
    logger.warning("[Evaluator] loaded trajectory: %s chars, session=%s", traj_json_chars, sid)

    text_llm = LLMClient()
    vision_llm = LLMClient()

    ctx = EvaluatorContext(
        trajectory=trajectory,
        session_id=sid,
        _text_llm=text_llm,
        _vision_llm=vision_llm,
        key_elements=key_elements,
    )
    ctx._resume_metadata = {
        "attempt": 1,
        "resumed_from": None,
        "retained_item_ids": [],
        "newly_checked_item_ids": [],
        "rechecked_item_ids": [],
    }
    ctx._trajectory_path = str(Path(trajectory_path).resolve())
    if evaluation_mode not in {"checklist", "audit"}:
        raise ValueError("evaluation_mode must be 'checklist' or 'audit'")
    ctx._evaluation_mode = evaluation_mode
    if checklist_policy not in {"auto", "llm"}:
        raise ValueError("checklist_policy must be auto or llm")
    ctx._checklist_policy = checklist_policy

    registry = build_default_registry()

    system = _build_system_prompt()
    initial_prompt = _build_initial_prompt(trajectory)
    messages: list[dict] = [{"role": "user", "content": initial_prompt}]

    logger.warning("[Evaluator] system_prompt_chars=%s initial_prompt_chars=%s", len(system), len(initial_prompt))

    text_model = getattr(config, "EVALUATOR_TEXT_MODEL", "qwen3.8-flash")

    # D6: Adaptive step budget — count actual screenshots on disk, not trajectory actions
    total_screenshots = _count_screenshots_in_dir(Path(output_dir) if output_dir else Path("."))
    if total_screenshots == 0:
        # Fallback: count from actions_flat (plan_steps subtasks are always empty)
        total_screenshots = len(trajectory.get("actions_flat", []))
    total_screenshots = max(total_screenshots, 1)
    max_steps = _evaluator_max_steps(total_screenshots)
    logger.warning("[Evaluator] %s screenshots → max_steps=%s", total_screenshots, max_steps)

    # ── Dialog log for debugging ────────────────────────────────────────
    ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if output_dir:
        dialog_base = Path(output_dir)
    else:
        dialog_base = config.PROJECT_ROOT / ".evaluator" / f"session_{sid}"
    dialog_log_path = dialog_base / f"_evaluator_dialog_{ts_tag}.jsonl"
    dialog_log_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep historical v1 summaries immutable; the structured state contract is
    # written beside them as a separate versioned artifact.
    ctx._step_summary_path = str(dialog_base / "_evaluator_step_summaries_v2.json")
    ctx._check_audit_path = str(dialog_base / f"_evaluator_check_audit_{ts_tag}.json")
    ctx._checkpoint_path = str(dialog_base / "_evaluator_checkpoint.json")
    ctx._primary_eval_system = system
    ctx._primary_eval_messages = messages
    ctx._primary_eval_model = text_model
    resumed = _load_resume_state(ctx, trajectory_path, resume_from, resume_auto)
    if resumed:
        system += (
            "\n\n## Resumed Checklist Evaluation\n"
            "The checklist is already loaded and validated. Do not call "
            "generate_verification_checklist. Continue from the first unchecked item "
            "in checklist order. Keep earlier checked results unless new direct evidence "
            "requires a revision; never skip an unchecked earlier item. This resume rule "
            "overrides the initial workflow instruction to generate a checklist."
        )
        ctx._primary_eval_system = system
        messages.append({"role": "user", "content": _resume_context(ctx)})
    dialog_fh = open(dialog_log_path, "w", encoding="utf-8")
    ctx._dialog_fh = dialog_fh  # E3: allow tools to write trace records

    last_tool_key = ""  # detects repeated identical tool calls
    last_tool_result_str = ""
    repeat_count = 0     # Fix 2a: consecutive repeat counter
    inspected_visual_queries: set[str] = set()
    consecutive_parse_failures = 0
    verdict_only_stalls = 0
    evidence_context_index: int | None = None  # replace stale journal snapshots

    for step in range(max_steps):
        if ctx.all_checked():
            dialog_fh.close()
            return _finalize_report(ctx, _compute_verdict(ctx), step)

        # Keep one current Evidence Progress/Vision Journal message. Replacing the
        # prior snapshot preserves the full cumulative journal for the next call
        # without leaving O(n²) duplicate copies in chat history.
        if evidence_context_index is not None:
            messages.pop(evidence_context_index)
            evidence_context_index = None
        context_summary = ctx.build_evidence_context()
        if context_summary:
            messages.append({"role": "user", "content": context_summary})
            evidence_context_index = len(messages) - 1

        _log_llm_input_text(step, system, messages)

        # ── Plain chat — NO tools/tool_choice; LLM outputs JSON in text ──
        resp = text_llm.chat(
            model=text_model,
            system=system,
            messages=messages,
            temperature=0.0,
            role="evaluator",
            phase="text",
        )

        content = resp.get("content", "").strip()
        _log_llm_output_text(step, content)

        # Write dialog record + LLM stats
        _write_dialog_record(dialog_fh, step, system, messages, content)
        _write_llm_stats(dialog_fh, step, text_model,
                         sum(len(json.dumps(m, ensure_ascii=False, default=str)) for m in messages),
                         len(content), resp.get("usage"))
        if not content:
            continue

        messages.append({"role": "assistant", "content": content})

        # ── Parse JSON response from text ───────────────────────────────
        parsed = _parse_response(content)

        if parsed is None:
            consecutive_parse_failures += 1
            logger.warning("[Evaluator step %s] could not parse JSON from response", step)
            if consecutive_parse_failures >= 3:
                logger.warning(
                    "[Evaluator step %s] FORCE STOP: %d consecutive unparseable responses",
                    step, consecutive_parse_failures,
                )
                ctx._evaluator_stop_reason = "repeated_unparseable_responses"
                _leave_unchecked_incomplete(ctx)
                dialog_fh.close()
                return _finalize_report(ctx, _compute_verdict(ctx), step + 1)
            messages.append({"role": "user", "content": _nudge_unparseable(consecutive_parse_failures)})
            continue

        consecutive_parse_failures = 0

        # A response may contain both newly decided items and a request for
        # evidence about the remaining items. Apply the former, then dispatch
        # the latter in the same turn.
        verdicts = parsed.get("verdicts")
        tool_name = str(parsed.get("tool") or "").strip()
        tool_args = parsed.get("args", {}) or {}
        if verdicts:
            updated, invalid = _apply_verdicts(verdicts, ctx)
            logger.warning("[Evaluator step %s] verdicts: %s new/revised, %s invalid, progress=%s/%s, tool=%s",
                           step, updated, invalid, ctx.passed_count() + ctx.failed_count(),
                           len(ctx.checklist), tool_name or "none")
            if invalid > 0:
                first_unchecked = next((item for item in ctx.checklist if not _check_item_complete(item)), None)
                order_hint = (
                    f" Continue with the first unchecked item {first_unchecked.get('id')}: "
                    f"{first_unchecked.get('description')}. Do not submit later checklist items yet."
                    if first_unchecked else ""
                )
                messages.append({
                    "role": "user",
                    "content": f"{invalid} verdict(s) were not accepted. Checklist items must be judged in their listed order. Use passed/failed only. Every passed item must include a non-empty direct observation and evidence_ref naming the exact file path of a screenshot/UI-tree source actually inspected through a tool or registered precomputed visual summary. Do not cite global_step, step labels, task text, plans, or your inference as evidence. Re-inspect a different path if necessary; never repeat an identical image_path plus keyword.{order_hint}"
                })

            final_verdict = parsed.get("final_verdict")
            if ctx.all_checked():
                verdict = _resolve_final_verdict(ctx, final_verdict)
                dialog_fh.close()
                return _finalize_report(ctx, verdict, step + 1)
            if final_verdict:
                messages.append({
                    "role": "user",
                    "content": (
                        "final_verdict was not accepted because checklist items remain unchecked. "
                        "Continue evaluating the first unchecked item using direct tool-inspected evidence; "
                        "submit its verdict before the next item. You may submit item verdicts incrementally. "
                        "Only submit final_verdict after every item is checked."
                    ),
                })
            if not tool_name:
                verdict_only_stalls = 0 if updated else verdict_only_stalls + 1
                if verdict_only_stalls >= 3:
                    logger.warning("[Evaluator step %s] no new verdict or tool for %s turns; finalizing incomplete", step, verdict_only_stalls)
                    _leave_unchecked_incomplete(ctx)
                    dialog_fh.close()
                    return _finalize_report(ctx, _compute_verdict(ctx), step + 1)
                if not updated:
                    messages.append({
                        "role": "user",
                        "content": "That verdict did not change any checklist result. Submit only a new or revised verdict supported by evidence, or call a tool to inspect the remaining items. Never repeat an unchanged item as progress.",
                    })
                last_tool_key = ""
                repeat_count = 0
                continue

        # ── Case 2: Tool call request ──────────────────────────────────
        if not tool_name:
            if parsed.get("final_verdict") and not ctx.all_checked():
                messages.append({
                    "role": "user",
                    "content": "A final verdict cannot be submitted while checklist items remain unchecked. Inspect evidence and submit the first unchecked item verdict next.",
                })
                continue
            logger.warning("[Evaluator step %s] no 'tool' or 'verdicts' in parsed JSON", step)
            messages.append({"role": "user", "content": _nudge_no_tool()})
            continue

        verdict_only_stalls = 0

        if tool_name == "finish":
            if not ctx.all_checked():
                messages.append({
                    "role": "user",
                    "content": (
                        "finish was not accepted: checklist items remain unchecked. "
                        "Inspect direct evidence and submit remaining item verdicts incrementally."
                    ),
                })
                continue
            declared = tool_args.get("verdict", "") if isinstance(tool_args, dict) else ""
            verdict = _resolve_final_verdict(ctx, declared)
            logger.info("[Evaluator step %s] finish called, declared=%r verdict=%s (unchecked=%d)",
                        step, declared, verdict, ctx.unchecked_count())
            dialog_fh.close()
            return _finalize_report(ctx, verdict, step + 1)

        if tool_name not in _tool_names(registry):
            messages.append({"role": "user", "content": f"Unknown tool '{tool_name}'. {_TOOL_CATALOG}"})
            continue

        if tool_name == "generate_verification_checklist" and ctx.checklist:
            logger.warning(
                "[Evaluator step %s] duplicate checklist generation suppressed; checklist already loaded",
                step,
            )
            messages.append({
                "role": "user",
                "content": (
                    "The verification checklist has already been generated and loaded. Do not call "
                    "generate_verification_checklist again. Continue with the first unchecked item: "
                    "use the exact image paths in the Vision Observation Journal, submit a direct "
                    "passed/failed verdict when supported, or leave the item unchecked if evidence "
                    "remains insufficient."
                ),
            })
            continue

        logger.info("[Evaluator step %s] dispatching tool: %s args=%s", step, tool_name, tool_args)

        # ── Detect repeated identical calls (Fix 2a: counter, no reset) ──
        call_key = f"{tool_name}:{json.dumps(tool_args, ensure_ascii=False, sort_keys=True)}"
        if call_key == last_tool_key:
            repeat_count += 1
        else:
            repeat_count = 0
            last_tool_result_str = ""
        last_tool_key = call_key

        # A visual query remains duplicate even when another tool call was
        # inserted between two attempts.  The image path + keyword pair is the
        # evidence identity; reissuing it cannot create a new observation.
        visual_query_key = ""
        duplicate_visual_query = False
        if tool_name == "inspect_image_region":
            visual_path = str(tool_args.get("image_path") or "").strip()
            visual_handle = visual_path or f"global_step:{tool_args.get('global_step', '')}"
            visual_query_key = (
                f"{_normalized_evidence_path(visual_handle)}|"
                f"{str(tool_args.get('target_keyword') or '').strip()}"
            )
            duplicate_visual_query = visual_query_key in inspected_visual_queries

        if repeat_count >= 3 and not duplicate_visual_query:
            logger.warning(
                "[Evaluator step %s] FORCE STOP: %d consecutive repeated calls to %s",
                step, repeat_count, tool_name,
            )
            _leave_unchecked_incomplete(ctx)
            dialog_fh.close()
            return _finalize_report(ctx, _compute_verdict(ctx), step + 1)

        if repeat_count >= 1 or duplicate_visual_query:
            logger.warning("[Evaluator step %s] REPEATED call detected: %s", step, call_key)
            # Reuse the exact earlier result without dispatching an identical read.
            # The model gets a recovery turn with explicit call/output history.
            result_str = (
                "This visual image_path plus target_keyword was already inspected earlier; "
                "the duplicate call was suppressed and produced no new evidence."
                if duplicate_visual_query else last_tool_result_str
            )
            unchecked = next((item for item in ctx.checklist if not item.get("checked")), None)
            unchecked_hint = (
                f"First unchecked item: {json.dumps(unchecked, ensure_ascii=False, default=str)}. "
                if unchecked else ""
            )
            if (tool_name == "fetch_step_detail"
                    and tool_args.get("global_step") == "final"
                    and tool_args.get("detail_type") == "screenshot_after"):
                repeat_guidance = (
                    "You already fetched the final screenshot. Do not fetch it again. "
                    "Use inspect_image_region(image_path=<the exact returned screenshot path>, "
                    "target_keyword=<checklist item to verify>) to inspect that standalone image. "
                    "The returned file path, not global_step=final, is the only valid evidence_ref. "
                    "If that image has already been inspected for this keyword, do not inspect it again: "
                    "try a different before/after/overlay path or leave the item unchecked."
                )
            elif tool_name == "inspect_image_region":
                recovery_candidates = _duplicate_visual_recovery_candidates(
                    ctx,
                    blocked_path=str(tool_args.get("image_path") or ""),
                    target_keyword=str(tool_args.get("target_keyword") or ""),
                    inspected_visual_queries=inspected_visual_queries,
                )
                candidate_hint = ""
                if recovery_candidates:
                    candidate_lines = [
                        f"  {index}. {candidate['path']} ({candidate['role']}, global_step={candidate['global_step']})"
                        for index, candidate in enumerate(recovery_candidates, 1)
                    ]
                    candidate_hint = (
                        "\nCandidate image paths not yet queried with this keyword:\n"
                        + "\n".join(candidate_lines)
                        + "\nIf the registered summary already establishes the item, submit its verdict now; "
                        "otherwise choose exactly one candidate above.\n"
                    )
                repeat_guidance = (
                    "The exact image_path plus target_keyword has already been inspected and produced no "
                    "new evidence. Do not call the same pair again. First determine whether the existing "
                    "Vision Observation Journal or precomputed visual summary already directly establishes "
                    "the first unchecked item; if so, submit the verdict immediately. Otherwise use one of "
                    "the explicitly listed candidate paths below, or a relevant UI-tree source. If no new "
                    "source can resolve the item, leave it unchecked and continue; do not force a passed "
                    "verdict. The evidence_ref for a passed item must be the exact file path that was "
                    "inspected, never global_step or a step label."
                    + candidate_hint
                )
            elif tool_name == "query_ui_tree_diff":
                repeat_guidance = (
                    "This keyword query already returned its available before/after UI-tree matches. "
                    "Do not repeat the same query or use a title keyword to test playback controls. "
                    "A custom video player may omit visible text from its UI tree. Inspect the relevant "
                    "before/after screenshots instead, then submit the first unchecked checklist verdict. "
                    "Missing text in one UI tree does not by itself prove a checklist item false."
                )
            else:
                repeat_guidance = (
                    "Instead of retrying the same call, use a different evidence source. Prefer an exact "
                    "file path from the Vision Observation Journal; use global_step only to locate missing "
                    "details. If stuck, judge only items supported by evidence and leave unresolved items unchecked."
                )
            messages.append({
                "role": "user",
                "content": (
                    f"[Repeated call suppressed: {call_key}]\n"
                    f"Previous tool output (unchanged):\n{result_str}\n\n"
                    "This exact call has already been executed; no new evidence was obtained. "
                    + unchecked_hint + repeat_guidance
                ),
            })
            if duplicate_visual_query:
                # This call was suppressed as a session-level duplicate, not a
                # new tool execution. Do not let repeated model insistence on
                # the blocked query trigger the consecutive-call kill switch.
                repeat_count = 0
                last_tool_key = ""
            continue

        result = registry.dispatch(tool_name, tool_args, ctx)
        if (tool_name == "generate_verification_checklist"
                and result.get("error")):
            dialog_fh.close()
            raise ValueError("checklist_generation_failed: " + str(result["error"]))

        if tool_name == "inspect_image_region" and not result.get("error") and visual_query_key:
            inspected_visual_queries.add(visual_query_key)

        result_for_llm = _sanitize_for_history(result)
        result_str = json.dumps(result_for_llm, ensure_ascii=False, default=str)
        last_tool_result_str = result_str
        logger.warning(
            "[Evaluator step %s] tool result: %s safe_chars=%s",
            step, tool_name, len(result_str),
        )
        messages.append({"role": "user", "content": f"[Tool result: {tool_name}]\n{result_str}"})

        # ── Track evidence for verdict_basis ──────────────────────────────
        _track_evidence(ctx, tool_name, tool_args, result)
        if ctx.checklist:
            _save_checkpoint(ctx, reason=f"tool_{tool_name}")

        if tool_name == "generate_verification_checklist" and result.get("checklist"):
            ctx.checklist = result["checklist"]
            _save_checkpoint(ctx, reason="checklist_generated")
            logger.info("[Evaluator step %s] checklist generated: %s items", step, len(result["checklist"]))
            # Extract and persist every action-step triplet before the text
            # evaluator starts issuing checklist verdicts. Calls are bounded
            # and each request contains exactly one step's images.
            from runtime_events import emit
            emit("stage", stage="visual_evidence")
            _collect_missing_vision_observations(ctx)
            _register_precomputed_visual_evidence(ctx)
            emit("stage", stage="checklist_verdicts")

    dialog_fh.close()
    _leave_unchecked_incomplete(ctx)
    return _finalize_report(ctx, _compute_verdict(ctx), max_steps)


# ── Response parser ──────────────────────────────────────────────────────────

def _parse_response(text: str) -> dict | None:
    """Extract JSON action block from LLM text response.

    Handles: nested braces, code fences, and ASCII double-quotes used as
    Chinese quotation marks inside string values (e.g. "12案：xx").
    """
    # Try the fenced block first. Capture its body without requiring balanced
    # braces so we can recover the common single-missing-final-brace case.
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        candidate = m.group(1)
        result = _safe_json_loads(candidate)
        if result is not None:
            return result
        repaired = _repair_one_missing_closing_brace(candidate)
        if repaired is not None:
            logger.warning("[Evaluator parser] repaired one missing final JSON object brace")
            return repaired

    # Try depth-based extraction of outermost {...}
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                result = _safe_json_loads(text[start:i + 1])
                if result is not None:
                    return result
                start = -1  # retry next {...} block
    return None


def _repair_one_missing_closing_brace(candidate: str) -> dict | None:
    """Repair one omitted final object brace only when strict JSON validates afterward."""
    stripped = candidate.rstrip()
    if not stripped.startswith("{") or stripped.endswith("}"):
        return None
    repaired = _safe_json_loads(stripped + "}")
    if not isinstance(repaired, dict):
        return None
    if not any(key in repaired for key in ("tool", "verdicts", "final_verdict")):
        return None
    return repaired


def _safe_json_loads(text: str) -> dict | None:
    """json.loads with automatic repair of ASCII quotes used as Chinese punctuation.

    LLMs often write "书名号" or "集数" inside JSON strings, using ASCII U+0022
    which breaks JSON parsing. This function detects and replaces those with 「」."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = _repair_chinese_quotes(text)
        if fixed != text:
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass
    return None


def _repair_chinese_quotes(json_str: str) -> str:
    """State machine: inside a JSON string value, replace "" pairs with 「」.

    A " inside a string is content (Chinese quotation) if the character
    immediately before it is CJK/punctuation, not : or , or whitespace.
    """
    result: list[str] = []
    in_string = False
    escape_next = False
    quote_count = 0  # tracks 「/」 alternation within a string value

    for i, ch in enumerate(json_str):
        if escape_next:
            escape_next = False
            result.append(ch)
            continue
        if ch == '\\':
            escape_next = True
            result.append(ch)
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                quote_count = 0
                result.append(ch)
            else:
                # Inside a string: is this a structural close or a content quote?
                # Look behind (previous char in result) and ahead
                prev = result[-1] if result else ''
                ahead = json_str[i + 1] if i + 1 < len(json_str) else ''

                # Structural close: previous char is not \ and next token is : , } ] or whitespace
                if ahead in (':', ',', '}', ']', '\n', '\r', ' ') or (ahead == ''):
                    in_string = False
                    result.append(ch)
                elif prev and _is_cjk_or_punct(prev) and ahead and _is_cjk_or_punct(ahead):
                    # Content quote: surrounded by CJK/punctuation → use Chinese quotes
                    if quote_count % 2 == 0:
                        result.append('「')
                    else:
                        result.append('」')
                    quote_count += 1
                elif prev == ' ' and ahead and _is_cjk_or_punct(ahead):
                    # Opening quote after space: 「
                    if quote_count % 2 == 0:
                        result.append('「')
                        quote_count += 1
                    else:
                        result.append('」')
                        quote_count += 1
                else:
                    # Probably structural
                    in_string = False
                    result.append(ch)
        else:
            result.append(ch)

    return ''.join(result)


def _is_cjk_or_punct(ch: str) -> bool:
    """CJK characters, Chinese punctuation, digits, and letters — any
    character that can appear inside a Chinese quoted phrase like "12案"."""
    cp = ord(ch)
    return (0x4E00 <= cp <= 0x9FFF or 0x3000 <= cp <= 0x303F or
            0xFF00 <= cp <= 0xFFEF or 0x2000 <= cp <= 0x206F or
            ch in '。，、；：！？）】》…—·～' or
            ch.isdigit() or ch.isalpha())


def _check_item_complete(item: dict) -> bool:
    return bool(item.get("checked") and item.get("result") in {"passed", "failed"})


def _resume_source(ctx: EvaluatorContext, resume_from: str, resume_auto: bool) -> Path | None:
    if resume_from:
        source = Path(resume_from)
        if not source.is_file():
            raise FileNotFoundError(f"resume source not found: {source}")
        return source
    if not resume_auto:
        return None
    checkpoint = Path(ctx._checkpoint_path)
    if checkpoint.is_file():
        checkpoint_data = read_report(checkpoint) or {}
        if isinstance(checkpoint_data.get("checklist_items"), list) and checkpoint_data["checklist_items"]:
            return checkpoint
    # Isolated output directories must never consume another run's report.
    case_dir = checkpoint.parent
    report_path, _ = latest_report(case_dir, ctx._trajectory_path)
    if report_path:
        report = read_report(report_path) or {}
        complete, _ = report_checklist_state(report)
        if not complete:
            return report_path
    return None


def _load_resume_state(ctx: EvaluatorContext, trajectory_path: str,
                       resume_from: str = "", resume_auto: bool = False) -> bool:
    source = _resume_source(ctx, resume_from, resume_auto)
    if source is None:
        return False
    payload = read_report(source)
    if payload is None:
        raise ValueError(f"resume source is not a readable JSON object: {source}")
    from frozen_checklist import validate_resume as validate_frozen_resume
    validate_frozen_resume(payload)
    is_checkpoint = payload.get("schema_version") == "evaluator.resume.v1"
    if payload.get("session_id") not in {None, "", ctx.session_id}:
        raise ValueError("resume source session_id does not match the trajectory")
    if str(payload.get("trajectory_sha256") or "") != sha256_file(trajectory_path):
        raise ValueError("resume source trajectory_sha256 does not match the current trajectory")
    policy = getattr(ctx, "_checklist_policy", "auto")
    if payload.get("checklist_policy", "auto") != policy:
        raise ValueError("resume source checklist policy does not match")
    for identity_key, expected in current_identity().items():
        if payload.get(identity_key) != expected:
            raise ValueError(f"resume source {identity_key} does not match")
    current_digest = str(getattr(config, "EVALUATOR_CONFIG_DIGEST", ""))
    if current_digest and payload.get("evaluator_config_digest") != current_digest:
        raise ValueError("resume source evaluator endpoint/config digest does not match")
    key_elements_digest = hashlib.sha256(ctx.key_elements.encode("utf-8")).hexdigest()
    if (policy == "llm" or current_identity()) and payload.get("key_elements_sha256") != key_elements_digest:
        raise ValueError("resume source key elements do not match")
    if policy == "llm" and payload.get("checklist_source") != "llm":
        raise ValueError("resume source is not an LLM-generated checklist")
    saved_models = payload.get("model_config") if isinstance(payload.get("model_config"), dict) else {}
    current_models = {
        "text_model": getattr(config, "EVALUATOR_TEXT_MODEL", ""),
        "vision_model": getattr(config, "EVALUATOR_VISION_MODEL", ""),
    }
    for model_key, current_model in current_models.items():
        saved_model = str(saved_models.get(model_key) or "")
        if saved_model and current_model and saved_model != str(current_model):
            raise ValueError(f"resume source {model_key}={saved_model!r} differs from current configuration {current_model!r}")
    if not is_checkpoint:
        complete, _reason = report_checklist_state(payload)
        if complete:
            raise ValueError("resume source is already complete; use a fresh evaluation if reevaluation is intended")
        if payload.get("evaluation_status") not in {None, "", "incomplete"}:
            raise ValueError("resume source is not marked incomplete")

    checklist = payload.get("checklist_items") if is_checkpoint else None
    if not isinstance(checklist, list):
        source_name = str(payload.get("checklist_source") or "")
        if not source_name.startswith("gold"):
            raise ValueError("legacy non-gold reports lack the full checklist needed for safe resume")
        gold = load_gold_checklist(str(ctx.trajectory.get("task") or ""), ctx.trajectory)
        if not gold:
            raise ValueError("current gold checklist could not be resolved")
        checklist = gold["checklist"]
        gold_path = Path(str(gold.get("source_path") or ""))
        current_checklist_hash = sha256_file(io_path(gold_path)) if io_path(gold_path).is_file() else ""
        if not current_checklist_hash or current_checklist_hash != str(payload.get("checklist_sha256") or ""):
            raise ValueError("resume source checklist_sha256 does not match the current gold checklist")
        saved_items = ((payload.get("checklist_summary") or {}).get("items") or [])
        if len(saved_items) != len(checklist):
            raise ValueError("resume source checklist length does not match the current gold checklist")
        for current, saved in zip(checklist, saved_items):
            if str(current.get("id")) != str(saved.get("id")) or str(current.get("description")) != str(saved.get("description")):
                raise ValueError("resume source checklist order or description differs from the current gold checklist")
            status = str(saved.get("status") or "unchecked").lower()
            current["checked"] = status in {"passed", "failed", "pass", "fail"}
            current["result"] = "passed" if status in {"passed", "pass"} else "failed" if status in {"failed", "fail"} else None
            basis = ((payload.get("failure_analysis") or {}).get("verdict_basis") or {}).get(str(current.get("id"))) or {}
            current["evidence"] = str(basis.get("evidence") or "")
        ctx.checklist_source = str(payload.get("checklist_source") or "gold")
        ctx.checklist_metadata = gold
    else:
        checklist = json.loads(json.dumps(checklist, ensure_ascii=False))
        ctx.checklist_source = str(payload.get("checklist_source") or "")
        ctx.checklist_metadata = payload.get("checklist_metadata") if isinstance(payload.get("checklist_metadata"), dict) else {}
        gold_path = Path(str(ctx.checklist_metadata.get("source_path") or ""))
        if ctx.checklist_source.startswith("gold"):
            current_checklist_hash = sha256_file(io_path(gold_path)) if io_path(gold_path).is_file() else ""
            if not current_checklist_hash or current_checklist_hash != str(payload.get("checklist_sha256") or ""):
                raise ValueError("checkpoint checklist_sha256 does not match the current gold checklist")

    if not checklist or any(not isinstance(item, dict) or not item.get("id") for item in checklist):
        raise ValueError("resume source has an invalid checklist")
    item_ids = [str(item.get("id")) for item in checklist]
    if len(set(item_ids)) != len(item_ids):
        raise ValueError("resume source checklist contains duplicate item ids")
    for item in checklist:
        if item.get("checked") and item.get("result") not in {"passed", "failed"}:
            raise ValueError(f"resume source has an invalid checked result for {item.get('id')}")
    basis_rows = payload.get("verdict_basis") if is_checkpoint else ((payload.get("failure_analysis") or {}).get("verdict_basis") or {})
    if isinstance(basis_rows, dict):
        for basis in basis_rows.values():
            if not isinstance(basis, dict):
                continue
            for source_path in [*(basis.get("screenshot_paths") or []), *(basis.get("ui_tree_paths") or [])]:
                if ctx.trajectory.get("_strict_artifacts"):
                    candidate = Path(resolve_artifact(ctx.trajectory, source_path))
                else:
                    candidate = Path(str(source_path))
                if not candidate.is_absolute():
                    candidate = Path(config.PROJECT_ROOT) / candidate
                if not candidate.is_file():
                    raise ValueError(f"resume source evidence file is missing: {source_path}")
    ctx.checklist = checklist
    failure_analysis = payload.get("failure_analysis") if not is_checkpoint else {}
    ctx._verdict_basis = json.loads(json.dumps(
        payload.get("verdict_basis") if is_checkpoint else (failure_analysis or {}).get("verdict_basis", {}),
        ensure_ascii=False,
    ))
    if is_checkpoint:
        ctx._check_audit = payload.get("check_audit") if isinstance(payload.get("check_audit"), dict) else {}
        ctx._evidence_log = payload.get("evidence_log") if isinstance(payload.get("evidence_log"), list) else []
        ctx._revisions = payload.get("revisions") if isinstance(payload.get("revisions"), list) else []
    else:
        ctx._check_audit = payload.get("check_audit") if isinstance(payload.get("check_audit"), dict) else {}
        ctx._evidence_log = (failure_analysis or {}).get("evidence_log", [])
        ctx._revisions = (failure_analysis or {}).get("revisions", [])
    if ctx._evaluation_mode == "checklist":
        prior_audit = ctx._check_audit
        ctx._check_audit = {
            "schema_version": "evaluator.check_audit.v1",
            "status": "skipped_checklist_mode",
            "reason": "default evaluator mode checks the checklist only",
            "prior_report_audit": prior_audit,
            "prior_report_path": str(source.resolve()) if not is_checkpoint else payload.get("resumed_from"),
        }
    previous_resume = payload.get("resume_metadata") if is_checkpoint and isinstance(payload.get("resume_metadata"), dict) else {}
    ctx._resume_metadata = {
        "resumed_from": previous_resume.get("resumed_from") or str(source.resolve()),
        "attempt": int(payload.get("attempt") or 0) + 1,
        "retained_item_ids": [str(item.get("id")) for item in checklist if item.get("checked")],
        "newly_checked_item_ids": list(previous_resume.get("newly_checked_item_ids") or []),
        "rechecked_item_ids": list(previous_resume.get("rechecked_item_ids") or []),
    }
    _save_checkpoint(ctx, reason="resume_started")
    return True


def _resume_context(ctx: EvaluatorContext) -> str:
    first_unchecked = next((i for i, item in enumerate(ctx.checklist) if not _check_item_complete(item)), len(ctx.checklist))
    rows = [{
        "index": index,
        "id": item.get("id"),
        "description": item.get("description"),
        "status": item.get("result") if _check_item_complete(item) else "unchecked",
        "evidence": item.get("evidence", "") if _check_item_complete(item) else "",
    } for index, item in enumerate(ctx.checklist)]
    return (
        "续评状态：checklist 已从同一轨迹的有效检查点恢复。按原顺序处理；"
        f"当前第一项未检查条目索引为 {first_unchecked}（从 0 开始）。"
        "只能先完成该项，不能越过它判定后续未检查项。已完成项如下，"
        "如需修订必须重新检查对应原始证据。\n"
        + json.dumps(rows, ensure_ascii=False, indent=2)
    )


def _save_checkpoint(ctx: EvaluatorContext, reason: str = "") -> None:
    path_value = str(getattr(ctx, "_checkpoint_path", "") or "")
    if not path_value:
        return
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        trajectory_hash = sha256_file(ctx._trajectory_path)
    except (OSError, AttributeError):
        trajectory_hash = ""
    checklist_path = Path(str(ctx.checklist_metadata.get("source_path") or ""))
    checklist_hash = sha256_file(checklist_path) if checklist_path.is_file() else ""
    metadata = getattr(ctx, "_resume_metadata", {}) or {}
    payload = {
        "schema_version": "evaluator.resume.v1",
        **current_identity(),
        "session_id": ctx.session_id,
        "trajectory_path": str(getattr(ctx, "_trajectory_path", "") or ""),
        "trajectory_sha256": trajectory_hash,
        "checklist_source": ctx.checklist_source,
        "checklist_policy": getattr(ctx, "_checklist_policy", "auto"),
        "evaluator_config_digest": str(getattr(config, "EVALUATOR_CONFIG_DIGEST", "")),
        "key_elements_sha256": hashlib.sha256(ctx.key_elements.encode("utf-8")).hexdigest(),
        "checklist_metadata": ctx.checklist_metadata,
        "checklist_sha256": checklist_hash,
        "checklist_items": ctx.checklist,
        "verdict_basis": ctx._verdict_basis,
        "evidence_log": ctx._evidence_log,
        "revisions": ctx._revisions,
        "check_audit": ctx._check_audit,
        "first_unchecked_index": next((i for i, item in enumerate(ctx.checklist) if not _check_item_complete(item)), len(ctx.checklist)),
        "attempt": int(metadata.get("attempt") or 1),
        "resumed_from": metadata.get("resumed_from"),
        "resume_metadata": metadata,
        "model_config": {
            "text_model": getattr(config, "EVALUATOR_TEXT_MODEL", ""),
            "vision_model": getattr(config, "EVALUATOR_VISION_MODEL", ""),
            "check_model": _CHECK_EVALUATOR_MODEL,
        },
        "status": "complete" if ctx.all_checked() else "incomplete",
        "reason": reason or None,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _normalized_evidence_path(value: object) -> str:
    """Return a comparable canonical file path without weakening path identity."""
    raw = str(value or "").strip().strip("`\"")
    if not raw:
        return ""
    try:
        return os.path.normcase(os.path.normpath(str(Path(raw).resolve(strict=False))))
    except (OSError, ValueError, TypeError):
        return os.path.normcase(os.path.normpath(raw))


def _register_precomputed_visual_evidence(ctx: EvaluatorContext) -> None:
    """Register successful step summaries as already-inspected image evidence.

    The visual summary VLM has already received the listed image files.  The text
    evaluator may reason over its objective descriptions, but a passed verdict
    still has to cite one of the exact registered file paths.
    """
    existing = {
        (_normalized_evidence_path(entry.get("path")), entry.get("type"))
        for entry in ctx._evidence_session
        if entry.get("path")
    }
    for global_step, observations in sorted(ctx._vision_observations.items()):
        if not isinstance(global_step, int) or global_step <= 0:
            continue
        for observation in observations or []:
            status = str(observation.get("summary_status") or observation.get("status") or "complete").lower()
            if status not in {"complete", "partial"}:
                continue
            for role in ("before_path", "overlay_path", "after_path"):
                path = str(observation.get(role) or "").strip()
                if not path or not Path(path).is_file():
                    continue
                key = (_normalized_evidence_path(path), "screenshot_inspected")
                if not key[0] or key in existing:
                    continue
                ctx._evidence_session.append({
                    "global_step": global_step,
                    "type": "screenshot_inspected",
                    "path": path,
                    "source": "precomputed_visual_summary",
                    "image_role": role.removesuffix("_path"),
                })
                observation["evidence_registered"] = True
                existing.add(key)


def _duplicate_visual_recovery_candidates(
    ctx: EvaluatorContext,
    *,
    blocked_path: str,
    target_keyword: str,
    inspected_visual_queries: set[str],
) -> list[dict[str, object]]:
    """Return exact journal image paths that can replace a blocked visual query.

    This is intentionally deterministic.  A duplicate request should not leave
    the model to rediscover the next source from a very large journal; the
    runtime gives it a short, ordered list of paths that have not yet been
    queried with the same keyword.
    """
    blocked_key = _normalized_evidence_path(blocked_path)
    keyword = str(target_keyword or "").strip()
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()

    def add(path: object, role: str, global_step: object) -> None:
        raw = str(path or "").strip()
        normalized = _normalized_evidence_path(raw)
        if not normalized or normalized == blocked_key or normalized in seen:
            return
        if not Path(raw).is_file():
            return
        query_key = f"{normalized}|{keyword}"
        if query_key in inspected_visual_queries:
            return
        seen.add(normalized)
        candidates.append({
            "path": raw,
            "role": role,
            "global_step": int(global_step) if isinstance(global_step, int) else global_step,
        })

    # Prefer the final action's after image, then the related overlay/before
    # images, followed by earlier action triplets in reverse chronological
    # order.  This is evidence search, not a pass/fail decision.
    for global_step in sorted(ctx._vision_observations, reverse=True):
        observations = ctx._vision_observations.get(global_step) or []
        for observation in observations:
            for role in ("after_path", "overlay_path", "before_path"):
                add(observation.get(role), role.removesuffix("_path"), global_step)

    add(ctx.trajectory.get("final_orch_screenshot"), "final_orch_screenshot", "final")
    return candidates[:6]


def _evidence_global_step(ctx: EvaluatorContext, source: str) -> int:
    """Resolve a verdict's exact evidence path back to its action ordinal."""
    source_key = _normalized_evidence_path(source)
    if not source_key:
        return 0
    for entry in reversed(ctx._evidence_session):
        if _normalized_evidence_path(entry.get("path")) == source_key:
            value = entry.get("global_step", 0)
            return int(value) if isinstance(value, int) else 0
    return 0


def _apply_verdicts(verdicts: list[dict], ctx: EvaluatorContext) -> tuple[int, int]:
    """Apply verdicts; count only first decisions and result revisions as progress."""
    updated = 0
    invalid = 0
    checkpoint_changed = False
    # Determine most recent global_step from evidence session
    last_gs = 0
    if ctx._evidence_session:
        last_gs = ctx._evidence_session[-1].get("global_step", 0)

    item_positions = {str(item.get("id") or ""): index for index, item in enumerate(ctx.checklist)}
    submitted_ids = {str(v.get("item_id") or "") for v in verdicts if isinstance(v, dict)}
    for v in verdicts:
        item_id = v.get("item_id", "")
        result = v.get("result", "")
        evidence = v.get("evidence", "")
        if result in ("passed", "failed"):
            position = item_positions.get(str(item_id))
            if position is None:
                invalid += 1
                logger.warning("[Evaluator] unknown checklist item: %s", item_id)
                continue
            first_unchecked = next((i for i, row in enumerate(ctx.checklist) if not _check_item_complete(row)), len(ctx.checklist))
            if position > first_unchecked:
                invalid += 1
                logger.warning("[Evaluator] out-of-order verdict rejected: %s; first unchecked index=%s", item_id, first_unchecked)
                continue
            source = ""
            if result == "passed":
                source = str(v.get("evidence_ref") or "").strip()
                observed = str(evidence or "").strip()
                inspected = [str(entry.get("path") or "") for entry in ctx._evidence_session
                             if entry.get("type") in {"screenshot_inspected", "ui_tree"}]
                if not source or not observed or not any(
                    _normalized_evidence_path(source) == _normalized_evidence_path(path)
                    for path in inspected if path
                ):
                    invalid += 1
                    logger.warning("[Evaluator] passed verdict lacks an inspected evidence_ref: %s", item_id)
                    continue
            result, evidence = _enforce_gold_not_count(item_id, result, evidence, ctx)
            if result == "passed" and source:
                evidence = f"{evidence} [evidence_ref: {source}]"
            old = next((item for item in ctx.checklist if item.get("id") == item_id), None)
            was_checked = bool(old.get("checked"))
            changed_result = not was_checked or old.get("result") != result
            changed_evidence = old.get("evidence") != evidence
            if not changed_result and old.get("evidence") == evidence:
                logger.info("[Evaluator] unchanged verdict ignored: %s = %s", item_id, result)
                continue
            evidence_step = _evidence_global_step(ctx, source) if source else last_gs
            ctx.mark_checklist_item(item_id, result, evidence, global_step=evidence_step)
            checkpoint_changed = checkpoint_changed or changed_result or changed_evidence
            if changed_result:
                updated += 1
                logger.warning("[Evaluator] verdict new/revised: %s = %s", item_id, result)
                metadata = getattr(ctx, "_resume_metadata", {})
                destination = "rechecked_item_ids" if was_checked else "newly_checked_item_ids"
                if item_id not in metadata.setdefault(destination, []):
                    metadata[destination].append(item_id)
            else:
                logger.info("[Evaluator] evidence updated without result change: %s", item_id)
            if result == "failed":
                for later in ctx.checklist[position + 1:]:
                    later_id = str(later.get("id") or "")
                    if later.get("checked") and later_id not in submitted_ids:
                        later["checked"] = False
                        later["result"] = None
                        later["evidence"] = None
                        ctx._verdict_basis.pop(later_id, None)
                        ctx._resume_metadata.setdefault("rechecked_item_ids", []).append(later_id)
                        checkpoint_changed = True
        else:
            invalid += 1
            logger.warning("[Evaluator] invalid verdict result: %s = %s (expected passed/failed)", item_id, result)
    if checkpoint_changed:
        _save_checkpoint(ctx, reason="checklist_progress")
    return updated, invalid


def _enforce_gold_not_count(item_id: str, result: str, evidence: str,
                            ctx: EvaluatorContext) -> tuple[str, str]:
    """Turn an explicit gold forbidden-state observation into a failed item.

    ``not_count`` is not merely advice to the evaluator model. Models can
    correctly describe a forbidden state and then still pass the item because
    the broad terminal goal appears satisfied. When the model's own evidence
    positively contains a forbidden-state concept, the structured gold
    constraint wins. Ordinary tasks without ``not_count`` are untouched.
    """
    if result != "passed":
        return result, evidence

    item = next((candidate for candidate in ctx.checklist
                 if candidate.get("id") == item_id), None)
    if not item or not item.get("not_count") or not evidence:
        return result, evidence

    evidence_text = _compact_gold_text(evidence)
    for forbidden in item.get("not_count", []):
        concept = _gold_forbidden_concept(str(forbidden))
        if not concept or not _positive_concept_in_text(evidence_text, concept):
            continue
        reason = f"gold not_count matched: {forbidden}"
        logger.warning("[Evaluator] gold constraint override: item=%s %s", item_id, reason)
        return "failed", f"{evidence}；{reason}"
    return result, evidence


def _compact_gold_text(text: str) -> str:
    return "".join(ch.lower() for ch in str(text)
                    if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _gold_forbidden_concept(forbidden: str) -> tuple[str, ...]:
    """Map a human phrase to distinctive evidence concepts.

    This is deliberately a small, transparent rule layer rather than a
    semantic classifier. It makes explicitly named forbidden states
    enforceable when the evaluator has already mentioned them in evidence.
    """
    text = _compact_gold_text(forbidden)
    if "charts" in text and "聚合页" in text:
        return ("charts", "聚合页")
    if "预览列表" in text:
        return ("预览列表",)
    if "卡片" in text and "飙升榜" in text:
        return ("飙升榜", "卡片")
    if "多个榜单" in text:
        return ("多个榜单",)
    if "飙升榜详情" in text and ("没有进入" in text or "未进入" in text):
        return ("飙升榜详情",)
    return tuple()


def _positive_concept_in_text(evidence: str, concept: tuple[str, ...]) -> bool:
    """Check concept presence while ignoring a nearby negated mention."""
    if not concept or not all(part in evidence for part in concept):
        return False
    for part in concept:
        start = evidence.find(part)
        if start < 0:
            continue
        prefix = evidence[max(0, start - 8):start]
        if any(neg in prefix for neg in ("不是", "并非", "非", "没有", "未", "不属于")):
            return False
    return True


# ── Nudge helpers ────────────────────────────────────────────────────────────

def _nudge_unparseable(attempt: int = 1) -> str:
    return (
        f"Your previous response could not be parsed (format retry {attempt}/2). "
        "Return exactly ONE complete JSON block. Close every object and array before ending the block; "
        "do not repeat the malformed response. Format:\n"
        "- To call a tool: {\"reasoning\":\"...\",\"tool\":\"tool_name\",\"args\":{...}}\n"
        "- To submit evaluations: {\"reasoning\":\"...\",\"verdicts\":[{\"item_id\":\"chk_01\",\"result\":\"passed\",\"evidence\":\"...\"}]}\n"
        "- When all done: add \"final_verdict\":\"success\" to the verdicts block.\n"
        + _TOOL_CATALOG
    )


def _nudge_no_tool() -> str:
    return (
        "Your JSON must include either 'tool' (to call a tool) or 'verdicts' (to submit evaluations). "
        "Example: {\"reasoning\":\"I need to see the final screen\",\"tool\":\"fetch_step_detail\",\"args\":{...}}"
    )


# ── Context / prompt builders ────────────────────────────────────────────────

def _tool_names(registry: EvaluatorToolRegistry) -> list[str]:
    return sorted(registry._tools.keys())


def _leave_unchecked_incomplete(ctx: EvaluatorContext) -> None:
    """Budget exhaustion is missing verification, not an observed failure."""
    for item in ctx.checklist:
        if not item.get("checked"):
            item["checked"] = False
            item["result"] = "unchecked"
            item["evidence"] = "verification incomplete; no verdict was reached"


def _sanitize_for_history(obj: object) -> object:
    if isinstance(obj, dict):
        return {k: ("<stripped>" if k in ("b64", "content", "base64") else _sanitize_for_history(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_history(i) for i in obj]
    if isinstance(obj, str) and len(obj) > 300 and obj.strip().startswith("<"):
        return "<XML content truncated — use processed UI tree instead>"
    return obj


def _load_evaluation_rules_text() -> str:
    """Load special-scenario evaluation rules from evaluator_skill JSON files."""
    import config as _cfg
    configured = os.environ.get("GUI_AGENT_EVALUATOR_RULES_DIR", "")
    skill_dir = Path(configured) if configured else _cfg.PROJECT_ROOT / "agent" / "evaluator" / "evaluator_skill"
    if os.environ.get("GUI_AGENT_EVALUATOR_STRICT_RULES") == "true":
        return "\n\n".join(rule["prompt_text"] for rule in load_rules(skill_dir)
                           if rule["id"] not in _EXCLUDED_EVALUATION_RULE_IDS)
    if not io_path(skill_dir).is_dir():
        return ""
    parts: list[str] = []
    for f in sorted(io_path(skill_dir).glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for rule in data.get("rules", []):
                if str(rule.get("id", "")).strip() in _EXCLUDED_EVALUATION_RULE_IDS:
                    continue
                text = str(rule.get("prompt_text", "")).strip()
                if text:
                    parts.append(text)
        except Exception:
            pass
    return "\n\n".join(parts)


def _build_system_prompt() -> str:
    return (
        "You are a GUI task evaluator. Determine whether a recorded trajectory "
        "completed the user's task.\n\n"
        "## Output format (CRITICAL — always output exactly ONE JSON block)\n\n"
        "To call a tool:\n"
        '```json\n{"reasoning": "<why>", "tool": "<name>", "args": {<params>}}\n```\n\n'
        "To report evaluation results (after gathering evidence):\n"
        '```json\n{\n'
        '  "reasoning": "<analysis summary>",\n'
        '  "verdicts": [\n'
        '    {"item_id": "chk_01", "result": "passed", "evidence": "<directly observed fact>", "evidence_ref": "<path returned by an inspected tool>"},\n'
        '    {"item_id": "chk_02", "result": "failed", "evidence": "<what you observed>"}\n'
        '  ]\n'
        '}\n```\n\n'
        "When ALL items are evaluated, add \"final_verdict\" to the block:\n"
        '```json\n{"reasoning":"...","verdicts":[...],"final_verdict":"partial_success"}\n```\n\n'
        "**Key rules:**\n"
        "- Output ONLY the JSON block. No markdown explanations before or after.\n"
        "- JSON must be valid: use double quotes, no trailing commas.\n"
        "- Submit only NEW checklist verdicts or evidence-based REVISIONS. Do not repeat an unchanged item.\n"
        "- If another item still needs evidence, request its tool directly. A tool request may accompany new verdicts; the evaluator will apply the verdicts and then run the tool.\n"
        "- reason字段用中文。\n\n"
        "## NON-NEGOTIABLE EVIDENCE GROUNDING — MUST OBEY FOR EVERY CHECKLIST VERDICT\n"
        "You MUST obey every rule in this section. These rules take precedence over "
        "plausibility, task intent, and a preference for producing a decisive verdict.\n"
        "- A passed item MUST be supported by a specific fact directly observable in an "
        "inspected screenshot, UI tree, or execution record. State the source and the "
        "exact observed fact in evidence.\n"
        "- It is FORBIDDEN to treat the task description, agent plan/action intent, "
        "model-generated explanations, semantic similarity, or your own inference as "
        "an observation that a checklist condition was completed.\n"
        "- It is FORBIDDEN to bridge an evidence gap by assuming two labels, pages, "
        "categories, or outcomes are equivalent. Do not turn 'not contradicted' into "
        "'confirmed'.\n"
        "- Apply an OBJECTIVE, BALANCED evidence standard: absence of a label or fact "
        "from one screenshot means only that this source does not show it; absence alone "
        "does not prove the condition false. Conversely, lack of visible contradiction "
        "does not prove the condition true. Use positive supporting facts and any "
        "positive contradicting facts in the available evidence.\n"
        "- If the required fact is not directly supported, inspect additional relevant "
        "evidence. If evidence remains insufficient or ambiguous, do NOT mark the item "
        "passed; preserve the uncertainty instead of guessing.\n"
        "- Clearly separate what each source visibly shows from any interpretation. "
        "Later actions cannot retroactively establish an unverified prerequisite.\n"
        "- Every passed item MUST include evidence_ref equal to the exact file path of the "
        "screenshot or UI-tree source that was inspected. This can be a path returned by a "
        "tool or a path in the precomputed Vision Observation Journal that was registered as "
        "already inspected. Never use global_step, step#N, file stepNNN, or a basename as "
        "evidence_ref. A valid reference only proves that you inspected the source; it does "
        "NOT make an unsupported interpretation valid.\n\n"
        "## Evidence Path and Duplicate-Inspection Contract (NON-NEGOTIABLE)\n"
        "- global_step is only an internal execution-ordinal lookup key. It is NOT an evidence "
        "path and must never appear as evidence_ref.\n"
        "- Use the exact absolute file path shown in the Vision Observation Journal, tool output, "
        "or trajectory final screenshot field. For visual checks, prefer "
        "inspect_image_region(image_path=<exact file path>, target_keyword=<keyword>).\n"
        "- The same image_path plus the same target_keyword is one evidence query. Calling it "
        "again cannot create new evidence. If it is suppressed as a duplicate, do not retry it. "
        "Try a different before/after/overlay/final file path or a relevant UI-tree source.\n"
        "- If all relevant sources have been checked and no new information is available, leave "
        "the item unchecked or mark it failed only when there is direct contradictory evidence. "
        "Never mark an item passed merely to finish the checklist.\n"
        "- The precomputed visual summary is already an evidence extraction over its listed image "
        "paths. Treat those paths as checked evidence for factual reasoning, while still keeping "
        "the VLM description separate from your own pass/fail decision.\n\n"
        "## Workflow\n"
        "1. Call generate_verification_checklist first.\n"
        "2. Gather evidence: use the exact screenshot paths in the Vision Observation Journal "
        "directly when available; use fetch_step_detail only for missing UI tree/action/model "
        "details or to discover a path.\n"
        "   **Check BOTH screenshot_before and screenshot_after — before is equally important!**\n"
        "3. Inspect with inspect_image_region (visual description extraction) or query_ui_tree_diff (structural).\n"
        "4. **Incrementally update verdicts** — after EACH inspection, output verdicts for\n"
        "   every item you can now judge. Don't wait until all items are done.\n"
        "   Example: after checking one screenshot, output:\n"
        '   {"reasoning":"the inspected screenshot directly shows the required page","verdicts":[{"item_id":"chk_01","result":"passed","evidence":"visible page title and content","evidence_ref":"<inspected screenshot path>"}]}\n'
        "5. Verdicts can be revised later if new evidence contradicts earlier findings.\n"
        "6. If some items need different screenshots, load them and repeat steps 2-4.\n"
        "7. When ALL items have verdicts, add final_verdict.\n"
        "8. Before submitting final_verdict, use the FINAL orchestrator screenshot as the preferred "
        "settled-state cross-check when the harness provides one. The harness may expose it as an "
        "exact path, a Vision Observation Journal entry, or a precomputed visual summary. If the "
        "final screenshot is already registered as checked evidence, including an entry whose "
        "evidence_status is 'precomputed visual summary registered as checked image evidence', "
        "reuse that evidence and do NOT call inspect_image_region again. If an exact final path is "
        "available but has not been inspected or registered, inspect it once. If no final path or "
        "summary is available, record that limitation and use the best available direct evidence; "
        "do not repeatedly request an unavailable source or retry a suppressed query. The final-state "
        "cross-check is an evidence-availability preference, not an unconditional fresh visual-model "
        "invocation. Only when the path is unavailable and a final detail lookup is genuinely needed, call:\n"
        "   fetch_step_detail(global_step=\"final\", detail_type=\"screenshot_after\")\n"
        "   Then inspect that standalone image with inspect_image_region(image_path=<exact path returned by the tool>, "
        "target_keyword=<checklist item to verify>). For action-step images, also use the exact\n"
        "before/after/overlay image_path from the journal; use global_step only when no path is available.\n"
        "   The final screenshot shows the page state when the agent declared task "
        "completion — it may differ from the last action's screenshot because some pages "
        "need loading time (network requests, animations, etc.). The last action step "
        "screenshot may capture a transitional state; the final orchestrator screenshot "
        "captures the SETTLED end state that the agent considered \"done\".\n"
        "   - For ONE-WAY actions (booking, payment, order, comment, like, follow): "
        "the final screenshot is the AUTHORITATIVE source for verification.\n"
        "   - For REVERSIBLE actions (filters, settings, toggles): use the relevant "
        "action's screenshot as primary evidence, but STILL cross-check against the "
        "final screenshot for consistency.\n\n"
        "## Evidence Search Strategy (CRITICAL)\n"
        "- **Do NOT trust subtask names blindly** — they are hints, not facts.\n"
        "  When evidence is not found via the expected screenshots, use\n"
        "  fetch_step_detail(detail_type=\"list_all_screenshots\") to discover\n"
        "  ALL available screenshots on disk, including ones not in the trajectory.\n"
        "- **When fetch_step_detail returns an error** (subtask not found, empty path, etc.):\n"
        "  1. FIRST call fetch_step_detail(detail_type=\"list_all_screenshots\") — this\n"
        "     scans the case folder on disk and returns every available screenshot.\n"
        "  2. Screenshots on disk that are NOT in the trajectory ARE valid evidence!\n"
        "     The trajectory may only record a subset of actions; the disk has more.\n"
        "  3. After listing, use the returned exact file path with image_path whenever possible.\n"
        "- **Check BOTH screenshot_before AND screenshot_after** for every action.\n"
        "  Many key states (filter panels, search results) appear in BEFORE screenshots.\n"
        "- **Chronological order matters** — for tasks with dependencies\n"
        "  (open app → search → filter → play), verify in global_step order.\n"
        "  Earlier steps may contain evidence that later steps have navigated away from.\n"
        "- **Maintain an evidence journal** — after examining each screenshot,\n"
        "  note what you found and which checklist items are still pending.\n"
        "  The journal is a set of checked evidence, not merely a hint. Avoid re-examining the\n"
        "  same image_path plus keyword: it cannot produce new evidence. If it is insufficient,\n"
        "  switch to another listed image or UI tree; if none is useful, keep the item unchecked.\n"
        "- **UI tree is useful for text + widget types, NOT for selection state**:\n"
        "  For filter/toggle/selection items, load BOTH screenshots AND the processed\n"
        "  UI tree via fetch_step_detail(detail_type='ui_tree_after_extracted').\n"
        "  UI tree provides reliable evidence for: text content, widget types\n"
        "  (SeekBar=playing, EditText=input).\n"
        "  ⚠️ Android accessibility selected/checked/focused attributes are often\n"
        "  UNRELIABLE for apps with custom views (腾讯视频, 抖音, 微信 etc.).\n"
        "  When UI tree selected/checked conflicts with the screenshot, TRUST THE\n"
        "  SCREENSHOT. Use UI tree for text, screenshots for visual state.\n\n"
        "## Text/Vision responsibility boundary\n"
        "- The VLM is only an evidence collector. For an action it receives BEFORE and AFTER\n"
        "  screenshots and returns objective descriptions plus readable text; it never decides\n"
        "  found/missing, pass/fail, beneficial/neutral/harmful, or checklist results.\n"
        "- Every observation is appended to the persistent `Vision Observation Journal`, indexed\n"
        "  by step#N (the final screenshot is indexed separately as `final screenshot`).\n"
        "- You (the text evaluator) compare observations across steps, resolve contradictions,\n"
        "  determine whether they establish each checklist item, and write the final evidence.\n"
        "- Treat a VLM uncertainty as missing evidence, not as a positive result.\n\n"
        "## Step numbering (read before citing steps)\n"
        "- global_step is the execution ORDINAL of committed actions (1..N), used only to locate\n"
        "  an action record. It is never a substitute for an evidence file path.\n"
        "- A screenshot FILE may carry a different number than its action's ordinal\n"
        "  (e.g. the 3rd action's screenshots can be step004_vla_*.jpg) because\n"
        "  cancelled prefetch candidates consumed step indices that were never\n"
        "  executed. Tools label every screenshot step#<ordinal> (file stepNNN) —\n"
        "  use the exact full file path as evidence_ref so a human can locate the file.\n"
        "- Files listed as 'file stepNNN' with NO ordinal are cancelled-prefetch\n"
        "  debris / uncommitted — load them via image_path, NEVER via global_step, and\n"
        "  cite their exact path only if the file itself was inspected.\n"
        "- fetch_step_detail(global_step=N) always loads the action whose ordinal is N.\n\n"
        "## Evidence Rules from evaluator_skill\n"
        + _load_evaluation_rules_text() + "\n\n"
        "## Reversible vs One-Way Conditions\n"
        "**Reversible** (settings, toggles, filters, play modes, selections, switches):\n"
        "  → Check the LAST screenshot that shows the RELEVANT page, which may NOT\n"
        "    be the final action's screenshot. If filters were applied mid-process\n"
        "    and the final screenshot is a playback page, verify from the filter\n"
        "    action's screenshots (both before and after).\n"
        "  → When VLM cannot tell the icon state (e.g., loop mode, shuffle, mute):\n"
        "    first use detect_image_changes(action_before, action_after) to find\n"
        "    WHERE the screen changed. Then use inspect_image_region on the\n"
        "    annotated_image (with red boxes) to ask VLM: 'do the highlighted\n"
        "    regions show the expected state?'\n"
        "  → Do NOT fail a reversible condition just because the final screenshot\n"
        "    (which may be a playback/detail page) does not show the filter panel.\n"
        "**One-way** (open app, search, play, comment, add to playlist, send):\n"
        "  → Check the screenshot of the action that performed the operation.\n"
        "    If the final screenshot still reflects it, great. If not, verify from\n"
        "    the operation's own after-screenshot.\n\n"
        "⚠️ **Cross-check verdicts against the final screenshot when available**: after "
        "verifying against individual action screenshots, prefer the final orchestrator "
        "screenshot when the harness provides an exact path or a registered summary. "
        "If it is already registered as checked evidence, reuse it rather than issuing a "
        "new visual query. If the path is available but not yet checked, inspect it once; "
        "if it is unavailable, record that limitation and use the best available direct "
        "evidence. Do not repeatedly request a missing or suppressed source. The final screenshot captures the settled end "
        "state and may reveal UI updates that loaded after the last action "
        "(e.g. booking confirmation toast, payment success page), elements the "
        "agent missed, or post-action changes (ads dismissed, pages navigated "
        "away).\n\n"
        "**Action overlay** (red-marked tap position on screenshot):\n"
        "  → Use fetch_step_detail(detail_type=\"action_overlay\") to see exactly\n"
        "    WHERE the agent tapped/clicked. This is especially useful for:\n"
        "    - Video playback: check if the agent tapped the play button or\n"
        "      the correct episode in a list.\n"
        "    - Toggle/switch states: verify the tap landed on the correct icon.\n"
        "    - Any case where the VLM is unsure what the agent actually did.\n\n"
        "**When visual verification is limited** (small icons, toggle states):\n"
        "  → Use detect_image_changes (step 2.5 in workflow) to find changed\n"
        "    regions, then inspect_image_region on the annotated diff.\n"
        "  → If still unable to confirm the state visually, note this in the\n"
        "    evidence field (e.g. 'VLM cannot distinguish this icon state at\n"
        "    this resolution'), then weigh all available signals (surrounding UI\n"
        "    context, action semantics, before/after diff) to reach a verdict.\n"
        "  → Do NOT use click counts or toggle-sequence logic as sole evidence.\n\n"
        "## Step Effectiveness Analysis\n\n"
        "`inspect_image_region(image_path=<exact path>)` should be used with the action's\n"
        "canonical BEFORE/AFTER/overlay file path. Its output is descriptive only.\n"
        "Use the step-indexed before/after descriptions to reason about effectiveness yourself.\n"
        "Prioritize step-effect reasoning for steps that:\n"
        "  - Appear in checklist items' evidence chains\n"
        "  - Involve navigation actions (tap, swipe, open_app)\n"
        "  - Are at decision boundaries between subtasks\n\n"
        "## Verdict\n"
        "- Per-item 'result' MUST be 'passed' or 'failed'. Never use 'partial_success' for a single item.\n"
        "- final_verdict is ADVISORY: the system computes the task verdict from the per-item\n"
        "  results in dependency order (front to back). 'success' = every item passed;\n"
        "  'partial_success' = the passes form a CONTIGUOUS PREFIX and every failure is in\n"
        "  the tail (the front of the task was achieved); 'fail' = a failure is followed by\n"
        "  a later pass, or nothing passed. A pass that comes after an earlier failure is\n"
        "  hollow — it depends on a broken prerequisite (e.g. play controls work, but the\n"
        "  WRONG content is playing), and the whole task is then 'fail', not 'partial_success'.\n\n"
        "## Root Cause Attribution\n"
        "After all verdicts are submitted, the system will analyze each failed item "
        "to determine whether the root cause is a plan_skill problem or action_skill problem:\n"
        "- plan_skill: wrong task decomposition (missing/incorrect step), wrong navigation strategy, "
        "wrong page choice\n"
        "- action_skill: correct plan but wrong execution (clicked wrong element, entered wrong text, "
        "failed to select correct option)\n"
        "- Items about 'entering/navigating to a page' → likely plan_skill\n"
        "- Items about specific UI interactions (click, select, input) → likely action_skill\n\n"
        + _TOOL_CATALOG
    )


def _build_initial_prompt(trajectory: dict) -> str:
    task = trajectory.get("task", "unknown task")
    plan_count = len(trajectory.get("plan_steps", []))

    # Use actions_flat as authoritative source (plan_steps subtasks are always empty)
    actions_flat = trajectory.get("actions_flat", [])
    total_actions = len(actions_flat)
    subtask_ids = set()
    for a in actions_flat:
        sid = a.get("subtask_id", "")
        if sid:
            subtask_ids.add(str(sid))
    subtask_count = len(subtask_ids)

    plan_summaries = []
    for pi, ps in enumerate(trajectory.get("plan_steps", [])):
        plan_text = ps.get("plan_results", "") or ps.get("plan_goal", "")
        plan_summaries.append(f"  Plan Step {pi}: \"{plan_text[:200]}\"")
    plan_overview = "\n".join(plan_summaries) if plan_summaries else "  (no plan steps)"

    # Display recorded plan todos if available
    plan_todos = trajectory.get("plan_todos", [])
    if plan_todos:
        plan_summaries.append("\n## Recorded Plan Todos (from orchestrator)")
        for pt in plan_todos:
            op = pt.get("operation", "?")
            turn = pt.get("turn", "?")
            cycle = pt.get("plan_cycle", "?")
            plan_summaries.append(f"  [cycle={cycle} turn={turn} op={op}]:")
            for t in pt.get("todos", []):
                status = t.get("status", "?")
                tid = t.get("id", "?")
                content = t.get("content", "")[:150]
                plan_summaries.append(f"    [{status}] {tid}: {content}")
    plan_overview = "\n".join(plan_summaries) if plan_summaries else "  (no plan steps)"

    action_lines = _build_action_summary(trajectory)

    # Final orchestrator screenshot — the last frame the agent saw
    final_shot = str(trajectory.get("final_orch_screenshot", "") or "").strip()
    if final_shot and not Path(final_shot).is_file():
        local_candidate = Path(str(trajectory.get("_case_dir") or "")) / Path(final_shot).name
        if local_candidate.is_file():
            final_shot = str(local_candidate)
    final_line = (
        f"\n**Final Orchestrator Screenshot (preferred settled-state cross-check)**: `{final_shot}`\n"
        f"This is the page state when the agent declared task completion. Use it when it is "
        f"available and not already represented by a checked journal entry or precomputed "
        f"summary. If it is already registered as checked evidence, reuse that evidence and "
        f"do not issue another visual query for the same image and keyword. If it is available "
        f"but not checked, inspect this exact file once with: inspect_image_region(image_path=`{final_shot}`, target_keyword=<checklist item>)\n"
        f"If this path is unavailable, use fetch_step_detail(global_step=\"final\", detail_type=\"screenshot_after\") only when a final-state lookup is needed; otherwise record the limitation and continue with available direct evidence.\n"
    ) if final_shot else ""

    return (
        f"# Trajectory\n\n**Task**: {task}\n**Plan Steps**: {plan_count}\n"
        f"**Subtasks**: {subtask_count}\n**Total Actions**: {total_actions}\n"
        f"{final_line}"
        f"## Plan Overview\n{plan_overview}\n\n## Action Trace\n{action_lines}\n\n"
        f"Begin. Call generate_verification_checklist first."
    )


def _build_action_summary(trajectory: dict) -> str:
    """Compact action reference: Quick Reference + concise full trace.

    Prioritizes \"actions_flat\" (atomic records).  Falls back to nested
    plan_steps -> subtasks -> actions when flat data is unavailable.
    """
    flat = trajectory.get("actions_flat", [])
    if flat:
        return _build_flat_summary(flat, trajectory)

    # ── Legacy fallback ──────────────────────────────────────────────────
    lines: list[str] = []
    for pi, ps in enumerate(trajectory.get("plan_steps", [])):
        seen: dict[str, int] = {}
        for si, st in enumerate(ps.get("subtasks", [])):
            sid = st.get("subtask_id", "?")
            seen[sid] = seen.get(sid, 0) + 1
            st["_dup_label"] = f"{sid}#{seen[sid]}" if seen[sid] > 1 else sid
            st["_subtask_index"] = si

    lines.append("## Quick Reference — COPY THESE EXACTLY TO LOAD SCREENSHOTS")
    lines.append("IMPORTANT: use subtask_index (integer), NOT subtask_id (string).")
    for pi, ps in enumerate(trajectory.get("plan_steps", [])):
        for si, st in enumerate(ps.get("subtasks", [])):
            acts = st.get("actions", [])
            if not acts:
                continue
            dup = st.get("_dup_label", "?")
            desc = st.get("subtask_desc", "")[:80]
            lines.append(
                f"  {dup} | pi={pi} si={si} actions=0..{len(acts)-1} | {desc}"
            )
            lines.append(
                f"    fetch_step_detail(plan_index={pi}, subtask_index={si}, "
                f"action_index={len(acts)-1}, detail_type=\"screenshot_after\")"
            )

    lines.append("\n## Action Trace")
    for pi, ps in enumerate(trajectory.get("plan_steps", [])):
        for si, st in enumerate(ps.get("subtasks", [])):
            dup = st.get("_dup_label", "?")
            desc = st.get("subtask_desc", "")[:100]
            lines.append(f"\n  Subtask {dup} (pi={pi}, si={si}): {desc}")
            for ai, act in enumerate(st.get("actions", [])):
                gs = act.get("global_step", "?")
                a = act.get("action", {})
                at = a.get("action", "?")
                d = ""
                if at in ("tap", "click"):
                    d = f"coord={a.get('coordinate', '?')}"
                elif at == "input_text":
                    d = f"text=\"{str(a.get('text', '?'))[:40]}\""
                elif at == "swipe":
                    d = f"{a.get('coordinate', '?')}→{a.get('coordinate2', '?')}"
                elif at == "system_button":
                    d = a.get("button", "?")
                lines.append(f"    [{gs}] {at}({d}) | ai={ai}")
    return "\n".join(lines) if lines else "  (no actions)"


def _resolve_prompt_artifact_path(trajectory: dict, raw_path: object) -> str:
    """Use an existing local case path when a trajectory retained a stale root."""
    return resolve_artifact(trajectory, raw_path)


def _build_flat_summary(flat: list[dict], trajectory: dict | None = None) -> str:
    """Build action summary from flat action records (global_step-indexed)."""
    lines = [
        "## Quick Reference — FLAT (use exact file paths for visual evidence)",
        "Each action has BEFORE + AFTER screenshots. Check BOTH for evidence.",
        "global_step is only an ordinal lookup key; evidence_ref must use the exact file path.",
    ]
    for a in flat:
        gs = a.get("global_step", "?")
        desc = a.get("action_desc", "")[:60]
        sub_desc = a.get("subtask_desc", "")[:60]
        before_path = _resolve_prompt_artifact_path(trajectory or {}, a.get("screenshot_before"))
        after_path = _resolve_prompt_artifact_path(trajectory or {}, a.get("screenshot_after"))
        overlay_path = _resolve_prompt_artifact_path(trajectory or {}, a.get("action_overlay_path"))
        lines.append(
            f"  {describe_action(a)}: {desc} | subtask: {sub_desc}"
        )
        if before_path:
            lines.append(f"    before_path={before_path}")
        if after_path:
            lines.append(f"    after_path={after_path}")
        if overlay_path:
            lines.append(f"    overlay_path={overlay_path}")
        lines.append(
            f"    inspect_image_region(image_path=<one of the exact paths above>, target_keyword=<keyword>)"
        )

    lines.append("\n## Action Trace (chronological)")
    for a in flat:
        gs = a.get("global_step", "?")
        desc = a.get("action_desc", "")[:80]
        sub_id = a.get("subtask_id", "?")
        lines.append(f"  [{describe_action(a)}] {desc}  (task={sub_id})")
    return "\n".join(lines)


def _track_evidence(ctx: EvaluatorContext, tool_name: str, args: dict, result: dict) -> None:
    """Record loaded evidence in ctx._evidence_session for verdict_basis tracking."""
    flat = ctx.trajectory.get("actions_flat", []) if ctx.trajectory else []
    file2ord = file_step_index(flat)

    def _extract_gs() -> int:
        """Extract a canonical step for an evidence entry.

        Returns the ORDINAL of the committed action that was examined, -1 for the
        final-orchestrator screenshot, or 0 when nothing committed is involved.
        Physical on-disk file indices leaked from ``global_step`` / ``path`` are
        resolved to their owning ordinal, so verdict_basis.global_steps can never
        contain an out-of-range physical index.
        """
        gs = args.get("global_step")
        if gs is not None and str(gs).lower() == "final":
            return -1  # sentinel for final orchestrator screenshot (not an action step)
        # Numeric candidates from args/result, treated as ordinal-first then
        # physical-index→owner. Non-numeric orchestrator labels resolve to None.
        for cand in (gs, result.get("step_index"), result.get("global_step")):
            if cand is None or cand == "?":
                continue
            o = resolve_ordinal(flat, gs=cand)
            if o is not None:
                return o
        # Path-based fallback: the physical file's committed owner.
        img = args.get("image_path", "") or result.get("path", "")
        fs = parse_file_step(img)
        if fs is not None and fs in file2ord:
            return file2ord[fs]
        return 0

    gs = _extract_gs()

    if tool_name == "fetch_step_detail":
        dtype = result.get("detail_type", "")
        path = result.get("path", "")
        if result.get("exists") and path:
            etype = "ui_tree" if "ui_tree" in dtype else "screenshot_loaded"
            ctx._evidence_session.append({
                "global_step": gs,
                "type": etype,
                "path": path,
            })

    elif tool_name == "query_ui_tree_diff":
        for side in ("before", "after"):
            if result.get(f"{side}_available") and result.get(f"{side}_path"):
                ctx._evidence_session.append({
                    "global_step": gs,
                    "type": "ui_tree",
                    "path": result[f"{side}_path"],
                })

    elif tool_name == "inspect_image_region":
        if not result.get("error"):
            images = [
                result.get("before_path", ""),
                result.get("after_path", ""),
                args.get("before_path", ""),
                args.get("image_path", ""),
            ]
            if not any(images):
                images = [entry.get("path", "") for entry in ctx._evidence_session
                          if entry.get("type") == "screenshot_loaded" and entry.get("global_step") == gs]
            for img in images:
                if img:
                    ctx._evidence_session.append({"global_step": gs, "type": "screenshot_inspected", "path": img})

    elif tool_name == "detect_image_changes":
        annotated = result.get("annotated_image", "")
        if annotated:
            ctx._evidence_session.append({
                "global_step": gs,
                "type": "screenshot",
                "path": annotated,
            })


def _build_context_summary(ctx: EvaluatorContext) -> str:
    if not ctx.checklist:
        return ""
    checked = sum(1 for c in ctx.checklist if c.get("checked"))
    passed = ctx.passed_count()
    failed = ctx.failed_count()
    total = len(ctx.checklist)
    unchecked = [f"{c['id']}: {c['description']}" for c in ctx.checklist if not c.get("checked")]
    lines = [f"Progress: {checked}/{total} checked ({passed} passed, {failed} failed)."]
    if unchecked:
        lines.append(f"Remaining: {', '.join(unchecked)}")
        lines.append("Gather evidence and submit verdicts incrementally — update items as soon as you can judge them.")
    else:
        lines.append("All items checked. Add final_verdict and finish.")
    return "\n".join(lines)


# ── Logging ──────────────────────────────────────────────────────────────────

def _log_llm_input_text(step: int, system: str, messages: list[dict]) -> None:
    sys_chars = len(system)
    msg_chars = sum(len(json.dumps(m, ensure_ascii=False, default=str)) for m in messages)
    total_chars = sys_chars + msg_chars
    est_tokens = total_chars // 2.5
    logger.warning(
        "[Input step %s] msgs=%s sys=%s msg=%s total=%s est_tok=~%s",
        step, len(messages), sys_chars, msg_chars, total_chars, int(est_tokens),
    )


# E1: track whether the system prompt has been written as meta header
_dialog_system_written = False


def _write_dialog_meta(fh, system: str) -> None:
    """E1: write system prompt once as a meta header."""
    global _dialog_system_written
    fh.write(json.dumps({"type": "meta", "format_version": 2, "system": system}, ensure_ascii=False) + "\n")
    fh._dialog_system = system
    _dialog_system_written = True


def _write_dialog_record(fh, step: int, system: str, messages: list[dict], response: str) -> None:
    """Version 2 edits support append, replacement and truncation of history."""
    if getattr(fh, '_dialog_system', None) != system:
        _write_dialog_meta(fh, system)
    # Snapshot serialized values, not mutable caller-owned message dictionaries.
    current = json.loads(json.dumps(messages, ensure_ascii=False, default=str))
    previous = getattr(fh, '_dialog_history', [])
    common = 0
    while common < min(len(previous), len(current)) and previous[common] == current[common]:
        common += 1
    record = {
        "type": "turn", "format_version": 2,
        "step": step,
        "truncate_to": common,
        "new_messages": current[common:],
        "response": response,
    }
    fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    fh.flush()
    fh._dialog_history = current


def _write_tool_trace(fh, step: int, tool_name: str, model: str, system: str,
                      user_prompt: str, raw_response: dict, usage: dict | None = None) -> None:
    """E3: write inner tool LLM/VLM call details for full traceability."""
    trace = {
        "type": "tool_trace",
        "step": step,
        "tool": tool_name,
        "model": model,
        "system": system[:300],
        "user_prompt": user_prompt[:500],
        "raw_response": raw_response,
        "usage": usage or {},
    }
    fh.write(json.dumps(trace, ensure_ascii=False, default=str) + "\n")
    fh.flush()


def _write_llm_stats(fh, step: int, model: str, input_chars: int, output_chars: int,
                     usage: dict | None = None) -> None:
    """E4: write top-level LLM call statistics."""
    stats = {
        "type": "llm_call",
        "step": step,
        "model": model,
        "input_chars": input_chars,
        "output_chars": output_chars,
        "usage": usage or {},
    }
    fh.write(json.dumps(stats, ensure_ascii=False, default=str) + "\n")
    fh.flush()


def _log_llm_output_text(step: int, content: str) -> None:
    logger.warning("[Output step %s] chars=%s preview=%s", step, len(content), content[:250])


# ── Computations ─────────────────────────────────────────────────────────────

def _count_screenshots_in_dir(output_dir: Path) -> int:
    """Count committed screenshot steps: distinct indices of ``stepNNN_vla_done.jpg``.

    Uses a COUNT, not the max index. Prefetch cancellations can leave committed
    file indices with holes (e.g. only step001/002/004/006/007 exist for 5
    committed actions), so the top index would over-state the real step count and
    inflate the adaptive max_steps budget.
    """
    if not output_dir or not output_dir.exists():
        return 0
    indices: set[int] = set()
    for f in output_dir.iterdir():
        m = re.match(r"step(\d+)_vla_done\.jpg$", f.name)
        if m:
            indices.add(int(m.group(1)))
    return len(indices)


def _evaluator_max_steps(total_screenshots: int) -> int:
    """Leave room for checklist, evidence, and recovery tool turns."""
    return max(10 * max(1, int(total_screenshots)), 50)


_CHECK_EVALUATOR_MODEL = os.environ.get(
    "GUI_AGENT_EVALUATOR_CHECK_MODEL",
    str(getattr(config, "EVALUATOR_TEXT_MODEL", "qwen3.8-flash")),
)
_CHECK_AUDIT_MAX_ROUNDS = 3


def _check_item_sources(ctx: EvaluatorContext, item: dict) -> tuple[list[str], list[str]]:
    """Collect original evidence, using step summaries only to locate missing sources."""
    from agent.evaluator.tools.inspect_image_region import _resolve_case_artifact_path
    from agent.evaluator.tools.image_prep import max_images_per_call

    item_id = str(item.get("id") or "")
    basis = ctx._verdict_basis.get(item_id, {})
    evidence = str(item.get("evidence") or "")
    image_paths: list[str] = []
    ui_paths: list[str] = []

    def add_path(target: list[str], value: str) -> None:
        resolved = _resolve_case_artifact_path(ctx, value)
        if resolved and Path(resolved).is_file() and resolved not in target:
            target.append(resolved)

    for ref in re.findall(r"\[evidence_ref:\s*([^\]]+)\]", evidence):
        raw = ref.strip().rstrip(".,;，；")
        if raw.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            add_path(image_paths, raw)
        elif raw.lower().endswith((".xml", ".txt")):
            add_path(ui_paths, raw)

    for value in basis.get("screenshot_paths", []):
        add_path(image_paths, str(value))
    for value in basis.get("ui_tree_paths", []):
        add_path(ui_paths, str(value))

    flat = ctx.trajectory.get("actions_flat", [])
    cited_steps = {int(value) for value in re.findall(r"\bstep\s*#\s*(\d+)\b", evidence, re.I)}
    for global_step in sorted(cited_steps):
        action = next((row for row in flat if row.get("global_step") == global_step), None)
        if not action:
            continue
        # Keep the post-action state ahead of auxiliary context when the image
        # budget requires truncation; explicit report citations were added first.
        for key in ("screenshot_after", "screenshot_before", "action_overlay_path"):
            add_path(image_paths, str(action.get(key) or ""))
        for key in ("ui_tree_before_raw", "ui_tree_after_raw"):
            add_path(ui_paths, str(action.get(key) or ""))

    if re.search(r"\bfinal\b|final screenshot|最终截图", evidence, re.I):
        add_path(image_paths, str(ctx.trajectory.get("final_orch_screenshot") or ""))

    if not image_paths and not ui_paths:
        records = list(ctx._step_summary_records.values())
        if not records and ctx._step_summary_path:
            artifact = _read_step_summary_artifact(Path(ctx._step_summary_path))
            if artifact.get("session_id") == ctx.session_id and (
                not ctx._trajectory_path
                or artifact.get("trajectory_path") == ctx._trajectory_path
            ):
                records = list(artifact.get("steps") or [])

        description = str(item.get("description") or "")
        keywords = [str(value) for key in ("target_keywords", "entities")
                    for value in (item.get(key) or []) if str(value).strip()]

        def bigrams(value: str) -> set[str]:
            compact = "".join(ch.lower() for ch in value if ch.isalnum())
            return {compact[index:index + 2] for index in range(len(compact) - 1)}

        target_pairs = bigrams(description)

        def relevance(record: dict) -> tuple[int, int]:
            summary = record.get("visual_summary") or {}
            after = json.dumps({key: summary.get(key) for key in (
                "after_state_summary", "after_page", "after_visible_entities",
                "observed_transition", "after_visible_text",
            )}, ensure_ascii=False)
            score = len(target_pairs & bigrams(after))
            score += sum(8 for word in keywords if word.lower() in after.lower())
            return score, int(record.get("global_step") or 0)

        candidates = sorted(
            (record for record in records if record.get("status") in {"complete", "partial"}),
            key=relevance, reverse=True,
        )
        limit = max_images_per_call()
        after_slots = max(1, limit - (1 if len(candidates) <= limit - 1 else 3))
        for record in candidates:
            if len(image_paths) >= after_slots:
                break
            add_path(image_paths, str(((record.get("images") or {}).get("after") or {}).get("path") or ""))
        if len(image_paths) < limit:
            add_path(image_paths, str(ctx.trajectory.get("final_orch_screenshot") or ""))
        if candidates and len(image_paths) < limit:
            add_path(image_paths, str(((candidates[0].get("images") or {}).get("before") or {}).get("path") or ""))
        if candidates and len(image_paths) < limit:
            add_path(image_paths, str(((candidates[0].get("images") or {}).get("overlay") or {}).get("path") or ""))
        logger.warning("[check evaluator] item=%s summary fallback selected %d original screenshots",
                       item_id, len(image_paths))
    return image_paths, ui_paths


def _check_one_item(ctx: EvaluatorContext, item: dict, primary_result: str) -> dict:
    """Independently audit one checklist verdict against cited original evidence."""
    from agent.evaluator.tools.image_prep import max_images_per_call, prepare_vision_image
    from agent.models.llm_client import make_image_content

    image_paths, ui_paths = _check_item_sources(ctx, item)
    if not image_paths and not ui_paths:
        return {
            "status": "incomplete", "result": "uncertain",
            "reason": "no original screenshot/UI-tree source was available for this checklist item",
            "image_sources": [], "ui_tree_sources": [], "omitted_image_sources": [],
        }
    image_limit = max_images_per_call()
    selected_images = image_paths[:image_limit]
    omitted_images = image_paths[image_limit:]
    ui_evidence: list[str] = []
    for path in ui_paths:
        try:
            from agent.evaluator.tools.fetch_step_detail import _process_ui_tree
            raw = Path(path).read_text(encoding="utf-8", errors="replace")
            ui_evidence.append(f"SOURCE {path}\n{_process_ui_tree(raw)[:10000]}")
        except OSError as exc:
            ui_evidence.append(f"SOURCE {path}\n[READ ERROR: {exc}]")

    prompt = {
        "task": str(ctx.trajectory.get("task") or ""),
        "checklist_item": {"id": item.get("id"), "description": item.get("description")},
        "primary_verdict": primary_result,
        "primary_evidence": str(item.get("evidence") or ""),
        "image_sources_attached": selected_images,
        "image_sources_omitted_due_to_limit": omitted_images,
        "ui_tree_evidence": ui_evidence,
    }
    system = (
        "You are an independent checklist-evidence auditor for a GUI evaluator. "
        "Audit exactly one checklist item. Independently inspect the attached original "
        "screenshots and UI-tree evidence, then compare your result with the primary "
        "evaluator's verdict and rationale. Do not merely agree with the primary report.\n\n"
        "NON-NEGOTIABLE, OBJECTIVE AND BALANCED EVIDENCE STANDARD:\n"
        "- A pass requires a positive fact directly visible in the supplied screenshot/UI "
        "tree/execution evidence that supports the checklist condition.\n"
        "- A missing label in one screenshot means only that this image does not show it; "
        "absence alone is not proof that the condition is false. Equally, lack of a "
        "contradiction is not proof that the condition is true.\n"
        "- A fail requires direct contradictory evidence or sufficiently complete evidence "
        "that establishes the condition was not met. If evidence is incomplete or ambiguous, "
        "return uncertain rather than guessing.\n"
        "- Do not infer equivalence from semantic similarity, uniqueness, action intent, "
        "plans, or the primary evaluator's explanation. Separate what is visible from "
        "interpretation. Check prerequisite meaning when the item depends on earlier items.\n"
        "- Report which attached source supports each fact. Never claim to have inspected "
        "a source that was not attached.\n\n"
        "Return ONLY JSON: {\"result\":\"passed|failed|uncertain\","
        "\"observed_facts\":[\"...\"],\"contradicting_facts\":[\"...\"],"
        "\"unsupported_claims\":[\"...\"],\"evidence_refs\":[\"exact attached source path\"],"
        "\"reason\":\"...\"}."
    )
    user_text = json.dumps(prompt, ensure_ascii=False, indent=2)
    messages: list[dict] = []
    if selected_images:
        content: list[dict] = [{"type": "text", "text": user_text}]
        for index, path in enumerate(selected_images, 1):
            try:
                encoded, _mime = prepare_vision_image(path)
                content.append({"type": "text", "text": f"Evidence image {index}: {path}"})
                content.append(make_image_content(encoded))
            except Exception as exc:
                return {
                    "status": "error", "result": "uncertain", "reason": f"image preparation failed: {exc}",
                    "image_sources": selected_images, "ui_tree_sources": ui_paths,
                    "omitted_image_sources": omitted_images,
                }
        messages = [{"role": "user", "content": content}]
    else:
        messages = [{"role": "user", "content": user_text}]

    try:
        checker = LLMClient()
        if selected_images:
            response = checker.chat_with_vision(
                model=_CHECK_EVALUATOR_MODEL,
                system=system,
                messages=messages,
                temperature=0.0,
                max_tokens=2400,
                role="evaluator",
                phase="check",
            )
        else:
            response = checker.chat(
                model=_CHECK_EVALUATOR_MODEL,
                system=system,
                messages=messages,
                temperature=0.0,
                max_tokens=2400,
                role="evaluator",
                phase="check",
            )
        parsed = _parse_response(str(response.get("content") or ""))
        if not isinstance(parsed, dict) or parsed.get("result") not in {"passed", "failed", "uncertain"}:
            return {
                "status": "error", "result": "uncertain", "reason": "checker returned invalid JSON/result",
                "image_sources": selected_images, "ui_tree_sources": ui_paths,
                "omitted_image_sources": omitted_images,
            }
        return {
            "status": "complete",
            "result": parsed["result"],
            "observed_facts": parsed.get("observed_facts", []),
            "contradicting_facts": parsed.get("contradicting_facts", []),
            "unsupported_claims": parsed.get("unsupported_claims", []),
            "evidence_refs": parsed.get("evidence_refs", []),
            "reason": str(parsed.get("reason") or ""),
            "image_sources": selected_images,
            "ui_tree_sources": ui_paths,
            "omitted_image_sources": omitted_images,
        }
    except Exception as exc:
        logger.exception("[check evaluator] failed item=%s", item.get("id"))
        return {
            "status": "error", "result": "uncertain", "reason": f"checker call failed: {exc}",
            "image_sources": selected_images, "ui_tree_sources": ui_paths,
            "omitted_image_sources": omitted_images,
        }


def _write_check_audit_artifact(ctx: EvaluatorContext) -> None:
    path_value = str(getattr(ctx, "_check_audit_path", "") or "")
    if not path_value or not ctx._check_audit:
        return
    path = Path(path_value)
    ctx._check_audit["artifact_path"] = str(path.resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(ctx._check_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _request_primary_revision(ctx: EvaluatorContext, disagreements: list[dict]) -> dict:
    """Ask the original text evaluator to reconsider using the checker evidence."""
    system = str(getattr(ctx, "_primary_eval_system", "") or _build_system_prompt())
    messages = getattr(ctx, "_primary_eval_messages", [])
    if not isinstance(messages, list):
        messages = []
    challenge = {
        "instruction": (
            "An independent evidence audit disagrees with some of your checklist judgments. "
            "Reconsider the disputed items and any checklist items whose prerequisites depend "
            "on them. Your previous verdict is not binding. Follow the grounding rules in your "
            "system prompt. Distinguish absent evidence from contradictory evidence: absence "
            "alone is not proof of failure, but lack of contradiction is not proof of success. "
            "Return revised verdicts for the affected items with direct observations and exact "
            "evidence_ref paths. Do not invent or silently change evidence."
        ),
        "check_findings": disagreements,
        "items_at_or_after_earliest_disagreement": [
            {"id": row.get("id"), "description": row.get("description"), "result": row.get("result")}
            for row in ctx.checklist[
                min((i for i, row in enumerate(ctx.checklist)
                     if str(row.get("id") or "") in {entry.get("item_id") for entry in disagreements}), default=0):
            ]
        ],
        "current_checklist": [
            {"id": row.get("id"), "description": row.get("description"), "result": row.get("result"),
             "evidence": row.get("evidence", "")}
            for row in ctx.checklist
        ],
    }
    messages.append({"role": "user", "content": json.dumps(challenge, ensure_ascii=False, indent=2)})
    try:
        response = ctx._text_llm.chat(
            model=str(getattr(ctx, "_primary_eval_model", "") or getattr(config, "EVALUATOR_TEXT_MODEL", "qwen3.8-flash")),
            system=system,
            messages=messages,
            temperature=0.0,
            max_tokens=5000,
            role="evaluator",
            phase="text",
        )
        content = str(response.get("content") or "")
        messages.append({"role": "assistant", "content": content})
        parsed = _parse_response(content)
        verdicts = parsed.get("verdicts", []) if isinstance(parsed, dict) else []
        for finding in disagreements:
            for source in finding.get("check", {}).get("image_sources", []):
                ctx._evidence_session.append({"global_step": 0, "type": "screenshot_inspected", "path": source})
            for source in finding.get("check", {}).get("ui_tree_sources", []):
                ctx._evidence_session.append({"global_step": 0, "type": "ui_tree", "path": source})
        updated, invalid = _apply_verdicts(verdicts, ctx) if verdicts else (0, 1)
        return {
            "status": "complete" if updated else "no_valid_revision",
            "updated_items": updated,
            "invalid_verdicts": invalid,
            "response": content[:6000],
            "results_after_revision": {row.get("id"): row.get("result") or "unchecked" for row in ctx.checklist},
        }
    except Exception as exc:
        logger.exception("[check evaluator] primary re-evaluation failed")
        return {"status": "error", "error": str(exc)}


def _run_check_audit(ctx: EvaluatorContext) -> dict:
    """Run up to three independent review/reconsideration cycles."""
    audit = {
        "schema_version": "evaluator.check_audit.v1",
        "check_model": _CHECK_EVALUATOR_MODEL,
        "primary_model": str(getattr(ctx, "_primary_eval_model", "") or getattr(config, "EVALUATOR_TEXT_MODEL", "")),
        "max_rounds": _CHECK_AUDIT_MAX_ROUNDS,
        "status": "running",
        "rounds": [],
        "unresolved_items": [],
    }
    ctx._check_audit = audit
    _write_check_audit_artifact(ctx)
    if not ctx.checklist:
        audit["status"] = "no_checklist"
        _write_check_audit_artifact(ctx)
        return audit

    review_item_ids = {str(row.get("id") or "") for row in ctx.checklist}
    for round_index in range(1, _CHECK_AUDIT_MAX_ROUNDS + 1):
        before = {str(row.get("id")): row.get("result") or "unchecked" for row in ctx.checklist}
        checks: list[dict] = []
        disagreements: list[dict] = []
        checker_errors = False
        reviewed_items = [row for row in ctx.checklist if str(row.get("id") or "") in review_item_ids]
        round_record = {
            "round": round_index,
            "primary_results_before_check": before,
            "checks": checks,
            "disagreement_item_ids": [],
            "primary_revision": None,
        }
        audit["rounds"].append(round_record)
        _write_check_audit_artifact(ctx)
        for item in reviewed_items:
            primary_result = str(item.get("result") or "unchecked")
            logger.warning("[check evaluator] round=%d item=%s start primary=%s",
                           round_index, item.get("id"), primary_result)
            check = _check_one_item(ctx, item, primary_result)
            row = {"item_id": str(item.get("id") or ""), "primary_result": primary_result, "check": check}
            checks.append(row)
            logger.warning("[check evaluator] round=%d item=%s status=%s result=%s",
                           round_index, row["item_id"], check.get("status"), check.get("result"))
            _write_check_audit_artifact(ctx)
            if check.get("status") != "complete":
                checker_errors = True
                continue
            if check.get("result") != primary_result:
                disagreements.append(row)
        round_record["disagreement_item_ids"] = [row["item_id"] for row in disagreements]
        audit["rounds_completed"] = round_index
        _write_check_audit_artifact(ctx)

        if checker_errors:
            audit["status"] = "checker_incomplete"
            audit["unresolved_items"] = [row["item_id"] for row in checks if row["check"].get("status") != "complete"]
            break
        if not disagreements:
            audit["status"] = "agreed"
            audit["unresolved_items"] = []
            break
        audit["unresolved_items"] = [row["item_id"] for row in disagreements]
        if round_index >= _CHECK_AUDIT_MAX_ROUNDS:
            audit["status"] = "unresolved_disagreement"
            break

        earliest_disagreement = min(
            (index for index, row in enumerate(ctx.checklist)
             if str(row.get("id") or "") in {item["item_id"] for item in disagreements}),
            default=0,
        )
        # Recheck the disputed item and later checklist items, whose evidence may
        # rely on the disputed prerequisite. Earlier, already-agreed items remain
        # in the audit history and are not needlessly sent again.
        review_item_ids = {
            str(row.get("id") or "") for row in ctx.checklist[earliest_disagreement:]
        }

        revision = _request_primary_revision(ctx, disagreements)
        round_record["primary_revision"] = revision
        _write_check_audit_artifact(ctx)
        if revision.get("status") == "error":
            audit["status"] = "primary_revision_error"
            break

    audit["final_primary_results"] = {
        str(row.get("id")): row.get("result") or "unchecked" for row in ctx.checklist
    }
    audit["final_verdict"] = _compute_verdict(ctx)
    audit["disagreement_unresolved"] = audit.get("status") == "unresolved_disagreement"
    audit["review_incomplete"] = audit.get("status") in {
        "checker_incomplete", "primary_revision_error"
    }
    _write_check_audit_artifact(ctx)
    return audit


def _finalize_report(ctx: EvaluatorContext, verdict: str, steps: int) -> EvaluationReport:
    """Collect missing visual facts, let the text model summarize, then report."""
    # Quality fusion consumes checklist verdicts and their original evidence only.
    # Initial evidence extraction and inspected-reference checks stay unchanged.
    quality_only = os.environ.get("ADF_CHECKLIST_OUTPUT_POLICY") == "quality_only_v1"
    if not quality_only:
        try:
            _collect_missing_vision_observations(ctx)
            _register_precomputed_visual_evidence(ctx)
        except Exception as e:
            logger.warning("[_finalize_report] vision observation collection failed: %s", e)
    if ctx.all_checked() and getattr(ctx, "_evaluation_mode", "checklist") == "audit":
        try:
            _run_check_audit(ctx)
            verdict = _compute_verdict(ctx)
        except Exception as e:
            logger.exception("[_finalize_report] check evaluator failed; retaining primary verdict")
            ctx._check_audit = {
                "schema_version": "evaluator.check_audit.v1",
                "check_model": _CHECK_EVALUATOR_MODEL,
                "max_rounds": _CHECK_AUDIT_MAX_ROUNDS,
                "status": "checker_error",
                "error": str(e),
                "disagreement_unresolved": False,
                "review_incomplete": True,
                "final_verdict": _compute_verdict(ctx),
            }
            _write_check_audit_artifact(ctx)
            verdict = _compute_verdict(ctx)
    elif getattr(ctx, "_evaluation_mode", "checklist") == "checklist" and not ctx._check_audit:
        ctx._check_audit = {
            "schema_version": "evaluator.check_audit.v1",
            "status": "skipped_checklist_mode",
            "reason": "default evaluator mode checks the checklist only",
        }
    key_steps, redundant_steps = [], []
    if quality_only:
        ctx._step_summary_metadata = {**getattr(ctx, "_step_summary_metadata", {}),
            "report_enrichment": "not_requested", "output_policy": "quality_only_v1"}
    else:
        try:
            key_steps, redundant_steps = _summarize_steps(ctx)
        except Exception as e:
            logger.warning("[_finalize_report] _summarize_steps failed: %s", e)
    report = _build_report(ctx, verdict, steps, key_steps, redundant_steps)
    _save_checkpoint(ctx, reason="checklist_complete" if ctx.all_checked() else "checklist_incomplete")
    return report


def _resolve_final_verdict(ctx: EvaluatorContext, declared: str | None = None) -> str:
    """The verdict that goes into the report — always the deterministic one.

    The evaluator LLM may declare its own ``final_verdict`` (or pass a ``verdict`` to the
    ``finish`` tool). That declaration is ADVISORY only: it is free text produced at the
    end of a long context and it does not apply the dependency-order rule (e.g. it can
    call a "mixed" checklist partial_success even though a core prerequisite failed and
    a later item passed hollowly). The deterministic verdict wins; disagreements are
    logged so a pattern of them is visible in the daemon log.
    """
    verdict = _compute_verdict(ctx)
    declared = (declared or "").strip()
    if declared and declared != verdict:
        logger.warning("[Verdict] model declared %r but deterministic verdict is %r "
                       "(checklist=%s) — using the deterministic one",
                       declared, verdict, _verdict_signature(ctx))
    return verdict


def _verdict_signature(ctx: EvaluatorContext) -> str:
    return ",".join(f"{c.get('id')}={c.get('result') or 'unchecked'}" for c in ctx.checklist)


def _compute_verdict(ctx: EvaluatorContext) -> str:
    """Front-to-back (dependency-order) verdict over the checklist.

    The checklist is generated as a dependency chain (navigate to the right page →
    pick the right target → the target content is actually shown/playing → …). A
    pass can therefore only be trusted if every earlier prerequisite passed.

      success           : every item passed.
      partial_success   : all passes form a contiguous PREFIX (every failure is in
                          the tail) — the front of the task was achieved and only
                          the tail was left incomplete.
      fail              : a failure occurs and a LATER item still passed — that
                          later pass depends on a broken prerequisite and is hollow
                          (e.g. the "play/pause works" item passed while the
                          "the SECOND drama is the one playing" item failed: the
                          agent played the WRONG content, so the task failed).

    Order = ctx.checklist order (chk_01, chk_02, … then any appended kp_NN).
    """
    passed = ctx.passed_count()
    failed = ctx.failed_count()
    unchecked = ctx.unchecked_count()
    total = len(ctx.checklist)
    if total == 0:
        return "fail_to_parse_checklist"  # D1: distinct from actual failure
    if unchecked > 0:
        return "unchecked"
    if failed == 0:
        return "success"
    if passed == 0:
        return "fail"
    # Dependency-order monotonicity: a pass AFTER an earlier failure is spurious.
    seen_failed = False
    for item in ctx.checklist:
        result = item.get("result")
        if result == "failed":
            seen_failed = True
        elif result == "passed" and seen_failed:
            return "fail"
    return "partial_success"


def _step_image_info(paths: dict[str, str]) -> dict[str, dict]:
    image_info: dict[str, dict] = {}
    for role in ("before", "overlay", "after"):
        path = str(paths.get(role) or "")
        info = {"path": path, "exists": bool(path and Path(path).is_file()), "size_bytes": 0, "sha256": ""}
        if info["exists"]:
            digest = hashlib.sha256()
            try:
                with open(path, "rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                info["size_bytes"] = Path(path).stat().st_size
                info["sha256"] = digest.hexdigest()
            except OSError:
                info["exists"] = False
        image_info[role] = info
    return image_info


def _step_visual_summary_fields(result: dict) -> dict:
    fields = (
        "before_state_summary", "before_page", "before_visible_entities",
        "overlay_target", "after_state_summary", "after_page",
        "after_visible_entities", "observed_transition", "uncertainty",
    )
    summary = {name: result.get(name, "" if name.endswith(("summary", "page", "transition")) else [])
               for name in fields}
    for name in ("before_visible_entities", "after_visible_entities", "uncertainty"):
        if isinstance(summary[name], str):
            summary[name] = [summary[name]] if summary[name].strip() else []
    if isinstance(summary["overlay_target"], str):
        summary["overlay_target"] = {"description": summary["overlay_target"], "visible_label": "", "certainty": "low"}
    # Keep established evaluator consumers working while making the richer
    # schema explicit for downstream tree construction.
    summary["before_description"] = result.get("before_description") or summary["before_state_summary"]
    summary["after_description"] = result.get("after_description") or summary["after_state_summary"]
    summary["transition_description"] = result.get("transition_description") or summary["observed_transition"]
    summary["before_visible_text"] = result.get("before_visible_text", "")
    summary["after_visible_text"] = result.get("after_visible_text", "")
    state_keys = (
        "app_id", "page_type", "page_title", "navigation_path", "active_tab",
        "selected_filters", "task_relevant_entities", "modal_or_ad", "media_state",
        "volatile_content", "visible_controls", "uncertainty", "evidence_refs",
    )
    for role in ("before", "after"):
        raw = result.get(f"{role}_state")
        state = raw if isinstance(raw, dict) else {}
        summary[f"{role}_state"] = {
            key: state.get(key, [] if key in {"navigation_path", "task_relevant_entities",
                                                "volatile_content", "visible_controls",
                                                "uncertainty", "evidence_refs"}
                           else {} if key == "selected_filters" else "unknown")
            for key in state_keys
        }
    summary["schema_version"] = "visual_state.v2"
    return summary


def _read_step_summary_artifact(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_step_summary_artifact(ctx: EvaluatorContext, expected: int | None = None) -> dict:
    path_value = str(getattr(ctx, "_step_summary_path", "") or "")
    if not path_value:
        return {"schema_version": _STEP_SUMMARY_SCHEMA, "status": "not_persisted"}
    path = Path(path_value)
    records = [ctx._step_summary_records[key] for key in sorted(ctx._step_summary_records)]
    expected_count = len(ctx.trajectory.get("actions_flat", [])) if expected is None else expected
    counts = {status: sum(1 for row in records if row.get("status") == status)
              for status in ("complete", "partial", "failed", "pending")}
    selected_count = counts["complete"] + counts["partial"] + counts["failed"]
    if expected_count == 0:
        status = "no_actions"
    elif counts["complete"] == expected_count:
        status = "complete"
    elif selected_count >= expected_count:
        status = "partial"
    else:
        status = "incomplete"
    document = {
        "schema_version": _STEP_SUMMARY_SCHEMA,
        "prompt_version": _STEP_SUMMARY_PROMPT_VERSION,
        "session_id": ctx.session_id,
        "task": str(ctx.trajectory.get("task") or ""),
        "trajectory_path": str(getattr(ctx, "_trajectory_path", "") or ""),
        "vision_model": str(getattr(config, "EVALUATOR_VISION_MODEL", "qwen3.8-flash")),
        "parallelism": _step_vision_concurrency(),
        "expected_step_count": expected_count,
        "counts": counts,
        "coverage": (counts["complete"] / expected_count) if expected_count else 1.0,
        "status": status,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "steps": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    metadata = {key: document[key] for key in (
        "schema_version", "prompt_version", "vision_model", "parallelism",
        "expected_step_count", "counts", "coverage", "status", "updated_at",
    )}
    metadata["artifact_path"] = str(path.resolve())
    ctx._step_summary_metadata = metadata
    return metadata


def _restore_step_summary_observation(ctx: EvaluatorContext, record: dict) -> None:
    try:
        gs = int(record.get("global_step"))
    except (TypeError, ValueError):
        return
    summary = dict(record.get("visual_summary") or {})
    images = record.get("images") or {}
    summary.setdefault("before_path", str((images.get("before") or {}).get("path") or ""))
    summary.setdefault("overlay_path", str((images.get("overlay") or {}).get("path") or ""))
    summary.setdefault("after_path", str((images.get("after") or {}).get("path") or ""))
    summary.setdefault("step_index", gs)
    summary.setdefault("summary_source", "step_triplet_v2")
    summary.setdefault("summary_status", record.get("status", "complete"))
    if record.get("image_roles_sent"):
        summary.setdefault("image_roles_sent", list(record["image_roles_sent"]))
    action = record.get("action") or {}
    summary.setdefault("action_text", str(action.get("action_desc") or ""))
    summary.setdefault("query", str(action.get("action_desc") or action.get("subtask_desc") or ""))
    summary.setdefault("skill_ids", list(action.get("action_skill_ids") or []))
    observations = ctx._vision_observations.setdefault(gs, [])
    for index, existing in enumerate(observations):
        if existing.get("summary_source") in {"step_triplet_v1", "step_triplet_v2"}:
            observations[index] = summary
            return
    observations.append(summary)


def _collect_missing_vision_observations(ctx: EvaluatorContext) -> None:
    """Ensure every action has a persisted objective before/overlay/after summary.

    Completed records are reused only when their image/action fingerprint and
    prompt/model version still match. Missing or failed steps are retried on a
    later evaluator pass; partial triplets remain explicitly marked partial.
    """
    flat = ctx.trajectory.get("actions_flat", [])
    if not flat:
        ctx._step_summary_metadata = _write_step_summary_artifact(ctx, expected=0)
        return

    from agent.evaluator.tools.inspect_image_region import _lookup_action_context, inspect_image_region
    from agent.models.llm_client import LLMClient

    actions: dict[int, dict] = {}
    for action in flat:
        try:
            gs = int(action.get("global_step"))
        except (TypeError, ValueError):
            continue
        if gs > 0:
            actions[gs] = action

    if not actions:
        ctx._step_summary_metadata = _write_step_summary_artifact(ctx, expected=0)
        return

    referenced_steps: set[int] = set()
    for basis in ctx._verdict_basis.values():
        referenced_steps.update(basis.get("global_steps", []))
    output_path = Path(ctx._step_summary_path) if ctx._step_summary_path else (
        Path(".evaluator") / f"session_{ctx.session_id}" / "_evaluator_step_summaries_v2.json"
    )
    existing = _read_step_summary_artifact(output_path)
    if existing.get("schema_version") == _STEP_SUMMARY_SCHEMA:
        for record in existing.get("steps", []):
            if isinstance(record, dict) and isinstance(record.get("global_step"), int):
                ctx._step_summary_records[record["global_step"]] = record
    ctx._step_summary_records = {
        gs: record for gs, record in ctx._step_summary_records.items() if gs in actions
    }

    model = str(getattr(config, "EVALUATOR_VISION_MODEL", "qwen3.8-flash"))
    prompt_version = _STEP_SUMMARY_PROMPT_VERSION
    work: list[tuple[int, dict, dict, str]] = []
    for gs, action in sorted(actions.items()):
        if _SKIP_UNREFERENCED_STEPS and referenced_steps and gs not in referenced_steps:
            continue
        image_context = _lookup_action_context(ctx, gs)
        image_paths = {
            "before": str(image_context.get("before_path") or action.get("screenshot_before") or ""),
            "overlay": str(image_context.get("overlay_path") or action.get("action_overlay_path") or ""),
            "after": str(image_context.get("image_path") or action.get("screenshot_after") or ""),
        }
        image_info = _step_image_info(image_paths)
        fingerprint_payload = {
            "image_paths": {key: value.get("path", "") for key, value in image_info.items()},
            "image_sha256": {key: value.get("sha256", "") for key, value in image_info.items()},
            "action": action.get("action", {}),
            "action_desc": action.get("action_desc", ""),
            "subtask_desc": action.get("subtask_desc", ""),
            "model": model,
            "prompt_version": prompt_version,
        }
        fingerprint = hashlib.sha256(json.dumps(
            fingerprint_payload, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")).hexdigest()
        cached = ctx._step_summary_records.get(gs, {})
        if (
            cached.get("fingerprint") == fingerprint
            and cached.get("status") in {"complete", "partial"}
        ):
            _restore_step_summary_observation(ctx, cached)
            continue
        record = {
            "run_id": str(ctx.trajectory.get("run_id") or ctx.session_id),
            "global_step": gs,
            "action_index": action.get("action_index", gs),
            "subtask_id": str(action.get("subtask_id") or ""),
            "action": {
                "action_desc": str(action.get("action_desc") or ""),
                "action_payload": action.get("action", {}),
                "subtask_desc": str(action.get("subtask_desc") or ""),
                "action_skill_ids": list(action.get("action_skill_ids") or []),
            },
            "images": image_info,
            "model": model,
            "prompt_version": prompt_version,
            "fingerprint": fingerprint,
            "status": "pending",
            "attempts": 0,
            "visual_summary": {},
            "error": "",
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        ctx._step_summary_records[gs] = record
        work.append((gs, action, image_context, fingerprint))

    ctx._step_summary_path = str(output_path)
    _write_step_summary_artifact(ctx, expected=len(actions))
    if not work:
        return

    concurrency = _step_vision_concurrency()
    logger.warning(
        "[step summary] extracting %d steps (concurrency=%d, model=%s)",
        len(work), concurrency, model,
    )
    thread_clients = local()

    def _analyze(item: tuple[int, dict, dict, str]) -> tuple[int, dict, int, str]:
        gs, action, image_context, _fingerprint = item
        # Thread-local client avoids races while initializing provider clients.
        client = getattr(thread_clients, "client", None)
        if client is None:
            client = LLMClient()
            thread_clients.client = client
        args = {
            "global_step": gs,
            "target_keyword": str(action.get("action_desc") or action.get("subtask_desc") or "")[:160],
            "action_text": str(action.get("action_desc") or action.get("subtask_desc") or ""),
            "intent_text": str(action.get("subtask_desc") or ""),
            "image_path": str(image_context.get("image_path") or ""),
            "before_path": str(image_context.get("before_path") or ""),
            "overlay_path": str(image_context.get("overlay_path") or ""),
            "_step_summary_mode": True,
            "_record_observation": False,
            "_write_tool_trace": False,
            "_vision_llm": client,
        }
        last_error = ""
        for attempt in range(1, 3):
            try:
                result = inspect_image_region(ctx, args)
                if result.get("error"):
                    last_error = str(result["error"])
                else:
                    return gs, result, attempt, ""
            except Exception as exc:
                last_error = str(exc)
            if attempt < 2:
                time.sleep(2)
        return gs, {}, 2, last_error or "step image analysis failed"

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="eval-step-vision") as pool:
        futures = {pool.submit(_analyze, item): item[0] for item in work}
        for future in as_completed(futures):
            gs = futures[future]
            record = ctx._step_summary_records[gs]
            try:
                _gs, result, attempts, error = future.result()
            except Exception as exc:
                result, attempts, error = {}, 2, str(exc)
            record["attempts"] = attempts
            record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            if error:
                record["status"] = "failed"
                record["error"] = error
            else:
                visual_summary = _step_visual_summary_fields(result)
                record["visual_summary"] = visual_summary
                record["status"] = "complete" if result.get("step_triplet_complete") else "partial"
                record["image_roles_sent"] = list(result.get("image_roles_sent") or [])
                record["error"] = "" if record["status"] == "complete" else "one or more triplet images unavailable"
                observation = {
                    **visual_summary,
                    "step_index": gs,
                    "query": record["action"]["action_desc"],
                    "action_text": record["action"]["action_desc"],
                    "before_path": record["images"]["before"]["path"],
                    "overlay_path": record["images"]["overlay"]["path"],
                    "after_path": record["images"]["after"]["path"],
                    "skill_ids": record["action"]["action_skill_ids"],
                    "image_roles_sent": record["image_roles_sent"],
                    "summary_source": "step_triplet_v2",
                    "summary_status": record["status"],
                }
                _restore_step_summary_observation(ctx, {"global_step": gs, "visual_summary": observation})
            _write_step_summary_artifact(ctx, expected=len(actions))
            logger.warning("[step summary] step=%d status=%s attempts=%d", gs, record["status"], attempts)


# ── Sliding-window summarize constants ──────────────────────────────────────
BATCH_SIZE = 8         # 每批 8 个 step
WINDOW_STRIDE = 6      # 滑动步长（相邻批重叠 2 步）
MAX_BATCH_RETRIES = 3  # 每批最多 3 次 LLM 调用

_OBSERVATION_MAX_CHARS = 600  # 单个视觉描述字段进入总结 prompt 的上限


def _clip_observation(value: object) -> str:
    text = str(value or "").strip()
    if len(text) <= _OBSERVATION_MAX_CHARS:
        return text
    return text[:_OBSERVATION_MAX_CHARS] + "..."


def _build_summarize_prompt(vision_observations: dict[int, list[dict]],
                            verdict_basis: dict,
                            labels: dict | None = None,
                            actions: dict[int, dict] | None = None) -> str:
    """构建单批 step-indexed visual observations 的文本判断 prompt。

    VLM 只提供客观描述；key/redundant 分类由 evaluator 文本模型完成。
    """
    lines = [
        "以下是 VLM 按步骤提取的客观视觉观察。它们只是事实，不包含有效性判断。",
        "你必须作为文本 evaluator，综合前后步骤和 checklist 依据自行判断。",
        "",
    ]
    for gs in sorted(vision_observations.keys()):
        label = (labels or {}).get(gs) or f"step {gs}"
        action = (actions or {}).get(gs, {})
        lines.append(f"## {label}")
        action_text = str(
            action.get("action_desc", "") or action.get("subtask_desc", "") or ""
        ).strip()
        if action_text:
            lines.append(f"- executed_action: {_clip_observation(action_text)}")
        skill_ids = sorted({
            str(skill_id)
            for observation in vision_observations[gs]
            for skill_id in (observation.get("skill_ids") or [])
            if skill_id not in (None, "")
        })
        if skill_ids:
            lines.append(f"- skill_ids: {', '.join(skill_ids)}")
        for index, observation in enumerate(vision_observations[gs], 1):
            lines.append(f"### observation {index}")
            structured = {key: observation.get(key) for key in ("before_state", "after_state")
                          if isinstance(observation.get(key), dict)}
            if structured:
                lines.append("- structured_visual_state_v2: " + _clip_observation(
                    json.dumps(structured, ensure_ascii=False, sort_keys=True)))
            for field_name in (
                "requested_focus", "before_description", "after_description",
                "transition_description", "before_visible_text",
                "after_visible_text", "uncertainty",
            ):
                value = _clip_observation(observation.get(field_name, ""))
                if value:
                    lines.append(f"- {field_name}: {value}")
        lines.append("")

    lines.append("## Checklist 判定依据")
    for item_id, basis in verdict_basis.items():
        lines.append(f"  {item_id}: steps={basis.get('global_steps', [])}")
    lines.append("")
    lines.append("""请综合以上客观观察，向前向后看每步的上下文：
- 不要把 action intent 当作结果已经发生；判断依据必须来自 before/after 描述或可见文本。
- 如果 before/after 描述不足以判断，不要将该步强行分类。
- key_step: 对任务完成有实质性推动的步骤
- redundant_step: 未推动任务、操作有误、或可省略的步骤

## reason 写作要求（重要）

每一条 reason 必须包含**具体视觉证据**，不能只复述动作意图。禁止以下写法：
- ❌ "属于无意义的填充步骤"
- ❌ "批量补充，没有实质推进"
- ❌ "操作有误"

正确写法必须包含**before/after 具体差异（或无差异） + 与任务或前后步骤的关系**：
- ✅ "step 3 before 与 after 均显示首页，页面元素和可见文本一致，没有出现目标页面或状态变化，该操作对任务无贡献"
- ✅ "step 7 重复点击了已在 step 5 成功打开的'我的收藏'入口，页面未变化，属于冗余操作"
- ✅ "step 12 滑动方向与目标列表的滚动方向相反（应向下滑却向上滑），导致页面回到已浏览区域，浪费了 step 11 的进度"
- ✅ "step 15 执行时子任务（选择播放源）实际未完成，页面仍停留在选择菜单，agent 误判为完成并进入下一步，属于 premature stop"

如果 VLM observation 标记 uncertainty，不能单独用它支持 key/redundant 结论。

## type 分类

redundant_step 的 type 字段：
- intent_mismatch: 操作意图与任务目标不匹配（如任务要求搜索却点击了浏览按钮）
- no_progress: 操作后页面状态无变化，没有推动任务
- wrong_direction: 操作方向错误导致倒退（如误返回、误滑动）
- premature_stop: 子任务未完成就提前停止

输出 JSON (不要输出任何其他文本):
{
  "key_steps_identified": [{"step": N, "reason": "...", "skill_ids": [...]}],
  "redundant_steps_identified": [
    {"step": N, "reason": "...",
     "type": "intent_mismatch|no_progress|wrong_direction|premature_stop",
     "target_skill_type": "action|plan",
     "skill_ids": [...]}
  ]
}""")
    return "\n".join(lines)


def _dump_summarize_debug(ctx: EvaluatorContext, batch_idx: int, retry: int,
                          prompt: str, response: str | None) -> None:
    """将每批 summarize 的原始输入/输出写入文件，便于排查 extract_json 失败原因。"""
    # 确定输出目录：优先用 dialog log 所在目录
    fh = getattr(ctx, "_dialog_fh", None)
    if fh is not None and hasattr(fh, "name"):
        debug_dir = Path(fh.name).parent / "_summarize_debug"
    else:
        debug_dir = Path(".evaluator") / f"session_{ctx.session_id}" / "_summarize_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)

    tag = f"batch{batch_idx}_retry{retry}"
    input_path = debug_dir / f"{tag}_input.txt"
    output_path = debug_dir / f"{tag}_output.txt"

    input_path.write_text(prompt, encoding="utf-8")
    output_path.write_text(str(response) if response else "(null)", encoding="utf-8")

    logger.warning(
        "[_summarize_steps] debug dumped: input=%s (%d chars), output=%s (%d chars)",
        input_path, len(prompt), output_path, len(str(response) if response else ""),
    )


def _summarize_steps(ctx: EvaluatorContext) -> tuple[list, list]:
    """Summarize step-indexed visual observations with the evaluator text model.

    将大量视觉观察按滑动窗口分批发给 evaluator text model。VLM 只提供
    before/after 客观描述；key/redundant 判断全部由文本模型完成。
    多批结果按 step 合并，key_step 优先级高于 redundant_step。

    When _SKIP_UNREFERENCED_STEPS is True, only steps referenced by at least one
    checklist verdict are included in the summarization; unreferenced steps are
    excluded from input, producing a sparser but faster result.
    """
    action_observations = {
        global_step: observations
        for global_step, observations in ctx._vision_observations.items()
        if isinstance(global_step, int) and global_step > 0 and observations
    }
    if not action_observations:
        return [], []

    # Compute referenced steps for optional filtering
    referenced_steps: set[int] = set()
    for basis in ctx._verdict_basis.values():
        referenced_steps.update(basis.get("global_steps", []))

    sorted_steps = sorted(action_observations.items())  # [(gs, observations), ...]
    if _SKIP_UNREFERENCED_STEPS and referenced_steps:
        sorted_steps = [
            (global_step, observations)
            for global_step, observations in sorted_steps
            if global_step in referenced_steps
        ]
        logger.warning(
            "[_summarize_steps] filtered to %d referenced steps (was %d total)",
            len(sorted_steps), len(action_observations),
        )
    if not sorted_steps:
        return [], []
    total = len(sorted_steps)

    # Canonical labels (step#N / file stepNNN) so the summarize model's reasons
    # cite an identity a human can locate on disk.
    act_by_gs = {a.get("global_step"): a
                 for a in ctx.trajectory.get("actions_flat", [])
                 if isinstance(a.get("global_step"), int)}
    labels = {gs: describe_action(act_by_gs.get(gs, {})) for gs, _ in sorted_steps}
    actions = {gs: act_by_gs.get(gs, {}) for gs, _ in sorted_steps}
    valid = {gs for gs, _ in sorted_steps}  # only these ordinals may be reported

    def _step_int(entry: dict) -> int | None:
        try:
            return int(entry.get("step"))
        except (TypeError, ValueError):
            return None

    # 按 step 聚合结果，key_step 优先级 > redundant_step
    key_step_map: dict[int, dict] = {}
    redundant_step_map: dict[int, dict] = {}

    batch_idx = 0
    start = 0
    while start < total:
        batch = dict(sorted_steps[start:start + BATCH_SIZE])
        prompt = _build_summarize_prompt(
            batch, ctx._verdict_basis, labels=labels, actions=actions,
        )
        batch_label = (
            f"batch {batch_idx} (steps {min(batch.keys())}-{max(batch.keys())})"
        )

        success = False
        for retry in range(MAX_BATCH_RETRIES):
            try:
                response_obj = ctx._text_llm.chat(
                    model=getattr(config, "EVALUATOR_TEXT_MODEL", "qwen3.8-flash"),
                    system=(
                        "You are the text evaluator for GUI trajectories. "
                        "Classify actions only from objective visual observations."
                    ),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=8192,
                    role="evaluator",
                    phase="text",
                )
                response = str(response_obj.get("content", "") or "")
                parsed = _parse_response(response)
                if parsed is None:
                    logger.warning(
                        "[_summarize_steps] %s extract_json returned None (retry %d/%d)",
                        batch_label, retry + 1, MAX_BATCH_RETRIES,
                    )
                    _dump_summarize_debug(ctx, batch_idx, retry, prompt, response)
                    continue

                batch_keys = parsed.get("key_steps_identified", [])
                batch_redundant = parsed.get("redundant_steps_identified", [])
                _dump_summarize_debug(ctx, batch_idx, retry, prompt, response)

                # key_step 直接覆盖（优先），redundant 只有不在 key 中才写入。
                # 只接受真实存在的 ordinal —— 防模型幻觉 step 0 / 越界 / 物理索引。
                for entry in batch_keys:
                    step = _step_int(entry)
                    if step in valid:
                        key_step_map[step] = entry
                        redundant_step_map.pop(step, None)

                for entry in batch_redundant:
                    step = _step_int(entry)
                    if step in valid and step not in key_step_map:
                        redundant_step_map[step] = entry

                logger.info(
                    "[_summarize_steps] %s: %d key + %d redundant (retry %d/%d)",
                    batch_label, len(batch_keys), len(batch_redundant),
                    retry + 1, MAX_BATCH_RETRIES,
                )
                success = True
                break

            except Exception as e:
                logger.warning(
                    "[_summarize_steps] %s LLM call failed (retry %d/%d): %s",
                    batch_label, retry + 1, MAX_BATCH_RETRIES, e,
                )

        if not success:
            logger.error(
                "[_summarize_steps] %s FAILED after %d retries, skipping",
                batch_label, MAX_BATCH_RETRIES,
            )

        batch_idx += 1
        start += WINDOW_STRIDE

    all_keys = [key_step_map[s] for s in sorted(key_step_map)]
    all_redundant = [redundant_step_map[s] for s in sorted(redundant_step_map)]
    return all_keys, all_redundant


def _build_report(ctx: EvaluatorContext, verdict: str, steps: int,
                  key_steps: list | None = None,
                  redundant_steps: list | None = None) -> EvaluationReport:
    task = ctx.trajectory.get("task", "")
    passed_items = [
        {"checklist_id": c["id"], "description": c["description"], "evidence": c.get("evidence", "")}
        for c in ctx.checklist if c.get("result") == "passed"
    ]
    failed_items = [
        {"checklist_id": c["id"], "description": c["description"], "evidence": c.get("evidence", "")}
        for c in ctx.checklist if c.get("result") == "failed"
    ]
    root_cause = ""
    if failed_items:
        fd = "; ".join(f"\"{f['description']}\" ({str(f.get('evidence', ''))[:120]})" for f in failed_items)
        pd = "; ".join(f"\"{p['description']}\"" for p in passed_items) if passed_items else "none"
        if verdict == "fail":
            if len(failed_items) >= len(ctx.checklist):
                root_cause = f"All {len(ctx.checklist)} conditions failed. Core goal not achieved."
            else:
                # Monotone-prefix fail: an earlier core prerequisite failed while a
                # later item passed — that later pass is hollow (depends on the
                # broken prerequisite), so the whole task failed despite partial pass.
                root_cause = (
                    f"核心前置条件未达成（通过 {len(passed_items)}/{len(ctx.checklist)}: {pd}; "
                    f"失败: {fd}）。后续个别项虽通过，但依赖已失败的前置条件"
                    f"（如启动了播放但播的是错误内容），不改变整体失败判定。"
                )
    return EvaluationReport(
        session_id=ctx.session_id,
        task=task,
        verdict=verdict,
        checklist_summary={
            "total": len(ctx.checklist),
            "passed": ctx.passed_count(),
            "failed": ctx.failed_count(),
            "unchecked": ctx.unchecked_count(),
            "items": [
                {"id": c["id"], "description": c["description"],
                 "status": c.get("result") or "unchecked"}
                for c in ctx.checklist
            ],
        },
        failure_analysis={
            "verdict": verdict,
            "root_cause": root_cause,
            "passed_items": passed_items,
            "failed_items": failed_items,
            "key_steps_identified": key_steps if key_steps is not None else ctx.traced_steps,
            "redundant_steps_identified": redundant_steps if redundant_steps is not None else [],
            "verdict_basis": ctx._verdict_basis,
            "revisions": ctx._revisions,
            "evidence_log": ctx._evidence_log,
        },
        evaluator_steps_used=steps,
        checklist_source=ctx.checklist_source,
        checklist_metadata=ctx.checklist_metadata,
        step_visual_summary_metadata=getattr(ctx, "_step_summary_metadata", {}),
        check_audit=getattr(ctx, "_check_audit", {}),
        resume_metadata=getattr(ctx, "_resume_metadata", {}),
    )


def evaluate_session(session_id: str, *, resume_from: str = "", resume_auto: bool = False,
                     evaluation_mode: str = "checklist", checklist_policy: str = "auto") -> str:
    """Evaluate by session ID (legacy .evaluator/ path or .runs/ search)."""
    # Try new .runs/ location first
    runs_dir = config.PROJECT_ROOT / ".runs"
    if runs_dir.exists():
        for run_dir in runs_dir.iterdir():
            for case_dir in run_dir.iterdir():
                tj = case_dir / "_trajectory_for_evaluate.json"
                if tj.exists():
                    with open(tj, "r", encoding="utf-8") as f:
                        t = json.load(f)
                    if t.get("session_id") == session_id:
                        return _eval_and_save(str(tj), session_id, str(case_dir),
                                              resume_from=resume_from, resume_auto=resume_auto,
                                              evaluation_mode=evaluation_mode, checklist_policy=checklist_policy)
    # Fall back to legacy path
    legacy = config.PROJECT_ROOT / ".evaluator" / f"session_{session_id}" / "trajectory.json"
    if not legacy.exists():
        raise FileNotFoundError(f"Trajectory not found for session {session_id}")
    return _eval_and_save(str(legacy), session_id,
                          str(config.PROJECT_ROOT / ".evaluator" / f"session_{session_id}"),
                          resume_from=resume_from, resume_auto=resume_auto,
                          evaluation_mode=evaluation_mode, checklist_policy=checklist_policy)


def _eval_and_save(trajectory_path: str, session_id: str, output_dir: str,
                   key_elements: str = "", skip_unreferenced_steps: bool = True,
                   resume_from: str = "", resume_auto: bool = False,
                   evaluation_mode: str = "checklist", report_suffix: str = "",
                   case_id: str = "", checklist_policy: str = "auto") -> str:
    import hashlib
    input_digest = sha256_file(trajectory_path)
    report = evaluate_trajectory(trajectory_path, session_id, output_dir=output_dir,
                                 key_elements=key_elements,
                                 skip_unreferenced_steps=skip_unreferenced_steps,
                                 resume_from=resume_from, resume_auto=resume_auto,
                                 evaluation_mode=evaluation_mode,
                                 case_id=case_id, checklist_policy=checklist_policy)
    if sha256_file(trajectory_path) != input_digest:
        raise ValueError("trajectory changed during evaluation")
    ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    suffix = str(report_suffix or "").strip()
    if suffix and not suffix.startswith("_"):
        suffix = "_" + suffix
    output_path = Path(output_dir) / f"_report_{ts_tag}{suffix}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.to_dict()
    payload["trajectory_sha256"] = input_digest
    payload["checklist_policy"] = checklist_policy
    payload.update(current_identity())
    payload["evaluator_config_digest"] = str(getattr(config, "EVALUATOR_CONFIG_DIGEST", ""))
    payload["key_elements_sha256"] = hashlib.sha256(key_elements.encode("utf-8")).hexdigest()
    gold_path = Path(str(report.checklist_metadata.get("source_path") or ""))
    payload["checklist_sha256"] = hashlib.sha256(io_path(gold_path).read_bytes()).hexdigest() if io_path(gold_path).is_file() else ""
    payload["evaluation_status"] = "complete" if report.checklist_summary.get("unchecked", 0) == 0 and report.checklist_summary.get("total", 0) else "incomplete"
    payload["step_visual_summary_status"] = report.step_visual_summary_metadata.get("status", "unknown")
    payload["check_audit_status"] = report.check_audit.get("status", "unknown")
    payload["verdict_disputed"] = bool(report.check_audit.get("disagreement_unresolved", False))
    payload["model_config"] = {
        "text_model": getattr(config, "EVALUATOR_TEXT_MODEL", ""),
        "vision_model": getattr(config, "EVALUATOR_VISION_MODEL", ""),
        "check_model": report.check_audit.get("check_model", _CHECK_EVALUATOR_MODEL),
    }
    payload["created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    payload["evaluation_mode"] = evaluation_mode
    payload["excluded_evaluator_rule_ids"] = sorted(_EXCLUDED_EVALUATION_RULE_IDS)
    payload["resume"] = {
        **(getattr(report, "resume_metadata", {}) or {}),
        "checkpoint_path": str(Path(output_dir) / "_evaluator_checkpoint.json"),
    }
    temporary_path = output_path.with_name(output_path.name + ".tmp")
    with open(temporary_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(temporary_path, output_path)
    logger.info("[Evaluator] report written to %s verdict=%s", output_path, report.verdict)
    return str(output_path)


def refresh_step_summaries_only(trajectory_path: str, session_id: str = "",
                                output_dir: str = "") -> dict:
    """Write/update v2 step summaries without running checklist evaluation."""
    trajectory_file = Path(trajectory_path).resolve()
    trajectory = json.loads(trajectory_file.read_text(encoding="utf-8-sig"))
    sid = session_id or str(trajectory.get("session_id") or "unknown")
    target_dir = Path(output_dir).resolve() if output_dir else trajectory_file.parent
    ctx = EvaluatorContext(trajectory=trajectory, session_id=sid, _vision_llm=LLMClient())
    ctx._trajectory_path = str(trajectory_file)
    ctx._step_summary_path = str(target_dir / "_evaluator_step_summaries_v2.json")
    _collect_missing_vision_observations(ctx)
    return dict(ctx._step_summary_metadata)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Evaluate a GUI agent session trajectory")
    parser.add_argument("--session-id", required=True, help="Session ID to evaluate")
    parser.add_argument("--trajectory-path", default="", help="Direct path to trajectory JSON (bypasses search)")
    parser.add_argument("--case-id", default="", help="Explicit benchmark/use-case id from the run path; takes precedence for gold lookup")
    parser.add_argument("--key-elements", default="", help="Page key elements from xlsx (as checklist generation reference)")
    parser.add_argument("--skip-unreferenced-steps", action="store_true",
                        default=False,
                        help="Optional legacy mode: skip steps not referenced by checklist verdicts (default: analyze all steps)")
    parser.add_argument("--resume-report", default="", help="Resume from a compatible partial report or evaluator checkpoint")
    parser.add_argument("--resume-auto", action="store_true", help="Resume from the case checkpoint or latest compatible partial report")
    parser.add_argument("--mode", choices=("checklist", "audit"), default="checklist",
                        help="Default checks every checklist item; audit runs the optional independent checker")
    parser.add_argument("--checklist-source", choices=("auto", "llm"), default="auto",
                        help="llm generates a fresh checklist without gold or heuristic fallback")
    parser.add_argument("--exclude-evaluator-rule", action="append", default=[],
                        help="Exclude this evaluator skill rule ID in memory for this process; repeatable")
    parser.add_argument("--report-suffix", default="",
                        help="Optional suffix appended to the report filename, e.g. _wo_evaluator_skills")
    parser.add_argument("--output-dir", default="",
                        help="Optional isolated directory for report, dialog, checkpoint, and step summaries")
    parser.add_argument("--step-summaries-only", action="store_true",
                        help="Only generate/update _evaluator_step_summaries_v2.json; do not write an evaluation report")
    args = parser.parse_args()
    _EXCLUDED_EVALUATION_RULE_IDS = {str(rule_id).strip() for rule_id in args.exclude_evaluator_rule if str(rule_id).strip()}
    if (args.output_dir or _EXCLUDED_EVALUATION_RULE_IDS or args.report_suffix or args.case_id) and not args.trajectory_path:
        parser.error("--output-dir, --report-suffix, --exclude-evaluator-rule, and --case-id require --trajectory-path")
    if args.trajectory_path:
        traj_path = Path(args.trajectory_path)
        out_dir = args.output_dir or str(traj_path.parent)
        if args.step_summaries_only:
            metadata = refresh_step_summaries_only(args.trajectory_path, args.session_id, out_dir)
            print(json.dumps(metadata, ensure_ascii=False, indent=2))
            raise SystemExit(0)
        report_path = _eval_and_save(str(traj_path), args.session_id, out_dir,
                                     key_elements=args.key_elements,
                                     skip_unreferenced_steps=args.skip_unreferenced_steps,
                                     resume_from=args.resume_report,
                                     resume_auto=args.resume_auto,
                                     evaluation_mode=args.mode,
                                     report_suffix=args.report_suffix,
                                     case_id=args.case_id, checklist_policy=args.checklist_source)
    else:
        if args.step_summaries_only:
            parser.error("--step-summaries-only requires --trajectory-path")
        report_path = evaluate_session(args.session_id, resume_from=args.resume_report,
                                       resume_auto=args.resume_auto, evaluation_mode=args.mode,
                                       checklist_policy=args.checklist_source)
    print(f"Report: {report_path}")
