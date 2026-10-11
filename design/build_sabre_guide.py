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
    print(f"Validated and generated {len(snapshots)} state snapshots and the initial DAG.")
    if not args.figures_only:
        build_pdf()
        print("Built design/SABRE_GUIDE.pdf")


if __name__ == "__main__":
    main()
