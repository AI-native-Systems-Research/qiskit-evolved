"""Reconcile every scheduled invocation and report paired quality and callable runtime."""

import argparse
from collections import Counter, defaultdict
import csv
import io
import math
from pathlib import Path
import statistics

from schemas import (
    REVISIONS,
    RUNTIME_MEASUREMENT,
    RUNTIME_METHOD,
    RUNTIME_SCOPE,
    atomic_json,
    atomic_text,
    digest,
    file_hash,
    invocation_identity,
    locked,
    manifest,
    raw_dir,
    read_json,
    read_jsonl,
    state_dir,
    valid_runtime,
)

RUN_COLUMNS = "run_id profile stage panel_id case_id node_id suite topology optimization_level lsf_job_id array_index host attempt revision qiskit_sha config_hash target_hash seed status reason input_qubits output_qubits native_2q_gate gate_count_2q depth_2q runtime_s runtime_method runtime_scope runtime_status runtime_reason raw_artifact_dir".split()
PAIR_COLUMNS = "profile stage panel_id case_id optimization_level seed pair_status baseline_gate_count_2q candidate_gate_count_2q gate_count_change gate_count_change_pct baseline_depth_2q candidate_depth_2q depth_change depth_change_pct baseline_runtime_s candidate_runtime_s runtime_change_s runtime_ratio runtime_change_pct runtime_status".split()
METRICS = {"gate_count": "gate_count_2q", "depth": "depth_2q", "runtime": "runtime_s"}


def change_column(metric):
    return "runtime_change_s" if metric == "runtime" else metric + "_change"


def metric_complete(row, metric):
    return row["runtime_status" if metric == "runtime" else "status"] == "complete"


LEVEL_COLUMNS = (
    ["panel_id", "revision", "paired_seeds"]
    + [
        field
        for label, key in METRICS.items()
        for field in (f"opt2_{key}", f"opt3_{key}", change_column(label), label + "_change_pct")
    ]
    + ["runtime_ratio", "runtime_status", "runtime_paired_seeds"]
)
RUNTIME_REGRESSION_COLUMNS = "panel_id case_id optimization_level suite topology runtime_status runtime_paired_seeds baseline_runtime_s candidate_runtime_s runtime_change_s runtime_ratio runtime_change_pct gate_count_change depth_change above_5_percent".split()


def csv_file(path, rows, columns=None):
    columns = columns or sorted({key for row in rows for key in row})
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(path, stream.getvalue())


def delta(baseline, candidate):
    return candidate - baseline, 100 * (candidate / baseline - 1) if baseline else None


def runtime_fields(quality):
    """Missing or invalid timing never invalidates otherwise usable quality data."""
    value = quality.get("runtime_s")
    row = {
        "runtime_s": None,
        "runtime_method": quality.get("runtime_method"),
        "runtime_scope": quality.get("runtime_scope"),
        "runtime_status": "missing",
        "runtime_reason": "Callable runtime was not recorded",
    }
    if value is None:
        return row
    if not valid_runtime(value):
        row.update(runtime_status="invalid", runtime_reason="Runtime must be finite and positive")
    elif row["runtime_method"] != RUNTIME_METHOD or row["runtime_scope"] != RUNTIME_SCOPE:
        row.update(runtime_status="invalid", runtime_reason="Unsupported runtime method or scope")
    else:
        row.update(runtime_s=value, runtime_status="available", runtime_reason="")
    return row


