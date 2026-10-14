"""Apply a checked, archived reproducibility patch to the pinned upstream source."""

import ast
import configparser
import difflib
from pathlib import Path
import re
from schemas import atomic_text

TEST_FILES = (
    "device_transpile/test_summit.py",
    "device_transpile/test_feynman.py",
    "device_transpile/test_hamiltonians.py",
    "abstract_transpile/test_qasmbench.py",
    "abstract_transpile/test_hamiltonians.py",
)
NOTICE = "# Modified by qiskit-evolved: deterministic, single-call quality comparison.\n"


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Pinned Benchpress source does not match adapter: {old!r}")
    return text.replace(old, new, 1)


def seed_calls(text):
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    inserts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "generate_preset_pass_manager":
                if any(k.arg == "seed_transpiler" for k in node.keywords):
                    raise ValueError("Unexpected preexisting seed")
                pos = offsets[node.end_lineno - 1] + node.end_col_offset - 1
                # Place before the closing parenthesis, allowing a trailing argument comma.
                prefix = "" if text[:pos].rstrip().endswith("(") else ","
                if text[:pos].rstrip().endswith(","):
                    prefix = ""
                inserts.append(
                    (pos, prefix + ' seed_transpiler=int(os.environ["BENCHPRESS_TRANSPILE_SEED"])')
                )
    if not inserts:
        raise ValueError("No preset calls found")
    for pos, value in sorted(inserts, reverse=True):
        text = text[:pos] + value + text[pos:]
    text = replace_once(
        text,
        "from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager",
        "from bp_runtime import generate_preset_pass_manager",
    )
    # Insert before imports, after any future statements (upstream has none).
    text = "import os\n" + text if not re.search(r"^import os$", text, re.M) else text
    ast.parse(text)
    return text


def apply_adapter(source, patch_path):
    source = Path(source)
    changes = {}
    for relative in TEST_FILES:
        path = "benchpress/qiskit_gym/" + relative
        changes[path] = seed_calls((source / path).read_text())
    path = "benchpress/config.py"
    changes[path] = replace_once(
        (source / path).read_text(),
        "self.filename = DEFAULT_FILENAME",
        'self.filename = os.environ.get("BENCHPRESS_CONFIG", DEFAULT_FILENAME)',
    )
    path = "benchpress/utilities/backends/flexible_backend.py"
    text = replace_once(
        (source / path).read_text(),
        "            control_flow=control_flow,\n        )",
        '            control_flow=control_flow,\n            seed=int(os.environ["BENCHPRESS_BACKEND_SEED"]),\n        )',
    )
    # rustworkx's make_symmetric appends reverse edges in per-process hash order, and
    # GenericBackendV2 draws seeded noise in edge order; symmetrize in sorted order instead.
    text = replace_once(
        text,
        "        if cmap:\n            cmap.make_symmetric()\n",
        "        if cmap:\n            edges = set(cmap.get_edges())\n"
        "            cmap = CouplingMap(sorted(edges | {(b, a) for a, b in edges}))\n",
    )
    changes[path] = "import os\n" + text
    path = "benchpress/qiskit_gym/utils/io.py"
    text = (source / path).read_text()
    text = replace_once(
        text,
        "    start = perf_counter()\n",
        '    start = None if os.environ.get("BENCHPRESS_QUALITY_ONLY") == "1" else perf_counter()\n',
    )
    text = replace_once(
        text,
        '    stop = perf_counter()\n    benchmark.extra_info["qasm_load_time"] = stop - start\n',
        '    if start is not None:\n        benchmark.extra_info["qasm_load_time"] = perf_counter() - start\n',
    )
    text = replace_once(
        text,
        '    benchmark.extra_info["output_num_qubits"] = circuit.num_qubits',
        '    benchmark.extra_info["native_2q_gate"] = two_qubit_gate\n    benchmark.extra_info["output_num_qubits"] = circuit.num_qubits',
    )
    changes[path] = "import os\n" + text
    patch = []
    for relative, text in sorted(changes.items()):
        path = source / relative
        original = path.read_text()
        text = NOTICE + text
        ast.parse(text)
        patch.extend(
            difflib.unified_diff(
                original.splitlines(True),
                text.splitlines(True),
                fromfile="a/" + relative,
                tofile="b/" + relative,
            )
        )
        atomic_text(path, text)
    atomic_text(patch_path, "".join(patch))


def write_configs(source, destination, levels):
    for level in levels:
        parser = configparser.ConfigParser()
        parser.read(Path(source) / "default.conf")
        parser["qiskit"]["optimization_level"] = str(level)
        import io

        stream = io.StringIO()
        parser.write(stream)
        atomic_text(Path(destination) / f"opt-{level}.conf", stream.getvalue())
