#!/usr/bin/env python3
"""Write deterministic SHA-256 checksums for this release."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


RELEASE = Path(__file__).resolve().parents[1]
OUTPUT = RELEASE / "CHECKSUMS.sha256"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    paths = sorted(
        path
        for path in RELEASE.rglob("*")
        if (
            path.is_file()
            and path != OUTPUT
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        )
    )
    payload = "".join(
        f"{file_sha256(path)}  {path.relative_to(RELEASE).as_posix()}\n"
        for path in paths
    )
    temporary = OUTPUT.with_suffix(f"{OUTPUT.suffix}.{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, OUTPUT)
    print(f"Wrote {OUTPUT} for {len(paths)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
