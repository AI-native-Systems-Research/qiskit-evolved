"""Build both pinned Linux wheels, freeze dependencies/inputs, and validate the adapter."""

import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tomllib

from adapter import apply_adapter, write_configs
from lsf.context import platform_info
from panel import expand, select_panel
from run import quality_env, pytest_argv, run_element
from schemas import (
    BENCHMARK,
    BENCHPRESS,
    REVISIONS,
    RUNTIME_MEASUREMENT,
    THREADS,
    atomic_json,
    atomic_text,
    clean_env,
    command,
    digest,
    file_hash,
    locked,
    read_json,
    state_dir,
    utcnow,
    write_jsonl,
)

AFFECTED = [
    "transpiler/test_sabre_swap.py",
    "transpiler/test_sabre_layout.py",
    "transpiler/test_preset_passmanagers.py",
    "transpiler/test_transpile_layout.py",
    "compiler/test_transpiler.py",
]


def archive(repo, ref, destination, paths=()):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    data = subprocess.check_output(["git", "-C", str(repo), "archive", ref, *paths])
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(destination, filter="data")


def pinned_source(url, sha, destination):
    destination = Path(destination)
    marker = destination / ".benchpress-source-pin.json"
    if marker.exists():
        if read_json(marker)["sha"] != sha:
            raise ValueError("Existing upstream snapshot has the wrong source pin")
        return
    if destination.exists():
        import uuid

        destination.rename(
            destination.with_name(destination.name + ".incomplete-" + uuid.uuid4().hex)
        )
    # Preserve the Git revision and original upstream tree for provenance.
    command(["git", "init", destination])
    command(["git", "-C", destination, "remote", "add", "origin", url])
    command(["git", "-C", destination, "fetch", "--depth=1", "origin", sha])
    command(["git", "-C", destination, "checkout", "--detach", sha])
    if command(["git", "-C", destination, "rev-parse", "HEAD"]) != sha:
        raise ValueError("Upstream source pin mismatch")
    atomic_json(marker, {"sha": sha, "url": url})


def logged(argv, path, cwd, env=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps([str(a) for a in argv]) + "\n")
        stream.flush()
        subprocess.run(
            [str(a) for a in argv],
            cwd=cwd,
            env=env or clean_env(),
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=True,
        )


def environment(path):
    if not path.exists():
        command([sys.executable, "-m", "venv", path])
    return path / "bin/python"


def freeze(python, path, cwd):
    value = command([python, "-m", "pip", "freeze", "--all"], cwd=cwd, env=clean_env())
    atomic_text(path, value + "\n")
    return value.splitlines()


def import_provenance(python, wheel, cwd):
    code = """import hashlib, importlib.metadata as m, json, pathlib, qiskit, qiskit._accelerate, sys
extension = pathlib.Path(qiskit._accelerate.__file__).resolve()
package = pathlib.Path(qiskit.__file__).resolve()
assert extension.is_relative_to(pathlib.Path(sys.prefix).resolve())
assert package.is_relative_to(pathlib.Path(sys.prefix).resolve())
print(json.dumps(dict(python=sys.executable, version=qiskit.__version__, module_path=str(package),
    extension_path=str(extension), extension_hash=hashlib.sha256(extension.read_bytes()).hexdigest(),
    direct_url=json.loads(m.distribution('qiskit').read_text('direct_url.json')))))"""
    data = json.loads(command([python, "-I", "-c", code], cwd=cwd, env=clean_env()))
    data.update(wheel_path=str(wheel), wheel_hash=file_hash(wheel))
    if (
        data["direct_url"].get("archive_info", {}).get("hashes", {}).get("sha256")
        != data["wheel_hash"]
    ):
        raise ValueError("Installed Qiskit does not identify the intended wheel")
    return data


def semantic_check(python, log, cwd):
    # Operator.from_circuit accounts for transpiler layout; use only a small unitary circuit.
    code = """from qiskit import QuantumCircuit
from qiskit.providers.fake_provider import GenericBackendV2
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit.quantum_info import Operator
qc = QuantumCircuit(4)
qc.h(0); qc.cx(0, 3); qc.ry(0.3, 1); qc.cx(1, 2); qc.cx(3, 1)
backend = GenericBackendV2(4, basis_gates=['id','sx','x','rz','cz'],
    coupling_map=[[0,1],[1,0],[1,2],[2,1],[2,3],[3,2]], seed=424242)
for level in (2, 3):
    outputs = [generate_preset_pass_manager(level, backend, seed_transpiler=7).run(qc) for _ in range(2)]
    assert outputs[0] == outputs[1], 'seed reproducibility failed'
    assert Operator.from_circuit(outputs[0]).equiv(Operator(qc)), 'semantic validation failed'
print('Small semantic/layout and seed reproducibility checks passed')"""
    logged([python, "-I", "-c", code], log, cwd)


