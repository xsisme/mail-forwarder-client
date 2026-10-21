"""Supervise a source or packaged offline startup check, including its result."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from mailclient import __version__
from mailclient.startup import SMOKE_RESULT, SMOKE_SCHEMA, SMOKE_VISIBLE_MS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    folder = args.data_dir.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    report = folder / SMOKE_RESULT
    report.unlink(missing_ok=True)
    if args.exe:
        command = [str(args.exe.resolve())]
    else:
        command = [sys.executable, "-X", "utf8", str(Path(__file__).with_name("app.py"))]
    command += ["--ui-smoke-test", "--data-dir", str(folder)]
    process = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            output, _ = process.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            # Kill only the child this checker started, including the onefile
            # bootloader's child on Windows. Never stop the user's real client.
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            process.kill()
            process.communicate(timeout=5)
            raise RuntimeError("Startup check timed out after 45 seconds.")
        if output and sys.stdout is not None:
            print(output.decode("utf-8", errors="replace"), end="")
        if process.returncode:
            raise RuntimeError(f"Startup process exited with code {process.returncode}.")
        result = json.loads(report.read_text(encoding="utf-8"))
        samples = result.get("samples", [])
        stable = []
        for sample in reversed(samples):
            if not (sample.get("visible") and sample.get("start_button_visible")
                    and sample.get("state") in ("normal", "zoomed")):
                break
            stable.append(sample)
        if (result.get("passed") is not True or result.get("version") != __version__
                or result.get("schema_version") != SMOKE_SCHEMA
                or result.get("stable_visible_ms", 0) < SMOKE_VISIBLE_MS
                or len(stable) < 3
                or stable[0].get("elapsed_ms", 0) - stable[-1].get("elapsed_ms", 0) < SMOKE_VISIBLE_MS):
            raise RuntimeError("Startup result is missing successful sustained visibility checks.")
        print(f"Startup verified: v{__version__}, window continuously visible for at least two seconds.")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Startup check FAILED: {exc}\nDiagnostic log: {folder / 'startup.log'}", file=sys.stderr)
        try:
            lines = (folder / "startup.log").read_text(encoding="utf-8").splitlines()
            print("\nStartup log (last 40 lines):\n" + "\n".join(lines[-40:]), file=sys.stderr)
        except OSError:
            print("The startup process did not leave a readable diagnostic log.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
