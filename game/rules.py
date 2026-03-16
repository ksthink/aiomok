# -*- coding: utf-8 -*-
"""
렌주룰 금수 판정 (흑만 적용)
- 33 금수: 열린 3이 두 방향 이상 동시에 만들어지는 경우
- 44 금수: 4가 두 방향 이상 동시에 만들어지는 경우
- 장목 금수: 6목 이상
- 단, 5목이 되는 수는 금수에서 제외 (이기는 수)
"""

BOARD_SIZE = 15
DIRECTIONS = [(1, 0), (0, 1), (1, 1), (1, -1)]


def get_line(board_grid, cx, cy, dx, dy, length=11):
    """중심(cx,cy)에서 (dx,dy) 방향으로 총 length개 셀을 추출.
    범위 밖은 -1(벽)으로 채움. 중심은 인덱스 length//2."""
    half = length // 2
    line = []
    for i in range(-half, half + 1):
        nx, ny = cx + dx * i, cy + dy * i
        if 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE:
            line.append(board_grid[ny][nx])
        else:
            line.append(-1)  # 벽
    return line, half  # half = 중심 인덱스


def count_line(board_grid, cx, cy, dx, dy, stone):
    """(cx,cy)에서 (dx,dy) 방향으로 연속된 stone 수를 셈."""
    count = 0
    for sign in (1, -1):
        nx, ny = cx + dx * sign, cy + dy * sign
        while 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE and board_grid[ny][nx] == stone:
            count += 1
            nx += dx * sign
            ny += dy * sign
    return count + 1  # 중심 포함


def makes_five(board_grid, x, y, stone):
    """(x,y)에 stone을 놓으면 정확히 5목(장목 제외)이 되는지 확인."""
    for dx, dy in DIRECTIONS:
        if count_line(board_grid, x, y, dx, dy, stone) == 5:
            return True
    return False


def makes_overline(board_grid, x, y, stone):
    """(x,y)에 stone을 놓으면 6목 이상이 되는지 확인."""
    for dx, dy in DIRECTIONS:
        if count_line(board_grid, x, y, dx, dy, stone) >= 6:
            return True
    return False


def count_open_fours(board_grid, x, y, stone):
    """(x,y)에 stone을 놓았을 때 생기는 '열린 4' 또는 '4'의 수를 반환.
    4 = 연속 4개이면서 다음 수에 5목이 가능한 형태.
    (닫힌 4도 포함 — 44 금수는 열린/닫힌 관계없이 4가 2개면 금수)"""
    board_grid[y][x] = stone
    count = 0
    for dx, dy in DIRECTIONS:
        if _is_four_in_direction(board_grid, x, y, dx, dy, stone):
            count += 1
    board_grid[y][x] = 0
    return count


def _is_four_in_direction(board_grid, x, y, dx, dy, stone):
    """(x,y)를 포함하여 (dx,dy) 방향으로 4(또는 열린4)가 있는지 확인.
    
    4의 정의: 해당 방향에서 돌이 정확히 4개 연속이고,
    그 직선 위에 한 칸 더 놓으면 5목이 될 수 있는 형태.
    즉, 4개 연속 + 양끝 중 하나 이상이 비어있음 (장목이 되지 않아야 함).
    """
    # 연속 길이 계산
    length = 1
    ends = []  # 양 끝 바로 다음 셀 상태
    for sign in (1, -1):
        nx, ny = x + dx * sign, y + dy * sign
        ext = 0
        while 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE and board_grid[ny][nx] == stone:
            length += 1
            ext += 1
            nx += dx * sign
            ny += dy * sign
        # 끝 다음 셀
        if 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE:
            ends.append(board_grid[ny][nx])
        else:
            ends.append(-1)  # 벽

    if length != 4:
        return False
    # 양 끝 중 하나 이상이 비어있어야 5목 가능 (장목 방지 불필요 — 이미 length==4 체크)
    return 0 in ends


def count_open_threes(board_grid, x, y, stone):
    """(x,y)에 stone을 놓았을 때 생기는 '열린 3' 수를 반환.
    
    열린 3의 정의 (렌주룰):
    한 수 더 놓으면 '열린 4'가 되는 3.
    즉, 한 칸 더 놓았을 때 양 끝이 열린 4(= 연속 4 + 양쪽 모두 빔)가 생기는 형태.
    """
    board_grid[y][x] = stone
    count = 0
    for dx, dy in DIRECTIONS:
        if _is_open_three_in_direction(board_grid, x, y, dx, dy, stone):
            count += 1
    board_grid[y][x] = 0
    return count


