"""Optional immutable Linux wheel cache; virtual environments always stay session-local."""

import os
from pathlib import Path
import shutil
import tempfile

from schemas import atomic_json, digest, file_hash, locked, read_json


def wheel_from_cache(cache_root, identity, destination, build):
    """Serialize one build per complete provenance key and verify every cache hit."""
    destination = Path(destination)
    if not cache_root:
        return build()
    cache_root = Path(cache_root)
    key = digest(identity)
    entry = cache_root / key
    with locked(cache_root / f"{key}.lock"):
        metadata = entry / "entry.json"
        if metadata.exists():
            record = read_json(metadata)
            wheel = entry / record["filename"]
            if record["identity"] != identity or file_hash(wheel) != record["sha256"]:
                raise ValueError(f"Wheel cache corruption: {entry}")
            target = destination / wheel.name
            shutil.copyfile(wheel, target)
            return target
        wheel = build()
        temporary = Path(tempfile.mkdtemp(prefix=f".{key}.", dir=cache_root))
        try:
            shutil.copyfile(wheel, temporary / wheel.name)
            atomic_json(
                temporary / "entry.json",
                {"identity": identity, "filename": wheel.name, "sha256": file_hash(wheel)},
            )
            os.rename(temporary, entry)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return wheel
