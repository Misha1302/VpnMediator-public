#!/usr/bin/env python3
"""Run a pytest suite in bounded batches with process-group cleanup."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


def fail(message: str) -> None:
    print(f"Bounded pytest gate failed: {message}", file=sys.stderr)
    raise SystemExit(1)


@dataclass(frozen=True)
class BatchResult:
    files: tuple[Path, ...]
    return_code: int
    output: str
    timed_out: bool


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def run_batch(
    interpreter: Path,
    files: tuple[Path, ...],
    timeout_seconds: int,
) -> BatchResult:
    command = [
        str(interpreter),
        "-W",
        "error::DeprecationWarning",
        "-m",
        "pytest",
        "-p",
        "pytest_asyncio.plugin",
        "-q",
        *(str(path) for path in files),
    ]
    environment = dict(os.environ)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as output_file:
        process = subprocess.Popen(
            command,
            stdout=output_file,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
            start_new_session=True,
        )
        timed_out = False
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_group(process)
            return_code = process.returncode if process.returncode is not None else 124

        output_file.seek(0)
        output = output_file.read()

    return BatchResult(
        files=files,
        return_code=return_code,
        output=output,
        timed_out=timed_out,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--test-directory", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--batch-size", type=int, default=5)
    args = parser.parse_args()

    interpreter = Path(args.python).absolute()
    test_directory = Path(args.test_directory).resolve()
    if not interpreter.is_file():
        fail(f"Python interpreter does not exist: {interpreter}")
    if not test_directory.is_dir():
        fail(f"test directory does not exist: {test_directory}")
    if args.timeout_seconds <= 0:
        fail("timeout must be positive")
    if args.batch_size <= 0:
        fail("batch size must be positive")

    test_files = sorted(test_directory.glob("test_*.py"))
    if not test_files:
        fail(f"no pytest files found in {test_directory}")

    batches = [
        tuple(test_files[index : index + args.batch_size])
        for index in range(0, len(test_files), args.batch_size)
    ]
    print(
        f"Running bounded pytest suite for {test_directory} "
        f"({len(test_files)} files in {len(batches)} batches, "
        f"per-batch deadline={args.timeout_seconds}s).",
        flush=True,
    )

    for index, files in enumerate(batches, start=1):
        result = run_batch(interpreter, files, args.timeout_seconds)
        if result.output:
            print(result.output.rstrip())
        if result.timed_out:
            names = ", ".join(path.name for path in files)
            fail(f"batch {index} timed out: {names}")
        if result.return_code != 0:
            names = ", ".join(path.name for path in files)
            fail(f"batch {index} exited with status {result.return_code}: {names}")
        print(f"Completed batch {index}/{len(batches)}.", flush=True)

    print(f"Bounded pytest gate passed for {test_directory}.", flush=True)


if __name__ == "__main__":
    main()
