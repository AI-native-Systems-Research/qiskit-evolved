"""The scientific denominator and pairing rules stay stable under failure and retry."""

import pytest
from compare import compare, paired_rows
from panel import expand
from schemas import (
    BASELINE,
    CANDIDATE,
    atomic_json,
    file_hash,
    invocation_identity,
    raw_dir,
    read_json,
    state_dir,
    write_jsonl,
)


def config():
    return {
        "run_id": "run",
        "profile": "quality",
        "opt_levels": [2, 3],
        "seeds": [0, 1, 2],
        "baseline_ref": BASELINE,
        "candidate_ref": CANDIDATE,
        "harness_ref": "a" * 40,
        "backend_seed": 424242,
        "adapter_hash": "adapter",
        "lock_hash": "lock",
        "builds": {r: {"wheel_hash": r, "extension_hash": r} for r in ("baseline", "candidate")},
        "retry_selection": "first attempt canonical; later attempts separate",
    }


def panel(count=50):
    return [
        {
            "panel_id": f"p-{i}",
            "node_id": f"test.py::test_{i}",
            "input_hash": f"input-{i}",
            "target_hash": "target",
            "suite": "synthetic",
            "topology": "linear",
            "input_qubits": 4,
        }
        for i in range(count)
    ]


def test_main_matrix_counts_and_order():
    cases, tasks = expand(panel(), config(), {"2": "config2", "3": "config3"})
    assert len(cases) == 100
    assert len(tasks) == 150
    assert len(tasks) * 2 * 2 == 600
    assert len(cases) * 3 == 300
    assert [t["array_index"] for t in tasks] == list(range(1, 151))
    assert len({t["task_id"] for t in tasks}) == 150
    assert tasks[0]["cases"]["2"]["case_id"] != tasks[0]["cases"]["3"]["case_id"]


def write_pass(root, cfg, task, level, revision, attempt=1, gates=10, depth=4):
    identity = invocation_identity(cfg, task, level, revision, attempt)
    path = raw_dir(root, task["stage"], task, level, revision, attempt)
    context = {
        "host": "node1",
        "lsf_job_id": f"123-{attempt}",
        "affinity": [0],
        "array_index": task["array_index"],
    }
    atomic_json(
        path / "run.json",
        dict(
            identity,
            status="passed",
            reason="",
            host="node1",
            lsf_job_id=context["lsf_job_id"],
            context=context,
            exit_code=0,
        ),
    )
    atomic_json(
        path / "quality.json",
        {
            "schema_version": 1,
            "execution_mode": "quality",
            "identity": identity,
            "node_id": task["node_id"],
            "call_count": 1,
            "status": "passed",
            "phases": {p: {"status": "passed"} for p in ("setup", "call", "teardown")},
            "observed": {
                "optimization_level": level,
                "seed": task["seed"],
                "target_hash": "target",
            },
            "extra_info": {
                "input_num_qubits": 4,
                "output_num_qubits": 4,
                "output_gate_count_2q": gates,
                "output_depth_2q": depth,
                "native_2q_gate": "cz",
                "output_circuit_operations": {"cz": gates},
            },
        },
    )


def test_reports_account_for_missing_zero_and_retry_without_mixing(tmp_path, monkeypatch):
    root = tmp_path / "run"
    cfg = config()
    _cases, tasks = expand(panel(2), cfg, {"2": "config2", "3": "config3"})
    atomic_json(root / "results/manifest.json", cfg)
    atomic_json(
        root / "build/ready.json", {"manifest_hash": file_hash(root / "results/manifest.json")}
    )
    write_jsonl(root / "tasks/quality.jsonl", tasks)
    for task in tasks:
        for level in (2, 3):
            for revision in ("baseline", "candidate"):
                if task["array_index"] == 1 and level == 2 and revision == "candidate":
                    continue
                gates = 0 if task["panel_id"] == "p-1" else 10 if revision == "baseline" else 8
                write_pass(root, cfg, task, level, revision, gates=gates)
    atomic_json(state_dir(root) / "attempt-2.json", {"attempt": 2, "indices": [1]})
    for level in (2, 3):
        for revision in ("baseline", "candidate"):
            write_pass(root, cfg, tasks[0], level, revision, attempt=2)
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    summary = compare(root)
    assert summary["invocations"] == 24
    assert summary["pairs"] == 12
    assert summary["paired"] == 11
    assert summary["cases"] == 4
    assert summary["complete_cases"] == 3
    assert summary["retry_invocations"] == 4
    assert (root / "results/plots/opt-2/gate_count.svg").exists()
    assert (root / "results/plots/levels/baseline.png").exists()
    assert "not_run" in (root / "results/tables/runs.csv").read_text()
    assert "runtime" not in (root / "results/tables/runs.csv").read_text()
    assert (root / "results/checksums.sha256").exists()


def test_forged_quality_identity_is_not_paired(tmp_path):
    from compare import read_run

    cfg = config()
    _, tasks = expand(panel(1), cfg, {"2": "config2", "3": "config3"})
    task = tasks[0]
    write_pass(tmp_path, cfg, task, 2, "candidate")
    path = raw_dir(tmp_path, "quality", task, 2, "candidate", 1) / "quality.json"
    quality = read_json(path)
    quality["identity"]["optimization_level"] = 3
    atomic_json(path, quality)
    row = read_run(tmp_path, cfg, task, 2, "candidate", 1)
    assert row["status"] == "invalid_artifact"
    assert row["gate_count_2q"] is None


def test_pairing_refuses_cross_host_allocation(tmp_path):
    from compare import read_run

    cfg = config()
    _, tasks = expand(panel(1), cfg, {"2": "c2", "3": "c3"})
    rows = []
    for revision in ("baseline", "candidate"):
        write_pass(tmp_path, cfg, tasks[0], 2, revision)
        rows.append(read_run(tmp_path, cfg, tasks[0], 2, revision, 1))
    rows[1]["context"]["affinity"] = [1]
    assert paired_rows(rows)[0]["pair_status"] == "identity_mismatch"


def test_zero_baseline_and_incomplete_cases():
    from compare import delta

    assert delta(0, 3) == (3, None)
    assert delta(0, 0) == (0, None)
    assert delta(10, 8) == pytest.approx((-2, -20))
