"""Reconcile every scheduled invocation and report paired gate/depth quality only."""

import argparse
from collections import Counter, defaultdict
import csv
import io
import math
from pathlib import Path
import statistics

from schemas import (
    REVISIONS,
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
)

RUN_COLUMNS = "run_id profile stage panel_id case_id node_id suite topology optimization_level lsf_job_id array_index host attempt revision qiskit_sha config_hash target_hash seed status reason input_qubits output_qubits native_2q_gate gate_count_2q depth_2q raw_artifact_dir".split()
PAIR_COLUMNS = "profile stage panel_id case_id optimization_level seed pair_status baseline_gate_count_2q candidate_gate_count_2q gate_count_change gate_count_change_pct baseline_depth_2q candidate_depth_2q depth_change depth_change_pct".split()
METRICS = {"gate_count": "gate_count_2q", "depth": "depth_2q"}
LEVEL_COLUMNS = ["panel_id", "revision", "paired_seeds"] + [
    field
    for label, key in METRICS.items()
    for field in (f"opt2_{key}", f"opt3_{key}", label + "_change", label + "_change_pct")
]


def csv_file(path, rows, columns=None):
    columns = columns or sorted({key for row in rows for key in row})
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(path, stream.getvalue())


def delta(baseline, candidate):
    return candidate - baseline, 100 * (candidate / baseline - 1) if baseline else None


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
        for label, key in METRICS.items():
            row[f"baseline_{key}"] = baseline[key]
            row[f"candidate_{key}"] = candidate[key]
            change, pct = delta(baseline[key], candidate[key]) if complete else (None, None)
            row[label + "_change"] = change
            row[label + "_change_pct"] = pct
            row[label + "_zero_baseline"] = complete and baseline[key] == 0
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
        )
        for label, key in METRICS.items():
            for revision in REVISIONS:
                row[f"{revision}_{key}"] = (
                    statistics.median(p[f"{revision}_{key}"] for p in group) if complete else None
                )
            row[label + "_change"] = (
                statistics.median(p[label + "_change"] for p in group) if complete else None
            )
            percentages = [p[label + "_change_pct"] for p in group]
            row[label + "_change_pct"] = (
                statistics.median(percentages)
                if complete and all(p is not None for p in percentages)
                else None
            )
            row[label + "_zero_baseline"] = complete and any(
                p[label + "_zero_baseline"] for p in group
            )
        rows.append(row)
    return rows


def level_rows(runs, cases, seeds):
    complete = {
        (r["panel_id"], r["optimization_level"]) for r in cases if r["status"] == "complete"
    }
    by_key = {(r["panel_id"], r["optimization_level"], r["seed"], r["revision"]): r for r in runs}
    panels = sorted(
        {panel for panel, level in complete if (panel, 2) in complete and (panel, 3) in complete}
    )
    rows = []
    for panel in panels:
        for revision in REVISIONS:
            row = {"panel_id": panel, "revision": revision, "paired_seeds": len(seeds)}
            for label, key in METRICS.items():
                low = [by_key[(panel, 2, seed, revision)][key] for seed in seeds]
                high = [by_key[(panel, 3, seed, revision)][key] for seed in seeds]
                changes = [delta(a, b) for a, b in zip(low, high)]
                row[f"opt2_{key}"], row[f"opt3_{key}"] = statistics.median(low), statistics.median(
                    high
                )
                row[label + "_change"] = statistics.median(d for d, _ in changes)
                row[label + "_change_pct"] = (
                    statistics.median(p for _, p in changes)
                    if all(p is not None for _, p in changes)
                    else None
                )
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
        }
        for metric in METRICS:
            changes = [r[metric + "_change"] for r in valid]
            percentages = [
                r[metric + "_change_pct"] for r in valid if r[metric + "_change_pct"] is not None
            ]
            ratios = [1 + pct / 100 for pct in percentages if pct > -100]
            row.update(
                {
                    f"{metric}_wins": sum(v < 0 for v in changes),
                    f"{metric}_ties": sum(v == 0 for v in changes),
                    f"{metric}_losses": sum(v > 0 for v in changes),
                    f"{metric}_median_change": statistics.median(changes) if changes else None,
                    f"{metric}_geomean_ratio": (
                        math.exp(statistics.mean(math.log(r) for r in ratios)) if ratios else None
                    ),
                    f"{metric}_positive_ratio_cases": len(ratios),
                    f"{metric}_zero_baseline_cases": sum(
                        r[metric + "_zero_baseline"] for r in valid
                    ),
                }
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
            figure, axis = plt.subplots(figsize=(6, 5))
            x, y = [r[f"baseline_{key}"] for r in complete], [
                r[f"candidate_{key}"] for r in complete
            ]
            axis.scatter(x, y, alpha=0.7)
            limit = max(x + y + [1])
            axis.plot([0, limit], [0, limit], color="gray", linestyle="--")
            axis.set(
                xlabel=f"Baseline {metric}",
                ylabel=f"Candidate {metric}",
                title=f"Level {level}: median across seeds ({len(complete)} complete cases)",
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
        results = root / "results"
        tables = results / "tables"
        for name, rows, columns in (
            ("runs", runs, RUN_COLUMNS),
            ("retry_runs", retries, RUN_COLUMNS),
            ("paired_seeds", pairs, PAIR_COLUMNS),
            ("cases", cases, None),
            ("coverage", coverage, None),
            ("regressions", regressions, PAIR_COLUMNS + ["above_5_percent"]),
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
            "coverage": coverage,
            "groups": summaries,
            "retry_invocations": len(retries),
            "retry_selection": config["retry_selection"],
        }
        atomic_json(results / "summary.json", summary)
        lines = [
            "# Benchpress quality comparison",
            "",
            f"Profile `{config['profile']}`; optimization levels {config['opt_levels']}; seeds {config['seeds']}. One transpilation per invocation.",
            "",
            f"Baseline `{config['baseline_ref']}`; candidate `{config['candidate_ref']}`; harness `{config['harness_ref']}`.",
            "",
            f"Coverage: {summary['paired']}/{len(pairs)} paired seeds; {summary['complete_cases']}/{len(cases)} complete cases; {len(runs)} scheduled first-attempt invocations.",
            "",
            "Negative gate/depth changes are improvements. Case summaries require every configured seed. Zero baselines use absolute changes; geometric means include strictly positive ratios only.",
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
            f"{len(regressions)} paired seed rows have gate-count or depth regressions. See [all regressions](tables/regressions.csv), including absolute counts and increases above 5%.",
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
            "- [Quality plots](plots/)",
            "",
            "No compilation-runtime or hardware-speedup conclusions are made. Circuit widths and native entangling gates are recorded in the invocation table. Native gate/connectivity checks and small build-time semantic checks are not a proof of equivalence for the full panel.",
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
