from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class EvaluatorContext:
    trajectory: dict
    session_id: str
    _text_llm: object = None
    _vision_llm: object = None
    checklist: list[dict] = field(default_factory=list)
    traced_steps: list[int] = field(default_factory=list)
    steps_since_last_progress: int = 0

    # B6: evidence tracking
    screenshots_examined: dict[int, dict] = field(default_factory=dict)
    evidence_summary: str = ""

    # Dynamic memory: verdict basis + revision tracking
    _verdict_basis: dict[str, dict] = field(default_factory=dict)
    _pending_revision: list[dict] = field(default_factory=list)
    _evidence_log: list[dict] = field(default_factory=list)
    _revisions: list[dict] = field(default_factory=list)

    # Screen recording for verdict_basis inference (session: append→consume→clear)
    _last_screenshot_type: str = ""
    _evidence_session: list[dict] = field(default_factory=list)
    # [{global_step, path, type: "screenshot"|"ui_tree"}]
    # Vision evidence extracted by the VLM. The VLM never writes a verdict here;
    # the text evaluator reasons over this step-indexed journal.
    _vision_observations: dict[int, list[dict]] = field(default_factory=dict)
    # {global_step: [{query, before_path, after_path, before_description, ...}]}
    _step_summary_path: str = ""
    _step_summary_records: dict[int, dict] = field(default_factory=dict)
    _step_summary_metadata: dict = field(default_factory=dict)
    _check_audit: dict = field(default_factory=dict)
    _check_audit_path: str = ""
    _primary_eval_system: str = ""
    _primary_eval_messages: list[dict] = field(default_factory=list)
    _primary_eval_model: str = ""
    _checkpoint_path: str = ""
    _resume_metadata: dict = field(default_factory=dict)
    _evaluation_mode: str = "checklist"
    _checklist_policy: str = "auto"

    # E3: dialog file handle for tool trace logging
    _dialog_fh: object = None

    # 页面关键元素（来自 xlsx，作为 checklist 生成的参考）
    key_elements: str = ""

    # Checklist provenance.  ``gold`` means the task matched the human-authored
    # task spec and the checklist LLM generation was bypassed.
    checklist_source: str = ""
    checklist_metadata: dict = field(default_factory=dict)

    def record_screenshot_examined(self, global_step: int, which: str,
                                   findings: str = "") -> None:
        entry = self.screenshots_examined.setdefault(global_step, {})
        entry[which] = "seen"
        if findings:
            prev = entry.get("findings", "")
            entry["findings"] = f"{prev}; {findings}".strip("; ")
            # Add to evidence log for report
            self._evidence_log.append({
                "global_step": global_step,
                "screenshot": which,
                "finding": findings[:200],
            })

    def record_vision_observation(self, global_step: int, observation: dict) -> None:
        """Append objective VLM evidence to a step-indexed persistent journal."""
        try:
            key = int(global_step)
        except (TypeError, ValueError):
            key = 0
        self._vision_observations.setdefault(key, []).append(observation)

    def mark_checklist_item(self, item_id: str, result: str, evidence: str = "",
                            global_step: int = 0, screenshot_type: str = "",
                            reversible: bool = False, check_method: str = "") -> None:
        for item in self.checklist:
            if item.get("id") == item_id:
                old = self._verdict_basis.get(item_id)
                changed_result = not item.get("checked") or item.get("result") != result
                # Detect revision: same item, different result
                if old and old.get("result") != result:
                    self._revisions.append({
                        "item_id": item_id,
                        "from": old.get("result"),
                        "to": result,
                        "old_evidence": old.get("evidence", ""),
                        "new_evidence": evidence,
                    })
                item["checked"] = True
                item["result"] = result
                item["evidence"] = evidence
                if changed_result:
                    self.steps_since_last_progress = 0
                # Filter session evidence to the steps this verdict was made on.
                # Evidence entries are ordinal-keyed (resolved by the caller), so:
                #   global_step == 0  → no specific step → take ALL session evidence
                #   global_step == -1 → final-orchestrator sentinel → only the
                #                      entry recorded against the final screenshot
                #   global_step  > 0  → ordinal → exact match on that ordinal
                # rel_steps holds only positive ordinals, so verdict_basis.global_steps
                # can never contain 0 / -1 / an out-of-range physical file index.
                rel_steps: set[int] = set()
                if isinstance(global_step, int) and global_step > 0:
                    rel_steps.add(global_step)

                rel_shots = []
                rel_uis = []
                for e in self._evidence_session:
                    p = e.get("path", "")
                    if ".runs" in p:
                        p = p[p.find(".runs"):]
                    eg = e.get("global_step")
                    if global_step == 0:
                        include = True
                    elif global_step == -1:
                        include = (eg == -1)
                    else:
                        include = (isinstance(eg, int) and eg in rel_steps)
                    if not include:
                        continue
                    if e.get("type") == "ui_tree":
                        if p and p not in rel_uis:
                            rel_uis.append(p)
                    else:
                        if p and p not in rel_shots:
                            rel_shots.append(p)
                steps = sorted(rel_steps)

                # Auto-detect check_method from evidence types
                if not check_method:
                    if rel_shots and rel_uis:
                        check_method = "combined"
                    elif rel_shots:
                        check_method = "screenshot"
                    elif rel_uis:
                        check_method = "ui_tree"

                existing = self._verdict_basis.get(item_id)
                if existing:
                    existing["result"] = result
                    existing["evidence"] = evidence
                    for gs in steps:
                        if gs not in existing.setdefault("global_steps", []):
                            existing["global_steps"].append(gs)
                    existing["reversible"] = reversible
                    if check_method:
                        existing["check_method"] = check_method
                    for p in rel_shots:
                        if p not in existing.setdefault("screenshot_paths", []):
                            existing["screenshot_paths"].append(p)
                    for p in rel_uis:
                        if p not in existing.setdefault("ui_tree_paths", []):
                            existing["ui_tree_paths"].append(p)
                else:
                    self._verdict_basis[item_id] = {
                        "result": result,
                        "evidence": evidence,
                        "check_method": check_method,
                        "global_steps": steps,
                        "reversible": reversible,
                        "screenshot_paths": rel_shots,
                        "ui_tree_paths": rel_uis,
                    }
                return
        self.steps_since_last_progress += 1

    def detect_pending_revision(self, global_step: int, subtask_id: str = "") -> None:
        """Check if newly loaded screenshot might conflict with existing verdicts."""
        if not subtask_id:
            return
        for item_id, basis in list(self._verdict_basis.items()):
            if not basis.get("reversible"):
                continue
            bs = basis.get("global_step", 0)
            if bs and bs != global_step and abs(bs - global_step) <= 5:
                # Same subtask context, different step → potential conflict
                existing = [r for r in self._pending_revision if r["item_id"] == item_id]
                if not existing:
                    self._pending_revision.append({
                        "item_id": item_id,
                        "old_result": basis["result"],
                        "old_evidence": basis["evidence"],
                        "old_screenshot": f"{basis['screenshot']} at global_step={bs}",
                        "new_screenshot": f"loaded at global_step={global_step}",
                        "conflict_reason": "同一可逆条件区域，不同步骤的截图",
                        "suggested_action": f"对比 global_step={bs} 和 global_step={global_step} 的截图",
                    })

    def build_evidence_context(self) -> str:
        """Return a summary of what's been examined, verdict basis, pending revisions."""
        if not self.checklist and not self._vision_observations:
            return ""
        unchecked = [c for c in self.checklist if not c.get("checked")]
        examined_steps = sorted(self.screenshots_examined.keys())
        lines = [f"## Evidence Progress"]

        # Examined steps
        if examined_steps:
            step_details = []
            for gs in examined_steps:
                entry = self.screenshots_examined.get(gs, {})
                types = [k for k in entry if k != "findings"]
                finding = entry.get("findings", "")[:80]
                step_details.append(f"  step{gs}: {types}" + (f" → {finding}" if finding else ""))
            lines.append("Examined steps:")
            lines.extend(step_details[:10])  # progress display only

        # Full step-indexed vision journal. Tool responses remain in chat history;
        # this section makes chronological retrieval explicit and cumulative.
        if self._vision_observations:
            lines.append("\n## Vision Observation Journal (objective evidence only)")
            lines.append("VLM descriptions are observations, not verdicts. Reason across steps yourself.")
            for gs in sorted(self._vision_observations):
                label = "final screenshot" if gs == -1 else f"step#{gs}"
                for idx, obs in enumerate(self._vision_observations[gs], 1):
                    lines.append(f"\n### {label}.{idx}")
                    if obs.get("query"):
                        lines.append(f"- requested_focus: {obs['query']}")
                    if obs.get("before_path"):
                        lines.append(f"- before: {obs['before_path']}")
                    if obs.get("overlay_path"):
                        lines.append(f"- action_overlay: {obs['overlay_path']}")
                    if obs.get("after_path"):
                        lines.append(f"- after: {obs['after_path']}")
                    if obs.get("summary_status"):
                        lines.append(f"- visual_summary_status: {obs['summary_status']}")
                    if obs.get("evidence_registered"):
                        lines.append("- evidence_status: precomputed visual summary registered as checked image evidence")
                    if obs.get("image_roles_sent"):
                        lines.append(f"- images_read: {obs['image_roles_sent']}")
                    for field_name in (
                        "before_state_summary", "before_page", "before_visible_entities",
                        "overlay_target", "after_state_summary", "after_page",
                        "after_visible_entities", "observed_transition",
                        "before_description", "after_description",
                        "transition_description", "before_visible_text",
                        "after_visible_text", "uncertainty",
                    ):
                        value = str(obs.get(field_name, "")).strip()
                        if value:
                            lines.append(f"- {field_name}: {value}")

        # Verdict basis
        if self._verdict_basis:
            lines.append("\n## Verdict Basis (what each verdict was based on)")
            for item_id, basis in self._verdict_basis.items():
                rev_tag = " [可逆]" if basis.get("reversible") else ""
                steps = basis.get("global_steps", [])
                paths = basis.get("screenshot_paths", [])
                lines.append(
                    f"  {item_id} {basis.get('result', '?').upper()} ← "
                    f"steps={steps} paths={len(paths)} screenshots{rev_tag}"
                )

        # Pending revisions
        if self._pending_revision:
            lines.append("\n## ⚠️ Suggested Revisions")
            for rev in self._pending_revision[-3:]:  # latest 3
                lines.append(
                    f"  {rev['item_id']}: previously {rev['old_result'].upper()} based on {rev['old_screenshot']}. "
                    f"{rev['conflict_reason']}. → {rev['suggested_action']}"
                )

        # Remaining unchecked
        if unchecked:
            lines.append(f"\n## Remaining ({len(unchecked)}/{len(self.checklist)})")
            for c in unchecked:
                lines.append(f"  - {c['id']}: {c['description'][:100]}")

        # Previous findings
        if self.evidence_summary:
            lines.append(f"\n## Findings so far\n{self.evidence_summary[:400]}")
        return "\n".join(lines)

    def all_checked(self) -> bool:
        return len(self.checklist) > 0 and all(
            item.get("checked") and item.get("result") in {"passed", "failed"}
            for item in self.checklist
        )

    def unchecked_count(self) -> int:
        return sum(1 for item in self.checklist
                   if not item.get("checked") or item.get("result") not in {"passed", "failed"})

    def passed_count(self) -> int:
        return sum(1 for item in self.checklist if item.get("result") == "passed")

    def failed_count(self) -> int:
        return sum(1 for item in self.checklist if item.get("result") == "failed")


