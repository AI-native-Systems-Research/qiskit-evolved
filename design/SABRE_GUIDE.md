# Understanding Qiskit's SABRE implementation

This guide explains [`crates/transpiler/src/passes/sabre`](../crates/transpiler/src/passes/sabre)
from the beginning. You do not need prior knowledge of quantum computing or Qiskit.
The descriptions refer to the code in this checkout, which already includes the
fusion credit and ready-time score described in [`RATIONALE.md`](RATIONALE.md).

Read this guide first, then [the detailed explanation of the change](SABRE_CHANGE_EXPLAINED.md).
The original rationale records experimental results; the companion explanation
separates those reported results from what the implementation itself establishes.

## 1. The problem SABRE solves

A quantum program may ask two qubits to interact, but a quantum processor may only
allow interactions between certain physical neighbors. SABRE inserts operations
that move the quantum states until the requested interactions become possible.
It also helps choose where those states should start.

For example, suppose the hardware has three connected positions:

```text
Physical positions:     p0 ---- p1 ---- p2
Logical states inside:   A      B       C
```

The program requests an operation between A and C. They are not neighbors.
One solution is to exchange the states at p0 and p1:

```text
Before SWAP(p0, p1):      A      B       C
After SWAP(p0, p1):       B      A       C
Physical positions:     p0 ---- p1 ---- p2
```

A and C can now interact on p1 and p2. Another solution would exchange p1 and p2.
Both solve the immediate problem, but they can have different costs for later
operations. Choosing between such alternatives is SABRE's main job.

## 2. The quantum concepts you need

### Qubits, states, and gates

A **qubit** is a quantum information unit. Measuring it in the usual computational
basis produces 0 or 1. Before measurement, its state can be a superposition of
those possibilities. Multiple qubits can also be entangled, so the full state
cannot always be described as independent states for each qubit.

For routing, you do not need to calculate these states. You only need to track
which logical qubit occupies which hardware position.

A **gate** transforms a quantum state:

| Term | Meaning for this guide |
| --- | --- |
| One-qubit gate, or `1q` gate | Acts on one qubit; usually creates no need to move states between hardware positions. |
| Two-qubit gate, or `2q` gate | Acts jointly on two qubits; their physical positions must support that interaction. |
| `CX`, also called CNOT | A two-qubit gate that flips the target when the control is 1 on computational-basis inputs; it also acts coherently on superpositions. |
| `CZ`, `CY`, `ECR` | Other two-qubit gates. Their matrices are unnecessary for understanding this router. |
| `SWAP` | Exchanges the quantum information carried by two qubits, preserving its relationships with the rest of the circuit. |
| Measurement | Produces classical information from a quantum system; it is not an ordinary reversible gate. |

A SWAP does not measure, copy, or physically relocate the hardware qubits. It
exchanges their quantum states. The hardware positions stay fixed.

### Circuits and wires

A **quantum circuit** is a program expressed as gates and other instructions on
qubits. A circuit drawing uses a **wire** for each qubit. Instructions sharing a
wire normally have an ordering dependency; independent instructions on different
wires may run in parallel.

SABRE runs on a classical computer while compiling this circuit. It does not run
a quantum algorithm on the device to discover routes, and does not simulate the
full quantum state to score candidate swaps.

### Logical versus physical qubits

A **logical** or **virtual qubit** is an identity in the user's program. Here,
"logical" does not mean an error-corrected logical qubit. A **physical qubit** is
a numbered hardware position.

A **layout** maps virtual qubits to physical positions. In the example above:

| Virtual qubit | Initial position | Position after SWAP(p0, p1) |
| --- | --- | --- |
| A | p0 | p1 |
| B | p1 | p0 |
| C | p2 | p2 |

Every later instruction must use the updated mapping. The program's A remains A,
even after its quantum information moves to another position.

The Rust `NLayout` type, defined outside this folder, maintains both mapping
directions. `layout[virtual_qubit]` gives a physical qubit;
`swap_physical(a, b)` updates the mapping after exchanging those positions.

### Connectivity, native gates, and cost

A **coupling graph** describes the hardware connections. Vertices are physical
qubits; edges represent available two-qubit connectivity. Its shortest-path
**distance** counts edges: neighbors have distance 1, and the endpoints of the
three-position line above have distance 2.

