"""Instrument the original timing test without editing the checked-out subject."""
import argparse
import ast
import contextlib
import hashlib
from importlib.metadata import version
import inspect
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import tempfile
import textwrap
import time


def instrument():
  from test.null import test_tqdm
  source = textwrap.dedent(inspect.getsource(test_tqdm.TestProgressBar.test_tqdm_perf))
  tree = ast.parse(source)
  method = tree.body[0]
  assert isinstance(method, ast.FunctionDef)
  assert len([node for node in method.body if isinstance(node, ast.For)]) == 2, "Select a commit with the original single-sample test"
  assert isinstance(method.body[-1], ast.Assert), "Expected the original timing assertion"
  assert ast.unparse(method.body[-1].test) == "tinytqdm_time < 5 * tqdm_time", "Unexpected assertion"
  statements = []
  for node in method.body:
    if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "st" for target in node.targets):
      # CPU interval surrounds the existing wall interval; no additional calls inside the wall interval.
      statements.extend(ast.parse("thread_start = diagnostic_thread_time()").body)
    statements.append(node)
    if isinstance(node, ast.Assign):
      for target in node.targets:
        if isinstance(target, ast.Name) and target.id in ("tqdm_time", "tinytqdm_time"):
          statements.extend(ast.parse(f"{target.id}_thread = diagnostic_thread_time() - thread_start").body)
  assertion = statements.pop()
  statements.extend(ast.parse("return {'tqdm_wall': tqdm_time, 'tqdm_thread_cpu': tqdm_time_thread, "
                              "'tinytqdm_wall': tinytqdm_time, 'tinytqdm_thread_cpu': tinytqdm_time_thread}").body)
  # Evaluate the same assertion condition, recording failures instead of stopping the experiment.
  statements.insert(-1, ast.Assign(targets=[ast.Name(id="diagnostic_failed", ctx=ast.Store())],
                                  value=ast.UnaryOp(op=ast.Not(), operand=assertion.test)))
  statements[-1].value.keys.append(ast.Constant("failed"))
  statements[-1].value.values.append(ast.Name(id="diagnostic_failed", ctx=ast.Load()))
  method.body = statements
  method.decorator_list = []
  namespace = dict(vars(test_tqdm), diagnostic_thread_time=time.thread_time)
  exec(compile(ast.fix_missing_locations(tree), "<instrumented original test>", "exec"), namespace)
  return namespace[method.name], source


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("destination", type=Path)
  parser.add_argument("--count", type=int, default=10000)
  args = parser.parse_args()
  if not 1 <= args.count <= 1000000:
    parser.error("count must be between 1 and 1000000")
  args.destination.mkdir(parents=True, exist_ok=True)
  run, source = instrument()
  (args.destination / "subject-test.py").write_text(source)
  rows = []
  # Same non-TTY file output as previous diagnostics. No diagnostic prints during measurements.
  with tempfile.TemporaryFile(mode="w+") as output, contextlib.redirect_stderr(output):
    for iteration in range(args.count):
      row = {"iteration": iteration, **run(None)}
      row["ratio"] = row["tinytqdm_wall"] / row["tqdm_wall"]
      rows.append(row)
  failures = [row for row in rows if row["failed"]]
  with (args.destination / "attempts.jsonl").open("w") as stream:
    for row in rows:
      stream.write(json.dumps(row) + "\n")
  (args.destination / "failures.json").write_text(json.dumps(failures, indent=2))
  report = {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
            "cpu_count": os.cpu_count(), "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "packages": {name: version(name) for name in ("numpy", "tqdm")}, "count": args.count,
            "failures": len(failures), "test_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "note": "Original loop bodies and wall timers retained. Thread-CPU timers surround each wall interval, "
                    "so include slightly more timer overhead. "
                    "One process, non-TTY file output, not the full pytest/xdist suite. Timing instrumentation can affect behavior. "
                    "Wall minus thread CPU is a clue, not a root-cause determination."}
  for label, group in (("passing", [row for row in rows if not row["failed"]]), ("failing", failures)):
    report[label] = {"count": len(group)}
    if group:
      for field in ("tqdm_wall", "tqdm_thread_cpu", "tinytqdm_wall", "tinytqdm_thread_cpu", "ratio"):
        report[label][field + "_median"] = statistics.median(row[field] for row in group)
  (args.destination / "metadata.json").write_text(json.dumps(report, indent=2))
  lines = ["# Original tqdm timing diagnostic", "", f"Subject: `{report['commit']}`", "",
           f"**{len(failures)} failures / {args.count} attempts**", "", report["note"], "",
           "| Attempts | tqdm wall median | tqdm thread CPU median | tiny wall median | tiny thread CPU median |",
           "|---|---:|---:|---:|---:|"]
  for label in ("passing", "failing"):
    group = report[label]
    if group["count"]:
      values = " | ".join(f"{group[field + '_median'] * 1000:.6f} ms" for field in
                          ("tqdm_wall", "tqdm_thread_cpu", "tinytqdm_wall", "tinytqdm_thread_cpu"))
      lines.append(f"| {label} ({group['count']}) | {values} |")
  lines += ["", "Artifacts include every attempt, all failed measurements, environment details, and the original test source."]
  summary = "\n".join(lines) + "\n"
  print(summary)
  (args.destination / "summary.md").write_text(summary)
  if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
      stream.write(summary)


if __name__ == "__main__":
  main()
