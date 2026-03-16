// ── 상수 ──────────────────────────────────────────
const BOARD_SIZE = 15;
const MAX_DIFFICULTY = 10;

// ── 동적 보드 크기 (resizeCanvas에서 계산) ──────────
let CELL_SIZE = 30;
let STONE_RADIUS = 12;
let PADDING = 15;
let BOARD_PX = 450;

// ── 상태 ──────────────────────────────────────────
let boardState = [];
let currentTurn = 1;
let moveHistory = [];
let difficulty = MAX_DIFFICULTY;
let aiThinking = false;
let gameOver = false;
let movePending = false;

// ── DOM 참조 (init에서 할당) ───────────────────────
let canvas = null;
let ctx = null;
let replayCanvas = null;
let replayCtx = null;

// ── 기보 재생 상태 ─────────────────────────────────
let replayBoardState = [];
let replayMoves = [];
let currentReplayMove = 0;
let replayAutoTimer = null;

// ── AI 시각화 상태 ─────────────────────────────────
let ghostCandidates = [];
let ghostBest = null;
let ghostAnimFrame = null;
let ghostPulse = 0;
let lastPvLine = null;  // PV line for final AI decision: [{x, y, color}, ...]

// ── 색상 ──────────────────────────────────────────
function getThemeColors() {
    return {
        boardBg:       '#dcb468',
        gridLine:      '#8a6d3b',
        dotColor:      '#6b5430',
        blackFill:     '#111111',
        blackBorder:   '#000000',
        whiteFill:     '#f0f0f0',
        whiteBorder:   '#aaaaaa',
        markerBlack:   '#ffffff',
        markerWhite:   '#333333',
    };
}

// ══════════════════════════════════════════════════
// 캔버스 크기 동적 계산
// ══════════════════════════════════════════════════
function resizeCanvas(target) {
    const targets = [];
    if ((target === 'game' || target === 'both') && canvas) targets.push(canvas);
    if ((target === 'replay' || target === 'both') && replayCanvas) targets.push(replayCanvas);
    if (targets.length === 0) return;

    const container = document.querySelector('.game-container');
    const containerWidth = container ? container.clientWidth : window.innerWidth;
    const available = Math.min(containerWidth, 600);

    const dpr = window.devicePixelRatio || 1;

    BOARD_PX = available;
    CELL_SIZE = (available - 2) / (BOARD_SIZE + 1);
    PADDING = CELL_SIZE;
    STONE_RADIUS = CELL_SIZE * 0.42;

    targets.forEach(c => {
        c.style.width = available + 'px';
        c.style.height = available + 'px';
        c.width = Math.round(available * dpr);
        c.height = Math.round(available * dpr);
        const cx2 = c.getContext('2d');
        if (cx2) cx2.setTransform(dpr, 0, 0, dpr, 0, 0);
    });
}

