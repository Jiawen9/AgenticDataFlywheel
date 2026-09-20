"""Current per-batch results, merged and invalidated at task granularity.

Stage revisions protect the files; task fingerprints protect independent work.
Adding task B must neither discard task A nor invalidate A's quality results.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .batch_operations import active_batch_lock
from .data_store import ArtifactStore, DATA_ROOT, RecordStore
from .stage_artifacts import quality_tables, write_payload_workbook, write_sidecar


class StaleTaskInput(ValueError):
    """An asynchronous result no longer matches its task's current input."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _root(root: Path | None) -> Path:
    return Path(root or DATA_ROOT).resolve()


def annotation_task_fingerprints(payload: dict) -> dict[str, str]:
    from .trajectory_context import row_task_id
    grouped: dict[str, list] = {}
    for name, rows in payload.get("sheets", {}).items():
        for row in rows:
            grouped.setdefault(row_task_id(row), []).append(row)
    return {task: digest(rows) for task, rows in grouped.items()}


def tree_task_fingerprints(payload: dict, config: dict) -> dict[str, str]:
    return {task: digest({"input": value, "config": config})
            for task, value in annotation_task_fingerprints(payload).items()}


def _read(store: ArtifactStore, batch_id: str, stage: str) -> tuple[dict | None, dict]:
    ref = store.get(batch_id, stage)
    return (ref, store.read_payload(ref)) if ref else (None, {})


def _rows_for_tasks(payload: dict, task_ids: set[str]) -> dict:
    """Subset either an annotation workbook or the three quality sheets."""
    from .trajectory_context import row_task_id
    value = deepcopy(payload)
    sheets = value.setdefault("sheets", {})
    trajectories = {str(row.get("trajectory_id")) for row in sheets.get("Trajectories", [])
                    if str(row.get("task_id")) in task_ids}
    for name, rows in list(sheets.items()):
        if name == "Steps":
            sheets[name] = [row for row in rows if str(row.get("trajectory_id")) in trajectories]
        elif name in {"Tasks", "Trajectories"}:
            sheets[name] = [row for row in rows if str(row.get("task_id")) in task_ids]
        else:
            sheets[name] = [row for row in rows if row_task_id(row) in task_ids]
    return value


def _merge_workbooks(previous: dict, incoming: dict, replaced: set[str]) -> dict:
    all_ids = {str(row.get("task_id")) for row in previous.get("sheets", {}).get("Tasks", [])} if "Tasks" in previous.get("sheets", {}) else set(annotation_task_fingerprints(previous))
    result = _rows_for_tasks(previous, all_ids - replaced)
    result.setdefault("schema_version", 1)
    for name, rows in incoming.get("sheets", {}).items():
        result.setdefault("sheets", {}).setdefault(name, []).extend(deepcopy(rows))
    for name, columns in incoming.get("columns", {}).items():
        result.setdefault("columns", {})[name] = list(dict.fromkeys(
            result.get("columns", {}).get(name, []) + columns))
    return result


def _current_tree_locked(store: ArtifactStore, batch_id: str) -> tuple[dict | None, dict]:
    reference, value = _read(store, batch_id, "04_tree")
    if not value:
        return reference, {}
    _, annotation = _read(store, batch_id, "02_annotation")
    current = annotation_task_fingerprints(annotation)
    expected = value.get("source_task_fingerprints", {})
    # Old data must be explicitly migrated before it becomes a current result.
    valid = {task for task in value.get("trees", {})
             if expected.get(task) is not None and expected[task] == current.get(task)}
    return reference, _filter_tree(value, valid)


def _filter_tree(value: dict, valid: set[str]) -> dict:
    result = deepcopy(value)
    for field in ("trees", "task_fingerprints", "source_task_fingerprints", "tree_hashes"):
        result[field] = {key: item for key, item in result.get(field, {}).items() if key in valid}
    result["tasks"] = [item for item in result.get("tasks", []) if item.get("task_id") in valid]
    for field in ("quality_input", "source_annotation"):
        result[field] = _rows_for_tasks(result.get(field, {}), valid)
    return result


def current_tree_payload(batch_id: str, root: Path | None = None) -> dict:
    store = ArtifactStore(_root(root))
    with store.batch_lock(batch_id):
        return _current_tree_locked(store, batch_id)[1]


