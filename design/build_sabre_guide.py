#!/usr/bin/env python3
"""Regenerate the SABRE state figures and PDF (requires pandoc and typst).

Run from any directory: python3 design/build_sabre_guide.py
Use --figures-only to rebuild the SVGs without the PDF tools.
The assertions replay each documented route, checking dependencies, adjacency,
layouts and the front/lookahead sets before drawing the figures.
"""

from dataclasses import dataclass
from html import escape
from pathlib import Path
import argparse
import re
import subprocess
import tempfile


DESIGN = Path(__file__).resolve().parent
FIGURES = DESIGN / "figures" / "sabre_states"
GATES = {
    1: (2, 3), 2: (5, 6), 3: (1, 2), 4: (3, 4),
    5: (2, 3), 6: (4, 6), 7: (5, 4), 8: (4, 1),
}
EDGES = [
    (1, 3, 2), (1, 4, 3), (3, 5, 2), (4, 5, 3), (4, 6, 4),
    (2, 6, 6), (2, 7, 5), (6, 7, 4), (7, 8, 4), (3, 8, 1),
]
INK, MUTED, LINE = "#182c42", "#526477", "#d5dee7"
BLUE, BLUE_BG = "#165f98", "#e9f3fc"
AMBER, AMBER_BG = "#875400", "#fff3d8"
GREEN, GREEN_BG = "#176850", "#e8f5ef"


@dataclass
class State:
    name: str
    title: str
    occupants: tuple
    done: frozenset
    front: frozenset
    lookahead: frozenset
    note: str
    swap: tuple = ()
    reverse: bool = False
    small: bool = False
    transient: bool = False

    @property
    def gates(self):
        return {1: ("A", "C"), 2: ("A", "B")} if self.small else GATES

    @property
    def edges(self):
        edges = [(1, 2, "A")] if self.small else EDGES
        return [(b, a, q) for a, b, q in edges] if self.reverse else edges

    @property
    def positions(self):
        return {q: p for p, q in enumerate(self.occupants)}

    def distance(self, gate):
        a, b = self.gates[gate]
        return abs(self.positions[a] - self.positions[b])

    def validate(self):
        assert len(set(self.occupants)) == len(self.occupants)
        predecessors = {g: {a for a, b, _ in self.edges if b == g} for g in self.gates}
        pending = self.gates.keys() - self.done
        ready = {g for g in pending if predecessors[g] <= self.done}
        if not self.transient:
            assert ready == self.front, (self.name, ready, self.front)
            assert all(self.distance(g) > 1 for g in ready), self.name
            expected = {g for g in pending - ready if predecessors[g] <= self.done | ready}
            # Document the checkout's incremental-lookahead gap (section 6.5).
            if self.name == "s1":
                assert expected == {5, 6}
                expected.remove(5)
            assert expected == self.lookahead, (self.name, expected, self.lookahead)
        assert not self.done & (self.front | self.lookahead)
        assert not self.front & self.lookahead
        matrix = [[abs(self.positions[a] - self.positions[b])
                   for b in sorted(self.occupants)] for a in sorted(self.occupants)]
        assert all(matrix[i][j] == matrix[j][i] for i in range(len(matrix))
                   for j in range(len(matrix)))


def route(prefix, title, start, initial, steps, reverse=False):
    occupants, done, result = list(start), set(), []
    edges = [(b, a, q) for a, b, q in EDGES] if reverse else EDGES
    predecessors = {g: {a for a, b, _ in edges if b == g} for g in GATES}
    for index, (swap, emitted, front, lookahead, note) in enumerate([initial] + steps):
        if swap:
            a, b = swap
            assert abs(a - b) == 1
            occupants[a], occupants[b] = occupants[b], occupants[a]
        for gate in emitted:
            assert gate not in done and predecessors[gate] <= done, (prefix, index, gate)
            a, b = GATES[gate]
            assert abs(occupants.index(a) - occupants.index(b)) == 1
            done.add(gate)
        result.append(State(
            f"{prefix}{index}", f"{title}{index}", tuple(occupants), frozenset(done),
            frozenset(front), frozenset(lookahead), note, swap, reverse,
        ))
    return result