// ══════════════════════════════════════════════════
// 캔버스 드로잉
// ══════════════════════════════════════════════════
function drawBoard(useReplay = false) {
    const c = useReplay ? replayCanvas : canvas;
    const cx = useReplay ? replayCtx : ctx;
    if (!cx || !c) return;

    const colors = getThemeColors();

    cx.fillStyle = colors.boardBg;
    cx.fillRect(0, 0, BOARD_PX, BOARD_PX);

    cx.strokeStyle = colors.gridLine;
    cx.lineWidth = 1;
    for (let i = 0; i < BOARD_SIZE; i++) {
        const p = PADDING + i * CELL_SIZE;
        cx.beginPath(); cx.moveTo(PADDING, p); cx.lineTo(PADDING + (BOARD_SIZE - 1) * CELL_SIZE, p); cx.stroke();
        cx.beginPath(); cx.moveTo(p, PADDING); cx.lineTo(p, PADDING + (BOARD_SIZE - 1) * CELL_SIZE); cx.stroke();
    }

    // 화점
    cx.fillStyle = colors.dotColor;
    [[3,3],[3,7],[3,11],[7,3],[7,7],[7,11],[11,3],[11,7],[11,11]].forEach(([x, y]) => {
        cx.beginPath();
        cx.arc(PADDING + x * CELL_SIZE, PADDING + y * CELL_SIZE, Math.max(2, CELL_SIZE * 0.1), 0, Math.PI * 2);
        cx.fill();
    });

    const state = useReplay ? replayBoardState : boardState;
    const lastMove = useReplay
        ? (currentReplayMove > 0 ? replayMoves[currentReplayMove - 1] : null)
        : (moveHistory.length > 0 ? moveHistory[moveHistory.length - 1] : null);

    state.forEach((row, y) => {
        row.forEach((cell, x) => {
            if (!cell) return;
            const px = PADDING + x * CELL_SIZE;
            const py = PADDING + y * CELL_SIZE;

            if (cell === 1) {
                cx.beginPath();
                cx.arc(px, py, STONE_RADIUS, 0, Math.PI * 2);
                cx.fillStyle = colors.blackFill;
                cx.fill();
                cx.strokeStyle = colors.blackBorder;
                cx.lineWidth = 1.5;
                cx.stroke();
            } else {
                cx.beginPath();
                cx.arc(px, py, STONE_RADIUS, 0, Math.PI * 2);
                cx.fillStyle = colors.whiteFill;
                cx.fill();
                cx.strokeStyle = colors.whiteBorder;
                cx.lineWidth = 1.5;
                cx.stroke();
            }

            if (lastMove && lastMove.x === x && lastMove.y === y) {
                const ms = Math.max(3, CELL_SIZE * 0.18);
                cx.fillStyle = cell === 1 ? colors.markerBlack : colors.markerWhite;
                cx.fillRect(px - ms / 2, py - ms / 2, ms, ms);
            }
        });
    });

    // 고스트 돌 (AI 후보 + PV 시각화)
    if (!useReplay && ghostCandidates.length > 0) {
        drawGhostStones(cx);
    }
}

function drawGhostStones(cx) {
    const alpha = 0.3;

    // 최선 후보 1개의 PV 라인만 그린다 (나머지 후보는 백돌+퍼센트만)
    if (ghostBest) {
        const bestCand = ghostCandidates.find(c =>
            ghostBest[0] === c.x && ghostBest[1] === c.y
        );
        if (bestCand && bestCand.pv && bestCand.pv.length > 1) {
            drawPvLine(cx, bestCand.pv.slice(1), true);
        }
    }

    // 후보 돌 (백돌) + 퍼센트 라벨
    ghostCandidates.forEach(cand => {
        const px = PADDING + cand.x * CELL_SIZE;
        const py = PADDING + cand.y * CELL_SIZE;
        const isBest = ghostBest && ghostBest[0] === cand.x && ghostBest[1] === cand.y;
        const r = STONE_RADIUS;

        cx.beginPath();
        cx.arc(px, py, r, 0, Math.PI * 2);
        cx.fillStyle = `rgba(240,240,240,${alpha})`;
        cx.strokeStyle = `rgba(170,170,170,${alpha})`;
        cx.fill();
        cx.lineWidth = 1.5;
        cx.stroke();

        if (cand.pct >= 1) {
            const fontSize = Math.max(7, Math.round(CELL_SIZE * (isBest ? 0.38 : 0.3)));
            cx.font = `bold ${fontSize}px 'D2Coding', monospace`;
            cx.textAlign = 'center';
            cx.textBaseline = 'middle';

            const text = Math.round(cand.pct) + '%';
            const tw = cx.measureText(text).width;
            cx.fillStyle = `rgba(0,0,0,${isBest ? 0.7 : 0.5})`;
            cx.fillRect(px - tw / 2 - 2, py - fontSize / 2 - 1, tw + 4, fontSize + 2);

            cx.fillStyle = isBest ? '#fff' : `rgba(255,255,255,${0.75 + 0.25 * (cand.pct / 100)})`;
            cx.fillText(text, px, py);
        }
    });
}

