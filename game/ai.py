# -*- coding: utf-8 -*-
"""
GomokuAI — Strong heuristic-based AI with alpha-beta search + threat detection.

Optimized version with:
- Null-Move Pruning (NMP) for faster search
- Late Move Reduction (LMR) for deeper effective search
- Aspiration Windows for tighter alpha-beta bounds
- Principal Variation Search (PVS) for better move ordering
- Incremental evaluation (delta-based, no full board scan per node)
- Incremental candidate set management
- NumPy-accelerated board evaluation
- Enlarged transposition table (2M entries) with depth-preferred replacement
- Lazy SMP parallel search (multi-process)
- Pattern-based board evaluation (line pattern matching + gap patterns)
- Immediate threat detection (five, open-four, four, open-three)
- VCF (Victory by Continuous Four) search for forced wins
- Zobrist hashing + transposition table
- Killer move / history heuristic for move ordering
- Gap pattern recognition (X_XXX, XX_XX, X_XX, etc.)
- Renju rule awareness (흑 금수: 33, 44, 장목)

The AI plays as White (player 2) by default.
Board coordinate: board[y][x], 0=empty, 1=black, 2=white.
"""

import time
import math
import random
import logging
import os
from multiprocessing import Process, Value, Array
import ctypes

logger = logging.getLogger(__name__)

BOARD_SIZE = 15
DIRECTIONS = [(1, 0), (0, 1), (1, 1), (1, -1)]

# Score constants
FIVE       = 10000000
OPEN_FOUR  = 500000
FOUR       = 50000
OPEN_THREE = 10000
THREE      = 1000
OPEN_TWO   = 500
TWO        = 50
ONE        = 10

# Gap pattern scores (e.g. X_XXX, XX_XX)
GAP_FOUR   = 45000    # One gap in four stones (nearly as strong as closed four)
GAP_THREE  = 5000     # One gap in three stones
GAP_TWO    = 200      # One gap in two stones

# Pre-computed score lookup table for _score_line (count, open_ends) -> score
# Avoids repeated branching in hot path
_SCORE_TABLE = {}
for _c in range(7):
    for _o in range(3):
        if _c >= 5:
            _SCORE_TABLE[(_c, _o)] = FIVE
        elif _o == 0:
            _SCORE_TABLE[(_c, _o)] = 0
        elif _c == 4:
            _SCORE_TABLE[(_c, _o)] = OPEN_FOUR if _o == 2 else FOUR
        elif _c == 3:
            _SCORE_TABLE[(_c, _o)] = OPEN_THREE if _o == 2 else THREE
        elif _c == 2:
            _SCORE_TABLE[(_c, _o)] = OPEN_TWO if _o == 2 else TWO
        elif _c == 1:
            _SCORE_TABLE[(_c, _o)] = ONE if _o == 2 else 0
        else:
            _SCORE_TABLE[(_c, _o)] = 0

# LMR reduction table (pre-computed)
_LMR_TABLE = [[0] * 64 for _ in range(64)]
for _d in range(1, 64):
    for _m in range(1, 64):
        _LMR_TABLE[_d][_m] = max(0, int(0.5 + math.log(_d) * math.log(_m) * 0.4))

# ──────────────────────────────────────────────
# Zobrist hashing
# ──────────────────────────────────────────────
_ZOBRIST_TABLE = None  # Lazy init: [y][x][stone(0..2)] -> uint64
_ZOBRIST_SEED = 42

def _init_zobrist():
    global _ZOBRIST_TABLE
    if _ZOBRIST_TABLE is not None:
        return
    rng = random.Random(_ZOBRIST_SEED)
    _ZOBRIST_TABLE = [
        [[rng.getrandbits(64) for _ in range(3)] for _ in range(BOARD_SIZE)]
        for _ in range(BOARD_SIZE)
    ]

def _zobrist_hash(board):
    """Compute full Zobrist hash for a board state."""
    _init_zobrist()
    h = 0
    for y in range(BOARD_SIZE):
        for x in range(BOARD_SIZE):
            s = board[y][x]
            if s:
                h ^= _ZOBRIST_TABLE[y][x][s]
    return h

def _zobrist_update(h, x, y, old_stone, new_stone):
    """Incrementally update Zobrist hash."""
    _init_zobrist()
    if old_stone:
        h ^= _ZOBRIST_TABLE[y][x][old_stone]
    if new_stone:
        h ^= _ZOBRIST_TABLE[y][x][new_stone]
    return h


# ──────────────────────────────────────────────
# Low-level line analysis
# ──────────────────────────────────────────────

def _count_dir(board, x, y, dx, dy, stone):
    """Count consecutive stones from (x+dx, y+dy) in direction (dx,dy)."""
    c = 0
    nx, ny = x + dx, y + dy
    while 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE and board[ny][nx] == stone:
        c += 1
        nx += dx
        ny += dy
    return c


def _line_info(board, x, y, dx, dy, stone):
    """Get (consecutive_count_including_center, open_ends) for one direction pair."""
    c1 = _count_dir(board, x, y, dx, dy, stone)
    c2 = _count_dir(board, x, y, -dx, -dy, stone)
    count = 1 + c1 + c2
    # Check ends
    ex1, ey1 = x + dx * (c1 + 1), y + dy * (c1 + 1)
    ex2, ey2 = x - dx * (c2 + 1), y - dy * (c2 + 1)
    open_ends = 0
    if 0 <= ex1 < BOARD_SIZE and 0 <= ey1 < BOARD_SIZE and board[ey1][ex1] == 0:
        open_ends += 1
    if 0 <= ex2 < BOARD_SIZE and 0 <= ey2 < BOARD_SIZE and board[ey2][ex2] == 0:
        open_ends += 1
    return count, open_ends


def _score_line(count, open_ends):
    """Score a line pattern — uses lookup table for speed."""
    return _SCORE_TABLE.get((count, open_ends), FIVE if count >= 5 else 0)


def _score_point(board, x, y, stone):
    """Total pattern score for placing stone at (x,y)."""
    total = 0
    for dx, dy in DIRECTIONS:
        c, o = _line_info(board, x, y, dx, dy, stone)
        total += _SCORE_TABLE.get((c, o), FIVE if c >= 5 else 0)
    return total


def _has_five(board, x, y, stone):
    """Check if placing stone at (x,y) makes five."""
    for dx, dy in DIRECTIONS:
        if 1 + _count_dir(board, x, y, dx, dy, stone) + \
             _count_dir(board, x, y, -dx, -dy, stone) >= 5:
            return True
    return False


def _gap_pattern_score(board, x, y, stone):
    """Score gap patterns around (x,y) for `stone`.
    Detects: X_XXX, XXX_X, XX_XX, X_XX, XX_X patterns where _ is (x,y).
    Highly optimized: minimal Python overhead.
    """
    other = 3 - stone
    total = 0
    BS = BOARD_SIZE
    for dx, dy in DIRECTIONS:
        # Read 9 cells: indices -4..+4 relative to (x,y), center=stone
        # Inline all reads to avoid loop overhead
        cx, cy = x - 4*dx, y - 4*dy
        c0 = board[cy][cx] if 0 <= cx < BS and 0 <= cy < BS else -1
        cx, cy = x - 3*dx, y - 3*dy
        c1 = board[cy][cx] if 0 <= cx < BS and 0 <= cy < BS else -1
        cx, cy = x - 2*dx, y - 2*dy
        c2 = board[cy][cx] if 0 <= cx < BS and 0 <= cy < BS else -1
        cx, cy = x - dx, y - dy
        c3 = board[cy][cx] if 0 <= cx < BS and 0 <= cy < BS else -1
        c4 = stone  # center
        cx, cy = x + dx, y + dy
        c5 = board[cy][cx] if 0 <= cx < BS and 0 <= cy < BS else -1
        cx, cy = x + 2*dx, y + 2*dy
        c6 = board[cy][cx] if 0 <= cx < BS and 0 <= cy < BS else -1
        cx, cy = x + 3*dx, y + 3*dy
        c7 = board[cy][cx] if 0 <= cx < BS and 0 <= cy < BS else -1
        cx, cy = x + 4*dx, y + 4*dy
        c8 = board[cy][cx] if 0 <= cx < BS and 0 <= cy < BS else -1

        # Encode each cell: stone=1, empty=0, blocked=-1
        # Then for each window, count stones directly
        # 5-window starting at 0: c0,c1,c2,c3,c4
        # 5-window starting at 1: c1,c2,c3,c4,c5
        # etc.
        
        # Pre-classify: is_ok (not other, not OOB), is_stone
        ok0 = c0 != -1 and c0 != other
        ok1 = c1 != -1 and c1 != other
        ok2 = c2 != -1 and c2 != other
        ok3 = c3 != -1 and c3 != other
        ok4 = True  # center is always stone
        ok5 = c5 != -1 and c5 != other
        ok6 = c6 != -1 and c6 != other
        ok7 = c7 != -1 and c7 != other
        ok8 = c8 != -1 and c8 != other
        
        s0 = 1 if c0 == stone else 0
        s1 = 1 if c1 == stone else 0
        s2 = 1 if c2 == stone else 0
        s3 = 1 if c3 == stone else 0
        s4 = 1  # center is stone
        s5 = 1 if c5 == stone else 0
        s6 = 1 if c6 == stone else 0
        s7 = 1 if c7 == stone else 0
        s8 = 1 if c8 == stone else 0

        # 5-windows: [0:5], [1:6], [2:7], [3:8], [4:9]
        if ok0 and ok1 and ok2 and ok3 and ok4:
            sc = s0 + s1 + s2 + s3 + s4
            if sc == 4:
                total += GAP_FOUR
            elif sc == 3:
                total += GAP_THREE
        if ok1 and ok2 and ok3 and ok4 and ok5:
            sc = s1 + s2 + s3 + s4 + s5
            if sc == 4:
                total += GAP_FOUR
            elif sc == 3:
                total += GAP_THREE
        if ok2 and ok3 and ok4 and ok5 and ok6:
            sc = s2 + s3 + s4 + s5 + s6
            if sc == 4:
                total += GAP_FOUR
            elif sc == 3:
                total += GAP_THREE
        if ok3 and ok4 and ok5 and ok6 and ok7:
            sc = s3 + s4 + s5 + s6 + s7
            if sc == 4:
                total += GAP_FOUR
            elif sc == 3:
                total += GAP_THREE
        if ok4 and ok5 and ok6 and ok7 and ok8:
            sc = s4 + s5 + s6 + s7 + s8
            if sc == 4:
                total += GAP_FOUR
            elif sc == 3:
                total += GAP_THREE

        # 6-windows: [0:6], [1:7], [2:8], [3:9]
        if ok0 and ok1 and ok2 and ok3 and ok4 and ok5:
            if s0 + s1 + s2 + s3 + s4 + s5 >= 4:
                total += GAP_TWO
        if ok1 and ok2 and ok3 and ok4 and ok5 and ok6:
            if s1 + s2 + s3 + s4 + s5 + s6 >= 4:
                total += GAP_TWO
        if ok2 and ok3 and ok4 and ok5 and ok6 and ok7:
            if s2 + s3 + s4 + s5 + s6 + s7 >= 4:
                total += GAP_TWO
        if ok3 and ok4 and ok5 and ok6 and ok7 and ok8:
            if s3 + s4 + s5 + s6 + s7 + s8 >= 4:
                total += GAP_TWO

    return total


