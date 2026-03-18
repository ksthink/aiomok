import os
import json
from supabase import create_client

SUPABASE_URL = "https://hkcbnibbguzbgqucnkzm.supabase.co"
SUPABASE_KEY = "sb_publishable_Kx0gAQwUHagHrNyFZo7xjg_7Y72LCFn"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
RECORDS_DIR = os.path.join(os.path.dirname(__file__), 'records')

def migrate():
    if not os.path.exists(RECORDS_DIR):
        print("기존 records 폴더가 없습니다.")
        return

    files = [f for f in os.listdir(RECORDS_DIR) if f.endswith('.json')]
    if not files:
        print("이관할 기보가 없습니다.")
        return

    print(f"총 {len(files)}개의 기보를 이관합니다...")
    success = 0
    fail = 0

    for filename in files:
        path = os.path.join(RECORDS_DIR, filename)
        with open(path, 'r', encoding='utf-8') as f:
            try:
                record = json.load(f)
                data = {
                    'id': record.get('id', filename[:-5]),
                    'timestamp': record.get('timestamp', ''),
                    'winner': record.get('winner', 'draw'),
                    'difficulty': record.get('difficulty', 0),
                    'move_count': record.get('move_count', len(record.get('moves', []))),
                    'moves': record.get('moves', [])
                }
                supabase.table("records").insert(data).execute()
                success += 1
                print(f"[{success}/{len(files)}] {filename} 이관 성공")
            except Exception as e:
                fail += 1
                print(f"[{fail}] {filename} 이관 실패: {e}")

    print(f"마이그레이션 완료. 성공: {success}, 실패: {fail}")

if __name__ == '__main__':
    migrate()