def states():
    main = route("s", "Routing state S", (1, 2, 6, 3, 4, 5),
        ((), (), (1, 2), (3, 4), "Before decision 1. Initial one-qubit gates have already been emitted."), [
        ((2, 3), (1, 3), (2, 4), (6,), "After decision 1: emit g1, then g3. Next: decision 2."),
        ((3, 4), (2, 4, 6, 5), (7,), (8,), "After decision 2: emit g2, g4, g6 and g5. Next: decision 3."),
        ((3, 4), (7,), (8,), (), "After decision 3: emit g7. Next: decision 4."),
        ((0, 1), (), (8,), (), "After decision 4: no CX emitted; g8 is still blocked. Next: decision 5."),
        ((1, 2), (), (8,), (), "After decision 5: no CX emitted; g8 is still blocked. Next: decision 6."),
        ((3, 4), (8,), (), (), "After decision 6: emit g8. Routing is complete."),
    ])
    backward = route("b", "Backward state B", main[-1].occupants,
        ((), (5, 8), (3, 7), (6,), "Before B1: visit g5 and g8. Arrows now point toward earlier circuit gates."), [
        ((0, 1), (3,), (7,), (6,), "After B1: visit g3. g1 still waits for g4. Next: B2."),
        ((3, 4), (7, 6), (2, 4), (1,), "After B2: visit g7, then g6. Next: B3."),
        ((3, 4), (2,), (4,), (1,), "After B3: visit g2. g4 is still blocked. Next: B4."),
        ((0, 1), (), (4,), (1,), "After B4: no CX visited; g4 is still blocked. Next: B5."),
        ((2, 3), (4, 1), (), (), "After B5: visit g4, then g1. Keep this layout; discard the exploratory SWAPs."),
    ], reverse=True)
    refined = route("r", "Refined forward state R", backward[-1].occupants,
        ((), (1, 2, 4), (3, 6), (5, 7), "Before R1: emit g1, g2 and g4 without a SWAP."), [
        ((2, 3), (6,), (3, 7), (5, 8), "After R1: emit g6. g7 joins g3 in the front. Next: R2."),
        ((3, 4), (7,), (3,), (5, 8), "After R2: emit g7. g8 still waits for g3. Next: R3."),
        ((0, 1), (3, 5), (8,), (), "After R3: emit g3, then g5. Next: R4."),
        ((2, 3), (8,), (), (), "After R4: emit g8. Four SWAPs; the final mapping is shown above."),
    ])
    intermediate = [
        State("s1-swap", "Decision 1 / immediately after the SWAP", main[1].occupants,
              frozenset(), frozenset((1, 2)), frozenset((3, 4)),
              "SWAP(p2,p3) changed locations only. g1 is now executable; no CX has been emitted yet.",
              (2, 3), transient=True),
        State("s1-emit", "Decision 1 / after emitting g1", main[1].occupants,
              frozenset((1,)), frozenset((2, 4)), frozenset(),
              "g3 is ready and adjacent: emit it next. Lookahead is rebuilt after the sweep.",
              transient=True),
        State("decay5", "Decay variant / after decision 5", (2, 1, 3, 4, 6, 5),
              main[4].done, frozenset((8,)), frozenset(),
              "Decay chooses SWAP(p3,p4) at decision 5. SWAP(p1,p2) then reaches state S6.",
              (3, 4)),
    ]
    small = [
        State("small-start", "Small example / steps 1-4", ("A", "B", "C"),
              frozenset(), frozenset((1,)), frozenset((2,)),
              "Read the front, build lookahead, enumerate candidates, score. The real state stays fixed.",
              small=True),
        State("small-swap", "Small example / step 5", ("B", "A", "C"),
              frozenset(), frozenset((1,)), frozenset((2,)),
              "Apply SWAP(p0,p1). g1 becomes executable; dependencies have not changed.",
              (0, 1), small=True, transient=True),
        State("small-emit", "Small example / step 6, after g1", ("B", "A", "C"),
              frozenset((1,)), frozenset(), frozenset(),
              "Emit g1 on (p1,p2). g2 is now ready and adjacent: emit it on (p1,p0).",
              small=True, transient=True),
        State("small-done", "Small example / step 7", ("B", "A", "C"),
              frozenset((1, 2)), frozenset(), frozenset(),
              "Both gates emitted. The final layout is A at p1, B at p0, C at p2.", small=True),
    ]
    result = small + main + intermediate + backward + refined
    for state in result:
        state.validate()
    return result


def qname(q):
    return f"q{q}" if isinstance(q, int) else q


def gset(gates):
    return "{" + ", ".join(f"g{g}" for g in sorted(gates)) + "}"