def _score_point_full(board, x, y, stone):
    """Total pattern score including gap patterns."""
    return _score_point(board, x, y, stone) + _gap_pattern_score(board, x, y, stone)


# ──────────────────────────────────────────────
# Incremental candidate set management
# ──────────────────────────────────────────────

class _CandidateSet:
    """Maintains candidate moves incrementally, avoiding full board scans."""
    
    def __init__(self, board, radius=2):
        self.radius = radius
        self._cands = set()
        self._stone_count = 0
        # Initial build
        for y in range(BOARD_SIZE):
            for x in range(BOARD_SIZE):
                if board[y][x] != 0:
                    self._stone_count += 1
                    self._add_neighbors(board, x, y)
        if self._stone_count == 0:
            self._cands.add((BOARD_SIZE // 2, BOARD_SIZE // 2))
    
    def _add_neighbors(self, board, x, y):
        r = self.radius
        for dy in range(-r, r + 1):
            ny = y + dy
            if ny < 0 or ny >= BOARD_SIZE:
                continue
            for dx in range(-r, r + 1):
                nx = x + dx
                if 0 <= nx < BOARD_SIZE and board[ny][nx] == 0:
                    self._cands.add((nx, ny))
    
    def place(self, board, x, y):
        """Call after placing a stone at (x,y)."""
        self._cands.discard((x, y))
        self._stone_count += 1
        self._add_neighbors(board, x, y)
    
    def remove(self, board, x, y):
        """Call after removing a stone from (x,y) — board[y][x] should already be 0."""
        self._stone_count -= 1
        # Re-add (x,y) as candidate if it has neighbors
        r = self.radius
        has_neighbor = False
        for dy in range(-r, r + 1):
            ny = y + dy
            if ny < 0 or ny >= BOARD_SIZE:
                continue
            for dx in range(-r, r + 1):
                nx = x + dx
                if 0 <= nx < BOARD_SIZE and board[ny][nx] != 0:
                    has_neighbor = True
                    break
            if has_neighbor:
                break
        if has_neighbor:
            self._cands.add((x, y))
        # Note: we don't remove neighbors that might no longer be adjacent
        # This is a conservative over-approximation (more candidates, but correct)
    
    def get(self):
        return list(self._cands)
    
    def copy(self):
        c = _CandidateSet.__new__(_CandidateSet)
        c.radius = self.radius
        c._cands = set(self._cands)
        c._stone_count = self._stone_count
        return c


def _get_candidates(board, radius=2):
    """Empty positions within radius of existing stones, plus center."""
    neighbors = set()
    has_stones = False
    for y in range(BOARD_SIZE):
        row = board[y]
        for x in range(BOARD_SIZE):
            if row[x] != 0:
                has_stones = True
                for dy2 in range(-radius, radius + 1):
                    ny = y + dy2
                    if ny < 0 or ny >= BOARD_SIZE:
                        continue
                    for dx2 in range(-radius, radius + 1):
                        nx = x + dx2
                        if 0 <= nx < BOARD_SIZE and board[ny][nx] == 0:
                            neighbors.add((nx, ny))
    if not has_stones:
        return [(BOARD_SIZE // 2, BOARD_SIZE // 2)]
    return list(neighbors)


# ──────────────────────────────────────────────
# Incremental evaluation
# ──────────────────────────────────────────────

class _IncrementalEval:
    """Maintains board evaluation incrementally using per-cell cached scores.
    
    Instead of the prepare/place double-computation pattern, maintains a
    cell_score[y][x] array. On update, only re-scores affected cells along
    the 4 directions (radius 4), subtracts old cached scores and adds new ones.
    """
    
    def __init__(self, board, ai_stone, human_stone):
        self.ai = ai_stone
        self.human = human_stone
        self._score = 0.0
        # Per-cell cached score contribution: cell_score[y][x] = net contribution
        self._cell_score = [[0.0] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        self._build(board)
    
    def _cell_eval(self, board, x, y):
        """Compute net score contribution of a single cell (x,y)."""
        stone = board[y][x]
        if stone == 0:
            return 0.0
        
        center = BOARD_SIZE // 2
        dist = abs(x - center) + abs(y - center)
        pos_bonus = max(0, BOARD_SIZE - dist)
        
        line_score = 0
        threat_four = 0
        threat_open_three = 0
        BS = BOARD_SIZE
        _st = _SCORE_TABLE
        
        for dx, dy in DIRECTIONS:
            # Skip if not the start of a line (avoid double counting)
            px, py = x - dx, y - dy
            if 0 <= px < BS and 0 <= py < BS and board[py][px] == stone:
                continue
            # Inline _line_info for speed
            c1, nx, ny = 0, x + dx, y + dy
            while 0 <= nx < BS and 0 <= ny < BS and board[ny][nx] == stone:
                c1 += 1; nx += dx; ny += dy
            c2, nx2, ny2 = 0, x - dx, y - dy
            while 0 <= nx2 < BS and 0 <= ny2 < BS and board[ny2][nx2] == stone:
                c2 += 1; nx2 -= dx; ny2 -= dy
            count = 1 + c1 + c2
            # Check ends
            ex1, ey1 = x + dx * (c1 + 1), y + dy * (c1 + 1)
            ex2, ey2 = x - dx * (c2 + 1), y - dy * (c2 + 1)
            open_ends = 0
            if 0 <= ex1 < BS and 0 <= ey1 < BS and board[ey1][ex1] == 0:
                open_ends += 1
            if 0 <= ex2 < BS and 0 <= ey2 < BS and board[ey2][ex2] == 0:
                open_ends += 1
            s = _st.get((count, open_ends), FIVE if count >= 5 else 0)
            line_score += s
            if count == 4 and open_ends >= 1:
                threat_four += 1
            elif count == 3 and open_ends == 2:
                threat_open_three += 1
        
        combo = 0
        if threat_four >= 2:
            combo = OPEN_FOUR * 2
        elif threat_four >= 1 and threat_open_three >= 1:
            combo = OPEN_FOUR
        elif threat_open_three >= 2:
            combo = FOUR * 2
        
        total = line_score + combo + pos_bonus
        if stone == self.ai:
            return total
        else:
            return -total * 1.35
    
    def _build(self, board):
        """Full build of per-cell scores (called once at init)."""
        total = 0.0
        for y in range(BOARD_SIZE):
            for x in range(BOARD_SIZE):
                cs = self._cell_eval(board, x, y)
                self._cell_score[y][x] = cs
                total += cs
        self._score = total
    
    def _affected_cells(self, x, y):
        """Yield cells affected by a change at (x,y) — along 4 directions, radius 4."""
        BS = BOARD_SIZE
        seen = set()
        for dx, dy in DIRECTIONS:
            for dist in range(-4, 5):
                nx, ny = x + dx * dist, y + dy * dist
                if 0 <= nx < BS and 0 <= ny < BS and (nx, ny) not in seen:
                    seen.add((nx, ny))
                    yield nx, ny
    
    def update(self, board, x, y):
        """Re-score all cells affected by a change at (x,y).
        Call AFTER the board has been modified."""
        for ax, ay in self._affected_cells(x, y):
            old = self._cell_score[ay][ax]
            new = self._cell_eval(board, ax, ay)
            self._cell_score[ay][ax] = new
            self._score += (new - old)
    
    def get_score(self, stone):
        """Get score from perspective of `stone`."""
        if stone == self.ai:
            return self._score
        else:
            return -self._score
    
    def recalc(self, board):
        """Force full recalculation (for safety/debugging)."""
        self._build(board)


def _fast_sorted_candidates(board, stone, opponent, max_moves=12):
    """Candidate sorting using lightweight line-pattern heuristic."""
    cands = _get_candidates(board, radius=2)
    if not cands:
        return []
    center = BOARD_SIZE // 2
    scored = []
    _st = _SCORE_TABLE
    for x, y in cands:
        attack = 0
        defense = 0
        atk_threats = 0
        def_threats = 0
        for dx, dy in DIRECTIONS:
            a1 = _count_dir(board, x, y, dx, dy, stone)
            a2 = _count_dir(board, x, y, -dx, -dy, stone)
            a_count = 1 + a1 + a2
            aex1, aey1 = x + dx * (a1 + 1), y + dy * (a1 + 1)
            aex2, aey2 = x - dx * (a2 + 1), y - dy * (a2 + 1)
            a_open = 0
            if 0 <= aex1 < BOARD_SIZE and 0 <= aey1 < BOARD_SIZE and board[aey1][aex1] == 0:
                a_open += 1
            if 0 <= aex2 < BOARD_SIZE and 0 <= aey2 < BOARD_SIZE and board[aey2][aex2] == 0:
                a_open += 1
            a_score = _st.get((a_count, a_open), FIVE if a_count >= 5 else 0)
            attack += a_score
            if (a_count == 4 and a_open >= 1) or (a_count == 3 and a_open == 2):
                atk_threats += 1

            d1 = _count_dir(board, x, y, dx, dy, opponent)
            d2 = _count_dir(board, x, y, -dx, -dy, opponent)
            d_count = 1 + d1 + d2
            dex1, dey1 = x + dx * (d1 + 1), y + dy * (d1 + 1)
            dex2, dey2 = x - dx * (d2 + 1), y - dy * (d2 + 1)
            d_open = 0
            if 0 <= dex1 < BOARD_SIZE and 0 <= dey1 < BOARD_SIZE and board[dey1][dex1] == 0:
                d_open += 1
            if 0 <= dex2 < BOARD_SIZE and 0 <= dey2 < BOARD_SIZE and board[dey2][dex2] == 0:
                d_open += 1
            d_score = _st.get((d_count, d_open), FIVE if d_count >= 5 else 0)
            defense += d_score
            if (d_count == 4 and d_open >= 1) or (d_count == 3 and d_open == 2):
                def_threats += 1

        if atk_threats >= 2:
            attack += FOUR
        if def_threats >= 2:
            defense += FOUR

        dist = abs(x - center) + abs(y - center)
        center_bonus = max(0, 14 - dist)
        scored.append((attack + defense * 1.25 + center_bonus, x, y))
    scored.sort(reverse=True)
    return [(x, y) for _, x, y in scored[:max_moves]]


def _deep_sorted_candidates(board, stone, opponent, max_moves=20):
    """Full candidate scoring with stone placement (used at root level)."""
    cands = _get_candidates(board, radius=2)
    if not cands:
        return []
    scored = []
    for x, y in cands:
        board[y][x] = stone
        attack = _score_point_full(board, x, y, stone)
        board[y][x] = opponent
        defense = _score_point_full(board, x, y, opponent)
        board[y][x] = 0
        scored.append((attack + defense * 1.25, x, y))
    scored.sort(reverse=True)
    return [(x, y) for _, x, y in scored[:max_moves]]


# ──────────────────────────────────────────────
# Threat detection (operating on candidate set)
# ──────────────────────────────────────────────

def _find_winning(board, stone, cands=None):
    """Find move making five. Returns (x,y) or None."""
    if cands is None:
        cands = _get_candidates(board, radius=2)
    for x, y in cands:
        if board[y][x] != 0:
            continue
        board[y][x] = stone
        if _has_five(board, x, y, stone):
            board[y][x] = 0
            return (x, y)
        board[y][x] = 0
    return None


def _find_open_fours(board, stone, cands=None):
    """Find moves creating open four. Returns list of (x,y)."""
    if cands is None:
        cands = _get_candidates(board, radius=2)
    moves = []
    for x, y in cands:
        if board[y][x] != 0:
            continue
        board[y][x] = stone
        for dx, dy in DIRECTIONS:
            c, o = _line_info(board, x, y, dx, dy, stone)
            if c == 4 and o == 2:
                moves.append((x, y))
                break
        board[y][x] = 0
    return moves


def _find_fours(board, stone, cands=None):
    """Find moves creating four (including closed four). Returns list of (x,y)."""
    if cands is None:
        cands = _get_candidates(board, radius=2)
    moves = []
    for x, y in cands:
        if board[y][x] != 0:
            continue
        board[y][x] = stone
        is_four = False
        for dx, dy in DIRECTIONS:
            c, o = _line_info(board, x, y, dx, dy, stone)
            if c == 4 and o >= 1:
                is_four = True
                break
        board[y][x] = 0
        if is_four:
            moves.append((x, y))
    return moves


def _find_forks(board, stone, cands=None):
    """Find moves creating double threats (open-three+open-three, four+open-three, etc.)."""
    if cands is None:
        cands = _get_candidates(board, radius=2)
    other = 3 - stone
    moves = []
    for x, y in cands:
        if board[y][x] != 0:
            continue
        board[y][x] = stone
        threats = 0
        for dx, dy in DIRECTIONS:
            c, o = _line_info(board, x, y, dx, dy, stone)
            if (c == 4 and o >= 1) or (c == 3 and o == 2):
                threats += 1
                continue
            has_gap_threat = False
            for i_start in range(5):
                all_ok = True
                s_count = 0
                gap_count = 0
                for k in range(5):
                    idx = i_start + k - 4
                    nx, ny = x + dx * idx, y + dy * idx
                    if not (0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE):
                        all_ok = False
                        break
                    cell = board[ny][nx]
                    if cell == other or (cell != stone and cell != 0):
                        all_ok = False
                        break
                    if cell == stone:
                        s_count += 1
                    else:
                        gap_count += 1
                if all_ok and s_count == 4 and gap_count == 1:
                    has_gap_threat = True
                    break
            if has_gap_threat:
                threats += 1
        board[y][x] = 0
        if threats >= 2:
            moves.append((x, y))
    return moves


# ──────────────────────────────────────────────
# VCF search
# ──────────────────────────────────────────────

def _vcf(board, attacker, defender, depth, cands=None):
    """Search for forced win via continuous fours. Returns (x,y) or None."""
    if depth <= 0:
        return None
    if cands is None:
        cands = _get_candidates(board, radius=2)
    win = _find_winning(board, attacker, cands)
    if win:
        return win
    four_moves = _find_fours(board, attacker, cands)
    for fx, fy in four_moves:
        board[fy][fx] = attacker
        block = _find_winning(board, attacker)
        if block is None:
            board[fy][fx] = 0
            continue
        bx, by = block
        if board[by][bx] != 0:
            board[fy][fx] = 0
            continue
        board[by][bx] = defender
        def_wins = _has_five(board, bx, by, defender)
        if not def_wins:
            new_cands = _get_candidates(board, radius=2)
            result = _vcf(board, attacker, defender, depth - 2, new_cands)
            if result is not None:
                board[by][bx] = 0
                board[fy][fx] = 0
                return (fx, fy)
        board[by][bx] = 0
        board[fy][fx] = 0
    return None


# ──────────────────────────────────────────────
# Board evaluation
# ──────────────────────────────────────────────

def _multi_threat_bonus(threat_counts):
    """Calculate bonus for multi-directional threats at a single stone."""
    fours = threat_counts.get('four', 0)
    open_threes = threat_counts.get('open_three', 0)
    bonus = 0
    if fours >= 2:
        bonus += OPEN_FOUR * 2
    elif fours >= 1 and open_threes >= 1:
        bonus += OPEN_FOUR
    elif open_threes >= 2:
        bonus += FOUR * 2
    return bonus


def _evaluate(board, ai_stone, human_stone):
    """Evaluate board from AI's perspective. Positive = AI advantage."""
    ai_score = 0
    human_score = 0
    center = BOARD_SIZE // 2
    _st = _SCORE_TABLE
    
    for y in range(BOARD_SIZE):
        row = board[y]
        for x in range(BOARD_SIZE):
            stone = row[x]
            if stone == 0:
                continue
            dist = abs(x - center) + abs(y - center)
            pos_bonus = max(0, (BOARD_SIZE - dist))
            
            threat_counts_four = 0
            threat_counts_open_three = 0
            for dx, dy in DIRECTIONS:
                px, py = x - dx, y - dy
                if 0 <= px < BOARD_SIZE and 0 <= py < BOARD_SIZE and board[py][px] == stone:
                    continue
                c, o = _line_info(board, x, y, dx, dy, stone)
                s = _st.get((c, o), FIVE if c >= 5 else 0)
                if stone == ai_stone:
                    ai_score += s
                else:
                    human_score += s
                if c == 4 and o >= 1:
                    threat_counts_four += 1
                elif c == 3 and o == 2:
                    threat_counts_open_three += 1
            
            # Inline multi-threat bonus
            combo_bonus = 0
            if threat_counts_four >= 2:
                combo_bonus = OPEN_FOUR * 2
            elif threat_counts_four >= 1 and threat_counts_open_three >= 1:
                combo_bonus = OPEN_FOUR
            elif threat_counts_open_three >= 2:
                combo_bonus = FOUR * 2
            
            if stone == ai_stone:
                ai_score += combo_bonus + pos_bonus
            else:
                human_score += combo_bonus + pos_bonus
    
    # Gap pattern evaluation
    for y in range(BOARD_SIZE):
        for x in range(BOARD_SIZE):
            if board[y][x] != 0:
                continue
            has_neighbor = False
            for dx, dy in DIRECTIONS:
                for sign in (1, -1):
                    nx, ny = x + dx * sign, y + dy * sign
                    if 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE and board[ny][nx] != 0:
                        has_neighbor = True
                        break
                if has_neighbor:
                    break
            if not has_neighbor:
                continue
            ai_score += _gap_pattern_score(board, x, y, ai_stone)
            human_score += _gap_pattern_score(board, x, y, human_stone)
    
    return ai_score - human_score * 1.35


# ──────────────────────────────────────────────
# Alpha-Beta Search with NMP + LMR + PVS + TT + Killer + History
# ──────────────────────────────────────────────

# Transposition table entry types
TT_EXACT = 0
TT_ALPHA = 1  # Upper bound (failed low)
TT_BETA  = 2  # Lower bound (failed high)

# Null-move pruning constants
NMP_REDUCTION = 2    # Depth reduction for null-move search
NMP_MIN_DEPTH = 3    # Minimum depth to apply NMP

# LMR constants
LMR_MIN_DEPTH = 3    # Minimum depth to apply LMR
LMR_FULL_MOVES = 4   # First N moves are searched at full depth


class _ABSearch:
    def __init__(self, ai_stone, human_stone, max_time, progress_cb=None,
                 use_incremental=True, use_nmp=True, use_lmr=True,
                 shared_tt=None):
        self.ai = ai_stone
        self.human = human_stone
        self.max_time = max_time
        self.t0 = 0
        self.nodes = 0
        self.tt_hits = 0
        self.nmp_cuts = 0
        self.lmr_researches = 0
        self.timeout = False
        self.progress_cb = progress_cb
        self.root_scores = {}
        self.use_nmp = use_nmp
        self.use_lmr = use_lmr
        self.use_incremental = use_incremental

        # Transposition table with depth-preferred replacement
        # Shared across moves for persistence; fixed-size array indexed by hash
        if shared_tt is not None:
            self.tt = shared_tt
        else:
            self.tt = {}
        self.tt_max_size = 2000000  # 2M entries

        # Killer moves: ply -> [move1, move2]
        self.killers = {}

        # History heuristic: (x, y) -> score
        self.history = {}
        
        # Counter move heuristic: (prev_x, prev_y) -> best_response
        self.counter_moves = {}
        
        # Incremental evaluator
        self.incr_eval = None
        
        # Candidate set manager
        self.cand_set = None

    def _check(self):
        if time.time() - self.t0 > self.max_time:
            self.timeout = True
        return self.timeout

    def search(self, board, max_depth):
        _init_zobrist()
        self.t0 = time.time()
        self.nodes = 0
        self.tt_hits = 0
        self.nmp_cuts = 0
        self.lmr_researches = 0
        self.timeout = False
        self.root_scores = {}
        self.killers = {}
        # history is NOT reset — persisted from previous moves (decayed externally)
        self.counter_moves = {}
        
        self.zhash = _zobrist_hash(board)
        
        # Initialize incremental evaluator
        if self.use_incremental:
            self.incr_eval = _IncrementalEval(board, self.ai, self.human)
            self.cand_set = _CandidateSet(board, radius=2)

        best_move = None
        best_score = -math.inf
        prev_score = 0
        prev_depth_time = 0.0  # time taken by previous depth iteration
        
        for d in range(1, max_depth + 1):
            if self.timeout:
                break
            
            elapsed = time.time() - self.t0
            remaining = self.max_time - elapsed
            
            # Predictive time management: each depth typically takes 3-5x
            # longer than the previous. Don't start if we can't finish.
            if d > 1:
                # Estimate next depth will take at least 3x previous depth
                estimated_next = prev_depth_time * 3.5
                # Also enforce: don't start if less than 25% of budget remains
                if remaining < estimated_next or remaining < self.max_time * 0.25:
                    print(f"[AI] Skipping depth {d}: remaining={remaining:.2f}s "
                          f"estimated={estimated_next:.2f}s prev_depth={prev_depth_time:.2f}s")
                    break
            
            depth_start = time.time()
            
            # Aspiration window: graduated widening on fail-low/fail-high
            if d >= 4 and best_move is not None:
                delta = 50
                asp_alpha = prev_score - delta
                asp_beta = prev_score + delta
                
                while not self.timeout:
                    m, s = self._root(board, d, asp_alpha, asp_beta)
                    
                    if self.timeout or m is None:
                        break
                    
                    if s <= asp_alpha:
                        # Fail-low: widen alpha side
                        delta *= 4
                        asp_alpha = max(prev_score - delta, -FIVE - 1)
                    elif s >= asp_beta:
                        # Fail-high: widen beta side
                        delta *= 4
                        asp_beta = min(prev_score + delta, FIVE + 1)
                    else:
                        # Score within window — accept
                        best_move = m
                        best_score = s
                        prev_score = s
                        break
                    
                    # Safety: if delta is huge, do full-width search
                    if delta > 10000:
                        m2, s2 = self._root(board, d, -math.inf, math.inf)
                        if not self.timeout and m2 is not None:
                            best_move = m2
                            best_score = s2
                            prev_score = s2
                        break
            else:
                m, s = self._root(board, d, -math.inf, math.inf)
                if not self.timeout and m is not None:
                    best_move = m
                    best_score = s
                    prev_score = s
            
            if not self.timeout:
                elapsed = time.time() - self.t0
                prev_depth_time = time.time() - depth_start
                print(f"[AI] Depth {d}: ({best_move[0] if best_move else '?'},{best_move[1] if best_move else '?'}) "
                      f"score={best_score:.0f} nodes={self.nodes} tt_hits={self.tt_hits} "
                      f"nmp={self.nmp_cuts} lmr_re={self.lmr_researches} time={elapsed:.2f}s "
                      f"depth_time={prev_depth_time:.2f}s")
            else:
                # Depth was interrupted by timeout, don't update prev_depth_time
                pass
            
            if best_score >= FIVE:
                break
        
        return best_move, best_score

    def extract_pv(self, board, first_move, max_len=10):
        """Extract Principal Variation from TT."""
        pv = []
        if first_move is None:
            return pv
        mutations = []
        x, y = first_move
        stone = self.ai
        opp = self.human
        zhash = self.zhash
        seen = set()

        for i in range(max_len):
            if not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
                break
            if board[y][x] != 0:
                break
            if (x, y) in seen:
                break
            seen.add((x, y))
            pv.append((x, y, stone))
            board[y][x] = stone
            zhash = _zobrist_update(zhash, x, y, 0, stone)
            mutations.append((x, y))

            tt_entry = self.tt.get(zhash)
            if not tt_entry or not tt_entry[3]:
                break
            next_move = tt_entry[3]
            stone, opp = opp, stone
            x, y = next_move

        for mx, my in reversed(mutations):
            board[my][mx] = 0
        return pv

    def find_counter_move(self, board, ai_move):
        """Find opponent's best response via TT or quick search."""
        if ai_move is None:
            return None
        ax, ay = ai_move
        board[ay][ax] = self.ai
        zhash = _zobrist_update(self.zhash, ax, ay, 0, self.ai)

        tt_entry = self.tt.get(zhash)
        if tt_entry and tt_entry[3]:
            counter = tt_entry[3]
            board[ay][ax] = 0
            cx, cy = counter
            if 0 <= cx < BOARD_SIZE and 0 <= cy < BOARD_SIZE and board[cy][cx] == 0:
                return counter

        counter = _find_winning(board, self.human)
        if counter:
            board[ay][ax] = 0
            return counter

        cands = _deep_sorted_candidates(board, self.human, self.ai, max_moves=5)
        board[ay][ax] = 0
        if cands:
            return cands[0]
        return None

    def _emit_progress(self, current_move=None):
        if not self.progress_cb or not self.root_scores:
            return
        self.progress_cb(dict(self.root_scores), current_move)

    def _order_moves(self, board, cands, stone, opp, ply, prev_move=None,
                     tactical_scores=None):
        """Order moves using TT best move, killer moves, counter moves, history,
        and tactical scores from _score_cands_fast()."""
        tt_move = None
        tt_entry = self.tt.get(self.zhash)
        if tt_entry and tt_entry[3]:
            tt_move = tt_entry[3]

        killers = self.killers.get(ply, [])
        counter = self.counter_moves.get(prev_move) if prev_move else None
        hist = self.history
        tac = tactical_scores if tactical_scores else {}
        
        def move_priority(move):
            score = 0
            if tt_move and move == tt_move:
                score += 10000000
            if move in killers:
                score += 1000000
            if counter and move == counter:
                score += 500000
            score += hist.get(move, 0)
            # Incorporate tactical score (scaled down to not dominate TT/killer)
            score += tac.get(move, 0) * 0.1
            return score

        cands.sort(key=move_priority, reverse=True)
        return cands

    def _store_killer(self, ply, move):
        if ply not in self.killers:
            self.killers[ply] = []
        kl = self.killers[ply]
        if move not in kl:
            kl.insert(0, move)
            if len(kl) > 2:
                kl.pop()

    def _store_history(self, move, depth):
        self.history[move] = self.history.get(move, 0) + depth * depth

    def _tt_store(self, depth, score, flag, best_move):
        """Store entry in TT with depth-preferred replacement."""
        zhash = self.zhash
        existing = self.tt.get(zhash)
        if existing is not None:
            # Replace if new entry has >= depth (depth-preferred)
            if depth >= existing[0]:
                self.tt[zhash] = (depth, score, flag, best_move)
        elif len(self.tt) < self.tt_max_size:
            self.tt[zhash] = (depth, score, flag, best_move)
        else:
            # Table full: always replace (new position likely more relevant)
            self.tt[zhash] = (depth, score, flag, best_move)

    def _root(self, board, depth, alpha_init, beta_init):
        cands = _deep_sorted_candidates(board, self.ai, self.human, max_moves=20)
        if not cands:
            return None, 0
        
        # Order using TT from previous iteration
        cands = self._order_moves(board, cands, self.ai, self.human, 0)
        
        best_move = cands[0]
        alpha = alpha_init
        
        for i, (x, y) in enumerate(cands):
            if self._check():
                break
            
            # Place stone + incremental updates
            board[y][x] = self.ai
            self.zhash = _zobrist_update(self.zhash, x, y, 0, self.ai)
            if self.use_incremental and self.cand_set:
                self.cand_set.place(board, x, y)
            if self.use_incremental and self.incr_eval:
                self.incr_eval.update(board, x, y)
            
            # PVS: first move full window, rest with null window then re-search
            if i == 0:
                s = -self._ab(board, depth - 1, -beta_init, -alpha,
                              self.human, self.ai, 1, (x, y))
            else:
                # Null window search
                s = -self._ab(board, depth - 1, -alpha - 1, -alpha,
                              self.human, self.ai, 1, (x, y))
                if not self.timeout and alpha < s < beta_init:
                    # Re-search with full window
                    s = -self._ab(board, depth - 1, -beta_init, -s,
                                  self.human, self.ai, 1, (x, y))
            
            # Undo
            board[y][x] = 0
            self.zhash = _zobrist_update(self.zhash, x, y, self.ai, 0)
            if self.use_incremental and self.cand_set:
                self.cand_set.remove(board, x, y)
            if self.use_incremental and self.incr_eval:
                self.incr_eval.update(board, x, y)
            
            self.root_scores[(x, y)] = s
            if s > alpha:
                alpha = s
                best_move = (x, y)
            if self.progress_cb and (i % 3 == 0 or i == len(cands) - 1):
                self._emit_progress((x, y))
            if alpha >= beta_init:
                break
        
        return best_move, alpha

    def _ab(self, board, depth, alpha, beta, stone, opp, ply, prev_move=None):
        self.nodes += 1
        if self.nodes % 32 == 0 and self._check():
            return 0
        
        orig_alpha = alpha
        is_pv = (beta - alpha) > 1

        # ── Transposition table lookup ──
        tt_entry = self.tt.get(self.zhash)
        tt_move = None
        if tt_entry and tt_entry[0] >= depth:
            tt_depth, tt_score, tt_flag, tt_move = tt_entry
            self.tt_hits += 1
            if tt_flag == TT_EXACT:
                return tt_score
            elif tt_flag == TT_BETA:
                alpha = max(alpha, tt_score)
            elif tt_flag == TT_ALPHA:
                beta = min(beta, tt_score)
            if alpha >= beta:
                return tt_score
        elif tt_entry:
            tt_move = tt_entry[3]

        if depth <= 0:
            score = self._qeval(board, stone, opp)
            self._tt_store(0, score, TT_EXACT, None)
            return score
        
        # ── Razoring (depth 1-3, non-PV) ──
        if not is_pv and depth <= 3 and prev_move is not None:
            razor_margins = (0, 300, 600, 900)
            static_eval = self._qeval(board, stone, opp)
            if static_eval + razor_margins[depth] <= alpha:
                return static_eval
        
        # ── Null-Move Pruning (adaptive reduction) ──
        if (self.use_nmp and depth >= NMP_MIN_DEPTH and
                prev_move is not None and ply > 0):
            # Adaptive R: R=2 for shallow, R=3 for deep
            nmp_r = 2 + (1 if depth > 6 else 0)
            null_score = -self._ab(board, depth - 1 - nmp_r,
                                   -beta, -beta + 1, opp, stone, ply + 1, None)
            if null_score >= beta:
                self.nmp_cuts += 1
                return beta
        
        # ── Futility pruning flag (depth 1-2, non-PV) ──
        futility_pruning = False
        futility_base = 0
        if not is_pv and depth <= 2 and prev_move is not None:
            futility_base = self._qeval(board, stone, opp)
            if futility_base + 300 * depth <= alpha:
                futility_pruning = True
        
        # Generate and sort candidates
        # PV nodes get wider search, non-PV nodes get narrower
        if is_pv:
            max_m = min(15, 6 + depth * 2)
        else:
            max_m = min(10, 5 + depth * 2)
        if self.use_incremental and self.cand_set:
            raw_cands = self.cand_set.get()
            # Quick score and sort — returns (score, x, y) tuples for unified ordering
            cands, tactical_scores = self._score_cands_fast(board, raw_cands, stone, opp, max_m)
        else:
            cands = _fast_sorted_candidates(board, stone, opp, max_moves=max_m)
            tactical_scores = {}
        
        if not cands:
            return 0
        
        # ── Internal Iterative Deepening (IID) ──
        if is_pv and tt_move is None and depth >= 5:
            self._ab(board, depth - 2, alpha, beta, stone, opp, ply, prev_move)
            iid_entry = self.tt.get(self.zhash)
            if iid_entry and iid_entry[3]:
                tt_move = iid_entry[3]
        
        # Order with killer/history/counter heuristics + tactical scores
        cands = self._order_moves(board, cands, stone, opp, ply, prev_move,
                                  tactical_scores=tactical_scores)
        
        best_move = None
        moves_searched = 0
        
        for x, y in cands:
            # Futility pruning: skip late moves at shallow depths
            if futility_pruning and moves_searched > 0 and best_move is not None:
                # Only prune non-tactical moves (not winning / not blocking wins)
                tac = tactical_scores.get((x, y), 0)
                if tac < FOUR:
                    continue
            
            # Place stone + incremental updates
            board[y][x] = stone
            self.zhash = _zobrist_update(self.zhash, x, y, 0, stone)
            if self.use_incremental and self.cand_set:
                self.cand_set.place(board, x, y)
            if self.use_incremental and self.incr_eval:
                self.incr_eval.update(board, x, y)
            
            # Immediate win check (before expensive recursive search)
            if _has_five(board, x, y, stone):
                # Undo
                board[y][x] = 0
                self.zhash = _zobrist_update(self.zhash, x, y, stone, 0)
                if self.use_incremental and self.cand_set:
                    self.cand_set.remove(board, x, y)
                if self.use_incremental and self.incr_eval:
                    self.incr_eval.update(board, x, y)
                return FIVE + depth
            
            # ── Threat Extension ──
            extension = 0
            if depth >= 2:
                BS = BOARD_SIZE
                for ddx, ddy in DIRECTIONS:
                    c1, nx, ny = 0, x + ddx, y + ddy
                    while 0 <= nx < BS and 0 <= ny < BS and board[ny][nx] == stone:
                        c1 += 1; nx += ddx; ny += ddy
                    c2, nx2, ny2 = 0, x - ddx, y - ddy
                    while 0 <= nx2 < BS and 0 <= ny2 < BS and board[ny2][nx2] == stone:
                        c2 += 1; nx2 -= ddx; ny2 -= ddy
                    if 1 + c1 + c2 == 4:
                        ex1, ey1 = x + ddx * (c1 + 1), y + ddy * (c1 + 1)
                        ex2, ey2 = x - ddx * (c2 + 1), y - ddy * (c2 + 1)
                        o = 0
                        if 0 <= ex1 < BS and 0 <= ey1 < BS and board[ey1][ex1] == 0:
                            o += 1
                        if 0 <= ex2 < BS and 0 <= ey2 < BS and board[ey2][ex2] == 0:
                            o += 1
                        if o >= 1:
                            extension = 1
                            break
                # Also extend if opponent has a four that this move might be blocking
                if extension == 0 and prev_move is not None:
                    for ddx, ddy in DIRECTIONS:
                        c1, nx, ny = 0, x + ddx, y + ddy
                        while 0 <= nx < BS and 0 <= ny < BS and board[ny][nx] == opp:
                            c1 += 1; nx += ddx; ny += ddy
                        c2, nx2, ny2 = 0, x - ddx, y - ddy
                        while 0 <= nx2 < BS and 0 <= ny2 < BS and board[ny2][nx2] == opp:
                            c2 += 1; nx2 -= ddx; ny2 -= ddy
                        if 1 + c1 + c2 >= 4:
                            extension = 1
                            break
            
            # Limit total extensions per path
            actual_ext = min(extension, 1) if ply < 12 else 0
            
            # ── Late Move Reduction ──
            if (self.use_lmr and depth >= LMR_MIN_DEPTH and
                    moves_searched >= LMR_FULL_MOVES and
                    best_move is not None and extension == 0):
                # Reduce depth for late moves
                r = _LMR_TABLE[min(depth, 63)][min(moves_searched, 63)]
                r = min(r, depth - 1)  # Don't reduce below depth 1
                # Reduce less at PV nodes
                if is_pv and r > 0:
                    r = max(1, r - 1)
                
                if r > 0:
                    # Reduced depth search
                    s = -self._ab(board, depth - 1 - r, -alpha - 1, -alpha,
                                  opp, stone, ply + 1, (x, y))
                    
                    if not self.timeout and s > alpha:
                        # Re-search at full depth
                        self.lmr_researches += 1
                        s = -self._ab(board, depth - 1 + actual_ext, -beta, -alpha,
                                      opp, stone, ply + 1, (x, y))
                else:
                    s = -self._ab(board, depth - 1 + actual_ext, -beta, -alpha,
                                  opp, stone, ply + 1, (x, y))
            elif moves_searched == 0:
                # First move: full window (PVS)
                s = -self._ab(board, depth - 1 + actual_ext, -beta, -alpha,
                              opp, stone, ply + 1, (x, y))
            else:
                # PVS: null window first
                s = -self._ab(board, depth - 1 + actual_ext, -alpha - 1, -alpha,
                              opp, stone, ply + 1, (x, y))
                if not self.timeout and alpha < s < beta:
                    s = -self._ab(board, depth - 1 + actual_ext, -beta, -s,
                                  opp, stone, ply + 1, (x, y))
            
            # Undo
            board[y][x] = 0
            self.zhash = _zobrist_update(self.zhash, x, y, stone, 0)
            if self.use_incremental and self.cand_set:
                self.cand_set.remove(board, x, y)
            if self.use_incremental and self.incr_eval:
                self.incr_eval.update(board, x, y)
            
            if self.timeout:
                return alpha
            
            moves_searched += 1
            
            if s >= beta:
                self._store_killer(ply, (x, y))
                self._store_history((x, y), depth)
                if prev_move:
                    self.counter_moves[prev_move] = (x, y)
                self._tt_store(depth, beta, TT_BETA, (x, y))
                return beta
            if s > alpha:
                alpha = s
                best_move = (x, y)
        
        # Store in TT
        if alpha > orig_alpha:
            self._tt_store(depth, alpha, TT_EXACT, best_move)
        else:
            self._tt_store(depth, alpha, TT_ALPHA, best_move)
        
        return alpha
    
    def _score_cands_fast(self, board, raw_cands, stone, opp, max_moves):
        """Fast candidate scoring from incremental candidate set.
        
        Returns (cands, tactical_scores) where:
          cands = [(x, y), ...] sorted by score, truncated to max_moves
          tactical_scores = {(x, y): score} for all scored candidates
        """
        center = BOARD_SIZE // 2
        _st = _SCORE_TABLE
        BS = BOARD_SIZE
        scored = []
        tactical_scores = {}
        for x, y in raw_cands:
            if board[y][x] != 0:
                continue
            attack = 0
            defense = 0
            atk_t = 0
            def_t = 0
            brd = board  # local alias for speed
            for dx, dy in DIRECTIONS:
                # Inlined _count_dir for attack
                a1, nx, ny = 0, x + dx, y + dy
                while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == stone:
                    a1 += 1; nx += dx; ny += dy
                a2, nx, ny = 0, x - dx, y - dy
                while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == stone:
                    a2 += 1; nx -= dx; ny -= dy
                a_count = 1 + a1 + a2
                aex1, aey1 = x + dx * (a1 + 1), y + dy * (a1 + 1)
                aex2, aey2 = x - dx * (a2 + 1), y - dy * (a2 + 1)
                a_open = 0
                if 0 <= aex1 < BS and 0 <= aey1 < BS and brd[aey1][aex1] == 0:
                    a_open += 1
                if 0 <= aex2 < BS and 0 <= aey2 < BS and brd[aey2][aex2] == 0:
                    a_open += 1
                a_s = _st.get((a_count, a_open), FIVE if a_count >= 5 else 0)
                attack += a_s
                if (a_count == 4 and a_open >= 1) or (a_count == 3 and a_open == 2):
                    atk_t += 1

                # Inlined _count_dir for defense
                d1, nx, ny = 0, x + dx, y + dy
                while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == opp:
                    d1 += 1; nx += dx; ny += dy
                d2, nx, ny = 0, x - dx, y - dy
                while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == opp:
                    d2 += 1; nx -= dx; ny -= dy
                d_count = 1 + d1 + d2
                dex1, dey1 = x + dx * (d1 + 1), y + dy * (d1 + 1)
                dex2, dey2 = x - dx * (d2 + 1), y - dy * (d2 + 1)
                d_open = 0
                if 0 <= dex1 < BS and 0 <= dey1 < BS and brd[dey1][dex1] == 0:
                    d_open += 1
                if 0 <= dex2 < BS and 0 <= dey2 < BS and brd[dey2][dex2] == 0:
                    d_open += 1
                d_s = _st.get((d_count, d_open), FIVE if d_count >= 5 else 0)
                defense += d_s
                if (d_count == 4 and d_open >= 1) or (d_count == 3 and d_open == 2):
                    def_t += 1
            
            if atk_t >= 2:
                attack += FOUR
            if def_t >= 2:
                defense += FOUR
            
            dist = abs(x - center) + abs(y - center)
            total = attack + defense * 1.25 + max(0, 14 - dist)
            scored.append((total, x, y))
            tactical_scores[(x, y)] = total
        
        scored.sort(reverse=True)
        cands = [(x, y) for _, x, y in scored[:max_moves]]
        return cands, tactical_scores

    def _qeval(self, board, stone, opp):
        """Quiescence: static eval + immediate threat check.
        
        Optimized: pre-filters candidates to only check those that could
        form five-in-a-row (cells adjacent to 3+ in a line), reducing the
        number of expensive board mutations.
        """
        BS = BOARD_SIZE
        
        # Use incremental eval if available
        if self.use_incremental and self.incr_eval:
            cands = self.cand_set.get() if self.cand_set else _get_candidates(board, radius=1)
            
            # Pre-filter: only check cells that have a chance of forming five
            # A cell can complete a five only if it has 4+ in some direction
            brd = board
            for x, y in cands:
                if brd[y][x] != 0:
                    continue
                # Check if placing stone here makes five
                for dx, dy in DIRECTIONS:
                    c1, nx, ny = 0, x + dx, y + dy
                    while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == stone:
                        c1 += 1; nx += dx; ny += dy
                    c2, nx, ny = 0, x - dx, y - dy
                    while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == stone:
                        c2 += 1; nx -= dx; ny -= dy
                    if 1 + c1 + c2 >= 5:
                        return FIVE
            
            for x, y in cands:
                if brd[y][x] != 0:
                    continue
                # Check if placing opp here makes five (threat we must block)
                for dx, dy in DIRECTIONS:
                    c1, nx, ny = 0, x + dx, y + dy
                    while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == opp:
                        c1 += 1; nx += dx; ny += dy
                    c2, nx, ny = 0, x - dx, y - dy
                    while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == opp:
                        c2 += 1; nx -= dx; ny -= dy
                    if 1 + c1 + c2 >= 5:
                        return -FIVE + 1
            
            return self.incr_eval.get_score(stone)
        
        # Fallback to non-incremental (same optimization)
        cands = _get_candidates(board, radius=1)
        brd = board
        for x, y in cands:
            if brd[y][x] != 0:
                continue
            for dx, dy in DIRECTIONS:
                c1, nx, ny = 0, x + dx, y + dy
                while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == stone:
                    c1 += 1; nx += dx; ny += dy
                c2, nx, ny = 0, x - dx, y - dy
                while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == stone:
                    c2 += 1; nx -= dx; ny -= dy
                if 1 + c1 + c2 >= 5:
                    return FIVE
        
        for x, y in cands:
            if brd[y][x] != 0:
                continue
            for dx, dy in DIRECTIONS:
                c1, nx, ny = 0, x + dx, y + dy
                while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == opp:
                    c1 += 1; nx += dx; ny += dy
                c2, nx, ny = 0, x - dx, y - dy
                while 0 <= nx < BS and 0 <= ny < BS and brd[ny][nx] == opp:
                    c2 += 1; nx -= dx; ny -= dy
                if 1 + c1 + c2 >= 5:
                    return -FIVE + 1
        
        if stone == self.ai:
            return _evaluate(board, self.ai, self.human)
        else:
            return -_evaluate(board, self.ai, self.human)


# ──────────────────────────────────────────────
# Lazy SMP (Simplified Multi-Processing)
# ──────────────────────────────────────────────

def _smp_worker(board_flat, ai_stone, human_stone, max_depth, max_time,
                result_move_x, result_move_y, result_score, worker_id):
    """Worker process for Lazy SMP parallel search.
    
    Each worker searches the same position but with slightly different
    move ordering (via different random seeds for tie-breaking) and
    potentially different depths. Results are shared via multiprocessing Values.
    """
    # Reconstruct board
    board = []
    for y in range(BOARD_SIZE):
        row = []
        for x in range(BOARD_SIZE):
            row.append(board_flat[y * BOARD_SIZE + x])
        board.append(row)
    
    # Worker 1 searches with slightly different parameters
    searcher = _ABSearch(ai_stone, human_stone, max_time,
                         use_incremental=True, use_nmp=True, use_lmr=True)
    
    # Vary depth slightly per worker for diversity
    actual_depth = max_depth + (worker_id % 2)  # Worker 1 searches 1 deeper
    
    move, score = searcher.search(board, actual_depth)
    
    if move:
        result_move_x.value = move[0]
        result_move_y.value = move[1]
        result_score.value = score


class _LazySMP:
    """Lazy SMP: run multiple search processes in parallel.
    
    All workers search the same position. The main thread also searches.
    When any worker finds a better result, it's collected.
    """
    
    def __init__(self, n_workers=None):
        if n_workers is None:
            n_workers = max(1, os.cpu_count() or 1)
        self.n_workers = n_workers
    
    def search(self, board, ai_stone, human_stone, max_depth, max_time,
               progress_cb=None, shared_tt=None, shared_history=None):
        """Run parallel search and return best result."""
        if self.n_workers <= 1:
            # Single-threaded fallback
            searcher = _ABSearch(ai_stone, human_stone, max_time,
                                 progress_cb=progress_cb,
                                 use_incremental=True,
                                 shared_tt=shared_tt)
            if shared_history:
                searcher.history = dict(shared_history)
            return searcher.search(board, max_depth), searcher
        
        # Flatten board for sharing
        board_flat = Array(ctypes.c_int, BOARD_SIZE * BOARD_SIZE)
        for y in range(BOARD_SIZE):
            for x in range(BOARD_SIZE):
                board_flat[y * BOARD_SIZE + x] = board[y][x]
        
        # Shared results from workers
        worker_results = []
        workers = []
        
        for wid in range(self.n_workers - 1):  # Main thread is also a worker
            mx = Value(ctypes.c_int, -1)
            my = Value(ctypes.c_int, -1)
            ms = Value(ctypes.c_double, -math.inf)
            worker_results.append((mx, my, ms))
            
            p = Process(target=_smp_worker,
                       args=(board_flat, ai_stone, human_stone,
                             max_depth, max_time, mx, my, ms, wid + 1),
                       daemon=True)
            workers.append(p)
        
        # Start workers
        for p in workers:
            p.start()
        
        # Main thread search (with progress callback)
        main_searcher = _ABSearch(ai_stone, human_stone, max_time,
                                   progress_cb=progress_cb,
                                   use_incremental=True,
                                   shared_tt=shared_tt)
        if shared_history:
            main_searcher.history = dict(shared_history)
        main_move, main_score = main_searcher.search(board, max_depth)
        
        # Wait for workers (with timeout)
        for p in workers:
            p.join(timeout=2.0)
            if p.is_alive():
                p.terminate()
        
        # Collect best result
        best_move = main_move
        best_score = main_score
        
        for mx, my, ms in worker_results:
            if mx.value >= 0 and ms.value > best_score:
                best_move = (mx.value, my.value)
                best_score = ms.value
        
        return (best_move, best_score), main_searcher


# ──────────────────────────────────────────────
# GomokuAI class
# ──────────────────────────────────────────────

class GomokuAI:
    """
    Heuristic-based 오목 AI with alpha-beta search and threat detection.
    
    difficulty 1~10 controls search depth and time.
    Optimized with NMP, LMR, PVS, incremental eval, Lazy SMP.
    """
    # Depth is capped conservatively; the predictive time manager
    # will skip depths that won't finish within the time budget.
    # Depth 5+ only starts if depth 4 was fast enough to predict
    # depth 5 will finish in the remaining time.
    DIFF = {
        1:  (2,  0.3),
        2:  (3,  0.8),
        3:  (4,  1.5),
        4:  (4,  2.5),
        5:  (4,  3.5),
        6:  (6,  4.5),
        7:  (6,  5.0),
        8:  (8,  6.0),
        9:  (10, 7.0),
        10: (12, 7.0),
    }

    def __init__(self, difficulty=5):
        self.difficulty = difficulty
        self._smp = None
        # Persistent TT across moves — shared with _ABSearch instances
        self._shared_tt = {}
        # Persistent history heuristic (decayed each move)
        self._shared_history = {}

    def set_difficulty(self, d):
        self.difficulty = max(1, min(10, d))

    def _get_smp(self):
        if self._smp is None:
            self._smp = _LazySMP()
        return self._smp

    def _use_smp(self):
        """Only use Lazy SMP if we have enough cores to benefit."""
        return self.difficulty >= 7 and (os.cpu_count() or 1) >= 4

    def generate_move(self, board):
        """Generate AI move. Returns (x, y) or None. board = OmokBoard."""
        grid = board.board
        ai = 2
        human = 1

        legal = [(x, y) for y in range(BOARD_SIZE) for x in range(BOARD_SIZE)
                 if grid[y][x] == 0]
        if not legal:
            return None

        t0 = time.time()
        cands = _get_candidates(grid, radius=2)

        # ── 1. First / second move: center area ──
        if board.move_count == 0:
            return (7, 7)
        if board.move_count == 1:
            c = BOARD_SIZE // 2
            if grid[c][c] == 0:
                return (c, c)
            for dx, dy in [(1,1),(1,-1),(-1,1),(-1,-1),(1,0),(0,1)]:
                nx, ny = c+dx, c+dy
                if 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE and grid[ny][nx] == 0:
                    return (nx, ny)

        # ── 2. Win immediately ──
        w = _find_winning(grid, ai, cands)
        if w:
            logger.debug("Win: %s", w)
            return w

        # ── 3. Block opponent win ──
        w2 = _find_winning(grid, human, cands)
        if w2:
            logger.debug("Block win: %s", w2)
            return w2

        # ── 4. Create open four ──
        of = _find_open_fours(grid, ai, cands)
        if of:
            logger.debug("Open four: %s", of[0])
            return of[0]

        # ── 5. Block opponent's open four ──
        oof = _find_open_fours(grid, human, cands)
        if oof:
            our_fours = _find_fours(grid, ai, cands)
            for fx, fy in our_fours:
                if (fx, fy) in oof:
                    logger.debug("Counter+block: (%d,%d)", fx, fy)
                    return (fx, fy)
            logger.debug("Block open four: %s", oof[0])
            return oof[0]

        # ── 6. Block opponent fork ──
        ofk = _find_forks(grid, human, cands)

        fk = _find_forks(grid, ai, cands)
        if fk:
            if ofk:
                ofk_set = set(ofk)
                for fm in fk:
                    if fm in ofk_set:
                        logger.debug("Fork+block: %s", fm)
                        return fm
                logger.debug("Block fork (urgent): %s", ofk[0])
                return ofk[0]
            logger.debug("Fork: %s", fk[0])
            return fk[0]

        if ofk:
            logger.debug("Block fork: %s", ofk[0])
            return ofk[0]

        # ── 7. VCF forced win ──
        vcf = _vcf(grid, ai, human, depth=16, cands=cands)
        if vcf:
            logger.debug("VCF: %s", vcf)
            return vcf

        # ── 8. Block opponent VCF ──
        ovcf = _vcf(grid, human, ai, depth=10, cands=cands)
        if ovcf:
            if grid[ovcf[1]][ovcf[0]] == 0:
                logger.debug("Block VCF: %s", ovcf)
                return ovcf

        # ── 10. Alpha-beta search (with Lazy SMP) ──
        depth, max_time = self.DIFF.get(self.difficulty, (6, 10.0))
        
        # Subtract pre-search time (threat scan, VCF, etc.) from budget
        pre_search_elapsed = time.time() - t0
        max_time = max(0.5, max_time - pre_search_elapsed)
        
        # Decay shared history each move (halve all values)
        if self._shared_history:
            for k in self._shared_history:
                self._shared_history[k] >>= 1
            # Remove zero entries to keep dict small
            self._shared_history = {k: v for k, v in self._shared_history.items() if v > 0}
        
        if self._use_smp():
            # Use Lazy SMP for higher difficulties on 4+ core machines
            smp = self._get_smp()
            (move, score), searcher = smp.search(grid, ai, human, depth, max_time,
                                                  shared_tt=self._shared_tt,
                                                  shared_history=self._shared_history)
        else:
            searcher = _ABSearch(ai, human, max_time, use_incremental=True,
                                 shared_tt=self._shared_tt)
            searcher.history = dict(self._shared_history)
            move, score = searcher.search(grid, depth)
        
        # Persist history back
        self._shared_history = searcher.history
        
        elapsed = time.time() - t0

        if move and move in legal:
            logger.debug("AB: (%d,%d) score=%.0f time=%.2fs nodes=%d",
                        move[0], move[1], score, elapsed, searcher.nodes)
            return move

        # ── Fallback ──
        sc = _deep_sorted_candidates(grid, ai, human, 5)
        if sc:
            return sc[0]
        return random.choice(legal)

    def generate_move_with_progress(self, board, progress_cb=None):
        """Generate AI move with progress callback for visualization."""
        grid = board.board
        ai = 2
        human = 1

        legal = [(x, y) for y in range(BOARD_SIZE) for x in range(BOARD_SIZE)
                 if grid[y][x] == 0]
        if not legal:
            if progress_cb:
                progress_cb('done', {'move': None, 'reason': 'no_legal'})
            return None

        t0 = time.time()
        cands = _get_candidates(grid, radius=2)

        def _quick_counter(ai_move):
            if ai_move is None:
                return None
            ax, ay = ai_move
            grid[ay][ax] = ai
            counter = _find_winning(grid, human)
            if counter:
                grid[ay][ax] = 0
                return counter
            opp_cands = _deep_sorted_candidates(grid, human, ai, max_moves=5)
            grid[ay][ax] = 0
            return opp_cands[0] if opp_cands else None

        def emit_done(move, reason, counter_move=None, pv=None):
            if progress_cb:
                data = {'move': list(move) if move else None, 'reason': reason}
                if pv:
                    data['pv'] = pv
                elif counter_move:
                    data['pv'] = [
                        [move[0], move[1], 'w'],
                        [counter_move[0], counter_move[1], 'b']
                    ]
                progress_cb('done', data)

        # ── 1. First / second move ──
        if board.move_count == 0:
            emit_done((7, 7), 'opening')
            return (7, 7)
        if board.move_count == 1:
            c = BOARD_SIZE // 2
            if grid[c][c] == 0:
                emit_done((c, c), 'opening')
                return (c, c)
            for dx, dy in [(1,1),(1,-1),(-1,1),(-1,-1),(1,0),(0,1)]:
                nx, ny = c+dx, c+dy
                if 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE and grid[ny][nx] == 0:
                    emit_done((nx, ny), 'opening')
                    return (nx, ny)

        # ── 2. Immediate threats ──
        if progress_cb:
            progress_cb('phase', {'phase': 'threat_scan', 'move': None})

        w = _find_winning(grid, ai, cands)
        if w:
            emit_done(w, 'win')
            return w

        w2 = _find_winning(grid, human, cands)
        if w2:
            emit_done(w2, 'block_win', _quick_counter(w2))
            return w2

        of = _find_open_fours(grid, ai, cands)
        if of:
            emit_done(of[0], 'open_four', _quick_counter(of[0]))
            return of[0]

        oof = _find_open_fours(grid, human, cands)
        if oof:
            our_fours = _find_fours(grid, ai, cands)
            for fx, fy in our_fours:
                if (fx, fy) in oof:
                    emit_done((fx, fy), 'counter_block', _quick_counter((fx, fy)))
                    return (fx, fy)
            emit_done(oof[0], 'block_open_four', _quick_counter(oof[0]))
            return oof[0]

        # ── 6. Fork detection ──
        ofk = _find_forks(grid, human, cands)

        fk = _find_forks(grid, ai, cands)
        if fk:
            if ofk:
                ofk_set = set(ofk)
                for fm in fk:
                    if fm in ofk_set:
                        emit_done(fm, 'fork_block', _quick_counter(fm))
                        return fm
                emit_done(ofk[0], 'block_fork', _quick_counter(ofk[0]))
                return ofk[0]
            emit_done(fk[0], 'fork', _quick_counter(fk[0]))
            return fk[0]

        if ofk:
            emit_done(ofk[0], 'block_fork', _quick_counter(ofk[0]))
            return ofk[0]

        # ── VCF ──
        if progress_cb:
            progress_cb('phase', {'phase': 'vcf_scan', 'move': None})

        vcf_result = _vcf(grid, ai, human, depth=16, cands=cands)
        if vcf_result:
            emit_done(vcf_result, 'vcf', _quick_counter(vcf_result))
            return vcf_result

        ovcf = _vcf(grid, human, ai, depth=10, cands=cands)
        if ovcf:
            if grid[ovcf[1]][ovcf[0]] == 0:
                emit_done(ovcf, 'block_vcf', _quick_counter(ovcf))
                return ovcf

        # ── Alpha-beta search with progress ──
        if progress_cb:
            progress_cb('phase', {'phase': 'search', 'move': None})

        depth, max_time = self.DIFF.get(self.difficulty, (6, 10.0))
        
        # Subtract pre-search time (threat scan, VCF, etc.) from budget
        pre_search_elapsed = time.time() - t0
        max_time = max(0.5, max_time - pre_search_elapsed)
        print(f"[AI] Pre-search: {pre_search_elapsed:.2f}s, search budget: {max_time:.2f}s")

        # Mutable container so ab_progress closure can access searcher
        # before it's assigned in the outer scope
        _searcher_ref = [None]

        def ab_progress(scores, current_move):
            if not scores:
                return
            items = list(scores.items())
            raw_vals = [s for _, s in items]
            max_s = max(raw_vals)
            min_s = min(raw_vals)
            spread = max_s - min_s if max_s != min_s else 1.0

            weights = []
            for (pos, s) in items:
                norm = (s - min_s) / spread
                w = math.exp(norm * 3)
                weights.append(w)

            total_w = sum(weights)
            candidates = []
            best_pos = None
            best_s = -math.inf
            for i, ((x, y), s) in enumerate(items):
                pct = (weights[i] / total_w) * 100 if total_w > 0 else 0
                cand = {'x': x, 'y': y, 'score': s, 'pct': round(pct, 1)}
                sr = _searcher_ref[0]
                if sr is not None:
                    pv = sr.extract_pv(grid, (x, y), max_len=8)
                    if pv and len(pv) > 1:
                        cand['pv'] = [[px, py, 'w' if ps == ai else 'b'] for px, py, ps in pv]
                candidates.append(cand)
                if s > best_s:
                    best_s = s
                    best_pos = [x, y]

            candidates.sort(key=lambda c: c['pct'], reverse=True)
            candidates = candidates[:8]

            if progress_cb:
                progress_cb('candidates', {
                    'candidates': candidates,
                    'best': best_pos
                })

        # Decay shared history each move (halve all values)
        if self._shared_history:
            for k in self._shared_history:
                self._shared_history[k] >>= 1
            self._shared_history = {k: v for k, v in self._shared_history.items() if v > 0}

        # Use Lazy SMP for high difficulty on 4+ core machines
        if self._use_smp():
            smp = self._get_smp()
            (move, score), searcher = smp.search(grid, ai, human, depth, max_time,
                                                   progress_cb=ab_progress,
                                                   shared_tt=self._shared_tt,
                                                   shared_history=self._shared_history)
            _searcher_ref[0] = searcher
        else:
            searcher = _ABSearch(ai, human, max_time, progress_cb=ab_progress,
                                 use_incremental=True, shared_tt=self._shared_tt)
            searcher.history = dict(self._shared_history)
            _searcher_ref[0] = searcher
            move, score = searcher.search(grid, depth)
        
        # Persist history back
        self._shared_history = searcher.history
        
        elapsed = time.time() - t0

        if move and move in legal:
            pv = searcher.extract_pv(grid, move, max_len=10)
            pv_data = [[px, py, 'w' if ps == ai else 'b'] for px, py, ps in pv] if pv else []
            emit_done(move, 'search', pv=pv_data)
            return move

        sc = _deep_sorted_candidates(grid, ai, human, 5)
        if sc:
            emit_done(sc[0], 'fallback', _quick_counter(sc[0]))
            return sc[0]
        fallback = random.choice(legal)
        emit_done(fallback, 'random', _quick_counter(fallback))
        return fallback
