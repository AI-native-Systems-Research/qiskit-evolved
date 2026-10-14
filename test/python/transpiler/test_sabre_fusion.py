# This code is part of Qiskit.
#
# (C) Copyright IBM 2026.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at https://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Focused coverage of SABRE fusion scoring and its internal preset policy."""

import pickle
from unittest.mock import patch

from qiskit import QuantumCircuit, transpile
from qiskit.circuit import Instruction, Parameter
from qiskit.circuit.library import Initialize
from qiskit.circuit.random import random_circuit
from qiskit.quantum_info import Operator
from qiskit.transpiler import CouplingMap, PassManager, PassManagerConfig, Target
from qiskit.transpiler.passes import SabreSwap, SabreLayout, ConsolidateBlocks, UnitarySynthesis
from qiskit.transpiler.passes.layout import sabre_layout
from qiskit.transpiler.passes.routing import sabre_swap
from qiskit.transpiler.passes.routing.sabre_swap import Heuristic, SetScaling
from qiskit.transpiler.preset_passmanagers import builtin_plugins
from test import QiskitTestCase


def heuristic(mode="decay", weight=0.25, width=3, attempt_limit=1000):
    """An explicit internal heuristic for testing the scoring independently of presets."""
    result = Heuristic(attempt_limit=attempt_limit).with_basic(1.0, SetScaling.Constant)
    if mode != "basic":
        result = result.with_lookahead([0.5 / width], SetScaling.Constant)
    if mode == "decay":
        result = result.with_decay(0.001, 5)
    return result.with_fusion(weight)


