# -*- coding: utf-8 -*-
import os
import re
import datetime
import zoneinfo
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# Supabase 설정 — .env 파일 또는 환경변수에서 읽음
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_KEY must be set in environment variables. "
        "Copy .env.example to .env and fill in the values."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def _sanitize_record_id(record_id):
    """Sanitize record_id to prevent path traversal."""
    if not record_id or not re.match(r'^[a-zA-Z0-9_\-]+$', record_id):
        return None
    return record_id

def save_record(history, winner, difficulty=0):
    now = datetime.datetime.now(zoneinfo.ZoneInfo('Asia/Seoul'))
    timestamp = now.strftime('%Y-%m-%d %H:%M')
    record_id = now.strftime('%Y%m%d_%H%M%S')
    
    data = {
        'id': record_id,
        'timestamp': timestamp,
        'winner': winner,
        'difficulty': difficulty,
        'move_count': len(history),
        'moves': history
    }
    
    try:
        supabase.table("records").insert(data).execute()
    except Exception as e:
        print(f"[RECORD] Supabase 저장 실패: {e}")
        
    return record_id

def list_records():
    try:
        # 내림차순 정렬 (최신 기보가 위로)
        res = supabase.table("records").select("id, timestamp, winner, difficulty, move_count").order("timestamp", desc=True).execute()
        return res.data
    except Exception as e:
        print(f"[RECORD] Supabase 목록 로드 실패: {e}")
        return []

def get_record(record_id):
    record_id = _sanitize_record_id(record_id)
    if not record_id:
        return None

    try:
        res = supabase.table("records").select("*").eq("id", record_id).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]
        return None
    except Exception as e:
        print(f"[RECORD] Supabase 개별 기보 로드 실패: {e}")
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
    """모든 기보 삭제 (단, 조건 없이 전체 삭제를 해야하므로 주의)"""
    try:
        # Supabase에서는 빈 필터 없이 삭제 불가, 조건을 주어서 전체를 삭제 (id != '')
        supabase.table("records").delete().neq("id", "impossible_id_to_delete_all").execute()
    except Exception as e:
        print(f"[RECORD] Supabase 전체 기보 삭제 실패: {e}")
