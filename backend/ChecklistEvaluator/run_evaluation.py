"""One isolated evaluation job; the 9011 service owns queues and retries.

The launcher replaces only its own process with a sanitized child environment.
This keeps the same PID, so scheduler timeouts cannot leave a model worker orphan.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
try:
    from .file_access import io_path, logical_path
except ImportError:
    from file_access import io_path, logical_path
import re
import sys

from runtime_config import SNAPSHOT_ENV, build_child_environment, config_digest, load_config
from report_validation import sha256_file, validate_report
from worker_lock import WorkerBusy, case_lock
from evaluation_profiles import IDENTITY_ENV, verify_snapshot


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--trajectory-path", required=True)
    result.add_argument("--case-id", required=True)
    result.add_argument("--session-id", required=True)
    result.add_argument("--output-dir", required=True)
    result.add_argument("--key-elements", default="")
    result.add_argument("--config-json", required=True)
    result.add_argument("--resume-auto", action="store_true")
    result.add_argument("--checklist-source", choices=("auto", "llm"), default="llm")
    result.add_argument("--profile-snapshot", default="")
    result.add_argument("--expected-trajectory-sha256", default="")
    result.add_argument("--input-reference", default="")
    result.add_argument("--input-reference-sha256", default="")
    result.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    return result


def redact(value: str, environment: dict[str, str]) -> str:
    try:
        cfg = json.loads(environment.get(SNAPSHOT_ENV, "{}"))
        secrets = [environment.get(item.get("api_key_env", ""), "") for item in cfg.values()]
    except (ValueError, AttributeError, TypeError):
        secrets = []
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", value)


class RedactedStream:
    def __init__(self, stream, environment):
        self.stream = stream
        self.environment = environment

    def write(self, text):
        self.stream.write(redact(str(text), self.environment))
        return len(text)

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _save_result(output: Path, payload: dict) -> None:
    output.mkdir(parents=True, exist_ok=True)
    path = output / "_evaluation_result.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _can_execve() -> bool:
    # Windows CRT exec is not POSIX process replacement and can fail with
    # redirected handles; the launcher already is an isolated child of 9011.
    return os.name != "nt"


@contextmanager
def _snapshot_environment(environment: dict[str, str]):
    previous = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(environment)
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _worker_main(args: argparse.Namespace) -> int:
    stdout, stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = RedactedStream(stdout, dict(os.environ))
        sys.stderr = RedactedStream(stderr, dict(os.environ))
        payload, code = run_worker(args)
        print(json.dumps(payload, ensure_ascii=False))
        return code
    finally:
        sys.stdout, sys.stderr = stdout, stderr


def run_worker(args: argparse.Namespace) -> tuple[dict, int]:
    output = io_path(args.output_dir).resolve()
    trajectory = io_path(args.trajectory_path).resolve()
    if output == trajectory.parent or trajectory.parent in output.parents:
        return {"status": "error", "reason": "output_must_be_separate_from_collection",
                "report_path": "", "trajectory_sha256": "", "config_digest": "", "exit_code": 1}, 1
    try:
        with case_lock(output):
            # Explicit process-local rules, never inherit a previous run's
            # profile. Context restoration also makes in-process tests safe.
            environment = dict(os.environ)
            for name in (IDENTITY_ENV, "GUI_AGENT_GOLD_CHECKLIST_PATH", "GUI_AGENT_EVALUATOR_RULES_DIR",
                         "GUI_AGENT_EVALUATOR_STRICT_RULES"):
                environment.pop(name, None)
            with _snapshot_environment(environment):
                return _run_worker_unlocked(args)
    except WorkerBusy:
        # Never overwrite the live worker's result/checkpoint when a competing
        # scheduler restart tries the same job.
        return {"status": "error", "reason": "worker_busy", "report_path": "",
                "trajectory_sha256": "", "config_digest": "", "exit_code": 75}, 75


def _run_worker_unlocked(args: argparse.Namespace) -> tuple[dict, int]:
    output = io_path(args.output_dir).resolve()
    trajectory = io_path(args.trajectory_path).resolve()
    payload = {"status": "error", "report_path": "", "trajectory_sha256": "",
               "config_digest": "", "reason": "", "case_id": args.case_id,
               "session_id": args.session_id, "exit_code": 1}
    reference = None
    try:
        if output == trajectory.parent or trajectory.parent in output.parents:
            raise ValueError("evaluation output must be separate from the collection case directory")
        from readonly_inputs import REFERENCE_ENV, OUTPUT_ENV, load_reference, verify_reference
        reference = None
        os.environ.pop(REFERENCE_ENV, None)
        os.environ[OUTPUT_ENV] = str(output)
        if getattr(args, 'input_reference', ''):
            reference = load_reference(args.input_reference, getattr(args, 'input_reference_sha256', ''), trajectory)
            verify_reference(reference)
            os.environ[REFERENCE_ENV] = str(io_path(args.input_reference).resolve())
        raw = json.loads(os.environ[SNAPSHOT_ENV])
        digest = config_digest(raw)
        payload["config_digest"] = digest
        payload["trajectory_sha256"] = sha256_file(trajectory)
        expected_trajectory = getattr(args, "expected_trajectory_sha256", "")
        if expected_trajectory and expected_trajectory != payload["trajectory_sha256"]:
            raise ValueError("trajectory digest differs from queued input snapshot")
        policy = getattr(args, "checklist_source", "llm")
        validation = {"checklist_policy": policy}
        if policy == "auto":
            if not getattr(args, "profile_snapshot", ""):
                raise ValueError("auto checklist source requires --profile-snapshot")
            snapshot = verify_snapshot(args.profile_snapshot)
            os.environ[IDENTITY_ENV] = json.dumps(snapshot, ensure_ascii=False)
            os.environ["GUI_AGENT_GOLD_CHECKLIST_PATH"] = snapshot["gold_path"]
            os.environ["GUI_AGENT_EVALUATOR_RULES_DIR"] = snapshot["rules_dir"]
            os.environ["GUI_AGENT_EVALUATOR_STRICT_RULES"] = "true"
            validation.update({key: snapshot[key] for key in ("profile_id", "profile_digest", "runtime_digest")})
            payload.update({key: snapshot[key] for key in ("profile_id", "profile_name", "profile_digest", "runtime_digest")})
        elif policy != "llm":
            raise ValueError("checklist source must be auto or llm")
        elif getattr(args, "profile_snapshot", ""):
            raise ValueError("llm legacy mode must not carry a profile snapshot")
        record = json.loads(trajectory.read_text(encoding="utf-8-sig"))
        if (not isinstance(record, dict) or not isinstance(record.get("task"), str)
                or not record["task"].strip() or not isinstance(record.get("actions_flat"), list)):
            raise ValueError("trajectory requires task and actions_flat")
        reports = sorted(output.glob("_report_*.json"), key=lambda path: (path.stat().st_mtime_ns, path.name))
        checkpoint = output / "_evaluator_checkpoint.json"
        if (reports or checkpoint.exists()) and not args.resume_auto:
            raise ValueError("output directory is not fresh; resume the same job or select a new output directory")
        if reports:
            prior = validate_report(reports[-1], trajectory, config_digest=digest, session_id=args.session_id, **validation)
            if prior["valid"]:
                previous = json.loads(reports[-1].read_text(encoding="utf-8-sig"))
                expected_elements = hashlib.sha256(args.key_elements.encode("utf-8")).hexdigest()
                if previous.get("key_elements_sha256") != expected_elements:
                    raise ValueError("completed report key elements do not match")
                payload.update(status="complete", report_path=str(reports[-1]), verdict=prior["verdict"],
                               reused=True, checklist_source=previous.get("checklist_source", ""), exit_code=0)
                _save_result(output, payload)
                return payload, 0
            if prior["status"] == "error":
                raise ValueError("existing report is incompatible: " + prior["reason"])
        from agent.evaluator.evaluator_agent import _eval_and_save
        report = _eval_and_save(
            str(trajectory), args.session_id, str(output), key_elements=args.key_elements,
            skip_unreferenced_steps=False, resume_auto=args.resume_auto,
            evaluation_mode="checklist", case_id=args.case_id, checklist_policy=policy,
        )
        if reference is not None:
            verify_reference(reference)
        if policy == "auto":
            verify_snapshot(snapshot)
        checked = validate_report(report, trajectory, config_digest=digest, session_id=args.session_id, **validation)
        saved_report = json.loads(Path(report).read_text(encoding="utf-8-sig"))
        code = 0 if checked["valid"] else 2 if checked["status"] == "incomplete" else 1
        payload.update(status=checked["status"], report_path=str(Path(report).resolve()),
                       verdict=checked["verdict"], reason=checked["reason"], exit_code=code,
                       checklist_source=saved_report.get("checklist_source", ""))
    except Exception as exc:
        if reference is not None:
            try:
                verify_reference(reference)
            except (ValueError, OSError) as source_error:
                exc = source_error
        payload.update(status="error", reason=redact(f"{type(exc).__name__}: {exc}", dict(os.environ)), exit_code=1)
    _save_result(output, payload)
    return payload, int(payload["exit_code"])


def main(argv: list[str] | None = None) -> int:
    actual = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(actual)
    if not args._worker:
        try:
            cfg = load_config(args.config_json)
            env = build_child_environment(cfg, os.environ)
            if not _can_execve():
                # Applies only inside this launcher process, never the manager;
                # the context also restores state for in-process CLI tests.
                with _snapshot_environment(env):
                    return _worker_main(args)
            command = [sys.executable, "-B", str(Path(__file__).resolve()), *actual, "--_worker"]
            os.execve(sys.executable, command, env)
        except Exception as exc:
            payload = {"status": "error", "reason": redact(f"{type(exc).__name__}: {exc}", dict(os.environ)),
                       "report_path": "", "trajectory_sha256": "", "config_digest": "", "exit_code": 1}
            _save_result(Path(args.output_dir), payload)
            print(json.dumps(payload, ensure_ascii=False))
            return 1
        return 1  # execve does not return on success
    return _worker_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