A **native gate** is an operation in the device's supported gate set, also called
its **basis**. A high-level SWAP is commonly decomposed into three CX gates. The
change in this checkout uses three native two-qubit operations as its SWAP cost
model. That is a model for the targeted compilation flow, not a universal physical
law for every processor or native gate set.

Two different output costs matter:

| Metric | What it measures | Why it matters |
| --- | --- | --- |
| Two-qubit gate count, `N2` in the rationale | Number of native two-qubit gates after the relevant compilation/optimization steps. | More gates require more interactions. |
| Two-qubit depth, `D2` in the rationale | Number of sequential two-qubit layers along the longest dependency chain. | Independent interactions can overlap, reducing the number of sequential layers. |

Four gates on disjoint pairs can have count 4 and depth 1. Four dependent gates on
the same pair can have count 4 and depth 4. This is why fewer gates and smaller
depth are related but different goals. Depth also differs from duration in seconds:
real gate durations need not be equal.

## 3. Where SABRE fits into Qiskit

**Qiskit** provides tools for constructing quantum circuits and compiling them
for devices. Its **transpiler** transforms a circuit into another circuit that
respects a target device while preserving the computation, including the mapping
of logical information to output positions.

A **pass** is one stage of this compilation process. A **pass manager** runs a
sequence of passes. Conceptually, compilation includes:

```text
User circuit
    -> decompose operations as needed
    -> choose an initial layout
    -> route interactions, inserting SWAPs
    -> translate and optimize operations for the native basis
    -> schedule operations, when scheduling is requested
```

Actual pipelines can interleave or repeat stages. SABRE primarily addresses layout
and routing. It does not by itself implement every native-gate optimization or
produce a timing-accurate hardware schedule.

The Python entry points are:

| Pass | Main role | Implementation link |
| --- | --- | --- |
| `SabreSwap` | Route a circuit whose wires already represent physical positions. | [`sabre_swap.py`](../qiskit/transpiler/passes/routing/sabre_swap.py) |
| `SabreLayout` | Search for a useful initial layout; its integrated path also routes the circuit. | [`sabre_layout.py`](../qiskit/transpiler/passes/layout/sabre_layout.py) |

These call compiled Rust functions through `qiskit._accelerate.sabre`. The
bindings are registered in [`mod.rs`](../crates/transpiler/src/passes/sabre/mod.rs)
using PyO3, the Rust/Python interoperability library.

## 4. Two different graphs—and two different meanings of layer

The router uses two graphs for different purposes:

| Graph | Vertices | Edges answer this question |
| --- | --- | --- |
| Hardware coupling graph | Physical qubits | Which positions can interact directly? |
| Circuit dependency graph | Operations or groups of operations | Which operations must precede another operation? |

The circuit graph is a **DAG**: a directed acyclic graph. Directed means that
dependencies have an order; acyclic means following dependency edges never brings
you back to the starting operation. A **topological order** lists operations in
an order that respects those dependencies.

The **front layer** contains dependency-ready two-qubit interactions that still
need routing. Earlier dependencies have been handled, but the current layout does
not make the interaction physically adjacent. Already executable interactions are
consumed eagerly instead of remaining in this layer.

**Lookahead layers** contain future two-qubit interactions, found by temporarily
treating earlier layers as satisfied. They help a swap choice account for what
comes next.

These search layers are different from **schedule layers**, the time-like levels
used to estimate two-qubit depth. Being in the front layer does not mean both
physical wires have ready-time value zero.

## 5. Map of the Rust folder

| File | Responsibility | Useful names to find |
| --- | --- | --- |
| [`mod.rs`](../crates/transpiler/src/passes/sabre/mod.rs) | Organizes modules and registers Python bindings. | `sabre` |
| [`dag.rs`](../crates/transpiler/src/passes/sabre/dag.rs) | Converts the circuit DAG into a smaller graph focused on routing. Also records estimated fusible block costs. | `SabreDAG`, `SabreNode`, `InteractionKind`, `block_gates` |
| [`layer.rs`](../crates/transpiler/src/passes/sabre/layer.rs) | Stores front/lookahead layers and calculates how swaps change distances. | `Layers`, `Layer::score`, `Layer::total_score` |
| [`heuristic.rs`](../crates/transpiler/src/passes/sabre/heuristic.rs) | Defines configurable scoring components, comparison tolerance, and fallback limit. | `Heuristic`, `BasicHeuristic`, `LookaheadHeuristic`, `DecayHeuristic`, `SetScaling` |
| [`route.rs`](../crates/transpiler/src/passes/sabre/route.rs) | Searches for swaps, updates state, handles fallback and control flow, and rebuilds the routed circuit. | `State`, `choose_best_swap`, `swap_map_trial`, `swap_map`, `RoutingResult` |
| [`layout.rs`](../crates/transpiler/src/passes/sabre/layout.rs) | Searches over starting layouts using forward/backward routing and multiple trials. | `sabre_layout_and_routing`, `layout_trial` |
| [`vec_map.rs`](../crates/transpiler/src/passes/sabre/vec_map.rs) | Provides array storage indexed by specific Rust index types. | `VecMap<Idx, T>` |

