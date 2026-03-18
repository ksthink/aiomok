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

-- ⚠️  보안 경고: 아래 구문은 RLS(Row Level Security)를 비활성화합니다.
-- anon key를 가진 누구나 이 테이블에 대해 무제한 CRUD가 가능해집니다.
-- 싱글 플레이어 전용 프라이빗 배포에서만 사용하세요.
-- public 리포지토리 또는 멀티 유저 환경에서는 RLS 정책을 반드시 활성화하세요.
ALTER TABLE records DISABLE ROW LEVEL SECURITY;
