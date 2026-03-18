# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, jsonify, session, Response
import os
import json
import queue
import threading
import psutil
from dotenv import load_dotenv
from game.omok import OmokBoard
from game.ai import GomokuAI
from game.rules import get_forbidden_type
from game.record import save_record, list_records, get_record, get_stats, clear_records

load_dotenv()

app = Flask(__name__)
_secret_key = os.environ.get('SECRET_KEY')
if not _secret_key:
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. "
        "Copy .env.example to .env and set a strong random value."
    )
app.secret_key = _secret_key

MAX_DIFFICULTY = 10

# Thread lock for AI computation — prevents concurrent board corruption
_ai_lock = threading.Lock()

def get_ai(difficulty=MAX_DIFFICULTY):
    """Create a fresh AI instance per call (thread-safe, no shared state)."""
    return GomokuAI(difficulty=difficulty)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/board/new', methods=['POST'])
def new_board():
    board = OmokBoard()
    session['board'] = board.to_dict()
    session['move_history'] = []
    session['ai_difficulty'] = MAX_DIFFICULTY
    session.modified = True
    return jsonify({'status': 'ok', 'board': board.to_list()})

@app.route('/api/move', methods=['POST'])
def make_move():
    data = request.get_json() or {}
    x, y = data.get('x'), data.get('y')

    if x is None or y is None:
        return jsonify({'status': 'error', 'message': '좌표가 필요합니다.'})
    try:
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': '잘못된 좌표입니다.'})
    if not (0 <= x < 15 and 0 <= y < 15):
        return jsonify({'status': 'error', 'message': '좌표가 범위를 벗어났습니다.'})

    board_data = session.get('board')
    if not board_data:
        return jsonify({'status': 'error', 'message': '게임을 먼저 시작하세요.'})

    board = OmokBoard.from_dict(board_data)

    # 흑 금수 판정
    if board.current_turn == 1:
        forbidden_type = get_forbidden_type(board, x, y)
        if forbidden_type:
            msg = {'33': '3-3 금수', '44': '4-4 금수', 'overline': '장목 금수'}.get(forbidden_type, '금수')
            return jsonify({'status': 'forbidden', 'message': msg})

    if not board.place_stone(x, y):
        return jsonify({'status': 'error', 'message': '이미 돌이 있습니다.'})

    history = session.get('move_history', [])
    color = 'black' if board.current_turn == 2 else 'white'
    history.append({'x': x, 'y': y, 'color': color, 'move': len(history) + 1})
    session['move_history'] = history
    session['board'] = board.to_dict()
    session.modified = True

    winner = board.check_winner()
    if winner:
        save_record(history, winner, MAX_DIFFICULTY)
        return jsonify({'status': 'win', 'board': board.to_list(), 'winner': winner, 'moves': len(history)})

    if board.move_count >= 225:
        save_record(history, 'draw', MAX_DIFFICULTY)
        return jsonify({'status': 'draw', 'board': board.to_list(), 'moves': len(history)})

    return jsonify({'status': 'continue', 'board': board.to_list(), 'turn': board.current_turn, 'moves': len(history)})

@app.route('/api/ai/move', methods=['POST'])
def ai_move():
    difficulty = MAX_DIFFICULTY

    board_data = session.get('board')
    if not board_data:
        return jsonify({'status': 'error', 'message': '게임을 먼저 시작하세요.'})

    board = OmokBoard.from_dict(board_data)
    ai = get_ai(difficulty)

    # Deep copy the board for AI search (prevents cross-request corruption)
    ai_board = OmokBoard.from_dict(board.to_dict())

    acquired = _ai_lock.acquire(timeout=60)
    if not acquired:
        return jsonify({'status': 'error', 'message': 'AI가 다른 게임을 처리 중입니다. 잠시 후 다시 시도해주세요.'})
    try:
        import time
        start_time = time.time()
        move = ai.generate_move(ai_board)
        elapsed_time = time.time() - start_time
    finally:
        _ai_lock.release()

    if move is None:
        return jsonify({'status': 'error', 'message': 'AI가 둘 자리가 없습니다.'})

    ax, ay = move
    board.place_stone(ax, ay)

    history = session.get('move_history', [])
    history.append({'x': ax, 'y': ay, 'color': 'white', 'move': len(history) + 1})
    session['move_history'] = history
    session['board'] = board.to_dict()
    session.modified = True

    winner = board.check_winner()
    if winner:
        save_record(history, winner, difficulty)
        return jsonify({'status': 'win', 'board': board.to_list(), 'winner': winner, 'ai_move': [ax, ay], 'ai_time': elapsed_time, 'moves': len(history)})

    return jsonify({'status': 'continue', 'board': board.to_list(), 'turn': board.current_turn, 'ai_move': [ax, ay], 'ai_time': elapsed_time, 'moves': len(history)})


