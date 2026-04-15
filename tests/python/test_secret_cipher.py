"""Tests for H4 PR #1: secret cipher primitives.

Scope: encrypt/decrypt round trip, prefix detection, wrong-key + malformed
rejection, master-key resolution (env → file → auto-generate),
permission bits on the auto-generated file, and a hard guarantee that
key bytes / canary plaintext never leak into log records.

No DB migration / read-write wiring is exercised here — that's H4 PRs
#2-#4. PR #1 ships the primitives + lifespan startup hook.
"""
import logging
import os
import stat
import sys

import pytest

sys.path.insert(0, "tests/python")
sys.path.insert(0, "app")
import conftest  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_cipher_cache(monkeypatch):
    """Each test gets a fresh module-level _SECRET_CIPHER. The primitives
    are designed to be loaded once per process; tests need to start
    clean so we can exercise different resolution paths."""
    import security
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    # Also unset the env var per default so file/auto-gen tests aren't
    # contaminated. Tests that need the env var set it explicitly.
    monkeypatch.delenv("MANGARR_SECRET_KEY", raising=False)


@pytest.fixture
def cfg_dir(tmp_path):
    """Per-test config_dir for the auto-generation path."""
    d = tmp_path / "config"
    d.mkdir()
    return str(d)


# ───────────────────── primitives ─────────────────────

def test_round_trip(cfg_dir):
    from security import load_or_create_secret_cipher, encrypt_secret, decrypt_secret
    load_or_create_secret_cipher(cfg_dir)
    for plain in ("hunter2", "a", "ünîcødé", "x" * 4096, "with spaces and symbols !@#$%"):
        ct = encrypt_secret(plain)
        assert ct.startswith("enc:v1:")
        assert decrypt_secret(ct) == plain


def test_empty_and_none_pass_through(cfg_dir):
    from security import load_or_create_secret_cipher, encrypt_secret, decrypt_secret
    load_or_create_secret_cipher(cfg_dir)
    assert encrypt_secret("") == ""
    assert encrypt_secret(None) is None
    assert decrypt_secret("") == ""
    assert decrypt_secret(None) is None


def test_decrypt_plaintext_passes_through(cfg_dir):
    """A value without the enc:v1: prefix is plaintext (back-compat /
    env-supplied secret) and must come back as-is."""
    from security import load_or_create_secret_cipher, decrypt_secret
    load_or_create_secret_cipher(cfg_dir)
    for s in ("plain-credential", "abc", "any-old-string"):
        assert decrypt_secret(s) == s


def test_is_encrypted_secret():
    from security import is_encrypted_secret
    assert is_encrypted_secret("enc:v1:gAAAAAB...") is True
    assert is_encrypted_secret("enc:v1:") is True
    assert is_encrypted_secret("plain") is False
    assert is_encrypted_secret("") is False
    assert is_encrypted_secret(None) is False
    assert is_encrypted_secret(42) is False
    assert is_encrypted_secret("v1:notprefixed") is False


def test_wrong_key_raises_decryption_error(cfg_dir):
    """Encrypt with key A, swap in key B, decrypt must raise
    SecretDecryptionError. The exception message must NOT contain the
    ciphertext or the key."""
    import security
    from security import (
        load_or_create_secret_cipher, encrypt_secret, decrypt_secret,
        SecretDecryptionError,
    )
    load_or_create_secret_cipher(cfg_dir)
    canary = "CANARY-PLAINTEXT-DO-NOT-LEAK"
    ct = encrypt_secret(canary)

    # Swap in a fresh cipher with a different key
    from cryptography.fernet import Fernet
    security._SECRET_CIPHER = Fernet(Fernet.generate_key())

    with pytest.raises(SecretDecryptionError) as exc:
        decrypt_secret(ct)
    msg = str(exc.value)
    assert "wrong key" in msg.lower() or "decrypt" in msg.lower()
    # The exception message must not contain the ciphertext bytes
    assert ct not in msg
    assert "enc:v1:" not in msg.replace("decrypt", "")  # generic message only
    # And nothing in the message should equal or contain the canary
    assert canary not in msg


