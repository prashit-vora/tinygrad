"""Read-only checks for tracing available on the hosted macOS runner."""
import argparse
import json
from pathlib import Path
import platform
import subprocess


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument("destination", type=Path)
  args = parser.parse_args()
  args.destination.mkdir(parents=True, exist_ok=True)
  commands = [
    ["csrutil", "status"],
    ["sysctl", "kern.hv_vmm_present", "machdep.cpu.brand_string", "hw.ncpu"],
    ["sudo", "-n", "dtrace", "-l", "-n", "syscall::ioctl:entry"],
    ["sudo", "-n", "dtrace", "-l", "-n", "profile-997"],
    ["sudo", "-n", "dtrace", "-n", "BEGIN { trace(1); exit(0); }"],
    ["xcrun", "xctrace", "list", "templates"],
  ]
  results = []
  for command in commands:
    try:
      result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
      row = {"command": command, "returncode": result.returncode,
             "stdout": result.stdout, "stderr": result.stderr}
    except (OSError, subprocess.TimeoutExpired) as error:
      row = {"command": command, "error": str(error)}
    results.append(row)
    print(json.dumps(row), flush=True)
  (args.destination / "trace-capabilities.json").write_text(json.dumps(
    {"platform": platform.platform(), "commands": results}, indent=2))


if __name__ == "__main__":
  main()
