"""Opt-in real multipart/Flask/ledger contract using a model-free collector fixture.

Set PHONE_FACTORY_COLLECTOR_SOURCE to the matching collector checkout, and put
its test dependencies on PYTHONPATH. No HTTP listener or phone is started.
"""
import importlib.util
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
from backend.quality_checklist import evaluate_task
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


if __name__ == "__main__":
    unittest.main()
