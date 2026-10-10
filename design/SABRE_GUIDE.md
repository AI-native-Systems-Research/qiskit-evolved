# Understanding Qiskit's SABRE implementation

SABRE is a classical compilation algorithm that chooses where a circuit's qubits
start and which SWAPs to insert so its two-qubit gates can run on a device.
Its name comes from **SWAP-based BidiREctional heuristic search**. The original
algorithm was introduced by Li, Ding, and Xie in
[Tackling the Qubit Mapping Problem for NISQ-Era Quantum Devices](https://arxiv.org/abs/1809.02573).

This guide assumes you have seen a quantum circuit and know that gates act on
qubits. You do not need quantum-state mathematics, graph theory, or Rust. By the
end, you should be able to route a small circuit by hand, calculate why SABRE
prefers one SWAP, and explain how a backward pass helps choose a starting layout.
A **heuristic** is a practical rule for ranking choices cheaply. It can guide
the search toward a useful route without guaranteeing the best possible route.

We follow one four-qubit circuit from dependencies to the final physical circuit.
Every scoring ingredient is introduced in words before its arithmetic. The code
references describe [`crates/transpiler/src/passes/sabre`](../crates/transpiler/src/passes/sabre)
in **this checkout**. It includes fusion credit and ready-time scoring in addition
to SABRE's distance, lookahead, and decay heuristics. Those additions are labeled
explicitly; they should not be assumed to exist in every implementation of SABRE.

On a first read, follow sections 1–4, the walkthrough in section 6, and the layout
example in section 8. Section 5 is a code map you can return to later.

- [Quantum concepts and notation](#2-the-quantum-concepts-you-need)
- [The example circuit and its dependencies](#4-two-different-graphsand-two-different-meanings-of-layer)
- [One routing trial, step by step](#6-how-one-routing-trial-works)
- [Choosing the initial layout, with a backward example](#8-how-sabre-also-chooses-the-initial-layout)
- [Runnable Python examples](#10-python-examples-you-can-run)
- [Check your understanding](#12-check-your-understanding)

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

There are two related decisions:

| Decision | Question | Example |
| --- | --- | --- |
| Initial layout | Where should each logical qubit begin? | Put A at p1 and C at p2 before the circuit starts. |
| Routing | Given where the information is now, how do we enable the next gates? | Insert SWAP(p0, p1) when A and C are too far apart. |

Choosing an initial layout assigns program wires to hardware before execution;
it does not itself insert a physical SWAP. Once execution has begun, changing
where quantum information lives generally requires actual operations. This is
why finding a useful initial layout can save work.

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

Throughout this guide, capitals such as `A` and `B` name logical qubits, while
`p0`, `p1`, and so on name fixed physical positions. A row such as `[B, A, C, D]`
always lists **the occupants of p0, p1, p2, p3**, in that order. It means
`A -> p1`, not `A -> p0`. This convention matters when reading the walkthrough.

An ordinary CX changes quantum information but leaves this identity mapping
unchanged. A routing SWAP changes both the quantum state on the hardware and the
compiler's record of where the program's information is located. Updating the
record without emitting the SWAP would not implement the requested computation.

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

### The circuit we will route

Use a four-position hardware line, initially with one logical qubit per position:

```text
Physical:      p0 ---- p1 ---- p2 ---- p3
Occupants:      A      B       C       D

Program order:
g1 = CX(A, C)
g2 = CX(A, B)
g3 = CX(C, D)
g4 = CX(B, D)
```

For connectivity, only the two participants matter. For the emitted CX, their
control/target order still matters: `CX(A, C)` must remain controlled by A.
We assume each hardware edge can support either direction, possibly after other
compiler passes translate the gate.

The program's dependencies are:

```text
             g2: CX(A, B)
            /            \
g1: CX(A, C)                g4: CX(B, D)
            \            /
             g3: CX(C, D)
```

Each connecting line is a dependency directed from left to right:

| Operation | Must wait for | Reason |
| --- | --- | --- |
| g1 | Nothing | It is the first gate on both A and C. |
| g2 | g1 | They share A. |
| g3 | g1 | They share C. |
| g4 | g2 and g3 | B comes from g2; D comes from g3. |

Although g3 is written after g2, those two gates act on disjoint logical pairs.
There is no dependency between them, so either can come first in a legal output.
SABRE follows the circuit's dependency graph. It does not assume it can reorder
two gates sharing a wire just because their matrices happen to commute.

### Two separate tests for executing a gate

A two-qubit gate can be emitted only when **both** tests pass:

1. **Dependency readiness:** all its preceding operations have been handled.
2. **Physical adjacency:** its two logical qubits currently occupy connected
   physical positions.

Initially g1 passes test 1 but fails test 2: A is at p0 and C at p2, distance 2.
Gates g2 and g3 pass the adjacency test, but cannot execute yet because they
depend on g1. Adjacency alone never authorizes skipping a dependency.

The **front layer**, written `F`, is the set of dependency-ready two-qubit
interactions at the current stage. In this implementation, already adjacent
interactions are emitted eagerly; the stored front layer therefore contains the
ones still blocked by connectivity. Initially, `F = {g1}`.

The first **lookahead layer**, written `E`, answers: "If the current front were
finished, which two-qubit interactions would become dependency-ready next?"
Initially, `E = {g2, g3}`. The router computes this with temporary dependency
counters; it has **not** executed either gate or changed the real front layer.
G4 is not in this first lookahead layer, because g2 and g3 still precede it.
An internal configuration with a second lookahead layer could include g4 there.

Lookahead can include a pair that is already adjacent. That is useful information:
a candidate SWAP might separate those partners and create work for later.

### Dependencies advance one completed gate at a time

For the example, think of each gate as having a "predecessors still needed"
counter. Initially g2 and g3 each need one predecessor, and g4 needs two.
Routing g1 drops the first two counters to zero. Routing g2 alone drops g4's
counter from two to one; only routing g3 as well makes g4 eligible.

The code counts dependency **edges**. There can be multiple edges from the same
routing node when more than one wire connects two nodes. Satisfying that node
satisfies all those edges. The table above uses distinct gate predecessors to
explain the same readiness condition without that implementation detail.

These search layers differ from **schedule layers**, which describe sequential
work when estimating two-qubit depth. G2 and g3 form a dependency layer even if
their physical wires become free at different estimated times. "Front" means
next in dependency order, not ready at physical time zero.

## 5. Map of the Rust folder

This section connects the explanation to code. You can skip to section 6 on a
first read; none of the Rust types are prerequisites for the walkthrough.

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
| `cx(0, 1)` | Creates routing node R1, a `TwoQ` node for the pair (0, 1). |
| `rz(0.1, 1)` | A one-qubit rotation on qubit 1 whose only predecessor is R1, so it is folded into R1. |
| `cx(0, 1)` | Both of its wires come from R1, so it is on the same pair and is folded into R1. |
| `cx(0, 2)` | Wire 0 comes from R1 but wire 2 is new, so it becomes node R2 with a dependency edge R1 → R2. |

The router only ever makes decisions about R1 and R2. When R1 is routed, its
three operations are emitted together, in order. This grouping is **routing
compression**, not a claim that those three gates are one physical operation or
take one hardware time step. In particular, the later ready-time approximation
counts the group more coarsely than its actual gate sequence.

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

A **routing trial** is one complete attempt to route the circuit from a supplied
initial layout. It uses a seeded random generator to choose between candidates
that have effectively equal scores. The central function is `swap_map_trial`.

First we work through distance and lookahead. Then we add decay and this
checkout's two extra scoring terms to the same picture.

### Step 1: prepare the input and distance table

The router needs the circuit dependency graph, a hardware graph, an initial
layout, and a scoring configuration. The graph has already been reduced to
routing nodes as described in section 5. In our four-gate example each gate is a
separate routing node, because consecutive interactions change partners.

Shortest-path distance is the number of hardware edges between two positions.
For our line, the precomputed table is:

| From / to | p0 | p1 | p2 | p3 |
| --- | --- | --- | --- | --- |
| p0 | 0 | 1 | 2 | 3 |
| p1 | 1 | 0 | 1 | 2 |
| p2 | 2 | 1 | 0 | 1 |
| p3 | 3 | 2 | 1 | 0 |

On this particular line, distance is the absolute difference between position numbers.
On other hardware it must come from graph paths, not from the numeric labels.

We use `d(A, C)` as shorthand for "look up the distance between the physical
positions currently holding A and C." Initially, `d(A, C) = distance(p0, p2) = 2`.
The target for a two-qubit interaction is distance **1**, not zero: two distinct
logical qubits must occupy two distinct physical positions.

For an isolated pair at distance `d`, `d - 1` adjacent SWAPs along a shortest
path suffice to make it adjacent. However, adding `d - 1` over a whole circuit
does not predict its total SWAP count. One SWAP can help multiple pairs, harm
another pair, or change the starting point for later interactions.

### Step 2: emit what is already possible and build the front

Starting from the supplied layout, visit operations whose dependencies are
satisfied. Emit a two-qubit interaction if its mapped positions are adjacent.
Otherwise keep it in the front layer. Emitting a node can unlock successors, so
continue until no more newly available operations can be emitted without a SWAP.

Here `g1 = CX(A, C)` is the only dependency-ready gate, but A and C are not
adjacent. Nothing can be emitted yet:

```text
Layout:       [A, B, C, D]
Front F:      {g1 = CX(A, C)}
Already done: nothing
```

With a different initial layout, `[B, A, C, D]`, g1 would be adjacent. The router
would emit g1, then g2 and g3, before needing any routing decision at all.
This eager traversal is why a circuit containing many gates can require only a
few visits to the SWAP-selection loop.

### Step 3: build lookahead without executing it

Copy the dependency state and pretend g1 is satisfied. G2 and g3 then become
ready, producing:

```text
Front F:      {g1 = CX(A, C)}
Lookahead E:  {g2 = CX(A, B), g3 = CX(C, D)}
Later:        g4 = CX(B, D)
```

The real layout is still `[A, B, C, D]`, and the output still has no gates.
Lookahead is a prediction of which pairs will matter next, not a separate
attempt to route those gates. In this checkout the named `lookahead` and `decay`
modes use one such future layer. Internal configurations can use more.

### Step 4: enumerate nearby candidate SWAPs

A candidate must be a hardware edge touching at least one physical position
used by a front-layer interaction. Here A and C occupy p0 and p2:

| Active position | Its neighbors | Candidate edges contributed |
| --- | --- | --- |
| p0, holding A | p1 | (p0, p1) |
| p2, holding C | p1 and p3 | (p1, p2), (p2, p3) |

Thus there are three candidates. `(p0, p2)` is not an option: it is not a
hardware edge. SABRE cannot fix a nonlocal interaction by inserting an equally
nonlocal SWAP.

If two active positions touch the same edge, that edge is included once.
A distant edge touching neither front-layer endpoint is excluded. For example,
on a five-position line with this same front pair, `(p3, p4)` would be excluded.
It cannot change this front pair's distance in a single move. Restricting the
candidate set makes the search cheaper, but also means SABRE does not examine
every conceivable way to improve future gates.

### Step 5: calculate how each candidate changes the distances

For **each** candidate, ask what the layout would be if we applied only that
SWAP to the **same current layout**. These are competing alternatives, not three
SWAPs applied in sequence.

| Candidate | Hypothetical occupants [p0, p1, p2, p3] | Front distance d(A, C) | Future d(A, B) | Future d(C, D) |
| --- | --- | --- | --- | --- |
| No SWAP: current state | [A, B, C, D] | 2 | 1 | 1 |
| SWAP(p0, p1) | [B, A, C, D] | 1 | 1 | 1 |
| SWAP(p1, p2) | [A, C, B, D] | 1 | 2 | 2 |
| SWAP(p2, p3) | [A, B, D, C] | 3 | 1 | 1 |

For the first candidate, A moves from p0 to p1. Its partner C stays at p2, so
`d(A, C)` falls from 2 to 1. B moves to p0, but remains adjacent to A; the
future A–B gate is still easy. For the middle candidate, C and B exchange
positions: this helps A–C but separates **both** future pairs.

The **basic distance score** considers the front layer. With a unit weight,
its distance changes are:

```text
SWAP(p0, p1):  1 - 2 = -1
SWAP(p1, p2):  1 - 2 = -1
SWAP(p2, p3):  3 - 2 = +1
```

Smaller is better. A negative value means improvement; it is not a negative
number of physical gates. Basic distance alone ties the first two options.

For a front with several interactions, add the distance changes for **all** of
them. Suppose the occupants are `[A, B, C, D]` and independent pending gates are
`CX(A, C)` and `CX(B, D)`. SWAP(p1, p2) changes both distances from 2 to 1:
`(1 - 2) + (1 - 2) = -2`. It enables two gates with one SWAP. This is one reason
to score the whole front instead of routing one selected pair in isolation.

The code's `Layer::score` computes these **changes**, also called deltas, rather
than recomputing a whole new sum. Only partners of the swapped occupants can
have changed distance, so this saves work. To rank candidates with distance
alone, subtracting the same old sum from every candidate changes nothing.

### Step 6: let lookahead separate equally good immediate moves

Lookahead adds a smaller contribution from future distances. Define:

```text
D_F = sum of distances for the front layer
D_E = sum of distances for the lookahead layer
ΔD  = D after the candidate SWAP - D before it
```

For the named `lookahead` mode in this checkout:

```text
distance-change score = ΔD_F + w × ΔD_E
w = 0.5 / number_of_physical_qubits
```

Here `w = 0.5 / 4 = 0.125`, and the current future-distance sum is `1 + 1 = 2`.
There is no prior gate history yet, so this checkout's additional scoring terms
are zero for this decision.

| Candidate | ΔD_F | ΔD_E | Score = ΔD_F + 0.125 × ΔD_E |
| --- | --- | --- | --- |
| (p0, p1) | -1 | (1 + 1) - 2 = 0 | **-1.00** |
| (p1, p2) | -1 | (2 + 2) - 2 = 2 | -0.75 |
| (p2, p3) | +1 | (1 + 1) - 2 = 0 | +1.00 |

SABRE chooses **SWAP(p0, p1)**. It enables g1 and leaves g2 and g3 adjacent.
The competing middle SWAP also enables g1, but it would leave both g2 and g3
needing more routing.

This is the point of lookahead: choose a useful next move using a small preview
of later work. It does not solve the entire future circuit, and cannot guarantee
that its preference leads to the globally best result.

### Step 7: apply only the winner and update the mapping

Now the hypothetical choice becomes part of the route being constructed:

```text
Insert physical SWAP(p0, p1).

Physical:      p0 ---- p1 ---- p2 ---- p3
Before:         A      B       C       D
After:          B      A       C       D
```

The hardware graph and distance table stay fixed. The **logical-to-physical
mapping** changes, so the mapped pairs in every stored layer must change too.
The output's next `CX(A, C)` must use `(p1, p2)`, not its original `(p0, p2)`.

There is no measurement or quantum simulation in this calculation. The compiler
tracks names and positions; the eventual hardware SWAP moves the information.

### Step 8: emit newly executable gates and advance dependencies

G1 is now adjacent, so emit `CX(p1, p2)`. This unlocks g2 and g3:

| Newly available gate | Positions under [B, A, C, D] | Action |
| --- | --- | --- |
| g2 = CX(A, B) | (p1, p0) | Emit it: adjacent. |
| g3 = CX(C, D) | (p2, p3) | Emit it: adjacent. |
| g4 = CX(B, D), after both g2 and g3 | (p0, p3) | Keep it in the front: distance 3. |

G2 and g3 can run in parallel because they use disjoint positions. A printed
instruction list still needs to list one first; this does not impose a physical
dependency between them.

After this update:

```text
Layout:       [B, A, C, D]
Done:         g1, g2, g3
Front F:      {g4 = CX(B, D)}
Lookahead E:  empty
```

Notice that emitting ordinary gates did not alter the layout. Also, we did not
blindly move the entire old lookahead into the front: its adjacent gates were
immediately emitted, which exposed g4 instead.

### Step 9: repeat until the whole circuit is routed

B and D now occupy the ends of the line. Only `(p0, p1)` and `(p2, p3)` touch
the front's endpoints. Both reduce their distance from 3 to 2. Suppose the
seeded tie choice selects `(p0, p1)`.

This second SWAP returns the occupants to `[A, B, C, D]`, but **does not undo the
computation**: g1, g2, and g3 have already occurred between the two SWAPs. B at
p1 and D at p3 are still not adjacent, so there is no gate to emit yet. SABRE
must score again from this new state.

Now `(p1, p2)` and `(p2, p3)` both reduce B–D distance from 2 to 1. With only
distance and lookahead they tie. The other candidate, `(p0, p1)`, would move B
away again and increase the distance to 3. Choose `(p2, p3)` for this walkthrough;
the fusion term described below also favors it in this checkout. G4 then runs
on p1 and p2.

| Event | Occupants afterward | Physical operation emitted | What remains blocked? |
| --- | --- | --- | --- |
| Start | [A, B, C, D] | — | g1 at distance 2 |
| First SWAP | [B, A, C, D] | SWAP(p0, p1) | Nothing before g1 |
| Route g1 | [B, A, C, D] | CX(p1, p2) | g2 and g3 become ready |
| Route g2 | [B, A, C, D] | CX(p1, p0) | g4 still waits for g3 |
| Route g3 | [B, A, C, D] | CX(p2, p3) | g4 at distance 3 |
| Second SWAP | [A, B, C, D] | SWAP(p0, p1) | g4 at distance 2 |
| Third SWAP | [A, B, D, C] | SWAP(p2, p3) | Nothing before g4 |
| Route g4 | [A, B, D, C] | CX(p1, p2) | Nothing: finished |

Every emitted two-qubit operation uses a hardware edge, and every original gate
keeps its logical participants and dependencies. Those two properties establish
routing legality. The final mapping is `A -> p0, B -> p1, C -> p3, D -> p2`.

These are valid choices for this checkout, but a tie may produce a different
valid route. An output DAG may also list disjoint operations in a different
order from this explanatory trace.

### What decay adds: discourage repeatedly using the same physical wires

Distance and lookahead only examine current positions. **Decay** adds short-term
memory: each physical qubit has a factor starting at `1.0`. Selecting a SWAP
usually increases both endpoints' factors by `0.001`. A candidate is multiplied
by the larger factor of its two endpoints.

Suppose two candidates both have a weighted **post-SWAP absolute distance score**
of `2.5`. One touches a recently used position with factor `1.001`; the other
uses positions still at `1.0`:

```text
Recently used wires:  2.5 × 1.001 = 2.5025
Other wires:          2.5 × 1.000 = 2.5000  <- preferred
```

This encourages distributing SWAP work, which can create more opportunities for
parallel execution. It does not prohibit reusing a wire or guarantee a smaller
depth. "Decay" here is the name of a heuristic, not physical decoherence.

**Why use absolute distance here?** Multiplying a negative delta by `1.001`
would make it *more* negative, accidentally rewarding recent reuse. The code
therefore adds the current distance total back before applying decay. For our
first decision, that total is `2 + 0.125 × 2 = 2.25`. The three decay-mode scores
are `1.25`, `1.50`, and `3.25` when all factors are 1, so the winner is unchanged.

Factors reset to 1 after routing progress and at the configured five-search-SWAP
interval. Thus decay remembers recent use, not the full history of a circuit.
After a SWAP immediately enables a gate, the progress reset removes its decay
penalty before the next search segment. The ready-time tracker below retains
longer-term information.

### Addition in this checkout: credit for a SWAP that can fuse with earlier gates

Counting one SWAP as three native two-qubit gates can overestimate its extra cost
when later optimization can combine it with operations on the same physical pair.
**Fusion** here means resynthesizing a compatible sequence into a cheaper gate
sequence with the same overall quantum action.

For example, a SWAP can be decomposed as the following chronological CX sequence:

```text
SWAP(a, b) = CX(a, b); CX(b, a); CX(a, b)
```

If `CX(a, b)` immediately precedes it on those wires, the first two CX gates in
the combined sequence cancel:

```text
Before cancellation:  CX(a, b); CX(a, b); CX(b, a); CX(a, b)
After cancellation:                         CX(b, a); CX(a, b)
```

The combined block takes two CX gates instead of four. The router still emits a
SWAP; a later optimization pass must realize that saving.

This checkout tracks an estimated cost `c` for the most recent compatible block
on each physical pair, capped at 3. It models the cost after appending a SWAP as
`min(3, c + 1)`. The candidate gets a negative credit in nominal SWAP units:

```text
saving = (c + 3) - min(3, c + 1)
credit = -saving / 3
```

| Previous estimated block cost c | Separate block + SWAP | Estimated combined cost | Candidate credit |
| --- | --- | --- | --- |
| No eligible block | — | — | 0 |
| 1 | 4 | 2 | -2/3 |
| 2 | 5 | 3 | -2/3 |
| 3 | 6 | 3 | -1 |

The cap and these costs are a synthesis-oriented estimate; the scoring loop does
not actually synthesize every candidate block. They are not guarantees for an
arbitrary operation or target basis.

In our walkthrough, g2 leaves a one-CX block on `(p0, p1)` and g3 leaves one on
`(p2, p3)`. Both endpoint candidates for routing g4 initially get credit `-2/3`.
After the second SWAP on `(p0, p1)`, that pair's tracked block is cleared. The
block on `(p2, p3)` remains, because neither of its wires was touched.
Consequently, at the third SWAP decision:

| Candidate | Front distance change | Credit | Sum before the ready-time term, without decay |
| --- | --- | --- | --- |
| (p0, p1) | +1 | 0 | +1 |
| (p1, p2) | -1 | 0 | -1 |
| (p2, p3) | -1 | -2/3 | **-5/3** |

The fusion estimate favors `(p2, p3)`, explaining the last choice in our trace.

A block stays eligible only when **both** physical endpoints still name each
other as their most recent block partner. If p1 has since interacted with p2,
an old record on p0 mentioning p1 is insufficient. Standard barriers,
measurements, resets, and other block-ending operations invalidate the relevant
history in final routing; selected SWAPs clear their own endpoints' records.

The classifier is deliberately coarse. It does not fully check downstream
synthesis eligibility, including unbound parameters and opaque instructions.
A credited saving can therefore fail to materialize. Fusion affects a routing
preference, not the correctness of the emitted gate sequence. The exploratory
layout search has an additional block-boundary limitation described in section 8.

### Addition in this checkout: prefer wires that become available earlier

The array `ready[p]` estimates the end of the two-qubit work already assigned to
physical wire `p`. It starts at zero. A two-qubit operation needs both wires, so
its start estimate is their larger ready value.

This implementation updates it as follows:

```text
After choosing SWAP(a, b):
    end = max(ready[a], ready[b]) + 3
    ready[a] = ready[b] = end

After routing one compressed TwoQ node on (a, b):
    end = max(ready[a], ready[b]) + 1
    ready[a] = ready[b] = end
```

For the walkthrough, the values are easy to calculate because every routing
node contains one CX:

| Event | ready[p0] | ready[p1] | ready[p2] | ready[p3] |
| --- | --- | --- | --- | --- |
| Start | 0 | 0 | 0 | 0 |
| SWAP(p0, p1) | 3 | 3 | 0 | 0 |
| g1 on (p1, p2) | 3 | 4 | 4 | 0 |
| g2 on (p1, p0) | 5 | 5 | 4 | 0 |
| g3 on (p2, p3) | 5 | 5 | 5 | 5 |
| SWAP(p0, p1) | 8 | 8 | 5 | 5 |
| SWAP(p2, p3) | 8 | 8 | 8 | 8 |
| g4 on (p1, p2) | 8 | 9 | 9 | 8 |

G2 and g3 both finish at 5: a sequential compiler visit does not force disjoint
physical work to run sequentially. Likewise, the last two SWAPs can overlap in
this estimate. Ready values belong to **fixed physical wires**, so they are not
exchanged when logical occupants are swapped.

Each candidate pays this additional score:

```text
ready penalty = 0.000022 × max(ready[a], ready[b])
```

For candidate wires ready at `(8, 5)`, the penalty is `0.000176`. For wires ready
at `(5, 5)`, it is `0.000110`. The second starts earlier in the estimate and gets
the smaller penalty. This is an additive term on every candidate, not merely a
rule used after exact score ties. It can outweigh a sufficiently small lookahead
advantage.

The value is an **estimated start layer measured from the circuit's beginning**.
It is not a gate duration in seconds, a hardware error probability, or a measured
amount of idle time. Its small coefficient converts that estimate into score
units; it does not make the score equal to the resulting circuit's depth.

There are important implementation limits:

- One compressed `TwoQ` node adds only **one** layer, even if it contains many
  original two-qubit gates. Its fusion estimate can be 3 while this timing
  update is still 1; those fields represent different approximations.
- The tracker does not model one-qubit durations, calibrated hardware durations,
  or the timing of nested control flow.
- Synchronization nodes such as barriers clear block history but do not
  synchronize the ready values. For example, `[10, 10, 0, 0]` can remain unchanged
  after a global barrier even though subsequent operations on p2 and p3 must
  wait for work on p0 and p1. The estimate can therefore suggest parallelism
  that the actual circuit cannot use.
- A selected SWAP adds 3 to ready time even when fusion credit predicted a
  smaller native gate cost.

These limits explain why this is a routing signal, not a complete scheduler.

### Putting all scoring terms together

The following formulas describe the named modes in **this checkout**. Let:

| Symbol | Meaning |
| --- | --- |
| `ΔD_F`, `ΔD_E` | Changes in the summed front and lookahead distances. |
| `b` | Effective basic weight, after any front-size scaling. |
| `w` | Effective lookahead weight. |
| `f` | Fusion credit: 0, -2/3, or -1 in the cases above. |
| `H_old` | Current weighted absolute distances: `b × D_F + w × D_E`. |
| `m` | Larger decay factor of the candidate's two physical endpoints. |
| `T` | Larger ready value of the candidate's two physical endpoints. |

Then the code does, in this order:

```text
Start with fusion credit f.
Add front distance change and apply the basic weight:
    x = b × (f + ΔD_F)
Add lookahead:
    x = x + w × ΔD_E
If decay is enabled:
    x = (H_old + x) × m
Finally add ready time:
    score = x + 0.000022 × T
Choose a candidate with the smallest score.
```

An absent lookahead contribution is zero. With several internal lookahead
layers, sum their separately weighted distance changes. Notice that the basic
weight also scales fusion credit, while the ready penalty is added **after**
decay and is not multiplied by it.

For our third SWAP decision in `lookahead` mode, the complete comparison is:

```text
(p0, p1):  +1       + 0.000022 × 8 = +1.000176
(p1, p2):  -1       + 0.000022 × 8 = -0.999824
(p2, p3):  -1 - 2/3 + 0.000022 × 5 = -1.666556...  <- chosen
```

The exact wrapper settings are:

| Mode | Effective basic weight b | Lookahead | Decay |
| --- | --- | --- | --- |
| `basic` | `1 / number_of_front_nodes` | None | None |
| `lookahead` | 1 | One layer, weight `0.5 / number_of_physical_qubits` | None |
| `decay` | 1 | Same as `lookahead` | Increment 0.001; reset interval 5 search SWAPs |

`SetScaling::Constant` uses a configured weight unchanged. `SetScaling::Size`
divides it by the number of nodes in that particular layer. For example, the
two-front-gate distance improvement `-2` from step 5 becomes `-1` in the named
`basic` mode, because it averages over two nodes.

**All three modes include fusion and ready-time scoring here.** Selecting
`basic` does not disable those additions. The integrated `SabreLayout` path
uses the `decay` configuration above. These settings describe the local Python
wrappers, not universal constants from the original SABRE paper.

### Ties, repeated moves, and the fallback

Scores within `best_epsilon`, normally `1e-10`, are treated as effectively tied
by the selection loop. The seeded random generator chooses from the retained
best candidates. In the second SWAP decision above, the two candidates have the
same distance improvement, fusion credit, and ready time, so a tie is expected.

A lower heuristic score is a preference, not proof that a gate is now executable.
After every chosen SWAP, the router explicitly checks adjacency. If no front gate
has become executable, it repeats the candidate search. It can encounter a
local trap: improving the near future, seeking fusion credit, or moving around
similar layouts may repeatedly fail to enable any front gate.

The **release valve** puts a limit on such searching. The wrappers set
`attempt_limit = 10 × number_of_qubits`. If a search segment exceeds that limit
without enabling a gate, the router:

1. Undoes the tentative SWAPs since the last routing progress, in reverse order.
2. Restores the corresponding layout, layer mappings, ready times, and block
   history. Previously routed gates remain committed.
3. Selects a closest front-layer pair and finds a shortest hardware path between
   its endpoints.
4. Inserts SWAPs along that path, moving the two endpoints toward each other,
   then routes the newly adjacent interaction.

For a fallback example, let the required pair be A–E on this five-position line:

```text
Start:               [A, B, C, D, E]   A–E distance 4
SWAP(p0, p1):        [B, A, C, D, E]   A–E distance 3
SWAP(p1, p2):        [B, C, A, D, E]   A–E distance 2
SWAP(p4, p3):        [B, C, A, E, D]   A–E distance 1
Now emit the A–E gate on p2 and p3.
```

The path supplies three necessary moves for that isolated pair; other unrelated
routing choices are temporarily ignored. On a connected component this gives
the algorithm a concrete way to make progress. A shortest-path fallback cannot
create a connection between disconnected pieces of hardware.

Undoing the tentative SWAPs is a **classical compiler rollback**. They had not
been sent to a quantum processor, so rollback does not add inverse SWAPs to the
final physical circuit.

### The whole loop, now with meaning attached

```text
Prepare the dependency graph, distances, layout, and empty history.
Emit all currently executable nodes; build front and lookahead layers.

While a blocked front-layer interaction remains:
    Save the point reached after the last emitted gates.
    Until at least one front interaction becomes adjacent:
        Enumerate hardware-edge SWAPs touching the front.
        Score each hypothetical SWAP from the current state.
        Select one; update the mapping, layer data, and history.
        If the unsuccessful-search limit is exceeded:
            Roll back this segment and force a shortest-path route.

    Record the selected SWAPs and newly executable operations.
    Satisfy their dependencies; eagerly emit any unlocked adjacent gates.
    Update front/lookahead, ready times, block history, and decay.

Return the route and its initial and final mappings.
```

SABRE avoids searching all complete routes by making one inexpensive, informed
choice at a time. That is why it is a **heuristic**: it is designed to find useful
routes efficiently, with no guarantee of minimum SWAP count or minimum depth.

For readers following the Rust implementation, the state has these roles:

| State field | What it remembers |
| --- | --- |
| `layout` | Current virtual-to-physical mapping. |
| `layers` | Physical partners for current front and lookahead interactions. |
| `predecessors` | Unsatisfied dependency-edge counters for each layer traversal. |
| `decay` | Recent physical-wire use in selected SWAPs. |
| `ready` | Estimated completion layer of two-qubit work on each physical wire. |
| `last_block` | Recent potentially fusible block partner and cost on each wire. |
| `ready_undo` | Previous ready times and block records for rollback. |
| `swap_scores`, `best_swaps` | Scratch storage for candidates and tied best choices. |
| `rng`, `seed` | Reproducible randomized choices and recursive routing. |

## 7. Multiple trials and reconstruction

`swap_map` runs several routing trials, potentially in parallel. It picks the
result with the fewest **top-level inserted swaps**, using trial index to break a
tie. It does not choose the winner by measured native two-qubit depth or by the
sum of candidate scores.

This distinction matters: adding a depth-related term changes how each trial
constructs its route, while the final trial selection still uses swap count.

For example, suppose three trials produce these results. The depth column is an
illustrative measurement made afterward, not an input to `swap_map`:

| Trial index | Inserted top-level SWAPs | Later measured two-qubit depth | Selected? |
| --- | --- | --- | --- |
| 0 | 5 | 12 | No: more SWAPs. |
| 1 | 4 | 15 | **Yes:** fewest SWAPs, then earliest trial index. |
| 2 | 4 | 11 | No: ties trial 1 on the criterion the code actually uses. |

Thus "best next SWAP" and "best completed trial" are different comparisons.
The router does not add up its heuristic scores and call that a route's final
cost. Multiple trials explore different tie choices; they increase the chance
of finding a useful route without proving optimality. For reproducible examples,
set both the seed and trial count, since a wrapper's default trial count can
depend on the available CPUs.

`RoutingResult::rebuild` and `rebuild_onto` then replay the recorded order. They
emit SWAP gates, update the mapping, and place the original operations on the
appropriate physical wires. Later compilation passes can decompose SWAPs and
optimize neighboring gates.

The final layout may differ from the initial layout. Ordinary top-level routing
does not require an extra sequence of swaps to put every state back. Instead,
Qiskit records the resulting permutation so later compilation and output
interpretation can account for where the logical information ended up.

For the walkthrough's final occupants `[A, B, D, C]`, a measurement of logical C
must measure **p3**, while a measurement of logical D must measure **p2**. If
the original circuit measures C into classical bit `c2`, reconstruction maps
the quantum operand to p3 and retains the classical destination `c2`. If you
inspect an unmeasured physical state directly, use the final layout to interpret
its wires. Reading p2 as though it still represented C would be a mistake.

To inspect costs, distinguish three stages. Our four-CX example emits three
high-level SWAPs and four CX gates. Replacing each SWAP by three CX gates gives
`3 × 3 + 4 = 13` CX gates **before optimization**. Later fusion can reduce that
number. Counting the seven high-level operations is neither the native
two-qubit gate count nor the native two-qubit depth.

## 8. How SABRE also chooses the initial layout

Starting A and C next to each other could avoid the example's first SWAP entirely.
`layout.rs` searches for placements that help the whole circuit, rather than just
its first gate.

### Why the final layout of a forward route is useful

A forward route moves information toward interactions near the circuit's end.
Its final layout therefore contains information about what that end needs. But
using it directly as the next initial layout could make the beginning awkward.
A backward traversal starts with the end's needs and works back toward the
beginning, changing the mapping along the way.

The mappings are passed between exploratory routes like this:

```text
Starting guess L0
    -- route dependencies forward --> final layout L1
    -- route dependencies backward -> final layout L2

Use L2 as the next starting guess, or as the initial layout for final routing.
```

Here "backward" means reversing the **dependency graph's direction**. The
compiler is exploring pair interactions, not asking the hardware to execute
time backward. It does not invert CX matrices, undo measurements, or construct
the inverse quantum program.

### A forward/backward pair using our four-qubit circuit

The forward walkthrough started with `[A, B, C, D]`, inserted three SWAPs, and
ended with `[A, B, D, C]`. Now start an exploratory reverse route from that final
layout. The reversed dependencies ask for g4 first, then g2 and g3, then g1:

| Exploratory action | Occupants afterward | Why this works |
| --- | --- | --- |
| Start from the forward route's final layout | [A, B, D, C] | No physical operation is needed to pass a mapping between compiler trials. |
| Visit g4's B–D pair | [A, B, D, C] | B at p1 and D at p2 are adjacent. |
| Visit g2's A–B and g3's C–D pairs | [A, B, D, C] | Both pairs are adjacent; the reverse dependency order permits them now. |
| Reach g1's A–C pair | [A, B, D, C] | A at p0 and C at p3 have distance 3. |
| Choose SWAP(p0, p1) | [B, A, D, C] | A moves one step toward C. |
| Choose SWAP(p2, p3) | [B, A, C, D] | C moves next to A; g1's pair is now adjacent. |

As before, the displayed moves choose one allowed resolution of a tie. The
reverse route ends with the candidate **initial** layout `[B, A, C, D]`.
Run the original circuit forward from there:

```text
Start at [B, A, C, D].
g1: A–C is adjacent; emit it.
g2: A–B is adjacent; emit it.
g3: C–D is adjacent; emit it.
g4: B–D still needs the two endpoint SWAPs from the earlier walkthrough.
```

The final route now uses **two SWAPs instead of three**, because choosing the
initial placement avoids the first physical move. The three exploratory forward
SWAPs and two exploratory backward SWAPs are compiler search work; they are
discarded, not prepended to the output circuit.

This example shows how the backward route makes the beginning easier again after
starting from the forward route's ending. Larger circuits can benefit from
several forward/backward pairs, but improvement on every iteration is not
guaranteed. Each pass makes heuristic choices.

### What one layout trial actually runs

For each starting-layout trial, `layout_trial` performs these steps:

1. **Complete a starting mapping.** Fill unspecified positions randomly and
   include extra unused physical positions through ancilla allocation. For
   example, if only `A -> p2` is supplied on a four-position device, B, C, and D
   are assigned to the remaining p0, p1, and p3 without collisions. A supplied
   partial layout seeds the search; its assignments need not remain fixed after
   exploratory routing.
2. **Route forward once.** Keep the final mapping and discard the exploratory
   emitted sequence.
3. **Route backward once.** Begin with the forward result, keep this route's
   final mapping, and discard its exploratory emitted sequence too.
4. **Repeat the pair `max_iterations` times.** The default is 3, meaning
   **three forward and three backward passes**, not three individual traversals.
   Each individual route starts fresh decay, ready-time, and block history;
   the mapping is what passes between routes.
5. **Route the original circuit for the output.** Use the refined starting mapping
   in `swap_trials` final routing trials and retain the best route by SWAP count.

`layout_trials` controls the number of random starting-layout trials.
`swap_trials` controls final routing trials for each refined layout. Those
controls are separate from `max_iterations`. Increasing them does more
classical search, not more execution on the quantum device.

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
block open, so the fusion credit there can be granted where final routing would
reject it. For example, a separate barrier between a prior CX block and a SWAP
can leave that block credited in an exploratory pass. Block-ending operations
that were folded *into* a two-qubit node are not affected, because they are
already recorded in that node's `block` value.

## 9. Control flow and disconnected devices

These cases extend the basic loop. They are easiest to understand after the
straight-line circuit example above.

A circuit can contain classical control flow, such as an `if` statement depending
on a measurement. `route_control_flow_block` routes a nested block recursively
and appends swaps to restore its entry layout. This lets the outer circuit
continue with a consistent mapping regardless of the branch taken.

For example, suppose a branch enters with `[A, B, C]`. Its true arm needs
`CX(A, C)`, routes with SWAP(p0, p1), and finishes its useful gates with
`[B, A, C]`. The branch then needs a restoring SWAP to return to `[A, B, C]`.
An empty false arm already has that mapping. After the conditional, the outer
router can interpret A as p0 in either case. These restoring SWAPs are part of
the output branch, unlike the discarded compiler search moves in section 8.

Each nested routing call has its own state. The outer ready-time tracker does not
accumulate an exact duration for nested branches. Control flow is therefore one
reason the ready-time values should be understood as a routing estimate, not a
complete schedule.

For disconnected hardware, `layout.rs` uses the surrounding disjoint-layout
helpers to place compatible circuit components in hardware components, then maps
the results back to full-device indices. A required interaction cannot be routed
across a missing physical connection. For all-to-all connectivity, no routing
swaps are needed, and the entry points have fast paths.

For example, if the device consists of `p0 -- p1` and, separately, `p2 -- p3`,
independent pairs A–B and C–D can occupy those two components. But a circuit
connecting A–B–C through successive interactions needs all three logical qubits
in one connected hardware component. Neither two-position component can hold
it, and no amount of SWAP searching can bridge the missing connection.

## 10. Python examples you can run

These examples use Qiskit built from this checkout, including its compiled Rust
extension. A source checkout without that extension cannot run them. A different
Qiskit release may use different heuristics or resolve seeded choices differently.

### One gate that needs one SWAP

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
no fusible block is open. The seeded random choice decides, and this build with
`seed=7` picks (1, 2). A different choice can be equally correct.

This example only runs routing. A high-level `swap` still appears as one operation
in the output. Counting its operations or taking its depth before native-gate
translation does **not** directly give the benchmark's native `N2` or `D2`.

### Run the four-qubit walkthrough

This is a complete, separate example. A, B, C, and D are integer circuit-wire
indices; `SabreSwap` starts with the identity layout because these wires already
represent the four physical positions.

```python
from qiskit import QuantumCircuit
from qiskit.transpiler import CouplingMap
from qiskit.transpiler.passes import SabreSwap

A, B, C, D = range(4)
circuit = QuantumCircuit(4)
circuit.cx(A, C)  # g1
circuit.cx(A, B)  # g2
circuit.cx(C, D)  # g3
circuit.cx(B, D)  # g4

hardware = CouplingMap.from_line(4)
routed = SabreSwap(
    hardware, heuristic="lookahead", seed=0, trials=1
)(circuit)

print("SWAPs:", routed.count_ops().get("swap", 0))
print("Final logical-to-physical:", routed.layout.final_index_layout())
for instruction in routed.data:
    positions = [routed.find_bit(q).index for q in instruction.qubits]
    print(instruction.operation.name, positions)
```

Output from the checked build:

```text
SWAPs: 3
Final logical-to-physical: [0, 1, 3, 2]
swap [1, 0]
cx [1, 2]
cx [1, 0]
swap [1, 0]
cx [2, 3]
swap [2, 3]
cx [1, 2]
```

The final array lists physical positions for logical **A, B, C, D**, in that
order. Its third entry, 3, means C ends at p3. This array is the inverse viewpoint
of the occupant rows used earlier, which list logical names by physical position.

SWAP's operand order is irrelevant: `[1, 0]` is the same exchange as `[0, 1]`.
CX's operand order is relevant: `cx [1, 0]` has control p1 and target p0.
The printed second SWAP appears before `cx [2, 3]`, whereas our table puts g3
first. They use disjoint physical pairs, so those orders represent the same
dependencies and computation.

### Let SABRE choose the initial layout too

Continue from the preceding example's `circuit` and `hardware`:

```python
from qiskit.transpiler import PassManager
from qiskit.transpiler.passes import SabreLayout

compiled = PassManager([
    SabreLayout(
        hardware,
        seed=0,
        max_iterations=1,
        layout_trials=1,
        swap_trials=1,
    )
]).run(circuit)

print("SWAPs:", compiled.count_ops().get("swap", 0))
print("Initial:", compiled.layout.initial_index_layout(filter_ancillas=True))
print("Final:", compiled.layout.final_index_layout())
```

Output from the checked build:

```text
SWAPs: 2
Initial: [1, 0, 2, 3]
Final: [0, 1, 3, 2]
```

`Initial: [1, 0, 2, 3]` means A starts at p1 and B starts at p0: the occupants
are `[B, A, C, D]`. This saves the first SWAP, as in section 8. Setting
`layout_trials=1` requests one **random** starting-layout trial; the integrated
search also considers its heuristic starting layouts. The example therefore
demonstrates the resulting improvement, without claiming that the internal
search visits only the hand-worked starting guess.

`SabreLayout`'s default integrated path returns a routed circuit here. We have
not run the full native-basis optimization pipeline, so its two high-level SWAPs
still need translation for the target device.

## 11. Suggested code-reading order

Start with `swap_map_trial` for the overall loop, then `choose_best_swap` for the
decision rule. Read `Layer::score` to see the distance calculation, and
`advance_routed` / `advance_swap` to see the new history tracking. Next read
`SabreDAG::from_dag` for graph compression and `layout_trial` for initial-layout
search. Leave reconstruction and control-flow details until those pieces make
sense.

For the change's motivation, formulas, worked examples, benchmark interpretation,
and limitations, continue to [SABRE_CHANGE_EXPLAINED.md](SABRE_CHANGE_EXPLAINED.md).

## 12. Check your understanding

Try answering before reading the explanation after each question.

**1. G2's qubits are already adjacent at the start. Why not execute it first?**

Because g2 shares A with g1 and follows g1 in the dependency graph. Adjacency is
only one condition for execution. Its predecessors must also be satisfied.
Lookahead may score g2's distance while g2 still cannot execute.

**2. On `p0 -- p1 -- p2 -- p3 -- p4`, the only front gate uses p0 and p4.
Is `(p1, p2)` a candidate?**

No. Neither endpoint belongs to the front pair. The local candidates are
`(p0, p1)` and `(p3, p4)`. After selecting one, the active positions change and
the candidate set is generated again. The algorithm can reach an interior edge
through successive decisions.

**3. Why not always move one partner along its shortest path?**

That solves an isolated pair, but may separate other pending or future partners.
In the worked example, swapping p1 and p2 enables g1 but makes both g2 and g3
nonadjacent. Swapping p0 and p1 enables g1 while keeping both future pairs
adjacent. The fallback uses shortest paths when the broader heuristic fails to
make progress.

**4. Does inserting SWAP(p0, p1) twice necessarily cancel the two SWAPs?**

Only if the intervening operations allow that cancellation. In our walkthrough,
gates use A and B in their changed positions between the exchanges. Removing
both SWAPs while leaving those physical gates unchanged would change the
computation. A compiler must prove any proposed gate simplification separately.

**5. Does the ready-time value 9 at the end prove that the optimized circuit
has native two-qubit depth 9?**

No. It is the result of a particular approximate accounting rule. This example
has a corresponding unoptimized three-CX-per-SWAP schedule, but synthesis and
fusion may change it. More generally, compressed nodes, barriers, and control
flow can also make the estimate differ from a real schedule.

**6. Why does layout search route backward if the output runs forward?**

It is collecting a better starting mapping. The forward route adapts the layout
to the circuit's end; the backward route carries that placement back through
the earlier interactions. Only the resulting mapping is retained. Final routing
then processes the original forward circuit with its actual operations.

**7. Can a route with fewer SWAPs still have more native gates or greater depth?**

Yes. Fusion can make different SWAPs have different incremental gate costs, and
work on independent pairs can overlap. This checkout still selects completed
trials by top-level SWAP count, so its selected route is not guaranteed to win
those other metrics.

### Review checklist for a hand-worked route

When checking your own example, verify four things:

1. Every inserted SWAP and emitted two-qubit gate uses a hardware edge.
2. Each gate's physical operands correspond to its original logical operands
   under the mapping **at that point**, including CX control/target order.
3. No original dependency is violated, even if independent gates are reordered.
4. The final layout records every exchange, and output measurements or state
   interpretation use that layout correctly.

These checks establish routing correctness. Comparing SWAP count, native gate
count, or depth is a separate assessment of route quality. A poor heuristic
choice can produce a valid circuit with unnecessary work.
