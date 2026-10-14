"""Structured LSF commands; absence or query failure is never proof of completion."""

import getpass
import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from schemas import atomic_json, read_json, utcnow

TERMINAL = {"DONE", "EXIT"}
ACTIVE = {"PEND", "RUN", "PSUSP", "USUSP", "SSUSP", "WAIT", "PROV"}
FIELDS = "jobid jobindex stat exit_code exit_reason exec_host pend_reason job_name user"


class Unresolved(RuntimeError):
    """Scheduler evidence is insufficient to proceed safely."""


def wall_seconds(value):
    match = re.fullmatch(r"(\d+)(?::([0-5]\d))?", value)
    if not match:
        raise ValueError(f"Invalid LSF wall time {value!r}; use H:MM")
    return 60 * int(match[1]) if match[2] is None else 3600 * int(match[1]) + 60 * int(match[2])


def resources(slots, mem_gb, unit="GB", exclusive=False, per_task=False):
    memory = mem_gb * (1024 if unit == "MB" else 1)
    if per_task:
        memory /= slots
    affinity = " affinity[core(1,exclusive=(core,alljobs))]" if exclusive else ""
    return f"span[hosts=1]{affinity} rusage[mem={memory:g}]"


@dataclass
class JobSpec:
    name: str
    command: list
    slots: int
    memory: float
    wall: str
    stdout: str
    stderr: str
    queue: str | None = None
    project: str | None = None
    exclusive: bool = False
    unit: str = "GB"
    per_task: bool = False

    def argv(self):
        result = [
            "bsub",
            "-J",
            self.name,
            "-n",
            str(self.slots),
            "-W",
            self.wall,
            "-R",
            resources(self.slots, self.memory, self.unit, self.exclusive, self.per_task),
            "-o",
            self.stdout,
            "-e",
            self.stderr,
        ]
        if self.queue:
            result += ["-q", self.queue]
        if self.project:
            result += ["-P", self.project]
        return result + self.command


def parse_bjobs(text):
    data = json.loads(text)
    if "RECORDS" not in data or not isinstance(data["RECORDS"], list):
        raise Unresolved("bjobs did not return a RECORDS list")
    rows = []
    for row in data["RECORDS"]:
        if row.get("ERROR"):
            if "not found" in row["ERROR"].lower() or "no unfinished job" in row["ERROR"].lower():
                continue
            raise Unresolved(row["ERROR"])
        job_id = str(row.get("JOBID", ""))
        index = str(row.get("JOB_INDEX", row.get("JOBINDEX", "0")))
        if index not in ("0", "", "None", "-") and "[" not in job_id:
            job_id += f"[{index}]"
        if not re.fullmatch(r"\d+(?:\[\d+\])?", job_id):
            raise Unresolved("Invalid scheduler job ID")
        rows.append(
            {
                "job_id": job_id,
                "state": row.get("STAT", "UNKWN"),
                "name": row.get("JOB_NAME"),
                "owner": row.get("USER"),
                "exit_code": row.get("EXIT_CODE"),
                "host": row.get("EXEC_HOST"),
                "reason": row.get("EXIT_REASON") or row.get("PEND_REASON", ""),
            }
        )
    return rows


def parse_history(job_id, text):
    text = re.sub(r"\n\s{10,}", "", text)
    id_match = re.search(r"Job <([^>]+)>", text)
    owner = re.search(r"User <([^>]+)>", text)
    name = re.search(r"Job Name <([^>]+)>", text)
    if not id_match or id_match[1] != job_id or not owner or not name:
        raise Unresolved(f"History lacks identity for {job_id}")
    state, reason = "UNKWN", "history has no terminal event"
    if "Done successfully" in text:
        state, reason = "DONE", ""
    elif re.search(r"Exited (with exit code|by (?:LSF )?signal)|Completed <exit>|TERM_", text):
        state, reason = "EXIT", text[-4000:]
    return {"job_id": job_id, "owner": owner[1], "name": name[1], "state": state, "reason": reason}


def verify_owner(row, intent):
    base = intent["name"].split("[")[0]
    if row.get("owner") != intent["owner"] or row.get("name", "").split("[")[0] != base:
        raise Unresolved(f"Job identity mismatch for {row.get('job_id')}")