def test_malformed_token_raises_decryption_error(cfg_dir):
    from security import (
        load_or_create_secret_cipher, decrypt_secret, SecretDecryptionError,
    )
    load_or_create_secret_cipher(cfg_dir)
    for bad in [
        "enc:v1:not-a-token-at-all",
        "enc:v1:!@#$%",
        "enc:v1:gAAAAAB" + "x" * 50,   # right shape, wrong contents
        "enc:v1:",                       # empty token
    ]:
        with pytest.raises(SecretDecryptionError):
            decrypt_secret(bad)


# ───────────────────── master key resolution ─────────────────────

def test_env_var_wins_over_file(cfg_dir, monkeypatch):
    """Both env and file present → env takes precedence."""
    from cryptography.fernet import Fernet
    from security import load_or_create_secret_cipher, encrypt_secret

    env_key = Fernet.generate_key()
    file_key = Fernet.generate_key()
    assert env_key != file_key

    # Pre-populate the file with the file_key
    key_path = os.path.join(cfg_dir, ".mangarr-secret-key")
    with open(key_path, "wb") as f:
        f.write(file_key)
    os.chmod(key_path, 0o600)

    # Set the env var to env_key
    monkeypatch.setenv("MANGARR_SECRET_KEY", env_key.decode())

    load_or_create_secret_cipher(cfg_dir)

    # Encrypt with our cached cipher; decrypt with a fresh Fernet seeded
    # from env_key (NOT file_key). If env won, the round trip works.
    ct = encrypt_secret("test")
    fresh = Fernet(env_key)
    assert fresh.decrypt(ct[len("enc:v1:"):].encode()).decode() == "test"


def test_file_key_loads_when_env_absent(cfg_dir):
    from cryptography.fernet import Fernet
    from security import load_or_create_secret_cipher, encrypt_secret

    file_key = Fernet.generate_key()
    key_path = os.path.join(cfg_dir, ".mangarr-secret-key")
    with open(key_path, "wb") as f:
        f.write(file_key)
    os.chmod(key_path, 0o600)

    load_or_create_secret_cipher(cfg_dir)
    ct = encrypt_secret("from-file-key")
    fresh = Fernet(file_key)
    assert fresh.decrypt(ct[len("enc:v1:"):].encode()).decode() == "from-file-key"


def test_auto_generate_creates_mode_0600_file(cfg_dir):
    """Neither env nor file present → primitive auto-generates the file
    at <cfg>/.mangarr-secret-key with mode 0600."""
    from security import load_or_create_secret_cipher
    key_path = os.path.join(cfg_dir, ".mangarr-secret-key")
    assert not os.path.exists(key_path)

    load_or_create_secret_cipher(cfg_dir)

    assert os.path.isfile(key_path)
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
    # File contents should look like a Fernet key (44 chars urlsafe-base64)
    with open(key_path, "rb") as f:
        content = f.read().strip()
    assert len(content) == 44, f"unexpected key length {len(content)}"


def test_auto_generated_key_persists_across_loads(cfg_dir, monkeypatch):
    """Boot 1 auto-generates → boot 2 should read the same key from
    disk, not regenerate."""
    from security import load_or_create_secret_cipher, encrypt_secret, decrypt_secret
    import security

    load_or_create_secret_cipher(cfg_dir)
    canary = "persisted-secret"
    ct = encrypt_secret(canary)
    key_path = os.path.join(cfg_dir, ".mangarr-secret-key")
    with open(key_path, "rb") as f:
        gen_key = f.read().strip()

    # Reset cache, load again — should read same key, decrypt same ct
    monkeypatch.setattr(security, "_SECRET_CIPHER", None)
    load_or_create_secret_cipher(cfg_dir)

    assert decrypt_secret(ct) == canary
    with open(key_path, "rb") as f:
        assert f.read().strip() == gen_key, "key was regenerated on second load"


def test_invalid_env_key_fails_clearly(monkeypatch, cfg_dir):
    from security import load_or_create_secret_cipher, SecretCipherUnavailable

    monkeypatch.setenv("MANGARR_SECRET_KEY", "not-a-valid-fernet-key")
    with pytest.raises(SecretCipherUnavailable) as exc:
        load_or_create_secret_cipher(cfg_dir)
    msg = str(exc.value)
    assert "MANGARR_SECRET_KEY" in msg
    assert "invalid" in msg.lower() or "format" in msg.lower()
    # Must NOT include the bad key bytes
    assert "not-a-valid-fernet-key" not in msg


