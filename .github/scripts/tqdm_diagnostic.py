"""Repeat the original test without changing its timers or assertion."""
import concurrent.futures
import contextlib
import json
from importlib.metadata import version
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import traceback


def worker(count):
  from test.null.test_tqdm import TestProgressBar
  failures = []
  errors = []
  # Non-TTY output, as in CI. Each process has its own output file.
  with tempfile.TemporaryFile(mode="w+") as output, contextlib.redirect_stderr(output):
    for iteration in range(count):
      try:
        TestProgressBar("test_tqdm_perf").test_tqdm_perf()
      except AssertionError as exc:
        tb = exc.__traceback__
        while tb and tb.tb_frame.f_code.co_name != "test_tqdm_perf":
          tb = tb.tb_next
        values = tb.tb_frame.f_locals if tb else {}
        failures.append({"iteration": iteration, **{k: values[k] for k in ("tqdm_time", "tinytqdm_time", "tqdm_med_time", "tinytqdm_med_time", "tqdm_times", "tinytqdm_times") if k in values}})
      except Exception:
        errors.append({"iteration": iteration, "traceback": traceback.format_exc()})
  return {"attempts": count, "failures": failures, "errors": errors}


if __name__ == "__main__":
  destination = Path(sys.argv[1])
  destination.mkdir(parents=True, exist_ok=True)
  report = {"platform": platform.platform(), "machine": platform.machine(), "python": sys.version,
            "cpu_count": os.cpu_count(), "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "packages": {name: version(name) for name in ("numpy", "tqdm")},
            "note": "Focused repetitions with non-TTY file output; not the full original pytest/xdist workload."}
  report["isolated"] = [worker(1000)]
  with concurrent.futures.ProcessPoolExecutor(max_workers=3) as pool:
    report["three_processes"] = list(pool.map(worker, [1000] * 3))
  (destination / "results.json").write_text(json.dumps(report, indent=2))
  lines = ["# macOS progress-bar diagnostic", "", f"Subject commit: `{report['commit']}`", "", report["note"], ""]
  for mode in ("isolated", "three_processes"):
    attempts = sum(x["attempts"] for x in report[mode])
    failures = sum(len(x["failures"]) for x in report[mode])
    errors = sum(len(x["errors"]) for x in report[mode])
    lines.append(f"- {mode}: {failures} assertion failures, {errors} other errors / {attempts} attempts")
  summary = "\n".join(lines) + "\n"
  print(summary)
  (destination / "summary.md").write_text(summary)
  if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
      stream.write(summary)
  # Assertion failures are diagnostic results, not a broken workflow.
  if any(x["errors"] for mode in ("isolated", "three_processes") for x in report[mode]):
    sys.exit(1)