class SVG:
    def __init__(self, height, title):
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="{height}" '
                      f'viewBox="0 0 1000 {height}" role="img"><title>{escape(title)}</title>',
                      '<defs><marker id="arrow" markerWidth="8" markerHeight="8" '
                      'refX="7" refY="4" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8" '
                      'fill="#687e91"/></marker></defs>',
                      f'<rect width="1000" height="{height}" fill="white"/>']

    def text(self, x, y, value, size=20, color=INK, anchor="start", bold=False):
        self.parts.append(f'<text x="{x}" y="{y}" font-family="DejaVu Sans, sans-serif" '
                          f'font-size="{size}" fill="{color}" text-anchor="{anchor}" '
                          f'font-weight="{"bold" if bold else "normal"}">{escape(str(value))}</text>')

    def rect(self, x, y, width, height, fill="white", stroke=LINE, radius=6):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
                          f'rx="{radius}" fill="{fill}" stroke="{stroke}"/>')

    def path(self, points, color=LINE, width=2, dash=False, arrow=False, reverse=False):
        data = "M" + " L".join(f"{x},{y}" for x, y in points)
        marker = f' marker-{"start" if reverse else "end"}="url(#arrow)"' if arrow else ""
        self.parts.append(f'<path d="{data}" fill="none" stroke="{color}" '
                          f'stroke-width="{width}"' + (' stroke-dasharray="7,5"' if dash else '') + marker + '/>')

    def save(self, path):
        path.write_text("\n".join(self.parts) + "\n</svg>\n", encoding="utf-8")


def draw_graph(svg, state, y):
    if state.small:
        nodes = {1: (240, 50), 2: (760, 50)}
        paths = {(1, 2): ([(308, 50), (692, 50)], (500, 33))}
        source_edges = [(1, 2, "A")]
    else:
        nodes = {1: (90, 140), 2: (90, 350), 3: (310, 100), 4: (310, 280),
                 5: (540, 100), 6: (540, 320), 7: (750, 320), 8: (910, 200)}
        paths = {
            (1, 3): ([(158, 130), (242, 110)], (200, 98)),
            (1, 4): ([(139, 173), (261, 247)], (175, 224)),
            (3, 5): ([(378, 100), (472, 100)], (425, 87)),
            (4, 5): ([(355, 247), (495, 133)], (442, 206)),
            (4, 6): ([(378, 291), (472, 308)], (426, 280)),
            (2, 6): ([(158, 350), (472, 328)], (302, 360)),
            (2, 7): ([(90, 383), (90, 422), (750, 422), (750, 353)], (428, 412)),
            (6, 7): ([(608, 320), (682, 320)], (645, 302)),
            (7, 8): ([(794, 287), (866, 233)], (867, 279)),
            (3, 8): ([(310, 67), (310, 26), (910, 26), (910, 167)], (615, 18)),
        }
        source_edges = EDGES
    for a, b, q in source_edges:
        points, (tx, ty) = paths[a, b]
        svg.path([(x, yy + y) for x, yy in points], "#687e91", arrow=True, reverse=state.reverse)
        label = f"{qname(q)},p{state.positions[q]}"
        svg.rect(tx - 46, ty + y - 20, 92, 26, "white", "none", 0)
        svg.text(tx, ty + y, label, 19, anchor="middle")
    ready = {g for g in state.gates.keys() - state.done
             if {a for a, b, _ in state.edges if b == g} <= state.done}
    for gate, (x, yy) in nodes.items():
        yy += y
        if gate in state.done:
            fill, color, status = "#edf0f3", MUTED, "done"
        elif gate in ready and state.distance(gate) == 1:
            fill, color, status = GREEN_BG, GREEN, "emit next"
        elif gate in state.front:
            fill, color, status = BLUE_BG, BLUE, "F"
        elif gate in state.lookahead:
            fill, color, status = AMBER_BG, AMBER, "E"
        else:
            fill, color, status = "white", MUTED, "wait"
        svg.rect(x - 68, yy - 33, 136, 66, fill, color)
        svg.text(x, yy - 7, f"g{gate}  {status}", 20, color, "middle", True)
        a, b = state.gates[gate]
        svg.text(x, yy + 18, f"{qname(a)},{qname(b)}", 18, color, "middle")
        if gate in state.front:
            svg.path([(x - 55, yy + 39), (x + 55, yy + 39)], BLUE, 3, dash=True)
        if gate in state.lookahead:
            svg.text(x, yy + 54, "* * *", 23, AMBER, "middle", True)


