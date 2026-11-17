"""One frozen row projection for complete selected-trajectory exports.

No model requests, evidence copies or mutation of the correction draft occur here.
Legacy SFT/RL and source-shaped workbook exports deliberately remain separate.
"""
from copy import deepcopy

EXPORT_PROFILE = "corrected_selection_v1"


def selection_records(groups, candidates=()):
    metadata = {(str(item.get("task_id")), str(item.get("trajectory_id"))): item for item in candidates}
    return [{**deepcopy(metadata.get((str(group["task_id"]), str(group["meta_task"])), {})),
             "task_id": group["task_id"], "trajectory_id": group["meta_task"],
             "group_id": group["group_id"], "goal": group.get("task", ""),
             "trajectory_count": 1, "step_count": len(group.get("rows", []))} for group in groups]


def selected_identity(items):
    result = {}
    for item in items:
        result.setdefault(str(item["task_id"]), []).append(str(item["trajectory_id"]))
    return result


def project_selected(*, session, snapshot, table, selection, cot_policy, tree_payload, quality_payload):
    from .service import _overlay_row
    from ..export_markers import annotate_rows
    if cot_policy not in {"generate", "skip"}:
        raise ValueError("COT 策略必须为 generate 或 skip")
    chosen = {item["group_id"] for item in selection["tasks"]}
    active = next(iter(table["sheets"]))
    source_rows = table["sheets"][active]
    columns = list(table.get("columns", {}).get(active) or dict.fromkeys(key for row in source_rows for key in row))
    for name in ("actions_box", "summary", "thought", "sop", "is_refined", "is_key_step"):
        if name not in columns:
            columns.append(name)
    groups, edits, cot, values_by_row = [], {}, {}, {}
    number = 2
    for group in snapshot["groups"]:
        if group["group_id"] not in chosen:
            continue
        frozen = {**deepcopy(group), "export": True, "trajectory_id": group["meta_task"], "rows": []}
        for base in group["rows"]:
            source_number = int(base["excel_row"])
            edit = session.get("row_edits", {}).get(str(source_number), {})
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
    return {"schema_version": 3, "export_profile": EXPORT_PROFILE, "cot_policy": cot_policy,
            "refinement_contract": "effective_action_sop_v1", "session_id": session.get("session_id"),
            "batch_id": selection["batch_id"], "tree_run_id": selection.get("tree_run_id"),
            "selection": frozen_selection, "groups": groups, "row_edits": edits, "cot": cot,
            "task_fingerprints": deepcopy(session.get("task_fingerprints") or selection.get("task_fingerprints", {})),
            "columns": {active: columns}, "sheets": {active: list(values_by_row.values())}}
