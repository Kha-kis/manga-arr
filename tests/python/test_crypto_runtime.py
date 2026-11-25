"""Offline contracts for truthful, build-time cryptography runtime identity."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from scripts import crypto_runtime as runtime

ROOT = Path(__file__).resolve().parents[2]
RECORD = "/usr/share/doc/mangarr/crypto-runtime.cdx.json"
HELPER = "/usr/local/lib/mangarr/crypto_runtime.py"


@pytest.fixture
def observation(tmp_path: Path) -> runtime.Observation:
    carrier = tmp_path / "_rust.abi3.so"
    _ = carrier.write_bytes(b"public test carrier, not isolated OpenSSL bytes")
    return runtime.Observation(
        package_version="50.0.2",
        embedded_openssl="OpenSSL 4.0.3 21 Sep 2026",
        stdlib_openssl="OpenSSL 3.5.7 1 Sep 2026",
        machine="x86_64",
        native_module=carrier,
    )


def test_requirement_selects_supported_patch_wheel() -> None:
    assert "cryptography==50.0.2" in (ROOT / "requirements.txt").read_text()


def test_image_context_allowlists_crypto_helper() -> None:
    context = (ROOT / ".dockerignore").read_text().splitlines()
    assert "!scripts/" in context and "scripts/*" in context
    assert "!scripts/crypto_runtime.py" in context


def test_image_collects_as_root_and_verifies_as_final_user() -> None:
    source = (ROOT / "Dockerfile").read_text()
    assert f"COPY scripts/crypto_runtime.py {HELPER}" in source
    assert "--only-binary=cryptography" in source
    assert "--only-binary=:all:" not in source
    before, after = source.split("USER mangarr", 1)
    assert f"{HELPER} collect --record {RECORD}" in before
    assert f"{HELPER} verify --record {RECORD}" in after
    assert 'ENTRYPOINT ["python", "/app/docker_entrypoint.py"]' in after


def test_collect_distinguishes_embedded_and_stdlib_and_hash_scope(
    observation: runtime.Observation,
) -> None:
    document = runtime.collect(observation)
    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == "1.6"
    components = cast(list[dict[str, object]], document["components"])
    parent, embedded, stdlib = components
    assert parent["name"] == "cryptography" and parent["version"] == "50.0.2"
    assert parent["purl"] == "pkg:pypi/cryptography@50.0.2"
    assert embedded["name"] == "OpenSSL" and embedded["version"] == "4.0.3"
    assert embedded["cpe"] == "cpe:2.3:a:openssl:openssl:4.0.3:*:*:*:*:*:*:*"
    assert stdlib["name"] == "OpenSSL" and stdlib["version"] == "3.5.7"
    assert stdlib["bom-ref"] != embedded["bom-ref"]
    props = {
        item["name"]: item["value"]
        for item in cast(list[dict[str, str]], embedded["properties"])
    }
    assert props["mangarr:scope"] == "cryptography-embedded"
    assert props["mangarr:native-module:path"] == str(observation.native_module)
    assert (
        props["mangarr:native-module:sha256"]
        == hashlib.sha256(observation.native_module.read_bytes()).hexdigest()
    )
    assert props["mangarr:openssl:version-text"] == observation.embedded_openssl
    assert {"name": "mangarr:scope", "value": "python-stdlib-ssl"} in cast(
        list[dict[str, str]], stdlib["properties"]
    )
    assert all("hashes" not in component for component in components)
    assert {"ref": parent["bom-ref"], "dependsOn": [embedded["bom-ref"]]} in cast(
        list[object], document["dependencies"]
    )
    assert "maintainer-declared" in str(embedded["description"]).lower()


@pytest.mark.parametrize("machine,arch", [("x86_64", "amd64"), ("aarch64", "arm64")])
def test_collect_records_actual_supported_machine(
    observation: runtime.Observation, machine: str, arch: str
) -> None:
    document = runtime.collect(replace(observation, machine=machine))
    properties = cast(dict[str, list[dict[str, str]]], document["metadata"])[
        "properties"
    ]
    assert {"name": "mangarr:machine", "value": machine} in properties
    assert {"name": "mangarr:architecture", "value": arch} in properties


@pytest.mark.parametrize("machine", ["", "amd64", "arm64", "AMD64", "i686", "riscv64"])
def test_collect_refuses_unknown_machine(
    observation: runtime.Observation, machine: str
) -> None:
    with pytest.raises(ValueError, match="machine"):
        _ = runtime.collect(replace(observation, machine=machine))


@pytest.mark.parametrize(
    "field,value",
    [
        ("package_version", "50.0.0"),
        ("package_version", "50.0.2.dev1"),
        ("embedded_openssl", "OpenSSL 4.0.1 9 Jun 2026"),
        ("embedded_openssl", "OpenSSL 4.0.30 1 Jan 2026"),
        ("embedded_openssl", "LibreSSL 4.0.3"),
        ("stdlib_openssl", "not a version"),
    ],
)
def test_collect_rejects_unexpected_observed_versions(
    observation: runtime.Observation, field: str, value: str
) -> None:
    with pytest.raises(ValueError, match="version"):
        _ = runtime.collect(replace(observation, **{field: value}))


def test_collect_reads_real_installed_observations() -> None:
    import importlib.metadata
    import platform
    import ssl

    from cryptography.hazmat.bindings import _rust
    from cryptography.hazmat.backends.openssl.backend import backend

    observed = runtime.observe()
    assert observed.package_version == importlib.metadata.version("cryptography")
    assert observed.embedded_openssl == backend.openssl_version_text()
    assert observed.stdlib_openssl == ssl.OPENSSL_VERSION
    assert observed.machine == platform.machine()
    assert observed.native_module == Path(_rust.__file__).resolve()


def test_collect_and_verify_are_deterministic_and_read_only(
    observation: runtime.Observation, tmp_path: Path
) -> None:
    first = runtime.encode(runtime.collect(observation))
    assert first == runtime.encode(runtime.collect(observation))
    assert (
        first
        == json.dumps(json.loads(first), sort_keys=True, separators=(",", ":")) + "\n"
    )
    record = tmp_path / "record.json"
    _ = record.write_text(first)
    record.chmod(0o444)
    before = record.stat()
    result = runtime.verify(record, observation)
    assert result == {
        "cryptography": "50.0.2",
        "embedded_openssl": "4.0.3",
        "stdlib_openssl": "3.5.7",
        "machine": "x86_64",
        "native_module_sha256": hashlib.sha256(
            observation.native_module.read_bytes()
        ).hexdigest(),
    }
    assert record.read_text() == first
    assert record.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize(
    "target,field,value",
    [
        (0, "version", "50.0.0"),
        (1, "version", "4.0.4"),
        (1, "cpe", "cpe:2.3:a:openssl:openssl:4.0.4:*:*:*:*:*:*:*"),
        (1, "properties", []),
        (1, "hashes", [{"alg": "SHA-256", "content": "0" * 64}]),
        (2, "version", "4.0.3"),
    ],
)
def test_verify_rejects_tampered_component_metadata(
    observation: runtime.Observation,
    tmp_path: Path,
    target: int,
    field: str,
    value: object,
) -> None:
    document = runtime.collect(observation)
    cast(list[dict[str, object]], document["components"])[target][field] = value
    record = tmp_path / "record.json"
    _ = record.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="mismatch"):
        _ = runtime.verify(record, observation)


def test_verify_rejects_changed_carrier_bytes(
    observation: runtime.Observation, tmp_path: Path
) -> None:
    record = tmp_path / "record.json"
    _ = record.write_text(runtime.encode(runtime.collect(observation)))
    _ = observation.native_module.write_bytes(b"changed carrier")
    with pytest.raises(ValueError, match="mismatch"):
        _ = runtime.verify(record, observation)


@pytest.mark.parametrize(
    "field", ["package_version", "embedded_openssl", "stdlib_openssl"]
)
def test_verify_rechecks_live_versions(
    observation: runtime.Observation, tmp_path: Path, field: str
) -> None:
    record = tmp_path / "record.json"
    _ = record.write_text(runtime.encode(runtime.collect(observation)))
    with pytest.raises(ValueError):
        _ = runtime.verify(record, replace(observation, **{field: "changed"}))


@pytest.mark.parametrize(
    "payload", ["not JSON", "{}", "[]", '{"version":1,"version":2}']
)
def test_verify_refuses_invalid_documents(
    observation: runtime.Observation, tmp_path: Path, payload: str
) -> None:
    record = tmp_path / "record.json"
    _ = record.write_text(payload)
    with pytest.raises(ValueError):
        _ = runtime.verify(record, observation)


def test_unreadable_files_fail_closed(
    observation: runtime.Observation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(OSError):
        _ = runtime.verify(tmp_path / "absent.json", observation)
    with pytest.raises(OSError):
        _ = runtime.collect(replace(observation, native_module=tmp_path / "missing.so"))

    def denied(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("test-only file denial")

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(PermissionError):
        _ = runtime.collect(observation)


def test_cli_collect_verify_and_refusal_are_bounded_to_record(
    observation: runtime.Observation,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runtime, "observe", lambda: observation)
    record = tmp_path / "record.json"
    arguments = ["--record", str(record)]
    assert runtime.main(["collect", *arguments]) == 0
    collected = record.read_bytes()
    _ = capsys.readouterr()
    assert runtime.main(["verify", *arguments]) == 0
    report = cast(dict[str, str], json.loads(capsys.readouterr().out))
    assert report["cryptography"] == "50.0.2"
    assert report["embedded_openssl"] == "4.0.3"
    assert len(report["native_module_sha256"]) == 64
    assert record.read_bytes() == collected
    assert runtime.main(["collect", *arguments]) == 1
    assert record.read_bytes() == collected
    _ = record.write_text("tampered")
    assert runtime.main(["verify", *arguments]) == 1
    assert "tampered" not in capsys.readouterr().err
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "_rust.abi3.so",
        "record.json",
    ]


def test_old_50_0_0_fernet_fixture_decrypts_through_existing_cipher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import security

    # Public fixture generated with cryptography 50.0.0 / embedded OpenSSL 4.0.1.
    # No TTL: this tests the existing ciphertext format, not wall-clock policy.
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    monkeypatch.setenv(
        "MANGARR_SECRET_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
    )
    _ = security.load_or_create_secret_cipher(str(tmp_path))
    token = (
        "gAAAAABrB09KhwE-Yn5FMZBvNwB0dsKDsCPdPupJY6dfUmxKvKN-tvlcOQ9GA2DqNXJ1DAZSzOhPo"
        "VebrUU_Ikt2mKmxOmhYTfl12iVwCJ_2pRMjdxXqdH1VoMFJ6qdUD2rpdB-p5Fzk"
    )
    assert (
        security.decrypt_secret("enc:v1:" + token)
        == "public crypto526 compatibility fixture"
    )
