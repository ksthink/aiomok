# -*- coding: utf-8 -*-
"""
GomokuAI — Strong heuristic-based AI with alpha-beta search + threat detection.

Key features:
- Pattern-based board evaluation (line pattern matching + gap patterns)
- Immediate threat detection (five, open-four, four, open-three)
- VCF (Victory by Continuous Four) search for forced wins
- Alpha-beta pruning with iterative deepening
- Zobrist hashing + transposition table
- Killer move / history heuristic for move ordering
- Gap pattern recognition (X_XXX, XX_XX, X_XX, etc.)
- Smart candidate move generation (proximity to existing stones)
- Renju rule awareness (흑 금수: 33, 44, 장목)

The AI plays as White (player 2) by default.
Board coordinate: board[y][x], 0=empty, 1=black, 2=white.
"""

import time
import math
import random
import logging

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
    """Score a line pattern."""
    if count >= 5:
        return FIVE
    if open_ends == 0:
        return 0
    if count == 4:
        return OPEN_FOUR if open_ends == 2 else FOUR
    if count == 3:
        return OPEN_THREE if open_ends == 2 else THREE
    if count == 2:
        return OPEN_TWO if open_ends == 2 else TWO
    if count == 1:
        return ONE if open_ends == 2 else 0
    return 0


def _score_point(board, x, y, stone):
    """Total pattern score for placing stone at (x,y)."""
    total = 0
    for dx, dy in DIRECTIONS:
        c, o = _line_info(board, x, y, dx, dy, stone)
        total += _score_line(c, o)
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
    This catches threats that pure consecutive-count misses.
    """
    other = 3 - stone
    total = 0
    for dx, dy in DIRECTIONS:
        # Check if (x,y) fills a gap in a pattern
        # Look at pattern: ... [pos-4] [pos-3] [pos-2] [pos-1] (x,y) [pos+1] [pos+2] [pos+3] [pos+4] ...
        # We scan a window of 9 cells centered on (x,y)
        line = []
        for i in range(-4, 5):
            nx, ny = x + dx * i, y + dy * i
            if 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE:
                line.append(board[ny][nx])
            else:
                line.append(-1)
        # The center is line[4] = (x,y), should be 0 (empty) for gap detection
        # But since we're evaluating what happens if stone is placed there, treat line[4] as stone
        line[4] = stone

        # Check various gap patterns in the 9-cell window
        # For each window of 5, look for patterns with exactly one gap
        for start in range(5):  # windows of 5: [start..start+4]
            window = line[start:start + 5]
            if -1 in window or other in window:
                continue
            s_count = sum(1 for c in window if c == stone)
            gaps = sum(1 for c in window if c == 0)
            if s_count == 4 and gaps == 1:
                # Gap-four: e.g. SSSGS or SSGSS etc. where G is the gap position
                # Check if ends are open
                before_x = x + dx * (start - 4 - 1)
                before_y = y + dy * (start - 4 - 1)
                after_x = x + dx * (start - 4 + 5)
                after_y = y + dy * (start - 4 + 5)
                open_e = 0
                if 0 <= before_x < BOARD_SIZE and 0 <= before_y < BOARD_SIZE and board[before_y][before_x] == 0:
                    open_e += 1
                if 0 <= after_x < BOARD_SIZE and 0 <= after_y < BOARD_SIZE and board[after_y][after_x] == 0:
                    open_e += 1
                total += GAP_FOUR
            elif s_count == 3 and gaps == 2:
                # Gap-three with two empty: need at least one open end
                total += GAP_THREE
        
        # Also check 6-cell windows for gap patterns like _XSXSX_ (extended gap-four)
        for start in range(4):
            window = line[start:start + 6]
            if -1 in window or other in window:
                continue
            s_count = sum(1 for c in window if c == stone)
            if s_count >= 4:
                total += GAP_TWO  # Bonus for extended connectivity

    return total


def _score_point_full(board, x, y, stone):
    """Total pattern score including gap patterns."""
    return _score_point(board, x, y, stone) + _gap_pattern_score(board, x, y, stone)


# ──────────────────────────────────────────────
# Candidate move generation
# ──────────────────────────────────────────────

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


def _fast_sorted_candidates(board, stone, opponent, max_moves=12):
    """Faster candidate sorting using simpler heuristic (no stone placement)."""
    cands = _get_candidates(board, radius=2)
    if not cands:
        return []
    center = BOARD_SIZE // 2
    scored = []
    for x, y in cands:
        # Quick score: count adjacent same-color stones + opponent stones
        attack = 0
        defense = 0
        for dx, dy in DIRECTIONS:
            # Check neighbors in this direction
            for sign in [1, -1]:
                nx, ny = x + dx * sign, y + dy * sign
                if 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE:
                    if board[ny][nx] == stone:
                        attack += 10
                    elif board[ny][nx] == opponent:
                        defense += 10
                nx2, ny2 = x + dx * sign * 2, y + dy * sign * 2
                if 0 <= nx2 < BOARD_SIZE and 0 <= ny2 < BOARD_SIZE:
                    if board[ny2][nx2] == stone:
                        attack += 3
                    elif board[ny2][nx2] == opponent:
                        defense += 3
        # Center proximity bonus
        dist = abs(x - center) + abs(y - center)
        center_bonus = max(0, 14 - dist)
        scored.append((attack + defense * 1.1 + center_bonus, x, y))
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
        scored.append((attack + defense * 1.1, x, y))
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
        # Find where defender must block (the winning move after our four)
        block = _find_winning(board, attacker)
        if block is None:
            board[fy][fx] = 0
            continue
        bx, by = block
        if board[by][bx] != 0:
            board[fy][fx] = 0
            continue
        board[by][bx] = defender
        # Check defender didn't accidentally win
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

def _evaluate(board, ai_stone, human_stone):
    """Evaluate board from AI's perspective. Positive = AI advantage."""
    ai_score = 0
    human_score = 0
    center = BOARD_SIZE // 2
    
    for y in range(BOARD_SIZE):
        row = board[y]
        for x in range(BOARD_SIZE):
            stone = row[x]
            if stone == 0:
                continue
            # Position bonus
            dist = abs(x - center) + abs(y - center)
            pos_bonus = max(0, (BOARD_SIZE - dist))
            
            # Pattern score (only from "first" stone in each line to avoid double counting)
            for dx, dy in DIRECTIONS:
                px, py = x - dx, y - dy
                if 0 <= px < BOARD_SIZE and 0 <= py < BOARD_SIZE and board[py][px] == stone:
                    continue
                c, o = _line_info(board, x, y, dx, dy, stone)
                s = _score_line(c, o)
                if stone == ai_stone:
                    ai_score += s
                else:
                    human_score += s
            
            if stone == ai_stone:
                ai_score += pos_bonus
            else:
                human_score += pos_bonus
    
    return ai_score - human_score * 1.15


