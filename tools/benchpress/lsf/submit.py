"""Submit one pinned quality comparison; all controller dependencies are standard library."""

import argparse
import getpass
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prepare import archive
from schemas import (
    BASELINE,
    CANDIDATE,
    atomic_json,
    atomic_text,
    command,
    file_hash,
    locked,
    read_json,
    read_jsonl,
    state_dir,
    utcnow,
)
from lsf.ledger import Ledger
from lsf.scheduler import JobSpec, Scheduler, TERMINAL, wall_seconds


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", type=Path)
    result.add_argument("--harness-ref", default="HEAD")
    result.add_argument("--baseline-ref", default=BASELINE)
    result.add_argument("--candidate-ref", default=CANDIDATE)
    result.add_argument("--results-root", required=True, type=Path)
    result.add_argument(
        "--profile", choices=("quality", "followup-quality", "full"), default="quality"
    )
    result.add_argument("--opt-levels", nargs="+", type=int)
    result.add_argument("--seeds", nargs="+", type=int)
    for arg in (
        "panel-size",
        "backend-seed",
        "max-parallel",
        "run-timeout-s",
        "element-timeout-s",
        "campaign-limit-min",
    ):
        result.add_argument("--" + arg, type=int)
    for arg in (
        "panel-file",
        "job-env-script",
        "scratch-root",
        "benchpress-source",
        "benchmark-source",
        "build-cache-root",
    ):
        result.add_argument("--" + arg, type=Path)
    for role in ("", "build-", "task-", "manager-", "report-"):
        result.add_argument("--" + role + "queue")
    result.add_argument("--project")
    result.add_argument("--lsf-memory-unit", choices=("GB", "MB"), default="GB")
    result.add_argument("--task-mem-gb", type=float, default=8)
    result.add_argument("--task-wall")
    result.add_argument("--build-slots", type=int, default=16)
    result.add_argument("--build-mem-gb", type=float, default=64)
    result.add_argument("--build-wall", default="6:00")
    result.add_argument("--manager-wall", default="24:00")
    result.add_argument("--report-wall", default="0:30")
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--resume", action="store_true")
    result.add_argument("--log-level", choices=("INFO", "DEBUG"), default="INFO")
    return result


def validate(args):
    config = vars(args).copy()
    if not args.repo:
        raise ValueError("--repo is required for a new campaign")
    if not args.results_root.is_absolute():
        raise ValueError("--results-root must be an absolute shared filesystem path")
    for key, value in list(config.items()):
        if isinstance(value, Path):
            config[key] = str(value.resolve())
    repo = Path(config["repo"])
    defaults = read_json(Path(__file__).resolve().parents[1] / "profiles" / f"{args.profile}.json")
    for key, value in defaults.items():
        if config.get(key) is None:
            config[key] = value
    levels, seeds = config["opt_levels"], config["seeds"]
    if not levels or len(set(levels)) != len(levels) or not set(levels) <= {2, 3}:
        raise ValueError("--opt-levels must be 2, 3, or 2 3 without duplicates")
    config["opt_levels"] = sorted(levels)
    if not seeds or len(set(seeds)) != len(seeds) or min(seeds) < 0:
        raise ValueError("Seeds must be nonnegative, nonempty, and distinct")
    if config["backend_seed"] < 0:
        raise ValueError("Backend seed must be nonnegative")
    for key in (
        "max_parallel",
        "run_timeout_s",
        "element_timeout_s",
        "campaign_limit_min",
        "task_mem_gb",
        "build_slots",
        "build_mem_gb",
    ):
        if config[key] is None or not math.isfinite(config[key]) or config[key] <= 0:
            raise ValueError(
                f"--{key.replace('_', '-')} must be positive (explicit for full campaign budget)"
            )
    if config["element_timeout_s"] < 2 * len(levels) * config["run_timeout_s"] + 60:
        raise ValueError(
            "Element timeout must allow all children plus at least 60 seconds for cleanup"
        )
    if wall_seconds(config["task_wall"]) < config["element_timeout_s"] + 60:
        raise ValueError("Task wall limit needs at least 60 seconds beyond the element deadline")
    for key in ("build_wall", "manager_wall", "report_wall"):
        if wall_seconds(config[key]) <= 0:
            raise ValueError(f"Invalid {key}")
    if config["profile"] == "followup-quality" and not config["panel_file"]:
        raise ValueError("followup-quality requires --panel-file with selection reasons")
    if config["panel_file"]:
        rows = read_jsonl(config["panel_file"])
        if not rows or any(not r.get("node_id") or not r.get("selection_reason") for r in rows):
            raise ValueError("Panel rows require node_id and selection_reason")
        if config["panel_size"] is not None and len(rows) != config["panel_size"]:
            raise ValueError("--panel-size does not match --panel-file")
        config["panel_size"] = len(rows)
    elif config["profile"] == "quality" and config["panel_size"] != 50:
        raise ValueError(
            "Quality uses the fixed 50-entry panel; explicit subsets require --panel-file"
        )
    import re

    for key in ("baseline_ref", "candidate_ref"):
        if not re.fullmatch(r"[0-9a-f]{40}", config[key]):
            raise ValueError("Algorithm pins must be full commit hashes")
    for key in ("baseline_ref", "candidate_ref", "harness_ref"):
        config[key] = command(["git", "-C", repo, "rev-parse", config[key] + "^{commit}"])
    if (
        command(["git", "-C", repo, "rev-parse", config["candidate_ref"] + "^"])
        != config["baseline_ref"]
    ):
        raise ValueError("Baseline must be the candidate algorithm commit's parent")
    harness_path = "tools/benchpress"
    tracked = command(
        ["git", "-C", repo, "ls-tree", "-r", "--name-only", config["harness_ref"], harness_path]
    )
    if f"{harness_path}/lsf/submit.py" not in tracked.splitlines():
        raise ValueError(
            "Harness commit lacks implementation; commit tools/benchpress on this branch first"
        )
    dirty = command(["git", "-C", repo, "status", "--porcelain", "--", harness_path])
    differs = command(["git", "-C", repo, "diff", config["harness_ref"], "--", harness_path])
    if dirty or differs:
        raise ValueError(
            "Executed harness/profile files differ from --harness-ref; commit them before submission"
        )
    if args.results_root.exists() or state_dir(args.results_root).exists():
        raise ValueError(
            "Results root and its .lsf sibling must both be new; use --resume for recovery"
        )
    for key in ("job_env_script", "panel_file"):
        if config[key] and not Path(config[key]).is_file():
            raise ValueError(f"Missing {key}")
    config.update(
        run_id=uuid.uuid4().hex[:16],
        created=utcnow(),
        owner=getpass.getuser(),
        controller_python=sys.executable,
        schema_version=1,
    )
    return config