def _is_open_three_in_direction(board_grid, x, y, dx, dy, stone):
    """(x,y)를 포함하는 방향에서 열린 3인지 확인.
    
    열린 3: 이 방향으로 한 칸을 추가했을 때 열린 4(양끝 열린 4개 연속)가 되어야 함.
    단, 그 한 칸 추가가 금수가 아니고 놓을 수 있는 자리여야 함.
    """
    # 이 방향의 연속 길이 계산
    length = 1
    pos = [(x, y)]
    for sign in (1, -1):
        nx, ny = x + dx * sign, y + dy * sign
        while 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE and board_grid[ny][nx] == stone:
            length += 1
            pos.append((nx, ny))
            nx += dx * sign
            ny += dy * sign

    if length != 3:
        return False

    # 3개 연속의 양 끝 빈칸을 찾아, 그 자리에 놓으면 열린 4가 되는지 확인
    # 양 끝 다음 빈칸 위치 찾기
    min_x = min(p[0] for p in pos)
    min_y = min(p[1] for p in pos)
    max_x = max(p[0] for p in pos)
    max_y = max(p[1] for p in pos)

    # 연속 블록의 앞뒤 빈칸
    candidates = []
    # 앞
    fx, fy = min_x - dx, min_y - dy
    if dx < 0 or (dx == 0 and dy < 0):
        fx, fy = max_x - dx, max_y - dy
    # 더 간단하게: 방향을 따라 처음과 끝 바깥 빈칸
    start = pos[0]
    end = pos[-1]
    # pos를 방향 기준으로 정렬
    if dx != 0:
        pos_sorted = sorted(pos, key=lambda p: p[0])
    else:
        pos_sorted = sorted(pos, key=lambda p: p[1])
    s = pos_sorted[0]
    e = pos_sorted[-1]

    before = (s[0] - dx, s[1] - dy)
    after = (e[0] + dx, e[1] + dy)

    for cx, cy in [before, after]:
        if not (0 <= cx < BOARD_SIZE and 0 <= cy < BOARD_SIZE):
            continue
        if board_grid[cy][cx] != 0:
            continue
        # 이 자리에 놓으면 열린 4가 되는지 확인
        board_grid[cy][cx] = stone
        is_open4 = _is_open_four_in_direction(board_grid, cx, cy, dx, dy, stone)
        board_grid[cy][cx] = 0
        if is_open4:
            return True

    return False


def _is_open_four_in_direction(board_grid, x, y, dx, dy, stone):
    """(x,y)를 포함하여 해당 방향이 '열린 4' (양끝이 모두 빈 4개 연속)인지 확인."""
    length = 1
    ends = []
    for sign in (1, -1):
        nx, ny = x + dx * sign, y + dy * sign
        while 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE and board_grid[ny][nx] == stone:
            length += 1
            nx += dx * sign
            ny += dy * sign
        if 0 <= nx < BOARD_SIZE and 0 <= ny < BOARD_SIZE:
            ends.append(board_grid[ny][nx])
        else:
            ends.append(-1)

    return length == 4 and ends.count(0) == 2


def get_forbidden_type(board, x, y):
    """렌주룰 금수 판정. 금수 종류 문자열 반환, 아니면 None.
    반환값: 'overline' | '44' | '33' | None
    """
    if not board.is_valid(x, y) or not board.is_empty(x, y):
        return None

    import copy
    grid = copy.deepcopy(board.board)

    # 5목이 되는 수는 금수 제외 (이기는 수)
    if makes_five(grid, x, y, 1):
        return None

    # 장목 금수 (6목 이상)
    if makes_overline(grid, x, y, 1):
        return 'overline'

    # 44 금수
    if count_open_fours(grid, x, y, 1) >= 2:
        return '44'

    # 33 금수
    if count_open_threes(grid, x, y, 1) >= 2:
        return '33'

    return None


def is_forbidden_move(board, x, y):
    """렌주룰 금수 판정. 금수면 True, 아니면 False."""
    return get_forbidden_type(board, x, y) is not None
