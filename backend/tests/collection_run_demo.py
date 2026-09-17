"""Demonstrate batch dispatch -> completed manifest -> ready input, offline."""
from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

from backend.phone_factory import PhoneFactoryStore
from backend.tests.test_collection_runs import raw_trajectory, seed_batch


def main():
    with tempfile.TemporaryDirectory(prefix="collection-run-demo-") as directory:
        root = Path(directory) / "data"
        batch, workbook = seed_batch(root, kind="augmentation")
        calls = []

        def mocked_client(args):
            calls.append(args)
            return {"ok": True, "output": '{"ok":true,"message":"offline mock accepted"}'}

        factory = PhoneFactoryStore(root, run_client_fn=mocked_client)
        factory.add_task({"filename": batch["filename"], "description": "offline sample",
                          "source_batch_id": batch["batch_id"],
                          "content_base64": base64.b64encode(workbook.read_bytes()).decode()})
        response = factory.remote_start({"filename": batch["filename"], "request_id": "demo-request"})
        run_id = response["collection_run_id"]
        run = factory.collection_runs.get(run_id)
        manifest = {"batch_id": batch["batch_id"], "collection_run_id": run_id,
                    "trajectories": [raw_trajectory(run, "CASE-01")],
                    "errors": [{"collection_case_id": "CASE-02", "error": "simulated unavailable phone"}]}
        factory.dispatch(f"collection-runs/{run_id}/complete", "POST", manifest)
        same, created = factory.collection_runs.complete(run_id, manifest)
        ready = factory.collection_runs.ready_input(batch["batch_id"])
        print(json.dumps({"offline": True, "network_calls": 0, "mock_dispatches": len(calls),
                          "completed_status": same["status"], "duplicate_created": created,
                          "completion_request": manifest, "ready_input": ready}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
