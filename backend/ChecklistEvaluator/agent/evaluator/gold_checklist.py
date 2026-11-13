"""Load and normalize human-authored gold checklists for the evaluator.

The current reviewed gold artifact is ``task_specs_gold_v3.json``.  Its
milestones are converted to the evaluator's checklist schema here.  The older
``task_specs.json`` remains a fallback for checkouts without the reviewed file.
This module is the compatibility boundary:

* a task entry with an explicit ``checklist`` is accepted only as a legacy
  compatibility fallback;
* an entry with only ``milestones`` is deterministically converted to one
  checklist item per milestone;
* task matching is exact after whitespace/punctuation normalization.  We do not
  fuzzy-match task descriptions because a wrong checklist is worse than no
  checklist in an open-world phone environment.

The returned metadata is intentionally kept with the evaluator report so that
future skill-evolve experiments can distinguish ``gold`` from ``llm``
checklists without inspecting logs.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
try:
    from file_access import io_path
except ImportError:
    # The platform profile catalogue loads this validator by exact file path,
    # without importing the isolated runtime's generic `agent` package.
    from backend.ChecklistEvaluator.file_access import io_path

logger = logging.getLogger(__name__)

_VALID_CHECK_METHODS = {"screenshot", "ui_tree", "combined"}
_PUNCT_RE = re.compile(r"[\s\u3000\u200b\uFEFF]+")


def default_gold_checklist_path() -> Path:
    """Return the configured task-spec artifact path.

    ``task_specs_gold_v3.json`` is the current reviewed gold source.  Keep the
    original ``task_specs.json`` as a compatibility fallback for checkouts that
    have not received the reviewed version yet.
    """
    import config
    configured = os.environ.get("GUI_AGENT_GOLD_CHECKLIST_PATH", "")
    if not configured:
        configured = getattr(config, "EVALUATOR_GOLD_CHECKLIST_PATH", "")
    if configured:
        return Path(configured)
    annotation_dir = config.PROJECT_ROOT / "skill-evolve" / "t0_annotation"
    reviewed = annotation_dir / "task_specs_gold_v3.json"
    if io_path(reviewed).is_file():
        return reviewed
    return annotation_dir / "task_specs.json"


def validate_gold_file(path: str | Path) -> dict[str, dict]:
    """Validate every native entry; malformed gold must not become LLM fallback."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    raw = json.loads(io_path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique)
    tasks = raw.get("tasks", raw) if isinstance(raw, dict) else None
    if not isinstance(tasks, dict):
        raise ValueError("gold checklist must be a task-id mapping")
    for task_id, spec in tasks.items():
        if not isinstance(task_id, str) or not task_id.strip() or not isinstance(spec, dict):
            raise ValueError("invalid gold task entry")
        if not isinstance(spec.get("task", spec.get("task_text")), str) or not _spec_task(spec).strip():
            raise ValueError(f"gold task {task_id} has no task text")
        source = spec.get("checklist") if _has_explicit_checklist(spec) else spec.get("milestones")
        if not isinstance(source, list) or not source or any(not isinstance(item, dict) for item in source):
            raise ValueError(f"gold task {task_id} has no valid checklist/milestones")
        items = normalize_checklist_items(spec)
        if len(items) != len(source) or len({item["id"] for item in items}) != len(items):
            raise ValueError(f"gold task {task_id} contains invalid or duplicate checklist items")
        for item in source:
            if item.get("check_method") and item["check_method"] not in _VALID_CHECK_METHODS:
                raise ValueError(f"gold task {task_id} has invalid check_method")
    return tasks


def load_gold_checklist(task: str, trajectory: dict | None = None, *,
                        strict: bool | None = None, path: str | Path | None = None) -> dict | None:
    """Resolve a task to a normalized gold checklist.

    Returns ``None`` when the artifact is missing, malformed, or contains no
    exact match.  A missing gold entry is a normal condition: the caller must
    then fall back to the existing LLM-generated checklist path.
    """
    strict = os.environ.get("GUI_AGENT_EVALUATOR_STRICT_RULES") == "true" if strict is None else strict
    path = Path(path) if path is not None else default_gold_checklist_path()
    if not io_path(path).is_file():
        if strict:
            raise ValueError(f"gold checklist file missing: {path}")
        logger.debug("[gold checklist] artifact not found: %s", path)
        return None

    try:
        tasks = validate_gold_file(path) if strict else _task_mapping(json.loads(io_path(path).read_text(encoding="utf-8")))
    except Exception as exc:
        if strict:
            raise ValueError(f"invalid gold checklist: {exc}") from exc
        logger.warning("[gold checklist] cannot read %s: %s", path, exc)
        return None

    if not tasks:
        logger.warning("[gold checklist] no task entries found in %s", path)
        return None

    trajectory = trajectory or {}
    candidates = _task_id_candidates(trajectory)
    matched_ids = list(dict.fromkeys(candidate for candidate in candidates if candidate in tasks))
    if strict and len(matched_ids) > 1:
        raise ValueError(f"conflicting gold task ids: {matched_ids}")
    matched_id = matched_ids[0] if matched_ids else ""
    match_method = "task_id" if matched_id else ""
    if strict and matched_id and normalize_task_text(_spec_task(tasks[matched_id])) != normalize_task_text(task):
        raise ValueError(f"gold task id {matched_id} conflicts with trajectory task text")

    if not matched_id:
        normalized_task = normalize_task_text(task)
        matches = [
            task_id for task_id, spec in tasks.items()
            if normalize_task_text(_spec_task(spec)) == normalized_task and normalized_task
        ]
        if len(matches) == 1:
            matched_id = matches[0]
            match_method = "exact_task_text"
        elif len(matches) > 1:
            if strict:
                raise ValueError(f"ambiguous exact gold task text match: {matches}")
            logger.warning(
                "[gold checklist] ambiguous exact task match for %r: %s",
                task, matches,
            )
            return None

    if not matched_id:
        logger.debug("[gold checklist] no exact match for task=%r", task)
        return None

    spec = tasks[matched_id]
    items = normalize_checklist_items(spec)
    if not items:
        if strict:
            raise ValueError(f"gold task {matched_id} contains no checklist")
        logger.warning("[gold checklist] matched %s but it has no checklist/milestones", matched_id)
        return None

    return {
        "checklist": items,
        "task_id": matched_id,
        "task": _spec_task(spec),
        "app": str(spec.get("app", "")) if isinstance(spec, dict) else "",
        "source_path": str(path),
        "match_method": match_method,
        "schema": "gold-checklist/v1-compatible",
        "derived_from": "checklist" if _has_explicit_checklist(spec) else "milestones",
    }