`VecMap<PhysicalQubit, u32>`, for example, is an array of integers indexed by
physical qubit. Its type helps prevent accidentally indexing it with a virtual
qubit or a circuit-node identifier. It is not a general hash map.

### How `SabreDAG` makes the problem smaller

`SabreDAG::from_dag` walks the input operations in dependency order. It can fold
single-qubit operations and successive interactions on the same pair into an
existing routing node when their dependencies permit it. Making that pair adjacent
is enough to route the entire group.

For example, this five-operation circuit becomes one start-of-circuit operation
plus two routing nodes:

| Operation | What `from_dag` does with it |
| --- | --- |
| `h(0)` | Nothing precedes it and it needs no routing, so it goes to `initial`, the list emitted at the very start. |
| `cx(0, 1)` | Creates routing node A, a `TwoQ` node for the pair (0, 1). |
| `rz(0.1, 1)` | A one-qubit rotation on qubit 1 whose only predecessor is A, so it is folded into A. |
| `cx(0, 1)` | Both of its wires come from A, so it is on the same pair and is folded into A. |
| `cx(0, 2)` | Wire 0 comes from A but wire 2 is new, so it becomes node B with a dependency edge A → B. |

The router only ever makes decisions about A and B. When A is routed, its three
operations are emitted together.

Each `SabreNode` contains:

- `indices`: references to the original circuit operations, retained for rebuilding.
- `kind`: `TwoQ`, `Synchronize`, or `ControlFlow`.
- `block`: an optional estimate of the native two-qubit cost available for fusion.

`Synchronize` represents dependencies that do not themselves require moving a
pair together. `ControlFlow` stores nested circuit blocks, such as branches of a
conditional. Simplifying the graph does not delete the original computation:
the saved indices allow `RoutingResult::rebuild` to emit the actual operations.

This routing representation assumes the surrounding pipeline has appropriately
handled larger gates. `InteractionKind::from_op` currently treats non-control-flow
operations on more than two qubits as synchronization nodes, with a TODO about
rejecting them. SABRE is not a general method for routing arbitrary undecomposed
multi-qubit gates.

## 6. How one routing trial works

The central function is `swap_map_trial` in `route.rs`. A **trial** is one complete
attempt to route the circuit from a supplied initial layout, using one random seed.

```text
Build state and route everything already executable.

While the front layer is not empty:
    Generate nearby candidate SWAPs.
    Score the candidates and choose one.
    Apply it to the layout and layer data; update its cost history.
    Repeat until an interaction becomes executable.

    If too many swaps fail to make progress:
        Undo this unsuccessful search segment.
        Force progress using a shortest path.

    Record the selected swaps and executable operations.
    Advance dependencies, front layer, lookahead, and ready-time estimates.

Return the routing order plus the initial and final layouts.
```

This is a heuristic search: it makes locally informed choices instead of searching
every possible complete routed circuit. It does not guarantee the globally optimal
swap count or depth.

### State that changes during the search

| State field | Purpose |
| --- | --- |
| `layout` | Current virtual-to-physical mapping. |
| `layers` | Current front and lookahead interactions. |
| `predecessors` | Tracks which dependencies remain unsatisfied for each layer's traversal. |
| `decay` | Remembers recent use of physical qubits in selected swaps. |
| `ready` | Estimates when each physical wire finishes its routed two-qubit work. |
| `last_block` | Records a recent fusible two-qubit block on each physical wire. |
| `ready_undo` | Saves ready-time and block history so unsuccessful swaps can be undone. |
| `swap_scores`, `best_swaps` | Reused temporary storage for candidate scoring and selection. |
| `rng`, `seed` | Control randomized choices and recursive routing. |

