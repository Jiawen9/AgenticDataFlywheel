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
    def test_platform_bridge_real_cli_flask_and_durable_results(self):
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
        with patch.object(cli.urllib.request, "urlopen", side_effect=open_request):
            checklists = evaluate_task(fixture.root / "platform", job, task, "input-frozen", store=Remote(), poll_seconds=0)
            first_calls = len(fixture.calls)
            repeated = evaluate_task(fixture.root / "platform", job, task, "input-frozen", store=Remote(), poll_seconds=0)
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


if __name__ == "__main__":
    unittest.main()
