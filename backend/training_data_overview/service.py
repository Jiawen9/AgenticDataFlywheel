"""Durable, rebuildable summaries; publication files are never modified."""
from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import date
import json
import logging
import math
from pathlib import Path
import shutil
import threading
import uuid

from ..data_store import RecordStore
from ..data_store.artifacts import _write_bytes, _write_tables, _json_bytes, _sha256
from ..data_store.paths import contained_path
from ..data_store.registry import utc_now
from .converter import ALL_DATA_COLUMNS, convert_release

log = logging.getLogger(__name__)
CONVERSIONS = "training_overview_conversions"
CATALOG = "training_overview_catalog"


def _present(value) -> bool:
    return value is not None and not (isinstance(value, float) and math.isnan(value))


def _unique(rows: list[dict], column: str) -> int:
    # DEV nunique excludes nulls, but ordinary labels such as 未分类 still count.
    return len({r.get(column) for r in rows if _present(r.get(column))})


def _counts(rows: list[dict]) -> dict:
    """DEV grouped counts; the overview subtask card separately counts rows."""
    return {"total_trajectories": _unique(rows, "轨迹"),
            "subtask_trajectories": _unique(rows, "子任务轨迹"),
            "total_steps": int(sum(r["step数量"] for r in rows if _present(r.get("step数量"))))}


def _matches(row: dict, filters: dict, *, omit: tuple[str, ...] = ()) -> bool:
    for key, column in (("source", "生产方式"), ("level1", "一级场景"), ("level2", "二级场景"), ("app", "APP")):
        value = filters.get(key)
        if key in ("source", "app") and value == "all":
            continue
        if key not in omit and value and row.get(column) != value:
            return False
    day = str(row["时间"])[:10]
    if filters.get("start_date") and day < filters["start_date"]:
        return False
    if filters.get("end_date") and day > filters["end_date"]:
        return False
    return True


def _manual_number(value) -> float:
    # Equivalent to DEV to_numeric(errors='coerce').fillna(0) for catalog values.
    try:
        number = float(value)
        return number if math.isfinite(number) else 0
    except (ValueError, TypeError):
        return 0


