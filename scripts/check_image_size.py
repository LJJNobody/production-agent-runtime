#!/usr/bin/env python3
"""Inspect an existing Docker image size and compare it with an explicit target."""

import argparse
import json
import subprocess


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--target-mb", type=float, default=150.0)
    args = parser.parse_args()
    completed = subprocess.run(
        ["docker", "image", "inspect", args.image, "--format", "{{.Size}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    size_bytes = int(completed.stdout.strip())
    size_mb = size_bytes / 1_000_000
    result = {
        "image": args.image,
        "size_bytes": size_bytes,
        "size_mb_decimal": size_mb,
        "target_mb_decimal": args.target_mb,
        "meets_target": size_mb <= args.target_mb,
    }
    print(json.dumps(result, indent=2))
    return 0 if result["meets_target"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
