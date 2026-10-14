"""Opt-in integration against the pinned upstream checkout and installed Qiskit.

Set BENCHPRESS_TEST_SOURCE to a Git checkout containing the pinned Benchpress SHA.
This checks the adapter, not the Linux baseline/candidate wheel comparison.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from adapter import apply_adapter, write_configs
from panel import expand, select_panel
from prepare import archive, semantic_check
from schemas import BENCHPRESS, THREADS, atomic_json, read_json


@pytest.mark.skipif(
    not os.environ.get("BENCHPRESS_TEST_SOURCE"), reason="opt-in real upstream test"
)
def test_real_collection_and_circuits_at_both_levels(tmp_path):
    source = tmp_path / "upstream"
    archive(os.environ["BENCHPRESS_TEST_SOURCE"], BENCHPRESS, source)
    apply_adapter(source, tmp_path / "adapter.patch")
    write_configs(source, tmp_path / "configs", [2, 3])
    harness = Path(__file__).resolve().parents[1]
    env = dict(
        os.environ,
        **THREADS,
        PYTHONPATH=os.pathsep.join((str(harness), str(source))),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONDONTWRITEBYTECODE="1",
        QISKIT_IGNORE_USER_SETTINGS="TRUE",
        BENCHPRESS_QUALITY_ONLY="1",
        BENCHPRESS_SOURCE=str(source),
        BENCHPRESS_TRANSPILE_SEED="0",
        BENCHPRESS_BACKEND_SEED="424242",
    )
    env.pop("PYTEST_ADDOPTS", None)
    base = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "pytest_benchmark.plugin",
        "-p",
        "quality_plugin",
        "--benchmark-disable",
        "--timeout-skip-list=0",
        "--rootdir=" + str(source),
        "--confcutdir=" + str(source),
        "-o",
        "addopts=",
        "-q",
    ]
    inventories = []
    for level in (2, 3):
        work = tmp_path / f"collection-{level}"
        work.mkdir()
        child_env = dict(
            env,
            BENCHPRESS_OPT_LEVEL=str(level),
            BENCHPRESS_CONFIG=str(tmp_path / f"configs/opt-{level}.conf"),
            BENCHPRESS_COLLECTION_OUTPUT=str(work / "inventory.json"),
        )
        result = subprocess.run(
            base
            + [
                "--collect-only",
                str(source / "benchpress/qiskit_gym/device_transpile"),
                str(source / "benchpress/qiskit_gym/abstract_transpile"),
            ],
            cwd=work,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        inventories.append(read_json(work / "inventory.json"))
    assert inventories[0] == inventories[1]
    assert len(inventories[0]) == 1054
    selected = select_panel(inventories[0], "quality", panel_size=50)
    env.update(BENCHPRESS_OPT_LEVEL="2", BENCHPRESS_CONFIG=str(tmp_path / "configs/opt-2.conf"))
    # Freeze twice in separate processes: target fingerprints must not depend on hash order.
    for name in ("panel.json", "panel-repeat.json"):
        atomic_json(tmp_path / name, selected)
        subprocess.run(
            [sys.executable, str(harness / "panel.py"), str(tmp_path / name)],
            cwd=tmp_path,
            env=env,
            check=True,
            timeout=180,
        )
    selected = read_json(tmp_path / "panel.json")
    assert selected == read_json(tmp_path / "panel-repeat.json")
    cases, tasks = expand(
        selected,
        {"profile": "quality", "opt_levels": [2, 3], "seeds": [0, 1, 2]},
        {"2": "c2", "3": "c3"},
    )
    assert (len(selected), len(cases), len(tasks)) == (50, 100, 150)
    smoke = [
        r
        for r in selected
        if r["input_name"] == "test_circSU2_89_transpile"
        or (r["input_name"] == "qaoa_n6" and r["topology"] == "linear")
    ]
    for level in (2, 3):
        for row in smoke:
            work = tmp_path / f"{row['panel_id']}-{level}"
            work.mkdir()
            identity = {"optimization_level": level, "seed": 0, "target_hash": row["target_hash"]}
            child_env = dict(
                env,
                BENCHPRESS_OPT_LEVEL=str(level),
                BENCHPRESS_CONFIG=str(tmp_path / f"configs/opt-{level}.conf"),
                BENCHPRESS_IDENTITY=json.dumps(identity),
                BENCHPRESS_QUALITY_OUTPUT=str(work / "quality.json"),
            )
            result = subprocess.run(
                base + [str(source / row["node_id"])],
                cwd=work,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            quality = read_json(work / "quality.json")
            assert quality["call_count"] == 1
            assert quality["status"] == "passed"
            assert quality["runtime_s"] > 0
            assert quality["runtime_method"] == "perf_counter"
            assert quality["runtime_scope"] == "benchmark_callable"
            assert "qasm_load_time" not in quality["extra_info"]
    semantic_check(sys.executable, tmp_path / "semantic.log", tmp_path)