class Scheduler:
    def __init__(self, directory, runner=None):
        self.directory = Path(directory)
        self.runner = runner or subprocess.run

    def execute(self, argv):
        try:
            result = self.runner(argv, text=True, capture_output=True, timeout=45, check=False)
            record = {
                "argv": argv,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "at": utcnow(),
            }
        except (subprocess.TimeoutExpired, OSError) as exc:
            record = {
                "argv": argv,
                "returncode": None,
                "stdout": "",
                "stderr": str(exc),
                "at": utcnow(),
            }
        atomic_json(self.directory / "diagnostics" / f"{uuid.uuid4().hex}.json", record)
        return record

    def submit(self, spec):
        result = self.execute(spec.argv())
        match = re.search(r"Job <(\d+)> is submitted", result["stdout"])
        if match:
            return dict(result, outcome="accepted", job_id=match[1])
        # A transport failure or unrecognized response may have submitted a job.
        rejected = result["returncode"] not in (0, None) and any(
            marker in result["stderr"].lower()
            for marker in (
                "job not submitted",
                "bad queue",
                "queue does not exist",
                "bad resource requirement",
            )
        )
        return dict(result, outcome="rejected" if rejected else "ambiguous", job_id=None)

    def query(self, ids=None, name=None, owner=None):
        argv = ["bjobs", "-a", "-json", "-o", FIELDS]
        if name:
            argv += ["-J", name, "-u", owner or getpass.getuser()]
        argv += list(ids or [])
        result = self.execute(argv)
        try:
            rows = parse_bjobs(result["stdout"])
        except (ValueError, Unresolved) as exc:
            raise Unresolved(f"bjobs query failed: {result['stderr']}") from exc
        if result["returncode"] != 0:
            response = result["stdout"] + result["stderr"]
            absent = any(
                term in response.lower()
                for term in ("not found", "no unfinished job", "no matching job")
            )
            if rows or not absent:
                raise Unresolved("bjobs query failed; job termination remains unresolved")
        return rows

    def reconcile(self, intent):
        if intent["status"] == "rejected":
            return []
        if not intent.get("job_id"):
            rows = self.query(name=intent["name"].split("[")[0], owner=intent["owner"])
            for row in rows:
                verify_owner(row, intent)
            ids = {r["job_id"].split("[")[0] for r in rows}
            if len(ids) != 1:
                raise Unresolved(f"Submission {intent['key']} is ambiguous; do not resubmit")
            intent.update(job_id=ids.pop(), status="accepted")
        job_id = intent["job_id"]
        expected = [f"{job_id}[{i}]" for i in intent["indices"]] if intent["indices"] else [job_id]
        evidence = self.directory / "outcomes" / f"scheduler-{intent.get('key', 'unknown')}.json"
        if evidence.exists():
            settled = read_json(evidence)
            if {r.get("job_id") for r in settled} == set(expected) and all(
                r.get("state") in TERMINAL for r in settled
            ):
                for row in settled:
                    verify_owner(row, intent)
                return settled
        found = {r["job_id"]: r for r in self.query(ids=[job_id])}
        rows = []
        for expected_id in expected:
            row = found.get(expected_id)
            if row is None:
                history = self.execute(["bhist", "-l", expected_id])
                if history["returncode"] != 0:
                    raise Unresolved(f"History unavailable for {expected_id}")
                row = parse_history(expected_id, history["stdout"])
            verify_owner(row, intent)
            rows.append(row)
        return rows

    def cancel(self, intent):
        rows = self.reconcile(intent)
        for row in rows:
            if row["state"] in ACTIVE:
                verify_owner(row, intent)
                self.execute(["bkill", row["job_id"]])
        return self.reconcile(intent)  # Caller must wait until every row is terminal.

    def cluster_config(self, requested_unit):
        lim = self.execute(["lsadmin", "showconf", "lim"])
        params = self.execute(["bparams", "-l"])
        if lim["returncode"] or params["returncode"]:
            raise ValueError(
                "Cannot inspect LSF units/array limits with lsadmin showconf lim and bparams -l"
            )
        unit = re.search(r"LSF_UNIT_FOR_LIMITS\s*=\s*(\w+)", lim["stdout"])
        actual_unit = unit[1].upper() if unit else "MB"
        if actual_unit != requested_unit:
            raise ValueError(f"LSF memory units are {actual_unit}, requested {requested_unit}")
        array = re.search(r"MAX_JOB_ARRAY_SIZE\s*=\s*(\d+)", params["stdout"])
        per_task = bool(re.search(r"RESOURCE_RESERVE_PER_TASK\s*=\s*[Yy]", params["stdout"]))
        if array and int(array[1]) < 1:
            raise ValueError("MAX_JOB_ARRAY_SIZE must be positive")
        return {
            "memory_unit": actual_unit,
            "max_array_size": int(array[1]) if array else 1000,
            "reserve_per_task": per_task,
        }