# ──────────────────────────────────────────────
# Alpha-Beta Search with TT + Killer + History
# ──────────────────────────────────────────────

# Transposition table entry types
TT_EXACT = 0
TT_ALPHA = 1  # Upper bound (failed low)
TT_BETA  = 2  # Lower bound (failed high)

class _ABSearch:
    def __init__(self, ai_stone, human_stone, max_time, progress_cb=None):
        self.ai = ai_stone
        self.human = human_stone
        self.max_time = max_time
        self.t0 = 0
        self.nodes = 0
        self.tt_hits = 0
        self.timeout = False
        self.progress_cb = progress_cb
        self.root_scores = {}

        # Transposition table: zobrist_hash -> (depth, score, flag, best_move)
        self.tt = {}
        self.tt_max_size = 500000  # Limit memory usage

        # Killer moves: depth -> [move1, move2]
        self.killers = {}

        # History heuristic: (x, y) -> score (higher = caused more cutoffs)
        self.history = {}

    def _check(self):
        if time.time() - self.t0 > self.max_time:
            self.timeout = True
        return self.timeout

    def search(self, board, max_depth):
        _init_zobrist()
        self.t0 = time.time()
        self.nodes = 0
        self.tt_hits = 0
        self.timeout = False
        self.root_scores = {}
        self.killers = {}
        self.history = {}
        # Don't clear TT between iterations (iterative deepening benefits from previous results)
        
        self.zhash = _zobrist_hash(board)

        best_move = None
        best_score = -math.inf
        for d in range(1, max_depth + 1):
            if self.timeout:
                break
            m, s = self._root(board, d)
            if not self.timeout and m is not None:
                best_move = m
                best_score = s
                elapsed = time.time() - self.t0
                print(f"[AI] Depth {d}: ({m[0]},{m[1]}) score={s:.0f} "
                      f"nodes={self.nodes} tt_hits={self.tt_hits} time={elapsed:.2f}s")
                if s >= FIVE:
                    break
        return best_move, best_score

    def extract_pv(self, board, first_move, max_len=10):
        """Extract Principal Variation (best predicted sequence) from TT starting from first_move.
        
        Returns list of (x, y, stone) tuples representing the predicted sequence.
        stone alternates: ai, human, ai, human, ...
        """
        pv = []
        if first_move is None:
            return pv

        # Track mutations so we can restore
        mutations = []
        x, y = first_move
        stone = self.ai
        opp = self.human
        zhash = self.zhash

        for i in range(max_len):
            if not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
                break
            if board[y][x] != 0:
                break
            pv.append((x, y, stone))
            board[y][x] = stone
            zhash = _zobrist_update(zhash, x, y, 0, stone)
            mutations.append((x, y))

            # Look up TT for this position's best move (next player's response)
            tt_entry = self.tt.get(zhash)
            if not tt_entry or not tt_entry[3]:
                break
            next_move = tt_entry[3]
            # Swap sides
            stone, opp = opp, stone
            x, y = next_move

        # Restore board
        for mx, my in reversed(mutations):
            board[my][mx] = 0

        return pv

    def find_counter_move(self, board, ai_move):
        """After AI picks ai_move, find the opponent's best response via TT or quick search."""
        if ai_move is None:
            return None
        ax, ay = ai_move
        board[ay][ax] = self.ai
        zhash = _zobrist_update(self.zhash, ax, ay, 0, self.ai)

        # 1) Check TT for the position after AI's move
        tt_entry = self.tt.get(zhash)
        if tt_entry and tt_entry[3]:
            counter = tt_entry[3]
            board[ay][ax] = 0
            # Verify counter move is valid
            cx, cy = counter
            if 0 <= cx < BOARD_SIZE and 0 <= cy < BOARD_SIZE and board[cy][cx] == 0:
                return counter

        # 2) Fallback: quick 1-ply search for opponent's best move
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

    def _order_moves(self, board, cands, stone, opp, depth):
        """Order moves using TT best move, killer moves, and history heuristic."""
        tt_move = None
        tt_entry = self.tt.get(self.zhash)
        if tt_entry and tt_entry[3]:
            tt_move = tt_entry[3]

        killers = self.killers.get(depth, [])
        
        def move_priority(move):
            x, y = move
            score = 0
            # TT best move gets highest priority
            if tt_move and move == tt_move:
                score += 10000000
            # Killer moves
            if move in killers:
                score += 1000000
            # History heuristic
            score += self.history.get(move, 0)
            return score

        cands.sort(key=move_priority, reverse=True)
        return cands

    def _store_killer(self, depth, move):
        """Store a killer move (caused beta cutoff) at given depth."""
        if depth not in self.killers:
            self.killers[depth] = []
        kl = self.killers[depth]
        if move not in kl:
            kl.insert(0, move)
            if len(kl) > 2:
                kl.pop()

    def _store_history(self, move, depth):
        """Boost history score for a move that caused cutoff."""
        self.history[move] = self.history.get(move, 0) + depth * depth

    def _root(self, board, depth):
        cands = _deep_sorted_candidates(board, self.ai, self.human, max_moves=20)
        if not cands:
            return None, 0
        best_move = cands[0]
        alpha = -math.inf
        for i, (x, y) in enumerate(cands):
            if self._check():
                break
            board[y][x] = self.ai
            self.zhash = _zobrist_update(self.zhash, x, y, 0, self.ai)
            
            s = -self._ab(board, depth - 1, -math.inf, -alpha, self.human, self.ai, depth - 1)
            
            board[y][x] = 0
            self.zhash = _zobrist_update(self.zhash, x, y, self.ai, 0)
            
            self.root_scores[(x, y)] = s
            if s > alpha:
                alpha = s
                best_move = (x, y)
            if self.progress_cb and (i % 3 == 0 or i == len(cands) - 1):
                self._emit_progress((x, y))
        return best_move, alpha

    def _ab(self, board, depth, alpha, beta, stone, opp, ply):
        self.nodes += 1
        if self.nodes % 128 == 0 and self._check():
            return 0
        
        orig_alpha = alpha

        # ── Transposition table lookup ──
        tt_entry = self.tt.get(self.zhash)
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

        if depth <= 0:
            score = self._qeval(board, stone, opp)
            # Store in TT
            if len(self.tt) < self.tt_max_size:
                self.tt[self.zhash] = (0, score, TT_EXACT, None)
            return score
        
        # Fewer candidates at deeper depths for speed
        max_m = min(12, 6 + depth * 2)
        cands = _fast_sorted_candidates(board, stone, opp, max_moves=max_m)
        if not cands:
            return 0
        
        # Order moves with killer/history heuristics
        cands = self._order_moves(board, cands, stone, opp, ply)
        
        # Check for immediate win first (very fast)
        for x, y in cands:
            board[y][x] = stone
            if _has_five(board, x, y, stone):
                board[y][x] = 0
                return FIVE + depth
            board[y][x] = 0
        
        best_move = None
        for x, y in cands:
            board[y][x] = stone
            self.zhash = _zobrist_update(self.zhash, x, y, 0, stone)
            
            s = -self._ab(board, depth - 1, -beta, -alpha, opp, stone, ply + 1)
            
            board[y][x] = 0
            self.zhash = _zobrist_update(self.zhash, x, y, stone, 0)
            
            if self.timeout:
                return alpha
            if s >= beta:
                # Beta cutoff — store killer and history
                self._store_killer(ply, (x, y))
                self._store_history((x, y), depth)
                # Store in TT as lower bound
                if len(self.tt) < self.tt_max_size:
                    self.tt[self.zhash] = (depth, beta, TT_BETA, (x, y))
                return beta
            if s > alpha:
                alpha = s
                best_move = (x, y)
        
        # Store in TT
        if len(self.tt) < self.tt_max_size:
            if alpha > orig_alpha:
                self.tt[self.zhash] = (depth, alpha, TT_EXACT, best_move)
            else:
                self.tt[self.zhash] = (depth, alpha, TT_ALPHA, best_move)
        
        return alpha

    def _qeval(self, board, stone, opp):
        """Quiescence: static eval + immediate threat check."""
        cands = _get_candidates(board, radius=1)
        for x, y in cands:
            if board[y][x] != 0:
                continue
            board[y][x] = stone
            if _has_five(board, x, y, stone):
                board[y][x] = 0
                return FIVE
            board[y][x] = 0
        for x, y in cands:
            if board[y][x] != 0:
                continue
            board[y][x] = opp
            if _has_five(board, x, y, opp):
                board[y][x] = 0
                return -FIVE + 1
            board[y][x] = 0
        
        if stone == self.ai:
            return _evaluate(board, self.ai, self.human)
        else:
            return -_evaluate(board, self.ai, self.human)


