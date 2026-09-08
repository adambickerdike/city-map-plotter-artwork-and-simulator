"""The new stadium export preserves the exact selected edition and native curves."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_repository import (  # noqa: E402
    UK_STADIUM_RELEASE,
    _html_map_provider_copy,
    _production_html_pages,
    _visible_map_provider_copy,
)
from verify_uk_stadiums import checked_path, verify  # noqa: E402


def test_latest_stadium_package() -> None:
    report = verify()
    assert report["stadiums"] == 44
    assert report["source_artwork_files"] == 396
    assert report["etihad_version"] == 10
    assert report["physical_execution_allowed"] is False


def test_public_stadium_artwork_gate() -> None:
    svgs = list(UK_STADIUM_RELEASE.rglob("*.svg"))
    assert len(svgs) == 88
    assert all(not _visible_map_provider_copy(path) for path in svgs)
    pages = list(UK_STADIUM_RELEASE.rglob("index.html"))
    assert len(pages) == 3
    assert set(pages).issubset(set(_production_html_pages()))
    assert all(not _html_map_provider_copy(path) for path in pages)


def test_package_paths_cannot_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsafe package path"):
        checked_path(tmp_path, "../outside.json")


def test_lfs_pointer_is_not_an_artwork(tmp_path: Path) -> None:
    (tmp_path / "test.svg").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:test\nsize 10\n"
    )
    with pytest.raises(ValueError, match="Unresolved LFS pointer"):
        checked_path(tmp_path, "test.svg")