def missing_status(root, task, attempt):
    directory = state_dir(root)
    submitted = False
    for path in directory.glob(f"chunk-{attempt}-*.json"):
        members = read_json(path)["indices"]
        if task["array_index"] not in members:
            continue
        chunk = path.stem.rsplit("-", 1)[-1]
        key = f"quality-{attempt}-{chunk}"
        ledger = read_json(directory / "ledger.json")
        intent = next((r for r in ledger["intents"] if r["key"] == key), None)
        if not intent or intent["status"] == "rejected":
            continue
        submitted = True
        index = members.index(task["array_index"]) + 1
        error = directory / "outcomes" / f"error-quality-{intent.get('job_id')}-{index}.json"
        if error.exists():
            return "precondition_failure", read_json(error)["reason"]
        status_path = directory / "outcomes" / f"scheduler-{key}.json"
        if status_path.exists():
            job_id = f"{intent['job_id']}[{index}]"
            row = next((r for r in read_json(status_path) if r["job_id"] == job_id), {})
            reason = row.get("reason") or ""
            if "TERM_MEMLIMIT" in reason:
                return "oom", reason
            if "TERM_RUNLIMIT" in reason:
                return "timeout", reason
            if row.get("state") == "EXIT":
                return (
                    "cancelled" if (directory / "stop.json").exists() else "terminated"
                ), reason or "LSF element exited before final artifact"
    return (
        ("missing_artifact", "submitted element has no final artifact")
        if submitted
        else ("not_run", "element was not submitted")
    )


def read_run(root, config, task, level, revision, attempt):
    expected = invocation_identity(config, task, level, revision, attempt)
    directory = raw_dir(root, task["stage"], task, level, revision, attempt)
    row = dict(
        expected,
        raw_artifact_dir=str(directory),
        gate_count_2q=None,
        depth_2q=None,
        runtime_s=None,
        runtime_method=None,
        runtime_scope=None,
        runtime_status="not_applicable",
        runtime_reason="Invocation did not produce a validated passing result",
        input_qubits=task["input_qubits"],
        output_qubits=None,
        native_2q_gate=None,
        host=None,
        lsf_job_id=None,
        context={},
    )
    path = directory / "run.json"
    if not path.exists():
        row["status"], row["reason"] = missing_status(root, task, attempt)
        return row
    try:
        run = read_json(path)
        for key, value in expected.items():
            if run.get(key) != value:
                raise ValueError(f"Run identity mismatch: {key}")
        row.update({k: run.get(k) for k in ("status", "reason", "host", "lsf_job_id", "context")})
        if row["status"] == "running":
            row["status"], row["reason"] = missing_status(root, task, attempt)
        if row["status"] != "passed":
            return row
        quality = read_json(directory / "quality.json")
        if quality.get("identity") != expected or quality.get("node_id") != task["node_id"]:
            raise ValueError("Quality identity mismatch")
        if (
            quality.get("status") != "passed"
            or quality.get("execution_mode") != "quality"
            or quality.get("call_count") != 1
        ):
            raise ValueError("Quality export is not a validated single-call result")
        if set(quality["phases"]) != {"setup", "call", "teardown"} or any(
            p["status"] != "passed" for p in quality["phases"].values()
        ):
            raise ValueError("Incomplete validation phases")
        for key in ("optimization_level", "seed", "target_hash"):
            if quality["observed"].get(key) != expected[key]:
                raise ValueError(f"Observed {key} mismatch")
        info = quality["extra_info"]
        for source, dest in (
            ("output_gate_count_2q", "gate_count_2q"),
            ("output_depth_2q", "depth_2q"),
            ("output_num_qubits", "output_qubits"),
        ):
            value = info.get(source)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Invalid quality metric {source}")
            row[dest] = value
        row["native_2q_gate"] = info.get("native_2q_gate")
        if row["native_2q_gate"] not in ("cx", "cz", "ecr"):
            raise ValueError("Missing native two-qubit gate identity")
        if info.get("input_num_qubits", row["input_qubits"]) != row["input_qubits"]:
            raise ValueError("Input width mismatch")
        row["output_circuit_operations"] = info.get("output_circuit_operations")
        row.update(runtime_fields(quality))
    except (ValueError, KeyError, OSError, TypeError) as exc:
        row.update(status="invalid_artifact", reason=str(exc), gate_count_2q=None, depth_2q=None)
    return row


