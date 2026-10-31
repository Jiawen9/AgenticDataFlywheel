"""Recoverable per-trajectory ingestion, independent of collection/evaluation clocks."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collection_runs import CollectionRunError, _component, _identifier, _relative
from .data_store.paths import contained_path
from .data_store.registry import utc_now
from .file_io import io_path, rglob, is_link_or_junction
from .task_generation.collection_batches import workbook_digest


def stream_enabled(run):
    return run.get("request", {}).get("metadata", {}).get("collection_artifacts_version") == 1


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


class ArtifactTransfer:
    """Called under the existing per-run synchronization and active-batch locks."""

    def __init__(self, manager):
        self.owner = manager
        self.records, self.runs, self.remote = manager.records, manager.runs, manager.remote

    def _envelope(self, run, value):
        options = run["request"]["metadata"]
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("collection_artifacts_version") != 1
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
        if type(item.get("sequence")) is not int or item["sequence"] < 1 or item.get("kind") not in {"trajectory", "diagnostic"}:
            raise CollectionRunError("增量产物序号或类型无效")
        self.owner._descriptor({"size": 0, "sha256": item.get("manifest_sha256")})
        size, _ = self.owner._descriptor(item.get("archive"))
        if size > self.owner.max_archive_bytes:
            raise CollectionRunError("单条轨迹归档超过接收上限", 413)
        return {key: item[key] for key in ("artifact_id", "sequence", "kind", "manifest_sha256", "archive")}

    def _entry(self, run, manifest, expected):
        self._envelope(run, manifest)
        if (manifest.get("artifact_id") != expected["artifact_id"] or manifest.get("kind") != expected["kind"]
                or manifest.get("archive") != expected["archive"] or digest(manifest) != expected["manifest_sha256"]):
            raise CollectionRunError("采集产物清单与已发布摘要不匹配")
        entry = manifest.get("trajectory" if expected["kind"] == "trajectory" else "diagnostic")
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
        if entry.get("execution_status") not in {"completed", "failed", "cancelled", "interrupted", "timeout"}:
            raise CollectionRunError("采集产物执行状态无效")
        relative = _relative(entry.get("relative_dir"), "产物目录")
        if expected["kind"] == "trajectory":
            self.runs._normalize(run, {**manifest, "trajectories": [entry], "errors": []}, entry.get("collected_at") or utc_now())
        elif relative != expected["artifact_id"] or not isinstance(entry.get("error"), str):
            raise CollectionRunError("诊断目录或错误说明无效")
        descriptors, names = {}, set()
        if not isinstance(entry.get("files"), list):
            raise CollectionRunError("采集产物缺少完整文件清单")
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

    def _ingest(self, run, journal, slot):
        run_id, item = run["collection_run_id"], slot["item"]
        manifest = slot.get("manifest") or self.remote.get_collection_artifact(run_id, item["artifact_id"])
        entry, descriptors = self._entry(run, manifest, item)
        instance = [entry["phone_id"], entry["collection_case_id"], entry["repeat_index"]]
        if tuple(instance) not in self.expected_instances:
            raise CollectionRunError("采集产物不属于冻结的轨迹实例计划")
        for other in journal["items"].values():
            other_entry = other.get("manifest", {}).get(other["item"]["kind"], {})
            if other is not slot and (other.get("instance") == instance
                    or other_entry.get("source_trajectory_id") == entry["source_trajectory_id"]
                    or (other_entry.get("relative_dir", "").casefold() == entry["relative_dir"].casefold()
                        and other["item"]["kind"] == item["kind"])):
                raise CollectionRunError("不同采集产物重复占用同一轨迹实例或目录")
        slot.update(manifest=manifest, instance=instance)
        self._save(run_id, journal)
        base = self._base(run, item["kind"])
        destination = contained_path(base, entry["relative_dir"])
        temp = self.owner._temporary(run_id) / "artifacts" / item["artifact_id"]
        if io_path(destination).exists():
            # Recovery after rename succeeded but the durable acknowledgement did not.
            self._verify(base, entry, descriptors)
        else:
            files = temp / "files"
            if slot.get("status") != "verified" or not io_path(files).is_dir():
                self.owner._remove(temp)
                io_path(temp).mkdir(parents=True, exist_ok=True)
                archive = temp / "archive.zip"
                self.remote.download_collection_artifact(run_id, item["artifact_id"], io_path(archive))
                size, checksum = self.owner._descriptor(item["archive"])
                if not io_path(archive).is_file() or io_path(archive).stat().st_size != size or workbook_digest(archive) != checksum:
                    raise CollectionRunError("单条采集归档大小或 SHA256 不匹配")
                self.owner._extract(archive, files, descriptors)
            self.owner._validate_tree(files, descriptors)
            if item["kind"] == "trajectory":
                self.runs._validate_source_files(run, {"trajectories": [entry]}, root_override=files)
            source = contained_path(files, entry["relative_dir"])
            # Empty diagnostics still have an identifiable directory.
            io_path(source).mkdir(parents=True, exist_ok=True)
            slot.update(status="verified", error=None)
            self._save(run_id, journal)
            self.runs._no_links(destination)
            io_path(destination.parent).mkdir(parents=True, exist_ok=True)
            os.replace(io_path(source), io_path(destination))
        slot.update(status="completed", error=None, completed_at=utc_now())
        self._save(run_id, journal)
        self.owner._remove(temp)

    def _progress(self, run, journal, snapshot, *, error=None):
        counts = {kind: sum(slot.get("status") == "completed" and slot["item"]["kind"] == kind
                            for slot in journal["items"].values()) for kind in ("trajectory", "diagnostic")}
        summary = {**snapshot.get("summary", {}), "transferred_trajectory_count": counts["trajectory"],
                   "transferred_diagnostic_count": counts["diagnostic"]}
        return self.owner._update(run["collection_run_id"], collection_artifacts_version=1,
            collection_status=snapshot["collection_status"], collection_summary=summary,
            transferred_trajectory_count=counts["trajectory"], transferred_diagnostic_count=counts["diagnostic"],
            transfer_status="failed" if error else "waiting", transfer_error=error,
            remote_status=snapshot["collection_status"])

    def sync(self, run, *, force=False):
        run_id = run["collection_run_id"]
        self.expected_instances = self._expected(run)
        journal = self.records.get("collection_artifact_transfers", run_id) or {
            "collection_run_id": run_id, "batch_id": run["batch_id"], "cursor": 0, "items": {}}
        snapshot = self.remote.get_collection_artifacts(run_id, cursor=journal["cursor"], limit=100)
        self._envelope(run, snapshot)
        if (snapshot.get("collection_status") not in {"starting", "running", "completed", "partial", "failed", "cancelled", "interrupted"}
                or type(snapshot.get("closed")) is not bool or type(snapshot.get("has_more")) is not bool
                or not isinstance(snapshot.get("items"), list) or len(snapshot["items"]) > 100
                or not isinstance(snapshot.get("summary"), dict)):
            raise CollectionRunError("采集产物索引状态无效")
        previous = journal["cursor"]
        for source in snapshot["items"]:
            item = self._item(source)
            if item["sequence"] != previous + 1:
                raise CollectionRunError("采集产物索引不连续")
            previous = item["sequence"]
            slot = journal["items"].get(item["artifact_id"])
            if slot is not None and slot["item"] != item:
                raise CollectionRunError("已登记采集产物身份或摘要发生变化")
            journal["items"].setdefault(item["artifact_id"], {"item": item, "status": "pending"})
        if (type(snapshot.get("next_cursor")) is not int or snapshot["next_cursor"] != previous
                or (snapshot["has_more"] and not snapshot["items"])):
            raise CollectionRunError("采集产物分页游标无效")
        journal.update(cursor=previous, snapshot=snapshot)
        self._save(run_id, journal)
        # Fair scheduling: later cases progress even if an earlier download fails.
        attempted_at = utc_now()
        pending = sorted((slot for slot in journal["items"].values() if slot.get("status") != "completed"
                          and (force or slot.get("next_retry_at", "") <= attempted_at)),
                         key=lambda slot: (slot.get("last_attempt_at", ""), slot["item"]["sequence"]))[:10]
        for slot in pending:
            slot["last_attempt_at"] = utc_now()
            try:
                self._ingest(run, journal, slot)
            except Exception as exc:
                attempts = slot.get("failure_count", 0) + 1
                delay = (30, 60, 120, 300)[min(attempts - 1, 3)]
                slot.update(error=str(exc), failure_count=attempts,
                            next_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat())
                self._save(run_id, journal)
        retained_errors = [f"{slot['item']['artifact_id']}: {slot['error']}" for slot in journal["items"].values()
                           if slot.get("status") != "completed" and slot.get("error")]
        result = self._progress(run, journal, snapshot, error="；".join(retained_errors[:10]) if retained_errors else None)
        if snapshot["has_more"] or not snapshot["closed"] or any(slot.get("status") != "completed" for slot in journal["items"].values()):
            return result
        final = self.remote.get_collection_manifest(run_id)
        self._envelope(run, final)
        if final.get("collection_status") not in {"completed", "partial", "failed", "cancelled", "interrupted"}:
            raise CollectionRunError("最终采集清单尚未结算")
        final_items = final.get("artifacts")
        if not isinstance(final_items, list) or [self._item(item) for item in final_items] != [
                slot["item"] for slot in sorted(journal["items"].values(), key=lambda slot: slot["item"]["sequence"])]:
            raise CollectionRunError("最终采集清单与已回传索引不一致")
        for kind, field in (("trajectory", "trajectories"), ("diagnostic", "diagnostics")):
            expected = [slot["manifest"][kind] for slot in journal["items"].values() if slot["item"]["kind"] == kind]
            actual = final.get(field)
            if not isinstance(actual, list) or sorted(map(digest, actual)) != sorted(map(digest, expected)):
                raise CollectionRunError("最终采集清单修改或遗漏已封存产物")
        all_entries = final["trajectories"] + final["diagnostics"]
        actual_instances = [(entry["phone_id"], entry["collection_case_id"], entry["repeat_index"]) for entry in all_entries]
        expected_instances = self.expected_instances
        if len(actual_instances) != len(set(actual_instances)) or set(actual_instances) != expected_instances:
            raise CollectionRunError("最终采集清单遗漏或重复冻结计划中的轨迹实例")
        expected_summary = {"original_task_count": len({case for _, case, _ in expected_instances}),
            "planned_trajectory_count": len(expected_instances),
            "collection_completed_count": sum(entry["execution_status"] == "completed" for entry in all_entries),
            "collection_cancelled_count": sum(entry["execution_status"] == "cancelled" for entry in all_entries),
            "collection_failed_count": sum(entry["execution_status"] not in {"completed", "cancelled"} for entry in all_entries),
            "sealed_trajectory_count": len(final["trajectories"]), "diagnostic_count": len(final["diagnostics"])}
        if any(type(final.get("summary", {}).get(key)) is not int or final["summary"][key] != value
               for key, value in expected_summary.items()):
            raise CollectionRunError("最终采集统计与冻结计划或产物清单不一致")
        if final["collection_status"] == "completed" and (expected_summary["collection_failed_count"]
                or expected_summary["collection_cancelled_count"] or expected_summary["diagnostic_count"] or final.get("errors")):
            raise CollectionRunError("部分失败的采集不能声明为全部成功")
        if final["collection_status"] != "completed" and not final.get("errors"):
            raise CollectionRunError("未完全成功的采集必须保留失败或取消原因")
        if journal.get("final_digest") and journal["final_digest"] != digest(final):
            raise CollectionRunError("最终采集清单不可修改")
        journal.update(final_digest=digest(final), final_manifest=final)
        self._save(run_id, journal)
        # No evaluation status is consulted here; only the sealed collection inventory matters.
        if final["trajectories"]:
            completed, _ = self.runs.complete(run_id, final)
            status, transfer_status, message = "completed", "completed", None
        else:
            self.runs._normalize(run, final, utc_now())
            status, transfer_status, message = "failed", "remote_failed", "采集已结束但没有可用轨迹；已保存失败诊断"
        result = self.owner._update(run_id, status=status, transfer_status=transfer_status, transfer_error=message,
            collection_status=final["collection_status"], remote_status=final["collection_status"],
            remote_errors=final.get("errors", []), collection_manifest_sha256=digest(final),
            diagnostics=final["diagnostics"])
        journal.update(status="completed", completed_at=utc_now())
        self._save(run_id, journal)
        return result