def draw_state(state):
    height = 590 if state.small else 1032
    svg = SVG(height, state.title)
    svg.text(24, 35, state.title, 27, bold=True)
    svg.text(24, 67, state.note, 18)
    n = len(state.occupants)
    xs = [90 + i * 820 / (n - 1) for i in range(n)]
    svg.text(24, 103, "Physical line / current occupants", 18, MUTED)
    for p in range(n - 1):
        highlighted = state.swap and set(state.swap) == {p, p + 1}
        svg.path([(xs[p], 137), (xs[p + 1], 137)], GREEN if highlighted else LINE, 6 if highlighted else 3)
    for p, q in enumerate(state.occupants):
        svg.rect(xs[p] - 48, 114, 96, 47, GREEN_BG if p in state.swap else "#f5f8fb")
        svg.text(xs[p], 144, f"{qname(q)},p{p}", 21, anchor="middle", bold=p in state.swap)
    svg.text(24, 191, "Dependency graph" + (" / reversed arrows" if state.reverse else " / forward arrows"), 21, bold=True)
    draw_graph(svg, state, 208)
    top = 330 if state.small else 696
    svg.text(24, top, "---  F: front", 18, BLUE, bold=True)
    svg.text(198, top, "***  E: lookahead", 18, AMBER, bold=True)
    svg.text(451, top, "done: already emitted" if not state.reverse else "done: already visited", 18, MUTED)
    svg.text(752, top, "wait: later gate", 18, MUTED)
    svg.text(24, top + 33, "Distances between logical qubits", 21, bold=True)
    svg.text(530, top + 33, "Layers at this snapshot", 21, bold=True)
    svg.text(530, top + 64, f"F = {gset(state.front)}", 20, BLUE, bold=True)
    lookahead = "E: rebuild pending" if state.name in {"s1-emit", "small-emit"} else f"E = {gset(state.lookahead)}"
    svg.text(530, top + 92, lookahead, 20, AMBER, bold=True)
    svg.text(530, top + 127, "Gate / physical operands / distance", 18, MUTED)
    displayed = sorted(state.front) + sorted(state.lookahead)
    if state.transient:
        displayed += [g for g in sorted(state.gates.keys() - state.done) if g not in displayed
                      and {a for a, b, _ in state.edges if b == g} <= state.done]
    for i, gate in enumerate(displayed):
        a, b = state.gates[gate]
        color = BLUE if gate in state.front else AMBER if gate in state.lookahead else GREEN
        svg.text(530, top + 157 + 28 * i,
                 f"g{gate}: (p{state.positions[a]},p{state.positions[b]})   d = {state.distance(gate)}", 20, color)
    if not displayed:
        svg.text(530, top + 161, "No gates remain to route.", 20, GREEN)
    qubits = sorted(state.occupants)
    cell, left, grid_top = (57 if not state.small else 93), 67, top + 52
    active = {tuple(sorted(state.gates[g])): BLUE_BG for g in state.front}
    active.update({tuple(sorted(state.gates[g])): AMBER_BG for g in state.lookahead})
    for i, q in enumerate(qubits):
        svg.text(left + (i + 1) * cell + cell / 2, grid_top + 20, qname(q), 19, anchor="middle", bold=True)
        svg.text(left + cell / 2, grid_top + 20 + (i + 1) * 30, qname(q), 19, anchor="middle", bold=True)
        for j, other in enumerate(qubits):
            x, yy = left + (j + 1) * cell, grid_top + (i + 1) * 30
            key = tuple(sorted((q, other)))
            fill = active.get(key, "#f1f4f7" if q == other else "white")
            svg.rect(x, yy, cell, 30, fill, LINE, 0)
            svg.text(x + cell / 2, yy + 21, abs(state.positions[q] - state.positions[other]), 19, anchor="middle")
    svg.text(24, height - 40, "Edge labels give current locations, even beside completed gates. They are not hardware links.", 17, MUTED)
    footer = "A SWAP changes the matrix; emitting a gate leaves the layout and matrix unchanged."
    if state.name == "s1":
        footer = "E matches this checkout: g5 is omitted by its incremental update (section 6.5)."
    elif state.transient:
        footer = "Inside the emit sweep: the marked F/E are not yet a new scoring state. Green means executable."
    svg.text(24, height - 14, footer, 17, MUTED)
    svg.save(FIGURES / f"{state.name}.svg")



