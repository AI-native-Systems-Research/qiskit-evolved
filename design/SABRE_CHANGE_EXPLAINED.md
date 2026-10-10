# Explaining the SABRE change in `RATIONALE.md`

The change makes SABRE consider two additional questions when choosing a SWAP:
**Can this SWAP share an optimized implementation with earlier gates? How early
can its physical wires be available?** The first can reduce native two-qubit gate
count; the second can improve opportunities for parallel execution.

This document expands [`RATIONALE.md`](RATIONALE.md) for a reader new to quantum
computing and Qiskit. Start with [the SABRE guide](SABRE_GUIDE.md) for the basic
concepts and folder structure.

The current checkout already implements the described `READY_WEIGHT = 2.2e-5`
rule. Implementation statements below are based on the local Rust code and Python
tests, and the worked examples were checked by running a build of this commit;
section 12 lists what was run. Benchmark numbers are **reported by the
rationale**, not independently reproduced for this documentation. Historical attempt names are labels from that
document, not a claim that their full code histories were compared here.

## 1. What changes, and relative to which baseline?

The rationale describes several stages of an experiment. Keeping them separate
avoids confusing the entire enhancement with its latest adjustment.

| Stage | How a candidate SWAP is evaluated |
| --- | --- |
| Original baseline, called the **seed** | Uses the configured basic distance, lookahead, and decay ingredients, then randomly selects among approximately tied candidates. |
| Intermediate approach, associated with `cc01391e` | Adds fusion credit and uses estimated ready time as an exact preference within the best-score band. |
| Priced-wait approach, `49dfeb40` | Adds ready time directly to the score with weight `1.5e-5`. |
| Current approach described by the rationale | Uses the same priced-wait rule, increasing its weight to `2.2e-5`. |

The references to "captain-nemo" and "captain-ahab" identify experiment
provenance. They are not concepts you need to understand the algorithm. The
rationale calls the fusion term a **NASSC credit**; here that means the
resynthesis-aware gate-saving credit explained below. NASSC abbreviates "Not All
SWAPs have the Same Cost", the title of a 2022 paper by Liu, Li, and Zhou on
optimization-aware routing. Its idea is the one used here: what a SWAP really
costs depends on what later optimization can merge it with.

"Seed" is overloaded: the **seed baseline** is an earlier implementation, while
a **random seed** is a number controlling randomized trial choices.

Relative to the original baseline, the full enhancement adds block-cost and
ready-time information. Relative to the intermediate approach that already tracks
both, the new mechanism is using ready time as a continuously priced term rather
than consulting it only when scores tie. Relative to `49dfeb40`, the latest change
is the weight itself.

## 2. Why distance alone misses useful information

Suppose two candidate swaps both bring a pending pair from distance 2 to distance
1. They are equally good for the immediate interaction. However:

- One may sit directly after a compatible gate on the same physical pair, making
  the combined native implementation cheaper.
- One may touch a wire occupied by a long chain of previous interactions, while
  the other can overlap with that chain on idle wires.

A distance score cannot distinguish these effects. Lookahead considers future
distances, and decay discourages recent repeated swap use, but neither explicitly
represents the last fusible block or the wire's accumulated ready time.

The improvement does not change the circuit's requested quantum computation. It
changes which legal routing path SABRE chooses to realize that computation.

## 3. Fusion credit: estimating how many native gates a SWAP adds

### What fusion means

A compiler can collect consecutive compatible operations on the same two qubits
and **resynthesize** them: replace the group with a different gate sequence that
implements the same combined quantum transformation. Such a group is a **block**.

For a concrete example, a SWAP can be implemented with this chronological CX
sequence:

```text
CX(a, b), CX(b, a), CX(a, b)
```

If a preceding gate is `CX(a, b)`, their combined sequence becomes:

```text
CX(a, b), CX(a, b), CX(b, a), CX(a, b)
```

The first two identical CX gates cancel. The whole block can use two CX gates
instead of four. Thus, "insert one SWAP" does not always mean "add three native
two-qubit gates after optimization."