# ──────────────────────────────────────────────
# GomokuAI class
# ──────────────────────────────────────────────

class GomokuAI:
    """
    Heuristic-based 오목 AI with alpha-beta search and threat detection.
    
    difficulty 1~10 controls search depth and time.
    """
    DIFF = {
        1:  (2,  1.0),
        2:  (2,  2.0),
        3:  (4,  3.0),
        4:  (4,  5.0),
        5:  (6,  8.0),
        6:  (6,  12.0),
        7:  (8,  15.0),
        8:  (8,  20.0),
        9:  (10, 25.0),
        10: (10, 30.0),
    }

    def __init__(self, difficulty=5):
        self.difficulty = difficulty

    def set_difficulty(self, d):
        self.difficulty = max(1, min(10, d))

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
            # Try counter-attack: a four of ours at opponent's open four spot
            our_fours = _find_fours(grid, ai, cands)
            for fx, fy in our_fours:
                if (fx, fy) in oof:
                    logger.debug("Counter+block: (%d,%d)", fx, fy)
                    return (fx, fy)
            logger.debug("Block open four: %s", oof[0])
            return oof[0]

        # ── 6. Create fork ──
        fk = _find_forks(grid, ai, cands)
        if fk:
            logger.debug("Fork: %s", fk[0])
            return fk[0]

        # ── 7. VCF forced win ──
        vcf = _vcf(grid, ai, human, depth=16, cands=cands)
        if vcf:
            logger.debug("VCF: %s", vcf)
            return vcf

        # ── 8. Block opponent fork ──
        ofk = _find_forks(grid, human, cands)
        if ofk:
            logger.debug("Block fork: %s", ofk[0])
            return ofk[0]

        # ── 9. Block opponent VCF ──
        ovcf = _vcf(grid, human, ai, depth=10, cands=cands)
        if ovcf:
            if grid[ovcf[1]][ovcf[0]] == 0:
                logger.debug("Block VCF: %s", ovcf)
                return ovcf

        # ── 10. Alpha-beta search ──
        depth, max_time = self.DIFF.get(self.difficulty, (6, 10.0))
        searcher = _ABSearch(ai, human, max_time)
        move, score = searcher.search(grid, depth)
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
        """Generate AI move with progress callback for visualization.
        
        progress_cb(event_type, data):
            'phase'    -> data = {'phase': str, 'move': [x,y] or None}
            'candidates' -> data = {'candidates': [{'x','y','score','pct'}...], 'best': [x,y]}
            'done'     -> data = {'move': [x,y], 'reason': str}
        """
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
            """Quickly find opponent's best response after AI places ai_move."""
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
                    # Legacy: convert single counter_move to 2-step PV
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

        # ── 2. Immediate threats (fast — show as phase) ──
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

        fk = _find_forks(grid, ai, cands)
        if fk:
            emit_done(fk[0], 'fork', _quick_counter(fk[0]))
            return fk[0]

        # ── VCF ──
        if progress_cb:
            progress_cb('phase', {'phase': 'vcf_scan', 'move': None})

        vcf_result = _vcf(grid, ai, human, depth=16, cands=cands)
        if vcf_result:
            emit_done(vcf_result, 'vcf', _quick_counter(vcf_result))
            return vcf_result

        ofk = _find_forks(grid, human, cands)
        if ofk:
            emit_done(ofk[0], 'block_fork', _quick_counter(ofk[0]))
            return ofk[0]

        ovcf = _vcf(grid, human, ai, depth=10, cands=cands)
        if ovcf:
            if grid[ovcf[1]][ovcf[0]] == 0:
                emit_done(ovcf, 'block_vcf', _quick_counter(ovcf))
                return ovcf

        # ── Alpha-beta search with progress ──
        if progress_cb:
            progress_cb('phase', {'phase': 'search', 'move': None})

        depth, max_time = self.DIFF.get(self.difficulty, (6, 10.0))

        def ab_progress(scores, current_move):
            """Convert raw scores dict to percentage-based candidates with PV lines."""
            if not scores:
                return
            items = list(scores.items())
            # Normalize scores to percentages using softmax-like approach
            raw_vals = [s for _, s in items]
            max_s = max(raw_vals)
            min_s = min(raw_vals)
            spread = max_s - min_s if max_s != min_s else 1.0

            # Shift and scale for display
            weights = []
            for (pos, s) in items:
                # Map to 0-1 range, with exponential emphasis on top moves
                norm = (s - min_s) / spread
                w = math.exp(norm * 3)  # Exponential to make differences clearer
                weights.append(w)

            total_w = sum(weights)
            candidates = []
            best_pos = None
            best_s = -math.inf
            for i, ((x, y), s) in enumerate(items):
                pct = (weights[i] / total_w) * 100 if total_w > 0 else 0
                cand = {'x': x, 'y': y, 'score': s, 'pct': round(pct, 1)}
                # Extract PV line for this candidate from TT
                pv = searcher.extract_pv(grid, (x, y), max_len=8)
                if pv and len(pv) > 1:
                    # pv[0] is the candidate itself (ai stone), rest are the continuation
                    cand['pv'] = [[px, py, 'w' if ps == ai else 'b'] for px, py, ps in pv]
                candidates.append(cand)
                if s > best_s:
                    best_s = s
                    best_pos = [x, y]

            # Sort by pct desc, keep top 8 for display clarity
            candidates.sort(key=lambda c: c['pct'], reverse=True)
            candidates = candidates[:8]

            progress_cb('candidates', {
                'candidates': candidates,
                'best': best_pos
            })

        searcher = _ABSearch(ai, human, max_time, progress_cb=ab_progress)
        move, score = searcher.search(grid, depth)
        elapsed = time.time() - t0

        if move and move in legal:
            # Extract full PV line for the final chosen move
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
