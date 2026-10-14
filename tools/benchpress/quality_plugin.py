"""Export quality and single-call runtime with benchmark calibration disabled."""

import json
import os
from pathlib import Path
from time import perf_counter

import pytest
from schemas import (
    QUALITY_FIELDS,
    RUNTIME_METHOD,
    RUNTIME_SCOPE,
    SCHEMA,
    atomic_json,
    valid_runtime,
)


def pytest_configure(config):
    if os.environ.get("BENCHPRESS_QUALITY_ONLY") != "1":
        raise pytest.UsageError("This plugin requires BENCHPRESS_QUALITY_ONLY=1")
    if not config.getoption("benchmark_disable") or config.getoption("timeout_skip_list") != 0:
        raise pytest.UsageError("Use --benchmark-disable --timeout-skip-list=0")
    for arg in config.invocation_params.args:
        if arg.startswith("--benchmark-") and arg != "--benchmark-disable":
            raise pytest.UsageError(f"Quality mode does not accept {arg}")
    from pytest_benchmark.fixture import BenchmarkFixture

    original = BenchmarkFixture.__call__
    config._bp_original_call = original

    def counted(fixture, function, *args, **kwargs):
        if not fixture.disabled or fixture.timeout_skip_list or fixture.cprofile:
            raise ValueError("Benchmark calibration, preflight, and profiling must be disabled")

        def once(*call_args, **call_kwargs):
            fixture._bp_calls = getattr(fixture, "_bp_calls", 0) + 1
            if fixture._bp_calls != 1:
                raise ValueError("More than one transpilation attempted")
            start = perf_counter()
            result = function(*call_args, **call_kwargs)
            fixture._bp_runtime_s = perf_counter() - start
            return result

        return original(fixture, once, *args, **kwargs)

    BenchmarkFixture.__call__ = counted


def pytest_unconfigure(config):
    if hasattr(config, "_bp_original_call"):
        from pytest_benchmark.fixture import BenchmarkFixture

        BenchmarkFixture.__call__ = config._bp_original_call


def pytest_collection_modifyitems(config, items):
    items.sort(key=lambda item: item.nodeid)
    path = os.environ.get("BENCHPRESS_COLLECTION_OUTPUT")
    if path:
        from panel import collected_entry

        rows = [collected_entry(item) for item in items]
        atomic_json(path, rows)
    elif len(items) != 1:
        raise pytest.UsageError("One selected node is required per quality process")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    record = getattr(item, "_bp_record", None)
    if record is None:
        record = {
            "schema_version": SCHEMA,
            "execution_mode": "quality",
            "phases": {},
            "identity": json.loads(os.environ.get("BENCHPRESS_IDENTITY", "{}")),
            "node_id": item.nodeid,
            "extra_info": {},
            "call_count": 0,
            "runtime_s": None,
            "runtime_method": RUNTIME_METHOD,
            "runtime_scope": RUNTIME_SCOPE,
        }
        item._bp_record = record
    fixture = item.funcargs.get("benchmark")
    if fixture is not None:
        record["call_count"] = getattr(fixture, "_bp_calls", 0)
        runtime = getattr(fixture, "_bp_runtime_s", None)
        record["runtime_s"] = runtime if valid_runtime(runtime) else None
        record["extra_info"] = {k: v for k, v in fixture.extra_info.items() if k in QUALITY_FIELDS}
    from bp_runtime import OBSERVED

    record["observed"] = dict(OBSERVED)
    if report.when == "call" and report.passed:
        errors = []
        if record["call_count"] != 1:
            errors.append("expected exactly one benchmark callable execution")
        if record["runtime_s"] is None:
            errors.append("expected a finite positive callable runtime")
        identity = record["identity"]
        for key in ("optimization_level", "seed", "target_hash"):
            if identity.get(key) != OBSERVED.get(key) or key not in OBSERVED:
                errors.append(f"observed {key} differs from frozen identity")
        for key in ("output_gate_count_2q", "output_depth_2q", "output_num_qubits"):
            value = record["extra_info"].get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"invalid quality field {key}")
        if errors:
            report.outcome = "failed"
            report.longrepr = "; ".join(errors)
    record["phases"][report.when] = {
        "status": report.outcome,
        "reason": str(report.longrepr) if report.longrepr else "",
    }
    statuses = [v["status"] for v in record["phases"].values()]
    record["status"] = (
        "failed"
        if "failed" in statuses
        else (
            "skipped"
            if "skipped" in statuses
            else (
                "passed" if set(record["phases"]) == {"setup", "call", "teardown"} else "incomplete"
            )
        )
    )
    path = os.environ.get("BENCHPRESS_QUALITY_OUTPUT")
    if path:
        if not Path(path).is_absolute():
            raise ValueError("Quality output path must be absolute")
        atomic_json(path, record)
