"""Append-only execution dispositions; received archives and manifests never change."""
import copy
import re
import time

from .batch_lifecycle import is_batch_active
from .collection_artifact_transfer import ArtifactTransfer
from .collection_runs import _identifier
from .task_generation.collection_batches import payload_digest


class ResultCorrections:
    def __init__(self, service):
        self.service, self.records = service, service.records
        self.capabilities = {}

    def corrected(self, root, execution, row, correction_id=None):
        entry = row.get("entry") or {}
        key = payload_digest([root, execution, entry.get("phone_id"), entry.get("collection_case_id"), entry.get("repeat_index")])
        saved = self.records.get("collection_result_dispositions", key)
        if not saved or (correction_id is not None and saved["correction_id"] != correction_id):
            return row
        if (saved.get("artifact_id") != row.get("artifact_id") or
                saved.get("manifest_sha256") != row.get("item", {}).get("manifest_sha256")):
            return row
        result = copy.deepcopy(row)
        result["entry"].update(execution_status=saved["execution_status"],
            result_correction={k: saved[k] for k in ("correction_id", "reason", "evidence_sha256")},
            classification=saved["classification"])
        return result

    def sync(self, run, projection):
        if not is_batch_active(run["batch_id"], self.service.root):
            return
        server = self.service.phone_control._server(run)
        supported, checked = self.capabilities.get(server, (False, 0))
        if time.monotonic() - checked > 60 or not checked:
            capability = self.service.phone_control._remote("phone-control-capabilities", [], server)
            supported = capability.get("collection_resume", {}).get("result_reconciliation_version") == 1
            self.capabilities[server] = (supported, time.monotonic())
        if not supported:
            return
        root = run["collection_run_id"]
        cursor_row = self.records.get("collection_result_cursors", root) or {"cursor": 0}
        cursor = cursor_row["cursor"]
        value = self.service._remote("result-corrections", root,
            extra=["--cursor", str(cursor), "--limit", "100", "--server", server])
        if (value.get("root_run_id") != root or not isinstance(value.get("items"), list)
                or type(value.get("next_cursor")) is not int or value["next_cursor"] < cursor
                or len(value["items"]) > 100):
            raise ValueError("结果处置索引身份或游标无效")
        expected = ArtifactTransfer._expected(run)
        previous = cursor
        for item in value["items"]:
            identity = (item.get("phone_id"), item.get("collection_case_id"), item.get("repeat_index"))
            if (type(item.get("sequence")) is not int or item["sequence"] <= previous
                    or item["sequence"] > value["next_cursor"] or identity not in expected
                    or item.get("execution_status") not in {"completed", "failed", "cancelled", "interrupted"}
                    or item.get("classification") not in {"completed", "technical_failed", "business_failed", "unknown"}
                    or item.get("reason") not in {"verified_worker_result", "completion_unconfirmed"}
                    or not isinstance(item.get("evidence_sha256"), dict)
                    or (not item["evidence_sha256"] and item.get("classification") == "completed")
                    or any(not isinstance(v, str) or not re.fullmatch("[a-f0-9]{64}", v) for v in item["evidence_sha256"].values())):
                raise ValueError("结果处置身份或证据无效")
            correction_id = _identifier(item.get("correction_id"), "结果处置编号")
            execution = _identifier(item.get("execution_id"), "执行编号")
            if execution != root:
                child = self.records.get("collection_executions", execution)
                if not child or child.get("root_run_id") != root:
                    # Child execution registration occurs in the next sync pass.
                    return
            if item.get("artifact_id"):
                _identifier(item["artifact_id"], "产物编号")
                if not re.fullmatch("[a-f0-9]{64}", item.get("manifest_sha256") or ""):
                    raise ValueError("结果处置缺少原产物摘要")
            saved = {**item, "root_run_id": root}
            old = self.records.get("collection_result_corrections", correction_id)
            if old and {k:v for k,v in old.items() if k not in {"storage_revision","updated_at"}} != saved:
                raise ValueError("同编号结果处置内容冲突")
            key = payload_digest([root, execution, *identity])
            head = self.records.get("collection_resume_heads", root) or {
                "run_id": root, "batch_id": run["batch_id"], "revision": 0}
            entries = [
                {"namespace": "collection_result_dispositions", "key": key, "payload": saved},
                {"namespace": "collection_result_cursors", "key": root, "payload": {"cursor": item["sequence"]}},
            ]
            if not old:
                entries.extend([
                    {"namespace": "collection_result_corrections", "key": correction_id, "payload": saved, "expected_revision": 0},
                    {"namespace": "collection_resume_heads", "key": root,
                     "payload": {**head, "active": True, "revision": head["revision"] + 1,
                                 "result_reconciliation_pending": True}, "expected_revision": head.get("storage_revision", 0)},
                ])
            self.records.put_many(entries)
            previous = item["sequence"]
        if not value["items"] and value["next_cursor"] != cursor:
            raise ValueError("空结果处置页不能跳过游标")

    def observe(self, run, projection, execution, ordinal, plan, ledger):
        rows = ledger.rows(limit=len(plan))
        projection.observe(execution, ordinal, plan,
            [self.corrected(run["collection_run_id"], execution, row) for row in rows])
