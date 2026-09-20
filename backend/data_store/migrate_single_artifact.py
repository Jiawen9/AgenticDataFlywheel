"""Offline, backed-up migration to one current artifact per batch/stage.

Usage: python -m backend.data_store.migrate_single_artifact check|apply|verify|rollback
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

from .artifacts import ArtifactStore, _identifier, _json_bytes
from .paths import DATA_ROOT, contained_path
from .registry import RecordStore, utc_now
from .migrate_root import _mutation_lock, _no_links
from .release_provenance import freeze_existing_release, FrozenReleaseSources


def _sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _inventory(root: Path) -> dict:
    _no_links(root, recursive=True)
    return {file.relative_to(root).as_posix(): {"sha256": _sha(file), "size": file.stat().st_size}
            for file in sorted(root.rglob("*")) if file.is_file()
            and not file.name.endswith(".lock")}


def _save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(_json_bytes(value))
    temporary.replace(path)


def _paths(root: Path):
    root = Path(root).resolve()
    if not root.name or root == Path(root.anchor) or root == root.parent:
        raise ValueError("A named data directory is required")
    journal = root.parent / ".single-artifact-migration" / root.name
    for path in (root, journal):
        _no_links(path)
    return root, journal


def _active(records: RecordStore) -> list[str]:
    active = []
    for namespace in ("tree_jobs", "quality_jobs", "preprocessing_jobs",
                      "correction_cot_jobs", "task_generation.jobs", "collection_runs",
                      "dataset_upload_jobs", "training_overview_conversions"):
        active.extend(f"{namespace}/{item.get('job_id') or item.get('collection_run_id', '')}"
                      for item in records.list(namespace)
                      if item.get("status") in {"running", "queued", "dispatching"})
    return active


def check(root: Path = DATA_ROOT) -> dict:
    root, journal = _paths(root)
    store = ArtifactStore(root)
    artifacts = store.list()
    for ref in artifacts:
        # Check without creating batch lock files in this read-only command.
        store._resolve_file(ref, "result.json")
    return {"data_root": str(root), "backup": str(journal / "backup"),
            "batches": sorted({ref["batch_id"] for ref in artifacts}),
            "legacy_versions": sum(ref.get("schema_version", 1) < 2 for ref in artifacts),
            "active_jobs": _active(store.records), "artifacts": len(artifacts)}


def _ancestor(reference: dict, stage: str, store: ArtifactStore) -> dict | None:
    visited = set()
    def visit(ref):
        key = (ref.get("batch_id"), ref.get("stage"), ref.get("version"))
        if not all(key) or key in visited:
            return None
        visited.add(key)
        current = store.get(*key)
        if current is None:
            raise ValueError(f"Missing upstream artifact: {key}")
        if current["stage"] == stage:
            return current
        for parent in current.get("source_refs", []):
            found = visit(parent)
            if found:
                return found
        return None
    return visit(reference)


def _observation_rows(trajectories: list) -> list:
    rows = []
    for trajectory in trajectories:
        for step in trajectory.get("steps", []):
            classification = step.get("classification") or {}
            rows.append({"任务编号": trajectory["task_id"], "轨迹编号": trajectory["trajectory_id"],
                "步骤": step["step"], "image": step.get("image"), "xml": step.get("xml"),
                "action": step.get("action_text"), "summary": step.get("summary"),
                "actions_box": step.get("actions_box"), "Observation": step.get("observation"),
                "中间态类别": classification.get("category"), "是否中间态": classification.get("is_intermediate"),
                "置信度": classification.get("confidence"), "判断原因": classification.get("reason"),
                "计入树": step.get("counted_in_tree"), "决策来源": step.get("decision_source"),
                "中途终止": step.get("excluded_intermediate_terminate")})
    return rows


def _batch_plan(old: ArtifactStore, batch_id: str) -> dict:
    from ..batch_results import (annotation_task_fingerprints, tree_task_fingerprints,
        tree_result_hashes, quality_task_fingerprints, _rows_for_tasks, _merge_workbooks)
    from ..tree_build_service import tree_build_config
    values = sorted(old.list(batch_id), key=lambda ref: (ref["created_at"], ref["version"]))
    selected = {ref["stage"]: ref for ref in values}
    annotation_ref = selected.get("02_annotation")
    if not annotation_ref:
        return {"selected": selected, "tree": None, "quality": None, "observation": None,
                "sessions": [], "session": None, "aliases": {}}
    annotation = old.read_payload(annotation_ref)
    annotation_hashes = annotation_task_fingerprints(annotation)
    conversion = _ancestor(annotation_ref, "01_conversion", old)
    if conversion:
        selected["01_conversion"] = conversion
        collection = _ancestor(conversion, "00_collection", old)
        if collection:
            selected["00_collection"] = collection
    trees, tasks, task_fps, observations, origins, aliases = {}, {}, {}, {}, {}, {}
    original_run_tasks = {}
    quality_input = {"schema_version": 1, "columns": {}, "sheets": {}}
    for ref in (item for item in values if item["stage"] == "04_tree"):
        payload = old.read_payload(ref)
        parent = _ancestor(ref, "02_annotation", old)
        if parent is None:
            raise ValueError("Tree artifact has no annotation lineage")
        hashes = annotation_task_fingerprints(old.read_payload(parent))
        valid = {task for task in payload.get("trees", {}) if hashes.get(task) == annotation_hashes.get(task)}
        run_id = str(payload.get("run_id") or ref.get("metadata", {}).get("run_id") or "")
        original_run_tasks.setdefault(run_id, set()).update(payload.get("trees", {}))
        run_path = old.root / "system" / "trajectory_tree_runs" / run_id / "manifest.json"
        run = json.loads(run_path.read_text(encoding="utf-8")) if run_path.is_file() else {}
        summaries = {task["task_id"]: task for task in run.get("tasks", payload.get("tasks", []))}
        observed_ref = _ancestor(ref, "03_observation", old)
        observed = old.read_payload(observed_ref) if observed_ref else {}
        for task in valid:
            tree = payload["trees"][task]
            config = tree_build_config()
            config.update(model=tree.get("model_name", run.get("model_name", config["model"])),
                          confidence_threshold=tree.get("confidence_threshold", 0.8),
                          max_incidental_skip=tree.get("max_incidental_skip", 2))
            trees[task] = tree
            task_fps[task] = tree_task_fingerprints(annotation, config)[task]
            tasks[task] = summaries.get(task, {"task_id": task, "goal": task, "tree_file": task + ".json",
                "trajectory_count": len(tree.get("source_trajectories", [])),
                "original_step_count": sum(len(item.get("steps", [])) for item in tree.get("source_trajectories", [])),
                "tree_step_count": 0, "ignored_step_count": 0, "action_node_count": 0})
            observations[task] = [item for item in observed.get("trajectories", []) if item.get("task_id") == task]
            quality_input = _merge_workbooks(quality_input, _rows_for_tasks(payload["quality_input"], {task}), {task})
            origins[task] = run_id
        aliases[run_id] = {"batch_id": batch_id, "retired": True}
    for stage in ("03_observation", "04_tree", "05_quality", "06_correction", "07_cot"):
        selected.pop(stage, None)
    if not trees:
        return {"selected": selected, "tree": None, "quality": None, "observation": None,
                "sessions": [], "session": None, "aliases": aliases}
    latest_tree = next(ref for ref in reversed(values) if ref["stage"] == "04_tree" and
                       str(old.read_payload(ref).get("run_id")) in set(origins.values()))
    source = _rows_for_tasks(annotation, set(trees))
    # Record the actual raw root, resolving previous data-root relocation only at use time.
    raw_root = annotation_ref.get("metadata", {}).get("raw_root")
    if not raw_root:
        parent = _ancestor(annotation_ref, "01_conversion", old)
        raw_root = next((ref["path"] for ref in (parent or {}).get("source_refs", [])
                         if ref.get("kind") == "raw_trajectories"), None)
    tree = {"schema_version": 2, "run_id": batch_id, "batch_id": batch_id, "trees": trees,
            "tasks": [tasks[key] for key in sorted(tasks)], "source_annotation": source,
            "quality_input": quality_input, "raw_root": raw_root,
            "source_task_fingerprints": {key: annotation_hashes[key] for key in trees},
            "task_fingerprints": task_fps, "completed_at": latest_tree["created_at"],
            "model_name": latest_tree.get("metadata", {}).get("model", next(iter(trees.values())).get("model_name", ""))}
    tree["tree_hashes"] = tree_result_hashes(trees, quality_input, task_fps)
    for run_id, original_tasks in original_run_tasks.items():
        if original_tasks and all(origins.get(task) == run_id for task in original_tasks):
            aliases[run_id] = {"batch_id": batch_id, "task_fingerprints":
                              {task: task_fps[task] for task in original_tasks}}
    observed = {"schema_version": 2, "trajectories": [item for task in sorted(observations) for item in observations[task]],
                "task_fingerprints": task_fps, "source_task_fingerprints": tree["source_task_fingerprints"]}
    observed["rows"] = _observation_rows(observed["trajectories"])
    quality_tasks = {}
    quality_candidates = [old.read_payload(ref) for ref in values if ref["stage"] == "05_quality"]
    for value in quality_candidates:
        for item in value.get("tasks", []):
            task = item.get("task_id")
            if task in origins and str(item.get("run_id") or value.get("run_id")) == origins[task]:
                quality_tasks[task] = {**item, "run_id": batch_id}
    qrefs = [ref for ref in values if ref["stage"] == "05_quality"]
    completed_at = qrefs[-1]["created_at"] if qrefs else None
    quality = {"schema_version": 2, "run_id": batch_id, "batch_id": batch_id,
        "tasks": [quality_tasks[key] for key in sorted(quality_tasks)], "completed_at": completed_at,
        "source_tree_hashes": {key: tree["tree_hashes"][key] for key in quality_tasks},
        "task_fingerprints": {key: value for key, value in quality_task_fingerprints(tree).items() if key in quality_tasks}}
    sessions = [item for item in old.records.list("correction_sessions")
                if str(item.get("storage_batch_id") or item.get("batch_id") or item.get("tree_run_id")) == batch_id
                or item.get("tree_run_id") in aliases]
    eligible = sorted((item for item in sessions if item.get("tree_run_id") in set(origins.values())),
                      key=lambda item: (item.get("updated_at", ""), item["session_id"]))
    chosen_session = eligible[-1] if eligible else None
    if any(item.get("session_id") != (chosen_session or {}).get("session_id")
           and (item.get("row_edits") or item.get("cot")) for item in sessions):
        raise ValueError("Multiple edited legacy drafts require an explicit merge before migration")
    if chosen_session:
        for ref in values:
            if ref["stage"] in {"06_correction", "07_cot"} and ref.get("metadata", {}).get("session_id") == chosen_session["session_id"]:
                selected[ref["stage"]] = ref
    return {"selected": selected, "tree": tree, "quality": quality, "observation": observed,
            "sessions": sessions, "session": chosen_session, "aliases": aliases, "origins": origins}


def _registry_for(root: Path):
    # Avoid constructing a registry that creates directories during verification.
    from ..data_publishing.service import DatasetReleaseRegistry
    registry = object.__new__(DatasetReleaseRegistry)
    registry.data_root, registry.project_root = root, root.parent
    return registry


def _release_stats(root: Path, release: dict) -> str:
    from ..training_data_overview.converter import convert_release
    return hashlib.sha256(_json_bytes(convert_release(_registry_for(root), release))).hexdigest()


def _apply_batch(root: Path, old: ArtifactStore, batch_id: str, plan: dict) -> dict:
    from ..stage_artifacts import quality_tables
    from ..batch_results import _write_quality_records
    from ..trajectory_correction.session_state import migrate_session_identity
    target = ArtifactStore(root)
    entries = []
    for stage, ref in sorted(plan["selected"].items()):
        payload = old.read_payload(ref)
        entry = {"stage": stage, "payload": payload, "legacy_aliases": [ref["version"]],
                 "metadata": ref.get("metadata", {}), "source_refs": [], "source_stages": []}
        for source in ref.get("source_refs", []):
            if source.get("stage") in plan["selected"] and source.get("stage") != stage:
                entry["source_stages"].append(source["stage"])
            elif not source.get("stage"):
                entry["source_refs"].append(source)
        if stage == "02_annotation":
            entry["source_stages"] = ["01_conversion"] if "01_conversion" in plan["selected"] else []
        for file in ref["files"]:
            if file["kind"] == "excel":
                entry.setdefault("workbooks", {})[file["name"]] = old.resolve_file(ref, file["name"])
        if stage in {"06_correction", "07_cot"}:
            payload.update(tree_run_id=batch_id, batch_id=batch_id)
            entry["source_stages"] = ["04_tree", "05_quality"] if plan["quality"]["tasks"] else ["04_tree"]
            if stage == "07_cot" and "06_correction" in plan["selected"]:
                entry["source_stages"].append("06_correction")
        entries.append(entry)
    if plan["tree"]:
        entries += [
            {"stage": "03_observation", "payload": plan["observation"], "source_stages": ["02_annotation"],
             "tables": {"Observation与中间态": plan["observation"]["rows"]}, "metadata": {"run_id": batch_id}},
            {"stage": "04_tree", "payload": plan["tree"], "source_stages": ["03_observation"],
             "metadata": {"run_id": batch_id, "task_ids": sorted(plan["tree"]["trees"])}},
        ]
        if plan["quality"]["tasks"]:
            entries.append({"stage": "05_quality", "payload": plan["quality"], "source_stages": ["04_tree"],
                            "tables": quality_tables(plan["quality"]["tasks"]), "metadata": {"run_id": batch_id}})
    entries.sort(key=lambda item: item["stage"])
    # Only reference stages present earlier in this same complete migration.
    preceding = set()
    for entry in entries:
        entry["source_stages"] = list(dict.fromkeys(stage for stage in entry["source_stages"] if stage in preceding))
        preceding.add(entry["stage"])
    published = target.publish_many(batch_id, entries, replace_legacy=True)
    current = {ref["stage"]: ref for ref in published}
    for ref in old.list(batch_id):
        if ref["stage"] not in current:
            target.records.delete("artifacts", target._key(batch_id, ref["stage"], ref["version"]))
            path = contained_path(root, "batches", batch_id, ref["stage"])
            if path.is_dir():
                target._remove(path)
    for run_id, alias in plan["aliases"].items():
        target.records.put("batch_run_aliases", run_id, alias)
    if plan["quality"] and plan["quality"]["tasks"]:
        _write_quality_records(target, batch_id, plan["quality"], current["05_quality"])
    for task in (plan["tree"] or {}).get("trees", {}):
        target.records.put("batch_task_states", f"{batch_id}:{task}", {"batch_id": batch_id, "task_id": task,
            "tree_status": "succeeded", "quality_status": "succeeded" if task in (plan["quality"] or {}).get("source_tree_hashes", {}) else "pending"})
    active_session = plan["session"]
    for session in plan["sessions"]:
        if not active_session or session["session_id"] != active_session["session_id"]:
            target.records.delete("correction_sessions", session["session_id"])
    if active_session:
        session_id = active_session["session_id"]
        inputs = root / "system" / "trajectory_correction" / "inputs" / session_id
        snapshot = json.loads((inputs / "snapshot.json").read_text(encoding="utf-8"))
        migrated = migrate_session_identity(active_session, snapshot, batch_id=batch_id,
            table_payload=plan["tree"]["source_annotation"], task_fingerprints=plan["tree"]["task_fingerprints"])
        migrated["selection"].update(source_path=str(inputs / "source.xlsx"))
        latest_exports = {}
        for export in migrated.get("exports", []):
            kind = export.get("kind", "sft_rl")
            if kind not in latest_exports:
                stage = "07_cot" if kind == "full_dataset" else "06_correction"
                if stage in current:
                    export["artifact"] = current[stage]
                latest_exports[kind] = export
        migrated["exports"] = list(latest_exports.values())
        target.records.put("correction_sessions", session_id, migrated)
        # Preserve the historical baseline bytes: the new stable-ID snapshot is in SQLite.
    # Remap current successful execution logs, without representing them as history choices.
    for namespace in ("tree_jobs", "quality_jobs"):
        for job in target.records.list(namespace):
            run_id = job.get("run_id")
            alias = plan["aliases"].get(run_id)
            if alias and not alias.get("retired"):
                job.update(batch_id=batch_id, run_id=batch_id)
                stage = "04_tree" if namespace == "tree_jobs" else "05_quality"
                if stage in current:
                    job["artifact"] = current[stage]
                if namespace == "tree_jobs":
                    job["task_fingerprints"] = alias["task_fingerprints"]
                else:
                    job["task_fingerprints"] = (plan["quality"] or {}).get("task_fingerprints", {})
                target.records.put(namespace, job["job_id"], job)
            elif alias:
                job["retired"] = True
                target.records.put(namespace, job["job_id"], job)
    return {"batch_id": batch_id, "stages": sorted(current),
            "session_id": active_session["session_id"] if active_session else None,
            "manual_edits": deepcopy((active_session or {}).get("row_edits", {})),
            "cot": deepcopy((active_session or {}).get("cot", {})),
            "tree_origins": plan.get("origins", {})}


def _clean_legacy_runtime(root: Path, plans: dict) -> None:
    # Only paths recorded by the migration are removed; raw/cache/resources are untouched.
    for batch_id, plan in plans.items():
        for run_id in plan["aliases"]:
            _identifier(run_id, "legacy run")
            for part in ("trajectory_tree_runs", "trajectory_quality_results"):
                path = contained_path(root, "system", part, run_id)
                if path.is_dir():
                    shutil.rmtree(path)
        chosen = (plan["session"] or {}).get("session_id")
        for session in plan["sessions"]:
            session_id = _identifier(session["session_id"], "session")
            if session_id != chosen:
                for part in ("inputs", "exports", "sessions"):
                    path = contained_path(root, "system", "trajectory_correction", part, session_id)
                    if path.is_dir():
                        shutil.rmtree(path)
        if chosen:
            current = RecordStore(root).get("correction_sessions", chosen)
            keep = {item["filename"] for item in current.get("exports", [])}
            directory = contained_path(root, "system", "trajectory_correction", "exports", chosen)
            if directory.is_dir():
                for file in directory.iterdir():
                    if file.is_file() and file.name not in keep:
                        file.unlink()

    # Superseded current-result indexes are not execution logs.
    records = RecordStore(root)
    aliases = {run for plan in plans.values() for run in plan["aliases"]}
    deletes = []
    with records._connection() as connection:
        for namespace, key, raw in connection.execute(
                "SELECT namespace, record_key, payload FROM records WHERE namespace IN ('quality_results','quality_manifests','trajectory_annotations')"):
            value = json.loads(raw)
            if namespace in {"quality_results", "quality_manifests"} and value.get("run_id") in aliases:
                deletes.append({"namespace": namespace, "key": key})
            elif namespace == "trajectory_annotations" and value.get("artifact", {}).get("batch_id") in plans:
                path = Path(value.get("path", "")).resolve()
                if path.is_relative_to(root / "system" / "preprocessing"):
                    deletes.append({"namespace": namespace, "key": key})
    records.put_many([], deletes=deletes)
    preprocessing = contained_path(root, "system", "preprocessing")
    if preprocessing.is_dir():
        for file in preprocessing.rglob("*.json"):
            if file.name not in {"annotated_trajectories.json", "trajectories_to_excel.json"}:
                continue
            value = json.loads(file.read_text(encoding="utf-8"))
            if value.get("source_ref", {}).get("batch_id") in plans:
                file.with_suffix(".xlsx").unlink(missing_ok=True)
                file.unlink()
    for batch_id in plans:
        # Legacy validation reports describe old versions and belong in the backup.
        reports = contained_path(root, "batches", batch_id, "validation")
        if reports.is_dir():
            _no_links(reports, recursive=True)
            shutil.rmtree(reports)


def apply(root: Path = DATA_ROOT) -> dict:
    root, journal = _paths(root)
    parent = journal.parent
    parent.mkdir(exist_ok=True)
    with _mutation_lock(journal):
        status_path = journal / "state.json"
        if status_path.exists():
            state = json.loads(status_path.read_text(encoding="utf-8"))
            if state["phase"] == "verified":
                if state.get("runtime_cleanup_version", 0) < 2:
                    if _active(RecordStore(root)) or _sha(root / "system" / "app.sqlite") != state.get("post_database_sha256"):
                        raise ValueError("Business records changed after migration; stop writers before completing cleanup")
                    backup = journal / "backup"
                    if _inventory(backup) != state["backup_inventory"]:
                        raise ValueError("Backup checksum verification failed")
                    old = ArtifactStore(backup)
                    plans = {item["batch_id"]: _batch_plan(old, item["batch_id"]) for item in state["batches"]}
                    _clean_legacy_runtime(root, plans)
                    state["runtime_cleanup_version"] = 2
                    state["post_database_sha256"] = _sha(root / "system" / "app.sqlite")
                    _save(status_path, state)
                return verify(root)
            raise ValueError("Existing migration is incomplete; verify or rollback it before retrying")
        report = check(root)
        if report["active_jobs"]:
            raise ValueError("Stop all writers and active jobs before migration")
        if not report["legacy_versions"]:
            return {**report, "status": "already_current"}
        backup = journal / "backup"
        if backup.exists():
            raise ValueError("Backup already exists without a migration journal")
        before = _inventory(root)
        shutil.copytree(root, backup)
        if _inventory(backup) != before:
            raise ValueError("Backup checksum verification failed; no data was changed")
        state = {"schema_version": 1, "phase": "backed_up", "data_root": str(root),
                 "backup": str(backup), "created_at": utc_now(), "backup_inventory": before,
                 "batches": [], "release_stats": {}, "release_excel": {}}
        _save(status_path, state)
        try:
            old = ArtifactStore(backup)
            plans = {batch: _batch_plan(old, batch) for batch in report["batches"]}
            records = RecordStore(root)
            for release in records.list("dataset_releases"):
                frozen = freeze_existing_release(root, release, store=old)
                from ..training_data_overview.converter import convert_release
                converted = convert_release(_registry_for(root), frozen)
                previous = records.get("training_overview_conversions", release["release_id"]) or {}
                if previous.get("status") == "succeeded":
                    catalog = records.get("training_overview_catalog", "current") or {}
                    original_rows = [row for row in catalog.get("rows", []) if row.get("release_id") == release["release_id"]]
                    if original_rows != converted["rows"] or previous.get("warnings", []) != converted["warnings"]:
                        raise ValueError("Frozen release statistics differ from the existing published catalog")
                records.put("dataset_releases", release["release_id"], frozen)
                _save(root / "releases" / release["release_id"] / "manifest.json",
                      {key: value for key, value in frozen.items() if key != "storage_revision"})
                state["release_stats"][release["release_id"]] = _release_stats(root, frozen)
                state["release_excel"][release["release_id"]] = [item["sha256"] for item in release.get("excel_paths", [])]
            state["phase"] = "publishing"
            _save(status_path, state)
            for batch_id, plan in plans.items():
                state["batches"].append(_apply_batch(root, old, batch_id, plan))
                _save(status_path, state)
            _clean_legacy_runtime(root, plans)
            state["runtime_cleanup_version"] = 2
            state["phase"] = "applied"
            _save(status_path, state)
            result = verify(root)
            state = json.loads(status_path.read_text(encoding="utf-8"))
            state["phase"] = "verified"
            state["post_database_sha256"] = _sha(root / "system" / "app.sqlite")
            _save(status_path, state)
            return result
        except BaseException:
            state["phase"] = "failed"
            state["post_database_sha256"] = _sha(root / "system" / "app.sqlite")
            _save(status_path, state)
            raise


def verify(root: Path = DATA_ROOT) -> dict:
    root, journal = _paths(root)
    state = json.loads((journal / "state.json").read_text(encoding="utf-8"))
    if state.get("data_root") != str(root) or state.get("backup") != str(journal / "backup"):
        raise ValueError("Migration journal paths do not match")
    store = ArtifactStore(root)
    store.recover()
    for ref in store.list():
        if ref.get("schema_version") != 2:
            raise ValueError("Legacy stage remains registered")
        directory = contained_path(root, "batches", ref["batch_id"], ref["stage"])
        if any(path.is_dir() for path in directory.iterdir()):
            raise ValueError("A stage still contains version directories")
        for file in ref["files"]:
            store.read_file(ref, file["name"])
        for parent in ref.get("source_refs", []):
            if parent.get("stage") and store.get(parent["batch_id"], parent["stage"], parent["version"]) is None:
                raise ValueError("Current artifact has an expired source reference")
    for batch in state["batches"]:
        if batch["session_id"]:
            session = store.records.get("correction_sessions", batch["session_id"])
            if session.get("row_edits", {}) != batch["manual_edits"] or session.get("cot", {}) != batch["cot"]:
                raise ValueError("Migration changed persisted manual edits or COT")
    for release in store.records.list("dataset_releases"):
        FrozenReleaseSources(root, release)
        if _release_stats(root, release) != state["release_stats"][release["release_id"]]:
            raise ValueError("Published training statistics changed")
        if [item["sha256"] for item in release["excel_paths"]] != state["release_excel"][release["release_id"]]:
            raise ValueError("Published Excel hashes changed")
    return {"status": "verified", "data_root": str(root), "backup": state["backup"],
            "batches": [{key: val for key, val in item.items() if key not in {"manual_edits", "cot"}}
                        for item in state["batches"]],
            "artifacts": len(store.list()), "releases_verified": len(state["release_stats"])}


def rollback(root: Path = DATA_ROOT) -> dict:
    root, journal = _paths(root)
    with _mutation_lock(journal):
        state = json.loads((journal / "state.json").read_text(encoding="utf-8"))
        if state.get("data_root") != str(root) or state.get("backup") != str(journal / "backup"):
            raise ValueError("Migration journal paths do not match")
        if _active(RecordStore(root)):
            raise ValueError("Stop writers before rollback")
        if state.get("post_database_sha256") and _sha(root / "system" / "app.sqlite") != state["post_database_sha256"]:
            raise ValueError("Business records changed after migration; rollback would discard new writes")
        backup = journal / "backup"
        if _inventory(backup) != state["backup_inventory"]:
            raise ValueError("Backup checksum verification failed")
        failed = journal / "replaced_current"
        if failed.exists():
            raise ValueError("A previous rollback copy already exists")
        _no_links(root, recursive=True)
        root.rename(failed)
        try:
            shutil.copytree(backup, root)
        except BaseException:
            if root.exists() and root.resolve() == Path(state["data_root"]):
                shutil.rmtree(root)
            failed.rename(root)
            raise
        state["phase"] = "rolled_back"
        _save(journal / "state.json", state)
        return {"status": "rolled_back", "data_root": str(root), "backup": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "apply", "verify", "rollback"])
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    args = parser.parse_args()
    print(json.dumps(globals()[args.command](args.data_root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
