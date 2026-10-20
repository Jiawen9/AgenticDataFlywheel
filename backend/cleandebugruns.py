"""清理调试产生的采集运行记录（临时脚本，用后可删除）。

用法（在后端根目录 backend/ 下执行）：
    python clean_debug_runs.py <run_id> [<run_id> ...]

清理内容：
  - app.sqlite 中 namespace=collection_runs / collection_transfers 的记录
  - raw/collection_batches/<batch_id>/runs/<run_id> 输出目录

真实数据目录由环境变量 ADF_DATA_ROOT 决定，缺省为默认 DATA_ROOT（backend_workspace）。"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.collection_runs import CollectionRunStore  # noqa: E402
from backend.data_store import DATA_ROOT, RecordStore  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = Path(os.environ.get("ADF_DATA_ROOT") or DATA_ROOT).resolve()
    records = RecordStore(root)
    store = CollectionRunStore(root)
    cleaned = 0
    for run_id in sys.argv[1:]:
        run = records.get("collection_runs", run_id)
        if run is None:
            print(f"{run_id}: 不存在，跳过")
            continue
        batch_id = run.get("batch_id") or (run.get("request") or {}).get("batch_id")
        run_dir = store.run_root(batch_id, run_id) if batch_id else None
        records.delete("collection_runs", run_id)
        records.delete("collection_transfers", run_id)
        if run_dir is not None and run_dir.exists():
            shutil.rmtree(run_dir)
        print(f"{run_id}: 已清理（批次 {batch_id}）")
        cleaned += 1
    print(f"共清理 {cleaned} 条运行记录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
