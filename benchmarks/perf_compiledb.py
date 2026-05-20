#!/usr/bin/env python3
"""Microbenchmarks for compiledb parser, JSON, and merge hot paths.

The script intentionally uses generated in-memory data so the repository does
not need committed large fixtures. It reports best/median timings and exits
non-zero only for benchmark harness errors; optimization pass/fail is decided by
comparing recorded baseline and final reports under the OMX performance goal.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from compiledb import merge_compdb
from compiledb.parser import parse_build_log

try:  # Optional speedup candidate; benchmarks must also run without it.
    import orjson  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on local environment extras.
    orjson = None


@dataclass(frozen=True)
class Timing:
    name: str
    seconds: float
    detail: str


PROJECT_DIR = os.getcwd()


def simple_compile_lines(entries: int) -> list[str]:
    return [f"gcc -Iinclude -DVALUE={idx} -c src/file{idx}.c -o build/file{idx}.o" for idx in range(entries)]


def noise_lines(entries: int) -> list[str]:
    return [f"checking for generated feature {idx}... yes" for idx in range(entries)]


def mixed_lines(entries: int) -> list[str]:
    lines: list[str] = []
    for idx in range(entries):
        lines.append(f"echo preparing generated unit {idx}")
        lines.append(f"g++ -std=c++17 -Iinclude -c src/file{idx}.cpp -o build/file{idx}.o")
    return lines


def substitution_lines(entries: int) -> list[str]:
    # Keep this case smaller by caller policy: it intentionally forks shells.
    return [f"gcc -DNAME=$(printf value{idx}) -c src/file{idx}.c -o build/file{idx}.o" for idx in range(entries)]


def compdb_entries(entries: int) -> list[dict[str, Any]]:
    return [
        {
            "directory": PROJECT_DIR,
            "file": f"src/file{idx}.c",
            "arguments": ["gcc", "-Iinclude", f"-DVALUE={idx}", "-c", f"src/file{idx}.c", "-o", f"build/file{idx}.o"],
        }
        for idx in range(entries)
    ]


def time_call(name: str, func: Callable[[], Any], *, repeat: int, detail: str) -> Timing:
    samples: list[float] = []
    for _ in range(repeat):
        gc.collect()
        start = time.perf_counter()
        func()
        samples.append(time.perf_counter() - start)
    return Timing(name=name, seconds=statistics.median(samples), detail=f"{detail}; best={min(samples):.6f}s")


def parser_benchmarks(entries: int, repeat: int) -> Iterable[Timing]:
    cases: list[tuple[str, list[str], str]] = [
        ("parser_noise", noise_lines(entries), f"lines={entries}"),
        ("parser_simple", simple_compile_lines(entries), f"lines={entries}"),
        ("parser_mixed", mixed_lines(entries), f"noise_lines={entries} compile_lines={entries}"),
        ("parser_substitution", substitution_lines(max(1, min(entries, 200))), f"lines={max(1, min(entries, 200))}"),
    ]
    for name, lines, detail in cases:
        def run(lines: list[str] = lines) -> tuple[int, int, int]:
            result = parse_build_log(lines, PROJECT_DIR, [])
            return result.count, result.skipped, len(result.compdb)

        yield time_call(name, run, repeat=repeat, detail=detail)


def json_benchmarks(entries: int, repeat: int) -> Iterable[Timing]:
    data = compdb_entries(entries)

    def stdlib_dump() -> int:
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as stream:
            json.dump(data, stream, indent=True)
            stream.write(os.linesep)
            stream.seek(0)
            return len(stream.read())

    def stdlib_load() -> int:
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as stream:
            json.dump(data, stream, indent=True)
            stream.write(os.linesep)
            stream.seek(0)
            return len(json.load(stream))

    yield time_call("json_stdlib_dump", stdlib_dump, repeat=repeat, detail=f"entries={entries} pretty=True")
    yield time_call("json_stdlib_load", stdlib_load, repeat=repeat, detail=f"entries={entries}")

    if orjson is None:
        yield Timing("json_orjson_dump", float("nan"), "orjson=not-installed")
        yield Timing("json_orjson_load", float("nan"), "orjson=not-installed")
        return

    def orjson_dump() -> int:
        payload = orjson.dumps(data, option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE)
        return len(payload)

    orjson_payload = orjson.dumps(data, option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE)

    def orjson_load() -> int:
        return len(orjson.loads(orjson_payload))

    yield time_call("json_orjson_dump", orjson_dump, repeat=repeat, detail=f"entries={entries} pretty=True")
    yield time_call("json_orjson_load", orjson_load, repeat=repeat, detail=f"entries={entries}")


def merge_benchmarks(entries: int, repeat: int) -> Iterable[Timing]:
    existing = compdb_entries(entries)
    new = compdb_entries(entries // 2)

    def merge_no_strict() -> int:
        return len(merge_compdb(existing, new, check_files=False))

    yield time_call("merge_no_strict", merge_no_strict, repeat=repeat, detail=f"existing={entries} new={entries // 2}")

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        strict_existing = []
        strict_new = []
        for idx in range(entries):
            src = root / "src" / f"file{idx}.c"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.touch()
            strict_existing.append({"directory": str(root), "file": f"src/file{idx}.c", "arguments": ["gcc", "-c", f"src/file{idx}.c"]})
        for idx in range(entries // 2):
            strict_new.append({"directory": str(root), "file": f"src/file{idx}.c", "arguments": ["gcc", "-c", f"src/file{idx}.c"]})

        def merge_strict() -> int:
            return len(merge_compdb(strict_existing, strict_new, check_files=True))

        yield time_call("merge_strict", merge_strict, repeat=repeat, detail=f"existing={entries} new={entries // 2}")


def emit(timings: Iterable[Timing], fmt: str) -> None:
    rows = list(timings)
    if fmt == "json":
        print(json.dumps([timing.__dict__ for timing in rows], indent=2, allow_nan=True))
        return
    for timing in rows:
        seconds = "nan" if timing.seconds != timing.seconds else f"{timing.seconds:.6f}s"
        print(f"{timing.name:24s} {seconds:>12s}  {timing.detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["all", "parser", "json", "merge"], default="all")
    parser.add_argument("--entries", type=int, default=10_000, help="Parser and merge synthetic entry count")
    parser.add_argument("--json-entries", type=int, default=100_000, help="JSON synthetic entry count")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    timings: list[Timing] = []
    if args.case in {"all", "parser"}:
        timings.extend(parser_benchmarks(args.entries, args.repeat))
    if args.case in {"all", "json"}:
        timings.extend(json_benchmarks(args.json_entries, args.repeat))
    if args.case in {"all", "merge"}:
        timings.extend(merge_benchmarks(args.entries, args.repeat))
    emit(timings, args.format)


if __name__ == "__main__":
    main()