def job_spec(config, role, name, script, indices=None):
    directory = state_dir(config["results_root"])
    worker = role == "quality"
    slots = config["build_slots"] if role == "build" else 1
    memory = config["build_mem_gb"] if role == "build" else config["task_mem_gb"] if worker else 4
    wall = config["task_wall"] if worker else config[role + "_wall"]
    queue = config.get(("task" if worker else role) + "_queue") or config["queue"]
    if indices:
        name += f"[1-{len(indices)}]%{config['max_parallel']}"
    return JobSpec(
        name=name,
        command=["/bin/bash", str(script)],
        slots=slots,
        memory=memory,
        wall=wall,
        stdout=str(directory / "logs/jobs" / f"{role}.%J.%I.out"),
        stderr=str(directory / "logs/jobs" / f"{role}.%J.%I.err"),
        queue=queue,
        project=config["project"],
        exclusive=worker,
        unit=config["lsf_memory_unit"],
        per_task=config.get("cluster", {}).get("reserve_per_task", False),
    )


def wrapper(config, role, epoch, chunk=None):
    import shlex

    root = Path(config["results_root"])
    directory = state_dir(root)
    script = directory / "scripts" / f"{role}-{epoch}-{chunk or 0}.sh"
    python = config["controller_python"]
    entry = (
        root / "harness/tools/benchpress/lsf" / ("manager.py" if role == "manager" else "job.py")
    )
    argv = [python, str(entry), "--results-root", str(root), "--epoch", str(epoch)]
    if role != "manager":
        argv += ["--role", role]
    if chunk is not None:
        argv += ["--chunk", str(chunk)]
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        "export PYTHONDONTWRITEBYTECODE=1",
        "unset PYTHONPATH PYTHONHOME PYTEST_ADDOPTS",
    ]
    if config.get("job_env_script"):
        lines.append("source " + shlex.quote(config["job_env_script"]))
    lines += ["cd " + shlex.quote(str(root)), "exec " + shlex.join(argv)]
    atomic_text(script, "\n".join(lines) + "\n")
    return script


