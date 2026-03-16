# -*- coding: utf-8 -*-
import os
import re
import json
import datetime
import shutil

RECORDS_DIR = os.path.join(os.path.dirname(__file__), '..', 'records')

def _sanitize_record_id(record_id):
    """Sanitize record_id to prevent path traversal."""
    if not record_id or not re.match(r'^[a-zA-Z0-9_\-]+$', record_id):
        return None
    return record_id

def get_record_path(record_id):
    os.makedirs(RECORDS_DIR, exist_ok=True)
    return os.path.join(RECORDS_DIR, f"{record_id}.json")

def save_record(history, winner, difficulty=0):
    now = datetime.datetime.now()
    timestamp = now.strftime('%Y-%m-%d %H:%M')
    record_id = now.strftime('%Y%m%d_%H%M%S')
    
    record = {
        'id': record_id,
        'timestamp': timestamp,
        'winner': winner,
        'difficulty': difficulty,
        'move_count': len(history),
        'moves': history
    }
    
    path = get_record_path(record_id)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    
    return record_id

def list_records():
    if not os.path.exists(RECORDS_DIR):
        return []
    
    records = []
    for filename in sorted(os.listdir(RECORDS_DIR), reverse=True):
        if filename.endswith('.json'):
            try:
                path = os.path.join(RECORDS_DIR, filename)
                with open(path, 'r', encoding='utf-8') as f:
                    record = json.load(f)
                    moves = record.get('moves', [])
                    records.append({
                        'id': record.get('id', filename[:-5]),
                        'timestamp': record.get('timestamp', ''),
                        'winner': record.get('winner', ''),
                        'difficulty': record.get('difficulty', 0),
                        'move_count': record.get('move_count', len(moves))
                    })
            except Exception as e:
                print(f"[RECORD] 기보 파일 읽기 실패: {filename} - {e}")
    
    return records

def get_record(record_id):
    record_id = _sanitize_record_id(record_id)
    if not record_id:
        return None
    path = get_record_path(record_id)
    if not os.path.exists(path):
        return None

    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None

def get_stats():
    """총 게임 수, 승/패/무승부, 승률 계산"""
    records = list_records()
    total = len(records)
    wins = sum(1 for r in records if r['winner'] == 'black')
    losses = sum(1 for r in records if r['winner'] == 'white')
    draws = sum(1 for r in records if r['winner'] == 'draw')
    win_rate = round(wins / total * 100, 1) if total > 0 else 0
    return {
        'total': total,
        'wins': wins,
        'losses': losses,
        'draws': draws,
        'win_rate': win_rate
    }

def clear_records():
    """모든 기보 삭제"""
    if os.path.exists(RECORDS_DIR):
        shutil.rmtree(RECORDS_DIR)
    os.makedirs(RECORDS_DIR, exist_ok=True)