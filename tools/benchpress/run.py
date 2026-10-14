"""Isolated sequential revision/level children with process-group watchdogs."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from schemas import (
    REVISIONS,
    atomic_json,
    clean_env,
    file_hash,
    invocation_identity,
    locked,
    raw_dir,
    read_json,
    state_dir,
    utcnow,
)


class Termination:
    def __init__(self):
        self.cancelled = False
        self.previous = {}

    def __enter__(self):
        def handler(signum, frame):
            self.cancelled = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            self.previous[sig] = signal.signal(sig, handler)
        return self

    def __exit__(self, *args):
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def supervised(argv, env, cwd, stdout, stderr, deadline, termination):
    with open(stdout, "w", encoding="utf-8") as out, open(stderr, "w", encoding="utf-8") as err:
        process = subprocess.Popen(
            argv, env=env, cwd=cwd, stdout=out, stderr=err, start_new_session=True
        )
        try:
            while True:
                if termination.cancelled:
                    return "cancelled", None
                remaining = deadline - time.time()
                if remaining <= 0:
                    return "timeout", None
                try:
                    code = process.wait(timeout=min(1, remaining))
                    return ("completed" if code == 0 else "failed"), code
                except subprocess.TimeoutExpired:
                    continue
        finally:
            kill_group(process)


def quality_env(root, config, level, seed):
    root = Path(root)
    env = clean_env()
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                (str(root / "harness/tools/benchpress"), str(root / "src/benchpress"))
            ),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "BENCHPRESS_QUALITY_ONLY": "1",
            "BENCHPRESS_SOURCE": str(root / "src/benchpress"),
            "BENCHPRESS_CONFIG": str(root / "results/configs" / f"opt-{level}.conf"),
            "BENCHPRESS_OPT_LEVEL": str(level),
            "BENCHPRESS_TRANSPILE_SEED": str(seed),
            "BENCHPRESS_BACKEND_SEED": str(config["backend_seed"]),
        }
    )
    return env


def pytest_argv(root, revision, selection, work):
    root, work = Path(root), Path(work)
    return [
        str(root / "env" / revision / "bin/python"),
        "-m",
        "pytest",
        "-p",
        "pytest_benchmark.plugin",
        "-p",
        "quality_plugin",
        "--benchmark-disable",
        "--timeout-skip-list=0",
        "--rootdir=" + str(root / "src/benchpress"),
        "--confcutdir=" + str(root / "src/benchpress"),
        "-o",
        "addopts=",
        "-o",
        "cache_dir=" + str(work / "cache"),
        "--basetemp=" + str(work / "pytest-tmp"),
        str(root / "src/benchpress" / selection),
    ]


def campaign_deadline(root, config, epoch):
    marker = state_dir(root) / f"epoch-{epoch}-started.json"
    with locked(state_dir(root) / "epoch.lock"):
        if not marker.exists():
            atomic_json(
                marker,
                {"started": utcnow(), "deadline": time.time() + config["campaign_limit_min"] * 60},
            )
        return read_json(marker)["deadline"]


def run_invocation(root, config, task, level, revision, attempt, context, deadline, termination):
    directory = raw_dir(root, task["stage"], task, level, revision, attempt)
    directory.mkdir(parents=True, exist_ok=False)
    identity = invocation_identity(config, task, level, revision, attempt)
    run = dict(
        identity,
        context=context,
        host=context["host"],
        lsf_job_id=context.get("lsf_job_id"),
        status="running",
        reason="",
        started=utcnow(),
        raw_artifact_dir=str(directory),
        execution_mode="quality",
    )
    atomic_json(directory / "run.json", run)
    work = directory / "work"
    work.mkdir()
    scratch = None
    try:
        if termination.cancelled or time.time() >= deadline:
            run.update(status="not_run", reason="element/campaign deadline or cancellation")
            return run
        for key in ("wheel_path", "extension_path"):
            path = config["builds"][revision][key]
            checksum = config["builds"][revision][key.replace("_path", "_hash")]
            if file_hash(path) != checksum:
                raise ValueError(f"Built artifact changed: {path}")
        scratch_root = config.get("scratch_root") or os.environ.get("TMPDIR")
        scratch = tempfile.mkdtemp(prefix="bp-", dir=scratch_root)
        env = quality_env(root, config, level, task["seed"])
        env.update(
            TMPDIR=scratch,
            BENCHPRESS_QUALITY_OUTPUT=str(directory / "quality.json"),
            BENCHPRESS_IDENTITY=json.dumps(identity, sort_keys=True),
        )
        argv = pytest_argv(root, revision, task["node_id"], work)
        argv += ["--junitxml=" + str(directory / "junit.xml"), "-q"]
        run.update(
            command=argv,
            environment={
                k: v
                for k, v in env.items()
                if k.startswith(
                    (
                        "BENCHPRESS",
                        "QISKIT",
                        "PYTHON",
                        "PYTEST",
                        "RAYON",
                        "OMP",
                        "MKL",
                        "OPENBLAS",
                        "TMP",
                    )
                )
            },
        )
        atomic_json(directory / "run.json", run)
        status, code = supervised(
            argv,
            env,
            work,
            directory / "stdout.log",
            directory / "stderr.log",
            min(deadline, time.time() + config["run_timeout_s"]),
            termination,
        )
        run.update(status=status, exit_code=code)
        if status == "completed":
            if not (directory / "quality.json").is_file():
                run.update(status="missing_artifact", reason="pytest exited without quality.json")
                return run
            quality = read_json(directory / "quality.json")
            if quality["identity"] != identity or quality["node_id"] != task["node_id"]:
                raise ValueError("Quality identity mismatch")
            run["status"] = quality["status"]
            if run["status"] not in ("passed", "skipped", "failed"):
                raise ValueError("Incomplete quality export")
            if run["status"] != "passed":
                run["reason"] = json.dumps(quality["phases"])
        elif status == "failed":
            run["reason"] = f"pytest exited with status {code}; see stderr/JUnit"
        else:
            run["reason"] = f"whole-child {status}"
    except Exception as exc:
        run.update(status="precondition_failure", reason=str(exc))
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
        run["finished"] = utcnow()
        atomic_json(directory / "run.json", run)
    return run


def run_element(root, config, task, attempt, context, epoch=1, campaign=True):
    deadline = time.time() + config["element_timeout_s"]
    if campaign:
        deadline = min(deadline, campaign_deadline(root, config, epoch))
    outcomes = []
    with Termination() as termination:
        for level in config["opt_levels"]:
            for revision in REVISIONS:
                outcomes.append(
                    run_invocation(
                        root, config, task, level, revision, attempt, context, deadline, termination
                    )
                )
    result = {
        "task_id": task["task_id"],
        "stage": task["stage"],
        "attempt": attempt,
        "array_index": task["array_index"],
        "context": context,
        "finished": utcnow(),
        "status": "passed" if all(r["status"] == "passed" for r in outcomes) else "failed",
        "children": [
            {k: r[k] for k in ("optimization_level", "revision", "status", "reason")}
            for r in outcomes
        ],
    }
    atomic_json(
        state_dir(root) / "outcomes" / f"{task['stage']}-{task['array_index']}-{attempt}.json",
        result,
    )
    return result
