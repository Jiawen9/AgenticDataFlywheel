from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class ActionStepRecord:
    action_index: int
    global_step: int
    action: dict
    activated_action_skills: list[str]
    screenshot_before: str
    screenshot_after: str
    ui_tree_before: str
    ui_tree_after: str
    action_overlay_path: str = ""
    action_skill_ids: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class SubtaskRecord:
    subtask_id: str
    subtask_desc: str
    subtask_result: dict
    action_skill_scores: dict[str, float] = field(default_factory=dict)
    action_example_scores: dict[str, float] = field(default_factory=dict)
    actions: list[ActionStepRecord] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class PlanStepRecord:
    plan_index: int
    plan_cycle: int
    plan_goal: str
    phase: str
    activated_plan_skills: list[str]
    plan_results: str
    screenshot_before_plan: str
    plan_skill_scores: dict[str, float] = field(default_factory=dict)
    subtasks: list[SubtaskRecord] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class FlatActionRecord:
    """Single atomic action record — all evidence inline, no state-machine dependency."""
    global_step: int
    action: dict                    # executable action dict
    action_desc: str                # human-readable description
    subtask_id: str                 # which todo this action belongs to
    subtask_desc: str               # the todo content/description
    screenshot_before: str          # absolute path
    screenshot_after: str           # absolute path
    ui_tree_before_raw: str         # absolute path to raw XML
    ui_tree_after_raw: str          # absolute path to raw XML
    action_overlay_path: str = ""   # absolute path to overlay image
    model_response_path: str = ""   # absolute path to model_response JSON
    action_skill_ids: list[str] = field(default_factory=list)  # loaded action skill IDs
    timestamp: str = ""             # ISO timestamp

    def to_dict(self):
        return asdict(self)


@dataclass
class HierarchicalTrajectory:
    task: str
    session_id: str
    plan_steps: list[PlanStepRecord] = field(default_factory=list)
    actions_flat: list[FlatActionRecord] = field(default_factory=list)
    final_orch_screenshot: str = ""  # Fix 1: agent判定完成时的最终截图

    def to_dict(self):
        return asdict(self)


@dataclass
class ChecklistItem:
    id: str
    description: str
    check_method: str  # "screenshot" | "ui_tree" | "combined"
    ui_check: dict | None = None
    visual_check: str = ""
    target_keywords: list[str] = field(default_factory=list)
    checked: bool = False
    result: str | None = None  # "passed" | "failed" | None
    evidence: str | None = None


@dataclass
class EvaluationReport:
    session_id: str
    task: str
    verdict: str  # "success" | "partial_success" | "fail"
    checklist_summary: dict = field(default_factory=dict)
    failure_analysis: dict = field(default_factory=dict)
    evaluator_steps_used: int = 0
    checklist_source: str = ""
    checklist_metadata: dict = field(default_factory=dict)
    step_visual_summary_metadata: dict = field(default_factory=dict)
    check_audit: dict = field(default_factory=dict)
    resume_metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)
