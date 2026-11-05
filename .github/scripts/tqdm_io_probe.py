"""Measure terminal and output operations; instrumentation changes timing costs."""
import argparse
import contextlib
import fcntl
import gc
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time
from unittest.mock import patch

from tqdm_diagnostic import instrument


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("destination", type=Path)
  parser.add_argument("--count", type=int, default=10000)
  args = parser.parse_args()
  if not 1 <= args.count <= 100000:
    parser.error("count must be between 1 and 100000")
  args.destination.mkdir(parents=True, exist_ok=True)
  run, _ = instrument(Path(__file__).with_name("context_switch_probe.py"))
  events, collections = [], []
  def track(function, label):
    def wrapped(*a, **kw):
      cpu_start = time.thread_time()
      start = time.perf_counter()
      try:
        return function(*a, **kw)
      finally:
        end = time.perf_counter()
        cpu = time.thread_time() - cpu_start
        events.append({"operation": label, "start": start, "end": end, "wall": end-start, "cpu": cpu})
    return wrapped

  def record_gc(phase, info):
    collections.append({"phase": phase, "wall": time.perf_counter(), "generation": info["generation"]})

  class Output:
    def __init__(self, output):
      self.output = output
      self.write = track(output.write, "stderr.write")
      self.flush = track(output.flush, "stderr.flush")
    def __getattr__(self, name):
      return getattr(self.output, name)

  rows, samples = [], []
  highest = 0.0
  gc.callbacks.append(record_gc)
  try:
    with tempfile.TemporaryFile(mode="w+") as output, contextlib.ExitStack() as stack:
      stack.enter_context(contextlib.redirect_stderr(Output(output)))
      for module, name in ((os, "get_terminal_size"), (shutil, "get_terminal_size"), (fcntl, "ioctl")):
        stack.enter_context(patch.object(module, name, track(getattr(module, name), module.__name__ + "." + name)))
      warmup = run(None)
      for iteration in range(args.count):
        events.clear()
        collections.clear()
        row = {"iteration": iteration, **run(None)}
        row["ratio"] = row["tinytqdm_wall"] / row["tqdm_wall"]
        row["gc_events"] = list(collections)
        rows.append(row)
        new_high = row["tinytqdm_thread_cpu"] > highest
        if row["failed"] or iteration % 1000 == 0 or new_high:
          sample = {"measurement": row, "operations": list(events)}
          if row["failed"] or iteration % 1000 == 0:
            samples.append(sample)
          if new_high:
            highest = row["tinytqdm_thread_cpu"]
            (args.destination / "io-highest-cpu.json").write_text(json.dumps(sample, indent=2))
  finally:
    gc.callbacks.remove(record_gc)
  with (args.destination / "io-attempts.jsonl").open("w") as stream:
    for row in rows:
      stream.write(json.dumps(row) + "\n")
  (args.destination / "io-samples.json").write_text(json.dumps(samples, indent=2))
  summary = {"count": args.count, "failures": sum(row["failed"] for row in rows), "warmup": warmup,
             "platform": platform.platform(), "python": platform.python_version(),
             "subject_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
             "note": "Wrapped terminal queries and text-stream write/flush calls, with wall and thread CPU timers. "
                     "One warmup per bar; non-TTY temporary file output. Instrumentation can change behavior."}
  (args.destination / "io-summary.json").write_text(json.dumps(summary, indent=2))
  print(json.dumps(summary, indent=2))
  if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
      stream.write("\n## Terminal/output diagnostic\n```json\n" + json.dumps(summary, indent=2) + "\n```\n")


if __name__ == "__main__":
  main()
