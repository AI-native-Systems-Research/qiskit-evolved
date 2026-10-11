// PDF styling for build_sabre_guide.py; Markdown remains the source of prose.
#let horizontalrule = line(length: 100%, stroke: 0.5pt + rgb("d5dee7"))
#set document(title: "Understanding Qiskit's SABRE implementation", author: "qiskit-evolved")
#set page(paper: "us-letter", margin: (x: 44pt, y: 45pt),
  footer: context align(center, text(size: 9pt, fill: rgb("526477"), counter(page).display("1"))))
#set text(font: "Libertinus Serif", size: 10.5pt, lang: "en")
#set par(leading: 0.55em, spacing: 0.7em)
#set heading(numbering: none)
#show heading: set text(fill: rgb("182c42"))
#show heading.where(level: 1): set text(size: 20pt)
#show heading.where(level: 2): set text(size: 15pt)
#show heading.where(level: 3): set text(size: 12pt)
#show link: set text(fill: rgb("165f98"))
#show raw: set text(font: "DejaVu Sans Mono", size: 8pt)
#show raw.where(block: true): it => block(width: 100%, breakable: false, fill: rgb("f5f8fb"), inset: 7pt, radius: 3pt, {
  set par(leading: 0.12em)
  set text(top-edge: "ascender", bottom-edge: "descender")
  // Circuit drawings and score formulas must retain their exact spacing.
  layout(size => {
    let natural = measure(it).width
    if natural > size.width { scale(x: size.width / natural * 100%, y: size.width / natural * 100%, reflow: true, it) }
    else { it }
  })
})
#show quote.where(block: true): it => block(width: 100%, fill: rgb("e9f3fc"),
  stroke: (left: 2pt + rgb("165f98")), inset: 9pt, it.body)
#set table(inset: (x: 4pt, y: 4pt), stroke: 0.4pt + rgb("d5dee7"), align: left + top)
#show figure.where(kind: table): set text(size: 8.5pt)
#show figure.where(kind: table): set par(leading: 0.45em, justify: false)
#show table.cell.where(y: 0): set text(weight: "bold")
#show table.cell.where(y: 0): set table.cell(fill: rgb("edf3f8"))
#show figure.caption: set text(size: 9pt, fill: rgb("526477"))
#set figure(gap: 6pt)
#set list(indent: 0.7em, spacing: 0.4em)
#set enum(indent: 0.7em, spacing: 0.4em)
