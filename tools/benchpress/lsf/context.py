"""Validate allocation and Linux build compatibility before starting any child."""

import os
import platform
from pathlib import Path
import socket
import sys


def platform_info():
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "libc": list(platform.libc_ver()),
        "python_abi": sys.implementation.cache_tag,
    }


def allocation(expected_platform, exclusive=True):
    if platform.system() != "Linux":
        raise ValueError("Cluster workers require Linux")
    if not os.environ.get("LSB_JOBID", "").isdigit():
        raise ValueError("Missing LSB_JOBID")
    words = os.environ.get("LSB_MCPU_HOSTS", "").split()
    if len(words) != 2 or not words[1].isdigit():
        raise ValueError("Expected a single-host LSB_MCPU_HOSTS allocation")
    actual = platform_info()
    for key in ("system", "machine", "python_abi", "libc"):
        if actual[key] != expected_platform[key]:
            raise ValueError(f"Build/runtime platform mismatch: {key}")
    cpus = sorted(os.sched_getaffinity(0))
    topology = {}
    for cpu in cpus:
        root = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        topology[str(cpu)] = {
            name: (root / name).read_text().strip()
            for name in ("physical_package_id", "core_id", "thread_siblings_list")
        }
    cores = {(r["physical_package_id"], r["core_id"]) for r in topology.values()}
    if exclusive:
        if int(words[1]) != 1 or len(cores) != 1:
            raise ValueError("Worker must have one slot with affinity to one physical core")
        os.sched_setaffinity(0, {cpus[0]})
    return {
        "host": socket.gethostname(),
        "allocation_host": words[0],
        "slots": int(words[1]),
        "lsf_job_id": os.environ["LSB_JOBID"],
        "lsf_job_index": os.environ.get("LSB_JOBINDEX"),
        "queue": os.environ.get("LSB_QUEUE"),
        "affinity_before": cpus,
        "affinity": sorted(os.sched_getaffinity(0)),
        "cpu_topology": topology,
        "cpuinfo": Path("/proc/cpuinfo").read_text(),
        "platform": actual,
    }
