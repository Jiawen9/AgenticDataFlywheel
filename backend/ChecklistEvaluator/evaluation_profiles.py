"""Credential-free, immutable evaluator rule profiles (stdlib only).

Profile directories contain profile.json, a native gold task-spec JSON and
evaluator_skill/*.json. Runtime configuration is deliberately not included.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
try:
    from .file_access import io_path, logical_path, resolve_path
except ImportError:
    from file_access import io_path, logical_path, resolve_path
import re
import shutil

PROFILE_ENV = "PHONE_FACTORY_EVALUATOR_PROFILES_DIR"
IDENTITY_ENV = "PHONE_FACTORY_EVALUATOR_PROFILE_SNAPSHOT"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


def read_json(path: str | Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}: {path}")
            result[key] = value
        return result
    return json.loads(io_path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique)


def _hash(path: Path) -> str:
    return hashlib.sha256(io_path(path).read_bytes()).hexdigest()


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_gold_file(path: Path) -> dict:
    # The manager may already have a different collector `agent` package loaded.
    # Load this stdlib-only validator by exact file path, without sys.path or
    # sys.modules mutations and without importing runtime config/credentials.
    source = resolve_path(__file__).parent / "agent" / "evaluator" / "gold_checklist.py"
    spec = importlib.util.spec_from_file_location("_phonefactory_profile_gold", io_path(source))
    if spec is None or spec.loader is None:
        raise ValueError("gold checklist validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_gold_file(path)


def default_profiles_root() -> Path:
    return resolve_path(os.environ.get(PROFILE_ENV) or resolve_path(__file__).parent / "profiles")


def _inside(base: Path, relative: str, *, directory=False) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("profile files must use relative paths")
    parts = relative.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise ValueError("invalid profile relative path")
    path = base.joinpath(*parts)
    current = base
    for part in parts:
        current = current / part
        entry = io_path(current)
        if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
            raise ValueError(f"profile links are forbidden: {current}")
    if not resolve_path(path).is_relative_to(resolve_path(base)):
        raise ValueError("profile path escapes its directory")
    if not (io_path(path).is_dir() if directory else io_path(path).is_file()):
        raise ValueError(f"profile file/directory missing: {path}")
    return resolve_path(path)


def load_rules(directory: str | Path) -> list[dict]:
    directory = Path(directory)
    if not io_path(directory).is_dir():
        raise ValueError("evaluation skill directory is missing")
    files = sorted(logical_path(p) for p in io_path(directory).glob("*.json"))
    if not files:
        raise ValueError("evaluation skill directory contains no JSON rules")
    rules, ids = [], set()
    for file in files:
        if io_path(file).is_symlink():
            raise ValueError("rule file links are forbidden")
        data = read_json(file)
        if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
            raise ValueError(f"rules must be a list: {file.name}")
        for rule in data["rules"]:
            if (not isinstance(rule, dict) or not isinstance(rule.get("id"), str)
                    or not rule["id"].strip() or not isinstance(rule.get("prompt_text"), str)
                    or not rule["prompt_text"].strip()):
                raise ValueError(f"invalid evaluation rule: {file.name}")
            if rule["id"] in ids:
                raise ValueError(f"duplicate evaluation rule id: {rule['id']}")
            ids.add(rule["id"])
            rules.append(rule)
    if not rules:
        raise ValueError("profile has no evaluation rules")
    return rules


def _load_profile(root: Path, profile_id: str) -> tuple[Path, dict, dict]:
    if not isinstance(profile_id, str) or not _ID.fullmatch(profile_id):
        raise ValueError("invalid profile id")
    base = _inside(root, profile_id, directory=True)
    manifest = _inside(base, "profile.json")
    meta = read_json(manifest)
    if (not isinstance(meta, dict) or meta.get("id") != profile_id
            or not isinstance(meta.get("name"), str) or not meta["name"].strip()):
        raise ValueError("profile.json requires matching id and a name")
    gold = _inside(base, meta.get("gold_path"))
    rules = _inside(base, meta.get("rules_dir"), directory=True)
    _validate_gold_file(gold)
    load_rules(rules)
    files = {"profile.json": _hash(manifest), gold.relative_to(base).as_posix(): _hash(gold)}
    for file in sorted(logical_path(p) for p in io_path(rules).glob("*.json")):
        checked = _inside(base, file.relative_to(base).as_posix())
        files[checked.relative_to(base).as_posix()] = _hash(checked)
    return base, meta, files


def list_profiles(root: str | Path | None = None) -> dict:
    base = resolve_path(root) if root is not None else default_profiles_root()
    profiles, errors = [], []
    if not io_path(base).is_dir():
        return {"profiles": [], "errors": [{"id": "", "error": "profile directory is missing"}]}
    for item in sorted(logical_path(p) for p in io_path(base).iterdir()):
        if not io_path(item).is_dir():
            continue
        try:
            _, meta, files = _load_profile(base, item.name)
            profiles.append({"id": item.name, "name": meta["name"], "digest": _digest(files)})
        except (ValueError, OSError) as exc:
            errors.append({"id": item.name, "error": str(exc)})
    return {"profiles": profiles, "errors": errors}


def runtime_digest(runtime_root: str | Path | None = None) -> str:
    root = resolve_path(runtime_root or resolve_path(__file__).parent)
    # Engine code and declared dependency contract; no credentials/config JSON,
    # cached bytecode, tests, profiles, generated output or absolute paths.
    files = sorted(logical_path(p) for p in [*io_path(root).glob("*.py"),
                                          *io_path(root / "agent").rglob("*.py")])
    if io_path(root / "requirements.txt").is_file():
        files.append(root / "requirements.txt")
    if not files:
        raise ValueError("evaluator runtime has no engine files")
    return _digest({p.relative_to(root).as_posix(): _hash(p) for p in files})


def freeze_profile(root: str | Path | None, profile_id: str, destination: str | Path,
                   runtime_root: str | Path | None = None, expected_digest: str | None = None) -> dict:
    root = resolve_path(root) if root is not None else default_profiles_root()
    base, meta, files = _load_profile(root, profile_id)
    digest = _digest(files)
    if expected_digest and expected_digest != digest:
        raise ValueError("profile changed since selection; refresh profiles before starting")
    target = resolve_path(destination)
    if io_path(target).exists() and any(io_path(target).iterdir()):
        saved = target / "profile_snapshot.json"
        if io_path(saved).is_file():
            existing = verify_snapshot(saved, runtime_root)
            if (existing.get("profile_id") == profile_id and existing.get("profile_digest") == digest
                    and resolve_path(existing.get("snapshot_root", "")) == target):
                return existing
        raise ValueError("profile snapshot destination must be empty")
    io_path(target).mkdir(parents=True, exist_ok=True)
    for relative in files:
        copied = target / relative
        io_path(copied.parent).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(io_path(base / relative), io_path(copied))
    snapshot = {"schema": "phonefactory.evaluation-profile.v1", "profile_id": profile_id,
                "profile_name": meta["name"], "profile_digest": digest,
                "snapshot_root": str(target), "gold_path": str(target / meta["gold_path"]),
                "rules_dir": str(target / meta["rules_dir"]), "files": files,
                "runtime_digest": runtime_digest(runtime_root)}
    verify_snapshot(snapshot, runtime_root)
    io_path(target / "profile_snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return snapshot


def verify_snapshot(snapshot: dict | str | Path, runtime_root: str | Path | None = None) -> dict:
    if not isinstance(snapshot, dict):
        snapshot = read_json(snapshot)
    if not isinstance(snapshot, dict) or snapshot.get("schema") != "phonefactory.evaluation-profile.v1":
        raise ValueError("invalid evaluation profile snapshot")
    base = resolve_path(snapshot["snapshot_root"])
    files = snapshot.get("files")
    if not isinstance(files, dict) or not files or "profile.json" not in files:
        raise ValueError("profile snapshot file manifest is missing")
    actual = {relative: _hash(_inside(base, relative)) for relative in files}
    if actual != files or _digest(actual) != snapshot.get("profile_digest"):
        raise ValueError("frozen profile content digest mismatch")
    meta = read_json(base / "profile.json")
    if meta.get("id") != snapshot.get("profile_id") or meta.get("name") != snapshot.get("profile_name"):
        raise ValueError("profile identity mismatch")
    gold = _inside(base, meta.get("gold_path"))
    rules = _inside(base, meta.get("rules_dir"), directory=True)
    expected_files = {"profile.json", gold.relative_to(base).as_posix()} | {
        logical_path(p).relative_to(base).as_posix() for p in io_path(rules).glob("*.json")}
    if set(files) != expected_files:
        raise ValueError("frozen profile has unrecorded or missing rule files")
    if str(gold) != snapshot.get("gold_path") or str(rules) != snapshot.get("rules_dir"):
        raise ValueError("profile snapshot paths do not match manifest")
    _validate_gold_file(gold)
    load_rules(rules)
    if snapshot.get("runtime_digest") != runtime_digest(runtime_root):
        raise ValueError("evaluator runtime changed; create a new evaluation run")
    return dict(snapshot)


def current_identity() -> dict:
    raw = os.environ.get(IDENTITY_ENV)
    if not raw:
        return {}
    snapshot = json.loads(raw)
    return {key: snapshot[key] for key in ("profile_id", "profile_name", "profile_digest", "runtime_digest")}
