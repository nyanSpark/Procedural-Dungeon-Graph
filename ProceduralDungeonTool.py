"""Procedural Dungeon / Encounter Layout Tool (PySide6 port).

Module layout:
    - Constants and dataclasses       : config, room, dungeon, validation result
    - DungeonGenerator                : core generator logic
    - DungeonCanvas (QWidget)         : custom widget that draws the dungeon and emits clicks on rooms
    - ControlsPanel (QWidget)         : left sidebar (form inputs, actions, metrics)
    - PreviewCanvas (QWidget)         : right side (canvas + legend + room inspector)
    - DungeonToolApp (QMainWindow)    : coordinator that wires the two panels together
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import sys
from collections import Counter, deque
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional, Set, Tuple

from PySide6.QtCore import Qt, Signal, QPointF, QRectF
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QMouseEvent, QPaintEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# Visual constants
# ---------------------------------------------------------------------------

ROOM_COLORS = {
    "start": "#4CAF50",
    "combat": "#D32F2F",
    "treasure": "#F9A825",
    "puzzle": "#1976D2",
    "boss": "#6A1B9A",
    "empty": "#ECEFF1",
}

ROOM_LABELS = {
    "start": "S",
    "combat": "C",
    "treasure": "T",
    "puzzle": "P",
    "boss": "B",
    "empty": "?",
}

CRITICAL_EDGE_COLOR = "#FFB300"
NORMAL_EDGE_COLOR = "#90A4AE"
GRID_COLOR = "#CFD8DC"
SELECTED_OUTLINE = "#111827"


# ---------------------------------------------------------------------------
# Generator tuning
# ---------------------------------------------------------------------------

STYLE_PRESETS = {
    "Linear": {"critical_fraction": 0.82, "branch_length": (1, 2)},
    "Balanced": {"critical_fraction": 0.62, "branch_length": (1, 3)},
    "Branching": {"critical_fraction": 0.45, "branch_length": (2, 4)},
}

MAX_GENERATION_ATTEMPTS = 200
PATH_DFS_RESTARTS_PER_START = 80
DEAD_END_TRIM_ITERATIONS = 10
CROSS_LINK_PROBABILITY = 0.28
BRANCH_ATTEMPT_BUDGET_MIN = 100
BRANCH_ATTEMPT_BUDGET_PER_ROOM = 20
PLACEMENT_ADJACENCY_BONUS = 0.35
LINEAR_CRITICAL_SCORE_BIAS = 0.15

BRANCH_ROOT_STYLE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "Linear":    {"critical": 0.8,  "side": 1.0},
    "Balanced":  {"critical": 1.2,  "side": 1.0},
    "Branching": {"critical": 1.6,  "side": 1.35},
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

class GenerationError(RuntimeError):
    pass


@dataclass
class GeneratorConfig:
    width: int = 12
    height: int = 8
    room_count: int = 18
    seed: str = "42"
    style: str = "Balanced"
    combat_ratio: int = 60
    treasure_ratio: int = 20
    puzzle_ratio: int = 20
    min_boss_distance: int = 6
    max_branch_factor: int = 3
    allow_dead_end_side_rooms: bool = True
    optional_treasure: bool = True

    def normalized(self) -> "GeneratorConfig":
        cfg = copy.deepcopy(self)
        cfg.width = max(3, int(cfg.width))
        cfg.height = max(3, int(cfg.height))
        cfg.room_count = max(2, min(int(cfg.room_count), cfg.width * cfg.height))
        cfg.min_boss_distance = max(1, int(cfg.min_boss_distance))
        cfg.max_branch_factor = max(2, min(4, int(cfg.max_branch_factor)))
        cfg.combat_ratio = max(0, int(cfg.combat_ratio))
        cfg.treasure_ratio = max(0, int(cfg.treasure_ratio))
        cfg.puzzle_ratio = max(0, int(cfg.puzzle_ratio))
        if cfg.style not in STYLE_PRESETS:
            cfg.style = "Balanced"
        return cfg


@dataclass
class Room:
    id: int
    x: int
    y: int
    room_type: str = "combat"
    neighbors: List[int] = field(default_factory=list)
    critical_index: Optional[int] = None

    @property
    def pos(self) -> Tuple[int, int]:
        return self.x, self.y


@dataclass
class ValidationResult:
    checks: Dict[str, bool]
    details: Dict[str, str]

    @property
    def all_passed(self) -> bool:
        return all(self.checks.values())


@dataclass
class Dungeon:
    width: int
    height: int
    seed: str
    config: Dict[str, Any]
    rooms: Dict[int, Room]
    start_id: int
    boss_id: int
    critical_path: List[int]
    type_targets: Dict[str, int]
    metrics: Dict[str, float]
    validation: ValidationResult

    def to_export_dict(self) -> Dict[str, Any]:
        return {
            "seed": self.seed,
            "config": self.config,
            "type_targets": self.type_targets,
            "metrics": self.metrics,
            "validation": {
                "checks": self.validation.checks,
                "details": self.validation.details,
                "all_passed": self.validation.all_passed,
            },
            "critical_path": self.critical_path,
            "rooms": [
                {
                    "id": room.id,
                    "x": room.x,
                    "y": room.y,
                    "type": room.room_type,
                    "neighbors": room.neighbors,
                    "critical_index": room.critical_index,
                }
                for room in sorted(self.rooms.values(), key=lambda r: r.id)
            ],
        }


# ---------------------------------------------------------------------------
# Generator (unchanged from the cleaned Tkinter version)
# ---------------------------------------------------------------------------

class DungeonGenerator:
    def __init__(self, config: GeneratorConfig):
        self.config = config.normalized()
        self.rng = random.Random(self._seed_to_int(self.config.seed))

    @staticmethod
    def _seed_to_int(seed: str) -> int:
        seed_text = str(seed).strip()
        if seed_text == "":
            return random.randrange(0, 2**32 - 1)
        if seed_text.lstrip("-").isdigit():
            return int(seed_text)
        digest = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
        return int(digest[:16], 16)

    def generate(self) -> Dungeon:
        cfg = self.config
        usable_slots = cfg.room_count - 2
        if usable_slots < 0:
            raise GenerationError("Room count must be at least 2 for start and boss.")
        if cfg.room_count > cfg.width * cfg.height:
            raise GenerationError("Room count exceeds available grid space.")

        type_targets = self._calculate_type_targets(usable_slots)
        critical_path_room_count = self._choose_critical_path_room_count(cfg, type_targets)

        best_dungeon: Optional[Dungeon] = None
        best_score = -1
        last_error: Optional[GenerationError] = None

        for _ in range(MAX_GENERATION_ATTEMPTS):
            try:
                rooms, critical_path, start_id, boss_id = self._build_topology(critical_path_room_count)
                self._assign_room_types(rooms, critical_path, type_targets)
                metrics = self._compute_metrics(rooms, critical_path)
                validation = self._validate(rooms, critical_path, start_id, boss_id, type_targets)
            except GenerationError as exc:
                last_error = exc
                continue

            dungeon = Dungeon(
                width=cfg.width,
                height=cfg.height,
                seed=str(cfg.seed),
                config=asdict(cfg),
                rooms=rooms,
                start_id=start_id,
                boss_id=boss_id,
                critical_path=critical_path,
                type_targets=type_targets,
                metrics=metrics,
                validation=validation,
            )
            score = sum(1 for ok in validation.checks.values() if ok)
            if validation.all_passed:
                return dungeon
            if score > best_score:
                best_score = score
                best_dungeon = dungeon

        if best_dungeon is not None:
            return best_dungeon
        raise GenerationError("Could not build a valid layout.") from last_error

    def _calculate_type_targets(self, available_slots: int) -> Dict[str, int]:
        cfg = self.config
        weights = {
            "combat": max(0, cfg.combat_ratio),
            "treasure": max(0, cfg.treasure_ratio),
            "puzzle": max(0, cfg.puzzle_ratio),
        }
        total_weight = sum(weights.values())
        if available_slots <= 0:
            return {"combat": 0, "treasure": 0, "puzzle": 0}
        if total_weight <= 0:
            return {"combat": available_slots, "treasure": 0, "puzzle": 0}

        exact = {k: (v / total_weight) * available_slots for k, v in weights.items()}
        counts = {k: int(math.floor(v)) for k, v in exact.items()}
        remainder = available_slots - sum(counts.values())
        for name, _ in sorted(exact.items(), key=lambda item: item[1] - math.floor(item[1]), reverse=True):
            if remainder <= 0:
                break
            counts[name] += 1
            remainder -= 1
        return counts

    def _choose_critical_path_room_count(self, cfg: GeneratorConfig, type_targets: Dict[str, int]) -> int:
        style_preset = STYLE_PRESETS[cfg.style]
        min_required = cfg.min_boss_distance + 1
        preferred = max(min_required, int(round(cfg.room_count * style_preset["critical_fraction"])))
        side_room_floor = type_targets["treasure"] if cfg.optional_treasure else 0
        max_critical = cfg.room_count - side_room_floor
        if max_critical < min_required:
            raise GenerationError(
                "Current treasure ratio and boss distance make the layout impossible. "
                "Lower treasure ratio, reduce min boss distance, or increase room count."
            )
        critical = max(min_required, min(preferred, max_critical))
        return max(2, min(critical, cfg.room_count))

    def _build_topology(
        self, critical_room_count: int
    ) -> Tuple[Dict[int, Room], List[int], int, int]:
        cfg = self.config
        path_cells = self._build_self_avoiding_path(critical_room_count)
        rooms: Dict[int, Room] = {}
        coord_to_id: Dict[Tuple[int, int], int] = {}

        def add_room(coord: Tuple[int, int], critical_index: Optional[int]) -> int:
            room_id = len(rooms)
            room = Room(id=room_id, x=coord[0], y=coord[1], room_type="combat", critical_index=critical_index)
            rooms[room_id] = room
            coord_to_id[coord] = room_id
            return room_id

        critical_path: List[int] = []
        for idx, coord in enumerate(path_cells):
            room_id = add_room(coord, idx)
            critical_path.append(room_id)
            if idx > 0:
                prev_id = critical_path[idx - 1]
                rooms[prev_id].neighbors.append(room_id)
                rooms[room_id].neighbors.append(prev_id)

        remaining = cfg.room_count - len(critical_path)
        min_len, max_len = STYLE_PRESETS[cfg.style]["branch_length"]
        branch_attempt_budget = max(
            BRANCH_ATTEMPT_BUDGET_MIN,
            cfg.room_count * BRANCH_ATTEMPT_BUDGET_PER_ROOM,
        )

        while remaining > 0 and branch_attempt_budget > 0:
            branch_attempt_budget -= 1
            candidates = [
                room_id
                for room_id, room in rooms.items()
                if len(room.neighbors) < cfg.max_branch_factor and room_id != critical_path[-1]
            ]
            if not candidates:
                break
            root_id = self._choose_branch_root(candidates, rooms, critical_path)
            desired_branch_len = min(remaining, self.rng.randint(min_len, max_len))
            built_len = self._grow_branch(root_id, desired_branch_len, rooms, coord_to_id, critical_path)
            if built_len == 0:
                continue
            remaining -= built_len

        if remaining != 0:
            raise GenerationError("Unable to place the requested number of rooms within the grid.")

        if not cfg.allow_dead_end_side_rooms:
            self._trim_or_extend_dead_end_side_rooms(rooms, coord_to_id, critical_path)

        start_id = critical_path[0]
        boss_id = critical_path[-1]
        return rooms, critical_path, start_id, boss_id

    def _build_self_avoiding_path(self, length: int) -> List[Tuple[int, int]]:
        cfg = self.config
        candidate_starts = [
            (cfg.width // 2, cfg.height // 2),
            (max(0, cfg.width // 2 - 1), cfg.height // 2),
            (cfg.width // 2, max(0, cfg.height // 2 - 1)),
            (self.rng.randrange(cfg.width), self.rng.randrange(cfg.height)),
            (self.rng.randrange(cfg.width), self.rng.randrange(cfg.height)),
        ]
        for start in candidate_starts:
            for _ in range(PATH_DFS_RESTARTS_PER_START):
                path = [start]
                occupied = {start}
                if self._path_dfs(path, occupied, length, start):
                    return path
        raise GenerationError("Could not place a critical path of the requested length.")

    def _path_dfs(
        self,
        path: List[Tuple[int, int]],
        occupied: Set[Tuple[int, int]],
        target_len: int,
        origin: Tuple[int, int],
    ) -> bool:
        if len(path) >= target_len:
            return True

        current = path[-1]
        scored: List[Tuple[float, Tuple[int, int]]] = []
        for nx, ny in self._neighbors_xy(current[0], current[1]):
            coord = (nx, ny)
            if coord in occupied:
                continue
            onward = sum(
                1
                for ox, oy in self._neighbors_xy(nx, ny)
                if (ox, oy) not in occupied and (ox, oy) != current
            )
            center_pull = abs(nx - origin[0]) + abs(ny - origin[1])
            score = onward * 2.0 + center_pull + self.rng.random()
            scored.append((score, coord))

        scored.sort(key=lambda item: item[0], reverse=True)

        for _, coord in scored:
            occupied.add(coord)
            path.append(coord)
            if self._path_dfs(path, occupied, target_len, origin):
                return True
            path.pop()
            occupied.remove(coord)
        return False

    def _choose_branch_root(self, candidates: List[int], rooms: Dict[int, Room], critical_path: List[int]) -> int:
        critical_set = set(critical_path)
        style_weights = BRANCH_ROOT_STYLE_WEIGHTS[self.config.style]
        weighted: List[Tuple[float, int]] = []
        for room_id in candidates:
            room = rooms[room_id]
            degree = len(room.neighbors)
            bucket = "critical" if room_id in critical_set else "side"
            weight = style_weights[bucket] * max(0.4, (self.config.max_branch_factor - degree + 0.2))
            weighted.append((weight, room_id))

        total = sum(w for w, _ in weighted)
        pick = self.rng.random() * total
        running = 0.0
        for weight, room_id in weighted:
            running += weight
            if running >= pick:
                return room_id
        return weighted[-1][1]

    def _grow_branch(
        self,
        root_id: int,
        desired_len: int,
        rooms: Dict[int, Room],
        coord_to_id: Dict[Tuple[int, int], int],
        critical_path: List[int],
    ) -> int:
        cfg = self.config
        built = 0
        current_id = root_id
        critical_set = set(critical_path)
        for _ in range(desired_len):
            if len(rooms[current_id].neighbors) >= cfg.max_branch_factor:
                break
            new_id = self._place_next_branch_room(current_id, rooms, coord_to_id, critical_set)
            if new_id is None:
                break
            self._add_opportunistic_cross_links(new_id, current_id, rooms, coord_to_id)
            built += 1
            current_id = new_id
        return built

    def _place_next_branch_room(
        self,
        current_id: int,
        rooms: Dict[int, Room],
        coord_to_id: Dict[Tuple[int, int], int],
        critical_set: Set[int],
    ) -> Optional[int]:
        cfg = self.config
        current_room = rooms[current_id]
        options: List[Tuple[float, Tuple[int, int]]] = []
        cx, cy = current_room.pos
        for nx, ny in self._neighbors_xy(cx, cy):
            if (nx, ny) in coord_to_id:
                continue
            openness = sum(1 for ox, oy in self._neighbors_xy(nx, ny) if (ox, oy) not in coord_to_id)
            adjacency_bonus = sum(1 for ox, oy in self._neighbors_xy(nx, ny) if (ox, oy) in coord_to_id)
            score = openness + self.rng.random() + adjacency_bonus * PLACEMENT_ADJACENCY_BONUS
            if current_id in critical_set and cfg.style == "Linear":
                score += LINEAR_CRITICAL_SCORE_BIAS
            options.append((score, (nx, ny)))
        if not options:
            return None
        options.sort(key=lambda item: item[0], reverse=True)
        _, coord = options[0]
        room_id = len(rooms)
        new_room = Room(id=room_id, x=coord[0], y=coord[1], room_type="combat")
        rooms[room_id] = new_room
        coord_to_id[coord] = room_id
        current_room.neighbors.append(room_id)
        new_room.neighbors.append(current_id)
        return room_id

    def _add_opportunistic_cross_links(
        self,
        new_id: int,
        parent_id: int,
        rooms: Dict[int, Room],
        coord_to_id: Dict[Tuple[int, int], int],
    ) -> None:
        cfg = self.config
        new_room = rooms[new_id]
        adjacent_room_ids: List[int] = []
        for nx, ny in self._neighbors_xy(new_room.x, new_room.y):
            neighbor_id = coord_to_id.get((nx, ny))
            if neighbor_id is None or neighbor_id == parent_id:
                continue
            if neighbor_id in new_room.neighbors:
                continue
            if len(new_room.neighbors) >= cfg.max_branch_factor:
                break
            if len(rooms[neighbor_id].neighbors) >= cfg.max_branch_factor:
                continue
            adjacent_room_ids.append(neighbor_id)
        self.rng.shuffle(adjacent_room_ids)
        for neighbor_id in adjacent_room_ids:
            if len(new_room.neighbors) >= cfg.max_branch_factor:
                break
            if len(rooms[neighbor_id].neighbors) >= cfg.max_branch_factor:
                continue
            if not cfg.allow_dead_end_side_rooms or self.rng.random() < CROSS_LINK_PROBABILITY:
                new_room.neighbors.append(neighbor_id)
                rooms[neighbor_id].neighbors.append(new_id)

    def _trim_or_extend_dead_end_side_rooms(
        self,
        rooms: Dict[int, Room],
        coord_to_id: Dict[Tuple[int, int], int],
        critical_path: List[int],
    ) -> None:
        cfg = self.config
        critical_set = set(critical_path)
        for _ in range(DEAD_END_TRIM_ITERATIONS):
            dead_end_side_rooms = [
                room_id
                for room_id, room in rooms.items()
                if room_id not in critical_set and len(room.neighbors) == 1
            ]
            if not dead_end_side_rooms:
                return
            changed = False
            for room_id in dead_end_side_rooms:
                room = rooms[room_id]
                if len(room.neighbors) >= cfg.max_branch_factor:
                    continue
                options: List[int] = []
                for nx, ny in self._neighbors_xy(room.x, room.y):
                    neighbor_id = coord_to_id.get((nx, ny))
                    if neighbor_id is None or neighbor_id == room.neighbors[0]:
                        continue
                    if len(rooms[neighbor_id].neighbors) >= cfg.max_branch_factor:
                        continue
                    if neighbor_id in critical_set:
                        continue
                    options.append(neighbor_id)
                self.rng.shuffle(options)
                if not options:
                    continue
                target = options[0]
                rooms[room_id].neighbors.append(target)
                rooms[target].neighbors.append(room_id)
                changed = True
            if not changed:
                return

    def _assign_room_types(self, rooms: Dict[int, Room], critical_path: List[int], targets: Dict[str, int]) -> None:
        critical_set = set(critical_path)
        start_id, boss_id = critical_path[0], critical_path[-1]
        rooms[start_id].room_type = "start"
        rooms[boss_id].room_type = "boss"

        remaining_targets = dict(targets)
        available_ids = [room_id for room_id in rooms if room_id not in {start_id, boss_id}]
        side_ids = [room_id for room_id in available_ids if room_id not in critical_set]

        if self.config.optional_treasure and remaining_targets["treasure"] > len(side_ids):
            raise GenerationError(
                "Optional treasure constraint could not be satisfied with the current layout style."
            )

        self.rng.shuffle(side_ids)

        treasure_ids = side_ids[: remaining_targets["treasure"]]
        for room_id in treasure_ids:
            rooms[room_id].room_type = "treasure"
        assigned = set(treasure_ids)

        remaining_pool = [room_id for room_id in available_ids if room_id not in assigned]
        self.rng.shuffle(remaining_pool)

        puzzle_take = min(remaining_targets["puzzle"], len(remaining_pool))
        puzzle_ids = remaining_pool[:puzzle_take]
        for room_id in puzzle_ids:
            rooms[room_id].room_type = "puzzle"
        assigned.update(puzzle_ids)

        for room_id in available_ids:
            if room_id in assigned:
                continue
            rooms[room_id].room_type = "combat"

    def _validate(
        self,
        rooms: Dict[int, Room],
        critical_path: List[int],
        start_id: int,
        boss_id: int,
        targets: Dict[str, int],
    ) -> ValidationResult:
        cfg = self.config
        checks: Dict[str, bool] = {}
        details: Dict[str, str] = {}

        reachable, distance = self._path_distance(rooms, start_id, boss_id)
        checks["start_reaches_boss"] = reachable
        details["start_reaches_boss"] = (
            f"Boss reachable from start. Distance = {distance}." if reachable else "Boss is not reachable from start."
        )

        critical_set = set(critical_path)
        treasure_on_critical = [rid for rid in critical_path if rooms[rid].room_type == "treasure"]
        treasure_optional_ok = (not cfg.optional_treasure) or (len(treasure_on_critical) == 0)
        checks["treasure_optional"] = treasure_optional_ok
        details["treasure_optional"] = (
            "All treasure rooms are optional side content."
            if treasure_optional_ok
            else f"Treasure rooms on critical path: {treasure_on_critical}."
        )

        branching_ok = max((len(room.neighbors) for room in rooms.values()), default=0) <= cfg.max_branch_factor
        checks["branching_factor_limit"] = branching_ok
        details["branching_factor_limit"] = (
            f"Maximum room degree is within {cfg.max_branch_factor}."
            if branching_ok
            else f"A room exceeds max branch factor {cfg.max_branch_factor}."
        )

        dead_critical: List[int] = []
        if len(critical_path) >= 2:
            for index, room_id in enumerate(critical_path):
                degree = len(rooms[room_id].neighbors)
                if index == 0 or index == len(critical_path) - 1:
                    expected_min = 1
                else:
                    expected_min = 2
                if degree < expected_min:
                    dead_critical.append(room_id)
        critical_ok = len(dead_critical) == 0
        checks["critical_path_contiguous"] = critical_ok
        details["critical_path_contiguous"] = (
            "Critical path is contiguous and does not terminate early."
            if critical_ok
            else f"Critical path rooms with insufficient connections: {dead_critical}."
        )

        actual_counts = self._count_types(rooms)
        target_counts_ok = actual_counts == {
            "start": 1,
            "boss": 1,
            "combat": targets["combat"],
            "treasure": targets["treasure"],
            "puzzle": targets["puzzle"],
        }
        checks["room_type_targets"] = target_counts_ok
        details["room_type_targets"] = (
            f"Actual counts match targets: {actual_counts}."
            if target_counts_ok
            else f"Counts differ. Actual = {actual_counts}, targets = {targets}."
        )

        boss_distance_ok = reachable and distance >= cfg.min_boss_distance
        checks["boss_distance"] = boss_distance_ok
        details["boss_distance"] = (
            f"Boss distance {distance} satisfies minimum of {cfg.min_boss_distance}."
            if boss_distance_ok
            else f"Boss distance {distance} is below minimum of {cfg.min_boss_distance}."
        )

        if not cfg.allow_dead_end_side_rooms:
            side_dead_ends = [
                room_id
                for room_id, room in rooms.items()
                if room_id not in critical_set and len(room.neighbors) == 1
            ]
            checks["dead_end_side_rooms"] = len(side_dead_ends) == 0
            details["dead_end_side_rooms"] = (
                "No dead-end side rooms present."
                if not side_dead_ends
                else f"Dead-end side rooms found: {side_dead_ends}."
            )
        else:
            checks["dead_end_side_rooms"] = True
            details["dead_end_side_rooms"] = "Dead-end side rooms are allowed by configuration."

        return ValidationResult(checks=checks, details=details)

    def _compute_metrics(self, rooms: Dict[int, Room], critical_path: List[int]) -> Dict[str, float]:
        room_count = len(rooms)
        degrees = [len(room.neighbors) for room in rooms.values()]
        critical_set = set(critical_path)
        dead_ends = [room.id for room in rooms.values() if len(room.neighbors) == 1]
        optional_rooms = [
            room.id for room in rooms.values()
            if room.id not in critical_set and room.room_type in {"treasure", "puzzle"}
        ]
        side_rooms = [room.id for room in rooms.values() if room.id not in critical_set]
        return {
            "room_count": room_count,
            "occupancy": round(room_count / max(1, self.config.width * self.config.height), 3),
            "critical_path_rooms": len(critical_path),
            "critical_path_edges": max(0, len(critical_path) - 1),
            "side_rooms": len(side_rooms),
            "dead_ends": len(dead_ends),
            "avg_degree": round(sum(degrees) / max(1, len(degrees)), 3),
            "max_degree": max(degrees) if degrees else 0,
            "linearity": round(len(critical_path) / max(1, room_count), 3),
            "optional_content_ratio": round(len(optional_rooms) / max(1, room_count), 3),
            "branch_rooms": len([room_id for room_id in side_rooms if len(rooms[room_id].neighbors) > 1]),
        }

    def _path_distance(self, rooms: Dict[int, Room], start_id: int, target_id: int) -> Tuple[bool, int]:
        queue = deque([(start_id, 0)])
        visited = {start_id}
        while queue:
            room_id, dist = queue.popleft()
            if room_id == target_id:
                return True, dist
            for neighbor in rooms[room_id].neighbors:
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                queue.append((neighbor, dist + 1))
        return False, -1

    def _count_types(self, rooms: Dict[int, Room]) -> Dict[str, int]:
        counts = Counter(room.room_type for room in rooms.values())
        for room_type in ["start", "boss", "combat", "treasure", "puzzle"]:
            counts.setdefault(room_type, 0)
        return dict(counts)

    def _neighbors_xy(self, x: int, y: int) -> List[Tuple[int, int]]:
        candidates = [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
        return [
            (nx, ny)
            for nx, ny in candidates
            if 0 <= nx < self.config.width and 0 <= ny < self.config.height
        ]


# ---------------------------------------------------------------------------
# DungeonCanvas: custom painted widget replacing tk.Canvas
# ---------------------------------------------------------------------------

class DungeonCanvas(QWidget):
    """Custom-painted canvas that draws the dungeon and emits clicks on rooms."""

    room_clicked = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.dungeon: Optional[Dungeon] = None
        self.selected_room_id: Optional[int] = None
        self.cell_hitboxes: Dict[int, Tuple[float, float, float, float]] = {}
        self.setMinimumSize(400, 400)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_dungeon(self, dungeon: Optional[Dungeon]) -> None:
        self.dungeon = dungeon
        self.selected_room_id = None
        self.update()  # request a repaint

    def set_selected(self, room_id: Optional[int]) -> None:
        self.selected_room_id = room_id
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#FAFAFA"))

        self.cell_hitboxes.clear()
        if not self.dungeon:
            return

        w = max(1, self.width())
        h = max(1, self.height())
        margin = 28
        grid_w = max(1, self.dungeon.width)
        grid_h = max(1, self.dungeon.height)
        cell_size = min((w - margin * 2) / grid_w, (h - margin * 2) / grid_h)
        room_size = max(18.0, min(48.0, cell_size * 0.62))
        offset_x = (w - cell_size * grid_w) / 2
        offset_y = (h - cell_size * grid_h) / 2

        # Grid
        grid_pen = QPen(QColor(GRID_COLOR))
        grid_pen.setWidthF(1)
        painter.setPen(grid_pen)
        for gx in range(grid_w + 1):
            x = offset_x + gx * cell_size
            painter.drawLine(QPointF(x, offset_y), QPointF(x, offset_y + grid_h * cell_size))
        for gy in range(grid_h + 1):
            y = offset_y + gy * cell_size
            painter.drawLine(QPointF(offset_x, y), QPointF(offset_x + grid_w * cell_size, y))

        # Edges
        critical_edges: Set[Tuple[int, int]] = set()
        for a, b in zip(self.dungeon.critical_path, self.dungeon.critical_path[1:]):
            critical_edges.add(tuple(sorted((a, b))))

        drawn_edges: Set[Tuple[int, int]] = set()
        for room in self.dungeon.rooms.values():
            x1, y1 = self._room_center(room, cell_size, offset_x, offset_y)
            for neighbor_id in room.neighbors:
                edge = tuple(sorted((room.id, neighbor_id)))
                if edge in drawn_edges:
                    continue
                neighbor = self.dungeon.rooms[neighbor_id]
                x2, y2 = self._room_center(neighbor, cell_size, offset_x, offset_y)
                is_critical = edge in critical_edges
                pen = QPen(QColor(CRITICAL_EDGE_COLOR if is_critical else NORMAL_EDGE_COLOR))
                pen.setWidthF(5.0 if is_critical else 3.0)
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
                drawn_edges.add(edge)

        # Rooms
        room_font = QFont(self.font())
        room_font.setPointSize(max(10, int(room_size * 0.25)))
        room_font.setBold(True)
        id_font = QFont(self.font())
        id_font.setPointSize(9)

        for room in self.dungeon.rooms.values():
            cx, cy = self._room_center(room, cell_size, offset_x, offset_y)
            x1 = cx - room_size / 2
            y1 = cy - room_size / 2
            x2 = cx + room_size / 2
            y2 = cy + room_size / 2

            fill = QColor(ROOM_COLORS.get(room.room_type, ROOM_COLORS["empty"]))
            outline_color = QColor(
                SELECTED_OUTLINE if room.id == self.selected_room_id else "#455A64"
            )
            outline_width = 4 if room.id == self.selected_room_id else 2

            pen = QPen(outline_color)
            pen.setWidth(outline_width)
            painter.setPen(pen)
            painter.setBrush(QBrush(fill))
            painter.drawRect(QRectF(x1, y1, room_size, room_size))

            # Room type letter, centered
            painter.setFont(room_font)
            painter.setPen(QColor("white"))
            label = ROOM_LABELS.get(room.room_type, "?")
            painter.drawText(QRectF(x1, y1, room_size, room_size), Qt.AlignCenter, label)

            # Room id, just above the rect
            painter.setFont(id_font)
            painter.setPen(QColor("#37474F"))
            painter.drawText(
                QRectF(x1, y1 - 16, room_size, 14),
                Qt.AlignCenter,
                str(room.id),
            )

            self.cell_hitboxes[room.id] = (x1, y1, x2, y2)

    def _room_center(
        self, room: Room, cell_size: float, offset_x: float, offset_y: float
    ) -> Tuple[float, float]:
        cx = offset_x + (room.x + 0.5) * cell_size
        cy = offset_y + (room.y + 0.5) * cell_size
        return cx, cy

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.dungeon:
            return
        pos = event.position()
        x, y = pos.x(), pos.y()
        for room_id, (x1, y1, x2, y2) in self.cell_hitboxes.items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                self.selected_room_id = room_id
                self.update()
                self.room_clicked.emit(room_id)
                return


# ---------------------------------------------------------------------------
# ControlsPanel: form inputs, action buttons, metrics + validation display
# ---------------------------------------------------------------------------

class ControlsPanel(QWidget):
    """Left sidebar. Exposes Qt signals for coordinator actions."""

    generate_requested = Signal()
    random_seed_requested = Signal()
    save_config_requested = Signal()
    load_config_requested = Signal()
    export_json_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._build_widgets()
        self._build_layout()
        self._wire_buttons()

    # -- Widget construction -------------------------------------------------

    def _build_widgets(self) -> None:
        # Generation settings
        self.width_spin = QSpinBox()
        self.width_spin.setRange(3, 30)
        self.width_spin.setValue(12)

        self.height_spin = QSpinBox()
        self.height_spin.setRange(3, 30)
        self.height_spin.setValue(8)

        self.room_count_spin = QSpinBox()
        self.room_count_spin.setRange(2, 300)
        self.room_count_spin.setValue(18)

        self.seed_edit = QLineEdit("42")

        self.style_combo = QComboBox()
        self.style_combo.addItems(list(STYLE_PRESETS.keys()))
        self.style_combo.setCurrentText("Balanced")

        # Ratios
        self.combat_slider, self.combat_value_label = self._make_slider_with_label(0, 100, 60)
        self.treasure_slider, self.treasure_value_label = self._make_slider_with_label(0, 100, 20)
        self.puzzle_slider, self.puzzle_value_label = self._make_slider_with_label(0, 100, 20)

        # Constraints
        self.min_boss_spin = QSpinBox()
        self.min_boss_spin.setRange(1, 100)
        self.min_boss_spin.setValue(6)

        self.max_branch_spin = QSpinBox()
        self.max_branch_spin.setRange(2, 4)
        self.max_branch_spin.setValue(3)

        self.optional_treasure_check = QCheckBox("Treasure rooms must stay optional")
        self.optional_treasure_check.setChecked(True)

        self.allow_dead_end_check = QCheckBox("Allow dead-end side rooms")
        self.allow_dead_end_check.setChecked(True)

        # Buttons
        self.generate_btn = QPushButton("Generate")
        self.random_seed_btn = QPushButton("Random Seed")
        self.save_config_btn = QPushButton("Save Config")
        self.load_config_btn = QPushButton("Load Config")
        self.export_json_btn = QPushButton("Export JSON")

        # Display labels
        self.metrics_label = QLabel("Generate a layout to see quality metrics.")
        self.metrics_label.setWordWrap(True)
        self.metrics_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.metrics_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.validation_label = QLabel("Validation results will appear here.")
        self.validation_label.setWordWrap(True)
        self.validation_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.validation_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

    def _make_slider_with_label(
        self, minimum: int, maximum: int, initial: int
    ) -> Tuple[QSlider, QLabel]:
        slider = QSlider(Qt.Horizontal)
        slider.setRange(minimum, maximum)
        slider.setValue(initial)
        value_label = QLabel(str(initial))
        value_label.setMinimumWidth(30)
        value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        slider.valueChanged.connect(lambda v: value_label.setText(str(v)))
        return slider, value_label

    def _slider_row(self, slider: QSlider, value_label: QLabel) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(slider)
        layout.addWidget(value_label)
        return container

    # -- Layout --------------------------------------------------------------

    def _build_layout(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        # Generation Settings
        gen_group = QGroupBox("Generation Settings")
        gen_form = QFormLayout(gen_group)
        gen_form.addRow("Map Width", self.width_spin)
        gen_form.addRow("Map Height", self.height_spin)
        gen_form.addRow("Room Count", self.room_count_spin)
        gen_form.addRow("Seed", self.seed_edit)
        gen_form.addRow("Style", self.style_combo)
        root.addWidget(gen_group)

        # Room Type Ratios
        ratios_group = QGroupBox("Room Type Ratios")
        ratios_form = QFormLayout(ratios_group)
        ratios_form.addRow("Combat", self._slider_row(self.combat_slider, self.combat_value_label))
        ratios_form.addRow("Treasure", self._slider_row(self.treasure_slider, self.treasure_value_label))
        ratios_form.addRow("Puzzle", self._slider_row(self.puzzle_slider, self.puzzle_value_label))
        root.addWidget(ratios_group)

        # Constraints
        constraints_group = QGroupBox("Constraints")
        constraints_layout = QVBoxLayout(constraints_group)
        spin_form = QFormLayout()
        spin_form.addRow("Min Boss Distance", self.min_boss_spin)
        spin_form.addRow("Max Branch Factor", self.max_branch_spin)
        constraints_layout.addLayout(spin_form)
        constraints_layout.addWidget(self.optional_treasure_check)
        constraints_layout.addWidget(self.allow_dead_end_check)
        root.addWidget(constraints_group)

        # Actions
        actions_group = QGroupBox("Actions")
        actions_grid = QGridLayout(actions_group)
        actions_grid.addWidget(self.generate_btn, 0, 0)
        actions_grid.addWidget(self.random_seed_btn, 0, 1)
        actions_grid.addWidget(self.save_config_btn, 1, 0)
        actions_grid.addWidget(self.load_config_btn, 1, 1)
        actions_grid.addWidget(self.export_json_btn, 2, 0, 1, 2)
        root.addWidget(actions_group)

        # Metrics
        metrics_group = QGroupBox("Quality Metrics")
        metrics_layout = QVBoxLayout(metrics_group)
        metrics_layout.addWidget(self.metrics_label)
        root.addWidget(metrics_group)

        # Validation
        validation_group = QGroupBox("Validation")
        validation_layout = QVBoxLayout(validation_group)
        validation_layout.addWidget(self.validation_label)
        root.addWidget(validation_group)

        root.addStretch(1)

    def _wire_buttons(self) -> None:
        self.generate_btn.clicked.connect(self.generate_requested.emit)
        self.random_seed_btn.clicked.connect(self.random_seed_requested.emit)
        self.save_config_btn.clicked.connect(self.save_config_requested.emit)
        self.load_config_btn.clicked.connect(self.load_config_requested.emit)
        self.export_json_btn.clicked.connect(self.export_json_requested.emit)

    # -- Public API ----------------------------------------------------------

    def get_config(self) -> GeneratorConfig:
        return GeneratorConfig(
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            room_count=self.room_count_spin.value(),
            seed=self.seed_edit.text(),
            style=self.style_combo.currentText(),
            combat_ratio=self.combat_slider.value(),
            treasure_ratio=self.treasure_slider.value(),
            puzzle_ratio=self.puzzle_slider.value(),
            min_boss_distance=self.min_boss_spin.value(),
            max_branch_factor=self.max_branch_spin.value(),
            allow_dead_end_side_rooms=self.allow_dead_end_check.isChecked(),
            optional_treasure=self.optional_treasure_check.isChecked(),
        ).normalized()

    def set_config(self, cfg: GeneratorConfig) -> None:
        self.width_spin.setValue(cfg.width)
        self.height_spin.setValue(cfg.height)
        self.room_count_spin.setValue(cfg.room_count)
        self.seed_edit.setText(cfg.seed)
        self.style_combo.setCurrentText(cfg.style)
        self.combat_slider.setValue(cfg.combat_ratio)
        self.treasure_slider.setValue(cfg.treasure_ratio)
        self.puzzle_slider.setValue(cfg.puzzle_ratio)
        self.min_boss_spin.setValue(cfg.min_boss_distance)
        self.max_branch_spin.setValue(cfg.max_branch_factor)
        self.allow_dead_end_check.setChecked(cfg.allow_dead_end_side_rooms)
        self.optional_treasure_check.setChecked(cfg.optional_treasure)

    def set_seed(self, seed: str) -> None:
        self.seed_edit.setText(seed)

    def update_display(self, dungeon: Dungeon) -> None:
        metrics = dungeon.metrics
        self.metrics_label.setText(
            "\n".join(
                [
                    f"Rooms: {int(metrics['room_count'])}",
                    f"Occupancy: {metrics['occupancy']:.3f}",
                    f"Critical path rooms: {int(metrics['critical_path_rooms'])}",
                    f"Critical path edges: {int(metrics['critical_path_edges'])}",
                    f"Side rooms: {int(metrics['side_rooms'])}",
                    f"Dead ends: {int(metrics['dead_ends'])}",
                    f"Average degree: {metrics['avg_degree']:.3f}",
                    f"Max degree: {int(metrics['max_degree'])}",
                    f"Linearity: {metrics['linearity']:.3f}",
                    f"Optional content ratio: {metrics['optional_content_ratio']:.3f}",
                ]
            )
        )
        validation_lines = []
        for key, ok in dungeon.validation.checks.items():
            label = key.replace("_", " ").title()
            icon = "✓" if ok else "✗"
            validation_lines.append(f"{icon} {label}: {dungeon.validation.details[key]}")
        self.validation_label.setText("\n\n".join(validation_lines))

    def clear_display(self, validation_message: str = "Validation results will appear here.") -> None:
        self.metrics_label.setText("No metrics available.")
        self.validation_label.setText(validation_message)


# ---------------------------------------------------------------------------
# PreviewCanvas: canvas + legend + room inspector
# ---------------------------------------------------------------------------

class PreviewCanvas(QWidget):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.dungeon: Optional[Dungeon] = None

        self.canvas = DungeonCanvas()
        self.canvas.room_clicked.connect(self._on_room_clicked)

        self.inspector_label = QLabel("Click a room to inspect it.")
        self.inspector_label.setWordWrap(True)
        self.inspector_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.inspector_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self._build_layout()

    def _build_layout(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        preview_group = QGroupBox("Dungeon Preview")
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setContentsMargins(10, 10, 10, 10)
        preview_layout.addWidget(self.canvas)
        root.addWidget(preview_group, 1)

        info_layout = QHBoxLayout()

        legend_group = QGroupBox("Legend")
        legend_layout = QVBoxLayout(legend_group)
        for room_type in ["start", "combat", "treasure", "puzzle", "boss"]:
            row = QHBoxLayout()
            swatch = QLabel()
            swatch.setFixedSize(18, 18)
            swatch.setStyleSheet(
                f"background-color: {ROOM_COLORS[room_type]}; border: 1px solid #37474F;"
            )
            row.addWidget(swatch)
            row.addWidget(QLabel(room_type.title()))
            row.addStretch(1)
            legend_layout.addLayout(row)
        legend_layout.addWidget(QLabel("Critical path highlighted in gold."))
        legend_layout.addStretch(1)

        inspector_group = QGroupBox("Room Inspector")
        inspector_layout = QVBoxLayout(inspector_group)
        inspector_layout.addWidget(self.inspector_label)

        info_layout.addWidget(legend_group)
        info_layout.addWidget(inspector_group, 1)
        root.addLayout(info_layout)

    def set_dungeon(self, dungeon: Optional[Dungeon]) -> None:
        self.dungeon = dungeon
        self.canvas.set_dungeon(dungeon)
        if dungeon is None:
            self.inspector_label.setText("Click a room to inspect it.")
            return
        counts = Counter(room.room_type for room in dungeon.rooms.values())
        self.inspector_label.setText(
            "Layout summary\n"
            f"Start: Room {dungeon.start_id}\n"
            f"Boss: Room {dungeon.boss_id}\n"
            f"Room types: {dict(counts)}\n"
            "Click a room for detailed inspection."
        )

    def _on_room_clicked(self, room_id: int) -> None:
        if not self.dungeon:
            return
        room = self.dungeon.rooms[room_id]
        critical_text = (
            f"Yes, index {room.critical_index}" if room.critical_index is not None else "No"
        )
        self.inspector_label.setText(
            "Selected room\n"
            f"ID: {room.id}\n"
            f"Type: {room.room_type.title()}\n"
            f"Grid position: ({room.x}, {room.y})\n"
            f"Degree: {len(room.neighbors)}\n"
            f"On critical path: {critical_text}\n"
            f"Neighbors: {room.neighbors}"
        )


# ---------------------------------------------------------------------------
# Main window coordinator
# ---------------------------------------------------------------------------

class DungeonToolApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Procedural Dungeon / Encounter Layout Tool")
        self.resize(1480, 900)
        self.setMinimumSize(1180, 760)

        self.dungeon: Optional[Dungeon] = None

        # Panels
        self.controls = ControlsPanel()
        self.preview = PreviewCanvas()

        # Wrap controls in a scroll area so the sidebar stays usable on small screens
        controls_scroll = QScrollArea()
        controls_scroll.setWidget(self.controls)
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QFrame.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(controls_scroll)
        splitter.addWidget(self.preview)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 1120])

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(12, 12, 12, 12)
        container_layout.addWidget(splitter)
        self.setCentralWidget(container)

        self.statusBar().showMessage("Ready.")

        # Wire signals
        self.controls.generate_requested.connect(self.generate_layout)
        self.controls.random_seed_requested.connect(self.randomize_seed_and_generate)
        self.controls.save_config_requested.connect(self.save_config)
        self.controls.load_config_requested.connect(self.load_config)
        self.controls.export_json_requested.connect(self.export_json)

        self.generate_layout()

    # -- Generation flow -----------------------------------------------------

    def generate_layout(self) -> None:
        try:
            cfg = self.controls.get_config()
            self.dungeon = DungeonGenerator(cfg).generate()
            self.controls.update_display(self.dungeon)
            self.preview.set_dungeon(self.dungeon)
            result = "PASS" if self.dungeon.validation.all_passed else "FAIL"
            self.statusBar().showMessage(
                f"Generated seed {self.dungeon.seed}. Validation: {result}."
            )
        except Exception as exc:
            self.dungeon = None
            self.controls.clear_display(f"Generation failed.\n\n{exc}")
            self.preview.set_dungeon(None)
            self.statusBar().showMessage(f"Generation failed: {exc}")

    def randomize_seed_and_generate(self) -> None:
        self.controls.set_seed(str(random.randrange(0, 10_000_000)))
        self.generate_layout()

    # -- File I/O ------------------------------------------------------------

    def save_config(self) -> None:
        cfg = asdict(self.controls.get_config())
        path, _ = QFileDialog.getSaveFileName(
            self, "Save configuration", "", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(cfg, handle, indent=2)
        except OSError as exc:
            QMessageBox.warning(self, "Save configuration", f"Could not save configuration:\n{exc}")
            self.statusBar().showMessage("Save failed.")
            return
        self.statusBar().showMessage(f"Saved configuration to {path}.")

    def load_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load configuration", "", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Load configuration", f"Could not load configuration:\n{exc}")
            self.statusBar().showMessage("Load failed.")
            return
        if not isinstance(payload, dict):
            QMessageBox.warning(self, "Load configuration", "Configuration file is not a JSON object.")
            self.statusBar().showMessage("Load failed.")
            return

        known = {f.name for f in fields(GeneratorConfig)}
        filtered = {k: v for k, v in payload.items() if k in known}
        try:
            cfg = GeneratorConfig(**filtered).normalized()
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Load configuration", f"Configuration is invalid:\n{exc}")
            self.statusBar().showMessage("Load failed.")
            return

        self.controls.set_config(cfg)
        self.statusBar().showMessage(f"Loaded configuration from {path}.")
        self.generate_layout()

    def export_json(self) -> None:
        if not self.dungeon:
            QMessageBox.information(self, "Export JSON", "Generate a dungeon first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export dungeon JSON", "", "JSON files (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self.dungeon.to_export_dict(), handle, indent=2)
        except OSError as exc:
            QMessageBox.warning(self, "Export JSON", f"Could not export dungeon:\n{exc}")
            self.statusBar().showMessage("Export failed.")
            return
        self.statusBar().showMessage(f"Exported dungeon JSON to {path}.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = QApplication(sys.argv)
    window = DungeonToolApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