# --- Section 11 figures: fusion credit -------------------------------------

def draw_circuit(svg, x0, y0, wires, ops, pitch=90, gap=64, label_w=64):
    """Draw a small circuit. ops: ("cx", ctrl, tgt), ("swap", a, b), ("1q", wire, text).
    Returns the x coordinate of each op's column."""
    xs = [x0 + label_w + pitch * (i + 0.5) for i in range(len(ops))]
    right = x0 + label_w + pitch * len(ops)
    for i, name in enumerate(wires):
        y = y0 + i * gap
        svg.text(x0 + label_w - 12, y + 7, name, 19, anchor="end", bold=True)
        svg.path([(x0 + label_w, y), (right, y)], "#687e91", 2)
    for x, op in zip(xs, ops):
        kind = op[0]
        if kind == "cx":
            ya, yb = y0 + op[1] * gap, y0 + op[2] * gap
            svg.path([(x, ya), (x, yb)], INK, 2.5)
            svg.parts.append(f'<circle cx="{x}" cy="{ya}" r="7" fill="{INK}"/>')
            svg.parts.append(f'<circle cx="{x}" cy="{yb}" r="12" fill="white" stroke="{INK}" stroke-width="2.5"/>')
            svg.path([(x - 12, yb), (x + 12, yb)], INK, 2.5)
            svg.path([(x, yb - 12), (x, yb + 12)], INK, 2.5)
        elif kind == "swap":
            ya, yb = y0 + op[1] * gap, y0 + op[2] * gap
            svg.path([(x, ya), (x, yb)], INK, 2.5)
            for y in (ya, yb):
                svg.path([(x - 9, y - 9), (x + 9, y + 9)], INK, 2.5)
                svg.path([(x - 9, y + 9), (x + 9, y - 9)], INK, 2.5)
        elif kind == "1q":
            y = y0 + op[1] * gap
            svg.rect(x - 19, y - 19, 38, 38, "white", INK, 4)
            svg.text(x, y + 7, op[2], 18, anchor="middle", bold=True)
    return xs


def draw_fusion_cancel():
    svg = SVG(800, "Why a SWAP right after a CX is cheaper: the two meeting CX gates cancel")
    svg.text(24, 35, "Why a SWAP right after a CX is cheaper", 27, bold=True)
    svg.text(24, 67, "Decision 3 of the running route: g6 = CX(p3, p4) was just emitted, and SWAP(p3, p4) is a candidate.", 18)
    wires = ["p3", "p4"]
    # Panel 1: what the router emits.
    svg.text(24, 118, "1.  What the router would place", 21, bold=True)
    svg.text(560, 118, "cost: 1 + 3 = 4 CX", 20, MUTED, bold=True)
    xs = draw_circuit(svg, 24, 165, wires, [("cx", 0, 1), ("swap", 0, 1)])
    svg.text(xs[0], 272, "g6", 18, MUTED, "middle")
    svg.text(xs[1], 272, "SWAP", 18, MUTED, "middle")
    # Panel 2: expand the swap.
    svg.text(24, 330, "2.  The SWAP written as three CX", 21, bold=True)
    svg.text(560, 330, "still 4 CX", 20, MUTED, bold=True)
    ops = [("cx", 0, 1), ("cx", 0, 1), ("cx", 1, 0), ("cx", 0, 1)]
    y0 = 395
    xs = draw_circuit(svg, 24, y0, wires, ops)
    svg.rect(xs[0] - 36, y0 - 40, xs[1] - xs[0] + 72, 64 + 80, GREEN_BG, GREEN, 8)
    draw_circuit(svg, 24, y0, wires, ops)
    svg.text(xs[0], y0 + 128, "g6", 18, MUTED, "middle")
    svg.text((xs[1] + xs[3]) / 2, y0 + 128, "the SWAP, as three CX", 18, MUTED, "middle")
    tx = xs[3] + 70
    svg.text(tx, y0 - 4, "The boxed CX gates are identical and adjacent.", 18, GREEN, bold=True)
    svg.text(tx, y0 + 22, "A CX undoes itself, so together they do nothing", 18, GREEN)
    svg.text(tx, y0 + 48, "and can be removed.", 18, GREEN)
    # Panel 3: after cancelling.
    svg.text(24, 580, "3.  After removing the pair", 21, bold=True)
    svg.text(560, 580, "2 CX", 20, GREEN, bold=True)
    xs = draw_circuit(svg, 24, 640, wires, [("cx", 1, 0), ("cx", 0, 1)])
    svg.text(xs[1] + 70, 640 + 32, "Same effect on p3 and p4 as g6 followed by the SWAP.", 18, MUTED)
    svg.text(xs[1] + 70, 640 + 58, "There is no separate SWAP left to run.", 18, MUTED)
    svg.text(24, 764, "Saving: 2 CX. One SWAP is 3 CX, so in the score's units (SWAPs) the credit is -2/3.", 17, MUTED)
    svg.save(FIGURES / "fusion-cancel.svg")


