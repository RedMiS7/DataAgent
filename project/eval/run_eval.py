"""Run the evaluation from a checkout: same code and options as
``python -m app.evaluation run``, writing eval/results/latest.json unless --output says
otherwise. Runs and case results also go to the app database's evaluation tables."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "backend"))

from app.evaluation.__main__ import add_run_arguments, cmd_run  # noqa: E402

RESULT_DIR = PROJECT_DIR / "eval" / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run DataAgent evaluation")
    add_run_arguments(parser, default_output=RESULT_DIR / "latest.json")
    return cmd_run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