function drawPvLine(cx, pvSteps, showNumber) {
    /**
     * PV 수순 돌을 반투명으로 그린다.
     * pvSteps: [{x, y, color:'black'|'white'}, ...] or [[x,y,'b'|'w'], ...]
     * showNumber: true이면 순번 숫자 표시
     */
    const alpha = 0.3;

    pvSteps.forEach((step, idx) => {
        const sx = Array.isArray(step) ? step[0] : step.x;
        const sy = Array.isArray(step) ? step[1] : step.y;
        const sc = Array.isArray(step) ? step[2] : step.color;
        const isBlack = sc === 'black' || sc === 'b';

        const px = PADDING + sx * CELL_SIZE;
        const py = PADDING + sy * CELL_SIZE;
        const r = STONE_RADIUS;

        cx.beginPath();
        cx.arc(px, py, r, 0, Math.PI * 2);
        if (isBlack) {
            cx.fillStyle = `rgba(17,17,17,${alpha})`;
            cx.strokeStyle = `rgba(0,0,0,${alpha})`;
        } else {
            cx.fillStyle = `rgba(240,240,240,${alpha})`;
            cx.strokeStyle = `rgba(170,170,170,${alpha})`;
        }
        cx.fill();
        cx.lineWidth = 1.5;
        cx.stroke();

        if (showNumber) {
            // 순번 표시 (1부터 시작)
            const num = idx + 1;
            const fontSize = Math.max(8, Math.round(CELL_SIZE * 0.35));
            cx.font = `bold ${fontSize}px 'D2Coding', monospace`;
            cx.textAlign = 'center';
            cx.textBaseline = 'middle';
            cx.fillStyle = isBlack ? `rgba(255,255,255,0.8)` : `rgba(0,0,0,0.8)`;
            cx.fillText(String(num), px, py);
        }
    });
}

function startGhostAnimation() {
    if (ghostAnimFrame) return;
    function animate() {
        drawBoard();
        ghostAnimFrame = requestAnimationFrame(animate);
    }
    ghostAnimFrame = requestAnimationFrame(animate);
}

function stopGhostAnimation() {
    if (ghostAnimFrame) {
        cancelAnimationFrame(ghostAnimFrame);
        ghostAnimFrame = null;
    }
    ghostCandidates = [];
    ghostBest = null;
    ghostPulse = 0;
}

// ══════════════════════════════════════════════════
// 게임 로직
// ══════════════════════════════════════════════════
function updateStatus() {
    const turnLabel = document.getElementById('turnLabel');
    const moveCount = document.getElementById('moveCount');
    if (turnLabel) {
        if (gameOver) {
            turnLabel.textContent = '게임 종료';
        } else {
            turnLabel.textContent = currentTurn === 1 ? '흑 차례' : 'AI 생각 중';
        }
    }
    if (moveCount) moveCount.textContent = moveHistory.length;
}

function showMessage(msg, type = '') {
    const el = document.getElementById('gameMessage');
    if (!el) return;
    el.textContent = msg;
    el.className = 'game-message' + (type ? ' ' + type : '');
}

function handleCanvasClick(e) {
    if (aiThinking || gameOver || currentTurn !== 1 || movePending) return;

    const rect = canvas.getBoundingClientRect();
    const cx = Math.round(((e.clientX - rect.left) * (BOARD_PX / rect.width) - PADDING) / CELL_SIZE);
    const cy = Math.round(((e.clientY - rect.top) * (BOARD_PX / rect.height) - PADDING) / CELL_SIZE);

    if (cx < 0 || cx >= BOARD_SIZE || cy < 0 || cy >= BOARD_SIZE) return;
    if (boardState[cy][cx] !== 0) return;

    makeMove(cx, cy);
}

