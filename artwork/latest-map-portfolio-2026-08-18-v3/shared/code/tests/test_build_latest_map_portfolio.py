from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_latest_map_portfolio as portfolio  # noqa: E402


def _write_triplet(root: Path, stem: str, *, title: str = "TEST PLATE") -> None:
    (root / f"{stem}.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"/>\n', encoding="utf-8"
    )
    (root / f"{stem}.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    (root / f"{stem}.plot.json").write_text(
        json.dumps(
            {
                "title": title,
                "subtitle": "SUBTITLE",
                "subject_id": stem,
                "format_id": "a5-portrait",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_triplets_require_exact_master_pairing_and_ignore_pen_jobs(
    tmp_path: Path,
) -> None:
    _write_triplet(tmp_path, "one")
    (tmp_path / "one.pen-01-black-0-25.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"/>\n', encoding="utf-8"
    )
    (tmp_path / "series-contact-sheet.png").write_bytes(b"contact")
    cohort = portfolio.Cohort(
        cohort_id="fixture",
        release_root=tmp_path,
        artwork_root=tmp_path,
        expected_count=1,
    )

    assert portfolio._triplets(cohort) == [
        (
            tmp_path / "one.svg",
            tmp_path / "one.png",
            tmp_path / "one.plot.json",
        )
    ]

    (tmp_path / "unpaired.png").write_bytes(b"extra")
    with pytest.raises(portfolio.PortfolioError, match="PNG inventory mismatch"):
        portfolio._triplets(cohort)


def test_copy_artwork_catalog_paths_are_promotable_relative_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_triplet(source, "one", title='A "QUOTED" PLACE')
    cohort = portfolio.Cohort(
        cohort_id="fixture",
        release_root=source,
        artwork_root=source,
        expected_count=1,
    )
    domain = portfolio.Domain(
        directory="03-domain",
        title="Domain",
        selection="Selection",
        status="Review-only",
        caveat="Caveat",
        reproduction="command",
        cohorts=(cohort,),
        contracts=(),
        code=(),
        docs=(),
    )
    staging = tmp_path / "staging"
    domain_root = staging / domain.directory

    records = portfolio._copy_artwork(
        domain, domain_root, cohort, portfolio._triplets(cohort)
    )

    assert records[0]["title"] == 'A "QUOTED" PLACE'
    assert records[0]["source_release"] == "source"
    assert records[0]["source_artifact"] == "one.svg"
    assert records[0]["svg"]["path"] == "03-domain/artwork/one.svg"
    assert not Path(records[0]["svg"]["path"]).is_absolute()
    copied_svg = staging / records[0]["svg"]["path"]
    assert (
        records[0]["svg"]["sha256"]
        == hashlib.sha256(copied_svg.read_bytes()).hexdigest()
    )


def test_checksums_cover_exact_files_but_not_the_checksum_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b.txt").write_text("b\n", encoding="utf-8")

    count, digest = portfolio._write_checksums(tmp_path)

    checksum = tmp_path / "CHECKSUMS.sha256"
    lines = checksum.read_text(encoding="utf-8").splitlines()
    assert count == 2
    assert len(lines) == 2
    assert all("CHECKSUMS.sha256" not in line for line in lines)
    assert digest == hashlib.sha256(checksum.read_bytes()).hexdigest()


def test_format_falls_back_to_binding_svg_page_dimensions(tmp_path: Path) -> None:
    svg = tmp_path / "course.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="297mm" height="210mm" '
        'viewBox="0 0 297 210"/>\n',
        encoding="utf-8",
    )

    assert portfolio._format_from_svg_page(svg) == "a4-landscape"


def test_declared_portfolio_is_seven_domains_and_423_distinct_plates() -> None:
    domains = portfolio._domains()

    assert len(domains) == 7
    assert [domain.directory for domain in domains] == [
        "01-university-cities-uk",
        "02-university-cities-us",
        "03-hiking-maps",
        "04-marathon-courses",
        "05-rowing-races",
        "06-f1-courses",
        "07-golf-courses",
    ]
    assert (
        sum(cohort.expected_count for domain in domains for cohort in domain.cohorts)
        == 423
    )
    marathon = next(
        domain for domain in domains if domain.directory == "04-marathon-courses"
    )
    rowing = next(
        domain for domain in domains if domain.directory == "05-rowing-races"
    )
    assert "Course geometry is included" in marathon.caveat
    assert "verified" in marathon.status.lower()
    assert sum(cohort.expected_count for cohort in rowing.cohorts) == 8
    assert {cohort.artwork_prefix.as_posix() for cohort in rowing.cohorts} == {
        "a5-portrait",
        "a3-portrait",
    }
    assert "pinned-source" in rowing.status.lower()
    cohort_ids = {
        cohort.cohort_id for domain in domains for cohort in domain.cohorts
    }
    assert "place-art-fixtures-v1" not in cohort_ids
    assert "york-best-factual-city-series" not in cohort_ids