def draw_fusion_nodes():
    svg = SVG(470, "Before routing, each two-qubit routing node gets a block estimate")
    svg.text(24, 35, "Step 1: each routing node carries a block estimate", 27, bold=True)
    svg.text(24, 67, "Consecutive operations on the same pair of program qubits form one routing node. Under each gate: its contribution.", 18)
    wires = ["q0", "q1", "q2"]
    ops = [("cx", 0, 1), ("1q", 0, "H"), ("cx", 1, 0), ("1q", 1, "Rz"), ("cx", 1, 2)]
    y0 = 150
    xs = draw_circuit(svg, 24, y0, wires, ops, pitch=120)
    svg.rect(xs[0] - 50, y0 - 46, xs[3] - xs[0] + 100, 64 * 2 + 92, BLUE_BG, BLUE, 10)
    svg.rect(xs[4] - 50, y0 - 46, 100, 64 * 2 + 92, AMBER_BG, AMBER, 10)
    draw_circuit(svg, 24, y0, wires, ops, pitch=120)
    for x, c in zip(xs, ["1", "0", "1", "0", "1"]):
        svg.text(x, y0 + 64 * 2 + 72, c, 22, INK, "middle", True)
    svg.text(xs[0] - 50, y0 - 58, "R1: pair (q0, q1)", 19, BLUE, bold=True)
    svg.text(xs[4] - 50, y0 - 58, "R2: pair (q1, q2)", 19, AMBER, bold=True)
    svg.path([(xs[3] + 50, y0 + 64), (xs[4] - 50, y0 + 64)], "#687e91", 2.5, arrow=True)
    svg.text((xs[3] + xs[4]) / 2, y0 + 64 - 14, "waits for", 16, MUTED, "middle")
    svg.text(24, 395, "R1.block = min(3, 1 + 0 + 1 + 0) = 2", 21, BLUE, bold=True)
    svg.text(560, 395, "R2.block = 1", 21, AMBER, bold=True)
    svg.text(24, 430, "CX, CY, CZ, ECR count 1; any other two-qubit gate counts 3; one-qubit gates count 0; the sum is capped at 3.", 17, MUTED)
    svg.text(24, 454, "A measurement, reset, barrier or control-flow operation inside a node sets block = None: that node ends any run.", 17, MUTED)
    svg.save(FIGURES / "fusion-nodes.svg")


FUSION_EVENTS = [
    # (label, kind, (a, b), node_block)
    ("SWAP(p2, p3)", "swap", (2, 3), None),
    ("g1 = CX(p1, p2)", "gate", (1, 2), 1),
    ("g3 = CX(p0, p1)", "gate", (0, 1), 1),
    ("SWAP(p3, p4)", "swap", (3, 4), None),
    ("g2 = CX(p5, p4)", "gate", (5, 4), 1),
    ("g4 = CX(p2, p3), 2 H", "gate", (2, 3), 1),
    ("g6 = CX(p3, p4), Z", "gate", (3, 4), 1),
    ("g5 = CX(p1, p2)", "gate", (1, 2), 1),
]
STRIP_XS = [300 + i * 640 / 5 for i in range(6)]


def replay_last_block(events, n=6):
    """Apply route.rs's last_block rules; yield the record after each event."""
    last = [None] * n
    for _, kind, (a, b), block in events:
        if kind == "swap":
            last[a] = last[b] = None
        else:
            open_c = last[a][1] if (last[a] and last[b] and last[a][0] == b and last[b][0] == a) else 0
            c = 0 if block is None else min(3, open_c + block)
            last[a] = (b, c) if c > 0 else None
            last[b] = (a, c) if c > 0 else None
        yield list(last)