def current_tree_batch(batch_id: str, root: Path | None = None) -> dict | None:
    store = ArtifactStore(_root(root))
    with store.batch_lock(batch_id):
        reference, value = _current_tree_locked(store, batch_id)
        if not value or not value.get("trees"):
            return None
        annotation = store.get(batch_id, "02_annotation")
        tasks = value.get("tasks", [])
        return {**{key: item for key, item in value.items()
                   if key not in {"trees", "quality_input", "source_annotation"}},
                "run_id": batch_id, "batch_id": batch_id,
                "task_ids": [item["task_id"] for item in tasks], "task_count": len(tasks),
                "total_original_steps": sum(item.get("original_step_count", 0) for item in tasks),
                "total_tree_steps": sum(item.get("tree_step_count", 0) for item in tasks),
                "annotation_version": annotation["version"] if annotation else None,
                "source_refs": [annotation] if annotation else [],
                "artifacts": [item for item in (store.get(batch_id, "03_observation"), reference) if item],
                "quality_input_file": "rubric_trajectories.xlsx", "quality_input_json": "rubric_trajectories.json"}


def list_current_tree_batches(root: Path | None = None) -> list[dict]:
    store = ArtifactStore(_root(root))
    values = [current_tree_batch(item["batch_id"], root) for item in store.list(stage="04_tree")]
    return sorted((value for value in values if value), key=lambda item: item.get("completed_at", ""), reverse=True)


def current_quality_payload(batch_id: str, root: Path | None = None) -> dict:
    store = ArtifactStore(_root(root))
    with store.batch_lock(batch_id):
        _, trees = _current_tree_locked(store, batch_id)
        reference, quality = _read(store, batch_id, "05_quality")
        valid = trees.get("tree_hashes", {})
        expected = quality.get("source_tree_hashes", {})
        tasks = [item for item in quality.get("tasks", [])
                 if item.get("task_id") in valid and expected.get(item["task_id"]) == valid[item["task_id"]]]
        valid_ids = {item["task_id"] for item in tasks}
        return {**quality, "run_id": batch_id, "batch_id": batch_id, "tasks": tasks,
                "task_fingerprints": {task: value for task, value in quality.get("task_fingerprints", {}).items()
                                      if task in valid_ids},
                "source_tree_hashes": {task: value for task, value in expected.items() if task in valid_ids},
                "artifact": reference, "updated_at": quality.get("completed_at")}


def assert_task_inputs(batch_id: str, expected: dict[str, str], root: Path | None = None) -> None:
    store = ArtifactStore(_root(root))
    with store.batch_lock(batch_id):
        _, annotation = _read(store, batch_id, "02_annotation")
        current = annotation_task_fingerprints(annotation)
        if any(current.get(task) != value for task, value in expected.items()):
            raise StaleTaskInput("所选任务的标注输入已变化，请使用当前数据重新建树")