ToolHandler = Callable[["EvaluatorContext", dict], dict]


@dataclass
class ToolDef:
    name: str
    description: str
    schema: dict
    handler: ToolHandler


class EvaluatorToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool

    def definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.schema,
                },
            }
            for t in self._tools.values()
        ]

    def dispatch(self, name: str, arguments: dict, ctx: EvaluatorContext) -> dict:
        tool = self._tools.get(name)
        if tool is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return tool.handler(ctx, arguments)
        except Exception as exc:
            logger.exception("[EvaluatorTool] %s failed: %s", name, exc)
            return {"error": str(exc), "tool": name}


def build_default_registry() -> EvaluatorToolRegistry:
    from agent.evaluator.tools.generate_checklist import generate_verification_checklist
    from agent.evaluator.tools.locate_key_steps import locate_key_steps
    from agent.evaluator.tools.fetch_step_detail import fetch_step_detail
    from agent.evaluator.tools.query_ui_tree_diff import query_ui_tree_diff
    from agent.evaluator.tools.inspect_image_region import inspect_image_region
    from agent.evaluator.tools.detect_changes import detect_image_changes

    registry = EvaluatorToolRegistry()
    registry.register(ToolDef(
        name="generate_verification_checklist",
        description="Decompose the original task into a structured checklist of pass/fail criteria (positive assertions only). Each item specifies a check_method: screenshot, ui_tree, or combined.",
        schema={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The original task description to decompose"},
            },
            "required": ["task"],
        },
        handler=generate_verification_checklist,
    ))
    registry.register(ToolDef(
        name="locate_key_steps",
        description="Identify key steps in the trajectory where page state likely changed. Uses text analysis of actions + UI tree diff heuristic + screenshot hash comparison to score steps.",
        schema={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=locate_key_steps,
    ))
    registry.register(ToolDef(
        name="fetch_step_detail",
        description="Load the screenshot, UI tree, or action overlay for a specific action. Prefer global_step (flat lookup). Use list_all_screenshots to discover extra evidence on disk.",
        schema={
            "type": "object",
            "properties": {
                "global_step": {"type": "integer", "description": "Global step number — preferred lookup method. Uses actions_flat array."},
                "plan_index": {"type": "integer", "description": "Index into plan_steps array (0-based) — legacy fallback"},
                "subtask_id": {"type": "string", "description": "The subtask_id to look up — legacy fallback"},
                "subtask_index": {"type": "integer", "description": "Index into subtasks array — use this, NOT subtask_id"},
                "action_index": {"type": "integer", "description": "Index into actions array within the subtask (0-based)"},
                "detail_type": {
                    "type": "string",
                    "enum": [
                        "screenshot_before", "screenshot_after",
                        "ui_tree_before_extracted", "ui_tree_after_extracted",
                        "action_overlay",
                        "model_response",
                        "list_all_screenshots",
                    ],
                },
            },
            "required": ["detail_type"],
        },
        handler=fetch_step_detail,
    ))
    registry.register(ToolDef(
        name="query_ui_tree_diff",
        description="Search and compare the processed UI trees before and after an action. Prefer global_step (committed action ordinal); the tool maps it to the actual stepNNN files.",
        schema={
            "type": "object",
            "properties": {
                "global_step": {"type": "integer", "description": "Committed action ordinal (preferred); may differ from the stepNNN file index"},
                "plan_index": {"type": "integer"},
                "subtask_id": {"type": "string"},
                "subtask_index": {"type": "integer", "description": "Alternative to subtask_id"},
                "action_index": {"type": "integer"},
                "keyword": {"type": "string", "description": "Keyword or text to search for in UI tree nodes"},
            },
            "required": ["keyword"],
        },
        handler=query_ui_tree_diff,
    ))
    registry.register(ToolDef(
        name="inspect_image_region",
        description="Send before/after screenshots to VLM for objective description extraction. Prefer global_step; descriptions are accumulated by step for the text model to judge.",
        schema={
            "type": "object",
            "properties": {
                "global_step": {"type": "integer", "description": "Global step number — PREFERRED. Harness loads the action BEFORE/AFTER screenshots, action text, and intent automatically."},
                "image_path": {"type": "string", "description": "Path to the AFTER or standalone screenshot (legacy; use global_step instead)"},
                "target_keyword": {"type": "string", "description": "What to look for (e.g. '含早餐', 'toast message')"},
                "bbox": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "w": {"type": "integer"},
                        "h": {"type": "integer"},
                    },
                    "description": "Optional bounding box. If omitted, VLM localizes the target in the full image.",
                },
            },
            "required": ["target_keyword"],
        },
        handler=inspect_image_region,
    ))
    registry.register(ToolDef(
        name="detect_image_changes",
        description="Detect pixel-level changes between before/after screenshots via absdiff. Returns bounding boxes of changed regions. Use when you need to verify whether an action (tap, swipe) actually changed the screen — especially for small icon/button state toggles that VLM cannot distinguish. Compare the action's screenshot_before and screenshot_after.",
        schema={
            "type": "object",
            "properties": {
                "before_path": {"type": "string", "description": "Absolute path to before screenshot"},
                "after_path": {"type": "string", "description": "Absolute path to after screenshot"},
                "threshold": {"type": "integer", "description": "Grayscale diff threshold, default 15"},
            },
            "required": ["before_path", "after_path"],
        },
        handler=detect_image_changes,
    ))
    registry.register(ToolDef(
        name="update_checklist_item",
        description="Mark a single checklist item as passed or failed with supporting evidence.",
        schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "Checklist item ID (e.g. chk_01)"},
                "result": {"type": "string", "enum": ["passed", "failed"], "description": "Evaluation result"},
                "evidence": {"type": "string", "description": "What you observed that supports this verdict"},
            },
            "required": ["item_id", "result", "evidence"],
        },
        handler=lambda ctx, args: _update_item_handler(ctx, args),
    ))
    registry.register(ToolDef(
        name="finish",
        description="Call this when all checklist items have been evaluated. Submit the final verdict.",
        schema={
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["success", "partial_success", "fail"],
                },
            },
            "required": ["verdict"],
        },
        handler=lambda ctx, args: {"action": "finish", "verdict": args.get("verdict", "fail")},
    ))
    return registry


def _update_item_handler(ctx: EvaluatorContext, args: dict) -> dict:
    item_id = str(args.get("item_id", ""))
    result = str(args.get("result", "failed"))
    evidence = str(args.get("evidence", ""))
    if result not in {"passed", "failed"}:
        return {"error": "result must be passed or failed"}
    positions = {str(item.get("id") or ""): index for index, item in enumerate(ctx.checklist)}
    position = positions.get(item_id)
    first_unchecked = next((i for i, item in enumerate(ctx.checklist)
                            if not item.get("checked") or item.get("result") not in {"passed", "failed"}),
                           len(ctx.checklist))
    if position is None:
        return {"error": f"unknown checklist item: {item_id}"}
    if position > first_unchecked:
        return {"error": f"checklist order violation: evaluate index {first_unchecked} before {position}"}
    ctx.mark_checklist_item(item_id, result, evidence)
    return {
        "updated": item_id,
        "result": result,
        "checklist_progress": {
            "total": len(ctx.checklist),
            "checked": len([c for c in ctx.checklist if c.get("checked")]),
            "passed": ctx.passed_count(),
            "failed": ctx.failed_count(),
        },
    }