class TestSabreFusion(QiskitTestCase):
    """Exercise eligibility, disabled behavior, synthesis savings and serialization."""

    def test_block_endings_match_disabled_routing(self):
        """A blocker removes the credit and preserves disabled mode's seeded choices."""
        theta = Parameter("theta")
        middles = [
            lambda qc: qc.rzz(theta, 0, 1),
            lambda qc: qc.rx(theta, 0),
            lambda qc: qc.append(Instruction("blob", 2, 0, []), [0, 1]),
            lambda qc: qc.append(Initialize([1, 0]), [0]),
            lambda qc: qc.barrier(0, 1),
            lambda qc: qc.delay(10, 0),
            lambda qc: qc.reset(0),
            lambda qc: qc.measure(0, 0),
        ]
        coupling = CouplingMap.from_line(3)
        for middle in middles:
            qc = QuantumCircuit(3, 1)
            qc.cx(0, 1)
            middle(qc)
            qc.cx(0, 2)
            for seed in range(8):
                with self.subTest(middle=qc.data[1].operation.name, seed=seed):
                    enabled = SabreSwap(coupling, heuristic("basic"), seed=seed, trials=1)(qc)
                    disabled = SabreSwap(coupling, heuristic("basic", 0), seed=seed, trials=1)(qc)
                    self.assertEqual(enabled, disabled)
                    self.assertEqual(
                        enabled.layout.final_index_layout(), disabled.layout.final_index_layout()
                    )

    def test_prefers_fusible_swap(self):
        """Tied distance candidates favor the pair whose preceding gates can absorb a SWAP."""
        coupling = CouplingMap.from_line(3)
        for mode in ["basic", "lookahead", "decay"]:
            for middle in [None, "numeric", "custom"]:
                qc = QuantumCircuit(3)
                qc.cx(0, 1)
                if middle == "numeric":
                    qc.rzz(0.3, 0, 1)
                elif middle == "custom":
                    block = QuantumCircuit(2)
                    block.rzz(0.3, 0, 1)
                    qc.append(block.to_gate(), [0, 1])
                qc.cx(0, 2)
                for seed in range(8):
                    with self.subTest(mode=mode, middle=middle, seed=seed):
                        routed = SabreSwap(coupling, heuristic(mode), seed=seed, trials=1)(qc)
                        swap = next(inst for inst in routed.data if inst.operation.name == "swap")
                        self.assertEqual({routed.find_bit(q).index for q in swap.qubits}, {0, 1})
                        self.assertTrue(Operator.from_circuit(routed).equiv(Operator(qc)))
                        if middle is None:
                            fused = PassManager(
                                [
                                    ConsolidateBlocks(basis_gates=["cx", "u"]),
                                    UnitarySynthesis(["cx", "u"]),
                                ]
                            ).run(routed)
                            self.assertEqual(fused.count_ops()["cx"], 3)

    def test_lookahead_synthesis_tradeoff(self):
        """A second SWAP can reduce synthesized CX count from nine to eight."""
        qc = QuantumCircuit(5)
        qc.cx(0, 1)
        qc.cx(2, 3)
        qc.h(0)
        for pair in [(1, 2), (1, 3), (2, 3), (1, 3)]:
            qc.cx(*pair)
        for weight, swaps, cx in [(0.0, 1, 9), (0.25, 2, 8)]:
            routed = SabreSwap(
                CouplingMap.from_line(5), heuristic("lookahead", weight, 5), seed=0, trials=1
            )(qc)
            self.assertEqual(routed.count_ops()["swap"], swaps)
            fused = PassManager(
                [ConsolidateBlocks(basis_gates=["cx", "u"]), UnitarySynthesis(["cx", "u"])]
            ).run(routed)
            self.assertEqual(fused.count_ops()["cx"], cx)
            self.assertTrue(Operator.from_circuit(routed).equiv(Operator(qc)))
        for level in [0, 1]:
            out = transpile(
                qc,
                coupling_map=CouplingMap.from_line(5),
                basis_gates=["cx", "u"],
                initial_layout=list(range(5)),
                routing_method="sabre",
                optimization_level=level,
                seed_transpiler=0,
            )
            self.assertEqual(out.count_ops()["cx"], 9)

    def test_internal_heuristic_configuration(self):
        """The existing constructor stays compatible and pickling retains fusion."""
        enabled = heuristic()
        self.assertEqual(enabled, pickle.loads(pickle.dumps(enabled)))
        self.assertIn("fusion_weight=0.25", repr(enabled))
        self.assertEqual(Heuristic().fusion_weight, 0.0)
        self.assertEqual(Heuristic(None, None, None, 20, 1e-8).fusion_weight, 0.0)
        for weight in [-1, float("nan"), float("inf"), -float("inf")]:
            with self.assertRaisesRegex(ValueError, "finite and nonnegative"):
                Heuristic().with_fusion(weight)

    def test_enabled_routing_and_layout_equivalence(self):
        """Fusion preserves the unitary under tight fallback limits and combined layout."""
        qc = random_circuit(5, 8, max_operands=2, seed=42)
        coupling = CouplingMap.from_line(5)
        for limit in [0, 1]:
            out = SabreSwap(coupling, heuristic(width=5, attempt_limit=limit), seed=2, trials=1)(qc)
            self.assertTrue(Operator.from_circuit(out).equiv(Operator(qc)))
        for weight in [0.0, 0.25]:
            pass_ = SabreLayout(
                coupling, seed=2, swap_trials=2, layout_trials=2, _fusion_weight=weight
            )
            with patch.object(
                sabre_layout,
                "sabre_layout_and_routing",
                wraps=sabre_layout.sabre_layout_and_routing,
            ) as route:
                out = pickle.loads(pickle.dumps(pass_))(qc)
            self.assertEqual(route.call_args.args[2].fusion_weight, weight)
            self.assertTrue(Operator.from_circuit(out).equiv(Operator(qc)))