The router itself still emits the SWAP. The saving is an estimate that depends
on later compilation performing suitable block optimization and synthesis.

### The credit formula

Let `c` be the estimated native two-qubit gate count of the preceding fusible
block, capped at 3.

The cap comes from a standard result: any operation on two qubits can be rebuilt
from at most three CX gates plus one-qubit gates. However many gates a block on
one pair contains, resynthesis never needs more than three for it. The model also
assumes that merging a SWAP into a block costs one more gate, up to that cap.

The model estimates:

```text
Separate cost:            c + 3
Combined block cost:      min(3, c + 1)
Incremental SWAP cost:    min(3, c + 1) - c
Saved native gates C:     3 - (min(3, c + 1) - c)
Candidate credit:         -C / 3
```

| Previous block `c` | Separate cost | Estimated fused cost | Saved gates `C` | Score credit |
| --- | --- | --- | --- | --- |
| 1 | 4 | 2 | 2 | `-2/3` |
| 2 | 5 | 3 | 2 | `-2/3` |
| 3 | 6 | 3 | 3 | `-1` |

The negative sign makes a candidate more attractive because SABRE minimizes the
score. Dividing by 3 expresses the saving in units of one nominal three-gate
SWAP. This is the rationale's conversion from an expression like
`3 * sum_of_distances - C` to `sum_of_distances - C/3`.

These are heuristic estimates, not exact synthesis results for every gate block
and target. In particular, the model does not inspect a target's calibrated
durations or solve the candidate block's synthesis problem inside the scoring loop.

### How the code tracks eligibility

In [`dag.rs`](../crates/transpiler/src/passes/sabre/dag.rs), `block_gates`
classifies each operation. The rows are checked from top to bottom:

| What the operation is | Estimate | Meaning |
| --- | --- | --- |
| Control flow, a standard instruction (barrier, delay, measure, or reset), a classical `store`, or a Pauli-product measurement | `None` | Ends the block. |
| Any other operation on one qubit | `Some(0)` | Adds no two-qubit gates. |
| `CX`, `CY`, `CZ`, or `ECR` | `Some(1)` | One native two-qubit gate. |
| Any other operation on two qubits, such as `RZZ`, a SWAP already in the input, or a custom unitary | `Some(3)` | Assumed to need all three. |
| Anything else: no qubits, or more than two | `None` | Ends the block. |

`Some(x)` means the estimate exists; `None` means the block cannot remain open.
This classifier is a lightweight proxy, not a full proof that an arbitrary
custom operation can be synthesized.

When nodes are folded together, their block estimates are added and capped at 3;
a block-ending operation makes the merged estimate `None`. `only_interactions`
preserves this metadata, so it remains available during layout search.

In [`route.rs`](../crates/transpiler/src/passes/sabre/route.rs),
`last_block[a] = Some((b, c))` records a recent block on physical pair `(a, b)`.
`choose_best_swap` checks that both endpoints still refer to each other. This
mutual check matters if one endpoint has since interacted with someone else.

`advance_routed` updates these records each time a node is routed. For a
two-qubit node on physical pair `(a, b)`:

- If the two wires' records point at each other, the block is still open. The
  node's estimate is added to it, capped at 3.
- Otherwise a new block starts from the node's own estimate.
- If the node's estimate is `None`, both records are cleared.

