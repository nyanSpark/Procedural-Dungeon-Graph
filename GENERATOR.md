## Design Overview
 
The codebase separates generation from presentation so the algorithm can be exercised — and tested — without spinning up a window. The generator runs as a deterministic, seeded pipeline; the Qt layer is a thin shell on top that visualizes the result and feeds configuration in.
 
### Generation pipeline
 
`DungeonGenerator.generate()` walks the stages below in order, retrying the whole pipeline up to `MAX_GENERATION_ATTEMPTS` (200) times when validation fails. The seeded `random.Random` instance threads through every stage, so the same seed plus the same config always produces the same output.
 
```
   ┌──────────────────────────────────────────────────────────┐
   │  GeneratorConfig (normalized, seeded RNG)                │
   └─────────────────────────┬────────────────────────────────┘
                             ▼
   1. Compute type targets ───── largest-remainder split of
      (combat / treasure /        (room_count − 2) across the
      puzzle counts)              three configured ratios
                             ▼
   2. Choose critical path ───── bounded below by
      length                      (min_boss_distance + 1) and
                                  above by the style preset's
                                  critical_fraction
                             ▼
   3. Build self-avoiding ────── DFS on the grid scored by
      critical path               onward room availability;
                                  retries from several start
                                  cells before giving up
                             ▼
   4. Grow branches ──────────── weighted root selection by
      until room_count met        style; each branch extends
                                  greedily by openness score
                             ▼
   5. Opportunistic cross- ───── new rooms may link to other
      links                       adjacent rooms — always when
                                  dead-ends are disallowed,
                                  otherwise at probability 0.28
                             ▼
   6. Dead-end cleanup ───────── if disallowed, iteratively
      (optional)                  extend single-degree side
                                  rooms to a free neighbor
                             ▼
   7. Assign room types ──────── start/boss fixed at path
                                  endpoints; treasure drawn
                                  from side rooms; puzzle and
                                  combat fill the rest
                             ▼
   8. Validate + metrics ─────── seven-check invariant suite +
                                  structural metrics (occupancy,
                                  degree distribution, linearity)
                             ▼
                          Dungeon
```
 
If a layout fails validation, the loop retries with a fresh draw from the RNG. The highest-scoring partial layout is held in reserve and returned as a fallback if no fully valid layout emerges within the attempt budget — better to render *something* than throw on a tight configuration.
 
### Key algorithmic choices
 
- **Self-avoiding DFS for the critical path.** Building start-to-boss as a single self-avoiding walk on the grid (rather than picking endpoints first and pathing between them) guarantees the path never doubles back on itself or creates accidental shortcuts. At each step, candidate neighbors are scored by how many *onward* moves they preserve, plus a small "center pull" term and a random tiebreak. This biases the search toward paths that can actually reach the target length without painting themselves into a corner — without that scoring, the DFS spends most of its budget backtracking.
- **Style presets shape topology, not just appearance.** The `Linear` / `Balanced` / `Branching` entries in `STYLE_PRESETS` set both the *fraction* of total rooms that sit on the critical path (0.82 / 0.62 / 0.45) and the typical branch-length range. `BRANCH_ROOT_STYLE_WEIGHTS` further tilts where new branches sprout: branching layouts weight side rooms higher as roots (producing wider trees), while linear layouts keep branches close to the spine.
- **Weighted root selection with degree falloff.** When picking where to grow the next branch, candidate rooms are weighted by `style_bucket × max(0.4, max_branch_factor − degree + 0.2)`. This naturally throttles over-connected rooms without hard-banning them — a room near its branch-factor cap is unlikely but not impossible to be chosen, which keeps the topology varied across seeds.
- **Treasure placement honors the "optional content" contract.** Treasure rooms are always drawn from side rooms (off the critical path), so a player can finish the dungeon without entering one. When `optional_treasure` is enabled, the generator fails fast if there aren't enough side rooms to satisfy the treasure target, rather than silently producing the wrong count.
- **Validation is a seven-point invariant suite.** `_validate` checks boss reachability from start, treasure-optionality, branch-factor limits, critical-path contiguity, exact type counts, minimum boss distance, and the dead-end side-room policy. Each check produces a human-readable detail string that's surfaced directly in the Validation panel — when a layout fails, you can see exactly which invariant broke and by how much.
### UI architecture
 
The Qt layer is intentionally thin and split into three widgets coordinated by the main window. Each piece is independently testable, and none of them reach into the generator's internals:
 
- **`DungeonCanvas`** — A `QWidget` with a custom `paintEvent` that draws the grid, edges, and rooms directly via `QPainter`. Builds a per-paint hitbox map of room rectangles and emits `room_clicked(int)` from `mousePressEvent`.
- **`ControlsPanel`** — Sidebar holding form inputs, action buttons, and the metrics/validation readout. Exposes its actions as Qt signals (`generate_requested`, `export_json_requested`, etc.) rather than wiring directly to the main window, which keeps it reusable.
- **`PreviewCanvas`** — Owns the `DungeonCanvas`, a legend, and a room inspector; relays clicks from the canvas into inspector updates.
- **`DungeonToolApp`** — `QMainWindow` coordinator. Owns the panels, wires their signals to handlers, and routes file I/O through `QFileDialog`.
Because `DungeonGenerator` has zero Qt dependencies, it can be driven from a CLI, a web service, or a test suite without importing any UI code.
