"""Tests for step 4.1: payload archive round-trip + checksum verification logic."""

from __future__ import annotations

import base64
import hashlib
import tarfile

from shbuild.builder import b64_chunks
from shbuild.shgen import _deterministic_tar, _sha256_hex


def test_archive_roundtrip_files_match(tmp_path) -> None:
    """create -> deterministic tar -> base64 encode (chunks) -> decode -> tar extract.

    The extracted tree must be byte-identical to the original payload, and a
    second build of the same content must yield the identical archive bytes
    (reproducible fingerprint).
    """
    payload = tmp_path / "payload"
    venv_bin = payload / "venv" / "bin"
    app_pkg = payload / "app" / "pkg"
    for d in (venv_bin, app_pkg):
        d.mkdir(parents=True)

    files = {
        payload / "MANIFEST.json": b'{"entry": "main.py"}\n',
        venv_bin / "python3.12": b"/fake/interp\x00bytes",
        app_pkg / "main.py": b"print('hi')\n",
        app_pkg / "mod.py": b"x = 42\n",
    }
    for p, data in files.items():
        p.write_bytes(data)

    archive1 = tmp_path / "p1.tar.gz"
    _deterministic_tar(payload, archive1)
    data1 = archive1.read_bytes()
    digest1 = _sha256_hex(data1)

    # encode -> decode via the exact chunking used in the wrapper:
    chunks = b64_chunks(data1)
    joined = "".join(chunks)  # what `printf '%s' "$B64_CHUNKS" | base64 -d` sees
    decoded = base64.b64decode(joined)
    assert decoded == data1, "base64 round-trip must reproduce archive bytes exactly"

    # tar extract and compare every file byte-for-byte.
    out = tmp_path / "extracted"
    out.mkdir()
    with tarfile.open(archive1, "r:gz") as tf:
        tf.extractall(out)
    for p, data in files.items():
        rel = p.relative_to(payload)
        got = (out / rel).read_bytes()
        assert got == data, f"mismatch after round-trip: {rel}"

    # Reproducibility: same content -> byte-identical archive + checksum.
    archive2 = tmp_path / "p2.tar.gz"
    _deterministic_tar(payload, archive2)
    assert archive2.read_bytes() == data1
    assert _sha256_hex(archive2.read_bytes()) == digest1


def test_checksum_logic_passes_and_fails(tmp_path) -> None:
    """The wrapper compares `ACTUAL = sha256_of(file)` against the embedded SHA.

    Simulate that comparison directly: matching digests pass, any bit flip fails.
    """
    payload = tmp_path / "payload"
    (payload / "venv").mkdir(parents=True)
    (payload / "hello.txt").write_bytes(b"abc")

    archive = tmp_path / "p.tar.gz"
    _deterministic_tar(payload, archive)
    digest = _sha256_hex(archive.read_bytes())

    # PASS: file on disk is exactly what was embedded.
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert actual == digest  # wrapper would NOT enter the mismatch branch

    # FAIL: corrupted archive must produce a different digest -> wrapper exits 1.
    bad = bytearray(archive.read_bytes())
    bad[0] ^= 0x01
    (tmp_path / "bad.tar.gz").write_bytes(bytes(bad))
    assert hashlib.sha256(bytes(bad)).hexdigest() != digest