### Candidate swaps

`choose_best_swap` considers hardware edges that touch at least one physical qubit
active in the front layer. It does not test all pairs of hardware qubits. Swapping
across a non-edge would itself require routing; swapping far from every pending
interaction would not improve its immediate distance.

If both endpoints are active, the code includes the undirected edge only once.
The routing neighbor graph is logically undirected; native gate direction and
basis restrictions require other parts of the compilation pipeline.

### Basic distance, lookahead, and decay

The traditional scoring ingredients are:

1. **Basic distance:** favor swaps that bring front-layer partners closer.
2. **Lookahead:** also consider distances for future interactions.
3. **Decay:** discourage repeatedly using the same physical wires for swaps by
   multiplying the distance-based score by a recent-use factor.

The term "decay" here names a software heuristic. It is not a simulation of
physical decoherence or a measurement of hardware error.

`Layer::score` returns the **change** in the layer's summed distances. If a pending
pair moves from distance 2 to distance 1, that contributes `1 - 2 = -1`; smaller
scores are better. Only interactions touching the swapped positions need updating.
`Layer::total_score` supplies the absolute distance sum needed when applying decay.

`SetScaling::Constant` uses the configured weight directly. `SetScaling::Size`
divides it by the current number of nodes in the layer, so the contribution is
based on an average rather than a sum.

The Python wrapper in this checkout constructs these named modes:

| Mode | Basic weight | Lookahead | Decay |
| --- | --- | --- | --- |
| `basic` | `1 / front_layer_size` | None | None |
| `lookahead` | `1` | One layer, weighted `0.5 / number_of_coupling_qubits` | None |
| `decay` | `1` | Same as `lookahead` | Increment `0.001`, reset interval `5` swaps |

Decay factors are also reset to 1 whenever the search makes routing progress. The
integrated `SabreLayout` path always uses the `decay` row of this table. Both
wrappers set `attempt_limit`, the release-valve threshold described below, to
ten times the number of qubits. Custom internal `Heuristic` objects can choose
other settings.

