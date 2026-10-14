"""Exercise the actual pinned benchmark fixture in isolated concurrent processes."""

import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

HARNESS = str(Path(__file__).resolve().parents[1])


def child(tmp_path, mode="pass", level=2, flags=()):
    work = tmp_path / f"work-{mode}-{level}"
    work.mkdir()
    test = work / "test_synthetic.py"
    test.write_text("""import os
from pathlib import Path
import pytest
from bp_runtime import OBSERVED

@pytest.fixture
def setup_guard():
    if os.environ['MODE'] == 'setup_fail':
        raise ValueError('setup failed')
    yield
    if os.environ['MODE'] == 'teardown_fail':
        raise ValueError('teardown failed')

def test_quality(benchmark, setup_guard):
    if os.environ['MODE'] == 'skip':
        pytest.skip('unsupported input')
    assert not Path('skipfile.txt').exists()
    OBSERVED.update(optimization_level=int(os.environ['LEVEL']), seed=0, target_hash='target')
    @benchmark
    def result():
        with Path('calls.txt').open('a') as stream:
            stream.write('call\\n')
        return 7
    Path('skipfile.txt').write_text('private state')
    benchmark.extra_info.update(output_gate_count_2q=result, output_depth_2q=0,
        output_num_qubits=4, native_2q_gate='cz', qasm_load_time=999)
    if os.environ['MODE'] == 'call_fail':
        assert False, 'validation failed'
""")
    identity = {"optimization_level": level, "seed": 0, "target_hash": "target"}
    env = dict(
        os.environ,
        PYTHONPATH=HARNESS,
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        BENCHPRESS_QUALITY_ONLY="1",
        BENCHPRESS_IDENTITY=json.dumps(identity),
        BENCHPRESS_QUALITY_OUTPUT=str(work / "quality.json"),
        LEVEL=str(level),
        MODE=mode,
    )
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "pytest_benchmark.plugin",
            "-p",
            "quality_plugin",
            "--benchmark-disable",
            "--timeout-skip-list=0",
            "-o",
            "addopts=",
            "--rootdir",
            str(work),
            "--confcutdir",
            str(work),
            "-q",
            *flags,
            str(test),
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return work, result


def test_actual_fixture_single_call_and_concurrent_isolation(tmp_path):
    with concurrent.futures.ThreadPoolExecutor() as pool:
        futures = [pool.submit(child, tmp_path, level=level) for level in (2, 3)]
        results = [f.result() for f in futures]
    for work, result in results:
        assert result.returncode == 0, result.stdout + result.stderr
        quality = json.loads((work / "quality.json").read_text())
        assert quality["status"] == "passed"
        assert quality["call_count"] == 1
        assert (work / "calls.txt").read_text() == "call\n"
        assert quality["extra_info"]["output_depth_2q"] == 0
        assert "qasm_load_time" not in quality["extra_info"]
        assert set(quality["phases"]) == {"setup", "call", "teardown"}


@pytest.mark.parametrize(
    "mode,status,calls",
    [
        ("setup_fail", "failed", 0),
        ("call_fail", "failed", 1),
        ("teardown_fail", "failed", 1),
        ("skip", "skipped", 0),
    ],
)
def test_failed_and_skipped_tests_export_quality(tmp_path, mode, status, calls):
    work, result = child(tmp_path, mode)
    quality = json.loads((work / "quality.json").read_text())
    assert quality["status"] == status, result.stdout + result.stderr
    assert quality["call_count"] == calls


@pytest.mark.parametrize(
    "flags",
    [("--timeout-skip-list=2",), ("--benchmark-enable",), ("--benchmark-cprofile=cumtime",)],
)
def test_timing_overrides_rejected(tmp_path, flags):
    work, result = child(tmp_path, flags=flags)
    assert result.returncode != 0
    assert not (work / "calls.txt").exists()