def paired_rows(runs):
    groups = defaultdict(dict)
    allocations = defaultdict(set)
    for row in runs:
        groups[(row["case_id"], row["seed"], row["attempt"])][row["revision"]] = row
        if row["status"] == "passed":
            allocations[(row["task_id"], row["attempt"])].add(digest(row["context"]))
    pairs = []
    for group in groups.values():
        baseline, candidate = group["baseline"], group["candidate"]
        row = {
            k: baseline[k]
            for k in (
                "profile",
                "stage",
                "panel_id",
                "case_id",
                "optimization_level",
                "seed",
                "suite",
                "topology",
                "attempt",
            )
        }
        complete = baseline["status"] == candidate["status"] == "passed"
        if complete:
            keys = (
                "host",
                "lsf_job_id",
                "input_hash",
                "target_hash",
                "config_hash",
                "attempt",
                "native_2q_gate",
            )
            complete = all(
                baseline.get(k) is not None and baseline.get(k) == candidate.get(k) for k in keys
            )
            complete = complete and baseline["context"] == candidate["context"]
            complete = (
                complete and len(allocations[(baseline["task_id"], baseline["attempt"])]) == 1
            )
            row["pair_status"] = "paired" if complete else "identity_mismatch"
        else:
            row["pair_status"] = f"{baseline['status']}/{candidate['status']}"
        runtime_statuses = {baseline["runtime_status"], candidate["runtime_status"]}
        if not complete:
            row["runtime_status"] = "ineligible"
        elif runtime_statuses == {"available"}:
            row["runtime_status"] = "paired"
        else:
            row["runtime_status"] = "invalid" if "invalid" in runtime_statuses else "missing"
        for label, key in METRICS.items():
            eligible = complete and (label != "runtime" or row["runtime_status"] == "paired")
            row[f"baseline_{key}"] = baseline[key]
            row[f"candidate_{key}"] = candidate[key]
            change, pct = delta(baseline[key], candidate[key]) if eligible else (None, None)
            row[change_column(label)] = change
            row[label + "_change_pct"] = pct
            row[label + "_zero_baseline"] = eligible and baseline[key] == 0
        row["runtime_ratio"] = (
            candidate["runtime_s"] / baseline["runtime_s"]
            if row["runtime_status"] == "paired"
            else None
        )
        pairs.append(row)
    return pairs


def case_rows(pairs, seeds):
    groups = defaultdict(list)
    for pair in pairs:
        groups[pair["case_id"]].append(pair)
    rows = []
    for group in groups.values():
        first = group[0]
        row = {
            k: first[k] for k in ("panel_id", "case_id", "optimization_level", "suite", "topology")
        }
        complete = {p["seed"] for p in group if p["pair_status"] == "paired"} == set(seeds)
        row.update(
            status="complete" if complete else "incomplete",
            expected_seeds=len(seeds),
            paired_seeds=sum(p["pair_status"] == "paired" for p in group),
            runtime_status=(
                "complete"
                if complete and all(p["runtime_status"] == "paired" for p in group)
                else "incomplete"
            ),
            runtime_paired_seeds=sum(p["runtime_status"] == "paired" for p in group),
        )
        for label, key in METRICS.items():
            eligible = metric_complete(row, label)
            for revision in REVISIONS:
                row[f"{revision}_{key}"] = (
                    statistics.median(p[f"{revision}_{key}"] for p in group) if eligible else None
                )
            row[change_column(label)] = (
                statistics.median(p[change_column(label)] for p in group) if eligible else None
            )
            percentages = [p[label + "_change_pct"] for p in group]
            row[label + "_change_pct"] = (
                statistics.median(percentages)
                if eligible and all(p is not None for p in percentages)
                else None
            )
            row[label + "_zero_baseline"] = eligible and any(
                p[label + "_zero_baseline"] for p in group
            )
        row["runtime_ratio"] = (
            statistics.median(p["runtime_ratio"] for p in group)
            if row["runtime_status"] == "complete"
            else None
        )
        rows.append(row)
    return rows