@app.route('/api/ai/move/stream', methods=['POST'])
def ai_move_stream():
    """SSE endpoint: streams AI thinking progress then final move."""
    difficulty = MAX_DIFFICULTY

    board_data = session.get('board')
    if not board_data:
        return jsonify({'status': 'error', 'message': '게임을 먼저 시작하세요.'})

    board = OmokBoard.from_dict(board_data)
    # Deep copy for AI search — isolates from other concurrent requests
    ai_board = OmokBoard.from_dict(board_data)
    ai = get_ai(difficulty)

    event_queue = queue.Queue()
    result_holder = []
    ai_done = threading.Event()

    # CPU usage monitor for current process
    _process = psutil.Process(os.getpid())
    _cpu_count = psutil.cpu_count() or 1
    # Prime the cpu_percent measurement (first call always returns 0)
    _process.cpu_percent()

    def progress_cb(event_type, data):
        event_queue.put((event_type, data))

    def cpu_monitor():
        """Periodically push CPU usage into event_queue while AI is running."""
        while not ai_done.is_set():
            try:
                raw_pct = _process.cpu_percent(interval=0.5)
                # Normalize to 0-100% range (psutil returns N*100% on N cores)
                pct = min(100, round(raw_pct / _cpu_count))
                event_queue.put(('cpu', {'cpu': pct}))
            except Exception:
                pass
        # Final: AI done, send 0%
        event_queue.put(('cpu', {'cpu': 0}))

    def run_ai():
        acquired = _ai_lock.acquire(timeout=60)
        if not acquired:
            event_queue.put(('error', {'message': 'AI가 다른 게임을 처리 중입니다.'}))
            ai_done.set()
            event_queue.put(('_finished', None))
            return
        try:
            import time
            start_time = time.time()
            move = ai.generate_move_with_progress(ai_board, progress_cb=progress_cb)
            elapsed_time = time.time() - start_time
            if result_holder:
                result_holder[0] = (move, elapsed_time)
            else:
                result_holder.append((move, elapsed_time))
        except Exception as e:
            event_queue.put(('error', {'message': str(e)}))
        finally:
            _ai_lock.release()
            ai_done.set()
            event_queue.put(('_finished', None))

    sess_history = list(session.get('move_history', []))

    t = threading.Thread(target=run_ai, daemon=True)
    cpu_t = threading.Thread(target=cpu_monitor, daemon=True)
    t.start()
    cpu_t.start()

    def generate():
        while True:
            try:
                evt_type, evt_data = event_queue.get(timeout=60)
            except queue.Empty:
                yield f"event: timeout\ndata: {{}}\n\n"
                break

            if evt_type == '_finished':
                if not result_holder:
                    yield f"event: result\ndata: {json.dumps({'status': 'error', 'message': 'AI 결과가 없습니다.'})}\n\n"
                    break

                move, elapsed_time = result_holder[0]
                if move is None:
                    yield f"event: result\ndata: {json.dumps({'status': 'error', 'message': 'AI가 둘 자리가 없습니다.'})}\n\n"
                    break

                ax, ay = move
                board.place_stone(ax, ay)
                new_history = sess_history + [{'x': ax, 'y': ay, 'color': 'white', 'move': len(sess_history) + 1}]

                winner = board.check_winner()
                if winner:
                    save_record(new_history, winner, difficulty)
                    result = {'status': 'win', 'board': board.to_list(), 'winner': winner,
                              'ai_move': [ax, ay], 'ai_time': elapsed_time, 'moves': len(new_history),
                              'history': new_history}
                else:
                    result = {'status': 'continue', 'board': board.to_list(), 'turn': board.current_turn,
                              'ai_move': [ax, ay], 'ai_time': elapsed_time, 'moves': len(new_history),
                              'history': new_history}

                yield f"event: result\ndata: {json.dumps(result)}\n\n"
                break
            else:
                yield f"event: {evt_type}\ndata: {json.dumps(evt_data)}\n\n"

    response = Response(generate(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response


@app.route('/api/ai/move/sync', methods=['POST'])
def ai_move_sync():
    """Sync session state after SSE stream completes."""
    data = request.get_json() or {}
    ai_move_data = data.get('ai_move')
    if not ai_move_data or len(ai_move_data) != 2:
        return jsonify({'status': 'error', 'message': 'ai_move 필요'})

    try:
        ax, ay = int(ai_move_data[0]), int(ai_move_data[1])
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': '잘못된 좌표입니다.'})
    if not (0 <= ax < 15 and 0 <= ay < 15):
        return jsonify({'status': 'error', 'message': '좌표가 범위를 벗어났습니다.'})

    board_data = session.get('board')
    if not board_data:
        return jsonify({'status': 'error', 'message': '게임이 없습니다.'})

    board = OmokBoard.from_dict(board_data)

    if not board.place_stone(ax, ay):
        return jsonify({'status': 'error', 'message': '돌을 놓을 수 없습니다.'})

    history = session.get('move_history', [])
    history.append({'x': ax, 'y': ay, 'color': 'white', 'move': len(history) + 1})
    session['move_history'] = history
    session['board'] = board.to_dict()
    session.modified = True

    winner = board.check_winner()
    status = 'win' if winner else 'continue'
    return jsonify({'status': status, 'synced': True})

@app.route('/api/records', methods=['GET'])
def api_list_records():
    records = list_records()
    return jsonify({'status': 'ok', 'records': records})

@app.route('/api/records/<record_id>', methods=['GET'])
def api_get_record(record_id):
    record = get_record(record_id)
    if record:
        return jsonify({'status': 'ok', 'record': record})
    return jsonify({'status': 'error', 'message': '기보를 찾을 수 없습니다.'}), 404

@app.route('/api/stats', methods=['GET'])
def api_stats():
    stats = get_stats()
    return jsonify({'status': 'ok', 'stats': stats})

@app.route('/api/records/clear', methods=['POST'])
def api_clear_records():
    clear_records()
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    os.makedirs('records', exist_ok=True)
    os.makedirs('model', exist_ok=True)
    app.run(host='0.0.0.0', port=8083, debug=False, threaded=True)
