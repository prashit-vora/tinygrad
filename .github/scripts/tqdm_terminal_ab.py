"""Compare real versus fixed terminal-size lookup without per-call profiling."""
import argparse
import contextlib
import gc
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

from tqdm_diagnostic import instrument


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("destination", type=Path)
  parser.add_argument("--count", type=int, default=10000)
  args = parser.parse_args()
  if not 1 <= args.count <= 100000:
    parser.error("count must be between 1 and 100000 pairs")
  args.destination.mkdir(parents=True, exist_ok=True)
  run, _ = instrument(Path(__file__).with_name("context_switch_probe.py"))
  import tinygrad.helpers as helpers
  rows, collections = [], []
  def record_gc(phase, info):
    collections.append({"phase": phase, "wall": time.perf_counter(), "generation": info["generation"]})

  with tempfile.TemporaryFile(mode="w+") as output, contextlib.redirect_stderr(output):
    size = shutil.get_terminal_size()
    # Replace only tinygrad's module reference, leaving external tqdm untouched.
    fixed_shutil = SimpleNamespace(**{**vars(shutil), "get_terminal_size": lambda: size})
    with patch.object(helpers, "shutil", fixed_shutil):
      run(None)
    run(None)
    gc.callbacks.append(record_gc)
    try:
      for iteration in range(args.count):
        # Reverse the order every pair so neither variant always runs first.
        for variant in (("real", "fixed") if iteration % 2 == 0 else ("fixed", "real")):
          collections.clear()
          with patch.object(helpers, "shutil", shutil if variant == "real" else fixed_shutil):
            row = {"iteration": iteration, "variant": variant, **run(None)}
          row["gc_events"] = list(collections)
          rows.append(row)
    finally:
      gc.callbacks.remove(record_gc)
  summaries = {}
  for variant in ("real", "fixed"):
    selected = [row for row in rows if row["variant"] == variant]
    failures = [row for row in selected if row["failed"]]
    summaries[variant] = {
      "attempts": len(selected), "failures": len(failures),
      "failures_without_recorded_switches_or_gc": sum(
        not row["tinytqdm_voluntary"] and not row["tinytqdm_involuntary"] and not row["gc_events"] for row in failures),
      "median_tiny_wall_ms": statistics.median(row["tinytqdm_wall"] for row in selected)*1000,
      "maximum_tiny_wall_ms": max(row["tinytqdm_wall"] for row in selected)*1000,
    }
  summary = {"variants": summaries, "terminal_size": list(size), "platform": platform.platform(),
             "python": platform.python_version(),
             "subject_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
             "note": "One warmup per variant, alternating order, same non-TTY file output; "
                     "only tinygrad's terminal-size lookup is replaced. No per-call wrappers. "
                     "GC remains enabled and process-wide switch counters are not thread-specific."}
  with (args.destination / "terminal-ab-attempts.jsonl").open("w") as stream:
    for row in rows:
      stream.write(json.dumps(row) + "\n")
  (args.destination / "terminal-ab-summary.json").write_text(json.dumps(summary, indent=2))
  print(json.dumps(summary, indent=2))
  if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
      stream.write("\n## Terminal lookup control\n```json\n" + json.dumps(summary, indent=2) + "\n```\n")


if __name__ == "__main__":
  main()
