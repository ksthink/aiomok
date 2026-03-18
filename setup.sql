-- Supabase SQL Editor에서 실행해주세요.

CREATE TABLE records (
    id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    winner TEXT NOT NULL,
    difficulty INTEGER DEFAULT 0,
    move_count INTEGER NOT NULL,
    moves JSONB NOT NULL
);

CREATE INDEX idx_records_timestamp ON records (timestamp DESC);

-- 프론트/백엔드에서 anon key로 접근 가능하도록 RLS 비활성화 (싱글 플레이용)
ALTER TABLE records DISABLE ROW LEVEL SECURITY;