def summarize(catalog: dict | None, conversions: list[dict], filters: dict) -> dict:
    """DEV statistics over the committed catalog; no conversion or workbook writes."""
    for name in ("start_date", "end_date"):
        if filters.get(name):
            try:
                if date.fromisoformat(filters[name]).isoformat() != filters[name]:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("日期必须为 YYYY-MM-DD") from None
    if filters.get("start_date") and filters.get("end_date") and filters["start_date"] > filters["end_date"]:
        raise ValueError("开始日期不能晚于结束日期")
    catalog = catalog or {}
    all_rows = catalog.get("rows", [])
    rows = [r for r in all_rows if _matches(r, filters)]
    counts = _counts(rows)
    known = [r for r in rows if r.get("manual_known")]
    flywheel = [r for r in rows if "数据飞轮" in str(r.get("生产方式", ""))]
    show_manual = bool(flywheel) and any("人工精修步骤数量" in r for r in all_rows)
    overview = {**counts,
        "subtask_trajectories": len(rows),
        "manual_refine_steps": int(sum(_manual_number(r.get("人工精修步骤数量")) for r in flywheel)) if show_manual else 0,
        "show_manual_refine_steps": show_manual,
        # Preserve coverage diagnostics without changing DEV's card arithmetic.
        "manual_known_steps": sum(r["step数量"] for r in known),
        "manual_unknown_steps": sum(r["step数量"] for r in rows if not r.get("manual_known")),
        "level1_scenes": _unique(rows, "一级场景"),
        "level2_scenes": _unique(rows, "二级场景"),
        "total_apps": _unique(rows, "APP"),
        "avg_steps_per_trajectory": round(counts["total_steps"] / counts["total_trajectories"], 1) if counts["total_trajectories"] else 0,
    }
    day_groups, app_groups, scene_groups = defaultdict(list), defaultdict(list), defaultdict(list)
    actions, steps = Counter(), Counter()
    for row in rows:
        day_groups[str(row["时间"])[:10]].append(row)
        if _present(row.get("APP")):
            app_groups[row["APP"]].append(row)
        second = row.get("二级场景") if _present(row.get("二级场景")) else "(空)"
        scene_groups[f"{row.get('一级场景')}-{second}"].append(row)
        actions.update(row["action_box"])
        if _present(row.get("轨迹")) and _present(row.get("step数量")):
            # DEV subtracts one record per subtask, including zero-step records.
            steps[row["轨迹"]] += row["step数量"] - 1
    trend, cumulative = [], {"total_trajectories": 0, "subtask_trajectories": 0, "total_steps": 0}
    for day, items in sorted(day_groups.items()):
        for key, value in _counts(items).items():
            cumulative[key] += value
        trend.append({"date": day, **cumulative})
    # Facets follow DEV's individual endpoints. Callers clear level2 when asking
    # for their scene/date context; the apps endpoint always ignores only app.
    scene_options = defaultdict(set)
    for row in rows:
        if _present(row.get("一级场景")):
            children = scene_options[row["一级场景"]]
            if _present(row.get("二级场景")) and row["二级场景"]:
                children.add(row["二级场景"])
    option_dates = [str(r["时间"])[:10] for r in rows]
    warnings = list(dict.fromkeys(w for c in conversions for w in c.get("warnings", [])))
    if overview["manual_unknown_steps"]:
        warnings.append(f"当前筛选中 {overview['manual_unknown_steps']} 步缺少人工修改记录，人工精修统计覆盖不完整。")
    return {
        "schema_version": 1, "version": catalog.get("version"), "updated_at": catalog.get("updated_at"),
        "overview": overview,
        "filters": {"sources": sorted({r["生产方式"] for r in all_rows if _present(r.get("生产方式"))}),
            "scenes": [{"name": name, "level2_scenes": sorted(children)} for name, children in scene_options.items()],
            "apps": sorted({r["APP"] for r in all_rows if _present(r.get("APP")) and _matches(r, filters, omit=("app",))}),
            "date_range": {"min_date": min(option_dates, default=None), "max_date": max(option_dates, default=None)}},
        "trend": trend,
        "app_stats": [{"name": name, **_counts(items)} for name, items in sorted(app_groups.items())],
        "scene_stats": [{"name": name, "level1": items[0].get("一级场景"), "level2": items[0].get("二级场景"),
                         **_counts(items)} for name, items in sorted(scene_groups.items())],
        "action_stats": [{"category": name, "count": count} for name, count in sorted(actions.items(), key=lambda item: -item[1])],
        "step_stats": [{"steps": int(number), "count": count} for number, count in sorted(Counter(steps.values()).items())],
        "conversions": [{key: c.get(key) for key in ("release_id", "name", "status", "error", "warnings", "updated_at")} for c in conversions],
        "warnings": warnings,
        "workbook_url": "/api/training-data-overview/workbook" if catalog.get("version") else None,
    }


