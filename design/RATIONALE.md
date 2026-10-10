# Sabre swap choice: score each swap by where it lands in the native two-qubit schedule

## Decision rule changed

**Before (seed).** `State::choose_best_swap` (`crates/transpiler/src/passes/sabre/route.rs`) scores
each candidate swap by the distance heuristic alone. That treats every swap as costing the same
three native 2q gates and as starting at the same time. It then picks at random among the swaps
within `best_epsilon` of the best score.

**After.** Two terms are added to each candidate's score, both in Sabre's distance units:

1. The fusion credit `−C/3` is applied when the swap merges into the 2q block that last ran on that
   same pair. This is captain-nemo's NASSC credit, attempt cc01391e.
2. The wait term `READY_WEIGHT · max(ready[a], ready[b])`, with `READY_WEIGHT = 2.2e-5`, prices the
   estimated 2q-schedule layer at which the swap's wires are both free. It uses captain-ahab's
   ready-time tracking from 0560f2ec.

The seeded random choice within the epsilon band is unchanged. Relative to cc01391e, the only new change is in how ready time is used. Before, it was an exact
tie-break applied only inside the epsilon band. Now it is a priced score term that competes with
lookahead and decay. The rule is the same as attempt 49dfeb40 (weight 1.5e-5, real 0.8496). This
attempt sets the exchange rate at the edge of the measured N2-safe frontier instead of its middle.

## Mechanism

- **Ready time.** This is ahab's tracking.
  - `ready[q]` is an ASAP estimate of when qubit `q` finishes its last 2q operation.
  - A routed 2q node sets both of its qubits to `max(ready) + 1`, and a routed swap sets both of
    its qubits to `max(ready) + 3`.
  - The release valve's journal restores `ready` when it unwinds swaps.
- **Fusion credit.** This is nemo's NASSC credit.
  - `last_block[q]` is `Some((partner, c))` when the last operation on `q` was a fusible 2q block of
    about `c ≤ 3` native gates. `SabreDAG` precomputes `c` per node, so the credit also acts in
    SabreLayout's layout iterations.
  - A swap on that same pair resynthesises to `min(3, c+1)` gates instead of `c+3`, which saves
    `C = 3 − (min(3, c+1) − c)`.
  - The candidate starts at `−C/3`, which is NASSC's `(3·Σd − C)` in units of one swap.
- **Wait term (new).** In the final min-selection loop, each candidate's accumulated score (basic +
  lookahead + decay, including the credit) gets `2.2e-5` added per layer of `max(ready[a],
  ready[b])` before it is compared with the running minimum.
  - `ready` values grow up to the circuit's estimated depth, about 10³ layers.
  - The term is therefore at most about 0.03, while one unit of front-layer distance is 1. So it
    never outweighs a real distance difference.
  - It does compete with lookahead and decay. One lookahead unit is `0.5/num_qubits`, which is
    2.6e-3 on the 193-qubit heavy hex and equals about 120 layers of wait. Decay moves scores by
    0.1 % per recent use of a qubit. So a wait of tens to hundreds of layers can outweigh a small
    lookahead or decay preference.
  - ahab's exact band tie-break is removed. At equal distance scores, the wait term still selects
    the earliest-starting swap, because a nonzero ready gap is ≥ 2.2e-5 and so larger than
    `best_epsilon`.
- **Cost.** There is one extra multiply-add per candidate in a loop that already reads the score.
  Nothing new is tracked or allocated.

## Why this is principled

- Sabre's lookahead and decay terms are already sub-unit refinements. Each one sways the choice only
  when front-layer distances are nearly equal.
- The seed has no term for when a swap can execute. A swap whose wire is busy for another 20 layers
  can be picked over one that starts now, just because the busy swap's lookahead is marginally
  better. That extends the critical path directly: the three native gates of a routing swap are the
  unit that D2 counts.
