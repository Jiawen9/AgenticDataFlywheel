"""Unified rollout export and Jiawen rubric-generation entry point."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import sys
from pathlib import Path

from trajectory_tools.gui_trajectory_excel import QwenSummarizer, export_trajectory_workbook
from trajectory_tools.settings import DEFAULT_ENV_FILE, configure_model_environment, load_repository_env


PROJECT_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
DEFAULT_SOURCE = REPOSITORY_ROOT / "backend_workspace" / "rollout_trajectories"
DEFAULT_WORKBOOK = REPOSITORY_ROOT / "backend_workspace" / "rubric_trajectories.xlsx"
DEFAULT_CACHE = REPOSITORY_ROOT / "backend_workspace" / "rubric_outputs" / "cache" / "qwen_summaries.json"
DEFAULT_CONFIG = PROJECT_ROOT / "examples" / "jiawen_rubric_config.json"
GENERATOR_PATH = PROJECT_ROOT / "examples" / "generate-jiawen-rubrics.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export rollout trajectories and generate Jiawen rubrics.")
    parser.add_argument("command", choices=("export", "generate", "all"))
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--workbook", type=Path, default=DEFAULT_WORKBOOK)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--skip-model", action="store_true", help="Export placeholder observations without Qwen.")
    return parser


def _load_generator() -> object:
    spec = importlib.util.spec_from_file_location("generate_jiawen_rubrics", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load rubric generator: {GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_export(args: argparse.Namespace) -> None:
    source = args.source.resolve()
    output = args.workbook.resolve()
    summarizer = None
    if not args.skip_model:
        values = load_repository_env(args.env_file.resolve())
        summarizer = QwenSummarizer(
            model=values["MODEL_NAME"],
            base_url=values["MODEL_URL"],
            api_key=values["YUNAI_API_KEY"],
            cache_path=args.cache.resolve(),
        )
    counts = export_trajectory_workbook(source, output, summarizer)
    print(f"Exported {counts[0]} task(s), {counts[1]} trajectories and {counts[2]} steps to {output}")


def run_generate(args: argparse.Namespace) -> None:
    configure_model_environment(args.env_file.resolve())
    generator = _load_generator()
    original_argv = sys.argv
    try:
        sys.argv = [str(GENERATOR_PATH), "--config", str(args.config.resolve())]
        asyncio.run(generator.main())
    finally:
        sys.argv = original_argv


def main() -> int:
    args = build_parser().parse_args()
    if args.command in {"export", "all"}:
        run_export(args)
    if args.command in {"generate", "all"}:
        run_generate(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
