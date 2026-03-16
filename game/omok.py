# -*- coding: utf-8 -*-
import copy

BOARD_SIZE = 15

class OmokBoard:
    def __init__(self):
        self.size = BOARD_SIZE
        self.board = [[0 for _ in range(self.size)] for _ in range(self.size)]
        self.current_turn = 1
        self.move_count = 0
        self.history = []
    
    def to_list(self):
        return copy.deepcopy(self.board)
    
    def to_dict(self):
        return {
            'board': copy.deepcopy(self.board),
            'current_turn': self.current_turn,
            'move_count': self.move_count,
            'history': copy.deepcopy(self.history)
        }
    
    @classmethod
    def from_dict(cls, data):
        obj = cls()
        obj.board = copy.deepcopy(data['board'])
        obj.current_turn = data['current_turn']
        obj.move_count = data['move_count']
        obj.history = copy.deepcopy(data.get('history', []))
        return obj
    
    def is_valid(self, x, y):
        return 0 <= x < self.size and 0 <= y < self.size
    
    def is_empty(self, x, y):
        return self.board[y][x] == 0
    
    def place_stone(self, x, y):
        if not self.is_valid(x, y) or not self.is_empty(x, y):
            return False
        self.board[y][x] = self.current_turn
        self.history.append((x, y, self.current_turn))
        self.move_count += 1
        self.current_turn = 2 if self.current_turn == 1 else 1
        return True
    
    def undo_move(self):
        if not self.history:
            return False
        x, y, turn = self.history.pop()
        self.board[y][x] = 0
        self.current_turn = turn
        self.move_count -= 1
        return True
    
    def check_winner(self):
        for y in range(self.size):
            for x in range(self.size):
                stone = self.board[y][x]
                if stone == 0:
                    continue
                for dx, dy in [(1, 0), (0, 1), (1, 1), (1, -1)]:
                    count = 1
                    nx, ny = x + dx, y + dy
                    while self.is_valid(nx, ny) and self.board[ny][nx] == stone:
                        count += 1
                        nx += dx
                        ny += dy
                    if count >= 5 and stone == 1:
                        nx2, ny2 = x - dx, y - dy
                        if self.is_valid(nx2, ny2) and self.board[ny2][nx2] == stone:
                            continue
                        return 'black'
                    elif count >= 5 and stone == 2:
                        return 'white'
        return None