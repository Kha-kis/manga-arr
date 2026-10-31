"""Real queue discovery conserves independent ordinary and image-only payloads."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import subprocess
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Literal

import pytest

from file_mutation_lock import file_mutation_guard
from test_import_pack_cleanup_durability import _PackEnv, pack_env as pack_env
from test_import_pack_private_placement import _provision
from test_import_pack_protocol import _fallback


@pytest.fixture(params=[False, True], ids=["native", "private"])
def mixed_env(
    pack_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[_PackEnv]:
    _provision(pack_env)
    if request.param:
        _fallback(monkeypatch)
    with sqlite3.connect(pack_env["db_path"]) as db:
        db.execute("UPDATE series SET monitored=1,total_volumes=3 WHERE id=1")
        db.execute(
            "INSERT INTO volumes(series_id,volume_num,status,monitored)"
            " VALUES(1,3,'wanted',1)"
        )
        db.execute(
            "INSERT INTO chapters(series_id,chapter_num,status,monitored)"
            " VALUES(1,2,'wanted',1)"
        )
    yield pack_env


def _archive(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("001.jpg", b"ordinary volume-three page")
    return path


def _images(source: Path) -> Path:
    leaf = source / "Pack Series c002"
    leaf.mkdir(parents=True)
    (leaf / "001.jpg").write_bytes(b"image chapter-two page one")
    (leaf / "002.png").write_bytes(b"image chapter-two page two")
    return leaf


def _hashes(source: Path) -> dict[str, str]:
    return {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source.rglob("*"))
        if path.is_file()
    }


def _queue(
    env: _PackEnv,
    source: Path,
    *,
    download_id: str = "mixed-502",
) -> tuple[int | None, list[sqlite3.Row]]:
    import import_queue
    import main

    with main.get_db() as db:
        queue_id, review = import_queue._queue_import(
            db,
            1,
            download_id,
            "Pack Series Complete Pack",
            f"magnet:{download_id}",
            None,
            str(source),
            respect_grab_claims=False,
        )
    assert not review
    with sqlite3.connect(env["db_path"]) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM import_queue_files WHERE queue_id=? ORDER BY id",
            (queue_id,),
        ).fetchall()
    return queue_id, rows


def _conserved(env: _PackEnv, source: Path, before: dict[str, str]) -> None:
    assert _hashes(source) == before
    print("source_sha256=" + json.dumps(before, sort_keys=True))
    with sqlite3.connect(env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM volumes WHERE status='downloaded'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM chapters WHERE status='downloaded'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM history WHERE event_type='imported'"
        ).fetchone() == (0,)


@pytest.mark.parametrize("payload", ["archive", "images", "same-leaf", "mixed"])
def test_actual_builder_representative_payloads(
    mixed_env: _PackEnv,
    payload: str,
) -> None:
    source = mixed_env["tmp_path"] / "completed"
    source.mkdir()
    ordinary = None
    leaf = None
    if payload != "images":
        ordinary = _archive(source / "Pack Series v003.cbz")
    if payload in ("images", "mixed"):
        leaf = _images(source)
    elif payload == "same-leaf":
        (source / "001.jpg").write_bytes(b"already archived page")
    before = _hashes(source)

    queue_id, rows = _queue(mixed_env, source)

    _conserved(mixed_env, source, before)
    assert queue_id is not None
    assert len({row["src_path"] for row in rows}) == len(rows)
    assert {
        (row["file_type"], row["proposed_volume"], row["proposed_chapter"])
        for row in rows
    } == (
        ({("volume", 3.0, None)} if ordinary is not None else set())
        | ({("chapter", None, 2.0)} if leaf is not None else set())
    )
    assert all(row["status"] == "pending" for row in rows)
    assert all(row["proposed_pack_type"] == "complete" for row in rows)
    if ordinary is not None:
        assert [row["src_path"] for row in rows].count(str(ordinary)) == 1
    if leaf is not None:
        generated = [
            Path(row["src_path"]) for row in rows if row["file_type"] == "chapter"
        ]
        assert len(generated) == 1 and generated[0].is_relative_to(
            mixed_env["pack_root"]
        )
        with zipfile.ZipFile(generated[0]) as archive:
            assert {name: archive.read(name) for name in archive.namelist()} == {
                path.name: path.read_bytes() for path in leaf.iterdir()
            }


def test_nested_ordinary_candidates_keep_same_basename_and_deterministic_order(
    mixed_env: _PackEnv,
) -> None:
    source = mixed_env["tmp_path"] / "nested"
    _images(source)
    ordinary = [
        _archive(source / parent / "Pack Series v003.cbz") for parent in ("z", "a")
    ]
    before = _hashes(source)
    queue_id, rows = _queue(mixed_env, source)
    _conserved(mixed_env, source, before)
    assert queue_id is not None and len(rows) == 3
    assert [row["src_path"] for row in rows if row["file_type"] == "volume"] == [
        str(path) for path in reversed(ordinary)
    ]
    assert len({row["src_path"] for row in rows}) == 3


def _wrappers(source: Path) -> set[str]:
    paths = set()
    for name, member in (("part-a.zip", "scene.rar"), ("part-b.zip", "scene.r00")):
        path = source / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, b"opaque scene part")
        paths.add(str(path))
    return paths


def test_mixed_images_exclude_recognized_wrappers_but_keep_ordinary_zip(
    mixed_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import import_queue

    source = mixed_env["tmp_path"] / "wrapped-mixed"
    _images(source)
    wrappers = _wrappers(source)
    ordinary = _archive(source / "Pack Series v003.zip")
    before = _hashes(source)

    def forbidden_extraction(*args: object) -> None:
        pytest.fail("successful image packing must not newly extract mixed wrappers")

    monkeypatch.setattr(import_queue, "_run_pack_extractor", forbidden_extraction)
    queue_id, rows = _queue(mixed_env, source)
    _conserved(mixed_env, source, before)
    assert queue_id is not None and len(rows) == 2
    assert str(ordinary) in {row["src_path"] for row in rows}
    assert not wrappers.intersection(row["src_path"] for row in rows)


@pytest.mark.parametrize("entry", ["direct", "resolved-alias"])
def test_f1_starting_in_older_claim_tree_does_not_admit_its_private_archive(
    mixed_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    entry: Literal["direct", "resolved-alias"],
) -> None:
    import import_pipeline
    import import_queue
    import main
    from private_pack_source import _freeze_pack_file_origin

    older_env: _PackEnv = {
        **mixed_env,
        "pack_root": mixed_env["tmp_path"] / "older-cache",
    }
    wrapped = mixed_env["tmp_path"] / "older-wrapped-input"
    wrapped.mkdir()
    _wrappers(wrapped)
    wrapped_before = _hashes(wrapped)

    def extractor(
        command: list[str],
        heartbeat: object,
        output_fd: int,
    ) -> subprocess.CompletedProcess[str]:
        output = Path(f"/proc/self/fd/{output_fd}")
        _archive(output / "Pack Series v003.cbz")
        _images(output)
        return subprocess.CompletedProcess(command, 0, "", "")

    # The older mixed tree is real wrapper output in an attached private carrier.
    # Only the external extractor is doubled, not inventory or source authority.
    with monkeypatch.context() as older:
        older.setattr(import_pipeline, "PACK_STAGING_ROOT", str(older_env["pack_root"]))
        _provision(older_env)
        _fallback(older)
        older.setattr(import_queue, "_run_pack_extractor", extractor)
        older_id, older_rows = _queue(
            older_env, wrapped, download_id="f1-older-private"
        )
        assert older_id is not None and len(older_rows) == 1
        with file_mutation_guard(mixed_env["db_path"]) as guard, main.get_db() as db:
            older_queue = dict(
                db.execute(
                    "SELECT * FROM import_queue WHERE id=?", (older_id,)
                ).fetchone()
            )
            older_origin = _freeze_pack_file_origin(
                guard,
                older_queue,
                older_rows[0]["id"],
                older_rows[0]["src_path"],
            )
            assert older_origin["kind"] == "pack"
    older_cbz = Path(older_rows[0]["src_path"])
    assert ".mangarr-claims" in older_cbz.parts
    assert not older_cbz.is_relative_to(mixed_env["pack_root"])
    starting = older_cbz.parent
    if entry == "resolved-alias":
        starting = mixed_env["tmp_path"] / "older-private-alias"
        starting.symlink_to(older_cbz.parent, target_is_directory=True)
        assert ".mangarr-claims" not in starting.parts
        assert starting.resolve() == older_cbz.parent
    before = _hashes(starting)
    with main.get_db() as db:
        reservation = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM import_pack_cleanup_reservations WHERE queue_id=?",
                (older_id,),
            )
        ]
        namespace = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM file_claim_namespaces WHERE parent_path=?",
                (str(older_env["pack_root"]),),
            )
        ]

    try:
        queue_id, rows = _queue(mixed_env, starting, download_id="f1-private-start")
        assert queue_id is not None and queue_id != older_id
        with file_mutation_guard(mixed_env["db_path"]) as guard, main.get_db() as db:
            queue = dict(
                db.execute(
                    "SELECT * FROM import_queue WHERE id=?", (queue_id,)
                ).fetchone()
            )
            origins = [
                _freeze_pack_file_origin(guard, queue, row["id"], row["src_path"])
                for row in rows
            ]
        print(
            "f1_frozen_sources="
            + json.dumps(
                [
                    {"src_path": row["src_path"], "kind": origin["kind"]}
                    for row, origin in zip(rows, origins, strict=True)
                ],
                sort_keys=True,
            )
        )
        assert older_cbz not in {Path(row["src_path"]).resolve() for row in rows}
        assert len(rows) == 1 and origins[0]["kind"] == "pack"
        with zipfile.ZipFile(rows[0]["src_path"]) as archive:
            assert (
                archive.read("001.jpg")
                == (starting / "Pack Series c002" / "001.jpg").read_bytes()
            )
    finally:
        _conserved(mixed_env, starting, before)
        assert _hashes(wrapped) == wrapped_before
        with main.get_db() as db:
            assert [
                tuple(row)
                for row in db.execute(
                    "SELECT * FROM import_pack_cleanup_reservations WHERE queue_id=?",
                    (older_id,),
                )
            ] == reservation
            assert [
                tuple(row)
                for row in db.execute(
                    "SELECT * FROM file_claim_namespaces WHERE parent_path=?",
                    (str(older_env["pack_root"]),),
                )
            ] == namespace
        print("f1_older_source_namespace_and_reservation_conserved=true")


@pytest.mark.parametrize("wrapped", [False, True], ids=["ordinary", "wrapped"])
def test_f2_zip_named_directory_does_not_abort_mixed_file_discovery(
    mixed_env: _PackEnv,
    wrapped: bool,
) -> None:
    import main
    from private_pack_source import _freeze_pack_file_origin

    source = mixed_env["tmp_path"] / "zip-directory"
    leaf = _images(source)
    ordinary = _archive(source / "Pack Series v003.zip")
    (source / "extras.zip").mkdir()
    wrappers = _wrappers(source) if wrapped else set()
    before = _hashes(source)
    try:
        queue_id, rows = _queue(mixed_env, source, download_id="f2-zip-directory")
        assert queue_id is not None and len(rows) == 2
        assert [row["src_path"] for row in rows].count(str(ordinary)) == 1
        assert not wrappers.intersection(row["src_path"] for row in rows)
        with file_mutation_guard(mixed_env["db_path"]) as guard, main.get_db() as db:
            queue = dict(
                db.execute(
                    "SELECT * FROM import_queue WHERE id=?", (queue_id,)
                ).fetchone()
            )
            origins = [
                _freeze_pack_file_origin(guard, queue, row["id"], row["src_path"])
                for row in rows
            ]
        assert {origin["kind"] for origin in origins} == {"pack", "nonpack"}
        generated = next(
            row["src_path"] for row in rows if row["file_type"] == "chapter"
        )
        with zipfile.ZipFile(generated) as archive:
            assert archive.read("001.jpg") == (leaf / "001.jpg").read_bytes()
    finally:
        _conserved(mixed_env, source, before)
        assert (source / "extras.zip").is_dir()
        with sqlite3.connect(mixed_env["db_path"]) as db:
            print(
                "f2_queue_count="
                + str(db.execute("SELECT COUNT(*) FROM import_queue").fetchone()[0])
            )


@pytest.mark.parametrize("packing", ["failure", "empty"])
def test_unsuccessful_image_packing_keeps_existing_ordinary_scan(
    mixed_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    packing: str,
) -> None:
    source = mixed_env["tmp_path"] / "pack-failure"
    leaf = _images(source)
    ordinary = _archive(source / "Pack Series v003.cbz")
    if packing == "failure":
        actual_open = zipfile.ZipFile.open

        def fail_write(
            self: zipfile.ZipFile,
            name: str | zipfile.ZipInfo,
            mode: Literal["r", "w"] = "r",
            pwd: bytes | None = None,
            *,
            force_zip64: bool = False,
        ) -> IO[bytes]:
            if mode == "w":
                raise OSError("injected generated ZIP write failure")
            return actual_open(self, name, mode, pwd, force_zip64=force_zip64)

        monkeypatch.setattr(zipfile.ZipFile, "open", fail_write)
    else:
        for page in leaf.iterdir():
            page.unlink()
        # The existing leaf classifier permits extensionless entries; the real
        # packer correctly returns None when there are no actual image pages.
        (leaf / "extensionless").write_bytes(b"not a page")
    before = _hashes(source)
    queue_id, rows = _queue(mixed_env, source)
    _conserved(mixed_env, source, before)
    assert queue_id is not None
    assert [row["src_path"] for row in rows] == [str(ordinary)]


@pytest.mark.parametrize("extraction", ["success", "empty", "failure"])
def test_wrapped_only_extraction_policy_is_unchanged(
    mixed_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    extraction: str,
) -> None:
    import import_queue

    source = mixed_env["tmp_path"] / "wrapped-only"
    source.mkdir()
    _wrappers(source)
    before = _hashes(source)

    def extract(
        command: list[str], heartbeat: object, output_fd: int
    ) -> subprocess.CompletedProcess[str]:
        if extraction == "success":
            _archive(Path(f"/proc/self/fd/{output_fd}") / "Pack Series v003.cbz")
        return subprocess.CompletedProcess(
            command, int(extraction == "failure"), "", ""
        )

    monkeypatch.setattr(import_queue, "_run_pack_extractor", extract)
    queue_id, rows = _queue(mixed_env, source)
    _conserved(mixed_env, source, before)
    if extraction == "success":
        assert queue_id is not None and len(rows) == 1
        assert Path(rows[0]["src_path"]).is_relative_to(mixed_env["pack_root"])
        assert rows[0]["proposed_volume"] == 3
    else:
        assert queue_id is None and rows == []


def test_real_freezer_distinguishes_discovered_pack_and_nonpack_sources(
    mixed_env: _PackEnv,
) -> None:
    import main
    from private_pack_source import _freeze_pack_file_origin

    source = mixed_env["tmp_path"] / "freeze-mixed"
    _images(source)
    ordinary = _archive(source / "Pack Series v003.cbz")
    before = _hashes(source)
    queue_id, rows = _queue(mixed_env, source)
    _conserved(mixed_env, source, before)
    assert queue_id is not None and len(rows) == 2
    with file_mutation_guard(mixed_env["db_path"]) as guard, main.get_db() as db:
        queue = dict(
            db.execute("SELECT * FROM import_queue WHERE id=?", (queue_id,)).fetchone()
        )
        origins = [
            _freeze_pack_file_origin(guard, queue, row["id"], row["src_path"])
            for row in rows
        ]
    assert {origin["kind"] for origin in origins} == {"pack", "nonpack"}
    assert all(origin["queue_id"] == queue_id for origin in origins)
    assert next(origin for origin in origins if origin["kind"] == "nonpack")[
        "src_path"
    ] == str(ordinary)


@pytest.mark.parametrize("other_cache", ["same-cache", "incoming-root"])
def test_nested_staging_does_not_requeue_current_or_other_private_generated_tree(
    mixed_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
    other_cache: str,
) -> None:
    import import_pipeline
    import main
    from private_pack_source import _freeze_pack_file_origin

    source = mixed_env["tmp_path"] / "nested-cache-input"
    leaf = _images(source)
    ordinary = _archive(source / "Pack Series v003.cbz")
    nested_env: _PackEnv = {**mixed_env, "pack_root": source / "cache"}
    monkeypatch.setattr(
        import_pipeline, "PACK_STAGING_ROOT", str(nested_env["pack_root"])
    )
    _provision(nested_env)
    other = mixed_env["tmp_path"] / "other-input"
    _images(other)
    with monkeypatch.context() as private:
        _fallback(private)
        if other_cache == "incoming-root":
            import private_file_claim as claims

            # A real older private queue can occupy the incoming directory's
            # claim namespace even when the current cache is a nested child.
            private.setattr(import_pipeline, "PACK_STAGING_ROOT", str(source))
            with file_mutation_guard(mixed_env["db_path"]) as guard:
                with claims.ensure_namespace(guard, str(source)):
                    pass
        other_id, other_rows = _queue(nested_env, other, download_id="other-502")
    assert other_id is not None and len(other_rows) == 1
    other_generated = Path(other_rows[0]["src_path"])
    assert ".mangarr-claims" in other_generated.parts
    other_bytes = other_generated.read_bytes()
    with main.get_db() as db:
        other_reservation = [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM import_pack_cleanup_reservations WHERE queue_id=?",
                (other_id,),
            )
        ]
    original_files = {
        ordinary: ordinary.read_bytes(),
        **{path: path.read_bytes() for path in leaf.iterdir()},
    }

    queue_id, rows = _queue(nested_env, source)

    assert all(path.read_bytes() == data for path, data in original_files.items())
    assert other_generated.read_bytes() == other_bytes
    assert queue_id is not None and queue_id != other_id
    assert len(rows) == 2 and len({row["src_path"] for row in rows}) == 2
    assert [row["src_path"] for row in rows].count(str(ordinary)) == 1
    assert other_generated not in {Path(row["src_path"]) for row in rows}
    with file_mutation_guard(mixed_env["db_path"]) as guard, main.get_db() as db:
        queue = dict(
            db.execute("SELECT * FROM import_queue WHERE id=?", (queue_id,)).fetchone()
        )
        origins = [
            _freeze_pack_file_origin(guard, queue, row["id"], row["src_path"])
            for row in rows
        ]
        assert {origin["kind"] for origin in origins} == {"pack", "nonpack"}
        assert [
            tuple(row)
            for row in db.execute(
                "SELECT * FROM import_pack_cleanup_reservations WHERE queue_id=?",
                (other_id,),
            )
        ] == other_reservation
    with zipfile.ZipFile(
        next(row["src_path"] for row in rows if row["file_type"] == "chapter")
    ) as archive:
        assert archive.read("001.jpg") == (leaf / "001.jpg").read_bytes()
    with sqlite3.connect(mixed_env["db_path"]) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM volumes WHERE status='downloaded'"
        ).fetchone() == (0,)
        assert db.execute(
            "SELECT COUNT(*) FROM chapters WHERE status='downloaded'"
        ).fetchone() == (0,)


def test_real_automatic_grab_evidence_is_preserved_for_mixed_discovery(
    mixed_env: _PackEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import grab_core
    import import_queue
    import main
    from clients import GrabResult
    from unittest.mock import AsyncMock

    source = mixed_env["tmp_path"] / "automatic-mixed"
    _images(source)
    ordinary = _archive(source / "Pack Series v003.cbz")
    before = _hashes(source)
    monkeypatch.setattr(
        grab_core,
        "grab_url",
        AsyncMock(
            return_value=GrabResult(
                True,
                "qbittorrent",
                "mixed-502",
                True,
                7,
            )
        ),
    )
    monkeypatch.setattr(grab_core, "notify_discord", AsyncMock())
    monkeypatch.setattr(grab_core, "broadcast_queue_event", AsyncMock())
    item = {
        "title": "Pack Series Complete Pack",
        "url": "https://indexer.test/mixed-502",
        "protocol": "torrent",
    }
    assert asyncio.run(grab_core.grab_item(item, 1))
    with main.get_db() as db:
        seen = dict(
            db.execute(
                "SELECT * FROM seen WHERE torrent_url=?", (item["url"],)
            ).fetchone()
        )
        grabbed = [dict(row) for row in db.execute("SELECT * FROM volumes ORDER BY id")]
        assert seen["respect_grab_claims"] == 1
        assert any(
            row["status"] == "grabbed" and row["volume_num"] == 3 for row in grabbed
        )
        queue_id, _ = import_queue._queue_import(
            db,
            1,
            "mixed-502",
            item["title"],
            item["url"],
            None,
            str(source),
            download_client_id=7,
            protocol="torrent",
        )
        assert queue_id is not None
        assert tuple(
            db.execute(
                "SELECT respect_grab_claims FROM import_queue WHERE id=?", (queue_id,)
            ).fetchone()
        ) == (1,)
        rows = db.execute(
            "SELECT src_path FROM import_queue_files WHERE queue_id=?", (queue_id,)
        ).fetchall()
        assert (
            dict(
                db.execute(
                    "SELECT * FROM seen WHERE torrent_url=?", (item["url"],)
                ).fetchone()
            )
            == seen
        )
        assert [
            dict(row) for row in db.execute("SELECT * FROM volumes ORDER BY id")
        ] == grabbed
    _conserved(mixed_env, source, before)
    assert len(rows) == 2 and str(ordinary) in {row[0] for row in rows}
    from import_lease import claim_import_queue_row
    from import_plan import _plan_import

    with file_mutation_guard(mixed_env["db_path"]), main.get_db() as db:
        assert claim_import_queue_row(db, queue_id, "automatic-502")
        plan = _plan_import(
            db,
            queue_id,
            "automatic-502",
            {},
            {},
            set(),
            "copy",
            lease_seconds=180,
        )
        assert plan is not None and plan.queue["respect_grab_claims"] == 1
        # The real complete-pack grab owns volume3, not the unlinked chapter2.
        # Discovery does not confer a missing acquisition claim on the chapter.
        assert {(file.file_type, file.plan_status) for file in plan.files} == {
            ("volume", "ready"),
            ("chapter", "skip"),
        }
        assert [
            dict(row) for row in db.execute("SELECT * FROM volumes ORDER BY id")
        ] == grabbed
    _conserved(mixed_env, source, before)
