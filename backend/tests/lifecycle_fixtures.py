"""Complete offline source chains for release/lifecycle tests."""
from backend.data_store import ArtifactStore
from backend.batch_results import annotation_task_fingerprints, digest


def seed_publishable_batch(root, batch_id, session):
    store = ArtifactStore(root)
    tasks = session["selection"]["tasks"]
    rows = [{"task_id": item["task_id"], "trajectory_id": f'{item["task_id"]}-{index}',
             "image": f'{item["task_id"]}/{index}/step001.jpg', "action": "wait", "actions_box": ""}
            for item in tasks for index in range(int(item.get("trajectory_count") or 1))]
    payload = {"schema_version": 1, "sheets": {"VLA trajectories": rows}}
    converted = store.publish(batch_id, "01_conversion", payload=payload)
    annotated = store.publish(batch_id, "02_annotation", payload=payload, source_refs=[converted])
    fingerprints = session.get("task_fingerprints") or annotation_task_fingerprints(payload)
    session["task_fingerprints"] = fingerprints
    trees = {item["task_id"]: {"steps": [], "task_id": item["task_id"]} for item in tasks}
    hashes = {task: digest(value) for task, value in trees.items()}
    refs = store.publish_many(batch_id, [
        {"stage": "03_observation", "payload": {"source_task_fingerprints": annotation_task_fingerprints(payload)},
         "source_refs": [annotated]},
        {"stage": "04_tree", "payload": {"batch_id": batch_id, "trees": trees, "tasks": tasks,
         "task_fingerprints": fingerprints, "tree_hashes": hashes,
         "source_task_fingerprints": annotation_task_fingerprints(payload), "source_annotation": payload},
         "source_stages": ["03_observation"]}])
    store.publish(batch_id, "05_quality", payload={
        "tasks": [{**item, "status": "succeeded",
            "evaluations": [{"trajectory_id": f'{item["task_id"]}-{index}', "passed": True}
                            for index in range(int(item.get("trajectory_count") or 1))]} for item in tasks],
        "source_tree_hashes": hashes, "task_fingerprints": fingerprints}, source_refs=[refs[-1]])
    return store