- A band-only tie-break (ahab's) fixes this only for exact score ties. In practice, lookahead and
  decay make nearly every candidate score distinct, so the band rarely contains more than one swap.
  Pricing the wait on the same scale as lookahead turns an "estimated time" signal that was almost
  never consulted into one that trades off smoothly: a long wait beats a small lookahead gain, and
  a large lookahead gain still wins.
- This is the same kind of term as NASSC's credit, a cost of the swap in the native schedule. That
  is why the two combine rather than compete. The credit prices how many native gates the swap
  costs, and the wait term prices when they run.
- There are no workload inputs. The rule reads only the router's own schedule estimate: no circuit,
  target, size or seed specifics.
- The weight is read off a frontier, not tuned to a point. Panel D2 falls from 0.868 (1e-6) to 0.826
  (3e-5), while qaoa N2 rises because the router trades extra swaps for depth: 2578 swaps per trial at
  weight 0, 2667 at 1.5e-5, 2725 at 3e-5. The one binding constraint is the 1.05 per-case N2 cap.
  2.2e-5 is about the largest weight whose qaoa N2 stays under it on every basis twin with some margin.
- The linear price is the simplest shape on that frontier. Convex prices (`mx²`, `mx^1.5`, `mx³`,
  `mx²/max ready`) and sum/max-min mixes all landed on the same D2-vs-qaoa-N2 curve, within the
  ±0.003 chaos noise, so none of them buys depth at a given N2 cost. Turning decay off to make room
  is refuted: D2 1.063 at weight 0, and qaoa D2 0.968 at 1.5e-5.

## Why D2 drops

- Heavy-hex routing of these 100-qubit circuits inserts about 2600–2800 swaps per trial. Many
  swap decisions are between candidates of equal front distance that differ only in lookahead.
- With the wait priced, those decisions move swaps off wires that already carry the critical path
  and onto idle ones, whenever the lookahead difference is small. Each such move keeps three native
  layers off the longest chain.
- The rule is also active in SabreLayout's forward/backward iterations, so the chosen layouts are the
  ones that route shallowly under it.
- Relative to cc01391e (panel D2 0.864), the priced term takes the panel to 0.850 at 1.5e-5
  (49dfeb40, real) and to 0.831 at 2.2e-5.

## Expected N2 / time effect

- **Panel N2 drops to 0.9656**, from 0.972 for cc01391e and 0.9665 at 1.5e-5.
- **QAOA N2 rises 4.6 %**: cz 1.0459, cx 1.0467, ecr 1.0459, against 1.036 at 1.5e-5. These are extra
  swaps bought for depth. They stay below the 1.05 per-case cap, but the margin is only about 0.003.
  At 2.3e-5 cx reads 1.0500, and at 2.5e-5 cz reads 1.051. So the weight should not grow.
- **Time.** Only the constant differs from 49dfeb40, whose real IA5 read timing ln 0.036 and
  companion ln 0.066 (guard 0.0953). The measurements below are for 49dfeb40. Any change here comes
  only from different routing behaviour.
- Single-core hardware cycle counts inside SabreLayout at the IA5 seed are +2.0 % (qft),
  +3.9 % (heisenberg) and +3.2 % (qaoa) relative to cc01391e.
  - A control build with weight 1e-9 costs +2.7 / +3.2 / +3.7 %. That build has the same arithmetic,
    and its term acts only as an exact tie-break, as in cc01391e. So most of the extra time is the
    per-candidate arithmetic in the hot loop, not a change in routing behaviour.
  - A pinned IA5 replica (vs seed) of a behaviour-identical earlier build of this rule passes both
    panels: timing ln −0.032, companion ln +0.017, with T17 at 1.055 / 1.086 and no failing case.
    That build's routed output was identical on all 900 panel rows.

## Evidence

Local replica of the iterations-profile panel (`local-d2-probe` skill, the benchmark's own
fixtures, exporter and metric), 100 seeds, candidate vs seed:

| case (cz) | D2 ratio | W/L | N2 ratio |
|---|---|---|---|
| qft_n100 | 0.8237 | 94/6 | 0.9086 |
| square_heisenberg_n100 | 0.8292 | 92/6 | 0.9474 |
| qaoa_ba_n100_3reps | 0.8410 | 100/0 | 1.0459 |
| **panel** | **0.8313** (ln+2SE −0.172) | | **0.9656** |

- **cx/ecr twins.** Panel D2 is 0.8317 on cx and 0.8313 on ecr. Panel N2 is 0.9663 on cx. QAOA N2 is
  1.0467 on cx and 1.0459 on ecr.
- The 49dfeb40 local panel (weight 1.5e-5) matched its real score exactly: 0.8496 local, 0.849634 real.
- **Canaries.** They hit their exact constants: su2_circular_n100 cz 300/300, and long_2q_sequence
  rochester L2/L3 3/3.
- **Weight frontier.** These runs apply the priced wait term on top of cc01391e.
  - Small weight changes move panel D2 chaotically, by about ±0.003, and qaoa N2 by about ±0.001.
    So the point at 2.2e-5 is partly a lucky draw. Its neighbours read 0.835 and 0.834, so a
    real result anywhere in 0.831–0.836 is consistent.

  | weight | panel D2 | panel N2 | qaoa N2 |
  |---|---|---|---|
  | 1e-6 | 0.868 | 0.972 | 1.034 |
  | 1e-5 | 0.854 | 0.966 | 1.030 |
  | 1.5e-5 (49dfeb40) | 0.850 | 0.967 | 1.036 |
  | 1.9e-5 | 0.840 | | 1.044 |
  | 2e-5 | 0.845 | 0.968 | 1.046 |
  | 2.1e-5 | 0.835 | | 1.047 |
  | **2.2e-5** | **0.831** | **0.966** | **1.046** (cx 1.047) |
  | 2.3e-5 | 0.834 | | 1.049 (cx 1.050) |
  | 2.5e-5 | 0.834 | 0.966 | 1.051 |
  | 1e-4 | 0.778 | 0.992 | 1.114 |

- **Breadth** (measured at 1.5e-5). On 100 seeds of the benchmark's other L2 circuits, the D2
  gmean is 0.888, against 0.933 for cc01391e. The N2 gmean is 0.953, against 0.9825.
  - The wait term does not change the few small circuits where N2 exceeds 1.05, such as
    multiplier_n16 and mcx_n5. Those come from the fusion credit on single-CX blocks and are the
    same as in cc01391e.
- **Mechanism test.** `TestSabreSwap.test_wire_ready_time_is_priced_against_lookahead` uses the line
  0-1-2-3-4 with a spur 1-5.
  - Qubit 1 is busy for 20 layers with gates that alternate with qubits 0 and 5 (so SabreDAG cannot
    merge them) when `cx(1, 3)` must be routed.
  - Swap (1,2) has the better lookahead toward `cx(3, 4)`. Swap (2,3) can start 20 layers earlier.
  - With lookahead weight 1e-4 the wait (20 × 2.2e-5 = 4.4e-4) wins, and (2,3) is chosen for seeds
    0–9. With the standard `0.5/num_qubits` weight the lookahead gain wins, and (1,2) is chosen.
  - The cc01391e band-tie-break build fails the first assertion, because the scores differ so the
    band never holds both swaps.
  - `test_equal_score_swaps_prefer_idle_wires` (exact ties) and
    `test_swap_fusing_with_previous_block_is_preferred` (credit) both still pass.

The existing tests whose pinned outputs changed are the same set, with the same updated
expectations, as in cc01391e. The wait term, at either weight, changed no further pinned outputs:

- `test_sabre_swap.TestSabreSwap.test_lookahead_mode`
- `test_sabre_swap.TestSabreSwapControlFlow` tests: `test_pre_intra_post_if_else`, the switch tests
  and `test_nested_vars`
- `test_sabre_layout`: the `TestSabreLayout` coupling/trial tests and
  `TestDisjointDeviceSabreLayout.test_dag_fits_in_one_component`
- `test_preset_passmanagers.TestFinalLayouts.test_layout_tokyo_fully_connected_cx`
- `test_transpile_layout.TranspileLayoutTest.test_final_{index,virtual}_layout_full_path*`
- `compiler.test_transpiler.TestPostTranspileIntegration.test_transpile_final_layout_updated_with_post_layout`

`test_sabre*`, `test_transpile_layout`, `test_preset_passmanagers`, `compiler.test_transpiler`,
`test_mapping` and `test_stochastic*` all pass: 826 tests, 0 failures.

Test command: .venv/bin/python -m unittest test.python.transpiler.test_sabre_swap.TestSabreSwap.test_wire_ready_time_is_priced_against_lookahead
