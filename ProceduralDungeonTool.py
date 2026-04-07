from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import statistics
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


ROOM_COLORS = {
    "start": "#4CAF50",
    "combat": "#D32F2F",
    "treasure": "#F9A825",
    "puzzle": "#1976D2",
    "boss": "#6A1B9A",
    "empty": "#ECEFF1",
}

CRITICAL_EDGE_COLOR = "#FFB300"
NORMAL_EDGE_COLOR = "#90A4AE"
GRID_COLOR = "#CFD8DC"
SELECTED_OUTLINE = "#111827"

STYLE_PRESETS = {
    "Linear": {"critical_fraction": 0.82, "branch_length": (1, 2)},
    "Balanced": {"critical_fraction": 0.62, "branch_length": (1, 3)},
    "Branching": {"critical_fraction": 0.45, "branch_length": (2, 4)},
}


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
    batch_count: int = 100

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
        cfg.batch_count = max(1, int(cfg.batch_count))
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
    config: Dict
    rooms: Dict[int, Room]
    start_id: int
    boss_id: int
    critical_path: List[int]
    type_targets: Dict[str, int]
    metrics: Dict[str, float]
    validation: ValidationResult

    def to_export_dict(self) -> Dict:
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
        output = int(digest[:16], 16)
        print(f"digest seed: {output}")
        return output

    def generate(self) -> Dungeon:
        cfg = self.config
        usable_slots = cfg.room_count - 2
        if usable_slots < 0:
            raise GenerationError("Room count must be at least 2 for start and boss.")
        if cfg.room_count > cfg.width * cfg.height:
            raise GenerationError("Room count exceeds available grid space.")

        type_targets = self._calculate_type_targets(usable_slots)
        critical_path_room_count = self._choose_critical_path_room_count(cfg, type_targets)

        best_dungeon = None
        best_score = -1
        last_error = None

        for _ in range(200):
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
        raise GenerationError(str(last_error or "Could not build a valid layout."))

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
        branch_attempt_budget = max(100, cfg.room_count * 20)

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
            for _ in range(80):
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
        neighbors = []
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
            neighbors.append((score, coord))

        self.rng.shuffle(neighbors)
        neighbors.sort(key=lambda item: item[0], reverse=True)

        for _, coord in neighbors:
            occupied.add(coord)
            path.append(coord)
            if self._path_dfs(path, occupied, target_len, origin):
                return True
            path.pop()
            occupied.remove(coord)
        return False

    def _choose_branch_root(self, candidates: List[int], rooms: Dict[int, Room], critical_path: List[int]) -> int:
        critical_set = set(critical_path)
        weighted = []
        for room_id in candidates:
            room = rooms[room_id]
            degree = len(room.neighbors)
            weight = 1.0
            if room_id in critical_set:
                if self.config.style == "Linear":
                    weight *= 0.8
                elif self.config.style == "Balanced":
                    weight *= 1.2
                else:
                    weight *= 1.6
            else:
                if self.config.style == "Branching":
                    weight *= 1.35
            weight *= max(0.4, (self.config.max_branch_factor - degree + 0.2))
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
            options = []
            cx, cy = rooms[current_id].pos
            for nx, ny in self._neighbors_xy(cx, cy):
                if (nx, ny) in coord_to_id:
                    continue
                openness = sum(1 for ox, oy in self._neighbors_xy(nx, ny) if (ox, oy) not in coord_to_id)
                adjacency_bonus = sum(1 for ox, oy in self._neighbors_xy(nx, ny) if (ox, oy) in coord_to_id)
                score = openness + self.rng.random() + adjacency_bonus * 0.35
                if current_id in critical_set and cfg.style == "Linear":
                    score += 0.15
                options.append((score, (nx, ny)))
            if not options:
                break
            options.sort(key=lambda item: item[0], reverse=True)
            _, coord = options[0]
            room_id = len(rooms)
            new_room = Room(id=room_id, x=coord[0], y=coord[1], room_type="combat")
            rooms[room_id] = new_room
            coord_to_id[coord] = room_id
            rooms[current_id].neighbors.append(room_id)
            new_room.neighbors.append(current_id)

            adjacent_room_ids = []
            for nx, ny in self._neighbors_xy(coord[0], coord[1]):
                neighbor_id = coord_to_id.get((nx, ny))
                if neighbor_id is None or neighbor_id == current_id:
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
                if not cfg.allow_dead_end_side_rooms or self.rng.random() < 0.28:
                    new_room.neighbors.append(neighbor_id)
                    rooms[neighbor_id].neighbors.append(room_id)

            built += 1
            current_id = room_id
        return built

    def _trim_or_extend_dead_end_side_rooms(
        self,
        rooms: Dict[int, Room],
        coord_to_id: Dict[Tuple[int, int], int],
        critical_path: List[int],
    ) -> None:
        cfg = self.config
        critical_set = set(critical_path)
        for _ in range(10):
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
                options = []
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
        critical_inner_ids = [room_id for room_id in available_ids if room_id in critical_set]

        if self.config.optional_treasure and remaining_targets["treasure"] > len(side_ids):
            raise GenerationError(
                "Optional treasure constraint could not be satisfied with the current layout style."
            )

        self.rng.shuffle(side_ids)
        self.rng.shuffle(critical_inner_ids)

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

        dead_critical = []
        if len(critical_path) >= 2:
            for index, room_id in enumerate(critical_path):
                degree = len(rooms[room_id].neighbors)
                if index == 0:
                    expected_min = 1
                elif index == len(critical_path) - 1:
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

        target_counts_ok = self._count_types(rooms) == {
            "start": 1,
            "boss": 1,
            "combat": targets["combat"],
            "treasure": targets["treasure"],
            "puzzle": targets["puzzle"],
        }
        checks["room_type_targets"] = target_counts_ok
        actual_counts = self._count_types(rooms)
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
        optional_rooms = [room.id for room in rooms.values() if room.id not in critical_set and room.room_type in {"treasure", "puzzle"}]
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


class DungeonToolApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Procedural Dungeon / Encounter Layout Tool")
        self.root.geometry("1480x900")
        self.root.minsize(1180, 760)
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        self.dungeon: Optional[Dungeon] = None
        self.cell_hitboxes: Dict[int, Tuple[float, float, float, float]] = {}
        self.selected_room_id: Optional[int] = None

        self.width_var = tk.IntVar(value=12)
        self.height_var = tk.IntVar(value=8)
        self.room_count_var = tk.IntVar(value=18)
        self.seed_var = tk.StringVar(value="42")
        self.style_var = tk.StringVar(value="Balanced")

        self.combat_ratio_var = tk.IntVar(value=60)
        self.treasure_ratio_var = tk.IntVar(value=20)
        self.puzzle_ratio_var = tk.IntVar(value=20)
        self.min_boss_distance_var = tk.IntVar(value=6)
        self.max_branch_factor_var = tk.IntVar(value=3)
        self.allow_dead_end_side_rooms_var = tk.BooleanVar(value=True)
        self.optional_treasure_var = tk.BooleanVar(value=True)
        self.batch_count_var = tk.IntVar(value=100)

        self.status_var = tk.StringVar(value="Ready.")
        self.metrics_var = tk.StringVar(value="Generate a layout to see quality metrics.")
        self.validation_var = tk.StringVar(value="Validation results will appear here.")
        self.room_details_var = tk.StringVar(value="Click a room to inspect it.")
        self.batch_summary_var = tk.StringVar(value="Batch generation summary will appear here.")

        self._build_ui()
        self.generate_layout()

    def _build_ui(self) -> None:
        main = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        left = ttk.Frame(main, padding=(0, 0, 8, 0))
        right = ttk.Frame(main)
        main.add(left, weight=0)
        main.add(right, weight=1)

        self._build_controls(left)
        self._build_preview(right)

        status_bar = ttk.Label(self.root, textvariable=self.status_var, anchor="w", relief=tk.GROOVE, padding=6)
        status_bar.pack(fill=tk.X, padx=12, pady=(0, 12))

    def _build_controls(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        generation = ttk.LabelFrame(parent, text="Generation Settings", padding=12)
        generation.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        generation.columnconfigure(1, weight=1)

        self._add_spinbox(generation, "Map Width", self.width_var, 3, 30, 0)
        self._add_spinbox(generation, "Map Height", self.height_var, 3, 30, 1)
        self._add_spinbox(generation, "Room Count", self.room_count_var, 2, 300, 2)

        ttk.Label(generation, text="Seed").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(generation, textvariable=self.seed_var).grid(row=3, column=1, sticky="ew", pady=4)

        ttk.Label(generation, text="Style").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(
            generation,
            textvariable=self.style_var,
            values=list(STYLE_PRESETS.keys()),
            state="readonly",
        ).grid(row=4, column=1, sticky="ew", pady=4)

        ratios = ttk.LabelFrame(parent, text="Room Type Ratios", padding=12)
        ratios.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
        ratios.columnconfigure(1, weight=1)
        self._add_slider(ratios, "Combat", self.combat_ratio_var, 0, 100, 0)
        self._add_slider(ratios, "Treasure", self.treasure_ratio_var, 0, 100, 1)
        self._add_slider(ratios, "Puzzle", self.puzzle_ratio_var, 0, 100, 2)

        constraints = ttk.LabelFrame(parent, text="Constraints", padding=12)
        constraints.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        constraints.columnconfigure(1, weight=1)
        self._add_spinbox(constraints, "Min Boss Distance", self.min_boss_distance_var, 1, 100, 0)
        self._add_spinbox(constraints, "Max Branch Factor", self.max_branch_factor_var, 2, 4, 1)
        ttk.Checkbutton(
            constraints,
            text="Treasure rooms must stay optional",
            variable=self.optional_treasure_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Checkbutton(
            constraints,
            text="Allow dead-end side rooms",
            variable=self.allow_dead_end_side_rooms_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=4)

        actions = ttk.LabelFrame(parent, text="Actions", padding=12)
        actions.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)

        ttk.Button(actions, text="Generate", command=self.generate_layout).grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=4)
        ttk.Button(actions, text="Random Seed", command=self.randomize_seed_and_generate).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(actions, text="Save Config", command=self.save_config).grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=4)
        ttk.Button(actions, text="Load Config", command=self.load_config).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(actions, text="Export JSON", command=self.export_json).grid(row=2, column=0, columnspan=2, sticky="ew", pady=4)

        """
        batch = ttk.LabelFrame(parent, text="Batch Analysis", padding=12)
        batch.grid(row=4, column=0, sticky="nsew", pady=(0, 10))
        batch.columnconfigure(1, weight=1)
        self._add_spinbox(batch, "Maps", self.batch_count_var, 1, 2000, 0)
        ttk.Button(batch, text="Batch Generate", command=self.batch_generate).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 8))
        ttk.Label(batch, textvariable=self.batch_summary_var, justify=tk.LEFT, wraplength=320).grid(
            row=2, column=0, columnspan=2, sticky="w"
        )
        """

        metrics = ttk.LabelFrame(parent, text="Quality Metrics", padding=12)
        metrics.grid(row=5, column=0, sticky="nsew", pady=(0, 10))
        ttk.Label(metrics, textvariable=self.metrics_var, justify=tk.LEFT, wraplength=320).pack(fill=tk.X)

        validation = ttk.LabelFrame(parent, text="Validation", padding=12)
        validation.grid(row=6, column=0, sticky="nsew")
        ttk.Label(validation, textvariable=self.validation_var, justify=tk.LEFT, wraplength=320).pack(fill=tk.X)

    def _build_preview(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        parent.rowconfigure(1, weight=0)

        preview_frame = ttk.LabelFrame(parent, text="Dungeon Preview", padding=10)
        preview_frame.grid(row=0, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(preview_frame, background="#FAFAFA", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda _event: self.redraw())
        self.canvas.bind("<Button-1>", self.on_canvas_click)

        info_frame = ttk.Frame(parent, padding=(0, 10, 0, 0))
        info_frame.grid(row=1, column=0, sticky="ew")
        info_frame.columnconfigure(0, weight=1)
        info_frame.columnconfigure(1, weight=1)

        legend = ttk.LabelFrame(info_frame, text="Legend", labelanchor='nw', padding=10)
        legend.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        legend.columnconfigure(0, weight=0)
        legend.columnconfigure(1, weight=0)
        for index, room_type in enumerate(["start", "combat", "treasure", "puzzle", "boss"]):
            swatch = tk.Canvas(legend, width=18, height=18, highlightthickness=0, bd=0, relief="flat")
            swatch.grid(row=index, column=0, padx=(0, 4), pady=2, sticky="w")
            swatch.create_rectangle(2, 2, 16, 16, fill=ROOM_COLORS[room_type], outline="#37474F")
            ttk.Label(legend, text=room_type.title()).grid(row=index, column=1, sticky="w", pady=2)
        ttk.Label(legend, text="Critical path highlighted in gold.").grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        room_info = ttk.LabelFrame(info_frame, text="Room Inspector", padding=10)
        room_info.grid(row=0, column=1, sticky="nsew")
        room_info.columnconfigure(0, weight=1)
        ttk.Label(room_info, textvariable=self.room_details_var, justify=tk.LEFT, wraplength=520).grid(row=0, column=0, sticky="w")

    def _add_spinbox(
        self,
        parent: ttk.LabelFrame,
        label: str,
        variable: tk.Variable,
        min_val: int,
        max_val: int,
        row: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        widget = ttk.Spinbox(parent, textvariable=variable, from_=min_val, to=max_val, increment=1, width=10)
        widget.grid(row=row, column=1, sticky="ew", pady=4)

    def _add_slider(
        self,
        parent: ttk.LabelFrame,
        label: str,
        variable: tk.IntVar,
        min_val: int,
        max_val: int,
        row: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=1, sticky="ew", pady=4)
        holder.columnconfigure(0, weight=1)
        ttk.Scale(holder, variable=variable, from_=min_val, to=max_val, orient=tk.HORIZONTAL).grid(row=0, column=0, sticky="ew")
        ttk.Label(holder, textvariable=variable, width=4).grid(row=0, column=1, padx=(8, 0))

    def get_config(self) -> GeneratorConfig:
        return GeneratorConfig(
            width=self.width_var.get(),
            height=self.height_var.get(),
            room_count=self.room_count_var.get(),
            seed=self.seed_var.get(),
            style=self.style_var.get(),
            combat_ratio=self.combat_ratio_var.get(),
            treasure_ratio=self.treasure_ratio_var.get(),
            puzzle_ratio=self.puzzle_ratio_var.get(),
            min_boss_distance=self.min_boss_distance_var.get(),
            max_branch_factor=self.max_branch_factor_var.get(),
            allow_dead_end_side_rooms=self.allow_dead_end_side_rooms_var.get(),
            optional_treasure=self.optional_treasure_var.get(),
            batch_count=self.batch_count_var.get(),
        ).normalized()

    def generate_layout(self) -> None:
        try:
            cfg = self.get_config()
            generator = DungeonGenerator(cfg)
            self.dungeon = generator.generate()
            self.selected_room_id = None
            self.update_panels()
            self.redraw()
            result = "PASS" if self.dungeon.validation.all_passed else "FAIL"
            self.status_var.set(f"Generated seed {self.dungeon.seed}. Validation: {result}.")
        except Exception as exc:
            self.dungeon = None
            self.canvas.delete("all")
            self.metrics_var.set("No metrics available.")
            self.validation_var.set(f"Generation failed.\n\n{exc}")
            self.room_details_var.set("Click a room to inspect it.")
            self.status_var.set(f"Generation failed: {exc}")

    def randomize_seed_and_generate(self) -> None:
        self.seed_var.set(str(random.randrange(0, 10_000_000)))
        self.generate_layout()

    def update_panels(self) -> None:
        if not self.dungeon:
            return
        metrics = self.dungeon.metrics
        self.metrics_var.set(
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
        for key, ok in self.dungeon.validation.checks.items():
            label = key.replace("_", " ").title()
            icon = "✓" if ok else "✗"
            validation_lines.append(f"{icon} {label}: {self.dungeon.validation.details[key]}")
        self.validation_var.set("\n\n".join(validation_lines))

        counts = Counter(room.room_type for room in self.dungeon.rooms.values())
        self.room_details_var.set(
            "Layout summary\n"
            f"Start: Room {self.dungeon.start_id}\n"
            f"Boss: Room {self.dungeon.boss_id}\n"
            f"Room types: {dict(counts)}\n"
            "Click a room for detailed inspection."
        )

    def redraw(self) -> None:
        self.canvas.delete("all")
        self.cell_hitboxes.clear()
        if not self.dungeon:
            return

        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        margin = 28
        grid_w = max(1, self.dungeon.width)
        grid_h = max(1, self.dungeon.height)
        cell_size = min((width - margin * 2) / grid_w, (height - margin * 2) / grid_h)
        room_size = max(18, min(48, cell_size * 0.62))
        offset_x = (width - cell_size * grid_w) / 2
        offset_y = (height - cell_size * grid_h) / 2

        for gx in range(grid_w + 1):
            x = offset_x + gx * cell_size
            self.canvas.create_line(x, offset_y, x, offset_y + grid_h * cell_size, fill=GRID_COLOR)
        for gy in range(grid_h + 1):
            y = offset_y + gy * cell_size
            self.canvas.create_line(offset_x, y, offset_x + grid_w * cell_size, y, fill=GRID_COLOR)

        critical_edges = set()
        for a, b in zip(self.dungeon.critical_path, self.dungeon.critical_path[1:]):
            critical_edges.add(tuple(sorted((a, b))))

        drawn_edges = set()
        for room in self.dungeon.rooms.values():
            x1, y1 = self._room_center(room, cell_size, offset_x, offset_y)
            for neighbor_id in room.neighbors:
                edge = tuple(sorted((room.id, neighbor_id)))
                if edge in drawn_edges:
                    continue
                neighbor = self.dungeon.rooms[neighbor_id]
                x2, y2 = self._room_center(neighbor, cell_size, offset_x, offset_y)
                is_critical = edge in critical_edges
                self.canvas.create_line(
                    x1,
                    y1,
                    x2,
                    y2,
                    width=5 if is_critical else 3,
                    fill=CRITICAL_EDGE_COLOR if is_critical else NORMAL_EDGE_COLOR,
                    capstyle=tk.ROUND,
                )
                drawn_edges.add(edge)

        for room in self.dungeon.rooms.values():
            cx, cy = self._room_center(room, cell_size, offset_x, offset_y)
            x1 = cx - room_size / 2
            y1 = cy - room_size / 2
            x2 = cx + room_size / 2
            y2 = cy + room_size / 2
            outline = SELECTED_OUTLINE if room.id == self.selected_room_id else "#455A64"
            outline_width = 4 if room.id == self.selected_room_id else 2
            self.canvas.create_rectangle(
                x1,
                y1,
                x2,
                y2,
                fill=ROOM_COLORS.get(room.room_type, ROOM_COLORS["empty"]),
                outline=outline,
                width=outline_width,
            )
            label = room.room_type[0].upper()
            if room.room_type == "combat":
                label = "C"
            elif room.room_type == "treasure":
                label = "T"
            elif room.room_type == "puzzle":
                label = "P"
            elif room.room_type == "start":
                label = "S"
            elif room.room_type == "boss":
                label = "B"
            self.canvas.create_text(cx, cy, text=label, font=("TkDefaultFont", max(10, int(room_size * 0.25)), "bold"), fill="white")
            self.canvas.create_text(cx, y1 - 10, text=str(room.id), font=("TkDefaultFont", 9), fill="#37474F")
            self.cell_hitboxes[room.id] = (x1, y1, x2, y2)

    def _room_center(self, room: Room, cell_size: float, offset_x: float, offset_y: float) -> Tuple[float, float]:
        cx = offset_x + (room.x + 0.5) * cell_size
        cy = offset_y + (room.y + 0.5) * cell_size
        return cx, cy

    def on_canvas_click(self, event: tk.Event) -> None:
        if not self.dungeon:
            return
        for room_id, (x1, y1, x2, y2) in self.cell_hitboxes.items():
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                self.selected_room_id = room_id
                room = self.dungeon.rooms[room_id]
                critical_text = (
                    f"Yes, index {room.critical_index}" if room.critical_index is not None else "No"
                )
                self.room_details_var.set(
                    "Selected room\n"
                    f"ID: {room.id}\n"
                    f"Type: {room.room_type.title()}\n"
                    f"Grid position: ({room.x}, {room.y})\n"
                    f"Degree: {len(room.neighbors)}\n"
                    f"On critical path: {critical_text}\n"
                    f"Neighbors: {room.neighbors}"
                )
                self.redraw()
                return

    def save_config(self) -> None:
        cfg = asdict(self.get_config())
        path = filedialog.asksaveasfilename(
            title="Save configuration",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=2)
        self.status_var.set(f"Saved configuration to {path}.")

    def load_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Load configuration",
            filetypes=[("JSON files", "*.json")],
        )
        if not path:
            return
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        cfg = GeneratorConfig(**payload).normalized()
        self.width_var.set(cfg.width)
        self.height_var.set(cfg.height)
        self.room_count_var.set(cfg.room_count)
        self.seed_var.set(cfg.seed)
        self.style_var.set(cfg.style)
        self.combat_ratio_var.set(cfg.combat_ratio)
        self.treasure_ratio_var.set(cfg.treasure_ratio)
        self.puzzle_ratio_var.set(cfg.puzzle_ratio)
        self.min_boss_distance_var.set(cfg.min_boss_distance)
        self.max_branch_factor_var.set(cfg.max_branch_factor)
        self.allow_dead_end_side_rooms_var.set(cfg.allow_dead_end_side_rooms)
        self.optional_treasure_var.set(cfg.optional_treasure)
        self.batch_count_var.set(cfg.batch_count)
        self.status_var.set(f"Loaded configuration from {path}.")
        self.generate_layout()

    def export_json(self) -> None:
        if not self.dungeon:
            messagebox.showinfo("Export JSON", "Generate a dungeon first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export dungeon JSON",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.dungeon.to_export_dict(), handle, indent=2)
        self.status_var.set(f"Exported dungeon JSON to {path}.")

    def batch_generate(self) -> None:
        cfg = self.get_config()
        base_seed = cfg.seed or "seed"
        successes = 0
        total = cfg.batch_count
        validation_failures = Counter()
        generation_failures = 0
        metric_buckets: Dict[str, List[float]] = {
            "critical_path_edges": [],
            "dead_ends": [],
            "linearity": [],
            "optional_content_ratio": [],
        }

        for index in range(total):
            batch_cfg = copy.deepcopy(cfg)
            batch_cfg.seed = f"{base_seed}:{index}"
            try:
                dungeon = DungeonGenerator(batch_cfg).generate()
            except Exception:
                generation_failures += 1
                continue
            if dungeon.validation.all_passed:
                successes += 1
                for metric in metric_buckets:
                    metric_buckets[metric].append(float(dungeon.metrics[metric]))
            else:
                for key, ok in dungeon.validation.checks.items():
                    if not ok:
                        validation_failures[key] += 1

        avg_metrics = {
            key: (statistics.mean(values) if values else 0.0)
            for key, values in metric_buckets.items()
        }
        top_failures = ", ".join(
            f"{name}={count}" for name, count in validation_failures.most_common(3)
        ) or "none"
        self.batch_summary_var.set(
            "\n".join(
                [
                    f"Pass rate: {successes}/{total} ({(successes / max(1, total)) * 100:.1f}%)",
                    f"Generation failures: {generation_failures}",
                    f"Top validation failures: {top_failures}",
                    f"Avg critical path edges: {avg_metrics['critical_path_edges']:.2f}",
                    f"Avg dead ends: {avg_metrics['dead_ends']:.2f}",
                    f"Avg linearity: {avg_metrics['linearity']:.3f}",
                    f"Avg optional content ratio: {avg_metrics['optional_content_ratio']:.3f}",
                ]
            )
        )
        self.status_var.set(f"Finished batch generation for {total} maps.")


def main() -> None:
    root = tk.Tk()
    app = DungeonToolApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