def normalize_task_text(value: Any) -> str:
    """Normalize only formatting, not semantics, for exact task matching."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _PUNCT_RE.sub("", text)
    return text.strip()


def normalize_checklist_items(spec: dict) -> list[dict]:
    """Convert a task spec into evaluator-compatible, ordered checklist items."""
    if not isinstance(spec, dict):
        return []

    explicit = spec.get("checklist")
    if isinstance(explicit, list) and explicit:
        source_items = explicit
        derived_from = "checklist"
    else:
        source_items = spec.get("milestones")
        derived_from = "milestones"

    if not isinstance(source_items, list):
        return []

    normalized: list[dict] = []
    for index, source in enumerate(source_items, 1):
        if not isinstance(source, dict):
            continue
        milestone_id = str(source.get("milestone_id") or source.get("id") or f"m{index}").strip()
        item_id = str(source.get("id") or f"gold_{milestone_id}").strip()
        if derived_from == "milestones" and not item_id.startswith("gold_"):
            item_id = f"gold_{item_id}"

        name = str(source.get("name") or source.get("title") or "").strip()
        verify = str(source.get("verify") or source.get("description") or name).strip()
        if not verify:
            continue
        description = verify if not name or verify == name else f"{name}：{verify}"

        method = str(source.get("check_method") or "screenshot").strip().lower()
        if method not in _VALID_CHECK_METHODS:
            method = "screenshot"

        entities = _as_string_list(source.get("entities"))
        keywords = _as_string_list(source.get("target_keywords"))
        if not keywords:
            # Placeholder entities such as 「第二部剧名」 are instructions,
            # not literal screen text.  Keep them in the description but do
            # not make the image tool search for the placeholder string.
            keywords = [entity for entity in entities if not _looks_like_placeholder(entity)]

        item = {
            "id": item_id,
            "description": description,
            "check_method": method,
            "target_keywords": keywords[:8],
            "checked": False,
            "result": None,
            "evidence": None,
            # Gold provenance and milestone semantics are useful to the later
            # DTR/evolve layer, while remaining harmless to the current loop.
            "gold": True,
            "gold_milestone_id": milestone_id,
            "gold_order": index,
            "state_type": str(source.get("state_type") or "").strip(),
            "entities": entities,
            "verify": verify,
        }
        if isinstance(source.get("ui_check"), dict):
            item["ui_check"] = source["ui_check"]
        if source.get("visual_check"):
            item["visual_check"] = str(source["visual_check"])
        for field_name in ("not_count", "evidence_requirements", "acceptance_mode"):
            value = source.get(field_name)
            if value:
                item[field_name] = value
        normalized.append(item)

    return normalized


def _task_mapping(raw: Any) -> dict[str, dict]:
    if not isinstance(raw, dict):
        return {}
    tasks = raw.get("tasks", raw)
    if not isinstance(tasks, dict):
        return {}
    return {str(task_id): spec for task_id, spec in tasks.items() if isinstance(spec, dict)}


def _spec_task(spec: dict) -> str:
    return str(spec.get("task") or spec.get("task_text") or "")


def _has_explicit_checklist(spec: dict) -> bool:
    return isinstance(spec.get("checklist"), list) and bool(spec.get("checklist"))


def _task_id_candidates(trajectory: dict) -> list[str]:
    candidates: list[str] = []
    for key in ("task_id", "task_spec_id", "case_id", "benchmark_task_id", "test_id"):
        value = trajectory.get(key)
        if value is not None and str(value).strip():
            candidates.append(str(value).strip())
    return candidates


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _looks_like_placeholder(value: str) -> bool:
    text = value.strip()
    if not text:
        return True
    placeholder_tokens = ("剧名", "歌曲名", "标题", "专辑名", "第", "最新")
    return any(token in text for token in placeholder_tokens)