class TestSabreFusionPresets(QiskitTestCase):
    """Verify internal enablement across default layout, explicit SABRE layout and routing."""

    def test_compatible_level_2_and_3_only(self):
        for basis, compatible in [
            (["cx", "rz", "sx", "x"], True),
            (["cz", "u"], True),
            (["ecr", "u"], True),
            (["iswap", "u"], False),
            (["swap", "cx", "u"], False),
            (["cz", "rzz", "rz", "sx", "x"], False),
            (["cx", "h", "t", "s"], False),
            (["cx", "x"], False),
        ]:
            target = Target.from_configuration(
                basis_gates=basis, coupling_map=CouplingMap.from_line(3)
            )
            for level in range(4):
                with self.subTest(basis=basis, level=level):
                    config = PassManagerConfig(target=target, basis_gates=basis)
                    self.assertEqual(
                        builtin_plugins._sabre_fusion_weight(config, level),
                        0.25 if compatible and level >= 2 else 0.0,
                    )
        for options in [
            {"optimization_method": "external"},
            {"unitary_synthesis_method": "external"},
            {"routing_method": "basic"},
        ]:
            config = PassManagerConfig(
                target=Target.from_configuration(
                    basis_gates=["cx", "u"], coupling_map=CouplingMap.from_line(3)
                ),
                **options,
            )
            self.assertEqual(builtin_plugins._sabre_fusion_weight(config, 3), 0.0)
        self.assertEqual(builtin_plugins._sabre_fusion_weight(PassManagerConfig(), 3), 0.0)

    def test_preset_heuristic_preserves_decay_defaults(self):
        """Only fusion differs from named decay, including all internal heuristic settings."""
        for width in [3, 5]:
            target = Target.from_configuration(
                basis_gates=["cx", "u"], coupling_map=CouplingMap.from_line(width)
            )
            qc = random_circuit(width, 8, max_operands=2, seed=42)
            for level in [2, 3]:
                with self.subTest(width=width, level=level):
                    preset = builtin_plugins._sabre_routing_heuristic(
                        PassManagerConfig(target=target), level
                    ).with_fusion(0.0)
                    with patch.object(
                        sabre_swap, "sabre_routing", wraps=sabre_swap.sabre_routing
                    ) as route:
                        expected = SabreSwap(target, "decay", seed=0, trials=1)(qc)
                    self.assertEqual(preset, route.call_args.args[2])
                    actual = SabreSwap(target, preset, seed=0, trials=1)(qc)
                    self.assertEqual(actual, expected)
                    self.assertEqual(
                        actual.layout.final_index_layout(), expected.layout.final_index_layout()
                    )

    def test_coupling_map_only_disables_fusion(self):
        """A preset without a synthesis basis keeps fusion disabled in actual layout trials."""
        qc = random_circuit(5, 8, max_operands=2, seed=42)
        for level in [2, 3]:
            with self.subTest(level=level):
                with patch.object(
                    sabre_layout,
                    "sabre_layout_and_routing",
                    wraps=sabre_layout.sabre_layout_and_routing,
                ) as route:
                    transpile(
                        qc,
                        coupling_map=CouplingMap.from_line(5),
                        layout_method="sabre",
                        optimization_level=level,
                        seed_transpiler=0,
                    )
                self.assertTrue(route.called)
                self.assertEqual(route.call_args.args[2].fusion_weight, 0.0)

    def test_all_builtin_sabre_paths(self):
        target = Target.from_configuration(
            basis_gates=["cx", "u"], coupling_map=CouplingMap.from_line(5)
        )
        for plugin in [
            builtin_plugins.DefaultLayoutPassManager,
            builtin_plugins.SabreLayoutPassManager,
            builtin_plugins.SabreSwapPassManager,
        ]:
            symbol, original = (
                ("SabreSwap", SabreSwap)
                if plugin is builtin_plugins.SabreSwapPassManager
                else ("SabreLayout", SabreLayout)
            )
            for level in range(4):
                with self.subTest(plugin=plugin.__name__, level=level):
                    with patch.object(builtin_plugins, symbol, wraps=original) as constructor:
                        plugin().pass_manager(PassManagerConfig(target=target), level)
                    if plugin is builtin_plugins.DefaultLayoutPassManager and level == 0:
                        continue
                    self.assertTrue(constructor.called)
                    for call in constructor.call_args_list:
                        if original is SabreSwap:
                            setting = call.kwargs["heuristic"]
                            weight = (
                                setting.fusion_weight if isinstance(setting, Heuristic) else 0.0
                            )
                        else:
                            weight = call.kwargs.get("_fusion_weight", 0.0)
                        self.assertEqual(weight, 0.25 if level >= 2 else 0.0)

    def test_transpile_executes_configured_routing(self):
        qc = random_circuit(5, 8, max_operands=2, seed=42)
        for level in range(4):
            seen = []

            def callback(**kwargs):
                if isinstance(pass_ := kwargs["pass_"], SabreSwap):
                    seen.append(
                        pass_.heuristic.fusion_weight
                        if isinstance(pass_.heuristic, Heuristic)
                        else 0.0
                    )

            out = transpile(
                qc,
                coupling_map=CouplingMap.from_line(5),
                basis_gates=["cx", "u"],
                initial_layout=list(range(5)),
                seed_transpiler=0,
                optimization_level=level,
                callback=callback,
            )
            self.assertEqual(seen, [0.25 if level >= 2 else 0.0])
            self.assertTrue(Operator.from_circuit(out).equiv(Operator(qc)))