async function makeMove(x, y) {
    movePending = true;
    lastPvLine = null;
    try {
        const res = await apiMove(x, y);
        if (!res) return;

        if (res.status === 'forbidden') {
            showMessage(res.message, 'error');
            return;
        }
        if (res.status === 'error') {
            showMessage(res.message, 'error');
            return;
        }

        if (res.status === 'draw') {
            endGame('draw');
            return;
        }
        if (res.status === 'win') {
            endGame(res.winner);
            return;
        }

        updateStatus();
        await runAiMoveStream();
    } finally {
        movePending = false;
    }
}

// ══════════════════════════════════════════════════
// SSE 기반 AI 이동
// ══════════════════════════════════════════════════
async function runAiMoveStream() {
    if (aiThinking || gameOver) return;
    aiThinking = true;
    document.getElementById('aiThinking').classList.remove('hidden');
    const turnLabel = document.getElementById('turnLabel');
    if (turnLabel) turnLabel.textContent = 'AI 생각 중';

    ghostCandidates = [];
    ghostBest = null;
    startGhostAnimation();

    try {
        const res = await fetch('/api/ai/move/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ difficulty })
        });

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let finalData = null;

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });

            let dblNewline;
            while ((dblNewline = buffer.indexOf('\n\n')) !== -1) {
                const block = buffer.substring(0, dblNewline);
                buffer = buffer.substring(dblNewline + 2);

                const lines = block.split('\n');
                let evtType = null;
                let evtData = null;

                for (const line of lines) {
                    if (line.startsWith('event: ')) {
                        evtType = line.substring(7).trim();
                    } else if (line.startsWith('data: ')) {
                        evtData = line.substring(6);
                    }
                }

                if (evtType && evtData !== null) {
                    try {
                        const data = JSON.parse(evtData);
                        handleSSEEvent(evtType, data);
                        if (evtType === 'result') {
                            finalData = data;
                        }
                    } catch (e) {
                        console.warn('SSE JSON parse error:', e);
                    }
                }
            }

            if (finalData) break;
        }

        if (finalData) {
            await sleep(300);
            stopGhostAnimation();
            lastPvLine = null;  // AI 돌 놓으면 PV 라인 제거

            if (finalData.status === 'win' || finalData.status === 'continue') {
                boardState = finalData.board;
                currentTurn = finalData.turn || 1;
                if (finalData.ai_move) {
                    moveHistory.push({
                        x: finalData.ai_move[0],
                        y: finalData.ai_move[1],
                        color: 'white',
                        move: moveHistory.length + 1
                    });
                    await syncAiMove(finalData.ai_move);
                }
                drawBoard();
                updateStatus();
                if (finalData.status === 'win') {
                    endGame(finalData.winner);
                }
            } else {
                showMessage('AI 오류', 'error');
            }
        }
    } catch (e) {
        console.error('SSE error:', e);
        stopGhostAnimation();
        await runAiMoveFallback();
    } finally {
        aiThinking = false;
        document.getElementById('aiThinking').classList.add('hidden');
        stopGhostAnimation();
    }
}

async function syncAiMove(aiMove, retries = 2) {
    for (let i = 0; i <= retries; i++) {
        try {
            const res = await fetch('/api/ai/move/sync', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ ai_move: aiMove })
            });
            if (res.ok) return;
            console.error('Session sync HTTP error:', res.status);
        } catch (e) {
            console.error(`Session sync failed (attempt ${i + 1}):`, e);
        }
        if (i < retries) await sleep(500);
    }
}

