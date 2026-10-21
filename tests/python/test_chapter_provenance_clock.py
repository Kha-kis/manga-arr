"""The foreign-file safety assertion must ignore creation-clock differences only."""

from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

import test_chapter_provenance as scenario


@pytest.fixture
def different_zip_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, bytes]:
    archives: dict[str, bytes] = {}
    seconds = {"src.cbz": 10, "dst.cbz": 12, "ref.cbz": 14}
    real_make_cbz = scenario._make_cbz

    def make_cbz(path: str | Path, page_count: int = 3) -> str:
        timestamp = (2026, 10, 21, 5, 8, seconds[Path(path).name], 0, 0, 0)

        def localtime(_seconds: float) -> tuple[int, ...]:
            return timestamp

        # Isolate ZIP's clock, not the process clock or the shared fixture writer.
        with monkeypatch.context() as clock:
            clock.setattr(
                zipfile,
                "time",
                SimpleNamespace(time=lambda: 0, localtime=localtime),
            )
            result = real_make_cbz(path, page_count=page_count)
        with zipfile.ZipFile(result) as archive:
            assert {entry.date_time for entry in archive.infolist()} == {timestamp[:6]}
        archives[Path(path).name] = Path(result).read_bytes()
        return result

    monkeypatch.setattr(scenario, "_make_cbz", make_cbz)
    return archives


@pytest.mark.parametrize("limit", [10, 4 * 1024 * 1024 * 1024])
def test_regular_file_swap_assertion_is_independent_of_zip_creation_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    different_zip_timestamps: dict[str, bytes],
) -> None:
    scenario.test_copy_never_writes_a_regular_file_swapped_in(
        tmp_path, monkeypatch, limit
    )
    _ = scenario._make_cbz(tmp_path / "ref.cbz", page_count=6)
    assert different_zip_timestamps["dst.cbz"] != different_zip_timestamps["ref.cbz"]
    assert (tmp_path / "dst.cbz").read_bytes() == different_zip_timestamps["dst.cbz"]


@pytest.mark.parametrize("limit", [10, 4 * 1024 * 1024 * 1024])
def test_regular_file_swap_assertion_still_rejects_foreign_byte_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    different_zip_timestamps: dict[str, bytes],
) -> None:
    import chapter_provenance

    real_copy = chapter_provenance.copy_new_file
    mutated: list[Path] = []

    def mutate_after_refusal(src: str, dst: str) -> None:
        try:
            _ = real_copy(src, dst)
        except chapter_provenance.ProvenanceError:
            # A trailing byte preserves ZIP entries but must fail exact-byte safety.
            with open(dst, "ab") as archive:
                _ = archive.write(b"\x00")
            mutated.append(Path(dst))
            raise

    monkeypatch.setattr(chapter_provenance, "copy_new_file", mutate_after_refusal)
    with pytest.raises(AssertionError):
        scenario.test_copy_never_writes_a_regular_file_swapped_in(
            tmp_path, monkeypatch, limit
        )
    assert mutated == [tmp_path / "dst.cbz"]
    assert mutated[0].read_bytes() == different_zip_timestamps["dst.cbz"] + b"\x00"
