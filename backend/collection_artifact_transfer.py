"""Recoverable per-trajectory ingestion, independent of collection/evaluation clocks."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collection_runs import CollectionRunError, _component, _identifier, _relative
from .data_store.paths import contained_path
from .data_store.registry import utc_now
from .file_io import io_path, rglob, is_link_or_junction
from .task_generation.collection_batches import workbook_digest
from .batch_operations import active_batch_lock
from .batch_lifecycle import is_batch_active
from .collection_receipts import ReceiptStore
from .collection_eligibility import inspect_eligibility
from .collection_receipt_storage import ReceiptStorage

_DOWNLOADS = threading.BoundedSemaphore(4)


def stream_enabled(run):
    return run.get("request", {}).get("metadata", {}).get("collection_artifacts_version") in {1, 2}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


class ArtifactTransfer:
    """Per-run synchronization; only durable commits hold admission locks."""

    def __init__(self, manager):
        self.owner = manager
        self.records, self.runs, self.remote = manager.records, manager.runs, manager.remote

    def _envelope(self, run, value):
        options = run["request"]["metadata"]
        version = options.get("collection_artifacts_version", 1)
        if (not isinstance(value, dict) or value.get("schema_version") != version
                or value.get("collection_artifacts_version") != version
                or value.get("collection_run_id") != run["collection_run_id"]
                or value.get("batch_id") != run["batch_id"]
                or value.get("run_mode") != options.get("run_mode", "generate")
                or value.get("agent_type", "newagent") != options.get("agent_type", "newagent")):
            raise CollectionRunError("增量回传的协议、运行、批次或引擎身份不匹配")

    def _save(self, run_id, journal):
        self.records.put("collection_artifact_transfers", run_id, journal)

    @staticmethod
    def _expected(run):
        options = run["request"]["metadata"]
        if run.get("root_run_id") and isinstance(run.get("execution_instances"), list):
            instances = {(row["phone_id"], row["collection_case_id"], row["repeat_index"])
                         for row in run["execution_instances"]}
            if not instances or len(instances) != len(run["execution_instances"]):
                raise CollectionRunError("续采分段计划为空或重复")
            return instances
        associations = options.get("phone_apps")
        phones = options.get("phone_ids") or ([options["phone_id"]] if options.get("phone_id") else [])
        expected = set()
        for case, task in run["batch_tasks"].items():
            assigned = ({row["phone_id"] for row in associations if row["app"] == task.get("app")}
                        if associations else set(phones))
            if options.get("phone_id"):
                assigned &= {options["phone_id"]}
            if options.get("app") and options["app"] != task.get("app"):
                continue
            for phone in assigned:
                for index in range(1, options.get("repeat_times", 1) + 1):
                    expected.add((phone, case, index))
        if not expected:
            raise CollectionRunError("运行缺少冻结的手机/任务分配，不能结算采集数据")
        return expected

    def _item(self, item):
        if not isinstance(item, dict):
            raise CollectionRunError("增量产物索引无效")
        _identifier(item.get("artifact_id"), "采集产物编号")
        if type(item.get("sequence")) is not int or item["sequence"] < 1 or item.get("kind") not in {"trajectory", "diagnostic", "outcome"}:
            raise CollectionRunError("增量产物序号或类型无效")
        self.owner._descriptor({"size": 0, "sha256": item.get("manifest_sha256")})
        if item["kind"] == "outcome":
            if item.get("archive") is not None:
                raise CollectionRunError("无文件结果不得包含归档")
            size = 0
        else:
            size, _ = self.owner._descriptor(item.get("archive"))
        if size > self.owner.max_archive_bytes:
            raise CollectionRunError("单条轨迹归档超过接收上限", 413)
        return {key: item[key] for key in ("artifact_id", "sequence", "kind", "manifest_sha256", "archive")}

    def _entry(self, run, manifest, expected):
        self._envelope(run, manifest)
        if (manifest.get("artifact_id") != expected["artifact_id"] or manifest.get("kind") != expected["kind"]
                or manifest.get("archive") != expected["archive"] or digest(manifest) != expected["manifest_sha256"]):
            raise CollectionRunError("采集产物清单与已发布摘要不匹配")
        entry = manifest.get(expected["kind"])
        if not isinstance(entry, dict):
            raise CollectionRunError("采集产物缺少轨迹或诊断清单")
        options = run["request"]["metadata"]
        case_id = _component(entry.get("collection_case_id"), "采集用例编号")
        phone = entry.get("phone_id")
        if case_id not in run["batch_tasks"] or not isinstance(phone, str) or not phone:
            raise CollectionRunError("采集产物不属于本次任务或手机")
        phones = options.get("phone_ids") or ([options["phone_id"]] if options.get("phone_id") else [])
        if phones and phone not in phones:
            raise CollectionRunError("采集产物手机不属于本次下发设备")
        associations = options.get("phone_apps")
        if associations and not any(row["phone_id"] == phone and row["app"] == run["batch_tasks"][case_id].get("app") for row in associations):
            raise CollectionRunError("采集产物与冻结手机/App 分配不匹配")
        count, index = entry.get("repeat_times"), entry.get("repeat_index")
        if (type(count) is not int or count != options.get("repeat_times", 1) or not 1 <= count <= 100
                or type(index) is not int or not 1 <= index <= count):
            raise CollectionRunError("采集产物重复次数或序号不匹配")
        _component(entry.get("expanded_case_id"), "扩展用例编号")
        _identifier(entry.get("source_trajectory_id"), "源轨迹编号")
        if entry.get("execution_status") not in {"completed", "failed", "cancelled", "interrupted", "timeout"} | ({"not_run"} if manifest["schema_version"] == 2 else set()):
            raise CollectionRunError("采集产物执行状态无效")
        relative = _relative(entry.get("relative_dir"), "产物目录")
        if expected["kind"] == "trajectory":
            self.runs._normalize(run, {**manifest, "trajectories": [entry], "errors": []}, entry.get("collected_at") or utc_now())
        elif relative != expected["artifact_id"] or not isinstance(entry.get("error", ""), str):
            raise CollectionRunError("诊断目录或错误说明无效")
        descriptors, names = {}, set()
        if not isinstance(entry.get("files"), list):
            raise CollectionRunError("采集产物缺少完整文件清单")
        if expected["kind"] == "outcome" and (entry["files"] or manifest["schema_version"] != 2):
            raise CollectionRunError("无文件结果仅支持 v2 且文件列表必须为空")
        for file in entry["files"]:
            if not isinstance(file, dict):
                raise CollectionRunError("采集产物文件描述无效")
            name = _relative(file.get("path"), "产物文件路径")
            if not name.startswith(relative + "/") or name.casefold() in names:
                raise CollectionRunError("采集产物文件重复或越出目录")
            names.add(name.casefold())
            descriptors[name] = self.owner._descriptor(file)
        if len(descriptors) > self.owner.max_files or sum(size for size, _ in descriptors.values()) > self.owner.max_expanded_bytes:
            raise CollectionRunError("单条采集产物超出接收上限", 413)
        return entry, descriptors

    def _base(self, run, kind):
        if kind == "trajectory":
            return self.runs.run_root(run["batch_id"], run["collection_run_id"])
        base = contained_path(self.runs.raw_root(run["batch_id"]), "diagnostics", run["collection_run_id"])
        self.runs._no_links(base)
        return base

    def _verify(self, base, entry, descriptors):
        directory = contained_path(base, entry["relative_dir"])
        self.runs._no_links(directory)
        actual = set()
        for path in rglob(directory):
            if is_link_or_junction(path):
                raise CollectionRunError("产物目录不允许链接")
            if io_path(path).is_file():
                actual.add(path.relative_to(base).as_posix())
        if actual != set(descriptors):
            raise CollectionRunError("已落盘产物文件清单不匹配")
        for name, (size, checksum) in descriptors.items():
            path = contained_path(base, name)
            if io_path(path).stat().st_size != size or workbook_digest(path) != checksum:
                raise CollectionRunError("已落盘产物大小或 SHA256 不匹配")

    def _assert_current(self, run):
        current = self.runs.get(run["collection_run_id"])
        if (not current or current.get("request_digest") != run.get("request_digest")
                or current["batch_id"] != run["batch_id"]):
            raise CollectionRunError("回传期间冻结运行身份发生变化")
        if current.get("dispatch_rejected") is True:
            raise CollectionRunError("下发已明确拒绝，不能提交迟到回传")
        return current

    def _prepare_ledger(self, run):
        run_id = run["collection_run_id"]
        ledger = ReceiptStore(self.records, run_id)
        with active_batch_lock(run["batch_id"], self.owner.root):
            self._assert_current(run)
            header = self.records.get("collection_artifact_transfers", run_id) or {
                "collection_run_id": run_id, "batch_id": run["batch_id"], "cursor": 0}
        if header.get("receipt_policy_version") != 2:
            # Historical unfinished v1 requests retain their wire bytes and
            # fingerprints. Large legacy journals migrate outside admission.
            slots = list(header.get("items", {}).values())
            for start in range(0, len(slots), 100):
                if not is_batch_active(run["batch_id"], self.owner.root):
                    raise CollectionRunError("已发布批次不能迁移回传记录")
                ledger.register([self._item(slot["item"]) for slot in slots[start:start + 100]])
                for slot in slots[start:start + 100]:
                    if slot.get("manifest"):
                        ledger.save_manifest(slot["item"]["artifact_id"], slot["manifest"], slot["item"]["manifest_sha256"])
            header = {key: value for key, value in header.items()
                      if key not in {"items", "snapshot", "final_digest", "final_manifest", "status"}}
            header.update(receipt_policy_version=2, migrated_at=utc_now())
            with active_batch_lock(run["batch_id"], self.owner.root):
                self._assert_current(run)
                self._save(run_id, header)
        return ledger, header

    def _ingest(self, run, ledger, row):
        run_id, item = run["collection_run_id"], row["item"]
        artifact_id = item["artifact_id"]
        try:
            ledger.update(artifact_id, status="downloading", last_attempt_at=utc_now())
            manifest = ledger.manifest(artifact_id) or self.remote.get_collection_artifact(run_id, artifact_id)
            entry, descriptors = self._entry(run, manifest, item)
            instance = [entry["phone_id"], entry["collection_case_id"], entry["repeat_index"]]
            if tuple(instance) not in self.expected_instances:
                raise CollectionRunError("采集产物不属于冻结的轨迹实例计划")
            ledger.save_manifest(artifact_id, manifest, item["manifest_sha256"])
            ledger.claim(artifact_id, instance, entry["source_trajectory_id"], entry["relative_dir"], item["kind"])
            details = {key: entry[key] for key in ("phone_id", "collection_case_id", "expanded_case_id",
                "source_trajectory_id", "repeat_index", "repeat_times", "execution_status")}
            if entry.get("error"):
                details["error"] = str(entry["error"])[:2000]
            ledger.update(artifact_id, instance=instance, entry=details)
            base = self._base(run, item["kind"])
            destination = contained_path(base, entry["relative_dir"])
            temp = self.owner._temporary(run_id) / "artifacts" / artifact_id
            if item["kind"] == "outcome":
                quality = inspect_eligibility(self.runs, run, entry, base, "outcome")
            elif io_path(destination).exists():
                # A lost durable acknowledgement never requires downloading a
                # second copy. Verify existing immutable bytes independently.
                self._verify(base, entry, descriptors)
                quality = inspect_eligibility(self.runs, run, entry, base, item["kind"])
            else:
                files = temp / "files"
                if not (row.get("status") == "verified" and io_path(files).is_dir()):
                    self.owner._remove(temp)
                    io_path(temp).mkdir(parents=True, exist_ok=True)
                    archive = temp / "archive.zip"
                    with _DOWNLOADS:
                        self.remote.download_collection_artifact(run_id, artifact_id, io_path(archive))
                    size, checksum = self.owner._descriptor(item["archive"])
                    if not io_path(archive).is_file() or io_path(archive).stat().st_size != size or workbook_digest(archive) != checksum:
                        raise CollectionRunError("单条采集归档大小或 SHA256 不匹配")
                    self.owner._extract(archive, files, descriptors)
                self.owner._validate_tree(files, descriptors)
                quality = inspect_eligibility(self.runs, run, entry, files, item["kind"])
                source = contained_path(files, entry["relative_dir"])
                io_path(source).mkdir(parents=True, exist_ok=True)
                ledger.update(artifact_id, status="verified", eligibility=quality, error=None)
                # No network, full tree scan or hashing while publication/stop
                # admission waits. Recheck immutable identities at commit.
                with active_batch_lock(run["batch_id"], self.owner.root):
                    self._assert_current(run)
                    if ledger.get(artifact_id)["item"] != item:
                        raise CollectionRunError("提交前产物身份发生变化")
                    self.runs._no_links(destination)
                    io_path(destination.parent).mkdir(parents=True, exist_ok=True)
                    if io_path(destination).exists():
                        raise CollectionRunError("目标产物目录并发出现，拒绝覆盖")
                    os.replace(io_path(source), io_path(destination))
            with active_batch_lock(run["batch_id"], self.owner.root):
                self._assert_current(run)
                ledger.update(artifact_id, status="received", error=None, eligibility=quality,
                              completed_at=utc_now(), receipt_policy_version=2, storage_error=False)
            if io_path(temp).exists():
                self.owner._remove(temp)
        except Exception as exc:
            local_storage_failure = self.storage.on_error(exc)
            # Filesystem/network outages can recover; immutable identity/path
            # violations are quarantined. Corrupt downloads get bounded retries.
            if not is_batch_active(run["batch_id"], self.owner.root):
                return
            current = ledger.get(artifact_id) or row
            if current.get("status") == "received":
                return  # A cleanup-only failure cannot revoke a saved receipt.
            failures = current.get("failure_count", 0) + 1
            integrity = (not local_storage_failure and isinstance(exc, (CollectionRunError, ValueError))
                         and getattr(exc, "remote_kind", None) is None)
            message = str(exc)
            retry_corruption = integrity and any(word in message for word in ("SHA256", "归档大小", "无法读取采集归档", "文件大小"))
            quarantined = integrity and (not retry_corruption or failures >= 3)
            delay = (30, 60, 120, 300)[min(failures - 1, 3)]
            ledger.update(artifact_id, status="quarantined" if quarantined else "retry_wait",
                error=message[:2000], failure_count=failures,
                storage_error=False if quarantined else local_storage_failure or current.get("storage_error", False),
                next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z"))

    def _progress(self, run, ledger, snapshot):
        counts = ledger.counts()
        summary = {**snapshot.get("summary", {}), "transferred_trajectory_count": counts["trajectory"],
            "transferred_diagnostic_count": counts["diagnostic"], "received_artifact_count": counts["received"],
            "received_outcome_count": counts["outcome"], "quarantined_count": counts["quarantined"],
            "evaluation_eligible_count": counts["evaluation"], "preprocessing_eligible_count": counts["preprocessing"]}
        failures = ledger.rows(limit=10, errors_only=True)
        message = "；".join(f"{row['artifact_id']}: {row['error']}" for row in failures[:10]) or None
        transfer = "retry_wait" if counts["pending"] and message else "partial" if counts["quarantined"] else "waiting"
        with active_batch_lock(run["batch_id"], self.owner.root):
            self._assert_current(run)
            from .collection_control import merge_control
            saved = self.records.update("collection_runs", run["collection_run_id"],
                lambda current: (merge_control(current, snapshot), None)[1])
            return self.owner._update(run["collection_run_id"], receipt_policy_version=2,
                collection_artifacts_version=run["request"]["metadata"]["collection_artifacts_version"],
                collection_status=saved.get("collection_status", snapshot["collection_status"]), collection_summary=summary,
                transferred_trajectory_count=counts["trajectory"], transferred_diagnostic_count=counts["diagnostic"],
                collection_artifact_count=counts["total"], transfer_status=transfer, transfer_error=message,
                remote_status=saved.get("collection_status", snapshot["collection_status"]), archive_status=snapshot.get("archive_status"))

    def sync(self, run, *, force=False):
        self.storage = ReceiptStorage(self.owner, run)
        try:
            result = self._sync(run, force=force)
            self.storage.recover(ReceiptStore(self.records, run["collection_run_id"]))
            return result
        except Exception as exc:
            self.storage.on_error(exc)
            raise

    def _sync(self, run, *, force=False):
        run_id = run["collection_run_id"]
        if not is_batch_active(run["batch_id"], self.owner.root):
            return run
        self.expected_instances = self._expected(run)
        if hasattr(self.remote, "get_run"):
            try:
                control = self.remote.get_run(run_id)
                if (not isinstance(control, dict) or control.get("run_id", control.get("collection_run_id")) != run_id
                        or control.get("run_mode", "generate") != run["request"]["metadata"].get("run_mode", "generate")
                        or control.get("agent_type", "newagent") != run["request"]["metadata"].get("agent_type", "newagent")):
                    raise CollectionRunError("采集控制状态身份不匹配")
                self.storage.observe(control)
                from .collection_control import merge_control
                with active_batch_lock(run["batch_id"], self.owner.root):
                    self._assert_current(run)
                    self.records.update("collection_runs", run_id, lambda current: (merge_control(current, control), None)[1])
            except Exception as exc:
                self.storage.on_error(exc)
                # Independent artifact reception must keep progressing even if
                # the lightweight control observation is temporarily unavailable.
                with active_batch_lock(run["batch_id"], self.owner.root):
                    self.records.update("collection_runs", run_id, lambda current: current.update(control_error=str(exc)[:500]))
        ledger, header = self._prepare_ledger(run)
        snapshot = self.remote.get_collection_artifacts(run_id, cursor=header["cursor"], limit=100)
        self._envelope(run, snapshot)
        if (snapshot.get("collection_status") not in {"starting", "running", "stopping", "completed", "partial", "failed", "cancelled", "interrupted"}
                or type(snapshot.get("closed")) is not bool or type(snapshot.get("has_more")) is not bool
                or not isinstance(snapshot.get("items"), list) or len(snapshot["items"]) > 100
                or not isinstance(snapshot.get("summary"), dict)):
            raise CollectionRunError("采集产物索引状态无效")
        previous, items = header["cursor"], []
        for source in snapshot["items"]:
            item = self._item(source)
            if item["sequence"] != previous + 1:
                raise CollectionRunError("采集产物索引不连续")
            if item["kind"] == "outcome" and run["request"]["metadata"]["collection_artifacts_version"] != 2:
                raise CollectionRunError("旧协议不支持无文件结果")
            previous = item["sequence"]
            items.append(item)
        if (type(snapshot.get("next_cursor")) is not int or snapshot["next_cursor"] != previous
                or (snapshot["has_more"] and not items)):
            raise CollectionRunError("采集产物分页游标无效")
        with active_batch_lock(run["batch_id"], self.owner.root):
            self._assert_current(run)
            ledger.register(items)
            for source in snapshot["items"]:
                if source.get("manifest") is not None:
                    # Invalid inline payload is handled by the normal per-item
                    # quarantine path, never by a whole-run registration failure.
                    if digest(source["manifest"]) == source["manifest_sha256"]:
                        ledger.save_manifest(source["artifact_id"], source["manifest"], source["manifest_sha256"])
            header.update(cursor=previous, collection_status=snapshot["collection_status"],
                          closed=snapshot["closed"], has_more=snapshot["has_more"], summary=snapshot["summary"],
                          archive_status=snapshot.get("archive_status"))
            self._save(run_id, header)
        pending = ledger.rows(limit=110, pending=True, force=force)
        outcomes = [row for row in pending if row["item"]["kind"] == "outcome"]
        files = [row for row in pending if row["item"]["kind"] != "outcome"][:10]
        # At most two downloads per run and four across this service process.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="artifact-receipt") as pool:
            list(pool.map(lambda row: self._ingest(run, ledger, row), outcomes + files))
        result = self._progress(run, ledger, snapshot)
        counts = ledger.counts()
        if snapshot["has_more"] or not snapshot["closed"] or counts["pending"]:
            return result
        return self._finalize(run, ledger, header, snapshot)

    def _finalize(self, run, ledger, header, snapshot):
        run_id = run["collection_run_id"]
        final = self.remote.get_collection_manifest(run_id)
        self._envelope(run, final)
        if final.get("collection_status") not in {"completed", "partial", "failed", "cancelled", "interrupted"}:
            raise CollectionRunError("最终采集清单尚未结算")
        rows = ledger.rows(limit=ledger.counts()["total"])
        items = [row["item"] for row in rows]
        version = run["request"]["metadata"]["collection_artifacts_version"]
        entries, originals = [], []
        for row in rows:
            manifest = ledger.manifest(row["artifact_id"])
            if isinstance(manifest, dict) and isinstance(manifest.get(row["item"]["kind"]), dict):
                originals.append(manifest)
            # Inline wire manifests are immutable evidence, not trusted identity.
            # A rejected manifest must not crash reconciliation or contaminate
            # the verified instance set; only _ingest can publish this entry.
            if row.get("entry"):
                entries.append(row["entry"])
        if version == 1:
            if not isinstance(final.get("artifacts"), list) or [self._item(item) for item in final["artifacts"]] != items:
                raise CollectionRunError("最终采集清单与已回传索引不一致")
            for kind, field in (("trajectory", "trajectories"), ("diagnostic", "diagnostics")):
                expected = [m[kind] for m in originals if m["kind"] == kind]
                actual = final.get(field)
                if not isinstance(actual, list) or sorted(map(digest, actual)) != sorted(map(digest, expected)):
                    raise CollectionRunError("最终采集清单修改或遗漏已封存产物")
        else:
            signatures = [{key: item[key] for key in ("artifact_id", "sequence", "kind", "manifest_sha256")} for item in items]
            if (final.get("artifact_count") != len(items) or final.get("last_sequence") != len(items)
                    or final.get("artifacts_digest") != digest(signatures)
                    or final.get("plan_digest") != digest(sorted([list(row) for row in self.expected_instances]))):
                raise CollectionRunError("最终采集清单与产物索引或冻结计划摘要不一致")
        instances = [(e["phone_id"], e["collection_case_id"], e["repeat_index"]) for e in entries]
        quarantine = sum(row["status"] == "quarantined" for row in rows)
        if (len(items) != len(self.expected_instances) or len(instances) != len(set(instances))
                or not set(instances).issubset(self.expected_instances)
                or (not quarantine and set(instances) != self.expected_instances)):
            raise CollectionRunError("最终采集清单遗漏或重复冻结计划中的轨迹实例")
        expected_summary = {"original_task_count": len({case for _, case, _ in self.expected_instances}),
            "planned_trajectory_count": len(self.expected_instances),
            "collection_completed_count": sum(e["execution_status"] == "completed" for e in entries),
            "collection_cancelled_count": sum(e["execution_status"] == "cancelled" for e in entries),
            "collection_failed_count": sum(e["execution_status"] not in {"completed", "cancelled", "not_run"} for e in entries),
            "sealed_trajectory_count": sum(item["kind"] == "trajectory" for item in items),
            "diagnostic_count": sum(item["kind"] == "diagnostic" for item in items)}
        if version == 2:
            expected_summary.update(collection_not_run_count=sum(e["execution_status"] == "not_run" for e in entries),
                                    outcome_count=sum(item["kind"] == "outcome" for item in items))
        summary = final.get("summary", {})
        execution_counts = ["collection_completed_count", "collection_cancelled_count", "collection_failed_count"]
        if version == 2:
            execution_counts.append("collection_not_run_count")
        if (not isinstance(summary, dict)
                or any(type(summary.get(key)) is not int or summary[key] < 0 for key in expected_summary)
                or sum(summary[key] for key in execution_counts) != len(self.expected_instances)
                or summary["planned_trajectory_count"] != len(self.expected_instances)
                or summary["original_task_count"] != expected_summary["original_task_count"]):
            raise CollectionRunError("最终采集统计与冻结计划不一致")
        if len(entries) == len(items) and any(type(final.get("summary", {}).get(key)) is not int
                or final["summary"][key] != value for key, value in expected_summary.items()):
            raise CollectionRunError("最终采集统计与冻结计划或产物清单不一致")
        if final["collection_status"] == "completed" and any(summary[key] for key in execution_counts if key != "collection_completed_count"):
            raise CollectionRunError("未执行或执行失败的采集不能声明为全部执行完成")
        if version == 1:
            if final["collection_status"] == "completed" and (summary["diagnostic_count"] or final.get("errors")):
                raise CollectionRunError("部分失败的旧协议采集不能声明为全部成功")
            if final["collection_status"] != "completed" and not final.get("errors"):
                raise CollectionRunError("未完全成功的旧协议采集必须保留失败或取消原因")
        if header.get("original_final_digest") and header["original_final_digest"] != digest(final):
            raise CollectionRunError("最终采集清单不可修改")
        training, errors = [], list(final.get("errors", []))
        for row in rows:
            manifest = ledger.manifest(row["artifact_id"])
            entry = (manifest[row["item"]["kind"]] if row.get("entry") and manifest
                     else row.get("entry", {}))
            if row["status"] == "received" and row.get("eligibility", {}).get("preprocessing"):
                training.append(entry)
            if (row["status"] == "quarantined" or not row.get("eligibility", {}).get("preprocessing")
                    or entry.get("execution_status") != "completed"):
                errors.append({**{k: entry[k] for k in ("collection_case_id", "phone_id", "repeat_index", "source_trajectory_id") if k in entry},
                    "error": row.get("error") or entry.get("error") or ("不满足预处理资格" if not row.get("eligibility", {}).get("preprocessing") else entry.get("execution_status", "不可用"))})
        local = {"batch_id": run["batch_id"], "collection_run_id": run_id,
            "agent_type": run["request"]["metadata"].get("agent_type", "newagent"), "trajectories": training, "errors": errors}
        completed_at = run.get("completed_at") or header.get("completed_at") or utc_now()
        normalized = self.runs._normalize(run, local, completed_at)
        # Revalidate only the stable, training-qualified subset, outside locks.
        self.runs._validate_sources(run, normalized)
        with active_batch_lock(run["batch_id"], self.owner.root):
            self._assert_current(run)
            header.update(original_final_digest=digest(final), final_digest=digest(local),
                receipt_policy_version=2, reconciled=True, status="completed", completed_at=completed_at)
            self.records.put("collection_source_final_manifests", run_id,
                {"manifest": final, "manifest_sha256": digest(final)}, expected_revision=0 if not self.records.get("collection_source_final_manifests", run_id) else None)
            self._save(run_id, header)
            result = self.runs.complete_received(run_id, local, normalized, header)
            transfer = "partial" if quarantine else "completed"
            # Keep the legacy empty-data indication while all evidence receipts
            # are durable; v2 consumers use the explicit independent counts.
            if version == 1 and not training and not ledger.counts()["evaluation"] and not quarantine:
                transfer = "remote_failed"
            result = self.owner._update(run_id, transfer_status=transfer,
                transfer_error="部分产物已隔离，请查看回传明细" if quarantine else None,
                collection_status=final["collection_status"], remote_status=final["collection_status"],
                collection_manifest_sha256=digest(final), remote_errors=final.get("errors", []), receipt_closed=True)
        return result
