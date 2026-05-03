# Procedural Dungeon / Encounter Layout Tool

<img width="1273" height="912" alt="procedural_tool" src="https://github.com/user-attachments/assets/9562a0b6-db61-44b4-a020-194e9af564e6" />

A desktop application for procedurally generating, validating, and visualizing 2D dungeon layouts on a grid. Built with Python and Qt (PySide6), it is intended as a level-design aid for tabletop campaigns, roguelike prototypes, and game-jam projects where you need a defensible dungeon shape fast — start room, boss room, a critical path between them, and side content scattered around the periphery.

## Table of Contents

- [Introduction](#introduction)
- [Features](#features)
- [Design Overview](#design-overview)
- [Requirements](#requirements)
- [Installation & Running](#installation--running)
  - [macOS / Linux](#macos--linux)
- [Usage](#usage)

## Introduction

Given a grid size, a target room count, and a few stylistic knobs, the tool builds a connected dungeon graph, places typed rooms (start, combat, treasure, puzzle, boss) according to your ratios, and renders the result as an interactive map. Every generation is seeded, so layouts are reproducible — paste the same seed back in and you get the same dungeon. Each layout is run through a validation suite (boss reachability, branch-factor limits, type-count targets, etc.) before it is shown, and metrics like critical-path length, average room degree, and optional-content ratio are reported alongside the map.

## Features

- Three style presets (Linear, Balanced, Branching) that bias critical-path length and branch shape.
- Adjustable combat / treasure / puzzle ratios via sliders.
- Constraints for minimum boss distance, maximum branching factor, optional-treasure enforcement, and dead-end side rooms.
- Reproducible seeded generation — numeric or text seeds both supported.
- Click any room to inspect its ID, type, grid position, degree, neighbors, and critical-path index.
- Validation panel showing pass/fail for each invariant with human-readable detail.
- Quality metrics: occupancy, linearity, average and max degree, dead-end count, optional-content ratio.
- Save and load generation configs as JSON; export the full generated dungeon as JSON.

## Design Overview

The codebase is split cleanly into four parts so the generator can be reused outside the GUI if needed:

- **Generation logic** (`DungeonGenerator`) — Pure algorithm, no Qt dependency. Builds a self-avoiding critical path via DFS, grows weighted branches off it, assigns room types, and computes metrics. Works off a normalized `GeneratorConfig` dataclass and emits a `Dungeon` dataclass.
- **Custom-painted canvas** (`DungeonCanvas`) — A `QWidget` subclass that overrides `paintEvent` to draw the grid, edges, and rooms directly with `QPainter`. Hit testing is done by storing per-room rectangles each paint and matching them against `mousePressEvent` coordinates. Emits a `room_clicked(int)` signal.
- **Sidebar** (`ControlsPanel`) — All form inputs (spin boxes, sliders, combo boxes, checkboxes), action buttons, and the metrics/validation readout. Exposes its actions as Qt signals (`generate_requested`, `export_json_requested`, etc.) rather than wiring directly to the main window, which keeps it independently testable.
- **Coordinator** (`DungeonToolApp`, a `QMainWindow`) — Owns the controls panel and the preview pane, wires their signals together, and handles file I/O (config save/load, dungeon JSON export) through `QFileDialog`.

The split means `DungeonGenerator` could be driven from a CLI, a web service, or a test suite without touching any of the Qt code.

## Requirements

- **Python 3.9 or newer** (tested on 3.9.6).
- **PySide6** — the official Qt for Python bindings. Pulled from PyPI; no separate Qt install needed.

All other dependencies (`copy`, `hashlib`, `json`, `math`, `random`, `dataclasses`, `collections`, `typing`) ships with standard library.

## Installation & Running

### macOS / Linux

From a terminal in the directory containing `ProceduralDungeonTool.py`:

```bash
# 1. Confirm interpreter version
python3 --version

# 2. Create and activate an isolated virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Upgrade pip inside the venv
python -m pip install --upgrade pip

# 4. Install PySide6
pip install PySide6

# If you encounter errors installing PySide6, try:
pip install --force-reinstall --no-compile PySide6

# 5. Run the tool
python ProceduralDungeonTool.py
```
Don't forget to `deactivate` your venv when you're done. 

## Usage

The window opens with a default Balanced layout already generated. From there:

1. Adjust grid size, room count, style, and ratios in the left sidebar.
2. Click **Generate** to build a new layout with the current seed, or **Random Seed** to roll a fresh one.
3. Click any room in the preview to populate the **Room Inspector** with its details.
4. Use **Save Config** / **Load Config** to persist generator settings as JSON, and **Export JSON** to save the full generated dungeon (rooms, neighbors, critical path, metrics, validation results) for use elsewhere.

The status bar at the bottom of the window reports the current seed and whether validation passed.