def draw_last_block_strip(svg, y, last, xs, occupants=None, swap=None):
    n = len(last)
    for p in range(n - 1):
        svg.path([(xs[p], y), (xs[p + 1], y)], LINE, 3)
    for p, rec in enumerate(last):
        if rec is None:
            continue
        partner, c = rec
        mutual = last[partner] is not None and last[partner][0] == p
        if mutual and p < partner:
            svg.path([(xs[p], y), (xs[partner], y)], GREEN, 7)
            svg.text((xs[p] + xs[partner]) / 2, y - 30, f"c = {c}", 17, GREEN, "middle", True)
        elif not mutual:
            direction = 1 if partner > p else -1
            svg.path([(xs[p] + direction * 44, y + 28), (xs[partner] - direction * 50, y + 28)],
                     AMBER, 2.5, dash=True, arrow=True)
    for p in range(n):
        label = f"p{p}" if occupants is None else f"{occupants[p]},p{p}"
        highlighted = swap is not None and p in swap
        svg.rect(xs[p] - 42, y - 20, 84, 40, GREEN_BG if highlighted else "#f5f8fb")
        svg.text(xs[p], y + 7, label, 19, anchor="middle", bold=highlighted)


def draw_fusion_history():
    events = FUSION_EVENTS
    rows = list(replay_last_block(events))
    final = rows[-1]
    assert final == [(1, 1), (2, 1), (1, 1), (4, 1), (3, 1), (4, 1)], final
    strip = 96
    height = 190 + strip * len(events) + 50
    svg = SVG(height, "last_block after each event of the running route, decisions 1 to 3")
    svg.text(24, 35, "Step 2: what each wire remembers, event by event", 27, bold=True)
    svg.text(24, 67, "The running route from decision 1 up to decision 3. After each event: which wire names which, and the run cost c.", 18)
    xs = STRIP_XS
    svg.path([(xs[0], 108), (xs[1], 108)], GREEN, 7)
    svg.text(xs[1] + 16, 114, "both wires name each other: a credit is possible", 17, GREEN)
    svg.path([(xs[0] + 20, 142), (xs[1] - 20, 142)], AMBER, 2.5, dash=True, arrow=True)
    svg.text(xs[1] + 16, 148, "one-sided: this wire names a partner that has moved on", 17, AMBER)
    for i, ((label, kind, pair, _), last) in enumerate(zip(events, rows)):
        y = 215 + strip * i
        svg.text(24, y + 7, label, 18, INK, bold=kind == "gate")
        draw_last_block_strip(svg, y, last, xs, swap=pair if kind == "swap" else None)
    svg.text(24, height - 22, "A SWAP clears both of its wires. A gate on (a, b) makes a and b name each other; any older record on a or b is overwritten.", 17, MUTED)
    svg.save(FIGURES / "fusion-history.svg")