def resume(root, dry_run):
    directory = state_dir(root)
    config = read_json(directory / "launch.json")
    if config["owner"] != getpass.getuser():
        raise ValueError("Only the session owner may resume")
    scheduler = Scheduler(directory)
    ledger = Ledger(directory)
    with locked(directory / "manager.lock", blocking=False):
        statuses = ledger.reconcile(scheduler)
        if any(row["state"] not in TERMINAL for rows in statuses.values() for row in rows):
            raise ValueError("Previous jobs are active or unresolved; use status/stop/reap first")
        epoch = max([r["attempt"] for r in ledger.read()["intents"]] or [0]) + 1
        if dry_run:
            return {"resume_epoch": epoch, "results_root": str(root)}
        (directory / "stop.json").unlink(missing_ok=True)
        (directory / "complete.json").unlink(missing_ok=True)
        (directory / "attention.json").unlink(missing_ok=True)
        script = wrapper(config, "manager", epoch)
        return ledger.submit(
            f"manager-{epoch}",
            "manager",
            epoch,
            [],
            lambda name: job_spec(config, "manager", name, script),
            scheduler,
        )


def main(argv=None):
    args = parser().parse_args(argv)
    if args.resume:
        # Recovery uses archived configuration; reject silently ignored overrides.
        options = sys.argv[1:] if argv is None else argv
        allowed = {"--resume", "--results-root", "--dry-run"}
        if any(arg.startswith("--") and arg.split("=")[0] not in allowed for arg in options):
            raise ValueError(
                "Resume only accepts --results-root and --dry-run; settings are immutable"
            )
        print(json.dumps(resume(args.results_root.resolve(), args.dry_run), indent=2))
        return
    config = validate(args)
    root = Path(config["results_root"])
    directory = state_dir(root)
    if args.dry_run:
        spec = job_spec(
            config, "manager", "bp-PREVIEW-manager", directory / "scripts/manager-1-0.sh"
        )
        panel_count = config["panel_size"]
        print(
            json.dumps(
                {
                    "configuration": config,
                    "initial_bsub": spec.argv(),
                    "array_elements": (
                        panel_count * len(config["seeds"])
                        if panel_count
                        else "resolved at collection"
                    ),
                    "invocations": (
                        panel_count * len(config["seeds"]) * len(config["opt_levels"]) * 2
                        if panel_count
                        else "resolved at collection"
                    ),
                    "cluster_preflight": "Memory units, affinity and array limits checked on Linux submission/workers",
                },
                indent=2,
            )
        )
        return
    if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
        raise ValueError("Submit on the Linux cluster with Python 3.12")
    for name in ("bsub", "bjobs", "bhist", "bkill", "bparams", "git"):
        if not shutil.which(name):
            raise ValueError(f"Missing cluster command {name}")
    with tempfile.TemporaryDirectory(prefix="bp-preflight-") as temp:
        scheduler = Scheduler(temp)
        config["cluster"] = scheduler.cluster_config(config["lsf_memory_unit"])
        scheduler.query(name="bp-probe-" + uuid.uuid4().hex, owner=config["owner"])
        root.mkdir(parents=True, exist_ok=False)
        directory.mkdir(exist_ok=False)
        for name in ("logs/jobs", "scripts", "outcomes", "diagnostics"):
            (directory / name).mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(temp) / "diagnostics", directory / "diagnostics", dirs_exist_ok=True)
    archive(config["repo"], config["harness_ref"], root / "harness", ["tools/benchpress"])
    for revision in ("baseline", "candidate"):
        archive(config["repo"], config[f"{revision}_ref"], root / "src" / revision)
    from schemas import BENCHMARK, BENCHPRESS

    for option, name, pin in (
        ("benchpress_source", "benchpress", BENCHPRESS),
        ("benchmark_source", "pytest-benchmark", BENCHMARK),
    ):
        if config[option]:
            archive(config[option], pin, root / "src" / name)
            atomic_json(root / "src" / name / ".benchpress-source-pin.json", {"sha": pin})
    for option, name in (("panel_file", "selected-panel.jsonl"), ("job_env_script", "job-env.sh")):
        if config[option]:
            target = directory / name
            shutil.copyfile(config[option], target)
            config[option] = str(target)
            config[option + "_hash"] = file_hash(target)
            target.chmod(0o444)
    atomic_json(directory / "launch.json", config)
    script = wrapper(config, "manager", 1)
    intent = Ledger(directory).submit(
        "manager-1",
        "manager",
        1,
        [],
        lambda name: job_spec(config, "manager", name, script),
        Scheduler(directory),
    )
    print(
        json.dumps(
            {
                "manager_job_id": intent["job_id"],
                "results_root": str(root),
                "report": str(root / "results/report.md"),
                "scheduler_state": str(directory),
            },
            indent=2,
        )
    )
    import shlex

    control = [config["controller_python"], str(root / "harness/tools/benchpress/lsf/control.py")]
    for action in ("status", "stop"):
        print(shlex.join(control + [action, "--results-root", str(root)]))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as exc:
        sys.exit(str(exc))
