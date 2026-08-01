#!/usr/bin/env python3
"""Supervise bounded TCGdex exporter processes until their checkpoint is complete."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", default="node")
    parser.add_argument("--tsx-cli", required=True, type=Path)
    parser.add_argument("--exporter", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--max-files-per-process", type=int, default=6000)
    parser.add_argument("--max-restarts", type=int, default=20)
    args = parser.parse_args()
    if args.max_files_per_process < 1:
        parser.error("--max-files-per-process must be positive")

    previous_index = -1
    for attempt in range(1, args.max_restarts + 1):
        if args.checkpoint.exists():
            checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
            if checkpoint.get("complete"):
                print(json.dumps({"status": "already_complete", "attempts": attempt - 1, **checkpoint}))
                return 0
            current_index = int(checkpoint.get("next_index", 0))
            if current_index == previous_index:
                raise RuntimeError(f"Exporter made no progress after restart {attempt - 1}")
            previous_index = current_index
        command = [
            args.node, str(args.tsx_cli.resolve()), str(args.exporter.resolve()),
            "--source-root", str(args.source_root.resolve()),
            "--output", str(args.output.resolve()),
            "--checkpoint", str(args.checkpoint.resolve()),
            "--max-files", str(args.max_files_per_process),
        ]
        result = subprocess.run(command, cwd=args.source_root, text=True, encoding="utf-8")
        if result.returncode != 0:
            # EMFILE is recoverable because the exporter only advances the checkpoint
            # after the corresponding JSONL batch has been flushed.
            if not args.checkpoint.exists():
                return result.returncode
            after = json.loads(args.checkpoint.read_text(encoding="utf-8"))
            if int(after.get("next_index", 0)) <= previous_index:
                return result.returncode
            print(json.dumps({"status": "recoverable_process_failure", "attempt": attempt,
                              "returncode": result.returncode, "next_index": after["next_index"]}),
                  file=sys.stderr)
    raise RuntimeError(f"Checkpoint did not complete after {args.max_restarts} supervised processes")


if __name__ == "__main__":
    raise SystemExit(main())
