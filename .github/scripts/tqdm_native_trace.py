"""Correlate original timing records with DTrace syscalls and stack samples."""
import argparse
import contextlib
import gc
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time

from tqdm_diagnostic import instrument

DTRACE = r'''
BEGIN { printf("TRACER_READY\n"); }
syscall:::entry /pid == $target/ {
  self->started = timestamp;
  self->cpu_started = vtimestamp;
  self->fd = arg0;
}
syscall::write:entry /pid == $target && arg0 == 1/ {
  printf("MARK %llu %d %s\n", timestamp, tid, copyinstr(arg1));
}
syscall::write:entry /pid == $target && arg0 == 1 && arg2 == 11/ { exit(0); }
syscall:::return /pid == $target && self->started && timestamp - self->started > 200000/ {
  printf("SYSCALL %llu %d %s fd=%d start=%llu wall=%llu cpu=%llu ret=%d errno=%d\n",
         timestamp, tid, probefunc, self->fd, self->started,
         timestamp - self->started, vtimestamp - self->cpu_started, arg0, errno);
  ustack(20);
}
sched:::off-cpu /pid == $target/ {
  self->off_started = timestamp;
  printf("OFFCPU %llu %d\n", timestamp, tid);
  ustack(20);
  stack(25);
}
sched:::on-cpu /pid == $target && self->off_started/ {
  printf("ONCPU %llu %d off_start=%llu off_wall=%llu\n",
         timestamp, tid, self->off_started, timestamp - self->off_started);
  self->off_started = 1;
}
profile-997 /pid == $target/ {
  printf("SAMPLE %llu %d kernelpc=%p userpc=%p\n", timestamp, tid, arg0, arg1);
  ustack(20);
  stack(15);
}
'''


def child(destination, count):
  run, _ = instrument(Path(__file__).with_name("context_switch_probe.py"))
  rows, collections = [], []
  def record_gc(phase, info):
    collections.append({"phase": phase, "wall": time.perf_counter(), "generation": info["generation"]})
  print("CHILD_READY", flush=True)
  sys.stdin.readline()
  print(json.dumps({"calibration_perf_ns": time.perf_counter_ns(), "native_tid": threading.get_native_id()}), flush=True)
  with tempfile.TemporaryFile(mode="w+") as output, contextlib.redirect_stderr(output):
    run(None)
    gc.callbacks.append(record_gc)
    try:
      for iteration in range(count):
        collections.clear()
        rows.append({"iteration": iteration, **run(None), "gc_events": list(collections)})
    finally:
      gc.callbacks.remove(record_gc)
  with (destination / "native-attempts.jsonl").open("w") as stream:
    for row in rows:
      stream.write(json.dumps(row) + "\n")
  (destination / "native-summary.json").write_text(json.dumps({
    "count": count, "failures": sum(row["failed"] for row in rows),
    "note": "DTrace adds overhead. One warmup, same non-TTY file output. "
            "Trace timestamps require alignment with calibration marker; stacks are samples, not complete execution."}, indent=2))
  print("CHILD_DONE", flush=True)
  # Keep mappings alive while DTrace resolves user-stack symbols and drains output.
  sys.stdin.readline()


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("destination", type=Path)
  parser.add_argument("--count", type=int, default=20000)
  parser.add_argument("--child", action="store_true")
  args = parser.parse_args()
  args.destination = args.destination.resolve()
  args.destination.mkdir(parents=True, exist_ok=True)
  if not 1 <= args.count <= 100000:
    parser.error("count must be between 1 and 100000")
  if args.child:
    child(args.destination, args.count)
    return
  script = args.destination / "probe.d"
  script.write_text(DTRACE)
  target = subprocess.Popen([sys.executable, __file__, str(args.destination), "--count", str(args.count), "--child"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
  tracer = None
  try:
    if not select.select([target.stdout], [], [], 30)[0] or target.stdout.readline().strip() != "CHILD_READY":
      raise RuntimeError("child did not reach ready state")
    with (args.destination / "dtrace-stderr.txt").open("w") as errors, (args.destination / "native-trace.txt").open("w") as trace:
      tracer = subprocess.Popen(["sudo", "-n", "dtrace", "-q", "-x", "bufsize=16m", "-x", "dynvarsize=16m",
                                 "-p", str(target.pid), "-s", str(script)],
                                stdout=trace, stderr=errors, text=True)
      deadline = time.monotonic() + 30
      while "TRACER_READY" not in (args.destination / "native-trace.txt").read_text():
        if tracer.poll() is not None or time.monotonic() > deadline:
          raise RuntimeError("DTrace failed to start; inspect dtrace-stderr.txt")
        time.sleep(0.1)
      target.stdin.write("go\n")
      target.stdin.flush()
      transcript = []
      deadline = time.monotonic() + 180
      while time.monotonic() < deadline:
        if select.select([target.stdout], [], [], 1)[0]:
          line = target.stdout.readline()
          if not line:
            raise RuntimeError("child exited before completing experiment")
          transcript.append(line)
          if line.strip() == "CHILD_DONE":
            break
      else:
        raise RuntimeError("trace experiment exceeded 180 seconds")
      (args.destination / "child-transcript.txt").write_text("".join(transcript))
      tracer.wait(timeout=30)
      target.stdin.write("done\n")
      target.stdin.flush()
      target.wait(timeout=30)
      if target.returncode:
        raise RuntimeError(target.stderr.read())
      print((args.destination / "native-summary.json").read_text())
  finally:
    if tracer is not None and tracer.poll() is None:
      subprocess.run(["sudo", "-n", "kill", "-TERM", str(tracer.pid)], check=False)
    if target.poll() is None:
      target.kill()
    (args.destination / "child-stderr.txt").write_text(target.stderr.read())


if __name__ == "__main__":
  main()
