"""Resolve collected tests into a fixed, content-addressed circuit/target panel."""

import functools
import os
from pathlib import Path
import re
from schemas import BENCHPRESS, atomic_json, digest, file_hash, read_json

SUMMIT_INPUTS = {
    "test_QFT_100_transpile": ("qft/qft_N100.qasm", 100),
    "test_QV_100_transpile": (None, 100),
    "test_circSU2_89_transpile": (None, 89),
    "test_circSU2_100_transpile": (None, 100),
    "test_BV_100_transpile": (None, 100),
    "test_square_heisenberg_100_transpile": ("square-heisenberg/square_heisenberg_N100.qasm", 100),
    "test_QAOA_100_transpile": ("qaoa/qaoa_barabasi_albert_N100_3reps.qasm", 100),
    "test_BVlike_simplification_transpile": (None, 100),
    "test_clifford_100_transpile": ("clifford/clifford_100_12345.qasm", 100),
}
FEYNMAN = {"adder_8", "barenco_tof_5", "gf2^8_mult", "qft_4", "rc_adder_6"}
QASM_SELECTION = {
    "small": {"adder_n10", "qaoa_n6"},
    "medium": {"qft_n18", "ising_n26"},
    "large": {"qft_n29", "swap_test_n41"},
}
HAM_EXPECTED = {
    "chemistry": "Li2",
    "condensedmatter": "BH_D-1_d-4",
    "binaryoptimization": "graph-complete_bipart",
    "discreteoptimization": "TSP_Ncity-5",
}


@functools.cache
def qasm_info(path):
    text = Path(path).read_text()
    qubits = sum(int(n) for n in re.findall(r"\bqreg\s+\w+\s*\[\s*(\d+)\s*\]", text))
    if not qubits:
        raise ValueError(f"Cannot determine QASM width: {path}")
    return file_hash(path), qubits


@functools.lru_cache(maxsize=1)
def ham_inputs(source):
    rows = read_json(Path(source) / "benchpress/hamiltonian/hamlib/100_representative.json")
    return {(r["ham_category"], r["ham_problem"], r["ham_instance"]): digest(r) for r in rows}


def collected_entry(item):
    source = Path(os.environ["BENCHPRESS_SOURCE"])
    params = item.callspec.params if hasattr(item, "callspec") else {}
    filename = item.path.name
    row = {"node_id": item.nodeid, "topology": "fake_torino", "selection_reason": ""}
    path = None
    ham = None
    if "device_transpile" in str(item.path):
        if filename == "test_summit.py":
            name = item.originalname or item.name
            relative, width = SUMMIT_INPUTS[name]
            row.update(suite="device-summit", input_name=name, input_qubits=width)
            if relative:
                path = source / "benchpress/qasm" / relative
            else:
                row["input_hash"] = digest(
                    {
                        "benchpress": BENCHPRESS,
                        "generator": name,
                        "qv_seed": 12345,
                        "su2_reps": 3,
                        "entanglement": "circular",
                    }
                )
        elif filename == "test_feynman.py":
            path = source / "benchpress/qasm/feynman" / params["filename"]
            row.update(suite="device-feynman", input_name=path.stem)
        else:
            ham = params["hamiltonian_info"]
            row["suite"] = "device-hamlib"
        row["device_capacity"] = item.module.BACKEND.num_qubits
    else:
        value, topology = params["circ_and_topo"]
        row["topology"] = topology
        if filename == "test_qasmbench.py":
            path = Path(value)
            size = next(size for size in QASM_SELECTION if f"qasmbench-{size}" in str(path))
            row.update(suite=f"abstract-qasmbench-{size}", input_name=path.stem)
        else:
            ham = value
            row["suite"] = "abstract-hamlib"
    if path:
        checksum, width = qasm_info(str(path))
        row.update(
            input_path=str(path.relative_to(source)), input_hash=checksum, input_qubits=width
        )
    if ham:
        row["hamiltonian"] = {k: v for k, v in ham.items() if k != "ham_hamlib_hamiltonian"}
        row.update(
            input_name=ham["ham_problem"] + ":" + ham["ham_instance"],
            input_qubits=int(ham["ham_qubits"]),
            input_hash=ham_inputs(str(source))[
                (ham["ham_category"], ham["ham_problem"], ham["ham_instance"])
            ],
        )
    row["panel_id"] = "p-" + digest({k: row[k] for k in ("node_id", "input_hash", "topology")})[:20]
    row["eligible"] = row["input_qubits"] <= row.get("device_capacity", row["input_qubits"])
    row["exclusion_reason"] = "" if row["eligible"] else "input exceeds device capacity"
    return row


