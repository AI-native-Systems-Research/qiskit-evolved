"""The scientific denominator and pairing rules stay stable under failure and retry."""

import csv
import json

import pytest
from compare import case_rows, compare, level_rows, paired_rows, read_run, summarize
from panel import expand
from schemas import (
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
        "baseline_ref": "b" * 40,
        "candidate_ref": "c" * 40,
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


def write_pass(root, cfg, task, level, revision, attempt=1, gates=10, depth=4, runtime=1.0):
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
            **(
                {
                    "runtime_s": runtime,
                    "runtime_method": "perf_counter",
                    "runtime_scope": "benchmark_callable",
                }
                if runtime is not None
                else {}
            ),
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


@pytest.mark.parametrize("with_requested_refs", [False, True])
def test_reports_account_for_missing_zero_and_retry_without_mixing(
    tmp_path, monkeypatch, with_requested_refs
):
    root = tmp_path / "run"
    cfg = config()
    if with_requested_refs:
        cfg["requested_refs"] = {
            "baseline": "main",
            "candidate": "sabre-gate-fusion-aware-scoring",
            "harness": "HEAD",
        }
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
                write_pass(
                    root,
                    cfg,
                    task,
                    level,
                    revision,
                    gates=gates,
                    runtime=2.0 if revision == "baseline" else 3.0,
                )
    atomic_json(state_dir(root) / "attempt-2.json", {"attempt": 2, "indices": [1]})
    for level in (2, 3):
        for revision in ("baseline", "candidate"):
            write_pass(root, cfg, tasks[0], level, revision, attempt=2)
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    summary = compare(root)
    assert summary["requested_refs"] == cfg.get("requested_refs", {})
    report = (root / "results/report.md").read_text()
    for role in ("baseline", "candidate", "harness"):
        assert cfg[f"{role}_ref"] in report
        if with_requested_refs:
            assert f"`{cfg['requested_refs'][role]}` at `{cfg[f'{role}_ref']}`" in report
    assert summary["invocations"] == 24
    assert summary["pairs"] == 12
    assert summary["paired"] == 11
    assert summary["cases"] == 4
    assert summary["complete_cases"] == 3
    assert summary["retry_invocations"] == 4
    assert summary["runtime_invocations"] == 23
    assert summary["runtime_paired"] == 11
    assert summary["runtime_complete_cases"] == 3
    all_group = next(
        r for r in summary["groups"] if r["optimization_level"] == 3 and r["dimension"] == "all"
    )
    assert all_group["runtime_complete"] == 2
    assert all_group["runtime_median_change_s"] == 1.0
    assert all_group["runtime_median_change_pct"] == 50.0
    assert all_group["runtime_geomean_ratio"] == pytest.approx(1.5)
    assert (root / "results/plots/opt-2/gate_count.svg").exists()
    assert (root / "results/plots/levels/baseline.png").exists()
    assert "not_run" in (root / "results/tables/runs.csv").read_text()
    assert (root / "results/plots/opt-2/runtime.svg").exists()
    assert (root / "results/plots/opt-3/runtime_topology.png").exists()
    assert (root / "results/plots/levels/baseline_runtime.svg").exists()
    assert "runtime_s" in (root / "results/tables/runs.csv").read_text()
    with (root / "results/tables/runtime_regressions.csv").open() as stream:
        regressions = list(csv.DictReader(stream))
    assert len(regressions) == 3
    assert all(float(r["runtime_change_s"]) == 1.0 for r in regressions)
    assert all(r["above_5_percent"] == "True" for r in regressions)
    assert (
        "Runtime coverage: 11/12 paired seeds; 3/4 complete cases"
        in (root / "results/report.md").read_text()
    )
    assert "| 3 | 2/2 | 1 | 50 |" in (root / "results/report.md").read_text()
    from lsf.report import operational_report

    operational_report(root)
    assert (
        "Runtime coverage: 11/12 paired seeds; 3/4 complete cases"
        in (state_dir(root) / "report.md").read_text()
    )
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
    pair = paired_rows(rows)[0]
    assert pair["pair_status"] == "identity_mismatch"
    assert pair["runtime_status"] == "ineligible"
    assert pair["runtime_ratio"] is None


def test_zero_baseline_and_incomplete_cases():
    from compare import delta

    assert delta(0, 3) == (3, None)
    assert delta(0, 0) == (0, None)
    assert delta(10, 8) == pytest.approx((-2, -20))


def test_runtime_aggregates_paired_changes_before_case_medians(tmp_path):
    cfg = config()
    _, tasks = expand(panel(1), cfg, {"2": "c2", "3": "c3"})
    timings = {"baseline": [1.0, 10.0, 100.0], "candidate": [2.0, 100.0, 90.0]}
    runs = []
    for task in tasks:
        for level in (2, 3):
            for revision in ("baseline", "candidate"):
                write_pass(
                    tmp_path,
                    cfg,
                    task,
                    level,
                    revision,
                    runtime=timings[revision][task["seed"]] * (level - 1),
                )
                runs.append(read_run(tmp_path, cfg, task, level, revision, 1))
    pairs = paired_rows(runs)
    assert pairs[0]["runtime_ratio"] == 2.0
    assert pairs[0]["runtime_change_s"] == 1.0
    assert pairs[0]["runtime_change_pct"] == 100.0
    assert pairs[-1]["runtime_change_s"] == -20.0
    cases = case_rows(pairs, cfg["seeds"])
    low = next(r for r in cases if r["optimization_level"] == 2)
    assert low["baseline_runtime_s"] == 10.0
    assert low["candidate_runtime_s"] == 90.0
    assert low["runtime_change_s"] == 1.0  # Not the difference of medians (80 s).
    assert low["runtime_ratio"] == 2.0  # Not the ratio of medians (9).
    assert low["runtime_change_pct"] == 100.0
    summary = next(r for r in summarize(cases) if r["optimization_level"] == 2)
    assert summary["runtime_geomean_ratio"] == 2.0
    assert summary["runtime_losses"] == 1
    levels = level_rows(runs, cases, cfg["seeds"])
    for row in levels:
        assert row["runtime_status"] == "complete"
        assert row["runtime_ratio"] == 2.0
        assert row["runtime_change_s"] == (10.0 if row["revision"] == "baseline" else 90.0)


@pytest.mark.parametrize(
    "value,status",
    [
        (None, "missing"),
        (0, "invalid"),
        (-1, "invalid"),
        (True, "invalid"),
        ("1.2", "invalid"),
        (float("nan"), "invalid"),
        (float("inf"), "invalid"),
    ],
)
def test_unusable_runtime_preserves_quality(tmp_path, value, status):
    cfg = config()
    _, tasks = expand(panel(1), cfg, {"2": "c2", "3": "c3"})
    rows = []
    for revision in ("baseline", "candidate"):
        write_pass(tmp_path, cfg, tasks[0], 2, revision)
        if revision == "candidate":
            path = raw_dir(tmp_path, "quality", tasks[0], 2, revision, 1) / "quality.json"
            quality = read_json(path)
            quality["runtime_s"] = value
            path.write_text(json.dumps(quality))
        rows.append(read_run(tmp_path, cfg, tasks[0], 2, revision, 1))
    assert rows[1]["status"] == "passed"
    assert rows[1]["gate_count_2q"] == 10
    assert rows[1]["runtime_status"] == status
    assert rows[1]["runtime_s"] is None
    pair = paired_rows(rows)[0]
    assert pair["pair_status"] == "paired"
    assert pair["runtime_status"] == status
    assert pair["runtime_change_s"] is None
    assert pair["runtime_ratio"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_method", "process_time"),
        ("runtime_scope", "whole_child"),
        ("runtime_method", None),
        ("runtime_scope", None),
    ],
)
def test_runtime_requires_supported_measurement_metadata(tmp_path, field, value):
    cfg = config()
    _, tasks = expand(panel(1), cfg, {"2": "c2", "3": "c3"})
    write_pass(tmp_path, cfg, tasks[0], 2, "baseline")
    path = raw_dir(tmp_path, "quality", tasks[0], 2, "baseline", 1) / "quality.json"
    quality = read_json(path)
    quality[field] = value
    atomic_json(path, quality)
    row = read_run(tmp_path, cfg, tasks[0], 2, "baseline", 1)
    assert row["status"] == "passed"
    assert row["runtime_status"] == "invalid"
    assert row["runtime_reason"]
    assert row["runtime_s"] is None


