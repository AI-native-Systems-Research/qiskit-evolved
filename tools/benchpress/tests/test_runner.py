"""External watchdog cleanup, fixed flags, and per-child directory separation."""

import os
import sys
import time
from types import SimpleNamespace

from run import pytest_argv, quality_env, supervised


def test_timeout_kills_child_process_group(tmp_path):
    program = tmp_path / "spawn.py"
    marker = tmp_path / "escaped"
    program.write_text(
        "import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', "
        + repr(
            f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()"
        )
        + "])\ntime.sleep(20)\n"
    )
    status, code = supervised(
        [sys.executable, str(program)],
        dict(os.environ),
        tmp_path,
        tmp_path / "out",
        tmp_path / "err",
        time.time() + 0.3,
        SimpleNamespace(cancelled=False),
    )
    assert status == "timeout"
    assert code is None
    time.sleep(2)
    assert not marker.exists()


def test_worker_environment_and_node_paths_do_not_expose_qiskit_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/untrusted/source")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--benchmark-enable")
    env = quality_env(tmp_path, {"backend_seed": 424242}, 3, 2)
    assert "/untrusted/source" not in env["PYTHONPATH"]
    assert "PYTEST_ADDOPTS" not in env
    assert env["BENCHPRESS_OPT_LEVEL"] == "3"
    assert env["BENCHPRESS_TRANSPILE_SEED"] == "2"
    assert env["RAYON_NUM_THREADS"] == "1"
    argv = pytest_argv(tmp_path, "candidate", "benchpress/test.py::test_name", tmp_path / "work")
    assert "--benchmark-disable" in argv
    assert "--timeout-skip-list=0" in argv
    assert argv[-1] == str(tmp_path / "src/benchpress/benchpress/test.py::test_name")


def test_counterpart_and_next_level_run_after_failure(tmp_path, monkeypatch):
    import run
    from schemas import read_json, state_dir

    calls = []

    def invoke(root, config, task, level, revision, attempt, context, deadline, termination):
        calls.append((level, revision))
        return {
            "optimization_level": level,
            "revision": revision,
            "status": "failed" if len(calls) == 1 else "passed",
            "reason": "",
        }

    monkeypatch.setattr(run, "run_invocation", invoke)
    task = {"task_id": "task", "stage": "quality", "array_index": 1}
    result = run.run_element(
        tmp_path,
        {"element_timeout_s": 1500, "opt_levels": [2, 3]},
        task,
        1,
        {"host": "node"},
        campaign=False,
    )
    assert calls == [(2, "baseline"), (2, "candidate"), (3, "baseline"), (3, "candidate")]
    assert result["status"] == "failed"
    assert len(read_json(state_dir(tmp_path) / "outcomes/quality-1-1.json")["children"]) == 4


def test_failed_build_resume_preserves_artifacts_and_restores_upstream(tmp_path):
    from prepare import preserve_failed_build

    for name in ("env", "results", "src/benchpress", "src/benchpress-pristine", "build"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/benchpress/file.py").write_text("patched")
    (tmp_path / "src/benchpress-pristine/file.py").write_text("original")
    (tmp_path / "results/failure.log").write_text("diagnostics")
    (tmp_path / "build/prepare.lock").touch()
    (tmp_path / "build/started.json").write_text("{}")
    preserve_failed_build(tmp_path)
    (preserved,) = (tmp_path / "failed-builds").iterdir()
    assert (preserved / "results/failure.log").read_text() == "diagnostics"
    assert (preserved / "benchpress/file.py").read_text() == "patched"
    assert (tmp_path / "src/benchpress/file.py").read_text() == "original"
    assert (tmp_path / "build/prepare.lock").exists()
    assert not (tmp_path / "env").exists()
