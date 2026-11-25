# Crypto Runtime Identity

Images require the upstream cryptography 50.0.2 binary wheel. Its observed
embedded OpenSSL must be 4.0.3; Python's `ssl.OPENSSL_VERSION` is observed
separately and must not be used to identify cryptography's native backend.
Only cryptography is subject to pip's binary-only requirement.

As root, the image build runs `scripts/crypto_runtime.py collect` and stores
`/usr/share/doc/mangarr/crypto-runtime.cdx.json`. This deterministic,
maintainer-declared CycloneDX 1.6 companion identifies the Python package,
its embedded OpenSSL dependency, and the separate Python stdlib OpenSSL scope.
It records the observed machine (`x86_64`/amd64 or `aarch64`/arm64); unknown
machines or an unexpected package/backend pair fail closed.

The embedded component's `mangarr:native-module:path` and
`mangarr:native-module:sha256` properties identify the complete loaded `_rust`
module. That digest is **not** a hash of isolated OpenSSL bytes and is not put
in component `hashes`. The companion does not replace or modify automatic
package inventories or scanner findings.

The helper is installed outside `/app`, root-owned at
`/usr/local/lib/mangarr/crypto_runtime.py`. After switching to the final runtime
user, the build runs its read-only `verify` command against the root-owned
record. Verification regenerates the expected document from live package,
backend, stdlib, machine and native carrier observations and rejects any
metadata or carrier mismatch. Success prints compact JSON versions and the
complete native module SHA256. It does not start Mangarr, create accounts,
read configuration or access the database. The existing entrypoint is unchanged.

To inspect an installed image without starting the application:

```sh
docker run --rm --entrypoint python IMAGE -B \
  /usr/local/lib/mangarr/crypto_runtime.py verify \
  --record /usr/share/doc/mangarr/crypto-runtime.cdx.json
```

This is artifact identity, not security clearance or full remediation for
the November OpenSSL findings targeting 4.0.4. Version 50.0.2/4.0.3 does not
establish inclusion of those fixes. Application encryption, keys, ciphertext
and backup formats are unchanged; focused compatibility tests include a public
Fernet fixture generated with 50.0.0/4.0.1. Native arm64 execution, exact image
qualification and applicable advisory/release gates still require their own
evidence. A unit test with injected arm64 observations is not that evidence.