def level_rows(runs, cases, seeds):
    complete = {
        (r["panel_id"], r["optimization_level"]) for r in cases if r["status"] == "complete"
    }
    runtime_complete = {
        (r["panel_id"], r["optimization_level"]) for r in cases if r["runtime_status"] == "complete"
    }
    by_key = {(r["panel_id"], r["optimization_level"], r["seed"], r["revision"]): r for r in runs}
    panels = sorted(
        {panel for panel, level in complete if (panel, 2) in complete and (panel, 3) in complete}
    )
    rows = []
    for panel in panels:
        for revision in REVISIONS:
            row = {"panel_id": panel, "revision": revision, "paired_seeds": len(seeds)}
            runtime_seeds = [
                seed
                for seed in seeds
                if all(
                    by_key[(panel, level, seed, revision)]["runtime_status"] == "available"
                    for level in (2, 3)
                )
            ]
            row.update(
                runtime_status=(
                    "complete"
                    if all((panel, level) in runtime_complete for level in (2, 3))
                    else "incomplete"
                ),
                runtime_paired_seeds=len(runtime_seeds),
                runtime_ratio=None,
            )
            for label, key in METRICS.items():
                if label == "runtime" and row["runtime_status"] != "complete":
                    row.update(
                        opt2_runtime_s=None,
                        opt3_runtime_s=None,
                        runtime_change_s=None,
                        runtime_change_pct=None,
                    )
                    continue
                low = [by_key[(panel, 2, seed, revision)][key] for seed in seeds]
                high = [by_key[(panel, 3, seed, revision)][key] for seed in seeds]
                changes = [delta(a, b) for a, b in zip(low, high)]
                row[f"opt2_{key}"], row[f"opt3_{key}"] = (
                    statistics.median(low),
                    statistics.median(high),
                )
                row[change_column(label)] = statistics.median(d for d, _ in changes)
                row[label + "_change_pct"] = (
                    statistics.median(p for _, p in changes)
                    if all(p is not None for _, p in changes)
                    else None
                )
                if label == "runtime":
                    row["runtime_ratio"] = statistics.median(b / a for a, b in zip(low, high))
            rows.append(row)
    return rows


def summarize(cases):
    groups = defaultdict(list)
    for case in cases:
        for dimension, value in (
            ("all", "all"),
            ("suite", case["suite"]),
            ("topology", case["topology"]),
        ):
            groups[(case["optimization_level"], dimension, value)].append(case)
    rows = []
    for (level, dimension, value), group in sorted(groups.items()):
        valid = [r for r in group if r["status"] == "complete"]
        row = {
            "optimization_level": level,
            "dimension": dimension,
            "group": value,
            "cases": len(group),
            "complete": len(valid),
            "runtime_complete": sum(r["runtime_status"] == "complete" for r in group),
            "runtime_incomplete": sum(r["runtime_status"] != "complete" for r in group),
        }
        for metric in METRICS:
            metric_valid = [r for r in valid if metric_complete(r, metric)]
            changes = [r[change_column(metric)] for r in metric_valid]
            percentages = [
                r[metric + "_change_pct"]
                for r in metric_valid
                if r[metric + "_change_pct"] is not None
            ]
            ratios = (
                [r["runtime_ratio"] for r in metric_valid]
                if metric == "runtime"
                else [1 + pct / 100 for pct in percentages if pct > -100]
            )
            median_column = (
                "runtime_median_change_s" if metric == "runtime" else f"{metric}_median_change"
            )
            row.update(
                {
                    f"{metric}_wins": sum(v < 0 for v in changes),
                    f"{metric}_ties": sum(v == 0 for v in changes),
                    f"{metric}_losses": sum(v > 0 for v in changes),
                    median_column: statistics.median(changes) if changes else None,
                    f"{metric}_geomean_ratio": (
                        math.exp(statistics.mean(math.log(r) for r in ratios)) if ratios else None
                    ),
                    f"{metric}_positive_ratio_cases": len(ratios),
                    f"{metric}_zero_baseline_cases": sum(
                        r[metric + "_zero_baseline"] for r in metric_valid
                    ),
                }
            )
            if metric == "runtime":
                row["runtime_median_change_pct"] = (
                    statistics.median(percentages) if percentages else None
                )
        rows.append(row)
    return rows