Any other routed node, meaning a synchronization point or control flow, clears
the records of the wires it touches. `SabreLayout`'s exploratory passes are an
exception to that last rule, explained in
[the guide](SABRE_GUIDE.md#8-how-sabre-also-chooses-the-initial-layout).

`advance_swap` clears the records for its endpoints after saving their old
values for possible rollback. The tracker does not try to resynthesize an
unlimited history of successive inserted swaps.

### The credit is a large term

A credit is `-2/3` or `-1`. Moving a front-layer pair one step closer is also
worth `-1`, so the credit is measured against real routing progress, not against
the small refinements. Three consequences follow:

- **It normally beats lookahead.** The standard lookahead weight is `0.1` per
  unit of distance on a 5-qubit device and about `0.0026` on a 193-qubit one.
- **It can tie with real progress.** A swap with credit `-1` that leaves the
  front-layer distance unchanged scores the same as an uncredited swap that
  brings a pair one step closer. The smaller terms then decide.
- **It shrinks in `basic` mode.** The credit sits inside the basic weight, which
  in that mode is `1 / front_layer_size`. With two gates in the front layer, a
  `-2/3` credit contributes `-1/3`. The distance term shrinks by the same factor,
  so their ratio is unchanged.

The existing test `test_lookahead_mode` shows the first consequence. The device
is the line `0 - 1 - 2 - 3 - 4`, the mode is `lookahead` with weight
`0.5 / 5 = 0.1`, and the circuit is:

```text
cx(0, 1)   cx(2, 3)   h(0)   cx(1, 2)     <- all executable immediately
cx(1, 3)                                  <- needs routing
cx(2, 3)   cx(1, 3)                       <- come afterwards
```

When `cx(1, 3)` needs routing, the candidates score as follows. `T` is the
estimated start layer from section 5.

| Candidate | Credit | Front change | Lookahead change × 0.1 | `T` × 0.000022 | Score |
| --- | --- | --- | --- | --- | --- |
| SWAP(1, 2) | `-2/3`, because it directly follows `cx(1, 2)` | `-1` | `+0.1` | `2 × 0.000022` | `-1.56662` |
| SWAP(2, 3) | 0 | `-1` | 0 | `2 × 0.000022` | `-0.99996` |
| SWAP(0, 1) | 0 | `+1` | 0 | `2 × 0.000022` | `+1.00004` |
| SWAP(3, 4) | 0 | `+1` | `+0.1` | `1 × 0.000022` | `+1.10002` |

SWAP(2, 3) is the better move for lookahead: it also serves the next gate,
`cx(2, 3)`, so the whole circuit needs only one SWAP. Before the change that is
what the router chose, and the test expected 7 two-qubit operations: 6 CX and 1
SWAP. Now SWAP(1, 2) wins on its credit, and a second SWAP is needed afterwards.

| Routing | SWAPs | CX after block resynthesis | Two-qubit depth after resynthesis |
| --- | --- | --- | --- |
| One SWAP, the choice before the change | 1 | 9 | 8 |
| Two SWAPs, the choice now | 2 | 8 | 7 |

The routing with more SWAPs is cheaper in native gates, because both of its SWAPs
merge into neighboring blocks. Two lessons follow:

- The number of SWAPs is no longer what each decision minimizes. It is still how
  `swap_map` picks the winning trial, as section 9 explains.
- This is why the fusion credit changed the pinned expectations of several
  existing tests, which the rationale lists.

## 4. The exact order of scoring

The implementation is more specific than "distance minus credit plus wait."
This section follows `State::choose_best_swap`.

For a candidate `s = (a, b)`, define:

| Symbol | Meaning |
| --- | --- |
| `k(s)` | Fusion credit, either 0 or `-C/3`. |
| `ΔF(s)` | Change in the sum of front-layer distances if the swap is applied. |
| `ΔL_i(s)` | Corresponding change for lookahead layer `i`. |
| `B` | Effective basic weight, including any layer-size scaling. |
| `W_i` | Effective weight of lookahead layer `i`. |
| `A` | Current weighted absolute distance sum across the enabled basic/lookahead layers. |
| `D(s)` | `max(decay[a], decay[b])`. |
| `T(s)` | `max(ready[a], ready[b])`. |
| `w` | `READY_WEIGHT`, currently `0.000022`. |

With the basic component enabled, the candidate's initial accumulated score is:

```text
q(s) = B * (k(s) + ΔF(s)) + sum_i(W_i * ΔL_i(s))
```

Then:

```text
Without decay:   h(s) = q(s)
With decay:      h(s) = (A + q(s)) * D(s)

Final score:     score(s) = h(s) + w * T(s)
```

For an unusual custom heuristic without a basic component, the initial `k(s)`
is retained without multiplication by `B`, before any lookahead is added.

Three implementation details follow:

1. The fusion credit is inside the basic scaling when basic is enabled and inside
   the decay multiplication when decay is enabled.
2. The ready-time term is added **after** decay, so decay does not multiply it.
3. Without decay, the common absolute distance sum can be omitted because it is
   the same for every candidate. With candidate-dependent decay factors, the code
   restores it before multiplying.

The new term is applied in the final minimum-selection loop:

```rust
let score =
    score + READY_WEIGHT * f64::from(self.ready[swap[0]].max(self.ready[swap[1]]));
```

The epsilon-band comparison and seeded random choice still follow. The algorithm
has no strict rule to prefer ready time regardless of distance or lookahead.
All enabled ingredients compete through the final score.

## 5. Ready time: estimating when a physical pair is free

`ready[q]` is an integer associated with a **physical** wire. It starts at zero.
The update resembles an **ASAP**, or "as soon as possible," schedule: start a
two-qubit operation when both participating wires are available.

For a routed two-qubit SABRE node on physical wires `(a, b)`:

```text
start = max(ready[a], ready[b])
finish = start + 1
ready[a] = ready[b] = finish
```

For an inserted routing SWAP:

```text
start = max(ready[a], ready[b])
finish = start + 3
ready[a] = ready[b] = finish
```

For example, with `ready[a] = 8` and `ready[b] = 3`, a SWAP starts at estimated
layer 8 and finishes at 11. It cannot start at layer 3 because wire `a` is busy.
An independent operation on two other wires can start earlier.

The `ready` values stay attached to physical positions; they are not simply
exchanged when the logical states swap. The SWAP consumes both physical wires
and updates both finish times.

### Why this is only an estimate

`advance_routed` adds one for a **SABRE two-qubit node**, which can represent
several original operations folded together. It does not add one separately for
every native gate inside that node. For example, a run of same-pair CX gates can
be compressed to one interaction node for routing purposes.

The tracker also:

- Adds three for a SWAP even when the separate fusion model predicts a cheaper
  implementation. Fusion credit does not reduce `SWAP_DEPTH`.
- Does not model actual gate durations, single-qubit durations, measurement
  latency, or all synchronization constraints.
- Uses separate states for nested control-flow blocks; their execution times do
  not become an exact outer ready-time schedule.

The candidate term prices its estimated absolute **start layer**, not the length
of an exact hardware idle interval. "Wait" is the rationale's convenient name for
this preference for earlier-available wires.

### Why rollback must restore history

When the release valve abandons unsuccessful swaps, it restores the layout and
layer state. It must also remove the swaps' artificial timing and block effects.
Otherwise later candidates would be penalized for swaps that will never appear
in the output, or lose fusion opportunities that still exist.

`ready_undo` saves `(physical_qubit, old_ready, old_last_block)` before modifying a
swap endpoint. Popping entries in reverse order restores both pieces of history.
Forced shortest-path swaps then advance the tracker in the normal way.

## 6. Why a priced term works differently from a tie-break

An exact tie-break only helps when two candidates' other scores are already equal
within the tolerance. A tiny lookahead advantage can exclude the earlier-starting
candidate before its ready time is ever considered.

Consider two hypothetical candidates after the other score components are
combined. Neither has a fusion advantage:

| Candidate | Existing score `h` | Estimated start `T` | Added wait price | Final score |
| --- | --- | --- | --- | --- |
| Busy wires | `1.0000` | 20 | `0.00044` | `1.00044` |
| Idle wires | `1.0001` | 0 | `0` | `1.00010` |

With an epsilon of `1e-10`, these existing scores are not tied. A band-only
tie-break chooses the busy candidate because `1.0000` is smaller than `1.0001`.
Pricing the wait chooses the idle candidate instead.

If the idle candidate's existing score were `1.01`, its larger disadvantage
would still win out over the `0.00044` wait saving. This is a tradeoff rather than
an unconditional "always use idle wires" rule.

At exactly equal existing scores, a one-layer start difference contributes
`2.2e-5`, much larger than the default epsilon, so it breaks that tie toward the
earlier candidate. This comparison assumes the default epsilon; a custom, larger
epsilon can change that behavior.

## 7. A worked example from the regression test

The test `test_wire_ready_time_is_priced_against_lookahead` in
[`test_sabre_swap.py`](../test/python/transpiler/test_sabre_swap.py) uses this graph:

```text
0 ---- 1 ---- 2 ---- 3 ---- 4
       |
       5
```

Starting with the identity layout, the circuit executes ten repetitions of:

```text
CX(0, 1)
CX(5, 1)
```

Then it requests:

```text
CX(1, 3)
CX(3, 4)
```

The first twenty gates all share wire 1, so their two-qubit work is sequential.
Alternating its partner between 0 and 5 prevents the interaction-graph builder
from merging the whole run into one same-pair node. This makes the estimated
ready time on wire 1 reach 20 in this example.

To route `CX(1, 3)`, the router scores the five hardware edges that touch
physical qubit 1 or 3. Three of them move a qubit the wrong way, so their
front-layer distance gets worse by 1 and they are out of the running:

| Candidate | Why it loses |
| --- | --- |
| SWAP(0, 1) | Moves logical qubit 1 away from qubit 3. |
| SWAP(1, 5) | Moves logical qubit 1 away from qubit 3. It does earn a `-2/3` fusion credit, because `CX(5, 1)` was the last block on that pair, but that does not make up for the lost distance. |
| SWAP(3, 4) | Moves logical qubit 3 away from qubit 1. |

That leaves two real contenders:

| Candidate | Immediate distance | Next `CX(3, 4)` distance | Estimated start |
| --- | --- | --- | --- |
| SWAP(1, 2) | Improves from 2 to 1 | Stays 1: logical qubit 3 remains at physical 3 | 20 |
| SWAP(2, 3) | Improves from 2 to 1 | Becomes 2: logical qubit 3 moves to physical 2 | 0 |

Both have the same basic distance improvement and neither has a fusion credit
on its candidate pair. The test supplies a custom heuristic without decay, so
the tradeoff is particularly easy to calculate.

With lookahead weight `0.0001`, the delta scores are:

```text
SWAP(1, 2): -1 + 0             + 20 * 0.000022 = -0.99956
SWAP(2, 3): -1 + 1 * 0.0001    +  0 * 0.000022 = -0.99990
```

The smaller score belongs to SWAP(2, 3): an estimated 20-layer head start is worth
more than the small lookahead disadvantage.

With the standard six-qubit lookahead weight `0.5 / 6 ≈ 0.08333`:

```text
SWAP(1, 2): -1 + 0             + 0.00044 = -0.99956
SWAP(2, 3): -1 + 0.08333       + 0       ≈ -0.91667
```

Now SWAP(1, 2) wins. The future distance penalty is too large to justify avoiding
the wait. The test checks both choices for random seeds 0 through 9.

The potential overlap explains the depth motivation. SWAP(2, 3) can use idle wires
while the twenty-gate chain runs on 0, 1, and 5. SWAP(1, 2) must follow that chain.

The saving has a price, which this example also shows. SWAP(2, 3) moves logical
qubit 3 away from qubit 4, so the following `CX(3, 4)` needs a second SWAP. With
every SWAP counted as three CX gates, the two complete routings compare as:

| Lookahead weight | First SWAP | SWAPs in total | Native two-qubit gates | Native two-qubit depth |
| --- | --- | --- | --- | --- |
| `0.0001` | (2, 3), on idle wires | 2 | 28 | 22 |
| `0.5 / 6` | (1, 2), after the chain | 1 | 25 | 25 |

The idle-wire routing is three layers shallower and three gates larger. This is
the same trade the rationale reports for QAOA, where depth falls while the gate
count rises. It is a property of this one circuit, not a guarantee: in a larger
circuit, later choices can change which path is the longest.

The router before the change produces the second row with either weight. It has
no wait term, so the lookahead difference alone separates the two contenders.

Two neighboring test families cover other aspects:

- `test_equal_score_swaps_prefer_idle_wires` verifies the equal-score case across
  `basic`, `lookahead`, and `decay` modes. Its repeated same-pair gates can be
  compressed in the SABRE graph; only a positive ready-time gap is needed here.
- `test_swap_fusing_with_previous_block_is_preferred` verifies choosing the
  fusible pair, then runs `ConsolidateBlocks` and `UnitarySynthesis` to check the
  resulting native CX count.

## 8. How large is `2.2e-5`?

`2.2e-5` is scientific notation for `0.000022`. It converts estimated schedule
layers into the same numerical scale as the routing score.

| Difference in estimated start layers | Difference in wait score |
| --- | --- |
| 1 | `0.000022` |
| 20 | `0.00044` |
| 100 | `0.0022` |
| 1,000 | `0.022` |

For the 193-qubit coupling graph discussed in the rationale, one lookahead
distance unit has weight:

```text
0.5 / 193 ≈ 0.002591
0.002591 / 0.000022 ≈ 117.8 estimated layers
```

Thus a roughly 118-layer start advantage can offset one unit of lookahead
distance, all else equal. With decay enabled, its multiplier must also be
included in the exact comparison.

The rationale says the term never outweighs a real front-distance difference.
Read this as a statement about its measured depth scale and standard unscaled
basic weight: a price around `0.02–0.03` is small compared with a whole distance
unit of 1. **The implementation contains no hard cap or strict priority rule.**
A start difference greater than about 45,455 layers would price above 1, and
custom basic weights or size scaling change the comparison as well.

Decay's standard increment of `0.001` increases a wire's multiplier by one
thousandth. Its absolute score effect depends on the distance score being
multiplied; it is not a fixed additive `0.001` penalty.

## 9. Why this can improve both layout and routing

The wait term favors candidate swaps that can overlap with earlier work when
their geometric disadvantages are small. This can keep additional operations
off the **critical path**, the longest dependency chain determining depth.

Moving one SWAP to idle wires does not automatically save three layers overall:
the previous path must be critical, and later choices can change which path is
longest. The mechanism creates opportunities that the benchmark can measure.

The same routing loop is used by `layout_trial` during its forward/backward
exploration. Because the reduced graph preserves block metadata and still routes
two-qubit nodes, both additions also affect which layouts the search discovers.

The final winner among trials still uses top-level **swap count**, not `D2`.
The change therefore influences depth through local routing decisions and the
layouts they produce, rather than replacing the whole optimization objective
with an exact depth minimizer.

The fusion and wait terms address different properties: estimated extra gate
count and estimated start layer. They can favor different candidates, and when
they do, the credit wins in practice. With the standard basic weight of 1, a
`-2/3` credit outweighs any start difference smaller than about 30,000 layers.
The wait term decides among candidates that have the same credit and nearly the
same distance score.

## 10. Reading the benchmark evidence

### Metrics and experiment vocabulary

| Term from the rationale | How to read it |
| --- | --- |
| `D2 ratio` | Candidate native two-qubit depth divided by baseline depth for the benchmark comparison; less than 1 is better. |
| `N2 ratio` | Candidate native two-qubit gate count divided by baseline count; less than 1 is better. |
| Panel | The collection of benchmark cases being summarized. |
| `gmean` | Geometric mean: multiply positive ratios and take the appropriate root; equivalently, exponentiate their mean logarithm. |
| `W/L` | Reported wins/losses across seeds. If they do not sum to the number of seeds, the remaining comparisons are not listed as wins or losses, typically ties. |
| Basis twins | Related runs compiled into different two-qubit gate bases, here CZ, CX, and ECR. |
| Canary | A case with a known expected result, used to notice an unintended change. |
| Frontier | Measured tradeoff between better depth and higher gate-count cost as the weight changes. |

The workload names identify different interaction patterns: QFT is the quantum
Fourier transform; the Heisenberg case represents a spin-model circuit; QAOA is
the quantum approximate optimization algorithm. You do not need their mathematics
to interpret the ratios, but their different patterns explain why one change can
affect them differently. Heavy-hex refers to the sparse hardware connectivity
used in the experiments.

### Reported results at the current weight

The rationale reports 100-seed CZ-basis comparisons:

| Case | `D2` ratio | Approximate depth change | `N2` ratio | Approximate count change |
| --- | --- | --- | --- | --- |
| `qft_n100` | `0.8237` | 17.63% lower | `0.9086` | 9.14% lower |
| `square_heisenberg_n100` | `0.8292` | 17.08% lower | `0.9474` | 5.26% lower |
| `qaoa_ba_n100_3reps` | `0.8410` | 15.90% lower | `1.0459` | 4.59% higher |
| Reported panel | `0.8313` | 16.87% lower | `0.9656` | 3.44% lower |

These are relative changes in benchmark metrics, not percentage-point changes in
hardware fidelity and not direct predictions of runtime in seconds.

The central tradeoff is visible: QAOA buys lower depth with more native gates,
even though the overall panel improves in both metrics. A panel average can
therefore hide a regression in one workload.

### Why this weight was selected

The experiment imposes a per-case `N2` ratio cap of `1.05`, meaning no more than
5% additional native two-qubit gates in the cases subject to that guard.
This is a benchmark acceptance criterion, not a condition enforced by SABRE at
runtime.

The rationale reports QAOA ratios of `1.0459` for CZ and `1.0467` for CX at
`2.2e-5`. The CX value has only `1.05 - 1.0467 = 0.0033` of ratio margin. At
`2.3e-5`, the reported CX value reaches `1.0500`; at `2.5e-5`, the reported CZ
value reaches `1.051`.

That is why the rationale argues against increasing the weight: the depth benefit
starts costing too many gates on the limiting workload. This empirically selected
coefficient is fixed for all circuits at runtime; there is no case-name or seed
lookup in `choose_best_swap`.

The rationale also reports that alternative nonlinear prices did not improve the
measured depth/count tradeoff, and that disabling decay harmed results. Those
are experimental findings for the tested workloads, not general proofs that no
other scoring function could perform better.

### What the evidence does and does not establish

The reported panel depth ratios improve from about `0.864` for `cc01391e`, to
`0.850` at `1.5e-5`, to `0.8313` at `2.2e-5`, all relative to the original baseline.
The last step is therefore roughly another 2.2% relative depth reduction versus
the `1.5e-5` panel, not another 16.9% reduction versus that intermediate version.

The rationale reports local/real agreement for the earlier `1.5e-5` variant
(`0.8496` local versus `0.849634` real). The `2.2e-5` numbers are described as
local-replica results. The broader circuit survey and several timing measurements
also concern the earlier weight, so they should not be relabeled as independent
measurements of the current coefficient.

Small weight changes can alter an early routing choice, then change the layout
and all later choices. The reported fluctuations of roughly `±0.003` in panel
`D2` therefore indicate sensitivity of the heuristic search, not evidence that
the exact minimum sampled coefficient is universally best. The rationale itself
allows a result around `0.831–0.836` for the current approach.

For `ln` statistics, `ln` is the natural logarithm. A log ratio of `0.036`
corresponds to `exp(0.036) ≈ 1.037`, about 3.7% higher. A guard of `0.0953` is
approximately a ratio of `1.10`. `ln + 2SE` describes a log-space estimate with
two standard errors added, an uncertainty adjustment used by the experiment.
Labels such as IA5 and T17 identify benchmark-specific profiles/cases; their full
definitions are not supplied by the rationale.

The broader survey at `1.5e-5` includes small circuits exceeding a ratio of `1.05`
because of the fusion-credit behavior. Consequently, passing the named panel's
guard must not be interpreted as a universal promise that every circuit stays
within 5% additional gates.

## 11. Runtime cost and implementation scope

Compared with the intermediate implementation that already maintains `ready` and
`last_block`, the priced-wait calculation adds reads, a maximum, numeric
conversion, multiplication, and addition per scored candidate. It needs no new
per-candidate allocation or state structure. Its arithmetic cost does not depend
on whether the weight is `1.5e-5` or `2.2e-5`.

Compared with the original distance-only baseline, the entire enhancement also
includes the tracking arrays, updates, block metadata, and undo journal. The
rationale's "nothing new is tracked or allocated" statement refers to the
incremental priced-term change, not to the full enhancement over that baseline.

The reported hardware-cycle measurements for the earlier coefficient show a few
percent overhead relative to `cc01391e`. A very small-weight control experiment
suggests that much of that overhead comes from work in the frequently executed
candidate loop. Different routing choices can also change how much search work
the compiler performs. Neither mechanism predicts a fixed overhead for every
circuit or machine.

No native synthesis is executed for each candidate. The only gate-saving
information available there is the inexpensive block estimate. The output remains
a routed circuit plus its layout information; actual gate synthesis happens later.

## 12. Tests and what was verified for these documents

The statements about the implementation were checked twice: by reading the code
and by running it. The benchmark numbers were not reproduced.

### What was run

The commit was built outside the repository, in release mode, on macOS (arm64)
with Python 3.11.8, numpy 2.4.6, scipy 1.17.1, and rustworkx 0.18.1.

| Check | Result |
| --- | --- |
| The three mechanism test families, 7 test cases in all | All pass. |
| The Python example in the guide | Runs and prints the output shown there. |
| The candidate scores in sections 3 and 7 | Match the hand calculations to the precision shown. |
| Release-valve rollback, section 5 | `ready` and `last_block` returned to their earlier values before the forced swaps were applied. |
| The exploratory-pass exception described in the guide | A measurement between two gates ended the block in final routing but not in an exploratory pass. |
| The "before the change" behavior in sections 3 and 7 | Matches a build of the previous commit. |
| Six test modules, listed below | 829 tests ran: 825 passed, 3 were skipped, 1 failed. |

The scores and the rollback were read from a temporary copy of `route.rs` with
logging added. That logging is not part of the repository.

The six modules are `test_sabre_swap`, `test_sabre_layout`,
`test_sabre_pre_layout`, `test_transpile_layout`, `test_preset_passmanagers`,
and `compiler.test_transpiler`. From the repository root, with Qiskit built and
`ddt` installed, one focused test runs with:

```sh
python -m unittest \
  test.python.transpiler.test_sabre_swap.TestSabreSwap.test_wire_ready_time_is_priced_against_lookahead
```

### One pinned test fails in this environment

`TestFinalLayouts.test_layout_tokyo_fully_connected_cx` fails at optimization
level 3. It expects the layout `[5, 11, 6, 10, 0]` and gets `[5, 6, 11, 10, 0]`.
Levels 0 to 2 pass. This differs from the rationale, which reports 826 tests and
no failures. What is known:

- The previous commit passes all 822 of its tests in the same environment,
  including this one with its older expected values. The environment is
  therefore not the whole explanation.
- The weight is not the cause. Every value tried from `1e-9` to `2.2e-5` gives
  the same layout.
- At level 3, SABRE does not have the last word. SABRE chooses
  `[5, 10, 6, 11, 0]`. The pass `VF2PostLayout` then runs twice, the second time
  after gate synthesis, and each time moves the circuit to a placement with lower
  estimated error.
- It is not a near-tie. For the circuit produced here, the chosen layout scores
  clearly better than the expected one.

The expected value was therefore recorded from a different circuit than this
commit produces here. That can happen if it was recorded on another platform
whose synthesis differs numerically, or against a different revision of the
code. Two details point to a different revision: the rationale counts 826 tests
where these modules hold 829, and it names `test_mapping` and `test_stochastic*`,
which do not exist in this checkout. Re-running the test where the rationale's
numbers were produced would settle it.

### What remains unverified

The code reading and the runs confirm the constant, score ordering, physical-wire
tracking, rollback, block metadata preservation, and trial-selection behavior
described above. The measured magnitude of the claimed performance improvements
still depends on reproducing the benchmark with its fixtures, native synthesis
pipeline, seeds, and metric aggregation.
