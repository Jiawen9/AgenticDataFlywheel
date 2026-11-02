"""Small per-artifact receipts; immutable wire manifests live in separate rows."""
from __future__ import annotations

import hashlib
import json

from .data_store import RevisionConflict
from .data_store.registry import utc_now


def canonical_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


class ReceiptStore:
    def __init__(self, records, run_id):
        self.records, self.run_id = records, run_id
        self.namespace = "collection_receipts:" + run_id
        self.manifests = "collection_receipt_manifests:" + run_id
        self.claims = "collection_receipt_claims:" + run_id

    def get(self, artifact_id):
        return self.records.get(self.namespace, artifact_id)

    def update(self, artifact_id, **changes):
        return self.records.update(self.namespace, artifact_id, lambda row: row.update(**changes))

    def manifest(self, artifact_id):
        value = self.records.get(self.manifests, artifact_id)
        return value.get("manifest") if value else None

    def save_manifest(self, artifact_id, manifest, checksum):
        if canonical_digest(manifest) != checksum:
            raise ValueError("采集产物清单与已发布摘要不匹配")
        current = self.records.get(self.manifests, artifact_id)
        if current:
            if current["manifest_sha256"] != checksum or canonical_digest(current["manifest"]) != checksum:
                raise ValueError("已登记产物清单不可修改")
            return
        self.records.put(self.manifests, artifact_id,
            {"manifest": manifest, "manifest_sha256": checksum}, expected_revision=0)

    def register(self, items):
        writes = []
        for item in items:
            artifact_id = item["artifact_id"]
            current = self.get(artifact_id)
            if current:
                if current["item"] != item:
                    raise ValueError("已登记采集产物身份或摘要发生变化")
                continue
            writes.append({"namespace": self.namespace, "key": artifact_id, "expected_revision": 0,
                "payload": {"artifact_id": artifact_id, "item": item, "status": "pending",
                            "receipt_policy_version": 2, "created_at": utc_now()}})
        self.records.put_many(writes)

    def claim(self, artifact_id, instance, source_id, relative, kind):
        """Reserve identity independently of download completion, atomically."""
        keys = [canonical_digest(["instance", *instance]), canonical_digest(["source", source_id]),
                canonical_digest(["directory", kind, relative.casefold()])]
        for _ in range(3):
            writes = []
            for key in keys:
                row = self.records.get(self.claims, key)
                if row and row["artifact_id"] != artifact_id:
                    raise ValueError("不同采集产物重复占用同一轨迹实例或目录")
                if not row:
                    writes.append({"namespace": self.claims, "key": key, "expected_revision": 0,
                                   "payload": {"artifact_id": artifact_id}})
            try:
                self.records.put_many(writes)
                return
            except RevisionConflict:
                continue
        raise ValueError("轨迹实例登记冲突")

    def rows(self, *, offset=0, limit=100, pending=False, force=False, search="", status="", errors_only=False):
        if not self.records.database_path.exists():
            return []
        where, params = "namespace = ?", [self.namespace]
        if search:
            where += " AND instr(lower(COALESCE(json_extract(payload,'$.entry'),'') || ' ' || json_extract(payload,'$.artifact_id') || ' ' || COALESCE(json_extract(payload,'$.error'),'')), ?) > 0"
            params.append(str(search).lower()[:500])
        if status == "abnormal":
            where += " AND (json_extract(payload,'$.status') IN ('quarantined','retry_wait') OR json_extract(payload,'$.entry.execution_status') IN ('failed','timeout','interrupted') OR json_array_length(json_extract(payload,'$.eligibility.issues')) > 0)"
        elif status:
            where += " AND json_extract(payload,'$.status') = ?"
            params.append("received" if status == "completed" else status)
        if errors_only:
            where += " AND json_extract(payload,'$.error') IS NOT NULL AND json_extract(payload,'$.status') != 'received'"
        if pending:
            where += " AND json_extract(payload, '$.status') NOT IN ('received','quarantined')"
            if not force:
                where += " AND COALESCE(json_extract(payload, '$.next_retry_at'),'') <= ?"
                params.append(utc_now())
        order = ("COALESCE(json_extract(payload, '$.last_attempt_at'),''), " if pending else "")
        order += "json_extract(payload, '$.item.sequence')"
        with self.records._connection() as connection:
            values = connection.execute(f"SELECT payload FROM records WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                                        (*params, max(0, limit), max(0, offset))).fetchall()
        return [json.loads(value[0]) for value in values]

    def counts(self):
        with self.records._connection() as connection:
            rows = connection.execute("""SELECT json_extract(payload,'$.status'),
                json_extract(payload,'$.item.kind'), json_extract(payload,'$.eligibility.evaluation'),
                json_extract(payload,'$.eligibility.preprocessing'), COUNT(*) FROM records
                WHERE namespace=? GROUP BY 1,2,3,4""", (self.namespace,)).fetchall()
        result = dict(total=0, received=0, quarantined=0, pending=0, trajectory=0,
                      diagnostic=0, outcome=0, evaluation=0, preprocessing=0)
        for status, kind, evaluation, preprocessing, count in rows:
            result["total"] += count
            result[status if status in {"received", "quarantined"} else "pending"] += count
            if status == "received":
                result[kind] += count
                result["evaluation"] += count if evaluation else 0
                result["preprocessing"] += count if preprocessing else 0
        return result

    def storage_blocked_count(self):
        with self.records._connection() as connection:
            return connection.execute("""SELECT COUNT(*) FROM records WHERE namespace=?
                AND json_extract(payload,'$.storage_error')=1
                AND json_extract(payload,'$.status')!='received'""", (self.namespace,)).fetchone()[0]

    @staticmethod
    def public(row):
        item, entry = row["item"], row.get("entry", {})
        return {"artifact_id": item["artifact_id"], "kind": item["kind"],
                **entry, "receipt_status": row["status"],
                "transfer_status": "completed" if row["status"] == "received" else row["status"],
                "eligibility": row.get("eligibility"), "transfer_error": row.get("error")}

    def page(self, offset=0, limit=100, search="", status=""):
        limit = min(100, max(1, int(limit)))
        offset = max(0, int(offset))
        if not self.records.database_path.exists():
            return {"items": [], "total": 0, "next_offset": 0, "has_more": False}
        rows = self.rows(offset=offset, limit=limit, search=search, status=status)
        where, params = "namespace=?", [self.namespace]
        if search:
            where += " AND instr(lower(COALESCE(json_extract(payload,'$.entry'),'') || ' ' || json_extract(payload,'$.artifact_id') || ' ' || COALESCE(json_extract(payload,'$.error'),'')), ?) > 0"
            params.append(str(search).lower()[:500])
        if status == "abnormal":
            where += " AND (json_extract(payload,'$.status') IN ('quarantined','retry_wait') OR json_extract(payload,'$.entry.execution_status') IN ('failed','timeout','interrupted') OR json_array_length(json_extract(payload,'$.eligibility.issues')) > 0)"
        elif status:
            where += " AND json_extract(payload,'$.status')=?"
            params.append("received" if status == "completed" else status)
        with self.records._connection() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM records WHERE {where}", params).fetchone()[0]
        return {"items": [self.public(row) for row in rows], "total": total,
                "next_offset": offset + len(rows), "has_more": offset + len(rows) < total}
