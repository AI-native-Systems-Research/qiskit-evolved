"""Entry point for archived build, array-element, and report jobs."""

import argparse
import os
from pathlib import Path
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schemas import atomic_json, clean_env, manifest, read_json, read_jsonl, state_dir, utcnow
from lsf.context import allocation, platform_info


def main(args):
    root = args.results_root.resolve()
    directory = state_dir(root)
    config = read_json(directory / "launch.json")
    if not os.environ.get("LSB_JOBID", "").isdigit():
        raise ValueError("This entry point must run in an LSF allocation")
    job_id = os.environ["LSB_JOBID"]
    # Prove this process belongs to a recorded owned submission, not merely an LSF job.
    ledger = read_json(directory / "ledger.json")
    role = args.role
    intent = next(
        (
            r
            for r in ledger["intents"]
            if r.get("job_id") == job_id and r["role"] == role and r["attempt"] == args.epoch
        ),
        None,
    )
    if intent is None:
        # Very fast dispatch can precede bsub's response; reconcile the durable name.
        from lsf.ledger import Ledger
        from lsf.scheduler import Scheduler

        Ledger(directory).reconcile(Scheduler(directory))
        ledger = read_json(directory / "ledger.json")
        intent = next(
            (
                r
                for r in ledger["intents"]
                if r.get("job_id") == job_id and r["role"] == role and r["attempt"] == args.epoch
            ),
            None,
        )
    if intent is None:
        raise ValueError("LSF job is not registered to this session/attempt/role")
    if role == "build":
        context = allocation(platform_info(), exclusive=False)
        if context["slots"] != config["build_slots"]:
            raise ValueError("Build allocation differs from requested slots")
        atomic_json(directory / "diagnostics" / f"build-context-{args.epoch}.json", context)
        from prepare import prepare

        prepare(root)
    elif role == "quality":
        chunk = read_json(directory / f"chunk-{args.epoch}-{args.chunk}.json")
        frozen = manifest(root, chunk["manifest_hash"])
        index = int(os.environ.get("LSB_JOBINDEX", "0"))
        if index < 1 or index > len(chunk["indices"]) or index not in intent["indices"]:
            raise ValueError("Invalid one-based LSB_JOBINDEX")
        if "affinity[core(1,exclusive=(core,alljobs))]" not in " ".join(intent["argv"]):
            raise ValueError("Worker lacks the exclusive-core reservation")
        tasks = read_jsonl(root / "tasks" / f"{config['profile']}.jsonl")
        task = tasks[chunk["indices"][index - 1] - 1]
        context = allocation(frozen["platform"])
        from run import run_element

        result = run_element(root, frozen, task, args.epoch, context, epoch=args.epoch)
        if result["status"] != "passed":
            return 1
    else:
        python = root / "env/analysis/bin/python"
        from prepare import logged

        env = clean_env()
        env["MPLCONFIGDIR"] = str(directory / "matplotlib")
        logged(
            [python, root / "harness/tools/benchpress/compare.py", "--results-root", root],
            directory / "logs" / f"report-{args.epoch}.log",
            root,
            env,
        )
        from lsf.report import operational_report

        operational_report(root)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--role", choices=("build", "quality", "report"), required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--chunk", type=int)
    args = parser.parse_args()
    try:
        code = main(args)
    except Exception as exc:
        atomic_json(
            state_dir(args.results_root)
            / "outcomes"
            / f"error-{args.role}-{os.environ.get('LSB_JOBID', 'unknown')}-{os.environ.get('LSB_JOBINDEX', '0')}.json",
            {
                "status": "precondition_failure",
                "reason": str(exc),
                "at": utcnow(),
                "traceback": traceback.format_exc(),
            },
        )
        raise
    sys.exit(code)
