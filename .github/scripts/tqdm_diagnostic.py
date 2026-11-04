"""Compare unchanged original and median test methods under the same conditions."""
import contextlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import traceback


def compare(count):
  from test.null.test_tqdm import TestProgressBar
  names = ("test_tqdm_perf_original", "test_tqdm_perf")
  results = {name: {"attempts": 0, "failures": [], "errors": [], "elapsed_seconds": []} for name in names}
  # Both methods write to the same non-TTY file, with no concurrent test workers.
  with tempfile.TemporaryFile(mode="w+") as output, contextlib.redirect_stderr(output):
    for iteration in range(count):
      for name in names:
        result = results[name]
        test = TestProgressBar(name)
        start = time.perf_counter()
        try:
          getattr(test, name)()
        except AssertionError as exc:
          tb = exc.__traceback__
          while tb and tb.tb_frame.f_code.co_name != name:
            tb = tb.tb_next
          values = tb.tb_frame.f_locals if tb else {}
          keys = ("tqdm_time", "tinytqdm_time", "tqdm_med_time", "tinytqdm_med_time", "tqdm_times", "tinytqdm_times")
          result["failures"].append({"iteration": iteration, **{key: values[key] for key in keys if key in values}})
        except Exception:
          result["errors"].append({"iteration": iteration, "traceback": traceback.format_exc()})
        finally:
          result["elapsed_seconds"].append(time.perf_counter() - start)
          result["attempts"] += 1
  return results


if __name__ == "__main__":
  destination = Path(sys.argv[1])
  destination.mkdir(parents=True, exist_ok=True)
  count = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
  report = {"platform": platform.platform(), "machine": platform.machine(), "python": sys.version,
            "cpu_count": os.cpu_count(), "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "packages": {name: version(name) for name in ("numpy", "tqdm")},
            "note": "One process, original then median in each pair; identical non-TTY file output. Not the full pytest/xdist workload."}
  report["comparison"] = compare(count)
  (destination / "results.json").write_text(json.dumps(report, indent=2))
  lines = ["# macOS progress-bar comparison", "", f"Subject commit: `{report['commit']}`", "", report["note"], "",
           "| Version | Attempts | Assertion failures | Other errors | Total measured runtime |",
           "|---|---:|---:|---:|---:|"]
  for name, result in report["comparison"].items():
    total = sum(result["elapsed_seconds"])
    lines.append(f"| {name} | {result['attempts']} | {len(result['failures'])} | {len(result['errors'])} | {total:.6f} s |")
  summary = "\n".join(lines) + "\n"
  print(summary)
  (destination / "summary.md").write_text(summary)
  if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
      stream.write(summary)
  # Assertion failures are recorded outcomes, rather than workflow infrastructure failures.
  if any(result["errors"] for result in report["comparison"].values()):
    sys.exit(1)