def plots(root, cases, levels):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def save(figure, directory, name):
        directory.mkdir(parents=True, exist_ok=True)
        for suffix in ("svg", "png"):
            figure.savefig(directory / f"{name}.{suffix}", bbox_inches="tight")
        plt.close(figure)

    for level in sorted({r["optimization_level"] for r in cases}):
        complete = [
            r for r in cases if r["optimization_level"] == level and r["status"] == "complete"
        ]
        for metric, key in METRICS.items():
            eligible = [r for r in complete if metric_complete(r, metric)]
            if metric == "runtime" and not eligible:
                continue
            figure, axis = plt.subplots(figsize=(6, 5))
            x, y = (
                [r[f"baseline_{key}"] for r in eligible],
                [r[f"candidate_{key}"] for r in eligible],
            )
            axis.scatter(x, y, alpha=0.7)
            limit = max(x + y + [1])
            axis.plot([0, limit], [0, limit], color="gray", linestyle="--")
            axis.set(
                xlabel=f"Baseline {metric}" + (" (s)" if metric == "runtime" else ""),
                ylabel=f"Candidate {metric}" + (" (s)" if metric == "runtime" else ""),
                title=f"Level {level}: median across seeds ({len(eligible)} complete cases)",
            )
            save(figure, root / f"opt-{level}", metric)
        figure, axis = plt.subplots(figsize=(8, 4))
        groups = defaultdict(list)
        for row in complete:
            if row["gate_count_change_pct"] is not None:
                groups[row["topology"]].append(row["gate_count_change_pct"])
        if groups:
            axis.boxplot(list(groups.values()), tick_labels=list(groups))
        axis.axhline(0, color="gray", linestyle="--")
        axis.set(ylabel="Native 2Q gate change (%)", title=f"Level {level}: quality by topology")
        save(figure, root / f"opt-{level}", "topology")
        groups = defaultdict(list)
        for row in complete:
            if row["runtime_status"] == "complete":
                groups[row["topology"]].append(row["runtime_change_pct"])
        if groups:
            figure, axis = plt.subplots(figsize=(8, 4))
            axis.boxplot(list(groups.values()), tick_labels=list(groups))
            axis.axhline(0, color="gray", linestyle="--")
            axis.set(
                ylabel="Callable runtime change (%)", title=f"Level {level}: runtime by topology"
            )
            save(figure, root / f"opt-{level}", "runtime_topology")
    for revision in REVISIONS:
        group = [r for r in levels if r["revision"] == revision]
        figure, axis = plt.subplots(figsize=(6, 5))
        x, y = [r["opt2_gate_count_2q"] for r in group], [r["opt3_gate_count_2q"] for r in group]
        axis.scatter(x, y)
        limit = max(x + y + [1])
        axis.plot([0, limit], [0, limit], color="gray", linestyle="--")
        axis.set(
            xlabel="Level 2 native 2Q gates",
            ylabel="Level 3 native 2Q gates",
            title=f"{revision}: common complete panel",
        )
        save(figure, root / "levels", revision)
        timed = [r for r in group if r["runtime_status"] == "complete"]
        if timed:
            figure, axis = plt.subplots(figsize=(6, 5))
            x = [r["opt2_runtime_s"] for r in timed]
            y = [r["opt3_runtime_s"] for r in timed]
            axis.scatter(x, y)
            limit = max(x + y + [1])
            axis.plot([0, limit], [0, limit], color="gray", linestyle="--")
            axis.set(
                xlabel="Level 2 callable runtime (s)",
                ylabel="Level 3 callable runtime (s)",
                title=f"{revision}: common complete runtime panel ({len(timed)} cases)",
            )
            save(figure, root / "levels", revision + "_runtime")