function handleSSEEvent(type, data) {
    if (type === 'candidates') {
        ghostCandidates = (data.candidates || []).map(c => ({
            x: c.x, y: c.y, pct: c.pct, color: 'white',
            pv: c.pv || null  // PV line: [[x,y,'w'|'b'], ...]
        }));
        ghostBest = data.best || null;
    } else if (type === 'phase') {
        const phases = {
            'threat_scan': '위협 분석 중...',
            'vcf_scan': '강제승 탐색 중...',
            'search': '최적 수 탐색 중...'
        };
        const phaseText = phases[data.phase] || '';
        if (phaseText) {
            const thinkEl = document.getElementById('aiThinking');
            if (thinkEl) thinkEl.textContent = phaseText;
        }
    } else if (type === 'done') {
        if (data.move) {
            const donePv = data.pv || null;
            ghostCandidates = [{
                x: data.move[0], y: data.move[1], pct: 100, color: 'white',
                pv: donePv
            }];
            ghostBest = data.move;
            lastPvLine = null;
        }
    }
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function runAiMoveFallback() {
    try {
        const res = await fetch('/api/ai/move', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ difficulty })
        });
        const data = await res.json();

        if (data.status === 'win' || data.status === 'continue') {
            boardState = data.board;
            currentTurn = data.turn || 1;
            if (data.ai_move) {
                moveHistory.push({
                    x: data.ai_move[0],
                    y: data.ai_move[1],
                    color: 'white',
                    move: moveHistory.length + 1
                });
            }
            drawBoard();
            updateStatus();
            if (data.status === 'win') {
                endGame(data.winner);
            }
        } else {
            showMessage('AI 오류', 'error');
        }
    } catch (e) {
        showMessage('통신 오류', 'error');
    }
}

async function apiMove(x, y) {
    try {
        const res = await fetch('/api/move', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ x, y })
        });
        const data = await res.json();

        if (data.status === 'continue' || data.status === 'win' || data.status === 'draw') {
            boardState = data.board;
            currentTurn = data.turn || 2;
            moveHistory.push({ x, y, color: 'black', move: moveHistory.length + 1 });
            drawBoard();
            updateStatus();
            document.getElementById('moveCount').textContent = moveHistory.length;
        }
        return data;
    } catch (e) {
        showMessage('통신 오류', 'error');
        return null;
    }
}

function endGame(winner) {
    gameOver = true;
    const turnLabel = document.getElementById('turnLabel');
    if (turnLabel) turnLabel.textContent = '게임 종료';
    if (winner === 'black') {
        showMessage('승리', 'win');
    } else if (winner === 'draw') {
        showMessage('무승부', 'lose');
    } else {
        showMessage('패배', 'lose');
    }
}

async function newGame() {
    gameOver = false;
    moveHistory = [];
    lastPvLine = null;
    stopGhostAnimation();

    try {
        const res = await fetch('/api/board/new', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify({ difficulty: MAX_DIFFICULTY })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            boardState = data.board;
            currentTurn = 1;
            drawBoard();
            updateStatus();
            showMessage('');
        }
    } catch (e) {
        showMessage('게임 시작 실패', 'error');
    }
}

// ══════════════════════════════════════════════════
// 게임 진행 중 확인
// ══════════════════════════════════════════════════
function isGameInProgress() {
    return moveHistory.length > 0 && !gameOver;
}

// ══════════════════════════════════════════════════
// 기보 목록 / 재생
// ══════════════════════════════════════════════════
async function loadRecords() {
    const listEl = document.getElementById('recordList');
    if (!listEl) return;
    listEl.innerHTML = '<div class="record-item"><span>불러오는 중...</span></div>';

    try {
        const res = await fetch('/api/records', { credentials: 'include' });
        const data = await res.json();
        listEl.innerHTML = '';

        if (!data.records || data.records.length === 0) {
            listEl.innerHTML = '<div class="record-item"><span>기록 없음</span></div>';
            return;
        }
        data.records.forEach(rec => {
            const item = document.createElement('div');
            item.className = 'record-item';
            const resultText = rec.winner === 'black' ? '승리' : rec.winner === 'draw' ? '무승부' : '패배';
            item.innerHTML = `
                <div class="record-info">
                    <span class="record-result">${resultText}</span>
                    <span class="record-time">${rec.timestamp || ''}</span>
                </div>
                <span class="record-moves">${rec.move_count || '-'}수</span>
            `;
            item.addEventListener('click', () => loadReplay(rec.id));
            listEl.appendChild(item);
        });
    } catch (e) {
        listEl.innerHTML = '<div class="record-item"><span>불러오기 실패</span></div>';
    }
}

