# Archive Runtime Release Gate

This explicit image gate does not install anything on the host or need app ports.
Run against the immutable ID of a candidate built from the reviewed Dockerfile:

```sh
MANGARR_ARCHIVE_GATE_IMAGE=sha256:<candidate-id> python -m pytest -q tests/archive_release
```

The original six checks and their probe are preserved verbatim from the approved
RED checkpoint. Default IMAGE is that historical RED image; set the candidate
explicitly. Hashes:

- `test_mangarr_archive_release_gate.py`: `f266942053f41b8b6030f45e9aaffb1c91e71cf3564dbeb848f5f5757e81b7b6`
- `mangarr_archive_runtime_probe.py`: `5f3ca526d02842a4aa0fceeeb6c9958cc202bfba58f84d9a70dcd9032352bbc6`

Probe containers have no network, readonly image/source, dropped capabilities,
UID1000, bounded private tmpfs, 512MiB memory/no extra swap, one CPU and 64 PIDs.
All extracted files are synthetic and live only in their own temporary directory.
Assertions require actual payload bytes through both `/usr/bin/7z` and the same
forced rarfile 7z backend, not a successful listing or stored-RAR workaround.

`tests/fixtures/archive_release/multipart-rar5.json` contains five genuine RAR5
volumes generated with the official RAR 7.11 executable in an isolated container.
Its source archive hash, command, deterministic payload recipe, full output hash,
and every volume hash are recorded. The payload is synthetic; no user media or
encoder executable is redistributed. No RAR encoder or new backend is added to
Mangarr. The image retains Debian's packaged 7zip-rar copyright file, including
the complete unRAR notice (the slim base filters the redundant separate text);
the application's AGPL label is not a claim that every image dependency is AGPL
or MIT, or that the RAR decompression codec may be used to implement an encoder.

Parser regressions are in `tests/python/test_archive_release_security.py` and
run as normal project tests with the pinned runtime requirements. A recording
file-like reader refuses excessive read requests before allocating, including
the OLD_SUB 32-bit size and RAR5 comment size bounds. Valid small and exactly
256KiB comments remain positive controls. The source-component tests execute
only the Dockerfile's edit/assert prefix against a private sample sources file,
never APT on the host. Image codec gates and minimum-Python qualification are
separate from, and do not claim, parent-owned actual NFS or arm64 qualification.