def merge_tree_results(batch_id: str, incoming: dict, observation: dict,
                       observation_rows: list[dict], root: Path | None = None) -> dict:
    """Publish 03/04 together, preserving other tasks against the current input."""
    store = ArtifactStore(_root(root))
    replaced = set(incoming["trees"])
    with active_batch_lock(batch_id, store.root):
        assert_task_inputs(batch_id, incoming["source_task_fingerprints"], root)
        _, previous = _current_tree_locked(store, batch_id)
        if all(previous.get("task_fingerprints", {}).get(task) == incoming["task_fingerprints"].get(task)
               for task in replaced):
            return current_tree_batch(batch_id, root)
        old_observation_ref, old_observation = _read(store, batch_id, "03_observation")
        unchanged = set(previous.get("trees", {})) - replaced
        result = {**previous, **deepcopy(incoming), "schema_version": 2,
                  "run_id": batch_id, "batch_id": batch_id}
        for field in ("trees", "task_fingerprints", "source_task_fingerprints", "tree_hashes"):
            result[field] = {**{key: item for key, item in previous.get(field, {}).items() if key in unchanged},
                             **deepcopy(incoming.get(field, {}))}
        result["tasks"] = sorted([item for item in previous.get("tasks", []) if item["task_id"] in unchanged]
                                 + deepcopy(incoming["tasks"]), key=lambda item: item["task_id"])
        for field in ("quality_input", "source_annotation"):
            result[field] = _merge_workbooks(previous.get(field, {}), incoming.get(field, {}), replaced)
        trajectories = [item for item in old_observation.get("trajectories", []) if item.get("task_id") in unchanged]
        trajectories += deepcopy(observation.get("trajectories", []))
        rows = [item for item in old_observation.get("rows", []) if item.get("任务编号") in unchanged] + observation_rows
        observed = {**observation, "schema_version": 2, "trajectories": trajectories, "rows": rows,
                    "task_fingerprints": result["task_fingerprints"],
                    "source_task_fingerprints": result["source_task_fingerprints"]}
        annotation = store.get(batch_id, "02_annotation")
        changed = {task for task in replaced if previous.get("tree_hashes", {}).get(task) != result["tree_hashes"].get(task)}
        store.publish_many(batch_id, [
            {"stage": "03_observation", "payload": observed, "tables": {"Observation与中间态": rows},
             "source_refs": [annotation] if annotation else [], "metadata": {"task_ids": sorted(result["trees"])}},
            {"stage": "04_tree", "payload": result, "source_stages": ["03_observation"],
             "metadata": {"run_id": batch_id, "task_ids": sorted(result["trees"])}},
        ], record_entries=[invalidation_record_entry(store, batch_id, "tree", changed)])
        drain_batch_invalidations(batch_id, root)
        return current_tree_batch(batch_id, root)


def merge_quality_results(batch_id: str, task_results: list[dict], expected_tree_hashes: dict[str, str],
                          fingerprints: dict[str, str], *, job_id: str, completed_at: str,
                          root: Path | None = None) -> dict:
    store = ArtifactStore(_root(root))
    with active_batch_lock(batch_id, store.root):
        tree_ref, trees = _current_tree_locked(store, batch_id)
        current = trees.get("tree_hashes", {})
        if any(current.get(task) != value for task, value in expected_tree_hashes.items()):
            raise StaleTaskInput("所选任务的轨迹树已变化，请使用当前数据重新质检")
        previous = current_quality_payload(batch_id, root)
        tasks = {item["task_id"]: {key: val for key, val in item.items() if key != "artifact"}
                 for item in previous["tasks"]}
        tasks.update({item["task_id"]: {key: deepcopy(val) for key, val in item.items() if key != "artifact"}
                      for item in task_results})
        valid_ids = set(tasks)
        value = {"schema_version": 2, "run_id": batch_id, "batch_id": batch_id, "job_id": job_id,
                 "completed_at": completed_at, "tasks": [tasks[key] for key in sorted(tasks)],
                 "source_tree_hashes": {task: current[task] for task in valid_ids},
                 "task_fingerprints": {**{task: value for task, value in previous.get("task_fingerprints", {}).items()
                                          if task in valid_ids}, **fingerprints}}
        changed = {item["task_id"] for item in task_results
                   if previous.get("task_fingerprints", {}).get(item["task_id"]) != fingerprints.get(item["task_id"])}
        ref = store.publish_many(batch_id, [{"stage": "05_quality", "payload": value,
            "tables": quality_tables(value["tasks"]), "source_refs": [tree_ref] if tree_ref else [],
            "metadata": {"run_id": batch_id, "task_ids": sorted(tasks)}}],
            record_entries=[invalidation_record_entry(store, batch_id, "quality", changed)])[0]
        drain_batch_invalidations(batch_id, root)
        return {**value, "artifact": ref, "updated_at": completed_at}


def _write_quality_records(store: ArtifactStore, batch_id: str, value: dict, reference: dict) -> None:
    entries = [{"namespace": "quality_results", "key": f"{batch_id}:{item['task_id']}",
                "payload": {**item, "artifact": reference}} for item in value.get("tasks", [])]
    entries.append({"namespace": "quality_manifests", "key": batch_id,
                    "payload": {"run_id": batch_id, "updated_at": value.get("completed_at"), "artifact": reference,
                                "tasks": [{key: val for key, val in item.items() if key not in {"evaluations", "rubric"}}
                                          for item in value.get("tasks", [])]}})
    store.records.put_many(entries)


