"""
cli.py
======
Command-line entry point for the Lead Qualification Tool.

    python cli.py leads/leads_sample_50.csv
    python cli.py leads/leads_sample_50.csv --out-dir runs/monday --config config.yaml

Reads a CSV of inbound leads, scores every one against the frozen rubric,
writes outreach messages for the qualified ones and produces the report.
Every file the run writes lands in --out-dir. The industry tier cache is the
one exception: it is shared across input files by design, so it stays beside
config.yaml.

Exit codes
----------
0   a report was produced. Individual message failures do not change this;
    they are recorded in the report's failures.
1   the run stopped. One line says why, with no traceback - the conditions
    that stop a run are expected ones, not defects.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pipeline
import report_builder as RB

# Failures the run is designed to stop on. Each is reported as one line.
EXPECTED_ERRORS = (FileNotFoundError, ValueError, RuntimeError, KeyError, PermissionError)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Score a CSV of inbound leads and produce the report.",
    )
    parser.add_argument("input_csv", help="CSV with the six required lead columns")
    parser.add_argument("--out-dir", default="output",
                        help="where the run's files are written (default: output)")
    parser.add_argument("--config", default="config.yaml",
                        help="the frozen rubric and run settings (default: config.yaml)")
    return parser.parse_args(argv)


def tier_cache_path(cfg: dict, config_path: Path) -> Path:
    """The shared tier cache, resolved relative to the config file's folder.

    The cache is not a run output. It is the accumulated record of how every
    industry string this tool has ever seen was tiered, and it has to stay
    with the config whose prompt produced it - not follow --out-dir around.
    """
    configured = Path(cfg["factors"]["industry"]["normalisation"]["cache_path"])
    return configured if configured.is_absolute() else config_path.parent / configured


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log = print

    try:
        config_path = Path(args.config)
        if not config_path.exists():
            raise FileNotFoundError(f"config not found: {config_path}")
        cfg = pipeline.load_config(config_path)
        api_key = pipeline.resolve_api_key(cfg)

        pipeline.log_settings(cfg, api_key, log)
        log("")

        report, _leads = pipeline.run_pipeline(
            args.input_csv,
            cfg,
            api_key=api_key,
            out_dir=args.out_dir,
            log=log,
            tier_cache_path=tier_cache_path(cfg, config_path),
        )

        log("")
        RB.render_report(report, log)

        log("")
        log("files written:")
        for written in report["files_written"]:
            log(f"  {written}")
        return 0

    except EXPECTED_ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