def ensure_settled(root):
    directory = state_dir(root)
    ledger = directory / "ledger.json"
    if not ledger.exists():
        return
    for intent in read_json(ledger)["intents"]:
        if intent["role"] != "quality" or intent["status"] == "rejected":
            continue
        evidence = directory / "outcomes" / f"scheduler-{intent['key']}.json"
        if not evidence.exists():
            raise ValueError("Cannot collect while quality jobs have unresolved scheduler states")
        rows = read_json(evidence)
        expected = {f"{intent['job_id']}[{i}]" for i in intent["indices"]}
        if {r.get("job_id") for r in rows} != expected or any(
            r.get("state") not in ("DONE", "EXIT") for r in rows
        ):
            raise ValueError("Cannot collect before every submitted element is terminal")


def reconcile_artifacts(root, rows):
    """Commit synthesized outcomes without overwriting evidence from interrupted children."""
    elements = defaultdict(list)
    for row in rows:
        directory = Path(row["raw_artifact_dir"])
        path = directory / "run.json"
        if not path.exists():
            atomic_json(path, dict(row, synthesized=True))
        else:
            try:
                original = read_json(path)
            except (ValueError, OSError):
                original = {}
            if original.get("status") == "running":
                atomic_json(directory / "run.interrupted.json", original)
                atomic_json(path, dict(row, synthesized=True))
        elements[(row["stage"], row["array_index"], row["attempt"])].append(row)
    for (stage, index, attempt), children in elements.items():
        atomic_json(
            state_dir(root) / "outcomes/reconciled" / f"{stage}-{index}-{attempt}.json",
            {
                "stage": stage,
                "array_index": index,
                "attempt": attempt,
                "status": (
                    "passed" if all(r["status"] == "passed" for r in children) else "incomplete"
                ),
                "children": [
                    {k: row[k] for k in ("revision", "optimization_level", "status", "reason")}
                    for row in children
                ],
            },
        )