def _invalidate_quality_locked(store: ArtifactStore, batch_id: str, task_ids: set[str]) -> None:
    reference, value = _read(store, batch_id, "05_quality")
    if not reference:
        return
    value["tasks"] = [item for item in value.get("tasks", []) if item.get("task_id") not in task_ids]
    for field in ("source_tree_hashes", "task_fingerprints"):
        value[field] = {task: item for task, item in value.get(field, {}).items() if task not in task_ids}
    tree = store.get(batch_id, "04_tree")
    ref = store.publish(batch_id, "05_quality", value, tables=quality_tables(value["tasks"]),
                        source_refs=[tree] if tree else [], metadata={"run_id": batch_id})
    _write_quality_records(store, batch_id, value, ref)
    for task in task_ids:
        store.records.delete("quality_results", f"{batch_id}:{task}")


def _notify_invalidated(batch_id: str, tasks: set[str], root: Path | None) -> None:
    from .trajectory_correction.service import on_batch_tasks_invalidated
    on_batch_tasks_invalidated(batch_id, sorted(tasks), root=_root(root))


def invalidate_tasks(batch_id: str, task_ids: list[str] | set[str], root: Path | None = None) -> None:
    """Remove only affected tasks from dependent stage payloads."""
    tasks = set(task_ids)
    store = ArtifactStore(_root(root))
    with store.batch_lock(batch_id):
        tree_ref, tree = _read(store, batch_id, "04_tree")
        observation_ref, observation = _read(store, batch_id, "03_observation")
        if tree_ref and observation_ref:
            kept = set(tree.get("trees", {})) - tasks
            tree = _filter_tree(tree, kept)
            observation["trajectories"] = [item for item in observation.get("trajectories", []) if item.get("task_id") in kept]
            observation["rows"] = [item for item in observation.get("rows", []) if item.get("任务编号") in kept]
            for field in ("task_fingerprints", "source_task_fingerprints"):
                observation[field] = {key: item for key, item in observation.get(field, {}).items() if key in kept}
            annotation = store.get(batch_id, "02_annotation")
            store.publish_many(batch_id, [
                {"stage": "03_observation", "payload": observation,
                 "tables": {"Observation与中间态": observation.get("rows", [])}, "source_refs": [annotation] if annotation else []},
                {"stage": "04_tree", "payload": tree, "source_stages": ["03_observation"],
                 "metadata": {"run_id": batch_id, "task_ids": sorted(kept)}},
            ])
        _invalidate_quality_locked(store, batch_id, tasks)
        for task in tasks:
            _task_state(store, batch_id, task, tree_status="stale", quality_status="stale")
        _notify_invalidated(batch_id, tasks, root)


def materialize_tree_inputs(batch_id: str, output_dir: Path, root: Path | None = None) -> tuple[Path, Path]:
    """Materialize a worker/session input snapshot, never a second current result."""
    value = current_tree_payload(batch_id, root)
    if not value.get("trees"):
        raise FileNotFoundError("该批次没有当前轨迹树")
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, field in (("source_annotated.xlsx", "source_annotation"),
                            ("rubric_trajectories.xlsx", "quality_input")):
        path = output_dir / filename
        write_payload_workbook(path, value[field])
        write_sidecar(path, value[field])
    return output_dir / "source_annotated.xlsx", output_dir / "rubric_trajectories.xlsx"


def quality_task_fingerprints(trees: dict, *, config_path: Path | None = None,
                              env_path: Path | None = None) -> dict[str, str]:
    backend = Path(__file__).resolve().parent
    config_path = config_path or backend / "DevelopRubrics/examples/jiawen_rubric_config.json"
    env_path = env_path or backend / ".env"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    # Include only model settings; credentials never become stored metadata.
    from .trajectories_preprocessing import read_env_file
    environment = read_env_file(env_path) if env_path.is_file() else {}
    model = {key: environment.get(key, "") for key in ("MODEL_NAME", "MODEL_URL")}
    return {task: digest({"tree": value, "quality_input": _rows_for_tasks(trees.get("quality_input", {}), {task}),
                          "config": config, "model": model, "pipeline_version": 2})
            for task, value in trees.get("tree_hashes", {}).items()}


def tree_result_hashes(trees: dict, quality_input: dict, fingerprints: dict) -> dict[str, str]:
    return {task: digest({"tree": value, "quality_input": _rows_for_tasks(quality_input, {task}),
                          "input_fingerprint": fingerprints.get(task)}) for task, value in trees.items()}