**In this checkout, every mode also receives the fusion credit and ready-time
term.** The mode names select the configurable traditional components; they do
not disable those two additions. See the [exact score walkthrough](SABRE_CHANGE_EXPLAINED.md#4-the-exact-order-of-scoring).

### Randomness and the release valve

Scores within `best_epsilon` are treated as tied by the selection loop. The default
tolerance is `1e-10`. The router randomly selects from its best-candidate list
using the seeded generator. A **random seed** controls this pseudo-random sequence;
different seeds can take different routes through otherwise similar choices.

A local search can get stuck. After exceeding `attempt_limit` without enabling a
gate, the **release valve** reverses the unsuccessful swaps, restores their
ready-time/block history, and calls `force_enable_closest_node`. That function
chooses a closest front-layer pair and moves its endpoints along a shortest path
until they become adjacent. This supplies a way to make progress even when the
heuristic keeps making unhelpful choices.

## 7. Multiple trials and reconstruction

`swap_map` runs several routing trials, potentially in parallel. It picks the
result with the fewest **top-level inserted swaps**, using trial index to break a
tie. It does not choose the winner by measured native two-qubit depth or by the
sum of candidate scores.

This distinction matters: adding a depth-related term changes how each trial
constructs its route, while the final trial selection still uses swap count.

`RoutingResult::rebuild` and `rebuild_onto` then replay the recorded order. They
emit SWAP gates, update the mapping, and place the original operations on the
appropriate physical wires. Later compilation passes can decompose SWAPs and
optimize neighboring gates.

The final layout may differ from the initial layout. Ordinary top-level routing
does not require an extra sequence of swaps to put every state back. Instead,
Qiskit records the resulting permutation so later compilation and output
interpretation can account for where the logical information ended up.

## 8. How SABRE also chooses the initial layout

Starting A and C next to each other could avoid the example's first SWAP entirely.
`layout.rs` searches for placements that help the whole circuit, rather than just
its first gate.

For each starting-layout trial, `layout_trial`:

1. Builds a full mapping, filling unspecified positions randomly and including
   extra unused qubits, called ancillas here.
2. Routes a simplified interaction graph forward and keeps its final layout.
3. Reverses the interaction graph's dependencies and routes it backward, using the
   previous result as the next starting layout.
4. Repeats that forward/backward pair until it has run `max_iterations` times in
   total. The `SabreLayout` default is 3.
5. Runs final routing trials on the original circuit using the resulting layout.

Backward routing is an analysis technique. The hardware is not executing the
program backward, and `reverse_dag` does not invert the quantum gate matrices.

The search includes random layouts, supplied partial layouts, and heuristic
starting layouts such as a densely connected subset and increasing/reversed
qubit order. The code also contains starting-layout patterns for certain device
sizes. The selected layout trial is again chosen by swap count.

`only_interactions` strips original operation indices for these exploratory
passes, but preserves `SabreNode.block`. Consequently, the fusion estimate and
ready-time scoring also influence the forward/backward search. These exploratory
results cannot reconstruct a full circuit because they no longer contain the
original operation references.

Stripping those references has one side effect on the fusion estimate. In final
routing, a separate synchronization node, such as a measurement that could not
be folded into a two-qubit node, ends any open fusible block on its wires. The
code does that by reading the node's original operations, which the exploratory
graph no longer has. In the exploratory passes such a node therefore leaves the
block open, so the fusion credit there is slightly more generous than in final
routing. Block-ending operations that were folded *into* a two-qubit node are
not affected, because they are already recorded in that node's `block` value.

## 9. Control flow and disconnected devices

A circuit can contain classical control flow, such as an `if` statement depending
on a measurement. `route_control_flow_block` routes a nested block recursively
and appends swaps to restore its entry layout. This lets the outer circuit
continue with a consistent mapping regardless of the branch taken.

Each nested routing call has its own state. The outer ready-time tracker does not
accumulate an exact duration for nested branches. Control flow is therefore one
reason the ready-time values should be understood as a routing estimate, not a
complete schedule.

For disconnected hardware, `layout.rs` uses the surrounding disjoint-layout
helpers to place compatible circuit components in hardware components, then maps
the results back to full-device indices. A required interaction cannot be routed
across a missing physical connection. For all-to-all connectivity, no routing
swaps are needed, and the entry points have fast paths.

## 10. A small Python example

This example illustrates the public routing pass. It needs Qiskit built from this
checkout, including its compiled extension.

```python
from qiskit import QuantumCircuit
from qiskit.transpiler import CouplingMap
from qiskit.transpiler.passes import SabreSwap

circuit = QuantumCircuit(3)
circuit.cx(0, 2)  # This pair is not adjacent on the hardware line.

hardware = CouplingMap.from_line(3)
routed = SabreSwap(
    hardware, heuristic="decay", seed=7, trials=1
)(circuit)

print(routed.draw(output="text"))
print(routed.count_ops())
```

Run against a build of this checkout, it prints:

```text
q_0 -> 0 ─────■──
            ┌─┴─┐
q_1 -> 1 ─X─┤ X ├
          │ └───┘
q_2 -> 2 ─X──────

OrderedDict([('swap', 1), ('cx', 1)])
```

Read the drawing from left to right. Each wire is a physical position, and the
label `q_2 -> 2` says that logical qubit `q_2` starts at position 2. The two `X`
marks joined by a line are the SWAP on positions 1 and 2, which moves `q_2` next
to `q_0`. The CX then acts on positions 0 and 1.

The circuit already uses the three physical positions expected by `SabreSwap`.
The router could equally have swapped positions 0 and 1. Nothing separates the
two edges here: they improve the distance equally, every ready time is zero, and
no fusible block is open. The seeded random choice decides, and `seed=7` picks
(1, 2). Over seeds 0 to 39, 22 runs picked (0, 1) and 18 picked (1, 2).

This example only runs routing. A high-level `swap` still appears as one operation
in the output. Counting its operations or taking its depth before native-gate
translation does **not** directly give the benchmark's native `N2` or `D2`.

## 11. Suggested code-reading order

Start with `swap_map_trial` for the overall loop, then `choose_best_swap` for the
decision rule. Read `Layer::score` to see the distance calculation, and
`advance_routed` / `advance_swap` to see the new history tracking. Next read
`SabreDAG::from_dag` for graph compression and `layout_trial` for initial-layout
search. Leave reconstruction and control-flow details until those pieces make
sense.

For the change's motivation, formulas, worked examples, benchmark interpretation,
and limitations, continue to [SABRE_CHANGE_EXPLAINED.md](SABRE_CHANGE_EXPLAINED.md).
