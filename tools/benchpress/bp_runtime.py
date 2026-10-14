"""Runtime bridge imported by the patched Benchpress tests, never by the controller."""

import os
from schemas import digest

OBSERVED = {}


def target_fingerprint(backend):
    target = backend.target
    instructions = []
    for name in sorted(target.operation_names):
        for qargs, prop in sorted(target[name].items(), key=lambda x: str(x[0])):
            instructions.append(
                [
                    name,
                    qargs,
                    None if prop is None else {"duration": prop.duration, "error": prop.error},
                ]
            )
    return digest({"qubits": target.num_qubits, "dt": target.dt, "instructions": instructions})


def generate_preset_pass_manager(*args, **kwargs):
    from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager as generate

    level = kwargs.get("optimization_level", args[0] if args else None)
    backend = kwargs.get("backend", args[1] if len(args) > 1 else None)
    if level != int(os.environ["BENCHPRESS_OPT_LEVEL"]):
        raise ValueError("Preset optimization level does not match the frozen task")
    if kwargs.get("seed_transpiler") != int(os.environ["BENCHPRESS_TRANSPILE_SEED"]):
        raise ValueError("Missing or incorrect transpiler seed")
    OBSERVED.update(
        optimization_level=level,
        seed=kwargs["seed_transpiler"],
        target_hash=target_fingerprint(backend),
    )
    return generate(*args, **kwargs)
