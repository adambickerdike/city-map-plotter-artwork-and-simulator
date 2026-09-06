"""Release imports must fail closed after clone or catalog damage."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_production_maps import relative_file  # noqa: E402


@pytest.mark.parametrize("path", ["../outside.svg", "/tmp/outside.svg"])
def test_catalog_paths_cannot_escape_release(tmp_path, path):
    with pytest.raises(ValueError, match="Nonportable"):
        relative_file(tmp_path, path)


def test_lfs_pointer_is_not_accepted_as_artwork(tmp_path):
    (tmp_path / "map.svg").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:123\nsize 9\n")
    with pytest.raises(ValueError, match="Git LFS pointer"):
        relative_file(tmp_path, "map.svg")


def test_symlink_is_not_accepted_as_pinned_artifact(tmp_path):
    (tmp_path / "real.svg").write_text("<svg/>")
    (tmp_path / "map.svg").symlink_to(tmp_path / "real.svg")
    with pytest.raises(ValueError, match="unsafe"):
        relative_file(tmp_path, "map.svg")