def compare(root):
    root = Path(root).resolve()
    with locked(state_dir(root) / "report.lock", blocking=False):
        ensure_settled(root)
        config = manifest(root, read_json(root / "build/ready.json")["manifest_hash"])
        tasks = read_jsonl(root / "tasks" / f"{config['profile']}.jsonl")
        runs = [
            read_run(root, config, task, level, revision, 1)
            for task in tasks
            for level in config["opt_levels"]
            for revision in REVISIONS
        ]
        retries = []
        for path in sorted(state_dir(root).glob("attempt-*.json")):
            attempt = read_json(path)
            if attempt["attempt"] == 1:
                continue
            for index in attempt["indices"]:
                for level in config["opt_levels"]:
                    for revision in REVISIONS:
                        retries.append(
                            read_run(
                                root, config, tasks[index - 1], level, revision, attempt["attempt"]
                            )
                        )
        reconcile_artifacts(root, runs + retries)
        pairs = paired_rows(runs)
        cases = case_rows(pairs, config["seeds"])
        levels = level_rows(runs, cases, config["seeds"])
        summaries = summarize(cases)
        coverage_counts = Counter(
            (r["optimization_level"], r["revision"], r["suite"], r["status"]) for r in runs
        )
        coverage = [
            {
                "optimization_level": key[0],
                "revision": key[1],
                "suite": key[2],
                "status": key[3],
                "count": count,
            }
            for key, count in sorted(coverage_counts.items())
        ]
        regressions = [
            r
            for r in pairs
            if r["pair_status"] == "paired"
            and (r["gate_count_change"] > 0 or r["depth_change"] > 0)
        ]
        for row in regressions:
            row["above_5_percent"] = any(
                (row[k] or 0) > 5 for k in ("gate_count_change_pct", "depth_change_pct")
            )
        runtime_regressions = sorted(
            (
                dict(r, above_5_percent=r["runtime_change_pct"] > 5)
                for r in cases
                if r["runtime_status"] == "complete" and r["runtime_change_s"] > 0
            ),
            key=lambda r: r["runtime_change_s"],
            reverse=True,
        )
        results = root / "results"
        tables = results / "tables"
        for name, rows, columns in (
            ("runs", runs, RUN_COLUMNS),
            ("retry_runs", retries, RUN_COLUMNS),
            ("paired_seeds", pairs, PAIR_COLUMNS),
            ("cases", cases, None),
            ("coverage", coverage, None),
            ("regressions", regressions, PAIR_COLUMNS + ["above_5_percent"]),
            ("runtime_regressions", runtime_regressions, RUNTIME_REGRESSION_COLUMNS),
            ("levels", levels, LEVEL_COLUMNS),
            ("groups", summaries, None),
        ):
            csv_file(tables / f"{name}.csv", rows, columns)
        summary = {
            "schema_version": 1,
            "execution_mode": "quality",
            "run_id": config["run_id"],
            "baseline": config["baseline_ref"],
            "candidate": config["candidate_ref"],
            "harness": config["harness_ref"],
            "levels": config["opt_levels"],
            "seeds": config["seeds"],
            "invocations": len(runs),
            "pairs": len(pairs),
            "paired": sum(p["pair_status"] == "paired" for p in pairs),
            "cases": len(cases),
            "complete_cases": sum(c["status"] == "complete" for c in cases),
            "runtime_measurement": RUNTIME_MEASUREMENT,
            "runtime_invocations": sum(r["runtime_status"] == "available" for r in runs),
            "runtime_paired": sum(p["runtime_status"] == "paired" for p in pairs),
            "runtime_complete_cases": sum(c["runtime_status"] == "complete" for c in cases),
            "runtime_coverage": dict(Counter(r["runtime_status"] for r in runs)),
            "coverage": coverage,
            "groups": summaries,
            "retry_invocations": len(retries),
            "retry_selection": config["retry_selection"],
        }
        atomic_json(results / "summary.json", summary)
        lines = [
            "# Benchpress quality and runtime comparison",
            "",
            f"Profile `{config['profile']}`; optimization levels {config['opt_levels']}; seeds {config['seeds']}. One transpilation per invocation.",
            "",
            f"Baseline `{config['baseline_ref']}`; candidate `{config['candidate_ref']}`; harness `{config['harness_ref']}`.",
            "",
            f"Coverage: {summary['paired']}/{len(pairs)} paired seeds; {summary['complete_cases']}/{len(cases)} complete cases; {len(runs)} scheduled first-attempt invocations.",
            "",
            f"Runtime coverage: {summary['runtime_paired']}/{len(pairs)} paired seeds; {summary['runtime_complete_cases']}/{len(cases)} complete cases; {summary['runtime_invocations']}/{len(runs)} measured passing invocations.",
            "",
            "Negative gate/depth changes are improvements. Case summaries require every configured seed. Zero baselines use absolute changes; geometric means include strictly positive ratios only.",
            "",
            "Runtime is wall-clock seconds around the single benchmark callable (`pm.run(circuit)`), measured with `perf_counter`. It excludes loading, pass-manager construction, pytest startup, and validation. Changes are candidate minus baseline; ratios are candidate/baseline, so negative changes and ratios below one mean faster. Case changes and ratios are medians of paired per-seed observations.",
            "",
            "These are single-call observations without warmup or calibration. Fixed baseline-first ordering, host load, and clock/cache effects can influence them; seed variation is not a repeated timing experiment. Missing or invalid timings remain unavailable, and failed invocations are excluded from runtime comparisons. Historical runtimes are not inferred from process or scheduler durations.",
            "",
            "| Level | Metric | Wins / ties / losses | Geometric mean ratio | Ratio cases |",
            "| ---: | --- | --- | ---: | ---: |",
        ]
        for row in summaries:
            if row["dimension"] != "all":
                continue
            for metric in METRICS:
                ratio = row[metric + "_geomean_ratio"]
                lines.append(
                    f"| {row['optimization_level']} | {metric} | {row[metric + '_wins']} / {row[metric + '_ties']} / {row[metric + '_losses']} | {ratio if ratio is not None else 'unavailable'} | {row[metric + '_positive_ratio_cases']} |"
                )
        lines += [
            "",
            "Runtime differences across complete cases:",
            "",
            "| Level | Runtime-complete cases | Median change (s) | Median change (%) |",
            "| ---: | ---: | ---: | ---: |",
        ]
        for row in summaries:
            if row["dimension"] != "all":
                continue
            seconds = row["runtime_median_change_s"]
            percent = row["runtime_median_change_pct"]
            seconds_text = f"{seconds:.6g}" if seconds is not None else "unavailable"
            percent_text = f"{percent:.6g}" if percent is not None else "unavailable"
            lines.append(
                f"| {row['optimization_level']} | {row['runtime_complete']}/{row['cases']} | {seconds_text} | {percent_text} |"
            )
        largest = sorted(
            (row for row in cases if row["status"] == "complete"),
            key=lambda row: max(abs(row["gate_count_change"]), abs(row["depth_change"])),
            reverse=True,
        )[:10]
        lines += [
            "",
            "Largest absolute case changes (medians across paired seeds):",
            "",
            "| Case | Level | 2Q gate change | 2Q depth change |",
            "| --- | ---: | ---: | ---: |",
        ]
        for row in largest:
            lines.append(
                f"| `{row['case_id']}` | {row['optimization_level']} | {row['gate_count_change']} | {row['depth_change']} |"
            )
        lines += [
            "",
            "Largest runtime slowdowns (complete cases; medians of paired seed changes):",
            "",
            "| Case | Level | Baseline (s) | Candidate (s) | Change (s) | Change (%) | Ratio | 2Q gate change | 2Q depth change |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for row in runtime_regressions[:10]:
            lines.append(
                f"| `{row['case_id']}` | {row['optimization_level']} | {row['baseline_runtime_s']:.6g} | {row['candidate_runtime_s']:.6g} | {row['runtime_change_s']:.6g} | {row['runtime_change_pct']:.6g} | {row['runtime_ratio']:.6g} | {row['gate_count_change']} | {row['depth_change']} |"
            )
        if not summary["runtime_complete_cases"]:
            lines += [
                "",
                "Runtime comparisons are unavailable: no cases have timing for every configured seed.",
            ]
        lines += [
            "",
            f"{len(regressions)} paired seed rows have gate-count or depth regressions. See [all regressions](tables/regressions.csv), including absolute counts and increases above 5%.",
            "",
            f"{len(runtime_regressions)} complete cases have runtime increases. See [runtime regressions](tables/runtime_regressions.csv), including seconds, percentages, and the corresponding quality changes.",
            "",
            "Gate/depth tradeoffs remain visible in the paired rows. All-to-all controls have their own topology group; no-routing attribution is not inferred from gate count.",
            "",
            "- [Coverage](tables/coverage.csv)",
            "- [Per-invocation outcomes](tables/runs.csv)",
            "- [Paired seeds](tables/paired_seeds.csv)",
            "- [Case summaries](tables/cases.csv)",
            "- [Suite and topology summaries](tables/groups.csv)",
            "- [Level 3 versus level 2](tables/levels.csv)",
            "- [Retry history (separate from the first attempt)](tables/retry_runs.csv)",
            "- [Raw artifacts](raw/)",
            "- [Manifest](manifest.json)",
            "- [Quality and runtime plots](plots/)",
            "",
            "Compilation runtime and circuit quality do not establish hardware execution speedups. Circuit widths and native entangling gates are recorded in the invocation table. Native gate/connectivity checks and small build-time semantic checks are not a proof of equivalence for the full panel.",
        ]
        atomic_text(results / "report.md", "\n".join(lines) + "\n")
        plots(results / "plots", cases, levels)
        files = sorted(
            p for p in results.rglob("*") if p.is_file() and p.name != "checksums.sha256"
        )
        atomic_text(
            results / "checksums.sha256",
            "".join(f"{file_hash(p)}  {p.relative_to(results)}\n" for p in files),
        )
        return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", required=True, type=Path)
    compare(parser.parse_args().results_root)
