"""Locate CPU work during timing spikes; profiled timings are not baseline timings."""
import argparse
import cProfile
import gc
import json
import os
from pathlib import Path
import pstats
import statistics
import subprocess
import sys
import tempfile
import time

from tqdm_diagnostic import instrument


def stats_rows(profiler):
  rows = []
  for (filename, line, name), (primitive, calls, own, cumulative, callers) in pstats.Stats(profiler).stats.items():
    rows.append({"file": filename, "line": line, "name": name, "calls": calls, "self_cpu_s": own,
                 "cumulative_cpu_s": cumulative,
                 "callers": [{"file": key[0], "line": key[1], "name": key[2], "stats": value} for key, value in callers.items()]})
  return sorted(rows, key=lambda row: row["self_cpu_s"], reverse=True)


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("destination", type=Path)
  parser.add_argument("--count", type=int, default=10000)
  args = parser.parse_args()
  if not 1 <= args.count <= 100000:
    parser.error("count must be between 1 and 100000")
  args.destination.mkdir(parents=True, exist_ok=True)
  run, source = instrument(Path(__file__).with_name("context_switch_probe.py"))
  rows, samples, gc_events = [], [], []
  def gc_callback(phase, info):
    gc_events.append({"phase": phase, "wall": time.perf_counter(), "generation": info["generation"]})
  gc.callbacks.append(gc_callback)
  old_stderr = sys.stderr
  try:
    with tempfile.TemporaryFile(mode="w+") as output:
      sys.stderr = output
      # One unmeasured call per bar before collecting profiled observations.
      warmup = run(None)
      highest = 0.0
      for iteration in range(args.count):
        begin_gc = len(gc_events)
        profiler = cProfile.Profile(time.thread_time)
        profiler.enable()
        row = {"iteration": iteration, **run(None)}
        profiler.disable()
        row["gc_events"] = gc_events[begin_gc:]
        rows.append(row)
        new_high = row["tinytqdm_thread_cpu"] > highest
        if row["failed"] or iteration % 1000 == 0 or new_high:
          sample = {"measurement": row, "functions": stats_rows(profiler)}
          if row["failed"] or iteration % 1000 == 0:
            samples.append(sample)
          if new_high:
            highest = row["tinytqdm_thread_cpu"]
            (args.destination / "highest-cpu-profile.json").write_text(json.dumps(sample, indent=2))
            profiler.dump_stats(str(args.destination / "highest-cpu.prof"))
  finally:
    sys.stderr = old_stderr
    gc.callbacks.remove(gc_callback)
  failures = [row for row in rows if row["failed"]]
  with (args.destination / "profile-attempts.jsonl").open("w") as stream:
    for row in rows:
      stream.write(json.dumps(row) + "\n")
  (args.destination / "profile-samples.json").write_text(json.dumps(samples, indent=2))
  (args.destination / "profile-failures.json").write_text(json.dumps(failures, indent=2))
  summary = {"count": args.count, "failures": len(failures), "python": sys.version,
             "subject_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
             "warmup": warmup, "note": "cProfile enabled with thread-CPU timer; it changes execution cost. "
             "Use profiles to locate work, not estimate baseline flake rate.",
             "tqdm_cpu_median_s": statistics.median(row["tqdm_thread_cpu"] for row in rows),
             "tiny_cpu_median_s": statistics.median(row["tinytqdm_thread_cpu"] for row in rows),
             "tiny_cpu_max_s": max(row["tinytqdm_thread_cpu"] for row in rows)}
  (args.destination / "profile-summary.json").write_text(json.dumps(summary, indent=2))
  print(json.dumps(summary, indent=2))
  if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
      stream.write("\n## CPU profiling diagnostic\n```json\n" + json.dumps(summary, indent=2) + "\n```\n")


if __name__ == "__main__":
  main()
