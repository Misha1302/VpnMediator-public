#!/usr/bin/env python3
"""Run pytest files in isolated processes with per-file timeouts.

Files may run concurrently, but each file always receives its own interpreter
process and process group. Output is emitted atomically per file so concurrent
workers do not interleave tracebacks.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


def fail(message: str) -> None:
    print(f"Isolated pytest gate failed: {message}", file=sys.stderr)
    raise SystemExit(1)


@dataclass(frozen=True)
class TestFileResult:
    path: Path
    return_code: int
    output: str
    timed_out: bool


def run_test_file(
    interpreter: Path,
    test_file: Path,
    timeout_seconds: int,
) -> TestFileResult:
    pytest_command = [
        str(interpreter),
        "-W",
        "error::DeprecationWarning",
        "-m",
        "pytest",
        "-p",
        "pytest_asyncio.plugin",
        "-q",
        str(test_file),
    ]
    command = [
        "timeout",
        "--signal=TERM",
        "--kill-after=10s",
        f"{timeout_seconds}s",
        *pytest_command,
    ]
    # A pipe can stay open when a test starts a descendant that inherits stdout,
    # making communicate() wait for EOF after pytest itself has exited. A real
    # file plus GNU timeout makes completion and forced termination independent
    # of inherited descriptors and Python subprocess wait edge cases.
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as output_file:
        environment = dict(os.environ)
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        completed = subprocess.run(
            command,
            stdout=output_file,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            env=environment,
        )
        output_file.seek(0)
        output = output_file.read()
    timed_out = completed.returncode in {124, 137}
    return TestFileResult(
        path=test_file,
        return_code=completed.returncode,
        output=output,
        timed_out=timed_out,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--test-directory", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    interpreter = Path(args.python).absolute()
    test_directory = Path(args.test_directory).resolve()
    if not interpreter.is_file():
        fail(f"Python interpreter does not exist: {interpreter}")
    if not test_directory.is_dir():
        fail(f"test directory does not exist: {test_directory}")
    if args.timeout_seconds <= 0:
        fail("timeout must be positive")
    if args.workers <= 0:
        fail("workers must be positive")

    test_files = sorted(test_directory.glob("test_*.py"))
    if not test_files:
        fail(f"no pytest files found in {test_directory}")

    worker_count = min(args.workers, len(test_files))
    print(
        f"Running {len(test_files)} isolated pytest files with {worker_count} worker(s).",
        flush=True,
    )
    results: list[TestFileResult] = []
    if worker_count == 1:
        for test_file in test_files:
            result = run_test_file(interpreter, test_file, args.timeout_seconds)
            results.append(result)
            status = "TIMEOUT" if result.timed_out else str(result.return_code)
            print(f"Completed {result.path.name}: status={status}", flush=True)
    else:
        pending_files = iter(test_files)
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_by_file: dict[concurrent.futures.Future[TestFileResult], Path] = {}
            for _ in range(worker_count):
                test_file = next(pending_files, None)
                if test_file is None:
                    break
                future = executor.submit(
                    run_test_file,
                    interpreter,
                    test_file,
                    args.timeout_seconds,
                )
                future_by_file[future] = test_file

            while future_by_file:
                done, _ = concurrent.futures.wait(
                    future_by_file,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    future_by_file.pop(future)
                    result = future.result()
                    results.append(result)
                    status = "TIMEOUT" if result.timed_out else str(result.return_code)
                    print(f"Completed {result.path.name}: status={status}", flush=True)
                    next_file = next(pending_files, None)
                    if next_file is not None:
                        next_future = executor.submit(
                            run_test_file,
                            interpreter,
                            next_file,
                            args.timeout_seconds,
                        )
                        future_by_file[next_future] = next_file

    failed_results = [
        result
        for result in sorted(results, key=lambda item: item.path.name)
        if result.timed_out or result.return_code != 0
    ]
    if failed_results:
        for result in failed_results:
            print(f"\n===== {result.path} =====", file=sys.stderr)
            if result.output:
                print(result.output.rstrip(), file=sys.stderr)
            if result.timed_out:
                print(
                    f"Exceeded {args.timeout_seconds} seconds and was terminated.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Exited with status {result.return_code}.",
                    file=sys.stderr,
                )
        fail(f"{len(failed_results)} of {len(test_files)} files failed")

    print(
        f"Isolated pytest gate passed for {test_directory} ({len(test_files)} files).",
        flush=True,
    )


if __name__ == "__main__":
    main()
