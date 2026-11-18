"""One frozen row projection for complete selected-trajectory exports.

No model requests, evidence copies or mutation of the correction draft occur here.
Legacy SFT/RL and source-shaped workbook exports deliberately remain separate.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

from .export_policy import EXPORT_PROFILE, OPD_PREFIX_PROFILE, opd_cutoff_row, validate_export_policy


def _relative_path(value, raw_root):
    image = str(value or "").replace("\\", "/")
    path = Path(image)
    if not image or ".." in PurePosixPath(image).parts:
        return None
    if path.is_absolute():
        if not raw_root:
            return None
        try:
            return PurePosixPath(path.resolve().relative_to(Path(raw_root).resolve()).as_posix())
        except ValueError:
            return None
    return PurePosixPath(image)


def _trajectory_directory(group, raw_root):
    """Use registered row paths and a bounded metadata-file lookup, not a scan."""
    from ..file_io import io_path
    paths = []
    identities = set()
    for row in group["rows"]:
        merged = {**(row.get("values") or {}), **row}
        for name in ("source_trajectory_id", "文件夹名"):
            if merged.get(name):
                identities.add(str(merged[name]))
        for name in ("image", "xml", "logprobs_path", "model_response_path"):
            relative = _relative_path(merged.get(name), raw_root)
            if relative is not None:
                paths.append(relative)
    if not paths:
        return None
    common = PurePosixPath(os.path.commonpath([p.parent.as_posix() for p in paths]).replace("\\", "/"))
    if common == PurePosixPath("."):
        return None
    for parent in (common, *common.parents):
        if parent == PurePosixPath("."):
            break
        # The original trajectory metadata is an explicit owner. This also
        # handles short storage aliases and all screenshots under screens/.
        if raw_root and io_path(Path(raw_root) / parent / "_trajectory_for_evaluate.json").is_file():
            return parent
        if parent.name in identities:
            return parent
    return common


def _training_image(row, group, batch_id, raw_root, directory):
    """Plan cloud names from registered source paths, never displayed IDs."""
    from ..data_publishing.upload_sources import _component
    relative = _relative_path(row.get("image"), raw_root)
    if relative is None or directory is None:
        return "", ""
    try:
        suffix = relative.relative_to(directory).as_posix()
    except ValueError:
        return "", ""
    identity = hashlib.sha256(json.dumps([str(batch_id), str(raw_root or ""), directory.as_posix()], ensure_ascii=False).encode()).hexdigest()[:20]
    case = row.get("collection_case_id") or row.get("case_id") or group.get("task_id") or "unknown"
    target = f"{_component(str(batch_id))}/{_component(str(case))}/{_component(directory.name)}__{identity}"
    return f"{target}/{suffix}", target


def selection_records(groups, candidates=()):
    metadata = {(str(item.get("task_id")), str(item.get("trajectory_id"))): item for item in candidates}
    return [{**deepcopy(metadata.get((str(group["task_id"]), str(group["meta_task"])), {})),
             "task_id": group["task_id"], "trajectory_id": group["meta_task"],
             "group_id": group["group_id"], "goal": group.get("task", ""),
             "trajectory_count": 1, "step_count": len(group.get("rows", [])),
             **({"opd_cutoff_step": group["opd_cutoff_step"]} if "opd_cutoff_step" in group else {})}
            for group in groups]


def selected_identity(items):
    result = {}
    for item in items:
        result.setdefault(str(item["task_id"]), []).append(str(item["trajectory_id"]))
    return result


def project_selected(*, session, snapshot, table, selection, cot_policy, tree_payload, quality_payload,
                     raw_root=None, training_prefix="", export_profile=EXPORT_PROFILE):
    from .service import _overlay_row
    from ..export_markers import annotate_rows
    validate_export_policy(export_profile, cot_policy)
    chosen = {item["group_id"] for item in selection["tasks"]}
    active = next(iter(table["sheets"]))
    source_rows = table["sheets"][active]
    columns = list(table.get("columns", {}).get(active) or dict.fromkeys(key for row in source_rows for key in row))
    for name in ("actions_box", "summary", "thought", "sop", "is_refined", "is_key_step",
                 "task", "task_id", "trajectory_id", "step", "history", "training_image_relative", "training_trajectory_relative", "training_image_prefix"):
        if name not in columns:
            columns.append(name)
    groups, edits, cot, values_by_row = [], {}, {}, {}
    number = 2
    raw_root = raw_root or (session.get("source_snapshot") or {}).get("raw_root") or tree_payload.get("raw_root")
    from ..data_publishing.paired_training import current_box
    from ..data_publishing.training_jsonl import parse_action, _description
    row_edits = session.get("row_edits", {})
    for group in snapshot["groups"]:
        if group["group_id"] not in chosen:
            continue
        frozen = {**deepcopy(group), "export": True, "trajectory_id": group["meta_task"], "rows": []}
        history = []
        directory = _trajectory_directory(group, raw_root)
        cutoff = None
        if export_profile == OPD_PREFIX_PROFILE:
            cutoff = opd_cutoff_row(group["rows"], row_edits)
            frozen["opd_cutoff_step"] = cutoff.get("step") if cutoff else None
        for base in group["rows"]:
            source_number = int(base["excel_row"])
            edit = row_edits.get(str(source_number), {})
            if edit.get("deleted"):
                continue
            stored_cot = session.get("cot", {}).get(str(source_number), {})
            if not isinstance(stored_cot, dict):
                stored_cot = {}
            task_version = (session.get("task_fingerprints") or selection.get("task_fingerprints", {})).get(group["task_id"])
            current_cot = (bool(base.get("source_fingerprint"))
                           and stored_cot.get("source_fingerprint") == base["source_fingerprint"]
                           and (not task_version or stored_cot.get("task_fingerprint") == task_version))
            # This profile only reuses generated text bound to this frozen step
            # and task. Legacy exports retain their existing reuse semantics.
            overlay = session
            if stored_cot and not current_cot:
                overlay = {**session, "cot": {key: value for key, value in session.get("cot", {}).items()
                                             if key != str(source_number)}}
            row = _overlay_row(session.get("session_id", ""), overlay, base)
            value = deepcopy(source_rows[source_number - 2])
            if "sop" not in edit and not row.get("sop"):
                row["sop"] = base.get("values", {}).get("sop", value.get("sop", ""))
            row["sop_source"] = "manual" if "sop" in edit else "original"
            row.update(source_excel_row=int(base.get("source_excel_row", source_number)), excel_row=number,
                       original_action=edit.get("original_actions", base.get("actions", "")),
                       original_sop=base.get("values", {}).get("sop", base.get("sop", "")))
            row.pop("image_url", None)
            if cot_policy == "skip":
                row.update(thought="", thought_source="policy_skip", cot_status="skipped")
            value["actions" if "actions" in columns else "action"] = row["actions"]
            for key in ("actions_box", "summary", "thought", "sop"):
                value[key] = row.get(key, "")
            try:
                action = parse_action(row["actions"])
                value["actions_box"] = current_box(row, action)
                # A source SOP cannot describe a different manually edited action.
                if row.get("action_edited") and not row.get("sop_edited"):
                    value["sop"] = ""
                value["summary"] = _description(row, action)
            except (ValueError, TypeError):
                action = None
                value["actions_box"] = ""
            description = _description(row, action)
            cloud_image, cloud_directory = _training_image(row, group, selection["batch_id"], raw_root, directory)
            value.update(task=group.get("task", ""), task_id=group["task_id"],
                         trajectory_id=group["meta_task"], step=row.get("step", len(history) + 1),
                         history="\n".join(history),
                         training_image_relative=cloud_image, training_trajectory_relative=cloud_directory,
                         training_image_prefix=training_prefix)
            history.append(f"Step {len(history) + 1}: {description}")
            # Coordinates and existing source identity columns are never rescaled.
            if "coordinate_info" in edit:
                value["coordinate_info"] = deepcopy(edit["coordinate_info"])
                if "coordinate_info" not in columns:
                    columns.append("coordinate_info")
            values_by_row[number] = value
            frozen["rows"].append(row)
            if edit:
                edits[str(number)] = deepcopy(edit)
            if cot_policy != "skip" and current_cot and row.get("cot_status") in {"generated", "manual"}:
                if session.get("cot", {}).get(str(source_number)):
                    cot[str(number)] = deepcopy(session["cot"][str(source_number)])
            number += 1
            if base is cutoff:
                break
        if not frozen["rows"]:
            raise ValueError(f"入选轨迹 {group['meta_task']} 没有保留步骤")
        groups.append(frozen)
    if len(groups) != len(chosen) or not groups:
        raise ValueError("请选择至少一条有保留步骤的有效轨迹")
    groups = annotate_rows(groups, tree_payload=tree_payload, quality_payload=quality_payload, row_edits=edits)
    for group in groups:
        for row in group["rows"]:
            values_by_row[row["excel_row"]].update(is_refined=row["is_refined"], is_key_step=row["is_key_step"])
    frozen_selection = {**deepcopy(selection), "schema_version": 2,
                        "tasks": selection_records(groups, selection.get("tasks", []))}
    frozen_selection["selected_trajectory_ids"] = selected_identity(frozen_selection["tasks"])
    frozen_selection["selected_groups"] = [item["group_id"] for item in frozen_selection["tasks"]]
    return {"schema_version": 3, "export_profile": export_profile, "cot_policy": cot_policy,
            "refinement_contract": "effective_action_sop_v1", "session_id": session.get("session_id"),
            "batch_id": selection["batch_id"], "tree_run_id": selection.get("tree_run_id"),
            "selection": frozen_selection, "groups": groups, "row_edits": edits, "cot": cot,
            "task_fingerprints": deepcopy(session.get("task_fingerprints") or selection.get("task_fingerprints", {})),
            "columns": {active: columns}, "sheets": {active: list(values_by_row.values())}}
