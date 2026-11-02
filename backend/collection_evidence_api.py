"""Read-only evidence access, constrained to a verified receipt's file list."""
from pathlib import Path, PurePosixPath
import hashlib

from fastapi.responses import FileResponse
from .collection_runs import _identifier
from .phone_factory import PhoneFactoryError


def dispatch(runtime, parts, data, mode):
    run_id = _identifier(parts[1], "采集运行编号")
    run = runtime.store.collection_runs.get(run_id)
    if not run or run.get("request", {}).get("metadata", {}).get("run_mode", "generate") != mode:
        raise PhoneFactoryError("采集运行不存在", 404)
    if len(parts) == 3:
        try:
            return runtime.transfer.artifact_page(run_id, int(data.get("offset", 0)),
                int(data.get("limit", 10)), str(data.get("search", "")), str(data.get("status", "")))
        except (TypeError, ValueError) as exc:
            raise PhoneFactoryError(str(exc)) from exc
    artifact_id = _identifier(parts[3], "采集产物编号")
    source = runtime.transfer.artifact_source(run_id, artifact_id)
    files = [{"path": path, "size": size, "sha256": sha}
             for path, (size, sha) in sorted(source["files"].items())]
    if len(parts) == 4:
        return {**source["receipt"], "manifest": source["manifest"], "files": files}
    if len(parts) != 5 or parts[4] != "file":
        raise PhoneFactoryError("原件接口不存在", 404)
    name = data.get("path", "")
    if not isinstance(name, str) or name not in source["files"]:
        raise PhoneFactoryError("文件不在已校验产物清单中", 404)
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
        raise PhoneFactoryError("原件路径无效", 409)
    root = Path(source["root"]).resolve()
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root) or any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
            for p in (path, *path.parents) if p != root and p.is_relative_to(root)):
        raise PhoneFactoryError("原件路径发生变化", 409)
    size, expected = source["files"][name]
    if not path.is_file() or path.stat().st_size != size:
        raise PhoneFactoryError("原件缺失或大小发生变化", 409)
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(chunk)
    if checksum.hexdigest() != expected:
        raise PhoneFactoryError("原件摘要发生变化", 409)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
