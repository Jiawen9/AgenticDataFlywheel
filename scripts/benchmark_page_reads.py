"""Offline HTTP read benchmark. Uses a disposable database, never production data."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.runs <= 10000 or not 5 <= args.samples <= 100:
        parser.error("--runs must be 1..10000; --samples must be 5..100")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix="adf-page-reads-") as directory:
        # Set before importing modules with a default data root. No live service,
        # phone, model or uploader is contacted by this benchmark.
        root = Path(directory)
        os.environ["ADF_DATA_ROOT"] = str(root)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from backend.data_store import RecordStore
        from backend.phone_factory import PhoneFactoryStore, create_router
        from backend.pipeline_router import router
        from backend.pipelines import PipelineManager, PipelineRuntime, STEPS

        def no_remote(_):
            raise AssertionError("summary reads must not contact the collector")

        store = PhoneFactoryStore(root, run_client_fn=no_remote)
        records = RecordStore(root)
        runtime = PipelineRuntime(root, preprocessing=None, tree=None, quality=None,
                                  cot=None, factory=store, overview=None)
        manager = PipelineManager(runtime, root)
        app = FastAPI()
        app.state.pipelines = manager
        app.include_router(router)
        app.include_router(create_router(store, prefix="/api/phone-factory", mode="generate"))
        app.include_router(create_router(store, prefix="/api/model-iter", mode="modeliter"))
        bulky = [{"case_id": f"case-{n}", "task": "离线基准任务 " * 30} for n in range(100)]
        entries = []
        for n in range(args.runs):
            batch = f"bench-{n:05}"
            created = f"2026-01-{1+n%28:02}T{n%24:02}:00:00Z"
            active = n % 10 == 0
            entries.extend([
                {"namespace": "pipelines", "key": f"pl_bench_{n:05}", "payload": {
                    "pipeline_id": f"pl_bench_{n:05}", "batch_id": batch, "name": f"流程 {n}",
                    "created_at": created, "updated_at": created,
                    "status": "running" if active else "succeeded", "mode": "manual", "start_mode": "collect",
                    "revision": 1, "steps": [{"id": step, "title": title, "status": "succeeded", "job_ids": []}
                                               for step, title in STEPS], "input_snapshot": {"tasks": bulky}}},
                {"namespace": "collection_runs", "key": f"cr_bench_{n:05}", "payload": {
                    "collection_run_id": f"cr_bench_{n:05}", "batch_id": batch, "task_source_batch_id": "source-bench",
                    "created_at": created, "status": "running" if active else "completed",
                    "collection_status": "running" if active else "completed",
                    "request": {"metadata": {"run_mode": "generate", "filename": "bench.xlsx",
                                              "agent_type": "pipelineagent", "repeat_times": 10}},
                    "collection_summary": {"planned_trajectory_count": 1000, "collection_completed_count": 900},
                    "manifest": {"tasks": bulky}, "trajectories": [], "batch_tasks": bulky}},
                {"namespace": "model_iter_runs", "key": f"eval_bench_{n:05}", "payload": {
                    "run_id": f"eval_bench_{n:05}", "batch_id": batch, "filename": "eval.xlsx",
                    "created_at": created, "status": "running" if active else "succeeded",
                    "request": {"execution_mode": "evaluate_only", "source_cases": bulky}}},
            ])
            if len(entries) >= 150:
                records.put_many(entries)
                entries.clear()
        if entries:
            records.put_many(entries)
        # The selected evaluation has 500 cases. Report history contains 30
        # rounds, each with five files; only metadata is generated here.
        selected_run = "eval_bench_00000"
        records.update("model_iter_runs", selected_run, lambda row: row["request"].update(evaluator_config={"text": {}, "vision": {}}))
        cases = [{"case_key": f"phone/case-{n}", "case_id": f"case-{n}", "phone_id": "phone",
                  "collection_status": "completed", "evaluation_status": "completed", "verdict": "success",
                  "errors": []} for n in range(500)]
        records.put("model_iter_results", selected_run, {"final": True, "result": {
            "run_id": selected_run, "run_mode": "modeliter", "cases": cases,
            "summary": {"total": 500, "evaluation_completed": 500, "success": 500, "success_rate": 1}}})
        store.runtime.read_models.index_evaluation(selected_run)
        report_rows = []
        for round_index in range(30):
            folder = {"index": round_index, "dir_name": f"round-{round_index}", "run_id": f"eval_bench_{round_index:05}"}
            for file_index in range(5):
                identity = f"report-{round_index}-{file_index}"
                report_rows.append((identity, "available", False, False, identity, {
                    "key": identity, "folder": folder, "file": {"name": identity + ".json", "file_id": identity, "size": 100}}))
        store.runtime.read_models._replace("reports", "modeliter", 1, {"ok": True, "refresh_error": None}, report_rows)
        records.start_summary_backfill()
        try:
            deadline = time.monotonic() + 30
            while not records.summary_page("collection_runs")["counts_complete"]:
                if time.monotonic() >= deadline:
                    raise RuntimeError("summary backfill did not finish")
                time.sleep(0.02)
            paths = {
                "pipelines": "/api/pipelines?view=summary&offset=0&limit=10",
                "collection": "/api/phone-factory/collection-runs?view=summary&include_published=true&offset=0&limit=10",
                "evaluation": "/api/model-iter/runs?view=summary&include_published=true&offset=0&limit=10",
                "evaluation_cases": f"/api/model-iter/runs/{selected_run}/evaluations?view=cases&offset=200&limit=10",
                "report_files": "/api/model-iter/reports?view=summary&offset=30&limit=10",
            }
            expected_totals = {**dict.fromkeys(("pipelines", "collection", "evaluation"), args.runs),
                               "evaluation_cases": 500, "report_files": 150}
            measurements = {}
            with TestClient(app) as client:
                for name, path in paths.items():
                    durations, sizes = [], []
                    for _ in range(args.samples):
                        started = time.perf_counter()
                        response = client.get(path)
                        durations.append((time.perf_counter() - started) * 1000)
                        response.raise_for_status()
                        body = response.json()
                        assert body["counts_complete"] and body["scope_counts"]["total"] == expected_totals[name], body
                        assert len(body["items"]) == min(expected_totals[name], 10), body
                        sizes.append(len(response.content))
                    ordered = sorted(durations)
                    measurements[name] = {
                        "p50_ms": round(statistics.median(durations), 2),
                        "p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 2),
                        "max_response_bytes": max(sizes),
                    }
            result = {"environment": {"python": platform.python_version(), "os": platform.platform(),
                                       "cpu_count": os.cpu_count()},
                      "runs_per_namespace": args.runs, "samples_per_endpoint": args.samples,
                      "evaluation_cases": 500, "report_rounds": 30, "report_files": 150,
                      "database_bytes": records.database_path.stat().st_size,
                      "measurements": measurements, "offline_only": True,
                      "passed": all(row["p95_ms"] <= 1000 for row in measurements.values())}
            encoded = json.dumps(result, ensure_ascii=False, indent=2)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(encoded + "\n", encoding="utf-8")
            print(encoded)
            return 0 if result["passed"] else 1
        finally:
            records.close_summary_backfill()
            store.runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