class TrainingOverviewManager:
    def __init__(self, registry, *, executor: Executor | None = None, converter=convert_release):
        self.registry = registry
        self.root = registry.data_root
        self.records = RecordStore(self.root)
        self.converter = converter
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="training-overview")
        self._owns_executor = executor is None
        self._lock = threading.RLock()
        self._conversion_lock = threading.Lock()
        self._futures = {}
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            # Only registered releases. Existing failed conversions await explicit retry.
            for release in self.registry.list_releases():
                current = self.records.get(CONVERSIONS, release["release_id"])
                if not current or current["status"] in {"queued", "running"}:
                    try:
                        self.submit(release["release_id"], recover=True)
                    except Exception:
                        log.exception("训练数据汇总恢复失败：%s", release["release_id"])

    def close(self) -> None:
        if self._owns_executor:
            self._executor.shutdown(wait=True)

    def submit(self, release_id: str, *, recover: bool = False) -> dict:
        with self._lock:
            release = self.registry.get(release_id)
            if release is None:
                raise FileNotFoundError("数据集发布记录不存在")
            current = self.records.get(CONVERSIONS, release_id)
            future = self._futures.get(release_id)
            if future and not future.done():
                return current
            if current and current["status"] == "succeeded" and self._outputs_valid(current):
                return current
            if current and current["status"] in {"queued", "running"} and not recover and not self._started:
                return current
            queued = {"release_id": release_id, "name": release["name"], "status": "queued", "error": None,
                      "warnings": [], "updated_at": utc_now(), "converter_version": 1}
            saved = self.records.put(CONVERSIONS, release_id, queued, expected_revision=current.get("storage_revision", 0) if current else 0)
            if self._started:
                try:
                    self._futures[release_id] = self._executor.submit(self._run, release_id)
                except Exception as exc:
                    saved = self.records.update(CONVERSIONS, release_id, lambda c: c.update(
                        status="failed", error=f"汇总任务排队失败：{exc}", updated_at=utc_now()))
            return saved

    def _outputs_valid(self, state: dict) -> bool:
        catalog = self.records.get(CATALOG, "current") or {}
        try:
            if not state.get("files") or not catalog.get("files"):
                return False
            for file in [*state["files"].values(), *catalog["files"].values()]:
                path = contained_path(self.root, file["path"])
                if not path.is_file() or _sha256(path) != file["sha256"]:
                    return False
            return True
        except (OSError, KeyError, ValueError):
            return False

    def wait(self, timeout: float = 60) -> None:
        for future in list(self._futures.values()):
            future.result(timeout=timeout)

    def _run(self, release_id: str) -> None:
        with self._conversion_lock:
            try:
                self.records.update(CONVERSIONS, release_id, lambda c: c.update(status="running", updated_at=utc_now()))
                release = self.registry.get(release_id)
                converted = self.converter(self.registry, release)
                self._publish(release_id, converted)
            except Exception as exc:
                log.exception("Training overview conversion failed for %s", release_id)
                self.records.update(CONVERSIONS, release_id, lambda c: c.update(status="failed", error=str(exc), updated_at=utc_now()))

    def _publish(self, release_id: str, converted: dict) -> None:
        # A single worker assembles immutable files, then atomically exposes one catalog.
        catalog = self.records.get(CATALOG, "current") or {}
        rows = [r for r in catalog.get("rows", []) if r["release_id"] != release_id] + converted["rows"]
        rows.sort(key=lambda r: (r["时间"], r["轨迹"], r["子任务轨迹"]))
        version = uuid.uuid4().hex
        base = contained_path(self.root, "system", "training_data_overview", "exports")
        temporary, final = base / ("." + version + ".tmp"), base / version
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            _write_bytes(temporary / "conversion.json", _json_bytes(converted))
            _write_bytes(temporary / "all_data.json", _json_bytes({"schema_version": 1, "version": version, "rows": rows}))
            _write_tables(temporary / "all_data.xlsx", {"all_data": [{key: r[key] for key in ALL_DATA_COLUMNS} for r in rows]})
            files = {name: {"path": (final / name).relative_to(self.root).as_posix(), "sha256": _sha256(temporary / name)}
                     for name in ("conversion.json", "all_data.json", "all_data.xlsx")}
            temporary.rename(final)
            state = self.records.get(CONVERSIONS, release_id)
            state.update(status="succeeded", error=None, warnings=converted.get("warnings", []), updated_at=utc_now(),
                         version=version, files=files, trajectory_count=len(converted["rows"]))
            self.records.put_many([
                {"namespace": CATALOG, "key": "current", "expected_revision": catalog.get("storage_revision", 0),
                 "payload": {"schema_version": 1, "version": version, "updated_at": utc_now(), "rows": rows, "files": files}},
                {"namespace": CONVERSIONS, "key": release_id, "expected_revision": state["storage_revision"], "payload": state},
            ])
        except BaseException:
            for candidate in (temporary, final):
                if candidate.is_dir() and candidate.resolve().is_relative_to(base.resolve()):
                    shutil.rmtree(candidate)
            raise

    def query(self, **filters) -> dict:
        # These two record sets must describe the same committed revision.
        if not self.records.database_path.exists():
            return summarize(None, [], filters)
        with self.records._connection() as connection:
            connection.execute("BEGIN")
            catalog = self.records._read(connection, CATALOG, "current")
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone()
            conversions = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM records WHERE namespace=? ORDER BY record_key", (CONVERSIONS,))] if exists else []
            connection.rollback()
        return summarize(catalog, conversions, filters)

    def workbook(self) -> Path:
        catalog = self.records.get(CATALOG, "current")
        if not catalog:
            raise FileNotFoundError("尚无可下载的训练数据汇总表")
        file = catalog["files"]["all_data.xlsx"]
        path = contained_path(self.root, file["path"])
        if not path.is_file() or _sha256(path) != file["sha256"]:
            raise ValueError("汇总表缺失或 SHA256 校验失败")
        return path
