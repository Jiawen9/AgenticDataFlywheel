"""Recoverable HTTP result ingestion; a remote idle phone is never a success signal."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import threading
import zipfile
from pathlib import Path

from .batch_lifecycle import is_batch_active
from .batch_operations import active_batch_lock
from .collection_runs import CollectionRunError, CollectionRunStore, _identifier, _relative
from .data_store import DATA_ROOT, RecordStore
from .data_store.paths import contained_path
from .data_store.registry import utc_now
from .task_generation.collection_batches import payload_digest, workbook_digest

LOG = logging.getLogger(__name__)
TERMINAL = {"completed", "succeeded", "partial", "failed", "interrupted", "cancelled"}


class CollectionTransferManager:
    def __init__(self, root: Path = DATA_ROOT, collection_runs: CollectionRunStore | None = None,
                 remote=None, *, interval: float = 10, max_archive_bytes: int = 2 * 1024**3,
                 max_expanded_bytes: int = 8 * 1024**3, max_files: int = 100000):
        self.root = contained_path(Path(root))
        self.runs = collection_runs or CollectionRunStore(self.root)
        self.records = RecordStore(self.root)
        self.remote = remote
        self.interval, self.max_archive_bytes = interval, max_archive_bytes
        self.max_expanded_bytes, self.max_files = max_expanded_bytes, max_files
        self._stop = threading.Event()
        self._thread = None
        self._guard = threading.Lock()

    def start(self):
        with self._guard:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._poll, name="collection-result-transfer", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def _poll(self):
        while not self._stop.is_set():
            try:
                self.recover()
            except Exception:
                LOG.exception("Collection result synchronization failed")
            self._stop.wait(self.interval)

    def recover(self):
        """Retry only registered, active production runs; never start collection."""
        results = []
        for run in self.runs.list_runs():
            if self._stop.is_set():
                break
            if run["status"] == "interrupted" or (run["status"] == "completed" and run.get("transfer_status") == "completed") or not is_batch_active(run["batch_id"], self.root):
                continue
            metadata = run.get("request", {}).get("metadata", {})
            if metadata.get("run_mode", "generate") != "generate":
                continue
            # Legacy local collectors do not advertise this transfer protocol.
            if not (metadata.get("result_transport") == "http" or run.get("transfer_status")
                    or run.get("dispatch_response", {}).get("result_transport") == "http"):
                continue
            if run.get("remote_status") in TERMINAL and run.get("transfer_status") == "remote_failed":
                continue
            try:
                results.append(self.sync(run["collection_run_id"]))
            except Exception:
                LOG.warning("Collection run %s is awaiting result synchronization", run["collection_run_id"], exc_info=True)
        return results

    def _update(self, run_id, **changes):
        return self.records.update("collection_runs", run_id, lambda value: value.update(
            **changes, transfer_updated_at=utc_now()))

    def _temporary(self, run_id):
        return contained_path(self.root, "tmp", "collection_transfers", _identifier(run_id, "采集运行编号"))

    def _remove(self, path):
        path = Path(path)
        allowed = contained_path(self.root, "tmp", "collection_transfers")
        if not path.resolve().is_relative_to(allowed) or path.resolve() == allowed or path.is_symlink():
            raise CollectionRunError("拒绝清理回传暂存目录之外的文件")
        if path.exists():
            shutil.rmtree(path)

    @staticmethod
    def _descriptor(item):
        if not isinstance(item, dict):
            raise CollectionRunError("归档文件描述无效")
        size, checksum = item.get("size"), item.get("sha256")
        if type(size) is not int or size < 0 or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
            raise CollectionRunError("归档文件大小或 SHA256 无效")
        return size, checksum.lower()

    def _manifest(self, run, manifest):
        if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
                or manifest.get("batch_id") != run["batch_id"]
                or manifest.get("collection_run_id") != run["collection_run_id"]
                or manifest.get("run_mode", "generate") != "generate"):
            raise CollectionRunError("远端完成清单的协议、批次、运行或模式不匹配")
        normalized = self.runs._normalize(run, manifest, run.get("completed_at") or utc_now())
        metadata = run.get("request", {}).get("metadata", {})
        phones = metadata.get("phone_ids") or ([metadata["phone_id"]] if metadata.get("phone_id") else [])
        descriptors, names = {}, set()
        for trajectory in manifest["trajectories"]:
            phone_id = trajectory.get("phone_id")
            if not isinstance(phone_id, str) or not phone_id or len(phone_id) > 256 or (phones and phone_id not in phones):
                raise CollectionRunError("远端轨迹手机不属于本次下发设备")
            for item in trajectory["files"]:
                name = _relative(item.get("path"), "归档文件路径")
                if name.casefold() in names:
                    raise CollectionRunError("归档清单文件路径重复")
                names.add(name.casefold())
                descriptors[name] = self._descriptor(item)
        reports = manifest.get("reports", [])
        if not isinstance(reports, list):
            raise CollectionRunError("报告清单必须为列表")
        for report in reports:
            name = _relative(report.get("path"), "报告路径")
            if not name.startswith("reports/") or name.casefold() in names:
                raise CollectionRunError("报告路径越界或重复")
            names.add(name.casefold())
            descriptors[name] = self._descriptor(report)
        if len(descriptors) > self.max_files or sum(size for size, _ in descriptors.values()) > self.max_expanded_bytes:
            raise CollectionRunError("结果归档超出平台接收上限", 413)
        archive_size, archive_hash = self._descriptor(manifest.get("archive"))
        if archive_size > self.max_archive_bytes:
            raise CollectionRunError("结果压缩包超出平台接收上限", 413)
        return normalized, descriptors, archive_size, archive_hash

    def _extract(self, archive_path, destination, descriptors):
        destination.mkdir(parents=True, exist_ok=False)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                infos = archive.infolist()
                if len(infos) > self.max_files * 2:
                    raise CollectionRunError("结果归档文件数量超限", 413)
                seen, files = set(), {}
                for item in infos:
                    name = _relative(item.filename.rstrip("/") if item.is_dir() else item.filename, "ZIP 路径")
                    if name.casefold() in seen or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                        raise CollectionRunError("ZIP 包含重复路径、链接或加密文件")
                    seen.add(name.casefold())
                    mode = stat.S_IFMT(item.external_attr >> 16)
                    if mode not in {0, stat.S_IFDIR, stat.S_IFREG}:
                        raise CollectionRunError("ZIP 包含不允许的特殊文件")
                    if item.is_dir():
                        if not any(path.startswith(name + "/") for path in descriptors):
                            raise CollectionRunError("ZIP 包含未声明目录")
                        continue
                    if name not in descriptors or item.file_size != descriptors[name][0]:
                        raise CollectionRunError("ZIP 文件清单或大小与完成清单不一致")
                    files[name] = item
                if set(files) != set(descriptors):
                    raise CollectionRunError("ZIP 缺少清单中的文件")
                for name, item in files.items():
                    target = contained_path(destination, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    expected_size, expected_hash = descriptors[name]
                    checksum, received = hashlib.sha256(), 0
                    with archive.open(item) as source, target.open("xb") as output:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            received += len(block)
                            if received > expected_size:
                                raise CollectionRunError("ZIP 解压文件超出声明大小")
                            checksum.update(block)
                            output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if received != expected_size or checksum.hexdigest() != expected_hash:
                        raise CollectionRunError(f"ZIP 源文件校验失败：{name}")
        except (zipfile.BadZipFile, OSError) as exc:
            raise CollectionRunError(f"无法读取采集归档：{exc}") from exc

    def _validate_tree(self, directory, descriptors):
        actual = set()
        for path in directory.rglob("*"):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise CollectionRunError("回传文件不允许链接")
            if path.is_file():
                relative = path.relative_to(directory).as_posix()
                actual.add(relative)
                if relative not in descriptors or path.stat().st_size != descriptors[relative][0] or workbook_digest(path) != descriptors[relative][1]:
                    raise CollectionRunError("回传文件在提交前发生变化")
        if actual != set(descriptors):
            raise CollectionRunError("回传文件在提交前缺失")

    def _finish(self, run, journal):
        manifest = journal["manifest"]
        if payload_digest(manifest) != journal["manifest_sha256"]:
            raise CollectionRunError("回传恢复日志校验失败")
        normalized, descriptors, _, _ = self._manifest(run, manifest)
        staging = self._temporary(run["collection_run_id"]) / "files"
        final = self.runs.run_root(run["batch_id"], run["collection_run_id"])
        if staging.exists():
            self._validate_tree(staging, descriptors)
            self.runs._validate_source_files(run, normalized, root_override=staging)
            if final.exists():
                if any(final.iterdir()):
                    raise CollectionRunError("目标运行目录已有文件，拒绝覆盖")
                final.rmdir()
            final.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(final)
        self._validate_tree(final, descriptors)
        completed, _ = self.runs.complete(run["collection_run_id"], manifest)
        # If the process stops between complete and this update, the manifest is
        # already authoritative and the next sync only finalizes this journal.
        completed = self._update(run["collection_run_id"], transfer_status="completed", transfer_error=None,
            remote_status=journal["remote_status"], remote_errors=manifest.get("errors", []))
        self.records.put("collection_transfers", run["collection_run_id"], {**journal, "status": "completed", "completed_at": utc_now()})
        self._remove(self._temporary(run["collection_run_id"]))
        return completed

    def sync(self, run_id):
        run = self.runs.get(run_id)
        if run is None:
            raise CollectionRunError("采集运行不存在", 404)
        if run.get("source_kind") == "rollout_import":
            return run  # Local imports have no remote execution to synchronize.
        if run["status"] == "interrupted":
            return run
        if run["status"] == "completed" and (run.get("transfer_status") == "completed" or not is_batch_active(run["batch_id"], self.root)):
            return run
        with active_batch_lock(run["batch_id"], self.root):
            run = self.runs.get(run_id)
            if run["status"] == "interrupted":
                return run
            if run["status"] == "completed":
                journal = self.records.get("collection_transfers", run_id)
                return self._finish(run, journal) if journal and journal.get("status") == "verified" else run
            if run.get("request", {}).get("metadata", {}).get("run_mode", "generate") != "generate":
                raise CollectionRunError("评估运行不能登记为生产采集结果")
            try:
                journal = self.records.get("collection_transfers", run_id)
                if journal and journal.get("status") == "verified":
                    return self._finish(run, journal)
                remote = self.remote.get_run(run_id)
                if (not isinstance(remote, dict) or remote.get("collection_run_id", remote.get("run_id")) != run_id
                        or remote.get("batch_id") != run["batch_id"] or remote.get("run_mode") != "generate"):
                    raise CollectionRunError("远端运行身份与已下发批次不匹配")
                state = remote.get("status")
                if not isinstance(state, str):
                    raise CollectionRunError("远端运行状态无效")
                run = self._update(run_id, remote_status=state, remote_errors=remote.get("errors", []),
                    transfer_status="waiting", transfer_error=None)
                if state not in TERMINAL:
                    return run
                manifest = remote.get("manifest")
                if manifest is None:
                    return self._update(run_id, status="interrupted" if state in {"interrupted", "cancelled"} else "failed",
                        transfer_status="remote_failed", transfer_error="远端运行结束但没有可验证的完成清单")
                normalized, descriptors, size, checksum = self._manifest(run, manifest)
                if not normalized["trajectories"]:
                    # A valid empty ZIP or an evaluation-only report is not a
                    # completed production collection. Keep remote diagnostics
                    # accessible while leaving preprocessing unavailable.
                    return self._update(run_id,
                        status="interrupted" if state in {"interrupted", "cancelled"} else "failed",
                        transfer_status="remote_failed", remote_errors=manifest.get("errors", []),
                        transfer_error="远端运行结束但没有可用轨迹；请查看采集错误和报告")
                temp = self._temporary(run_id)
                self._remove(temp)
                temp.mkdir(parents=True)
                self._update(run_id, transfer_status="downloading", transfer_error=None)
                archive = temp / "archive.zip"
                self.remote.download_archive(run_id, archive)
                if not archive.is_file() or archive.stat().st_size != size or workbook_digest(archive) != checksum:
                    raise CollectionRunError("远端归档的大小或 SHA256 不匹配")
                self._extract(archive, temp / "files", descriptors)
                self.runs._validate_source_files(run, normalized, root_override=temp / "files")
                # The same missing collected_at must stay stable across retries.
                manifest = {**manifest, "trajectories": [{**original, "collected_at": value["collected_at"]}
                    for original in manifest["trajectories"] for value in normalized["trajectories"]
                    if original["relative_dir"] == value["relative_dir"]]}
                journal = {"collection_run_id": run_id, "batch_id": run["batch_id"], "status": "verified",
                    "manifest": manifest, "manifest_sha256": payload_digest(manifest), "remote_status": state,
                    "verified_at": utc_now()}
                self.records.put("collection_transfers", run_id, journal)
                return self._finish(run, journal)
            except Exception as exc:
                # A failed DB acknowledgement may have committed completion.
                current = self.runs.get(run_id)
                if current and current["status"] != "completed":
                    self._update(run_id, transfer_status="failed", transfer_error=str(exc))
                raise