def _task_state(store: ArtifactStore, batch_id: str, task_id: str, **changes) -> None:
    store.records.update("batch_task_states", f"{batch_id}:{task_id}",
                         lambda current: current.update(changes),
                         default={"batch_id": batch_id, "task_id": task_id})


def resolve_current_batch_id(identifier: str, root: Path | None = None) -> str | None:
    store = ArtifactStore(_root(root))
    if store.get(identifier, "04_tree"):
        return identifier
    alias = store.records.get("batch_run_aliases", identifier)
    if not alias or alias.get("retired"):
        return None
    value = current_tree_payload(alias["batch_id"], root)
    expected = alias.get("task_fingerprints", {})
    if not expected or any(value.get("task_fingerprints", {}).get(task) != signature
                           for task, signature in expected.items()):
        return None
    return alias["batch_id"]


def invalidation_record_entry(store: ArtifactStore, batch_id: str, stage: str,
                              task_ids: list[str] | set[str]) -> dict:
    """Build an outbox update for the same transaction as an upstream replacement.

    The caller holds the batch lock through publish_many. One pending record per
    batch survives a crash between publication and downstream reconciliation.
    """
    stage = {"02_annotation": "annotation", "04_tree": "tree", "05_quality": "quality"}.get(stage, stage)
    if stage not in {"annotation", "tree", "quality"}:
        raise ValueError("Unsupported invalidation stage")
    current = store.records.get("batch_invalidations", batch_id) or {}
    payload = {key: deepcopy(value) for key, value in current.items() if key != "storage_revision"}
    payload["batch_id"] = batch_id
    payload[stage] = sorted(set(payload.get(stage, [])) | set(task_ids))
    return {"namespace": "batch_invalidations", "key": batch_id, "payload": payload,
            "expected_revision": current.get("storage_revision", 0)}


def drain_batch_invalidations(batch_id: str, root: Path | None = None) -> None:
    store = ArtifactStore(_root(root))
    with store.batch_lock(batch_id):
        event = store.records.get("batch_invalidations", batch_id)
        if not event:
            return
        if "annotation" in event:
            _, tree = _read(store, batch_id, "04_tree")
            _, annotation = _read(store, batch_id, "02_annotation")
            fingerprints = annotation_task_fingerprints(annotation)
            changed = {task for task in event["annotation"]
                       if tree.get("source_task_fingerprints", {}).get(task) != fingerprints.get(task)}
            invalidate_tasks(batch_id, changed, root)
        if "tree" in event:
            tasks = set(event["tree"])
            _, current_tree = _current_tree_locked(store, batch_id)
            _, quality = _read(store, batch_id, "05_quality")
            obsolete = {task for task in tasks if quality.get("source_tree_hashes", {}).get(task)
                        != current_tree.get("tree_hashes", {}).get(task)}
            _invalidate_quality_locked(store, batch_id, obsolete)
            valid = current_tree.get("trees", {})
            reviewed = {item["task_id"] for item in current_quality_payload(batch_id, root)["tasks"]}
            for task in tasks:
                _task_state(store, batch_id, task, tree_status="succeeded" if task in valid else "stale",
                            quality_status="stale" if task not in valid else ("succeeded" if task in reviewed else "pending"))
            _notify_invalidated(batch_id, tasks, root)
        if "quality" in event:
            reference, value = _read(store, batch_id, "05_quality")
            if reference:
                _write_quality_records(store, batch_id, value, reference)
                valid = {item["task_id"] for item in current_quality_payload(batch_id, root)["tasks"]}
                for task in event["quality"]:
                    _task_state(store, batch_id, task, quality_status="succeeded" if task in valid else "stale")
            _notify_invalidated(batch_id, set(event["quality"]), root)
        store.records.delete("batch_invalidations", batch_id, expected_revision=event["storage_revision"])


def recover_pending_batch_results(root: Path | None = None) -> None:
    """Finish committed invalidations on startup, without rerunning any models."""
    store = ArtifactStore(_root(root))
    store.recover()
    for item in store.records.list("batch_invalidations"):
        drain_batch_invalidations(item["batch_id"], root)