async function loadReplay(recordId) {
    try {
        const res = await fetch(`/api/records/${recordId}`, { credentials: 'include' });
        const data = await res.json();
        if (data.status === 'ok' && data.record) {
            replayMoves = data.record.moves || [];
            currentReplayMove = 0;
            replayBoardState = Array.from({ length: BOARD_SIZE }, () => new Array(BOARD_SIZE).fill(0));
            stopAutoPlay();
            showSection('replayView');
            document.getElementById('replayTotalMoves').textContent = replayMoves.length;
            document.getElementById('replayMoveNum').textContent = '0';
            const msg = data.record.winner === 'black' ? '승리' : data.record.winner === 'draw' ? '무승부' : '패배';
            document.getElementById('replayMessage').textContent = msg;
            drawBoard(true);
        }
    } catch (e) {
        console.error('기보 로드 실패', e);
    }
}

function replayGoFirst() {
    currentReplayMove = 0;
    updateReplayBoard();
}

function replayGoLast() {
    currentReplayMove = replayMoves.length;
    updateReplayBoard();
}

function prevMove() {
    if (currentReplayMove <= 0) return;
    currentReplayMove--;
    updateReplayBoard();
}

function nextMove() {
    if (currentReplayMove >= replayMoves.length) return;
    currentReplayMove++;
    updateReplayBoard();
}

function toggleAutoPlay() {
    if (replayAutoTimer) {
        stopAutoPlay();
    } else {
        startAutoPlay();
    }
}

function startAutoPlay() {
    if (currentReplayMove >= replayMoves.length) {
        currentReplayMove = 0;
        updateReplayBoard();
    }
    const btn = document.getElementById('replayAutoPlay');
    if (btn) {
        btn.textContent = '정지';
        btn.classList.add('playing');
    }
    replayAutoTimer = setInterval(() => {
        if (currentReplayMove >= replayMoves.length) {
            stopAutoPlay();
            return;
        }
        currentReplayMove++;
        updateReplayBoard();
    }, 800);
}

function stopAutoPlay() {
    if (replayAutoTimer) {
        clearInterval(replayAutoTimer);
        replayAutoTimer = null;
    }
    const btn = document.getElementById('replayAutoPlay');
    if (btn) {
        btn.textContent = '재생';
        btn.classList.remove('playing');
    }
}

function updateReplayBoard() {
    replayBoardState = Array.from({ length: BOARD_SIZE }, () => new Array(BOARD_SIZE).fill(0));
    for (let i = 0; i < currentReplayMove; i++) {
        const m = replayMoves[i];
        if (replayBoardState[m.y] !== undefined) {
            replayBoardState[m.y][m.x] = m.color === 'black' ? 1 : 2;
        }
    }
    document.getElementById('replayMoveNum').textContent = currentReplayMove;
    drawBoard(true);
}

// ══════════════════════════════════════════════════
// 전적 확인
// ══════════════════════════════════════════════════
async function loadStats() {
    try {
        const res = await fetch('/api/stats', { credentials: 'include' });
        const data = await res.json();
        if (data.status === 'ok' && data.stats) {
            const s = data.stats;
            document.getElementById('statTotal').textContent = s.total;
            document.getElementById('statWins').textContent = s.wins;
            document.getElementById('statLosses').textContent = s.losses;
            document.getElementById('statDraws').textContent = s.draws;
            document.getElementById('statWinRate').textContent = s.win_rate + '%';
        }
    } catch (e) {
        console.error('전적 로드 실패', e);
    }
}

