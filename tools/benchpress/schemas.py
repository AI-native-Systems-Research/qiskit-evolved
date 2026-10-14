"""Shared identities, atomic artifacts, and execution environment (stdlib only)."""

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

SCHEMA = 1
BASELINE = "1298f960b7e814cfcb6aa9338b548ec5c6d2b355"
CANDIDATE = "90ccaac29b2a547592e870797265007a128a02eb"
BENCHPRESS = "b695f30e83a32bac05b9b4d8e98d37ba9aae5236"
BENCHMARK = "e33f5934cb462e719b13b8d352ced107f575b4f7"
REVISIONS = ("baseline", "candidate")
THREADS = {
    "RAYON_NUM_THREADS": "1",
    "QISKIT_NUM_PROCS": "1",
    "QISKIT_PARALLEL": "FALSE",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "BLIS_NUM_THREADS": "1",
}
QUALITY_FIELDS = (
    "input_num_qubits",
    "output_num_qubits",
    "output_gate_count_2q",
    "output_depth_2q",
    "output_circuit_operations",
    "native_2q_gate",
)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def file_hash(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    return [
        json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line
    ]


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def write_jsonl(path, rows):
    atomic_text(path, "".join(json.dumps(r, sort_keys=True, allow_nan=False) + "\n" for r in rows))


@contextlib.contextmanager
def locked(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def state_dir(root):
    return Path(str(root) + ".lsf")


def command(argv, **kwargs):
    return subprocess.run(
        [str(x) for x in argv], check=True, text=True, capture_output=True, **kwargs
    ).stdout.strip()


def clean_env():
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("PYTHON", "PYTEST", "BENCHPRESS", "QISKIT", "RUST")):
            env.pop(key)
    env.update(THREADS)
    env["QISKIT_IGNORE_USER_SETTINGS"] = "TRUE"
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONHASHSEED="0")
    return env


def manifest(root, expected=None):
    path = Path(root) / "results/manifest.json"
    if expected and file_hash(path) != expected:
        raise ValueError("Manifest checksum mismatch")
    data = read_json(path)
    for relative, checksum in data.get("artifacts", {}).items():
        if file_hash(Path(root) / relative) != checksum:
            raise ValueError(f"Frozen artifact changed: {relative}")
    return data


def raw_dir(root, stage, task, level, revision, attempt):
    return (
        Path(root)
        / "results/raw"
        / stage
        / task["panel_id"]
        / f"opt-{level}"
        / f"seed-{task['seed']}"
        / f"attempt-{attempt}"
        / revision
    )


def invocation_identity(config, task, level, revision, attempt):
    case = task["cases"][str(level)]
    return {
        "schema_version": SCHEMA,
        "run_id": config["run_id"],
        "profile": config["profile"],
        "stage": task["stage"],
        "task_id": task["task_id"],
        "panel_id": task["panel_id"],
        "case_id": case["case_id"],
        "node_id": task["node_id"],
        "suite": task["suite"],
        "topology": task["topology"],
        "optimization_level": level,
        "seed": task["seed"],
        "revision": revision,
        "attempt": attempt,
        "array_index": task["array_index"],
        "qiskit_sha": config[f"{revision}_ref"],
        "harness_sha": config["harness_ref"],
        "benchpress_sha": BENCHPRESS,
        "benchmark_sha": BENCHMARK,
        "input_hash": task["input_hash"],
        "config_hash": case["config_hash"],
        "target_hash": task["target_hash"],
        "backend_seed": config["backend_seed"],
        "adapter_hash": config["adapter_hash"],
        "lock_hash": config["lock_hash"],
        "wheel_hash": config["builds"][revision]["wheel_hash"],
        "extension_hash": config["builds"][revision]["extension_hash"],
    }
