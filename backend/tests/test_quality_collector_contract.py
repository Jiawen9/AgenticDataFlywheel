"""Opt-in real multipart/Flask/ledger contract using a model-free collector fixture.

Set PHONE_FACTORY_COLLECTOR_SOURCE to the matching collector checkout, and put
its test dependencies on PYTHONPATH. No HTTP listener or phone is started.
"""
import importlib.util
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit
from urllib.error import HTTPError
from unittest.mock import patch

from backend import phonefactory_client as cli
from backend.quality_checklist import build_task, evaluate_task
from backend.quality_fusion import enrich_task
from backend.trajectories_tree.tree_builder import Node


@unittest.skipUnless(os.environ.get("PHONE_FACTORY_COLLECTOR_SOURCE"), "explicit matching collector checkout required")
class QualityCollectorContractTests(unittest.TestCase):
    def _bridge(self):
        collector = Path(os.environ["PHONE_FACTORY_COLLECTOR_SOURCE"])
        sys.path.insert(0, str(collector))
        self.addCleanup(lambda: sys.path.remove(str(collector)))
        spec = importlib.util.spec_from_file_location("contract_collector_fixture", collector / "tests/test_phonefactory_quality_jobs.py")
        fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture_module)
        fixture = fixture_module.QualityJobsTests("test_exact_manifest_freezes_once_and_preserves_originals")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.app.add_url_rule("/capabilities", "capabilities", lambda: {"quality_checklist_jobs": {"version": 1}})
        originals = fixture.hashes()
        request_paths = []

        def open_request(request, **kwargs):
            url = urlsplit(request.full_url)
            path = url.path + ("?" + url.query if url.query else "")
            request_paths.append(path)
            if request.get_method() == "GET" and url.path.count("/") == 2 and url.path.startswith("/quality-checklist-jobs/"):
                fixture.service._work(url.path.rsplit("/", 1)[1])
            response = fixture.client.open(path, method=request.get_method(), data=request.data,
                                           headers=dict(request.header_items()))
            if response.status_code >= 400:
                raise HTTPError(request.full_url, response.status_code, "fixture", response.headers, io.BytesIO(response.data))
            return io.BytesIO(response.data)

        class Remote:
            def _remote_result(self, argv):
                args = cli.build_parser().parse_args(argv)
                return cli.cmd_quality_checklist(args)

        job = {"job_id": "platform-quality", "checklist_options": {
            "collector_server": "http://fixture.invalid", **{key: fixture.request[key] for key in
                ("evaluation_profile_id", "evaluation_profile_digest", "evaluator_config")}}}
        task = fixture.manifest["tasks"][0]
        return fixture, Remote(), job, task, open_request, request_paths, originals

    def test_platform_bridge_real_cli_flask_and_durable_results(self):
        fixture, remote, job, task, open_request, request_paths, originals = self._bridge()
        with patch.object(cli.urllib.request, "urlopen", side_effect=open_request):
            checklists = evaluate_task(fixture.root / "platform", job, task, "input-frozen", store=remote, poll_seconds=0)
            first_calls = len(fixture.calls)
            repeated = evaluate_task(fixture.root / "platform", job, task, "input-frozen", store=remote, poll_seconds=0)
        self.assertEqual(repeated, checklists)
        self.assertEqual(len(fixture.calls), first_calls)
        self.assertEqual(sum(stage == "definition" for stage, _ in fixture.calls), 1)
        self.assertEqual(fixture.hashes(), originals)
        self.assertFalse(list(fixture.service.runs.rglob("*.png")))
        self.assertTrue(all(path.startswith(("/quality-checklist-jobs", "/capabilities")) for path in request_paths))
        tree = Node(0, 0, children=[Node(1, 1, occurrences=[{"trajectory": "one"}, {"trajectory": "two"}])]).to_dict()
        result = enrich_task({"evaluations": {"one": {"global_score": 5}, "two": {"global_score": 2}}},
                             tree, checklists, ["one", "two"], checklists["one"]["definition_digest"])
        self.assertTrue(result["fusion_complete"])
        self.assertEqual(result["fusion_order"], ["one", "two"])
        self.assertAlmostEqual(result["node_scores"]["1"]["q"], .9)

    def test_existing_sources_recover_after_input_root_configuration_is_fixed(self):
        fixture, remote, job, task, open_request, _, originals = self._bridge()
        offline = fixture.service.offline
        real_roots = type(offline).allowed_roots.__get__(offline)
        with patch.object(offline, "allowed_roots", real_roots), \
                patch.dict(os.environ, {"PHONE_FACTORY_EVALUATION_INPUT_ROOTS": json.dumps([str(fixture.root / "unrelated")])}), \
                patch.object(cli.urllib.request, "urlopen", side_effect=open_request):
            failed = evaluate_task(fixture.root / "platform", job, task, "input-frozen", store=remote, poll_seconds=0)
            for result in failed.values():
                self.assertEqual(result["status"], "error")
                self.assertIn("outside configured evaluation input roots", result["error"])
                self.assertTrue(result["definition_digest"])
            self.assertFalse(any(stage == "evaluate" for stage, _ in fixture.calls))
            os.environ["PHONE_FACTORY_EVALUATION_INPUT_ROOTS"] = json.dumps([str(fixture.inputs)])
            recovered = evaluate_task(fixture.root / "platform", {**job, "job_id": "retry-quality"}, task,
                                      "input-frozen", store=remote, poll_seconds=0)
        self.assertTrue(all(result["status"] == "complete" for result in recovered.values()))
        self.assertEqual(sum(stage == "definition" for stage, _ in fixture.calls), 1)
        self.assertEqual(sum(stage == "evaluate" for stage, _ in fixture.calls), 2)
        self.assertEqual(recovered["one"]["definition_digest"], failed["one"]["definition_digest"])
        self.assertEqual(fixture.hashes(), originals)
        self.assertFalse(list(fixture.service.runs.rglob("*.png")))

    def test_real_collector_failure_reaches_platform_and_retry_reuses_success(self):
        fixture, remote, job, task, open_request, request_paths, originals = self._bridge()
        fixture.fail_sources.add("two")
        with patch.object(cli.urllib.request, "urlopen", side_effect=open_request):
            first = evaluate_task(fixture.root / "platform", job, task, "input-frozen", store=remote, poll_seconds=0)
            self.assertEqual(first["one"]["status"], "complete")
            self.assertNotIn("error", first["one"])
            self.assertEqual(first["two"]["status"], "error")
            self.assertIn("fake model temporary failure", first["two"]["error"])
            self.assertEqual(first["two"]["definition_digest"], first["one"]["definition_digest"])
            self.assertEqual(first["two"]["source"], "llm")
            sessions = [session for stage, session in fixture.calls if stage == "evaluate"]
            self.assertEqual(len(sessions), 2)

            fixture.fail_sources.clear()
            recovered = evaluate_task(fixture.root / "platform", {**job, "job_id": "retry-quality"}, task,
                                      "input-frozen", store=remote, poll_seconds=0)
            count_after_recovery = len(fixture.calls)
            repeated = evaluate_task(fixture.root / "platform", job, task, "input-frozen", store=remote, poll_seconds=0)

        self.assertEqual(recovered["one"], first["one"])
        self.assertEqual(recovered["two"]["status"], "complete")
        self.assertNotIn("error", recovered["two"])
        self.assertEqual(repeated, recovered)
        self.assertEqual(len(fixture.calls), count_after_recovery)
        evaluations = [session for stage, session in fixture.calls if stage == "evaluate"]
        self.assertEqual(evaluations.count(sessions[0]), 1, "successful trajectory must not run again")
        self.assertEqual(evaluations.count(sessions[1]), 2, "only the failed trajectory is retried")
        self.assertEqual(sum(stage == "definition" for stage, _ in fixture.calls), 1)
        self.assertTrue(any(path.endswith("/retry") for path in request_paths))
        self.assertEqual(fixture.hashes(), originals)
        self.assertFalse(list(fixture.service.runs.rglob("*.png")))

    def test_real_producer_task_text_reaches_collector_and_replaces_wrong_definition(self):
        # This joins the previously separate producer and CLI contracts. The
        # collector's real _reference task comparison must run, not a fake that
        # accepts any task attached to an otherwise readable source directory.
        from backend.tests.test_quality_source_recovery import QualitySourceRecoveryTests

        for engine in ("newagent", "pipelineagent"):
            with self.subTest(engine=engine):
                producer = QualitySourceRecoveryTests()
                producer.setUp()
                self.addCleanup(producer.doCleanups)
                directory, grouped, workbook = producer.fixture(engine)
                tasks, trajectories = producer.generate(grouped, workbook)
                quality_task = tasks[producer.task_id]
                original_task = deepcopy(quality_task.model_dump())
                snapshot_bytes = workbook.with_suffix(".json").read_bytes()
                original_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in directory.iterdir() if p.is_file()}
                manifest = build_task(producer.task_id, quality_task, trajectories, raw_root=producer.raw)
                self.assertEqual(manifest["task"], "打开目标页面")

                fixture, remote, job, _, open_request, _, _ = self._bridge()
                # The real collector inspects these generated original files.
                # Only its model/profile execution remains the existing stub.
                fixture.inputs = producer.raw
                wrong = deepcopy(manifest)
                wrong["task"] = quality_task.instruction
                ident = producer.trajectory_id
                ledger = fixture.service.ledger
                with patch.object(cli.urllib.request, "urlopen", side_effect=open_request):
                    failed = evaluate_task(fixture.root / "platform", job, wrong, "input-frozen",
                                           store=remote, poll_seconds=0)
                    self.assertIn("manifest task differs from source trajectory", failed[ident]["error"])
                    self.assertFalse(any(stage == "evaluate" for stage, _ in fixture.calls))
                    with ledger.connection() as c:
                        old_id = c.execute("SELECT id FROM jobs").fetchone()[0]
                    old_job = deepcopy(ledger.get(old_id))
                    old_definitions = {p: p.read_bytes() for p in
                                       fixture.service._directory(old_id).rglob("definition.json")}
                    self.assertEqual(len(old_definitions), 1)

                    correct = evaluate_task(fixture.root / "platform", {**job, "job_id": "retry-quality"},
                                            manifest, "input-frozen", store=remote, poll_seconds=0)
                    self.assertEqual(correct[ident]["status"], "complete", correct)
                    self.assertNotEqual(correct[ident]["definition_digest"], failed[ident]["definition_digest"])
                    call_count = len(fixture.calls)
                    repeated = evaluate_task(fixture.root / "platform", job, manifest, "input-frozen",
                                             store=remote, poll_seconds=0)
                    self.assertEqual(repeated, correct)
                    self.assertEqual(len(fixture.calls), call_count)

                with ledger.connection() as c:
                    jobs = list(c.execute("SELECT id,fingerprint FROM jobs"))
                self.assertEqual(len(jobs), 2)
                self.assertEqual(len({row[1] for row in jobs}), 2)
                self.assertEqual(ledger.get(old_id), old_job)
                self.assertEqual({p: p.read_bytes() for p in old_definitions}, old_definitions)
                self.assertEqual(sum(stage == "definition" for stage, _ in fixture.calls), 2)
                self.assertEqual(sum(stage == "evaluate" for stage, _ in fixture.calls), 1)
                self.assertEqual(quality_task.model_dump(), original_task)
                self.assertEqual(workbook.with_suffix(".json").read_bytes(), snapshot_bytes)
                self.assertEqual({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in directory.iterdir() if p.is_file()}, original_hashes)
                self.assertFalse(list(fixture.service.runs.rglob("*.jpg")))

    def test_real_task_disagreement_is_not_replaced_by_source_text(self):
        from backend.tests.test_quality_source_recovery import QualitySourceRecoveryTests

        producer = QualitySourceRecoveryTests()
        producer.setUp()
        self.addCleanup(producer.doCleanups)
        directory, grouped, workbook = producer.fixture("newagent")
        tasks, trajectories = producer.generate(grouped, workbook)
        quality_task = tasks[producer.task_id]
        # A genuinely different frozen goal must remain different; the bridge
        # must not replace it with the source text merely to make checks pass.
        quality_task.context["task_description"] = "播放另一首歌曲"
        original = (directory / "_trajectory_for_evaluate.json").read_bytes()
        manifest = build_task(producer.task_id, quality_task, trajectories, raw_root=producer.raw)
        self.assertEqual(manifest["task"], "播放另一首歌曲")
        fixture, remote, job, _, open_request, _, _ = self._bridge()
        fixture.inputs = producer.raw
        with patch.object(cli.urllib.request, "urlopen", side_effect=open_request):
            results = evaluate_task(fixture.root / "platform", job, manifest, "input-frozen",
                                    store=remote, poll_seconds=0)
        self.assertEqual(results[producer.trajectory_id]["status"], "error")
        self.assertIn("manifest task differs from source trajectory", results[producer.trajectory_id]["error"])
        self.assertFalse(any(stage == "evaluate" for stage, _ in fixture.calls))
        self.assertEqual((directory / "_trajectory_for_evaluate.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