def select_panel(inventory, profile, panel_file=None, panel_size=None):
    if len({r["node_id"] for r in inventory}) != len(inventory):
        raise ValueError("Duplicate collected node IDs")
    if panel_file:
        from schemas import read_jsonl

        wanted = read_jsonl(panel_file)
        by_node = {r["node_id"]: r for r in inventory}
        selected = []
        for entry in wanted:
            row = dict(by_node[entry["node_id"]])
            for field in ("input_hash", "panel_id", "topology"):
                if field in entry and entry[field] != row[field]:
                    raise ValueError(f"Selected input changed: {entry['node_id']}")
            if not entry.get("selection_reason"):
                raise ValueError("Each selected panel row needs a selection_reason")
            row["selection_reason"] = entry["selection_reason"]
            selected.append(row)
    elif profile == "full":
        if len(inventory) != 1054:
            raise ValueError(f"Expected 1054 pinned inventory entries, found {len(inventory)}")
        selected = [
            dict(r, selection_reason="eligible full corpus") for r in inventory if r["eligible"]
        ]
    elif profile == "quality":
        chosen = {}
        for category in HAM_EXPECTED:
            rows = [
                r
                for r in inventory
                if r["suite"] == "device-hamlib"
                and r["hamiltonian"]["ham_category"] == category
                and r["eligible"]
            ]
            eligible = [r for r in rows if 8 <= r["input_qubits"] <= 32]
            ordered = sorted(
                eligible or rows,
                key=lambda r: (
                    r["input_qubits"],
                    r["hamiltonian"]["ham_problem"],
                    r["hamiltonian"]["ham_instance"],
                ),
            )
            if not ordered:
                raise ValueError(f"No device-compatible Hamiltonian for {category}")
            chosen[category] = ordered[len(ordered) // 2 if eligible else 0]["input_hash"]
        selected = []
        for row in inventory:
            suite = row["suite"]
            keep = suite == "device-summit" or (
                suite == "device-feynman" and row["input_name"] in FEYNMAN
            )
            if suite.startswith("abstract-qasmbench-"):
                keep = row["input_name"] in QASM_SELECTION[suite.rsplit("-", 1)[-1]]
            if "hamlib" in suite:
                category = row["hamiltonian"]["ham_category"]
                keep = row["input_hash"] == chosen[category] and (
                    suite == "device-hamlib" or category in ("chemistry", "condensedmatter")
                )
            if keep:
                selected.append(
                    dict(row, selection_reason="fixed 50-entry panel; HamLib category median")
                )
        if len(selected) != 50:
            raise ValueError(f"Fixed panel must contain 50 entries, found {len(selected)}")
    else:
        raise ValueError("followup-quality requires --panel-file")
    if not selected or len({r["node_id"] for r in selected}) != len(selected):
        raise ValueError("Panel must be nonempty and unique")
    if panel_size is not None and len(selected) != panel_size:
        raise ValueError(
            f"Panel has {len(selected)} entries, expected {panel_size}; use --panel-file to select"
        )
    return sorted(selected, key=lambda r: r["node_id"])


def expand(panel, config, config_hashes, stage=None):
    cases, tasks = [], []
    stage = stage or config["profile"]
    for row in panel:
        per_level = {}
        for level in config["opt_levels"]:
            case = dict(row, optimization_level=level, config_hash=config_hashes[str(level)])
            case["case_id"] = (
                "c-"
                + digest([row["panel_id"], level, case["config_hash"], row["target_hash"]])[:20]
            )
            per_level[str(level)] = case
            cases.append(case)
        for seed in config["seeds"]:
            task = dict(row, cases=per_level, stage=stage, seed=seed, array_index=len(tasks) + 1)
            task["task_id"] = "t-" + digest([stage, row["panel_id"], seed])[:20]
            tasks.append(task)
    return cases, tasks


def freeze_targets(panel_path):
    """Run inside each built environment; compare the resulting fingerprints."""
    from benchpress.config import Configuration

    Configuration.gym_name = "qiskit"
    from benchpress.utilities.backends import FlexibleBackend
    from bp_runtime import target_fingerprint

    rows = read_json(panel_path)
    device = Configuration.backend()
    cache = {}
    for row in rows:
        key = row["topology"], row["input_qubits"]
        if row["topology"] == "fake_torino":
            key = ("fake_torino",)
        if key not in cache:
            backend = (
                device
                if key == ("fake_torino",)
                else FlexibleBackend(row["input_qubits"], row["topology"], control_flow=True)
            )
            cache[key] = target_fingerprint(backend)
        row["target_hash"] = cache[key]
    atomic_json(panel_path, rows)


if __name__ == "__main__":
    import sys

    freeze_targets(sys.argv[1])