def preserve_failed_build(root):
    """Explicit resume starts fresh environments and retains the interrupted build."""
    import uuid

    preserved = root / "failed-builds" / uuid.uuid4().hex
    preserved.mkdir(parents=True)
    for name in (
        "env",
        "wheels",
        "target",
        "results",
        "tasks",
        "build-requirements.lock",
        "shared-requirements.lock",
        "analysis-requirements.lock",
        "baseline.freeze.txt",
        "candidate.freeze.txt",
    ):
        path = root / name
        if path.exists():
            path.rename(preserved / name)
    bp = root / "src/benchpress"
    pristine = root / "src/benchpress-pristine"
    if bp.exists():
        bp.rename(preserved / "benchpress")
    if pristine.exists():
        shutil.copytree(pristine, bp)
    for path in (root / "build").iterdir():
        if path.is_file() and path.name != "prepare.lock":
            path.rename(preserved / path.name)
    atomic_json(
        preserved / "recovery.json",
        {"preserved": utcnow(), "reason": "explicit resume of incomplete build"},
    )


def prepare(root):
    root = Path(root).resolve()
    config = read_json(state_dir(root) / "launch.json")
    for key in ("panel_file", "job_env_script"):
        if config.get(key) and file_hash(config[key]) != config[key + "_hash"]:
            raise ValueError(f"Archived {key} changed")
    if sys.version_info[:2] != (3, 12) or sys.platform != "linux":
        raise ValueError("Build jobs require Linux and Python 3.12")
    with locked(root / "build/prepare.lock", blocking=False):
        ready = root / "build/ready.json"
        if ready.exists():
            from schemas import manifest

            manifest(root, read_json(ready)["manifest_hash"])
            return
        if (root / "build/started.json").exists():
            preserve_failed_build(root)
        atomic_json(root / "build/started.json", {"started": utcnow()})
        bp = root / "src/benchpress"
        plugin = root / "src/pytest-benchmark"
        pinned_source("https://github.com/Qiskit/benchpress.git", BENCHPRESS, bp)
        pinned_source("https://github.com/1ucian0/pytest-benchmark.git", BENCHMARK, plugin)
        pristine = root / "src/benchpress-pristine"
        if not pristine.exists():
            shutil.copytree(bp, pristine, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        results = root / "results"
        patch = results / "benchpress-reproducibility.patch"
        apply_adapter(bp, patch)
        write_configs(bp, results / "configs", config["opt_levels"])
        config["adapter_hash"] = file_hash(patch)
        validation = results / "validation"
        validation.mkdir(parents=True, exist_ok=True)
        build_env = clean_env()
        build_env.update(
            RUSTUP_TOOLCHAIN="1.89.0",
            QISKIT_BUILD_PROFILE="release",
            CARGO_BUILD_JOBS=str(config["build_slots"]),
        )
        build_env.pop("CARGO_ENCODED_RUSTFLAGS", None)
        atomic_json(
            root / "build/toolchain.json",
            {
                "platform": platform_info(),
                "python": sys.version,
                "rustc": command(["rustup", "run", "1.89.0", "rustc", "--version"], env=build_env),
                "cc": command(["cc", "--version"]),
                "flags": {
                    k: build_env[k]
                    for k in ("RUSTUP_TOOLCHAIN", "QISKIT_BUILD_PROFILE", "CARGO_BUILD_JOBS")
                },
            },
        )
        builder = environment(root / "env/build")
        source = root / "src/baseline"
        requirements = tomllib.loads((source / "pyproject.toml").read_text())["build-system"][
            "requires"
        ]
        logged(
            [builder, "-m", "pip", "install", *requirements, "wheel"],
            results / "build-dependencies.log",
            root,
        )
        freeze(builder, root / "build-requirements.lock", root)
        wheels = {}
        for revision in REVISIONS:
            wheel_dir = root / "wheels" / revision
            wheel_dir.mkdir(parents=True)
            env = dict(build_env, CARGO_TARGET_DIR=str(root / "target" / revision))

            def build_wheel():
                logged(
                    [
                        builder,
                        "-m",
                        "pip",
                        "wheel",
                        "--no-build-isolation",
                        "--no-deps",
                        "--wheel-dir",
                        wheel_dir,
                        root / "src" / revision,
                    ],
                    results / f"build-{revision}.log",
                    root,
                    env,
                )
                matches = list(wheel_dir.glob("qiskit-*.whl"))
                if len(matches) != 1:
                    raise ValueError("Expected exactly one Qiskit release wheel")
                return matches[0]

            from cache import wheel_from_cache

            identity = {
                "source": config[f"{revision}_ref"],
                "toolchain": read_json(root / "build/toolchain.json"),
                "build_lock": file_hash(root / "build-requirements.lock"),
                "cargo_lock": file_hash(root / "src" / revision / "Cargo.lock"),
                "adapter": config["adapter_hash"],
                "profile": "release",
            }
            wheels[revision] = wheel_from_cache(
                config.get("build_cache_root"), identity, wheel_dir, build_wheel
            )
        plugin_wheels = root / "wheels/plugin"
        plugin_wheels.mkdir()
        logged(
            [builder, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", plugin_wheels, plugin],
            results / "build-plugin.log",
            root,
        )
        (plugin_wheel,) = plugin_wheels.glob("*.whl")
        baseline = environment(root / "env/baseline")
        logged(
            [
                baseline,
                "-m",
                "pip",
                "install",
                wheels["baseline"],
                plugin_wheel,
                "-r",
                bp / "requirements.txt",
                "pytest>=8,<9",
                "qiskit-ibm-runtime",
                "stestr",
                "ddt",
                "testtools",
                "fixtures",
                "threadpoolctl",
            ],
            results / "install-baseline.log",
            root,
        )
        lines = freeze(baseline, root / "baseline.freeze.txt", root)
        shared = [line for line in lines if not line.lower().startswith(("qiskit==", "qiskit @"))]
        atomic_text(root / "shared-requirements.lock", "\n".join(shared) + "\n")
        candidate = environment(root / "env/candidate")
        logged(
            [
                candidate,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "-r",
                root / "shared-requirements.lock",
                wheels["candidate"],
            ],
            results / "install-candidate.log",
            root,
        )
        candidate_lines = freeze(candidate, root / "candidate.freeze.txt", root)
        if shared != [
            line
            for line in candidate_lines
            if not line.lower().startswith(("qiskit==", "qiskit @"))
        ]:
            raise ValueError("Runtime dependency inventories differ")
        config["lock_hash"] = file_hash(root / "shared-requirements.lock")
        config["builds"] = {}
        inventories = []
        for revision in REVISIONS:
            python = root / "env" / revision / "bin/python"
            logged([python, "-m", "pip", "check"], validation / f"{revision}-pip-check.log", root)
            config["builds"][revision] = import_provenance(python, wheels[revision], root)
            config["builds"][revision]["cargo_lock_hash"] = file_hash(
                root / "src" / revision / "Cargo.lock"
            )
            # Copy only test packages; never put the Qiskit source directory on sys.path.
            tests = validation / revision
            tests.mkdir()
            shutil.copytree(root / "src" / revision / "test", tests / "test")
            test_env = clean_env()
            test_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            if revision == "candidate" and config.get("skip_candidate_unit_tests"):
                atomic_text(
                    validation / f"{revision}-affected-tests.log",
                    "Skipped: --skip-candidate-unit-tests was set at submission.\n",
                )
            else:
                logged(
                    [
                        python,
                        "-m",
                        "pytest",
                        "-o",
                        "addopts=",
                        "--rootdir",
                        tests,
                        *[tests / "test/python" / name for name in AFFECTED],
                    ],
                    validation / f"{revision}-affected-tests.log",
                    tests,
                    test_env,
                )
            semantic_check(python, validation / f"{revision}-semantic.log", root)
            for level in config["opt_levels"]:
                work = validation / f"collect-{revision}-{level}"
                work.mkdir()
                output = work / "inventory.json"
                env = quality_env(root, config, level, 0)
                env["BENCHPRESS_COLLECTION_OUTPUT"] = str(output)
                argv = pytest_argv(root, revision, "benchpress/qiskit_gym/device_transpile", work)
                argv += [
                    str(bp / "benchpress/qiskit_gym/abstract_transpile"),
                    "--collect-only",
                    "-q",
                ]
                logged(argv, work / "collection.log", work, env)
                inventories.append(read_json(output))
        if not all(inventory == inventories[0] for inventory in inventories):
            raise ValueError("Collection differs by revision/optimization level")
        inventory = inventories[0]
        if len(inventory) != 1054:
            raise ValueError(f"Pinned collection expected 1054 entries, got {len(inventory)}")
        write_jsonl(results / "inventory.jsonl", inventory)
        panel = select_panel(
            inventory, config["profile"], config.get("panel_file"), config["panel_size"]
        )
        targets = []
        for revision in REVISIONS:
            path = validation / f"targets-{revision}.json"
            atomic_json(path, panel)
            env = quality_env(root, config, config["opt_levels"][0], 0)
            logged(
                [
                    root / "env" / revision / "bin/python",
                    root / "harness/tools/benchpress/panel.py",
                    path,
                ],
                validation / f"targets-{revision}.log",
                root,
                env,
            )
            targets.append(read_json(path))
        if targets[0] != targets[1]:
            raise ValueError("Target fingerprints differ across revisions")
        panel = targets[0]
        config_hashes = {
            str(level): file_hash(results / "configs" / f"opt-{level}.conf")
            for level in config["opt_levels"]
        }
        cases, tasks = expand(panel, config, config_hashes)
        write_jsonl(results / "panel.jsonl", panel)
        write_jsonl(results / "cases.jsonl", cases)
        write_jsonl(root / "tasks" / f"{config['profile']}.jsonl", tasks)
        config["runtime_measurement"] = RUNTIME_MEASUREMENT
        atomic_json(
            results / "profile.json",
            {
                key: config[key]
                for key in (
                    "profile",
                    "opt_levels",
                    "seeds",
                    "backend_seed",
                    "run_timeout_s",
                    "element_timeout_s",
                    "runtime_measurement",
                )
            },
        )
        # Smoke selection is independent of a focused/full panel and scientific rows.
        smoke = [
            r
            for r in inventory
            if (r["suite"] == "device-summit" and r["input_name"] == "test_circSU2_89_transpile")
            or (
                r["suite"] == "abstract-qasmbench-small"
                and r["input_name"] == "qaoa_n6"
                and r["topology"] == "linear"
            )
        ]
        smoke_path = validation / "smoke-panel.json"
        atomic_json(smoke_path, smoke)
        logged(
            [baseline, root / "harness/tools/benchpress/panel.py", smoke_path],
            validation / "smoke-targets.log",
            root,
            quality_env(root, config, config["opt_levels"][0], 0),
        )
        smoke_config = dict(config, seeds=[0])
        _, smoke_tasks = expand(read_json(smoke_path), smoke_config, config_hashes, stage="smoke")
        import socket

        context = {
            "host": socket.gethostname(),
            "lsf_job_id": os.environ.get("LSB_JOBID"),
            "validation": True,
        }
        for task in smoke_tasks:
            if (
                run_element(root, smoke_config, task, 1, context, campaign=False)["status"]
                != "passed"
            ):
                raise ValueError("Single-call adapter smoke validation failed; see raw/smoke")
        analysis = environment(root / "env/analysis")
        logged(
            [analysis, "-m", "pip", "install", "matplotlib"], results / "analysis-install.log", root
        )
        freeze(analysis, root / "analysis-requirements.lock", root)
        config.update(
            platform=platform_info(),
            schema_version=1,
            built=utcnow(),
            panel_count=len(panel),
            task_count=len(tasks),
            invocation_count=len(tasks) * len(config["opt_levels"]) * 2,
            execution_mode="quality",
            thread_settings=THREADS,
            qiskit_ignore_user_settings=True,
            sabre_preset_defaults={
                "2": {"layout_trials": 20, "swap_trials": 20, "max_iterations": 2},
                "3": {"layout_trials": 20, "swap_trials": 20, "max_iterations": 4},
            },
            retry_selection="first attempt canonical; later attempts separate",
        )
        frozen = [
            results / "inventory.jsonl",
            results / "panel.jsonl",
            results / "cases.jsonl",
            results / "profile.json",
            patch,
            root / "tasks" / f"{config['profile']}.jsonl",
            root / "shared-requirements.lock",
            root / "build-requirements.lock",
            root / "analysis-requirements.lock",
            plugin_wheel,
            root / "build/toolchain.json",
        ]
        frozen += list((results / "configs").glob("*.conf"))
        frozen += [p for p in bp.rglob("*") if p.is_file() and ".git" not in p.parts]
        frozen += [p for p in (root / "harness/tools/benchpress").rglob("*") if p.is_file()]
        config["artifacts"] = {str(p.relative_to(root)): file_hash(p) for p in frozen}
        config["cache_identity"] = digest(
            {
                "sources": [config["baseline_ref"], config["candidate_ref"]],
                "platform": config["platform"],
                "build_lock": file_hash(root / "build-requirements.lock"),
                "runtime_lock": config["lock_hash"],
                "adapter": config["adapter_hash"],
                "toolchain": read_json(root / "build/toolchain.json"),
            }
        )
        atomic_json(results / "manifest.json", config)
        # Prevent incidental writes to the shared upstream tree and frozen configs.
        for path in frozen:
            path.chmod(path.stat().st_mode & ~0o222)
        atomic_json(
            ready,
            {
                "manifest_hash": file_hash(results / "manifest.json"),
                "cache_identity": config["cache_identity"],
                "validated": utcnow(),
            },
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", required=True, type=Path)
    prepare(parser.parse_args().results_root)