def test_invalid_file_key_fails_clearly(cfg_dir):
    """File exists but contents aren't a Fernet key."""
    from security import load_or_create_secret_cipher, SecretCipherUnavailable
    key_path = os.path.join(cfg_dir, ".mangarr-secret-key")
    with open(key_path, "wb") as f:
        f.write(b"garbage that is not a Fernet key")
    os.chmod(key_path, 0o600)

    with pytest.raises(SecretCipherUnavailable) as exc:
        load_or_create_secret_cipher(cfg_dir)
    assert "key file" in str(exc.value).lower()


def test_idempotent_load(cfg_dir):
    """Calling load twice returns the same Fernet instance (module cache)."""
    from security import load_or_create_secret_cipher
    a = load_or_create_secret_cipher(cfg_dir)
    b = load_or_create_secret_cipher(cfg_dir)
    assert a is b


def test_permissive_file_logs_warning(cfg_dir, caplog):
    from cryptography.fernet import Fernet
    from security import load_or_create_secret_cipher
    key_path = os.path.join(cfg_dir, ".mangarr-secret-key")
    with open(key_path, "wb") as f:
        f.write(Fernet.generate_key())
    os.chmod(key_path, 0o644)   # world-readable — bad

    with caplog.at_level(logging.WARNING, logger="security"):
        load_or_create_secret_cipher(cfg_dir)

    msgs = [r.getMessage() for r in caplog.records]
    assert any("permissive" in m.lower() and "chmod 600" in m for m in msgs), \
        f"expected chmod-600 advisory; got: {msgs}"


# ───────────────────── never-leak guards ─────────────────────

def test_logs_never_include_key_or_canary_during_full_lifecycle(cfg_dir, caplog):
    """End-to-end: auto-generate a key, encrypt a canary plaintext,
    decrypt with wrong key (triggers SecretDecryptionError log path),
    capture every log record. Neither the key file's contents nor the
    canary string may appear in any record."""
    import security
    from security import (
        load_or_create_secret_cipher, encrypt_secret, decrypt_secret,
        SecretDecryptionError,
    )

    canary = "TOP-SECRET-CANARY-ZXY"

    with caplog.at_level(logging.DEBUG, logger="security"):
        load_or_create_secret_cipher(cfg_dir)
        ct = encrypt_secret(canary)

        # Read the auto-generated key bytes; assert they don't appear in logs
        key_path = os.path.join(cfg_dir, ".mangarr-secret-key")
        with open(key_path, "rb") as f:
            key_bytes = f.read().strip().decode()

        # Trigger a decrypt-failure log path
        from cryptography.fernet import Fernet
        security._SECRET_CIPHER = Fernet(Fernet.generate_key())
        try:
            decrypt_secret(ct)
        except SecretDecryptionError:
            pass

    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert canary not in joined, f"canary plaintext leaked into logs: {joined!r}"
    assert key_bytes not in joined, f"key bytes leaked into logs: {joined!r}"
    # And the ciphertext shouldn't appear either (defense in depth)
    assert ct not in joined, f"ciphertext leaked into logs: {joined!r}"


def test_encrypt_without_init_raises_cipher_unavailable(cfg_dir):
    """If a future caller invokes encrypt_secret before lifespan has
    initialised the cipher, they get a clear error — not a cryptic
    NoneType crash."""
    from security import encrypt_secret, SecretCipherUnavailable
    with pytest.raises(SecretCipherUnavailable, match="not initialised"):
        encrypt_secret("anything")


def test_encrypt_secret_rejects_non_string(cfg_dir):
    from security import load_or_create_secret_cipher, encrypt_secret
    load_or_create_secret_cipher(cfg_dir)
    with pytest.raises(TypeError):
        encrypt_secret(42)
    with pytest.raises(TypeError):
        encrypt_secret(b"bytes-not-str")