async function clearRecords() {
    if (!confirm('모든 전적과 기보를 초기화하시겠습니까?')) return;
    try {
        const res = await fetch('/api/records/clear', { method: 'POST', credentials: 'include' });
        const data = await res.json();
        if (data.status === 'ok') {
            await loadStats();
        }
    } catch (e) {
        console.error('초기화 실패', e);
    }
}

// ══════════════════════════════════════════════════
// 화면 전환
// ══════════════════════════════════════════════════
function showSection(id) {
    ['mainMenu', 'gameBoard', 'replaySelect', 'replayView', 'statsView'].forEach(sid => {
        const el = document.getElementById(sid);
        if (!el) return;
        if (sid === id) {
            el.classList.remove('hidden');
        } else {
            el.classList.add('hidden');
        }
    });
    if (id === 'gameBoard') {
        setTimeout(() => { resizeCanvas('game'); drawBoard(); }, 30);
    }
    if (id === 'replayView') {
        setTimeout(() => { resizeCanvas('replay'); drawBoard(true); }, 30);
    }
}

// ══════════════════════════════════════════════════
// 초기화
// ══════════════════════════════════════════════════
function init() {
    canvas = document.getElementById('board');
    ctx = canvas ? canvas.getContext('2d') : null;
    replayCanvas = document.getElementById('replayBoard');
    replayCtx = replayCanvas ? replayCanvas.getContext('2d') : null;

    resizeCanvas('both');

    if (canvas) canvas.addEventListener('click', handleCanvasClick);

    let resizeTimer = null;
    window.addEventListener('resize', () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {
            const gameVisible = !document.getElementById('gameBoard').classList.contains('hidden');
            const replayVisible = !document.getElementById('replayView').classList.contains('hidden');
            if (gameVisible) { resizeCanvas('game'); drawBoard(); }
            if (replayVisible) { resizeCanvas('replay'); drawBoard(true); }
        }, 100);
    });

    // 메인 메뉴 버튼
    document.getElementById('btnStartGame')?.addEventListener('click', async () => {
        await newGame();
        showSection('gameBoard');
    });
    document.getElementById('btnReplay')?.addEventListener('click', async () => {
        await loadRecords();
        showSection('replaySelect');
    });
    document.getElementById('btnStats')?.addEventListener('click', async () => {
        await loadStats();
        showSection('statsView');
    });

    // 게임 컨트롤
    document.getElementById('newGameBtn')?.addEventListener('click', async () => {
        if (isGameInProgress()) {
            if (!confirm('게임을 그만둘까요?')) return;
        }
        await newGame();
    });
    document.getElementById('menuBtn')?.addEventListener('click', () => {
        if (isGameInProgress()) {
            if (!confirm('게임을 그만둘까요?')) return;
        }
        stopGhostAnimation();
        showSection('mainMenu');
    });

    // 기보 목록
    document.getElementById('backFromReplay')?.addEventListener('click', () => showSection('mainMenu'));
    document.getElementById('backToReplayList')?.addEventListener('click', async () => {
        stopAutoPlay();
        await loadRecords();
        showSection('replaySelect');
    });

    // 기보 재생 컨트롤
    document.getElementById('replayFirst')?.addEventListener('click', replayGoFirst);
    document.getElementById('prevMove')?.addEventListener('click', prevMove);
    document.getElementById('replayAutoPlay')?.addEventListener('click', toggleAutoPlay);
    document.getElementById('nextMove')?.addEventListener('click', nextMove);
    document.getElementById('replayLast')?.addEventListener('click', replayGoLast);

    // 전적
    document.getElementById('backFromStats')?.addEventListener('click', () => showSection('mainMenu'));
    document.getElementById('btnClearRecords')?.addEventListener('click', clearRecords);

    // 빈 보드 초기 렌더
    boardState = Array.from({ length: BOARD_SIZE }, () => new Array(BOARD_SIZE).fill(0));
    resizeCanvas('game');
    drawBoard();
}

document.addEventListener('DOMContentLoaded', init);
