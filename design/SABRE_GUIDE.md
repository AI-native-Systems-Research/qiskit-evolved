# Understanding Qiskit's SABRE implementation

SABRE is the algorithm Qiskit uses to make a circuit runnable on a real device.
A device only lets certain pairs of physical qubits interact, so SABRE decides
**where each program qubit should start** and **which SWAP gates to insert** so
that every two-qubit gate lands on a connected pair. The name stands for
**SWAP-based BidiREctional heuristic search**; the algorithm was introduced by
Li, Ding, and Xie in
[Tackling the Qubit Mapping Problem for NISQ-Era Quantum Devices](https://arxiv.org/abs/1809.02573).

**Who this is for.** You know that a circuit is a list of gates acting on
qubits, and that a CX gate touches two qubits. That is all you need. There is no
state-vector mathematics, no graph theory beyond "neighbors", and no Rust.

**What you will be able to do afterwards.**

- Route a small circuit by hand, making exactly the choices SABRE makes.
- Compute the score of each candidate SWAP and say why one wins.
- Explain how routing the circuit *backward* produces a better starting layout.
- Find the code that performs each step.

**What this guide describes.** The Rust code in
[`crates/transpiler/src/passes/sabre`](../crates/transpiler/src/passes/sabre)
of **this checkout**. It is classic SABRE (distance, lookahead, decay) plus two
extra scoring terms added here: a *fusion credit* and a *ready-time penalty*.
Parts I, II and IV explain classic SABRE. Part III explains the additions and is
clearly separated so you can tell which behaviour is standard.

**How to read it.**

| If you want... | Read |
| --- | --- |
| The idea in ten minutes | Part I, especially section 3 |
| To understand the algorithm properly | Parts I and II in order; every decision is worked in full |
| What this checkout changed | Part III |
| How the initial layout is chosen | Part IV |
| Code, runnable examples, FAQ | Part V |

Contents:

- **Part I — The problem**
  - [1. The problem in one picture](#1-the-problem-in-one-picture)
  - [2. The vocabulary you need](#2-the-vocabulary-you-need)
  - [3. SABRE in one page](#3-sabre-in-one-page)
- **Part II — Routing, one decision at a time**
  - [4. The running example](#4-the-running-example)
  - [5. Ready versus adjacent: the front layer and the lookahead layer](#5-ready-versus-adjacent-the-front-layer-and-the-lookahead-layer)
  - [6. The first decision, step by step](#6-the-first-decision-step-by-step)
  - [7. Finishing the circuit: decisions 2 and 3](#7-finishing-the-circuit-decisions-2-and-3)
  - [8. Decay: spread the SWAPs over different wires](#8-decay-spread-the-swaps-over-different-wires)
  - [9. Ties, randomness, trials, and the safety valve](#9-ties-randomness-trials-and-the-safety-valve)
  - [10. The whole loop, with every step named](#10-the-whole-loop-with-every-step-named)
- **Part III — What this checkout adds to the score**
  - [11. Fusion credit: a SWAP right after a CX is cheaper](#11-fusion-credit-a-swap-right-after-a-cx-is-cheaper)
  - [12. Ready time: prefer wires that are free sooner](#12-ready-time-prefer-wires-that-are-free-sooner)
  - [13. The complete score, in the order the code computes it](#13-the-complete-score-in-the-order-the-code-computes-it)
  - [14. What these estimates do not know](#14-what-these-estimates-do-not-know)
- **Part IV — Choosing the initial layout**
  - [15. The idea: route it backward](#15-the-idea-route-it-backward)
  - [16. Worked example: three SWAPs become two](#16-worked-example-three-swaps-become-two)
  - [17. What one layout trial runs](#17-what-one-layout-trial-runs)
- **Part V — The rest of the picture**
  - [18. How the circuit is compressed before routing](#18-how-the-circuit-is-compressed-before-routing)
  - [19. Control flow and disconnected devices](#19-control-flow-and-disconnected-devices)
  - [20. Map of the Rust code and a reading order](#20-map-of-the-rust-code-and-a-reading-order)
  - [21. Python examples you can run](#21-python-examples-you-can-run)
  - [22. Common confusions](#22-common-confusions)
  - [23. Check your understanding](#23-check-your-understanding)
  - [24. Glossary](#24-glossary)

After this guide, read [SABRE_CHANGE_EXPLAINED.md](SABRE_CHANGE_EXPLAINED.md)
for the motivation, benchmarks and limitations of the change made in this
checkout.

> **Notation used everywhere in this guide**
>
> - Capital letters `A`, `B`, `C`, `D` are **program qubits** (also called
>   logical or virtual qubits). `p0`, `p1`, `p2`, ... are **physical positions**
>   on the device. Positions never move; program qubits do.
> - A row in brackets lists **who sits where**, position by position.
>   `[B, A, C, D]` means B is at p0, A is at p1, C is at p2, D is at p3.
> - `SWAP(p0, p1)` exchanges the occupants of positions p0 and p1.
> - `d(A, C)` is the number of hardware edges between the positions that
>   currently hold A and C. Neighbors have distance 1.
> - Scores: **smaller is better**. A negative score change means "this SWAP
>   makes things better"; it is not a negative number of gates.

---

# Part I — The problem

## 1. The problem in one picture

Imagine three people, A, B and C, sitting in a row of three chairs. They may
only whisper to the person in the chair next to them, and they may trade chairs
with a neighbor, but every trade takes time. The program says "A must whisper
to C". They are two chairs apart, so somebody has to move.

The quantum version is the same picture:

```text
Physical positions:   p0 ---- p1 ---- p2
Occupants:             A       B       C

Program:  CX(A, C)   then   CX(A, B)
```

The device only connects p0–p1 and p1–p2. The first gate needs A and C to be
neighbors, and they are not. There are two ways to fix it with one SWAP:

```text
Option 1: SWAP(p0, p1)    ->   [B, A, C]     A and C are now at p1, p2: adjacent.
Option 2: SWAP(p1, p2)    ->   [A, C, B]     A and C are now at p0, p1: adjacent.
```

Both options make the first gate possible. They are **not** equally good,
because of the *second* gate, `CX(A, B)`:

| After | Where A and B are | d(A, B) | Extra SWAP needed for CX(A, B)? |
| --- | --- | --- | --- |
| Option 1, `[B, A, C]` | p1 and p0 | 1 | No |
| Option 2, `[A, C, B]` | p0 and p2 | 2 | Yes, one more |

Option 1 solves the whole program with one SWAP; option 2 needs two. Choosing
between such options, quickly and reasonably well, for circuits with thousands
of gates, is SABRE's entire job.

There are two decisions to make, and SABRE makes both:

| Decision | Question | In the example |
| --- | --- | --- |
| **Initial layout** | Where should each program qubit *start*? | If A had started at p1 and C at p2, no SWAP would be needed at all. |
| **Routing** | Given where everyone is now, which SWAPs make the next gates possible? | Insert SWAP(p0, p1). |

Choosing the initial layout is free: it just assigns program wires to hardware
positions before anything runs. Routing is not free: every SWAP is a real
operation on the device. On most hardware a SWAP is built from **three** CX
gates, so each SWAP adds noise and time. SABRE therefore tries to use few SWAPs
and, secondarily, to let independent SWAPs run at the same time.

## 2. The vocabulary you need

### Gates and SWAPs

| Term | What it means for routing |
| --- | --- |
| One-qubit gate | Acts on one qubit. Never needs routing: a single qubit is always "connected to itself". |
| Two-qubit gate | Acts on two qubits. The two must sit on **connected** physical positions. |
| `CX` (CNOT) | The most common two-qubit gate. The first operand is the control, the second the target; the order matters for the gate's meaning. |
| `SWAP` | Exchanges the quantum states held by two positions. The hardware positions stay put; the information moves. Usually implemented as three CX gates. |
| Measurement, barrier | Not routing constraints themselves, but they are ordering points that other gates must respect. |

A SWAP does not copy or measure anything. After `SWAP(p0, p1)`, whatever A's
state was is now at p1, and B's state is at p0. The compiler must remember
this, which is what the layout does.

### Program qubits, physical positions, and the layout

A **program qubit** (logical or virtual qubit) is a name in the user's circuit:
A, B, C, D. A **physical position** (physical qubit) is a numbered place on the
device: p0, p1, p2, p3. The **layout** is the table that says who is where.
It changes every time a SWAP is inserted:

| Program qubit | Position at start | Position after SWAP(p0, p1) |
| --- | --- | --- |
| A | p0 | p1 |
| B | p1 | p0 |
| C | p2 | p2 |

Every later gate must be placed using the *current* layout. "CX(A, C)" is
always a gate on A and C; which physical positions that means depends on when
it runs. In the Rust code the layout is an `NLayout`, which stores both
directions (`layout[A]` gives A's position, and the reverse lookup gives the
occupant of a position) and has a `swap_physical(a, b)` method.

### The device: coupling map and distance

The **coupling map** is the device's connection graph: positions are nodes,
and an edge means "a two-qubit gate can run on this pair". The **distance**
between two positions is the smallest number of edges on a path between them.
For the four-position line used in this guide:

```text
p0 ---- p1 ---- p2 ---- p3
```

| From / to | p0 | p1 | p2 | p3 |
| --- | --- | --- | --- | --- |
| p0 | 0 | 1 | 2 | 3 |
| p1 | 1 | 0 | 1 | 2 |
| p2 | 2 | 1 | 0 | 1 |
| p3 | 3 | 2 | 1 | 0 |

This table is computed once from the coupling map and never changes. The
target for a two-qubit gate is distance **1**, not 0: two different qubits must
be in two different positions. A pair at distance `d` needs at least `d - 1`
SWAPs to become adjacent if we move it along a shortest path, but one SWAP can
also help two pairs at once, or hurt one while helping another.

### The circuit as a dependency graph

Two gates that share a qubit must run in the order the program wrote them.
Gates on disjoint qubits are independent and may run in either order or at the
same time. This "must come before" relation forms a **DAG** (directed acyclic
graph), and SABRE follows it strictly: it never reorders two gates that share a
qubit.

### What "cost" means

| Measure | Meaning |
| --- | --- |
| SWAP count | Number of SWAPs SABRE inserted. This is what SABRE itself uses to compare complete attempts. |
| Two-qubit gate count | Number of native two-qubit gates after later compiler passes (each SWAP is usually 3, sometimes fewer after optimization). |
| Two-qubit depth | Number of sequential layers of two-qubit gates. Independent gates can overlap, so depth is not the same as count. |

## 3. SABRE in one page

Here is the whole routing algorithm in plain words. Everything after this
section is detail.

1. **Find the blocked gates.** Look at the gates whose predecessors are all
   done but whose two qubits are not adjacent. This set is the **front
   layer**. (Gates that are ready *and* adjacent are simply emitted at once.)
2. **Peek ahead.** Find the gates that would become ready right after the
   front layer. This is the **lookahead layer**. It is only looked at, never
   executed early.
3. **List the candidate SWAPs.** Every hardware edge that touches a qubit of a
   front-layer gate is a candidate. Nothing else is.
4. **Score each candidate.** Pretend to apply it, and measure how much the
   front-layer distances shrink. Add a small fraction of how much the
   lookahead distances shrink. (This checkout adds two more small terms,
   explained in Part III.) Smaller is better.
5. **Apply the best candidate.** Insert that SWAP into the output and update
   the layout. If several candidates tie exactly, pick one at random.
6. **Emit what is now possible.** Any front gate that became adjacent is
   emitted. That may unlock successors; emit those too if they are adjacent,
   otherwise they join the front layer.
7. **Repeat** from step 1 until no gates remain.

```text
        ┌──────────────────────────────────────────────┐
        │ front layer F = ready but blocked            │
        │ lookahead  E = what comes right after F      │
        └──────────────────────┬───────────────────────┘
                               ▼
        ┌──────────────────────────────────────────────┐
        │ candidates = hardware edges touching F       │
        └──────────────────────┬───────────────────────┘
                               ▼
        ┌──────────────────────────────────────────────┐
        │ score each candidate: distance, lookahead,   │
        │ decay (+ this checkout's two extra terms)    │
        └──────────────────────┬───────────────────────┘
                               ▼
        ┌──────────────────────────────────────────────┐
        │ apply the best SWAP; update the layout       │
        └──────────────────────┬───────────────────────┘
                               ▼
        ┌──────────────────────────────────────────────┐
        │ emit every gate that is now adjacent;        │
        │ advance dependencies; rebuild F and E        │
        └──────────────────────┬───────────────────────┘
                               ▼
                 gates left? ──yes──▶ back to the top
                               │
                              no
                               ▼
                           finished
```

### The smallest complete example

Take the three-chair program from section 1 and run the loop once.

```text
Hardware:   p0 ---- p1 ---- p2         Occupants: [A, B, C]
Program:    g1 = CX(A, C),  g2 = CX(A, B)
```

**Step 1, front layer.** g1 is ready (nothing precedes it) but A is at p0 and
C at p2, distance 2. So `F = {g1}`. g2 is not ready: it must wait for g1
because they share A.

**Step 2, lookahead.** If g1 were done, g2 would be ready. So `E = {g2}`.

**Step 3, candidates.** The front qubits sit at p0 and p2. Edges touching
them: `(p0, p1)` and `(p1, p2)`. Two candidates.

**Step 4, score.** For each candidate, imagine applying it and measure the
distances:

| Candidate | Imagined occupants | d(A, C) front | change | d(A, B) lookahead | change |
| --- | --- | --- | --- | --- | --- |
| none (now) | [A, B, C] | 2 | — | 1 | — |
| SWAP(p0, p1) | [B, A, C] | 1 | −1 | 1 | 0 |
| SWAP(p1, p2) | [A, C, B] | 1 | −1 | 2 | +1 |

Both candidates improve the front gate by the same amount, so the front
distance alone cannot choose. The lookahead gate breaks the tie: SWAP(p1, p2)
would pull A and B apart. With the lookahead weight used for a three-qubit
device, `w = 0.5 / 3 ≈ 0.167`:

```text
score(p0, p1) = −1 + 0.167 × 0   = −1.000   <- best
score(p1, p2) = −1 + 0.167 × (+1) = −0.833
```

**Step 5, apply.** Insert `SWAP(p0, p1)`. The occupants become `[B, A, C]`.

**Step 6, emit.** g1 = CX(A, C) is now on p1 and p2, adjacent: emit it. That
unlocks g2 = CX(A, B), on p1 and p0, adjacent: emit it too.

**Step 7.** Nothing is left. Routing is finished with one SWAP:

```text
p0: ──X─────────⊕──        X–X  = SWAP(p0, p1)
      │         │          ●/⊕  = CX control / target
p1: ──X────●────●──        g1 = CX on (p1, p2), g2 = CX on (p1, p0)
           │
p2: ───────⊕───────
```

Running this in Qiskit (section 21) produces exactly this circuit.

That is the whole algorithm. The remaining parts add: a slightly larger example
where every decision is tabulated (Part II), the extra terms in the score
(Part III), and how the same routine is reused to choose the initial layout by
running it backward (Part IV).

---

# Part II — Routing, one decision at a time

## 4. The running example

Everything in Parts II–IV uses one circuit on one device.

```text
Device:  p0 ---- p1 ---- p2 ---- p3        Starting occupants: [A, B, C, D]

Program (in written order):
    g1 = CX(A, C)
    g2 = CX(A, B)
    g3 = CX(C, D)
    g4 = CX(B, D)
```

As a circuit drawing (● = control, ⊕ = target):

```text
          g1      g2      g3      g4
A  ───────●───────●───────────────────────
          │       │
B  ───────┼───────⊕───────────────●───────
          │                       │
C  ───────⊕───────────────●───────┼───────
                          │       │
D  ───────────────────────⊕───────⊕───────
```

Which gates must wait for which? Two gates are ordered only if they share a
qubit:

| Gate | Must wait for | Because |
| --- | --- | --- |
| g1 = CX(A, C) | nothing | It is the first gate on both A and C. |
| g2 = CX(A, B) | g1 | They share A. |
| g3 = CX(C, D) | g1 | They share C. |
| g4 = CX(B, D) | g2 **and** g3 | B comes from g2, D comes from g3. |

As a dependency graph (arrows mean "must come before"):

```text
              ┌──▶ g2 = CX(A, B) ──┐
g1 = CX(A, C) ┤                    ├──▶ g4 = CX(B, D)
              └──▶ g3 = CX(C, D) ──┘
```

Note that g2 and g3 are written one after the other but act on different
qubits, so there is **no** arrow between them. Either may run first.

With the starting occupants `[A, B, C, D]`:

| Gate | Positions now | Distance | Adjacent? |
| --- | --- | --- | --- |
| g1 = CX(A, C) | p0, p2 | 2 | No |
| g2 = CX(A, B) | p0, p1 | 1 | Yes |
| g3 = CX(C, D) | p2, p3 | 1 | Yes |
| g4 = CX(B, D) | p1, p3 | 2 | No |

g2 and g3 are adjacent, but they cannot run yet: they are waiting for g1.
That distinction is the subject of the next section.

## 5. Ready versus adjacent: the front layer and the lookahead layer

A two-qubit gate can be emitted only when **both** of these hold:

1. **Ready:** every gate it depends on has already been emitted.
2. **Adjacent:** its two qubits currently sit on connected positions.

Readiness comes from the program; adjacency comes from the layout. SWAPs
change adjacency. Nothing changes readiness except emitting the predecessors.

### How readiness is tracked

Give each gate a counter: "how many of my predecessors are still not done".

| Gate | Counter at start | After g1 is emitted | After g2 | After g3 |
| --- | --- | --- | --- | --- |
| g1 | 0 | done | done | done |
| g2 | 1 | 0 → ready | done | done |
| g3 | 1 | 0 → ready | 0 → ready | done |
| g4 | 2 | 2 | 1 | 0 → ready |

A gate with counter 0 is ready. Emitting a gate decrements the counters of
its successors. (The code counts dependency *edges*, and two gates can be
joined by more than one edge when they share two wires, but the idea is the
same.)

### The front layer

The **front layer**, written `F`, is the set of gates that are ready but
**not** adjacent. These are the gates that routing must do something about.

Gates that are ready *and* adjacent never sit in `F`: the router emits them
immediately, then checks whether that made anything else ready, and keeps
going. Only when every ready gate is blocked does it stop and start choosing
SWAPs. This "emit everything you can" sweep is why a circuit with thousands of
gates may need only a few hundred SWAP decisions.

At the start of our example: g1 is ready, not adjacent. `F = {g1}`.

### The lookahead layer

The **lookahead layer**, written `E`, answers: "if the whole front layer were
done, which two-qubit gates would become ready?" To compute it, the router
copies the readiness counters, pretends every front gate is done, and collects
the gates whose copied counter reaches 0. Nothing is actually emitted; the real
counters are untouched.

At the start: pretending g1 is done makes g2 and g3 ready. `E = {g2, g3}`.
g4 is not in `E`, because it also waits for g2 and g3, which are not done
even in the pretend world.

Two things about `E` are worth noticing:

- It can contain gates that are **already adjacent** (g2 and g3 are). That is
  useful: a SWAP that pulled them apart would create work later, and the score
  should see that.
- The named `lookahead` and `decay` modes in this checkout use **one**
  lookahead layer. The code supports more (each with its own weight), but the
  wrappers do not use them.

The state before the first decision:

```text
Occupants:     [A, B, C, D]
Emitted:       nothing
Front F:       { g1 = CX(A, C) }        A at p0, C at p2, distance 2
Lookahead E:   { g2 = CX(A, B), g3 = CX(C, D) }
Not yet seen:  g4
```

## 6. The first decision, step by step

### 6.1 Which SWAPs are candidates

**Rule.** A candidate is any hardware edge that has at least one endpoint
occupied by a qubit of a front-layer gate. An edge touching two such qubits is
listed once.

**Why this rule.** A SWAP that touches no front qubit cannot change any front
distance in this step, so it cannot help now. Restricting candidates this way
keeps every decision cheap: there are at most (2 × number of blocked gates ×
device degree) candidates, however many edges the device has.

**Example.** The front gate g1 uses A (at p0) and C (at p2):

| Front position | Its neighbors | Candidate edges |
| --- | --- | --- |
| p0, holding A | p1 | (p0, p1) |
| p2, holding C | p1, p3 | (p1, p2), (p2, p3) |

Three candidates: `(p0, p1)`, `(p1, p2)`, `(p2, p3)`.

Not a candidate: `(p0, p2)`, because it is not a hardware edge. A SWAP can
only be applied to connected positions. Also not a candidate on a longer line:
an edge like `(p3, p4)` touching neither A nor C.

### 6.2 Score by front-layer distance

**Rule.** For each candidate, imagine applying it to the *current* layout
(the candidates are alternatives, not a sequence), and compute

```text
ΔD_F = (sum of front distances after the SWAP) − (sum of front distances now)
```

Smaller is better. A negative value is an improvement.

**Example.**

| Candidate | Imagined occupants [p0, p1, p2, p3] | d(A, C) | ΔD_F |
| --- | --- | --- | --- |
| none (now) | [A, B, C, D] | 2 | — |
| SWAP(p0, p1) | [B, A, C, D] | 1 | 1 − 2 = **−1** |
| SWAP(p1, p2) | [A, C, B, D] | 1 | 1 − 2 = **−1** |
| SWAP(p2, p3) | [A, B, D, C] | 3 | 3 − 2 = +1 |

Two candidates tie at −1. The third moves C away from A and is worse.

When the front layer has several gates, add up the changes for all of them.
For instance, if the front were `{CX(A, C), CX(B, D)}` with occupants
`[A, B, C, D]`, then SWAP(p1, p2) gives `[A, C, B, D]`, bringing both pairs
to distance 1: `(1 − 2) + (1 − 2) = −2`. One SWAP, two gates helped. Scoring
the whole front is why SABRE can find such moves.

The code (`Layer::score`) does not recompute every distance. Only the two
swapped positions move, so only their partners' distances can change; it adds
up just those changes. That is the "delta" the name `ΔD_F` refers to.

### 6.3 Lookahead breaks the tie

**Rule.** Compute the same change for the lookahead layer, `ΔD_E`, and add a
small multiple of it:

```text
score = ΔD_F + w × ΔD_E,        w = 0.5 / (number of physical qubits)
```

The weight is small on purpose: the front layer must win when they disagree,
and lookahead should only separate options that are equally good for the front.
On our four-position device `w = 0.5 / 4 = 0.125`.

**Example.** The lookahead gates are g2 = CX(A, B) and g3 = CX(C, D). Now
their distances sum to `1 + 1 = 2`.

| Candidate | Imagined occupants | d(A, B) | d(C, D) | ΔD_E | ΔD_F | score = ΔD_F + 0.125 × ΔD_E |
| --- | --- | --- | --- | --- | --- | --- |
| SWAP(p0, p1) | [B, A, C, D] | 1 | 1 | 0 | −1 | **−1.000** |
| SWAP(p1, p2) | [A, C, B, D] | 2 | 2 | +2 | −1 | −0.750 |
| SWAP(p2, p3) | [A, B, D, C] | 1 | 1 | 0 | +1 | +1.000 |

**Winner: SWAP(p0, p1).** Both tied candidates enable g1, but SWAP(p1, p2)
would push *both* upcoming gates apart, and the lookahead term sees that.

This is exactly what lookahead is for: choose among equally good immediate
moves using a glimpse of what comes next. It does not plan the whole future
and it does not guarantee the globally best route; it is a cheap, informed
preference.

### 6.4 Apply the winner

Only the winning SWAP is applied. The other candidates were hypothetical.

```text
Insert SWAP(p0, p1) into the output.

Positions:   p0 ---- p1 ---- p2 ---- p3
Before:       A       B       C       D
After:        B       A       C       D
```

What changed and what did not:

| Thing | Changed? |
| --- | --- |
| Hardware graph, distance table | No, never. |
| Layout (who is where) | Yes: A → p1, B → p0. |
| The gates still to route | No, but their physical positions must be re-read from the new layout. |
| Readiness counters | No. A SWAP emits no program gate. |

### 6.5 Emit what is now possible

g1 = CX(A, C): A at p1, C at p2, adjacent. Emit it as `CX(p1, p2)`. Its
counter-decrement makes g2 and g3 ready:

| Newly ready gate | Positions under [B, A, C, D] | Adjacent? | Action |
| --- | --- | --- | --- |
| g2 = CX(A, B) | p1, p0 | Yes | Emit as CX(p1, p0). |
| g3 = CX(C, D) | p2, p3 | Yes | Emit as CX(p2, p3). |
| g4 = CX(B, D), ready after g2 and g3 | p0, p3 | No, distance 3 | Put in the front layer. |

g2 and g3 use disjoint positions, so they can run simultaneously; the output
list has to write one first, but that imposes no ordering on the hardware.

The lookahead layer is rebuilt: with g4 as the whole front, nothing comes after
it, so `E` is empty.

State after the first decision:

```text
Occupants:     [B, A, C, D]
Emitted:       SWAP(p0, p1), g1 on (p1, p2), g2 on (p1, p0), g3 on (p2, p3)
Front F:       { g4 = CX(B, D) }        B at p0, D at p3, distance 3
Lookahead E:   { }
```

Notice that the front layer did not simply inherit the old lookahead layer.
The old lookahead gates were adjacent, so they were emitted, and that exposed
g4 instead.

## 7. Finishing the circuit: decisions 2 and 3

### Decision 2

Front: g4 with B at p0 and D at p3. Candidates are the edges touching p0 or
p3: `(p0, p1)` and `(p2, p3)`. The edge `(p1, p2)` touches neither, so it is
not considered.

| Candidate | Imagined occupants | d(B, D) | ΔD_F | ΔD_E (E is empty) | score |
| --- | --- | --- | --- | --- | --- |
| SWAP(p0, p1) | [A, B, C, D] | 2 | −1 | 0 | **−1** |
| SWAP(p2, p3) | [B, A, D, C] | 2 | −1 | 0 | **−1** |

An exact tie, and nothing in classic SABRE separates them. (Part III shows that
this checkout's extra terms are also identical for the two, so it stays a tie.)
The router picks one at random, using a seeded generator so the result is
reproducible.

Here the tie is harmless: both SWAPs are needed before g4 can run, and they act
on disjoint pairs. Whichever is chosen now, this checkout's extra terms pick
the other one at the next decision (section 11 shows why), so the final result
is the same either way. (You can see this in section 21: every seed produces
the same output circuit.) We follow the branch that picks **SWAP(p0, p1)**.

After it, the occupants are `[A, B, C, D]` again. This is the same arrangement
as at the start, but the computation is **not** undone: g1, g2 and g3 have
already been emitted between the two SWAPs. B is at p1 and D at p3, distance
2, so g4 is still blocked and the router scores again.

### Decision 3

Front: g4 with B at p1 and D at p3. Candidates: edges touching p1 or p3, which
are `(p0, p1)`, `(p1, p2)` and `(p2, p3)`.

| Candidate | Imagined occupants | d(B, D) | ΔD_F | score (classic SABRE) |
| --- | --- | --- | --- | --- |
| SWAP(p0, p1) | [B, A, C, D] | 3 | +1 | +1 |
| SWAP(p1, p2) | [A, C, B, D] | 1 | −1 | **−1** |
| SWAP(p2, p3) | [A, B, D, C] | 1 | −1 | **−1** |

Classic SABRE sees another tie between the last two. In this checkout the
fusion credit (section 11) and ready-time term (section 12) both prefer
**SWAP(p2, p3)**, which is what we follow. The occupants become
`[A, B, D, C]`; B at p1 and D at p2 are adjacent, and g4 is emitted as
`CX(p1, p2)`. The front layer is empty, so routing is finished.

### The complete route

| Event | Occupants afterward | Emitted | Still blocked |
| --- | --- | --- | --- |
| Start | [A, B, C, D] | — | g1 at distance 2 |
| Decision 1 | [B, A, C, D] | SWAP(p0, p1) | nothing in front of g1 |
| Emit | [B, A, C, D] | CX(p1, p2) = g1 | g2, g3 become ready |
| Emit | [B, A, C, D] | CX(p1, p0) = g2 | g4 waits for g3 |
| Emit | [B, A, C, D] | CX(p2, p3) = g3 | g4 at distance 3 |
| Decision 2 | [A, B, C, D] | SWAP(p0, p1) | g4 at distance 2 |
| Decision 3 | [A, B, D, C] | SWAP(p2, p3) | nothing |
| Emit | [A, B, D, C] | CX(p1, p2) = g4 | finished |

The physical output circuit (X–X = SWAP, ● = control, ⊕ = target):

```text
         SWAP   g1    g2    g3   SWAP  SWAP   g4
p0  ──────X───────────⊕────────────X──────────────────
          │           │            │
p1  ──────X─────●─────●────────────X───────────●──────
                │                              │
p2  ────────────⊕───────────●────────────X─────⊕──────
                            │            │
p3  ────────────────────────⊕────────────X────────────
```

Three SWAPs, four CX gates. Expanding each SWAP into three CX gives 13 CX
gates before any optimization; later passes can reduce that (section 11).

**Final layout:** `A → p0, B → p1, C → p3, D → p2`. The output does not swap
everyone back to where they started. Qiskit records this final layout so that
later passes and measurement results are interpreted correctly: a measurement
of program qubit C must read position **p3**.

**Why this route is legal** (worth checking for any hand-worked example):

1. Every SWAP and every CX uses a hardware edge.
2. Every CX acts on the positions that hold its program qubits *at that
   moment*, with control and target in the right order.
3. No dependency is violated: g1 before g2 and g3; both before g4.
4. The final layout accounts for every SWAP.

A route can be legal and still wasteful. Legality is a correctness property;
SWAP count and depth are quality properties.

## 8. Decay: spread the SWAPs over different wires

Distance and lookahead look only at positions, never at history. The third
classic ingredient, **decay**, adds short-term memory so the router does not
keep hammering the same two wires.

**Rule.** Each physical position has a multiplier, starting at `1.0`. When a
SWAP is chosen during a search, both of its endpoints' multipliers increase by
`0.001`. A candidate's score is multiplied by the **larger** of its two
endpoints' multipliers. The multipliers reset to `1.0` whenever a gate gets
emitted, and also after every fifth search SWAP.

**Why multiply an absolute score.** Multiplying a *negative* change by 1.001
would make it more negative, rewarding reuse instead of penalizing it. So in
decay mode the code first adds back the current weighted distance total, making
the score positive, and only then multiplies:

```text
decay score = (H_now + ΔD_F + w × ΔD_E) × m
H_now = D_F + w × D_E      (the current weighted distance sum)
m     = max(multiplier of endpoint 1, multiplier of endpoint 2)
```

**Example, decision 1 in decay mode.** `H_now = 2 + 0.125 × 2 = 2.25`, and
no SWAP has been chosen yet so every multiplier is 1:

| Candidate | ΔD_F + w × ΔD_E | H_now + that | × m = 1 | Rank |
| --- | --- | --- | --- | --- |
| SWAP(p0, p1) | −1.000 | 1.250 | 1.250 | best |
| SWAP(p1, p2) | −0.750 | 1.500 | 1.500 | |
| SWAP(p2, p3) | +1.000 | 3.250 | 3.250 | |

Same ranking as before; decay changes nothing when nothing has been reused.

**Example where it matters.** Suppose two candidates both score `2.5` before
decay. One touches a position used by the previous search SWAP (multiplier
1.001); the other touches only fresh positions:

```text
Reused wire:   2.5 × 1.001 = 2.5025
Fresh wires:   2.5 × 1.000 = 2.5000   <- preferred
```

The effect is tiny by design: it breaks near-ties toward fresh wires, which
tends to let SWAPs run in parallel and lowers depth. It never overrides a
clear distance advantage. "Decay" is just the name of this heuristic; it has
nothing to do with physical decoherence.

## 9. Ties, randomness, trials, and the safety valve

### Ties

Scores within `1e-10` of the best are treated as tied. The router keeps all
tied candidates and picks one with a seeded random generator. The same seed
always gives the same route.

### Multiple trials

`swap_map` runs the routing several times with different seeds (the `trials`
argument; the default is the number of CPUs), possibly in parallel, and keeps
the attempt with the **fewest inserted SWAPs**. Among equal counts, the earliest
trial wins. Depth is not measured when choosing:

| Trial | SWAPs inserted | Depth (measured afterwards, not used) | Chosen? |
| --- | --- | --- | --- |
| 0 | 5 | 12 | No |
| 1 | 4 | 15 | **Yes** |
| 2 | 4 | 11 | No, ties trial 1 and comes later |

So "best next SWAP" and "best complete route" use different criteria. The
per-step score guides construction; the swap count picks the winner.

### The safety valve

A lower score is a preference, not a guarantee that a gate becomes executable.
After every SWAP the router checks adjacency directly. It can happen that many
SWAPs in a row improve the score without ever making a front gate adjacent,
for example by chasing lookahead or fusion gains around in circles.

To prevent that, each search has an **attempt limit** (the Python wrappers use
`10 × number of qubits`). If more than that many SWAPs are chosen without
emitting any gate, the router:

1. **Rolls back** every SWAP chosen since the last emitted gate, restoring the
   layout, the layer bookkeeping, and this checkout's history arrays.
   Already-emitted gates stay.
2. Picks the front gate whose qubits are **closest**.
3. Finds a **shortest path** between its two positions.
4. Inserts SWAPs along that path, moving both ends toward the middle, until the
   pair is adjacent, and emits the gate.

**Example** on a five-position line with the front gate CX(A, E):

```text
Start:          [A, B, C, D, E]     d(A, E) = 4
SWAP(p0, p1):   [B, A, C, D, E]     d = 3
SWAP(p1, p2):   [B, C, A, D, E]     d = 2
SWAP(p4, p3):   [B, C, A, E, D]     d = 1  -> emit CX on (p2, p3)
```

The rollback is a compiler-side undo. Those SWAPs were never sent to hardware,
so no inverse SWAPs appear in the output.

## 10. The whole loop, with every step named

The same algorithm as section 3, now with the names used in `route.rs`:

```text
State::begin
    take the compressed dependency graph (section 18), distance table, layout
    emit every initially executable gate; fill the front and lookahead layers

swap_map_trial:
    while the front layer is not empty:
        remember the output position                  (rollback point)
        until some front gate becomes adjacent:
            choose_best_swap                           (sections 6.1–6.3, 8, 11–13)
            apply it to the layout and layers          (section 6.4)
            update ready times and block history       (section 12)
            if the attempt limit is exceeded:
                roll back; force_enable_closest_node   (section 9)
        update_route:
            record the chosen SWAPs and the gate(s) that became adjacent
            decrement successors; emit anything adjacent (section 6.5)
            rebuild the lookahead layer
            advance_routed: ready times and fusion history for emitted gates
        reset decay multipliers
    return the order of SWAPs and gates, plus the initial and final layouts
```

The router's long-lived state, for readers following the code:

| Field of `State` | Remembers |
| --- | --- |
| `layout` | Who is where right now. |
| `layers` | The front and lookahead layers, as physical partner pairs. |
| `predecessors` | The readiness counters, one copy per layer. |
| `decay` | The per-position multipliers of section 8. |
| `ready` | Estimated time each position becomes free (section 12). |
| `last_block` | The most recent fusible two-qubit block on each position (section 11). |
| `ready_undo` | Enough history to roll back `ready` and `last_block`. |
| `swap_scores`, `best_swaps` | Scratch space for candidates and the tied best. |
| `rng`, `seed` | The seeded random generator. |

---

# Part III — What this checkout adds to the score

Classic SABRE scores a candidate SWAP by distances alone. Two things it cannot
see:

- **A SWAP placed right after a CX on the same pair is cheaper than three CX.**
  A later optimization pass merges them, so the SWAP's real cost is lower.
- **A SWAP on busy wires has to wait**, while a SWAP on idle wires can run in
  parallel with other work. Distance says nothing about this.

This checkout adds a term for each. Both are small compared with a distance
step, both are computed from the history of the current routing attempt, and
both apply in every heuristic mode (`basic`, `lookahead`, `decay`). Part II's
walkthrough already used them at decision 3; this part shows the numbers.

## 11. Fusion credit: a SWAP right after a CX is cheaper

### The idea

A SWAP on positions a and b can be written as three CX gates:

```text
SWAP(a, b) = CX(a, b); CX(b, a); CX(a, b)
```

If the circuit already has `CX(a, b)` immediately before the SWAP on the same
two positions, the first two CX gates in the combined sequence are identical
neighbors and cancel:

```text
Before:  CX(a, b); CX(a, b); CX(b, a); CX(a, b)       4 CX
After:                       CX(b, a); CX(a, b)       2 CX
```

A CX followed by a SWAP costs 2 native gates, not 4. More generally, any
two-qubit unitary on one pair can be re-synthesized into at most 3 CX gates, so
a block that already "costs" `c` gates grows to only `min(3, c + 1)` when a SWAP
is appended, instead of `c + 3`. Qiskit's optimization passes perform this
re-synthesis after routing. The router does not do it; it only *predicts* it
and lets the prediction influence which SWAP it chooses.

### How the router keeps track

Each two-qubit routing node carries a `block` value: an estimate of how many
native two-qubit gates it contributes to a fusible block.

| Operation | `block` value |
| --- | --- |
| CX, CY, CZ, ECR | 1 |
| Any other two-qubit gate | 3 (worst case) |
| One-qubit gate folded into the node | 0 |
| Measurement, reset, barrier, control flow | none: these end a block |

Several gates compressed into one node (section 18) add up, capped at 3.

During routing, every physical position remembers its **most recent block**:
`last_block[p] = (partner, c)`, meaning "the last two-qubit work on p was with
`partner`, and the block costs about `c` gates (at most 3)". The rules:

| Event | Effect on `last_block` |
| --- | --- |
| A two-qubit node is emitted on (a, b) | If a and b already named each other, the block continues: `c = min(3, old c + node's block)`. Otherwise a fresh block starts with the node's value. Both a and b now name each other. |
| A SWAP is chosen on (a, b) | Both records are cleared. |
| A measurement, barrier, or other block-ending node runs on p | p's record is cleared (final routing only; see section 17). |

### The credit

A candidate SWAP on (a, b) gets a credit only if `last_block[a]` names b
**and** `last_block[b]` names a. Then, with `c` the block's cost:

```text
without fusion:  c + 3          (the block, then a full SWAP)
with fusion:     min(3, c + 1)
saving         = (c + 3) − min(3, c + 1)
credit         = −saving / 3    (in units of "one SWAP", the same units as distance)
```

| Previous block cost c | Separate cost | Fused cost | Saving | Credit |
| --- | --- | --- | --- | --- |
| no eligible block | — | — | 0 | 0 |
| 1 (one CX) | 4 | 2 | 2 | −2/3 |
| 2 | 5 | 3 | 2 | −2/3 |
| 3 | 6 | 3 | 3 | −1 |

A credit of −2/3 is worth two thirds of a distance step. That is deliberately
large: it is the estimated saving in real gates.

### The walkthrough, with fusion credit

Follow `last_block` through Part II's route:

| Event | last_block afterward (position: partner, cost) |
| --- | --- |
| SWAP(p0, p1) | p0 and p1 cleared (they were empty anyway) |
| g1 = CX on (p1, p2) | p1: (p2, 1), p2: (p1, 1) |
| g2 = CX on (p1, p0) | p1: (p0, 1), p0: (p1, 1); p2 still names p1 |
| g3 = CX on (p2, p3) | p2: (p3, 1), p3: (p2, 1) |

**Decision 2.** Candidates (p0, p1) and (p2, p3). p0 and p1 name each other:
credit −2/3. p2 and p3 name each other: credit −2/3. Both get the same credit,
so the tie from section 7 remains a tie.

After SWAP(p0, p1) is chosen, p0 and p1 are cleared. p2 and p3 still name each
other, because the SWAP did not touch them.

**Decision 3.** Candidates (p0, p1), (p1, p2), (p2, p3):

| Candidate | ΔD_F | Both endpoints name each other? | Credit | Sum |
| --- | --- | --- | --- | --- |
| SWAP(p0, p1) | +1 | No (cleared) | 0 | +1 |
| SWAP(p1, p2) | −1 | No: p1 is cleared, p2 names p3 | 0 | −1 |
| SWAP(p2, p3) | −1 | Yes, from g3 | −2/3 | **−5/3** |

The credit chooses SWAP(p2, p3): after optimization, `CX(p2, p3); SWAP(p2, p3)`
becomes two CX gates instead of four.

### What the estimate does not check

The classifier is coarse on purpose, so that it costs almost nothing per
candidate. It does not verify that the later optimization pass will really
accept the block (parameters, opaque instructions, and optimization level all
matter), so a credited saving can fail to materialize. That affects route
quality only, never correctness: the router still emits a complete SWAP, and
whether it gets merged later is up to other passes.

## 12. Ready time: prefer wires that are free sooner

### The idea

Think of each physical position as having a clock: "the two-qubit work already
assigned to this wire ends at time T". A gate on two wires can start only when
both are free. If two candidate SWAPs are otherwise equal, the one whose wires
free up earlier can start earlier and is less likely to lengthen the circuit.

### The bookkeeping

`ready[p]` starts at 0 for every position and is updated like this:

```text
When a SWAP is chosen on (a, b):          end = max(ready[a], ready[b]) + 3
                                          ready[a] = ready[b] = end

When a two-qubit node is emitted on (a, b): end = max(ready[a], ready[b]) + 1
                                          ready[a] = ready[b] = end
```

A SWAP counts as 3 layers (three native gates); a routed two-qubit node counts
as 1, even if it contains several gates. Ready times belong to **positions**,
not to program qubits, so they are not exchanged by a SWAP.

Through Part II's route:

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

Read the g3 row: g3 starts at `max(4, 0) = 4` and ends at 5, the same time as
g2. The estimate correctly lets g2 and g3 overlap, because they use different
wires. Likewise the last two SWAPs overlap.

### The penalty

Each candidate SWAP on (a, b) pays

```text
ready penalty = 0.000022 × max(ready[a], ready[b])
```

The coefficient is tiny, so this term only matters when everything else is
nearly equal, but it is added to every candidate, not applied only after exact
ties. A large enough difference in ready time can outweigh a small lookahead
difference.

**Decision 3 in full** (lookahead mode, so no decay):

| Candidate | ΔD_F | Credit | Ready of endpoints | Penalty | Total |
| --- | --- | --- | --- | --- | --- |
| SWAP(p0, p1) | +1 | 0 | 8, 8 | 0.000176 | +1.000176 |
| SWAP(p1, p2) | −1 | 0 | 8, 5 | 0.000176 | −0.999824 |
| SWAP(p2, p3) | −1 | −2/3 | 5, 5 | 0.000110 | **−1.666556** |

Here the credit already decided; the ready term agrees.

### An example where the ready term decides by itself

Route the single gate `CX(A, D)` on the four-position line with occupants
`[A, B, C, D]`, distance 3. The first decision is a coin flip between
SWAP(p0, p1) and SWAP(p2, p3); say it picks SWAP(p0, p1), so A is at p1 and
`ready = [3, 3, 0, 0]`. The second decision has two candidates that bring the
pair to distance 1:

| Candidate | ΔD_F | Ready of endpoints | Penalty | Total |
| --- | --- | --- | --- | --- |
| SWAP(p1, p2) | −1 | 3, 0 | 0.000066 | −0.999934 |
| SWAP(p2, p3) | −1 | 0, 0 | 0 | **−1.000000** |

Classic SABRE would flip a second coin. This checkout prefers SWAP(p2, p3),
whose wires are idle. The two SWAPs then act on disjoint pairs and can run at
the same time, so the SWAP work takes 3 layers instead of 6. Running this in
Qiskit, every seed we tried gives SWAPs on (p0, p1) and (p2, p3), never on
(p1, p2).

### What the number means

`ready[p]` is an estimated layer index counted from the start of the circuit.
It is not seconds, not an error rate, and not a measured idle time. It is a
cheap proxy that steers the router toward parallel-friendly choices.

## 13. The complete score, in the order the code computes it

| Symbol | Meaning |
| --- | --- |
| `ΔD_F` | Change in the summed front-layer distances (section 6.2). |
| `ΔD_E` | Change in the summed lookahead-layer distances (section 6.3). |
| `b` | Basic weight: 1, or `1 / (number of front gates)` in `basic` mode. |
| `w` | Lookahead weight: `0.5 / (number of physical qubits)`. |
| `f` | Fusion credit: 0, −2/3, or −1 (section 11). |
| `H_now` | Current weighted distance total, `b × D_F + w × D_E` (section 8). |
| `m` | Larger decay multiplier of the two endpoints (section 8). |
| `T` | Larger ready time of the two endpoints (section 12). |

`choose_best_swap` computes, for every candidate:

```text
x = b × (f + ΔD_F)               fusion credit and front distance, scaled together
x = x + w × ΔD_E                 lookahead
if decay is enabled:
    x = (H_now + x) × m          make it absolute, then penalize reused wires
score = x + 0.000022 × T         ready-time penalty, added last and never multiplied
```

The candidate with the smallest score wins; ties within `1e-10` are broken at
random.

Two details that are easy to miss: the basic weight scales the fusion credit as
well as the distance change, and the ready penalty is added *after* decay, so
the decay multiplier never touches it.

The three named modes of the Python wrappers:

| Mode | `b` | Lookahead | Decay | Fusion and ready terms |
| --- | --- | --- | --- | --- |
| `basic` | `1 / number of front gates` | none | none | included |
| `lookahead` | 1 | one layer, `w = 0.5 / qubits` | none | included |
| `decay` | 1 | same as lookahead | +0.001 per use, reset every 5 search SWAPs | included |

`SabreSwap` defaults to `basic`; `SabreLayout`'s integrated path uses the
`decay` configuration. In `basic` mode the two-gate improvement of −2 from
section 6.2 would be reported as −1, because it is averaged over the two front
gates.

## 14. What these estimates do not know

Collected here so that the main text can stay simple:

- A compressed routing node (section 18) adds **1** to ready time even if it
  holds several two-qubit gates, while its fusion estimate may be 3. The two
  fields are different approximations.
- Barriers and measurements clear fusion history but do **not** synchronize
  ready times. After a global barrier, `[10, 10, 0, 0]` stays as it is, even
  though later work on p2 and p3 really has to wait.
- A chosen SWAP adds 3 to ready time even when the fusion credit predicted it
  would merge into a cheaper block.
- Nested control-flow blocks are routed with their own fresh state; the outer
  ready times do not include their duration.
- Complete routing attempts are still compared by **SWAP count** (section 9),
  not by native gate count or depth. The new terms change how each attempt is
  built, not how the winner is picked.
- Both terms are history, so the safety valve (section 9) rolls them back
  together with the layout when it undoes a failed search.

---

# Part IV — Choosing the initial layout

## 15. The idea: route it backward

Part II started with everyone in a fixed arrangement and paid one SWAP just to
make the first gate possible. If A had started next to C, that SWAP would have
been unnecessary. `SabreLayout` searches for a starting arrangement that suits
the *whole* circuit, not just its first gate, and it does so by reusing the
router.

The trick has two observations:

1. **The end of a forward route is a good arrangement for the end of the
   circuit.** Routing moves qubits toward the pairs that interact; by the last
   gate, the layout has adapted to the circuit's late interactions.
2. **Routing the dependencies in reverse carries that arrangement back to the
   start.** Reverse every "must come before" arrow, so the last gates come
   first, and route again starting from the forward route's final layout. The
   final layout of *that* route is adapted to the circuit's first gates, while
   still remembering what the end needed.

```text
starting guess L0
    ── route forward ──▶ final layout L1      (good for the end of the circuit)
    ── route backward ─▶ final layout L2      (good for the start, informed by the end)

use L2 as the initial layout, or as the next starting guess
```

"Backward" means the **dependency graph is reversed**: g4 is visited first and
g1 last. The compiler is not running the circuit in reverse time, inverting
gates, or undoing measurements. Only the pattern of *which pairs interact in
which order* matters for choosing a layout, and that pattern can be walked in
either direction.

## 16. Worked example: three SWAPs become two

**Forward pass.** Part II routed the circuit from `[A, B, C, D]` with three
SWAPs and ended at `[A, B, D, C]`. Keep only that final layout.

**Backward pass.** Reverse the dependencies, so the order of obligations is
g4, then g2 and g3, then g1. Start from `[A, B, D, C]`:

| Step | Occupants | What happens |
| --- | --- | --- |
| Start | [A, B, D, C] | The forward route's final layout. No gate is emitted to get here. |
| g4 = CX(B, D) | [A, B, D, C] | B at p1, D at p2: adjacent. Done. |
| g2 = CX(A, B), g3 = CX(C, D) | [A, B, D, C] | A–B at p0, p1 and C–D at p3, p2: both adjacent. Done. |
| g1 = CX(A, C) | [A, B, D, C] | A at p0, C at p3, distance 3. Blocked: this is the front. |
| Decision | [B, A, D, C] | Candidates (p0, p1) and (p2, p3) tie; take SWAP(p0, p1). |
| Decision | [B, A, C, D] | SWAP(p2, p3) brings C next to A (the fusion credit from g3's block prefers it). g1 is adjacent. |

The backward pass ends at `[B, A, C, D]`. (Taking the other tie branch first
leads to the same final arrangement.)

**Use it as the initial layout** and route the real circuit forward:

```text
Start at [B, A, C, D]:
  g1 = CX(A, C):  A at p1, C at p2   adjacent, emit
  g2 = CX(A, B):  A at p1, B at p0   adjacent, emit
  g3 = CX(C, D):  C at p2, D at p3   adjacent, emit
  g4 = CX(B, D):  B at p0, D at p3   distance 3: two SWAPs, as in decisions 2–3
```

**Two SWAPs instead of three.** The three forward and two backward SWAPs used
during the search were exploratory: they are discarded, not added to the
output. Only the layout they produced survives.

Larger circuits benefit from repeating the forward/backward pair several
times. Each repetition is a heuristic pass, so improvement is likely but not
guaranteed.

## 17. What one layout trial runs

`layout_trial` in `layout.rs` performs these steps for one starting guess:

1. **Complete the starting layout.** Fill in any unspecified program qubits at
   random, and add "ancilla" program qubits for every unused physical position
   so that the layout is a full permutation. A supplied partial layout only
   seeds the search; its assignments may move during the exploratory passes.
2. **Route forward once.** Keep the final layout; throw away the SWAPs.
3. **Route backward once**, starting from that layout. Keep its final layout.
4. **Repeat steps 2–3 `max_iterations` times.** The default of 3 means three
   forward and three backward passes. Each pass starts with fresh decay,
   ready-time and fusion history; only the layout is carried over.
5. **Route the real circuit** from the resulting layout, `swap_trials` times
   with different seeds, and keep the attempt with the fewest SWAPs.

The pass runs many such trials from different starting guesses and keeps the
one whose final routing used the fewest SWAPs:

| Starting guesses tried | Count |
| --- | --- |
| Fully random layouts | `layout_trials` (default: number of CPUs) |
| User-supplied partial layouts | as many as given |
| A densely connected subset of the device (`dense_layout`) | 1 |
| Program qubit *i* at position *i* | 1 |
| Program qubit *i* at position *n − 1 − i* | 1 |
| A device-specific ring, for 127-, 133- and 156-qubit IBM devices | 1 if applicable |

Three parameters, often confused:

| Parameter | Controls |
| --- | --- |
| `max_iterations` | Forward/backward pairs per starting guess. |
| `layout_trials` | Number of random starting guesses. |
| `swap_trials` | Final routing attempts per refined layout. The exploratory passes always run one attempt each. |

All of these are classical search effort. None of them run anything on a
quantum device.

**One subtlety of the exploratory passes.** They route a stripped-down graph
(`only_interactions`) that keeps the two-qubit pairs, dependencies and `block`
values but drops the original operations. Two consequences: these passes cannot
rebuild a circuit (which is fine, their output is discarded), and a barrier or
measurement that forms its own node can no longer be inspected, so it does
**not** clear fusion history during the exploratory passes the way it does in
final routing. The fusion credit can therefore be slightly more generous while
the layout is being searched than when the output is routed.

---

# Part V — The rest of the picture

## 18. How the circuit is compressed before routing

The router does not look at the original circuit gate by gate. `SabreDAG::from_dag`
first builds a smaller graph of **routing nodes**, folding together operations
that cannot change any routing decision:

- One-qubit gates never need routing, so each one is folded into the node that
  precedes it on its wire.
- Consecutive two-qubit gates on the **same pair** are folded into one node:
  once the pair is adjacent, all of them can run.

Example: five operations become one "run immediately" operation plus two
routing nodes.

| Operation | What happens to it |
| --- | --- |
| `h(0)` | Nothing precedes it and it needs no routing: it goes into `initial`, the list emitted before routing starts. |
| `cx(0, 1)` | Becomes routing node R1, a `TwoQ` node for the pair (0, 1). |
| `rz(0.1, 1)` | One-qubit gate whose only predecessor is R1: folded into R1. |
| `cx(0, 1)` | Same pair, both wires come from R1: folded into R1. R1's `block` becomes 2. |
| `cx(0, 2)` | Wire 0 comes from R1 but wire 2 is new: becomes node R2, with a dependency R1 → R2. |

The router only ever decides about R1 and R2. When R1 is routed, its three
operations are emitted together, in order. This is bookkeeping, not a claim
that the three gates are one hardware operation. In the running example of
Part II every gate changes partner, so each gate is its own node.

Each `SabreNode` holds:

| Field | Content |
| --- | --- |
| `indices` | The original circuit operations, kept so the output can be rebuilt. |
| `kind` | `TwoQ` (a pair to make adjacent), `Synchronize` (an ordering point such as a barrier or a measurement that needs no routing), or `ControlFlow` (a nested block). |
| `block` | The fusion estimate of section 11, or none if the node contains a block-ending operation. |

Operations on more than two qubits are treated as synchronization points; SABRE
expects earlier passes to have decomposed them.

## 19. Control flow and disconnected devices

**Control flow.** A circuit can contain an `if` whose body is itself a circuit.
`route_control_flow_block` routes each body recursively with its own fresh
state, then appends SWAPs that bring the body's final layout back to the layout
at its entry. That way the code after the `if` sees the same layout whichever
branch ran. Those restoring SWAPs are real output, unlike the discarded search
SWAPs of Part IV. Because every block restores its layout, the exploratory
layout passes treat control-flow nodes as plain synchronization points.

Example: a branch is entered with `[A, B, C]`, its body needs `CX(A, C)`,
routes it with SWAP(p0, p1), and ends at `[B, A, C]`. One more SWAP(p0, p1) is
appended inside the branch to return to `[A, B, C]`. An empty `else` branch
already has that layout.

**Disconnected devices.** If the device has several unconnected pieces, the
layout pass first splits the circuit into independent components and assigns
each to a piece that can hold it, then routes. A gate between two qubits on
different pieces can never be routed: no sequence of SWAPs crosses a missing
connection. If the device is fully connected, no routing is needed and both
passes return early.

## 20. Map of the Rust code and a reading order

| File | Responsibility | Names to look for |
| --- | --- | --- |
| [`mod.rs`](../crates/transpiler/src/passes/sabre/mod.rs) | Registers the Python bindings. | `sabre` |
| [`dag.rs`](../crates/transpiler/src/passes/sabre/dag.rs) | Compresses the circuit into routing nodes (section 18); computes `block`. | `SabreDAG::from_dag`, `SabreNode`, `InteractionKind`, `block_gates`, `only_interactions`, `reverse_dag` |
| [`layer.rs`](../crates/transpiler/src/passes/sabre/layer.rs) | Stores the front and lookahead layers; computes distance changes. | `Layers`, `Layer::score`, `Layer::total_score` |
| [`heuristic.rs`](../crates/transpiler/src/passes/sabre/heuristic.rs) | The configurable weights, decay settings, tie epsilon and attempt limit. | `Heuristic`, `BasicHeuristic`, `LookaheadHeuristic`, `DecayHeuristic`, `SetScaling` |
| [`route.rs`](../crates/transpiler/src/passes/sabre/route.rs) | The routing loop, scoring, safety valve, control flow, and output rebuild. | `swap_map_trial`, `choose_best_swap`, `update_route`, `advance_swap`, `advance_routed`, `force_enable_closest_node`, `swap_map`, `RoutingResult::rebuild` |
| [`layout.rs`](../crates/transpiler/src/passes/sabre/layout.rs) | The forward/backward layout search and the list of starting guesses. | `sabre_layout_and_routing`, `layout_trial`, `add_heuristic_layouts` |
| [`vec_map.rs`](../crates/transpiler/src/passes/sabre/vec_map.rs) | A typed array: `VecMap<PhysicalQubit, u32>` cannot be indexed by a program qubit by mistake. | `VecMap` |

The Python entry points are
[`SabreSwap`](../qiskit/transpiler/passes/routing/sabre_swap.py) (routing only;
the circuit's wires are already physical positions) and
[`SabreLayout`](../qiskit/transpiler/passes/layout/sabre_layout.py) (layout
search, which by default also routes). Both build a `Heuristic` from the mode
name and call into Rust through `qiskit._accelerate.sabre`.

**Suggested reading order.** `swap_map_trial` for the loop; `choose_best_swap`
for the decision; `Layer::score` for the distance delta; `update_route` for
the emit-and-advance sweep; `advance_swap` and `advance_routed` for this
checkout's history; then `SabreDAG::from_dag` and `layout_trial`. Leave
`rebuild_onto` and control flow for last.

## 21. Python examples you can run

These need Qiskit built from this checkout, including its Rust extension. The
outputs below were produced from such a build. Another Qiskit version may make
different seeded choices.

### The smallest example (section 3)

```python
from qiskit import QuantumCircuit
from qiskit.transpiler import CouplingMap
from qiskit.transpiler.passes import SabreSwap

circuit = QuantumCircuit(3)
circuit.cx(0, 2)  # g1: not adjacent on a line
circuit.cx(0, 1)  # g2: adjacent now, but it must wait for g1

routed = SabreSwap(CouplingMap.from_line(3), heuristic="lookahead", seed=0, trials=1)(circuit)
print(routed.draw(output="text"))
for instruction in routed.data:
    print(instruction.operation.name, [routed.find_bit(q).index for q in instruction.qubits])
```

Output:

```text
                 ┌───┐
q_0 -> 0 ─X──────┤ X ├
          │      └─┬─┘
q_1 -> 1 ─X───■────■──
            ┌─┴─┐
q_2 -> 2 ───┤ X ├─────
            └───┘
swap [1, 0]
cx [1, 2]
cx [1, 0]
```

Read the drawing left to right; each wire is a physical position, and
`q_0 -> 0` says program qubit `q_0` starts at position 0. The two `X` marks
joined by a line are the SWAP. Exactly as worked in section 3: SWAP(p0, p1),
then both CX gates without further SWAPs. The lookahead term makes this choice
unique, so every seed gives the same result.

### One gate, a genuine coin flip

```python
circuit = QuantumCircuit(3)
circuit.cx(0, 2)

routed = SabreSwap(CouplingMap.from_line(3), heuristic="decay", seed=7, trials=1)(circuit)
print(routed.draw(output="text"))
print(routed.count_ops())
```

Output:

```text
q_0 -> 0 ─────■──
            ┌─┴─┐
q_1 -> 1 ─X─┤ X ├
          │ └───┘
q_2 -> 2 ─X──────

OrderedDict([('swap', 1), ('cx', 1)])
```

With no second gate there is nothing to look ahead to, every ready time is 0,
and no fusible block exists. SWAP(0, 1) and SWAP(1, 2) score identically, and
the seeded generator picks one. Over many seeds, both appear.

### The running example (Part II)

```python
A, B, C, D = range(4)
circuit = QuantumCircuit(4)
circuit.cx(A, C)  # g1
circuit.cx(A, B)  # g2
circuit.cx(C, D)  # g3
circuit.cx(B, D)  # g4

hardware = CouplingMap.from_line(4)
routed = SabreSwap(hardware, heuristic="lookahead", seed=0, trials=1)(circuit)

print("SWAPs:", routed.count_ops().get("swap", 0))
print("Final program-to-position:", routed.layout.final_index_layout())
for instruction in routed.data:
    print(instruction.operation.name, [routed.find_bit(q).index for q in instruction.qubits])
```

Output:

```text
SWAPs: 3
Final program-to-position: [0, 1, 3, 2]
swap [1, 0]
cx [1, 2]
cx [1, 0]
swap [1, 0]
cx [2, 3]
swap [2, 3]
cx [1, 2]
```

The final array lists the position of **A, B, C, D** in that order: C ends at
p3 and D at p2, matching `[A, B, D, C]` from section 7. (This array is the
inverse view of the occupant rows used in the text, which list program qubits
by position.)

Two things look different from the hand-worked table and are not:

- `cx [2, 3]` (g3) is printed *after* the second SWAP, whereas section 7 emitted
  g3 before it. The two act on disjoint positions, so the circuit's listing
  order between them is arbitrary; the dependencies are identical.
- The second SWAP was a coin flip (section 7). Any seed gives this same output,
  because the other branch chooses the same two SWAPs in the other order, and
  the printed listing sorts them the same way.

### Let SABRE choose the initial layout (Part IV)

```python
from qiskit.transpiler import PassManager
from qiskit.transpiler.passes import SabreLayout

compiled = PassManager([
    SabreLayout(hardware, seed=0, max_iterations=1, layout_trials=1, swap_trials=1)
]).run(circuit)

print("SWAPs:", compiled.count_ops().get("swap", 0))
print("Initial:", compiled.layout.initial_index_layout(filter_ancillas=True))
print("Final:", compiled.layout.final_index_layout())
print(compiled.draw(output="text"))
```

Output:

```text
SWAPs: 2
Initial: [1, 0, 2, 3]
Final: [0, 1, 3, 2]
              ┌───┐
q_1 -> 0 ─────┤ X ├─X──────
              └─┬─┘ │
q_0 -> 1 ──■────■───X───■──
         ┌─┴─┐        ┌─┴─┐
q_2 -> 2 ┤ X ├──■───X─┤ X ├
         └───┘┌─┴─┐ │ └───┘
q_3 -> 3 ─────┤ X ├─X──────
              └───┘
```

`Initial: [1, 0, 2, 3]` means A starts at p1 and B at p0: occupants
`[B, A, C, D]`, the layout found in section 16. The first SWAP is gone; the
two SWAPs for g4 remain. `layout_trials=1` asks for one random starting guess,
but the pass also tries its built-in guesses (section 17), so this shows the
improvement without claiming the search visited only the hand-worked path.

The output still contains high-level `swap` operations. Translating them to the
device's native gates and merging them with neighbors is done by later passes.

## 22. Common confusions

**"Does a SWAP physically move the qubits?"** No. The positions are fixed
hardware. A SWAP exchanges the *states* held by two positions; the layout
records where each program qubit's state now lives.

**"Why is a good score negative?"** Scores are *changes* in summed distance.
Negative means the SWAP brings pairs closer. It is not a count of anything.

**"Why isn't the front layer just all ready gates?"** Ready gates that are
also adjacent are emitted on the spot, so they never wait in the front layer.
The front layer holds the gates that need a SWAP.

**"Why is g2 in the lookahead layer when it is already adjacent?"** So that
the score can notice a SWAP that would pull it apart. Being adjacent now is
information worth protecting.

**"Does SABRE consider every possible SWAP?"** No. Only hardware edges that
touch a front-layer qubit are candidates. Interior edges can still be reached,
one decision at a time, as the front qubits move.

**"Is the result optimal?"** No. SABRE makes one cheap, informed choice at a
time and never looks back further than the safety valve's rollback. It is fast
and usually good, which is the point of a heuristic.

**"Is decay about decoherence?"** No. It is a small multiplier that
discourages reusing the same wires in consecutive search SWAPs.

**"Why does my printed circuit order differ from the walkthrough?"**
Independent operations on disjoint positions can be listed in any order. Only
the dependencies are fixed.

**"Does the final layout have to be undone?"** Not at the top level. Qiskit
records the final layout, and measurements are placed on the correct positions.
Inside control-flow blocks, the layout *is* restored, with real SWAPs.

**"Are the search SWAPs of the layout pass in my circuit?"** No. Forward and
backward passes only produce a layout; their SWAPs are thrown away. Only the
final routing's SWAPs appear in the output.

**"Does `heuristic="basic"` turn off fusion and ready-time scoring?"** No. In
this checkout all three modes include them. `basic` only removes lookahead and
decay and averages the distance term over the front gates.

## 23. Check your understanding

Try to answer before reading the explanation.

**1. g2's qubits are adjacent at the start. Why not emit it first?**

Because g2 depends on g1, which has not been emitted. Adjacency is one of two
conditions; readiness is the other. Lookahead may score g2's distance, but that
is a prediction, not permission to run it.

**2. On `p0 – p1 – p2 – p3 – p4`, the only front gate uses p0 and p4. Is
`(p1, p2)` a candidate?**

No. Neither endpoint holds a front qubit. The candidates are `(p0, p1)` and
`(p3, p4)`. After one of them is applied, the front qubit has moved, and the
candidate list is rebuilt around its new position.

**3. Why not always move one qubit along the shortest path to its partner?**

Because that ignores every other pending and upcoming gate. In section 6,
SWAP(p1, p2) would have enabled g1 and separated both g2 and g3. Scoring the
whole front plus a lookahead avoids that. The shortest-path move is reserved
for the safety valve.

**4. The route of section 7 contains SWAP(p0, p1) twice. Do they cancel?**

No. Gates that use A and B in their swapped positions run between them.
Removing both SWAPs while leaving those gates on the same physical positions
would change the computation.

**5. The last ready time in section 12 is 9. Is the circuit's two-qubit depth
9?**

Not necessarily. The ready time is an estimate under a fixed accounting rule
(SWAP = 3 layers, routed node = 1). Later synthesis, fusion, barriers and
control flow can all make the real depth differ.

**6. Why does the layout pass route backward if the circuit runs forward?**

To carry what the end of the circuit needs back to the start. The forward pass
adapts the layout to the late gates; the backward pass adapts that layout to
the early gates. Only the resulting layout is kept; the output is then routed
forward from it.

**7. Can a route with fewer SWAPs still have more native gates or more depth?**

Yes. Fusion makes some SWAPs cheaper than others, and parallel SWAPs cost less
depth than sequential ones. Complete attempts are still compared by SWAP count,
so the chosen attempt is not guaranteed to win on those other measures.

**8. In section 12's single-gate example, what would classic SABRE do at the
second decision?**

Flip a coin between SWAP(p1, p2) and SWAP(p2, p3). Half the time it would pick
SWAP(p1, p2), which must wait for the first SWAP to finish, making the two
SWAPs sequential. The ready-time term always picks the idle pair.

## 24. Glossary

| Term | Meaning |
| --- | --- |
| Program qubit (logical, virtual) | A qubit name in the user's circuit: A, B, C, D. |
| Physical position (physical qubit) | A numbered place on the device: p0, p1, ... |
| Layout | The mapping from program qubits to physical positions, updated after every SWAP. |
| Coupling map | The device's graph of which positions can run a two-qubit gate. |
| Distance | Fewest edges between two positions. A pair needs distance 1 to interact. |
| Ready | All of a gate's predecessors have been emitted. |
| Adjacent | A gate's two qubits are on connected positions. |
| Front layer `F` | Gates that are ready but not adjacent. |
| Lookahead layer `E` | Gates that would become ready once the front layer is done. |
| Candidate SWAP | A hardware edge touching at least one front-layer qubit. |
| `ΔD_F`, `ΔD_E` | Change in summed front / lookahead distances caused by a candidate. |
| Decay | A per-position multiplier that discourages reusing recently swapped wires. |
| Fusion credit | This checkout's estimate of native gates saved when a SWAP merges into the preceding block on the same pair. |
| Ready time | This checkout's estimate of when a position's assigned two-qubit work ends. |
| Safety valve | Rollback plus shortest-path SWAPs when a search makes no progress. |
| Trial | One complete routing attempt with one seed. The attempt with fewest SWAPs wins. |
| Forward / backward pass | Routing the dependency graph in its normal or reversed direction to refine the initial layout. |
| Heuristic | A cheap rule for ranking choices that usually works well but offers no optimality guarantee. |
