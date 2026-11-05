"""Instrument the original timing test without editing the checked-out subject."""
import argparse
import ast
import contextlib
import gc
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
import threading


def instrument(probe=None):
  if probe is not None:
    from test.null import test_tqdm
    source = probe.read_text()
    tree = ast.parse(source)
    method = tree.body[0]
    assert isinstance(method, ast.FunctionDef)
    assert isinstance(method.body[-1], ast.Assert)
    condition = method.body[-1].test
    mapping = {"tqdm_wall": "wall_elapsed_tqdm", "tqdm_thread_cpu": "cpu_elapsed_tqdm",
               "tinytqdm_wall": "wall_elapsed_tiny", "tinytqdm_thread_cpu": "cpu_elapsed_tiny",
               "tqdm_voluntary": "voluntary_tqdm", "tqdm_involuntary": "involuntary_tqdm",
               "tinytqdm_voluntary": "voluntary_tiny", "tinytqdm_involuntary": "involuntary_tiny",
               "tqdm_wall_start": "diagnostic_start_tqdm", "tqdm_wall_end": "diagnostic_end_tqdm",
               "tinytqdm_wall_start": "diagnostic_start_tiny", "tinytqdm_wall_end": "diagnostic_end_tiny"}
    # Keep the user's measurement statements; replace prints and assertion with returned records.
    method.body = [node for node in method.body[:-1] if not
                   (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and
                    isinstance(node.value.func, ast.Name) and node.value.func.id == "print")]
    # Save the exact existing clock readings; do not add extra clock calls inside the wall intervals.
    statements = []
    for node in method.body:
      if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        target = node.targets[0].id
        if target in ("involuntary_tqdm", "involuntary_tiny"):
          statements.append(node)
          label = "tqdm" if target == "involuntary_tqdm" else "tinytqdm"
          # Existing getrusage snapshots already contain these fields; no extra calls in timed work.
          for field in ("ru_utime", "ru_stime", "ru_minflt", "ru_majflt", "ru_nsignals", "ru_inblock", "ru_oublock"):
            name = f"{label}_{field}"
            mapping[name] = name
            statements.extend(ast.parse(f"{name} = after.{field} - before.{field}").body)
          continue
        if target in ("wall_elapsed_tqdm", "wall_elapsed_tiny"):
          label = "tqdm" if target == "wall_elapsed_tqdm" else "tiny"
          node.value.left = ast.NamedExpr(target=ast.Name(id=f"diagnostic_end_{label}", ctx=ast.Store()), value=node.value.left)
          statements.append(node)
          statements.extend(ast.parse(f"diagnostic_start_{label} = wall_start").body)
          continue
      statements.append(node)
    method.body = statements
    result = ast.Dict(keys=[ast.Constant(key) for key in mapping],
                      values=[ast.Name(id=value, ctx=ast.Load()) for value in mapping.values()])
    result.keys.append(ast.Constant("failed"))
    result.values.append(ast.UnaryOp(op=ast.Not(), operand=condition))
    method.body.append(ast.Return(value=result))
    namespace = dict(vars(test_tqdm))
    exec(compile(ast.fix_missing_locations(tree), "<user context-switch diagnostic>", "exec"), namespace)
    return namespace[method.name], source

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
  parser.add_argument("--probe", type=Path)
  args = parser.parse_args()
  if not 1 <= args.count <= 1000000:
    parser.error("count must be between 1 and 1000000")
  args.destination.mkdir(parents=True, exist_ok=True)
  run, source = instrument(args.probe)
  (args.destination / "subject-test.py").write_text(source)
  rows = []
  gc_events = []
  def record_gc(phase, info):
    gc_events.append((phase, time.perf_counter(), time.thread_time(), threading.get_ident(),
                      info["generation"], info.get("collected", 0), info.get("uncollectable", 0)))
  assert gc.isenabled(), "Garbage collection must remain enabled for this observation"
  gc_thresholds = gc.get_threshold()
  gc.callbacks.append(record_gc)
  try:
    # Same non-TTY file output as previous diagnostics. No diagnostic prints during measurements.
    with tempfile.TemporaryFile(mode="w+") as output, contextlib.redirect_stderr(output):
      for iteration in range(args.count):
        row = {"iteration": iteration, **run(None)}
        row["ratio"] = row["tinytqdm_wall"] / row["tqdm_wall"]
        rows.append(row)
  finally:
    gc.callbacks.remove(record_gc)
  collections = []
  pending = {}
  for phase, wall, cpu, thread, generation, collected, uncollectable in gc_events:
    if phase == "start":
      pending[thread] = (wall, cpu, generation)
    elif thread in pending:
      start, cpu_start, generation = pending.pop(thread)
      collections.append({"start": start, "end": wall, "wall_seconds": wall - start,
                          "thread_cpu_seconds": cpu - cpu_start, "thread_id": thread,
                          "generation": generation, "collected": collected, "uncollectable": uncollectable})
  if args.probe is not None:
    for row in rows:
      for label in ("tqdm", "tinytqdm"):
        overlaps = [index for index, event in enumerate(collections)
                    if event["start"] < row[label + "_wall_end"] and event["end"] > row[label + "_wall_start"]]
        row[label + "_gc_events"] = overlaps
        row[label + "_gc_overlap_seconds"] = sum(
          min(collections[index]["end"], row[label + "_wall_end"]) -
          max(collections[index]["start"], row[label + "_wall_start"]) for index in overlaps)
  (args.destination / "gc-events.json").write_text(json.dumps(collections, indent=2))
  failures = [row for row in rows if row["failed"]]
  with (args.destination / "attempts.jsonl").open("w") as stream:
    for row in rows:
      stream.write(json.dumps(row) + "\n")
  (args.destination / "failures.json").write_text(json.dumps(failures, indent=2))
  report = {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(),
            "cpu_count": os.cpu_count(), "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "packages": {name: version(name) for name in ("numpy", "tqdm")}, "count": args.count,
            "failures": len(failures), "gc_enabled": gc.isenabled(), "gc_thresholds": gc_thresholds,
            "gc_collections": len(collections), "test_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "note": "Original loop bodies and wall timers retained. Thread-CPU timers surround each wall interval, "
                    "so include slightly more timer overhead. "
                    "One process, non-TTY file output, not the full pytest/xdist suite. Timing instrumentation can affect behavior. "
                    "Wall minus thread CPU is a clue, not a root-cause determination. "
                    "Context-switch counters, when present, cover the whole process, not only the test thread. "
                    "GC remains enabled with unchanged thresholds. Callback timestamps approximate collection intervals; "
                    "callbacks and retained records can themselves change allocation and timing behavior. Overlap alone does not prove causation."}
  for label, group in (("passing", [row for row in rows if not row["failed"]]), ("failing", failures)):
    report[label] = {"count": len(group)}
    if group:
      for field in ("tqdm_wall", "tqdm_thread_cpu", "tinytqdm_wall", "tinytqdm_thread_cpu", "ratio"):

        report[label][field + "_median"] = statistics.median(row[field] for row in group)
    if group and "tqdm_voluntary" in group[0]:
      for field in ("tqdm_voluntary", "tqdm_involuntary", "tinytqdm_voluntary", "tinytqdm_involuntary"):
        report[label][field + "_nonzero_attempts"] = sum(row[field] > 0 for row in group)
        report[label][field + "_total"] = sum(row[field] for row in group)
    if group and "tqdm_gc_events" in group[0]:
      for bar in ("tqdm", "tinytqdm"):
        report[label][bar + "_gc_overlap_attempts"] = sum(bool(row[bar + "_gc_events"]) for row in group)
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
  if rows and "tqdm_voluntary" in rows[0]:
    lines += ["", "| Attempts with switches | tqdm voluntary | tqdm involuntary | tiny voluntary | tiny involuntary |",
              "|---|---:|---:|---:|---:|"]
    for label in ("passing", "failing"):
      group = report[label]
      if group["count"]:
        counts = " | ".join(str(group[field + "_nonzero_attempts"]) for field in
                            ("tqdm_voluntary", "tqdm_involuntary", "tinytqdm_voluntary", "tinytqdm_involuntary"))
        lines.append(f"| {label} ({group['count']}) | {counts} |")
  if rows and "tqdm_gc_events" in rows[0]:
    lines += ["", "| Attempts overlapping GC | tqdm | tiny |", "|---|---:|---:|"]
    for label in ("passing", "failing"):
      group = report[label]
      if group["count"]:
        lines.append(f"| {label} ({group['count']}) | {group['tqdm_gc_overlap_attempts']} | {group['tinytqdm_gc_overlap_attempts']} |")
  lines += ["", "Artifacts include every attempt, all failed measurements, environment details, and the original test source."]
  summary = "\n".join(lines) + "\n"
  print(summary)
  (args.destination / "summary.md").write_text(summary)
  if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
      stream.write(summary)


if __name__ == "__main__":
  main()
