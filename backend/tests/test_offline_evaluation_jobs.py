"""No phones or model calls: durable async API and CLI contract regressions."""
from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend import evaluation_job_previews as jobs, model_iteration, offline_evaluation_dispatch as dispatch, phonefactory_client
from backend.phone_factory import PhoneFactoryError, PhoneFactoryStore
from backend.tests.test_model_iteration import evaluator_config, results


class ManualPool:
    def __init__(self):
        self.items = {}

    def submit(self, key, callback):
        self.items.setdefault(key, callback)

    def close(self):
        self.items.clear()

    def drain(self):
        current, self.items = self.items, {}
        for callback in current.values():
            callback()


class AsyncEvaluationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.calls, self.remote_jobs, self.remote_runs = [], {}, {}
        self.lose_preview_ack = self.lose_start_ack = False
        self.supported = True
        self.count = 300
        self.store = PhoneFactoryStore(Path(tmp.name), run_client_fn=self.client)
        self.runtime = self.store.runtime
        for name in ("_preview_pool", "_evaluation_pool", "_control_pool"):
            getattr(self.runtime, name).close()
            setattr(self.runtime, name, ManualPool())
        self.addCleanup(self.runtime.close)
        model_iteration.save_config(self.store, evaluator_config())

    def client(self, args):
        self.calls.append(args)
        command = args[0]
        value = {}
        if command == "capabilities":
            value = {"offline_evaluation_jobs": {"version": 1 if self.supported else 0, "readonly_input_version": 1}}
        elif command == "evaluation-preview":
            source = json.loads(args[2])
            pid = jobs.preview_id(source.pop("request_id"))
            if pid not in self.remote_jobs:
                self.remote_jobs[pid] = {"request": source, "status": "preparing", "revision": 1}
            elif self.remote_jobs[pid]["request"] != source:
                raise PhoneFactoryError("request conflict", 409)
            value = self.preview(pid)
            if self.lose_preview_ack:
                self.lose_preview_ack = False
                raise PhoneFactoryError("lost acknowledgement", 502)
        elif command == "evaluation-preview-status":
            value = self.preview(args[1], int(args[3]), int(args[5]))
        elif command in {"evaluation-preview-cancel", "evaluation-preview-retry"}:
            pid, body = args[1], json.loads(args[3])
            row = self.remote_jobs[pid]
            if body["expected_revision"] != row["revision"]:
                raise PhoneFactoryError("stale preview revision", 409)
            row.update(status="cancelled" if command.endswith("cancel") else "preparing", revision=row["revision"] + 1)
            value = self.preview(pid)
        elif command == "start-evaluation":
            body = json.loads(args[2])
            rid = body["collection_run_id"]
            if rid not in self.remote_runs:
                self.remote_runs[rid] = {"run_id": rid, "run_mode": "modeliter", "execution_mode": "evaluate_only",
                    "status": "preparing", "preparation": {"status": "preparing", "phase": "preparing_input",
                        "revision": 1, "progress": {"prepared": 0, "total": self.count}}, "request": body}
            elif self.remote_runs[rid]["request"] != body:
                raise PhoneFactoryError("request conflict", 409)
            value = self.remote_runs[rid]
            if self.lose_start_ack:
                self.lose_start_ack = False
                raise PhoneFactoryError("lost start acknowledgement", 502)
        elif command == "run":
            value = self.remote_runs[args[1]]
        elif command == "evaluation-preparation-retry":
            value = self.remote_runs[args[1]]
            value.update(status="preparing", preparation={"status": "preparing", "phase": "preparing_input",
                         "revision": 3, "progress": {"prepared": 1, "total": self.count}})
        elif command == "cancel-run":
            value = self.remote_runs[args[1]]
            value.update(status="cancelled", preparation={**value["preparation"], "status": "cancelled"})
        elif command == "run-evaluations":
            rid = args[1]
            value = results(rid, [{"case_key": str(i), "source_trajectory_id": str(i), "case_id": "case",
                "phone_id": "", "collection_status": "completed", "evaluation_status": "completed", "verdict": "success"}
                for i in range(self.count)])
        else:
            raise AssertionError(args)
        return {"ok": True, "output": json.dumps(value)}

    def preview(self, pid, offset=0, limit=50):
        row = self.remote_jobs[pid]
        complete = row["status"] == "completed"
        return {"job_version": 1, "preview_id": pid, "revision": row["revision"], "status": row["status"],
            "phase": "completed" if complete else row["status"], "valid": complete, "errors": [],
            "trajectory_count": self.count if complete else 0,
            "source": {"kind": "directory", "path": row["request"].get("source_path", "run")},
            "source_digest": "a" * 64, "case_offset": offset,
            "has_more": bool(complete and offset + limit < self.count),
            "cases": [{"source_trajectory_id": str(i), "case_id": "case", "task": "打开应用", "phone_id": ""}
                      for i in range(offset, min(offset + limit, self.count))] if complete else [],
            "progress": {"checked": self.count if complete else 0, "eligible": self.count if complete else 0}}

    def submit(self, **data):
        return model_iteration.preview_source(self.store, {"source_path": "D:/目录/case", "request_id": "preview-request", **data})

    def ready(self):
        value = self.submit()
        self.runtime._preview_pool.drain()
        pid = value["preview_id"]
        self.remote_jobs[pid].update(status="completed", revision=5)
        for _ in range((self.count + 99) // 100):
            jobs.refresh(self.store, pid)
        return pid

    def start(self, pid, **extra):
        return self.runtime.remote_start_evaluation({"request_id": "evaluation-request", "preview_id": pid,
            "evaluation_profile_id": "rules-v1", "evaluation_profile_digest": "1" * 64, **extra})

    def test_accept_and_status_are_local_then_persist_300_rows_as_pages(self):
        begun = time.monotonic()
        value = self.submit()
        self.assertLess(time.monotonic() - begun, 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(value["status"], "preparing")
        pid = self.ready()
        self.assertEqual(len(jobs.state(self.store, pid, 290, 10)["cases"]), 10)
        self.assertEqual(jobs.state(self.store, pid, 99, 3)["cases"][1]["source_trajectory_id"], "100")
        stored = self.store.records.get("model_iter_previews", pid)
        self.assertNotIn("cases", stored["preview"])
        self.assertLess(len(json.dumps(stored)), 3000)
        self.assertEqual(len(self.store.records.list(jobs._pages_namespace(pid, 5))), 3)
        self.assertEqual(len(set(jobs.SourceCases(self.store.records, pid, "a" * 64))), 300)

    def test_request_replay_freezes_source_and_endpoint_after_lost_ack_and_restart(self):
        with patch.dict(os.environ, {"PHONE_FACTORY_SERVER": "http://collector-a:9011"}):
            pid = self.submit()["preview_id"]
        self.lose_preview_ack = True
        self.runtime._preview_pool.drain()
        with patch.dict(os.environ, {"PHONE_FACTORY_SERVER": "http://collector-b:9011"}):
            self.assertEqual(self.submit()["preview_id"], pid)
            self.runtime._preview_pool.drain()
        self.assertEqual(len(self.remote_jobs), 1)
        self.assertTrue(all(call[-1] == "http://collector-a:9011" for call in self.calls))
        with self.assertRaisesRegex(PhoneFactoryError, "不同"):
            self.submit(source_path="D:/other")
        self.store.records.update("model_iter_previews", pid, lambda row: row.update(checked_epoch=0))
        self.runtime._schedule_previews()
        self.assertIn(pid, self.runtime._preview_pool.items)

    def test_new_request_does_not_silently_fall_back_to_old_collector(self):
        self.supported = False
        pid = self.submit()["preview_id"]
        self.runtime._preview_pool.drain()
        self.assertEqual(jobs.state(self.store, pid)["status"], "failed")
        self.assertFalse(any(call[0] == "evaluation-preview" for call in self.calls))

    def test_omitted_request_id_and_run_source_are_also_async(self):
        for source in ({"source_path": "D:/external"}, {"source_run_id": "collected"}):
            with self.subTest(source=source):
                before = len(self.calls)
                value = model_iteration.preview_source(self.store, source)
                self.assertEqual(value['status'], 'preparing')
                self.assertEqual(len(self.calls), before)
                self.assertTrue(self.store.records.get('model_iter_previews', value['preview_id'])['request_id'])
                self.runtime._preview_pool.drain()

    def test_cancel_unknown_acceptance_replays_identity_then_cancels_remote(self):
        self.lose_preview_ack = True
        pid = self.submit()["preview_id"]
        self.runtime._preview_pool.drain()
        jobs.control(self.store, pid, "cancel", {"request_id": "cancel", "expected_revision": 0})
        self.runtime._preview_pool.drain()
        self.assertEqual(len(self.remote_jobs), 1)
        self.assertEqual(self.remote_jobs[pid]["status"], "cancelled")
        self.assertEqual(jobs.state(self.store, pid)["status"], "cancelled")

    def test_cancel_before_preview_dispatch_does_not_start_scan_on_replay(self):
        pid = self.submit()['preview_id']
        jobs.control(self.store, pid, 'cancel', {'request_id': 'cancel-first', 'expected_revision': 0})
        self.runtime._preview_pool.drain()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.submit()['status'], 'cancelled')
        self.runtime._preview_pool.drain()
        self.assertEqual(self.remote_jobs, {})

    def test_stale_cancel_revision_ends_operation_and_can_retry_with_fresh_state(self):
        pid = self.submit()["preview_id"]
        self.runtime._preview_pool.drain()
        jobs.control(self.store, pid, "cancel", {"request_id": "stale", "expected_revision": 1})
        self.remote_jobs[pid]["revision"] = 2
        self.runtime._preview_pool.drain()
        row = self.store.records.get("model_iter_previews", pid)
        self.assertNotIn("pending_control", row)
        self.assertIn("stale", row["control_error"])
        self.assertEqual(row["preview"]["status"], "preparing")
        jobs.refresh(self.store, pid)
        jobs.control(self.store, pid, "cancel", {"request_id": "fresh", "expected_revision": 2})
        self.runtime._preview_pool.drain()
        self.assertEqual(jobs.state(self.store, pid)["status"], "cancelled")

    def test_failed_preview_retry_conflict_forces_one_status_reconciliation(self):
        self.count = 2
        pid = self.submit()["preview_id"]
        self.runtime._preview_pool.drain()
        self.remote_jobs[pid].update(status='failed', revision=2)
        jobs.refresh(self.store, pid)
        jobs.control(self.store, pid, 'retry', {'request_id': 'retry-old', 'expected_revision': 2})
        self.remote_jobs[pid].update(status='completed', revision=4)
        self.runtime._preview_pool.drain()
        row = self.store.records.get('model_iter_previews', pid)
        self.assertTrue(row['force_refresh'])
        self.assertEqual(row['preview']['status'], 'failed')
        jobs.refresh(self.store, pid)
        self.assertEqual(jobs.state(self.store, pid)['revision'], 4)
        self.assertEqual(jobs.state(self.store, pid)['status'], 'completed')

    def test_preparation_start_replay_and_get_never_block_or_reread_defaults(self):
        self.count = 2
        pid = self.ready()
        before = len(self.calls)
        run = self.start(pid)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(run["status"], "preparing")
        self.assertNotIn("source_cases", run["request"])
        self.lose_start_ack = True
        self.runtime._evaluation_pool.drain()
        changed = evaluator_config()
        changed["text"]["model"] = "new default"
        model_iteration.save_config(self.store, changed)
        self.assertTrue(self.start(pid)["reused"])
        self.runtime._evaluation_pool.drain()
        rid = run["run_id"]
        self.assertEqual(len(self.remote_runs), 1)
        before = len(self.calls)
        result = self.store.dispatch(f"runs/{rid}/evaluations", "GET", {}, mode="modeliter")
        self.assertEqual(len(self.calls), before)
        self.assertEqual(result["preparation"]["status"], "preparing")
        self.runtime._evaluation_pool.drain()
        self.assertFalse(any(call[0] == "run-evaluations" for call in self.calls))
        with self.assertRaisesRegex(PhoneFactoryError, "request_id"):
            self.start(pid, evaluator_config=changed)

    def test_failed_preparation_does_not_freeze_empty_report_and_retries(self):
        self.count = 2
        rid = self.start(self.ready())["run_id"]
        self.runtime._evaluation_pool.drain()
        self.remote_runs[rid].update(status="failed", preparation={"status": "failed", "phase": "preparing_input",
            "revision": 2, "error": "disk full", "retryable": True, "progress": {"prepared": 1, "total": 2}})
        self.runtime._sync_evaluation(rid)
        self.assertIsNone(self.store.records.get("model_iter_results", rid))
        dispatch.retry(self.runtime, rid, {"request_id": "retry", "expected_revision": 2})
        self.runtime._control_pool.drain()
        self.assertEqual(self.store.records.get("model_iter_runs", rid)["status"], "preparing")
        self.remote_runs[rid].update(status="succeeded", preparation={"status": "completed", "revision": 4})
        self.runtime._sync_evaluation(rid)
        self.assertTrue(self.store.records.get("model_iter_results", rid)["final"])
        self.assertEqual(self.store.records.get("model_iter_results", rid)["result"]["summary"]["success"], 2)

    def test_late_retry_ack_keeps_newer_preparation_observation(self):
        self.count = 2
        rid = self.start(self.ready())['run_id']
        self.runtime._evaluation_pool.drain()
        self.store.records.update('model_iter_runs', rid, lambda row: row.update(status='failed',
            preparation={'status': 'failed', 'revision': 2, 'retryable': True}))
        original = self.store.run_client
        def late_ack(args):
            result = original(args)
            if args[0] == 'evaluation-preparation-retry':
                self.store.records.update('model_iter_runs', rid, lambda row: row.update(status='running',
                    preparation={'status': 'completed', 'phase': 'evaluating', 'revision': 8}))
            return result
        self.store.run_client = late_ack
        dispatch.retry(self.runtime, rid, {'request_id': 'retry-late', 'expected_revision': 2})
        self.runtime._control_pool.drain()
        row = self.store.records.get('model_iter_runs', rid)
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['preparation']['revision'], 8)
        self.assertNotIn('pending_preparation_control', row)

    def test_stop_before_dispatch_never_starts_evaluator(self):
        rid = self.start(self.ready())["run_id"]
        self.runtime.cancel_evaluation(rid)
        self.runtime._evaluation_pool.drain()
        self.runtime._control_pool.drain()
        self.assertFalse(any(call[0] == "start-evaluation" for call in self.calls))
        self.assertEqual(self.store.records.get("model_iter_runs", rid)["status"], "cancelled")

    def test_definitely_rejected_start_can_stop_and_replay_does_not_restart(self):
        pid = self.ready()
        original = self.store.run_client
        def rejected(args):
            if args[0] == 'start-evaluation':
                raise PhoneFactoryError('profile missing', 409)
            return original(args)
        self.store.run_client = rejected
        rid = self.start(pid)['run_id']
        self.runtime._evaluation_pool.drain()
        self.assertTrue(self.store.records.get('model_iter_runs', rid)['dispatch_rejected'])
        result = self.runtime.cancel_evaluation(rid)
        self.assertEqual(result['status'], 'cancelled')
        self.runtime._control_pool.drain()
        self.store.run_client = original
        replay = self.start(pid)
        self.runtime._evaluation_pool.drain()
        self.assertEqual(replay['status'], 'cancelled')
        self.assertEqual(self.remote_runs, {})

    def test_pre_cancel_response_cannot_replace_new_control_state(self):
        pid = self.ready()
        previous = self.store.records.get('model_iter_previews', pid)
        stale = self.preview(pid, 0, 100)
        jobs.control(self.store, pid, 'cancel', {'request_id': 'cancel', 'expected_revision': 5})
        jobs.save_page(self.store, pid, stale, 0, previous['control_epoch'])
        self.assertEqual(jobs.state(self.store, pid)['phase'], 'cancelling')

    def test_legacy_completed_preview_and_accepted_run_remain_readable_and_replayable(self):
        from backend.evaluation_previews import accept, state
        from backend.task_generation.collection_batches import payload_digest
        legacy = {'preview_id': 'legacy', 'valid': True, 'trajectory_count': 1,
            'source': {'kind': 'directory', 'path': 'D:/old'}, 'source_digest': 'b' * 64,
            'cases': [{'source_trajectory_id': 'old-one', 'case_id': 'old', 'task': '打开应用'}], 'errors': []}
        accept(self.store, {'source_path': 'D:/old'}, legacy)
        self.assertEqual(state(self.store, 'legacy')['cases'], legacy['cases'])
        rid = 'eval_' + payload_digest('legacy-request')
        options = model_iteration.evaluation_options(self.store, {
            'evaluation_profile_id': 'retired', 'evaluation_profile_digest': 'c' * 64}, offline=True)
        self.store.records.put('model_iter_runs', rid, {'run_id': rid, 'status': 'succeeded',
            'request': {**options, 'execution_mode': 'evaluate_only', 'preview_id': 'legacy',
                        'source_cases': legacy['cases']}})
        self.store.records.delete('model_iter_previews', 'legacy')
        changed = evaluator_config()
        changed['text']['model'] = 'new-default'
        model_iteration.save_config(self.store, changed)
        replay = self.runtime.remote_start_evaluation({'request_id': 'legacy-request', 'preview_id': 'legacy'})
        self.assertTrue(replay['reused'])
        self.assertEqual(replay['request']['evaluator_config'], options['evaluator_config'])
        self.assertEqual(self.calls, [])

    def test_legacy_run_adopted_by_async_collector_keeps_preparation_failure_visible(self):
        from backend.task_generation.collection_batches import payload_digest
        rid = 'eval_' + payload_digest('legacy-preparation')
        self.store.records.put('model_iter_runs', rid, {'run_id': rid, 'status': 'preparing',
            'request': {'execution_mode': 'evaluate_only', 'evaluator_config': evaluator_config(),
                        'source_cases': [{'source_trajectory_id': 'old', 'case_id': 'old', 'task': '打开应用'}]}})
        self.remote_runs[rid] = {'run_id': rid, 'run_mode': 'modeliter', 'execution_mode': 'evaluate_only',
            'status': 'failed', 'preparation': {'status': 'failed', 'revision': 2, 'retryable': True, 'error': 'disk full'}}
        current = self.runtime._sync_evaluation(rid)
        self.assertEqual(current['status'], 'failed')
        self.assertEqual(current['preparation']['error'], 'disk full')
        self.assertFalse(any(call[0] == 'run-evaluations' for call in self.calls))
        state = self.store.dispatch(f'runs/{rid}/evaluations', 'GET', {}, mode='modeliter')
        self.assertEqual(state['preparation']['error'], 'disk full')
        self.assertEqual(state['summary']['total'], 1)


class AsyncClientTests(unittest.TestCase):
    def test_commands_keep_payload_and_frozen_endpoint(self):
        commands = {
            "evaluation-preview-cancel": "/evaluation_previews/ep_one/cancel",
            "evaluation-preview-retry": "/evaluation_previews/ep_one/retry",
            "evaluation-preparation-retry": "/runs/ep_one/evaluation-preparation/retry"}
        for command, path in commands.items():
            with self.subTest(command=command), patch.object(phonefactory_client, "SERVER", "http://old"), \
                    patch("sys.argv", ["client", command, "ep_one", "--payload", '{"request_id":"x","expected_revision":3}',
                                       "--server", "http://frozen:9011"]), \
                    patch.object(phonefactory_client, "post_json", return_value={"ok": True}) as post, redirect_stdout(io.StringIO()):
                phonefactory_client.main()
                post.assert_called_once_with(path, {"request_id": "x", "expected_revision": 3})
                self.assertEqual(phonefactory_client.SERVER, "http://frozen:9011")


if __name__ == "__main__":
    unittest.main()