def draw_fusion_decision3():
    final = list(replay_last_block(FUSION_EVENTS))[-1]
    w = 0.5 / 6
    candidates = [
        ("SWAP(p2, p3)", (2, 3), +1, -w, False),
        ("SWAP(p3, p4)", (3, 4), -1, +w, True),
        ("SWAP(p4, p5)", (4, 5), -1, 0.0, False),
    ]
    svg = SVG(700, "Decision 3: the fusion credit overturns the classic ranking")
    svg.text(24, 35, "Decision 3: the credit overturns the classic ranking", 27, bold=True)
    svg.text(24, 67, "Top: wire memory at S2 (the last strip of the previous figure). Bottom: each candidate's score, one term at a time.", 18)
    xs = STRIP_XS
    svg.text(24, 158, "wire memory at S2", 18, MUTED)
    draw_last_block_strip(svg, 152, final, xs, occupants=["q1", "q2", "q3", "q4", "q6", "q5"])
    # Waterfall of score terms.
    left, right = 300, 960
    lo, hi = -2.0, 1.5
    scale = (right - left) / (hi - lo)
    zero_x = left - lo * scale
    axis_y = 262
    svg.path([(left, axis_y), (right, axis_y)], "#687e91", 2)
    for v in (-2, -1, 0, 1):
        x = zero_x + v * scale
        svg.path([(x, axis_y - 6), (x, axis_y + 6)], "#687e91", 2)
        svg.text(x, axis_y - 12, f"{v:+d}" if v else "0", 16, MUTED, "middle")
    svg.text(24, axis_y + 6, "score (lower wins)", 18, MUTED)
    svg.path([(zero_x, axis_y), (zero_x, 630)], LINE, 2, dash=True)
    terms = (("front distance", BLUE), ("lookahead", AMBER), ("credit", GREEN))
    for i, (name, pair, d_f, look, handshake) in enumerate(candidates):
        y = 320 + i * 110
        mutual = final[pair[0]] is not None and final[pair[0]][0] == pair[1] \
            and final[pair[1]] is not None and final[pair[1]][0] == pair[0]
        assert mutual == handshake, (name, final)
        credit = -2 / 3 if mutual else 0.0
        total = d_f + look + credit
        svg.text(24, y + 7, name, 20, INK, bold=True)
        svg.text(24, y + 32, "handshake: yes, from g6" if mutual else "handshake: no", 16, GREEN if mutual else MUTED)
        pos = 0.0
        for k, (value, (label, color)) in enumerate(zip((d_f, look, credit), terms)):
            yy = y + 22 * k
            if value == 0:
                svg.text(zero_x + (8 if pos >= 0 else -8), yy + 5, f"{label} 0", 14, MUTED,
                         "start" if pos >= 0 else "end")
                continue
            x1, x2 = zero_x + pos * scale, zero_x + (pos + value) * scale
            svg.path([(x1, yy), (x2, yy)], color, 12)
            text = f"{label} {value:+.4f}".replace("+1.0000", "+1").replace("-1.0000", "-1")
            if k == 0:
                svg.text((x1 + x2) / 2, yy - 11, text, 15, color, "middle", True)
            else:
                svg.text(x2 + (8 if value > 0 else -8), yy + 5, text, 15, color,
                         "start" if value > 0 else "end", True)
            pos += value
        x_total = zero_x + total * scale
        svg.path([(x_total, y - 12), (x_total, y + 56)], INK, 3)
        svg.text(x_total + (10 if total > 0 else -10), y + 76, f"total {total:+.4f}", 17, INK,
                 "start" if total > 0 else "end", bold=True)
    svg.text(24, 660, "Without the credit SWAP(p4, p5) wins by 1/12. The credit of -2/3 is eight times larger and moves SWAP(p3, p4) to the front.", 17, MUTED)
    svg.text(24, 684, "A credit never beats a full distance step: a SWAP that pushes a front pair apart still scores +1/3 at best.", 17, MUTED)
    svg.save(FIGURES / "fusion-decision3.svg")


def draw_fusion_figures():
    draw_fusion_cancel()
    draw_fusion_nodes()
    draw_fusion_history()
    draw_fusion_decision3()


def build_pdf():
    body = subprocess.check_output([
        "pandoc", str(DESIGN / "SABRE_GUIDE.md"), "-f", "markdown+gfm_auto_identifiers", "-t", "typst",
    ], text=True)
    body = re.sub(r"columns: \([\d.]+%(?:,\s*[\d.]+%)*,?\),",
                  lambda m: "columns: (auto,) * %d," % len(re.findall(r"[\d.]+%", m[0])), body)
    body = re.sub(r"align: \((?:auto,)+\),", "align: left + top,", body)
    # All diagrams are vector images; constrain their width to the text area.
    body = re.sub(r'#image\("(figures/[^\"]+)"\)', r'#image("\1", width: 100%)', body)
    template = (DESIGN / "sabre_guide.typ").read_text()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".typ", dir=DESIGN, delete=False) as handle:
        handle.write(template + "\n" + body)
        source = Path(handle.name)
    try:
        subprocess.run(["typst", "compile", "--root", str(DESIGN.parent),
                        str(source), str(DESIGN / "SABRE_GUIDE.pdf")], check=True)
    finally:
        source.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--figures-only", action="store_true")
    args = parser.parse_args()
    FIGURES.mkdir(parents=True, exist_ok=True)
    snapshots = states()
    for state in snapshots:
        draw_state(state)
    graph = SVG(470, "Initial dependency graph with current physical locations")
    draw_graph(graph, next(state for state in snapshots if state.name == "s0"), 12)
    graph.save(FIGURES / "initial-dag.svg")
    draw_fusion_figures()
    print(f"Validated and generated {len(snapshots)} state snapshots, the initial DAG and 4 fusion figures.")
    if not args.figures_only:
        build_pdf()
        print("Built design/SABRE_GUIDE.pdf")


if __name__ == "__main__":
    main()
