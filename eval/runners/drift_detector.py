"""Model drift detector — compares latest eval run vs 7-day rolling baseline.

Loads eval/reports/run_*.json files, computes rolling averages, and flags
regressions:
  - Pass rate delta < -5 percentage points → drift alert
  - Hallucination rate delta > +2 percentage points → hallucination drift alert

Usage:
    python -m eval.runners.drift_detector [--reports-dir PATH] [--verbose]

Exit code 1 if drift detected.
Exit code 0 if no drift (or insufficient history — documented as warning).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent.parent / "reports"
PASS_RATE_ALERT_DELTA = -5.0       # pp drop triggers alert
HALLUCINATION_RATE_ALERT_DELTA = 2.0  # pp rise triggers alert
ROLLING_WINDOW_DAYS = 7
MIN_RUNS_FOR_BASELINE = 2          # need at least 2 prior runs to detect drift


def _load_reports(reports_dir: Path) -> list[dict]:
    """Load all run_*.json reports sorted by timestamp ascending."""
    reports = []
    for path in sorted(reports_dir.glob("run_*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
                data["_path"] = str(path)
                reports.append(data)
        except Exception as e:
            logger.warning("Could not load report %s: %s", path, e)
    return reports


def _filter_window(reports: list[dict], days: int) -> list[dict]:
    """Return reports within the last N days."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    result = []
    for r in reports:
        ts_str = r.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts >= cutoff:
                result.append(r)
        except ValueError:
            result.append(r)  # include if timestamp unparseable
    return result


def detect_drift(reports_dir: Path = REPORTS_DIR, verbose: bool = False) -> bool:
    """Run drift detection. Returns True if drift detected (exit 1)."""
    reports = _load_reports(reports_dir)

    if len(reports) < 2:
        print(
            f"\nDrift detector: only {len(reports)} report(s) found — need at least 2.\n"
            "No drift baseline yet. This is expected for the first few nightly runs.\n"
            "Drift detection will be meaningful after 7+ nightly runs.\n"
        )
        return False

    latest = reports[-1]
    prior = reports[:-1]

    window_reports = _filter_window(prior, ROLLING_WINDOW_DAYS)
    if len(window_reports) < MIN_RUNS_FOR_BASELINE:
        window_reports = prior  # use all available if window is sparse
        print(
            f"  Note: fewer than {MIN_RUNS_FOR_BASELINE} runs in {ROLLING_WINDOW_DAYS}-day window; "
            f"using all {len(prior)} prior reports as baseline.\n"
        )

    def _avg(key: str) -> float:
        vals = [r[key] for r in window_reports if key in r and r[key] is not None]
        return sum(vals) / len(vals) if vals else 0.0

    baseline_pass_rate = _avg("pass_rate_pct")
    baseline_hallucination_rate = _avg("hallucination_rate_pct")

    latest_pass_rate = latest.get("pass_rate_pct", 0.0)
    latest_hallucination_rate = latest.get("hallucination_rate_pct", 0.0)

    pass_delta = latest_pass_rate - baseline_pass_rate
    hallucination_delta = latest_hallucination_rate - baseline_hallucination_rate

    print(f"\nDrift detection — latest run: {latest.get('timestamp', 'unknown')}")
    print(f"  Baseline ({len(window_reports)} runs):  pass_rate={baseline_pass_rate:.1f}%  hallucination_rate={baseline_hallucination_rate:.1f}%")
    print(f"  Latest run:               pass_rate={latest_pass_rate:.1f}%  hallucination_rate={latest_hallucination_rate:.1f}%")
    print(f"  Delta:                    pass_rate={pass_delta:+.1f}pp  hallucination_rate={hallucination_delta:+.1f}pp")

    drift_detected = False
    alerts = []

    if pass_delta < PASS_RATE_ALERT_DELTA:
        alerts.append(
            f"PASS RATE REGRESSION: {pass_delta:+.1f}pp (threshold: {PASS_RATE_ALERT_DELTA:+.1f}pp)"
        )
        drift_detected = True

    if hallucination_delta > HALLUCINATION_RATE_ALERT_DELTA:
        alerts.append(
            f"HALLUCINATION RATE SPIKE: {hallucination_delta:+.1f}pp (threshold: +{HALLUCINATION_RATE_ALERT_DELTA:.1f}pp)"
        )
        drift_detected = True

    if verbose and latest.get("category_breakdown"):
        print("\n  Category breakdown (latest):")
        for cat, stats in latest["category_breakdown"].items():
            print(f"    {cat:20s}  pass={stats.get('pass_rate_pct', 0):.0f}%  hallucination={stats.get('hallucination_rate_pct', 0):.0f}%")

    # Write drift report
    drift_report = {
        "timestamp": datetime.utcnow().isoformat(),
        "latest_run": latest.get("timestamp"),
        "baseline_runs": len(window_reports),
        "baseline_pass_rate_pct": round(baseline_pass_rate, 2),
        "baseline_hallucination_rate_pct": round(baseline_hallucination_rate, 2),
        "latest_pass_rate_pct": round(latest_pass_rate, 2),
        "latest_hallucination_rate_pct": round(latest_hallucination_rate, 2),
        "pass_rate_delta_pp": round(pass_delta, 2),
        "hallucination_rate_delta_pp": round(hallucination_delta, 2),
        "drift_detected": drift_detected,
        "alerts": alerts,
    }
    reports_dir.mkdir(parents=True, exist_ok=True)
    drift_path = reports_dir / f"drift_{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.json"
    with open(drift_path, "w") as f:
        json.dump(drift_report, f, indent=2)

    if drift_detected:
        print(f"\nDRIFT DETECTED:")
        for alert in alerts:
            print(f"  ⚠  {alert}")
        print(f"\nDrift report: {drift_path}\n")
    else:
        print(f"\nNo drift detected. Delta vs {ROLLING_WINDOW_DAYS}-day avg: "
              f"pass_rate={pass_delta:+.1f}pp, hallucination_rate={hallucination_delta:+.1f}pp ✓\n")

    return drift_detected


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description="Model drift detector")
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=REPORTS_DIR,
        help="Directory containing run_*.json reports",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    drift = detect_drift(reports_dir=args.reports_dir, verbose=args.verbose)
    sys.exit(1 if drift else 0)


if __name__ == "__main__":
    main()