@pytest.mark.parametrize("timed", [False, True])
def test_untimed_or_partial_cases_keep_quality_and_omit_runtime_aggregates(
    tmp_path, monkeypatch, timed
):
    cfg = config()
    _, tasks = expand(panel(1), cfg, {"2": "c2", "3": "c3"})
    atomic_json(tmp_path / "results/manifest.json", cfg)
    checksum = file_hash(tmp_path / "results/manifest.json")
    atomic_json(tmp_path / "build/ready.json", {"manifest_hash": checksum})
    write_jsonl(tmp_path / "tasks/quality.jsonl", tasks)
    for task in tasks:
        for level in (2, 3):
            for revision in ("baseline", "candidate"):
                runtime = 2.0 if timed and task["seed"] != 2 else None
                write_pass(tmp_path, cfg, task, level, revision, runtime=runtime)
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    summary = compare(tmp_path)
    assert summary["complete_cases"] == 2
    assert summary["runtime_complete_cases"] == 0
    assert summary["runtime_paired"] == (4 if timed else 0)
    assert file_hash(tmp_path / "results/manifest.json") == checksum
    assert not (tmp_path / "results/plots/opt-2/runtime.svg").exists()
    assert not (tmp_path / "results/plots/levels/baseline_runtime.svg").exists()
    assert (tmp_path / "results/plots/opt-2/gate_count.svg").exists()
    with (tmp_path / "results/tables/cases.csv").open() as stream:
        cases = list(csv.DictReader(stream))
    assert all(r["status"] == "complete" and r["runtime_status"] == "incomplete" for r in cases)
    assert all(r["runtime_change_s"] == "" for r in cases)
    assert all(int(r["runtime_paired_seeds"]) == (2 if timed else 0) for r in cases)
    with (tmp_path / "results/tables/runtime_regressions.csv").open() as stream:
        assert not list(csv.DictReader(stream))
    assert all(r["runtime_geomean_ratio"] is None for r in summary["groups"])


@pytest.mark.parametrize("status", ["failed", "timeout", "skipped", "cancelled"])
def test_failed_invocations_never_contribute_runtime(tmp_path, status):
    cfg = config()
    _, tasks = expand(panel(1), cfg, {"2": "c2", "3": "c3"})
    write_pass(tmp_path, cfg, tasks[0], 2, "baseline")
    path = raw_dir(tmp_path, "quality", tasks[0], 2, "baseline", 1) / "run.json"
    run = read_json(path)
    run["status"] = status
    atomic_json(path, run)
    row = read_run(tmp_path, cfg, tasks[0], 2, "baseline", 1)
    assert row["status"] == status
    assert row["runtime_status"] == "not_applicable"
    assert row["runtime_s"] is None
