"""Cache hits require exact provenance and unmodified wheel bytes."""

import pytest
from cache import wheel_from_cache


def test_verified_cache_publication_and_reuse(tmp_path):
    builds = []
    dest = tmp_path / "wheels"
    dest.mkdir()

    def build():
        builds.append(True)
        path = dest / "qiskit-test.whl"
        path.write_bytes(b"wheel")
        return path

    identity = {"source": "source", "abi": "cp312", "lock": "lock"}
    first = wheel_from_cache(tmp_path / "cache", identity, dest, build)
    first.unlink()
    second = wheel_from_cache(tmp_path / "cache", identity, dest, build)
    assert second.read_bytes() == b"wheel"
    assert len(builds) == 1
    (cached,) = (tmp_path / "cache").glob("*/qiskit-test.whl")
    cached.write_bytes(b"corruption")
    with pytest.raises(ValueError, match="corruption"):
        wheel_from_cache(tmp_path / "cache", identity, dest, build)
