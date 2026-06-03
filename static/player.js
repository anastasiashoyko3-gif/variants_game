const box = document.getElementById('playerState');
let last = null;
let lastKey = '';
let poll = null;

async function api(force = false) {
  try {
    const r = await fetch('/api/state/' + window.INVITE_CODE, {cache: 'no-store'});
    const s = await r.json();
    last = s;
    render(s, force);
  } catch (e) {
    box.innerHTML = '<p class="error">Не вдалося завантажити гру. Онови сторінку.</p>';
    console.error(e);
  }
}

function stateKey(s) {
  const g = s.game || {};
  const q = s.question || {};
  return [g.phase, g.current_q, q.id || '', (s.options || []).length, (s.revealed || []).length, g.scoreboard_visible].join('|');
}

function timeLeft(s) {
  if (s.game.phase === 'answering') return Number(s.answer_left ?? 0);
  if (s.game.phase === 'voting') return Number(s.vote_left ?? 0);
  return null;
}

function timer(left) {
  if (left === null || left === undefined) return '';
  return `<div class="timer" id="timerBox">${Math.max(0, Number(left))} сек</div>`;
}

function updateTimer() {
  const t = document.getElementById('timerBox');
  if (!t || !last) return;
  const left = timeLeft(last);
  if (left === null) return;
  t.textContent = `${Math.max(0, left)} сек`;
}

function qblock(s) {
  const q = s.question;
  if (!q) return '<p>Чекаємо питання.</p>';
  return `<div class="pill">Раунд ${q.round_no} · питання ${s.game.current_q + 1}</div><div class="question">${escapeHtml(q.text)}</div>${q.photo_url ? `<img class="photo" src="${q.photo_url}" alt="Фото до питання">` : ''}`;
}

function render(s, force = false) {
  const key = stateKey(s);
  const phase = s.game.phase;
  const ans = document.getElementById('ans');

  if (!force && key === lastKey && phase === 'answering' && ans) {
    updateTimer();
    return;
  }

  lastKey = key;
  let html = qblock(s);

  if (phase === 'lobby' || phase === 'setup') html = '<p>Чекаємо старт гри.</p>';

  if (phase === 'question_preview') html += `<p class="muted">Подивіться питання, подумайте, відповіді ще не відкриті.</p>`;

  if (phase === 'answering') {
    const left = Number(s.answer_left ?? 0);
    if (left <= 0) {
      html += `<div id="timerHolder">${timer(0)}</div><p class="muted">Час на відповідь вийшов.</p>`;
    } else {
      html += `<div id="timerHolder">${timer(left)}</div>
        <textarea id="ans" autocomplete="off" placeholder="Твій варіант відповіді"></textarea>
        <button type="button" onclick="sendAnswer()">Відправити</button>
        <p id="sendMsg" class="muted"></p>`;
    }
  }

  if (phase === 'preview') {
    html += `<h2>Варіанти</h2><p class="muted">Поки тільки читаємо. Голосування ще не почалось.</p>` +
      (s.options || []).map((o, i) => `<button class="option" disabled>${i + 1}. ${escapeHtml(o.text)}</button>`).join('');
  }

  if (phase === 'voting') {
    const left = Number(s.vote_left ?? 0);
    html += `<div id="timerHolder">${timer(left)}</div><h2>Голосування</h2>`;
    html += (s.options || []).map((o, i) => {
      const isOwn = o.type === 'player' && Number(o.player_id) === Number(s.player_id);
      if (left <= 0) return `<button class="option" disabled>${i + 1}. ${escapeHtml(o.text)}</button>`;
      if (isOwn) return `<button class="option ownOption" disabled>${i + 1}. ${escapeHtml(o.text)} — твоя відповідь</button>`;
      return `<button class="option" onclick="vote('${o.id}')">${i + 1}. ${escapeHtml(o.text)}</button>`;
    }).join('');
    if (left <= 0) html += '<p class="muted">Час голосування вийшов.</p>';
  }

  if (phase === 'results') {
    html += `<h2>Відкриття відповідей</h2>` +
      ((s.revealed || []).length ? s.revealed.map(revealHtml).join('') : '<p class="muted">Ведуча відкриває відповіді по черзі.</p>') +
      (s.game.scoreboard_visible ? scoreHtml(s.players || []) : '');
  }

  if (phase === 'finished') html = '<h2>Фінал</h2>' + scoreHtml(s.players || []);

  box.innerHTML = html;
}

async function sendAnswer() {
  const ans = document.getElementById('ans');
  const msg = document.getElementById('sendMsg');
  const answer = ans ? ans.value.trim() : '';
  if (!answer) { if (msg) msg.textContent = 'Спочатку напиши відповідь.'; return; }

  const r = await fetch('/api/answer/' + window.INVITE_CODE, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({answer})
  });
  const data = await r.json();
  if (msg) msg.textContent = data.ok ? 'Відповідь збережено 💜' : (data.message || 'Не вдалося надіслати');
  await api(true);
}

async function vote(option_id) {
  const r = await fetch('/api/vote/' + window.INVITE_CODE, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({option_id})
  });
  const data = await r.json();
  if (!data.ok && data.message) alert(data.message);
  await api(true);
}

function av(p, size = '') {
  const v = (p && p.avatar) || '';
  const cls = `avatar ${size}`.trim();
  if (v.startsWith('http') || v.startsWith('/static/')) return `<span class="${cls}" style="background-image:url('${v}')"></span>`;
  return `<span class="${cls}">${escapeHtml(v || ((p && p.name) || '?')[0])}</span>`;
}

function revealHtml(r) {
  const author = r.author || (r.type === 'correct' ? {name: 'Правильна відповідь', avatar: '✓'} : r.type === 'fake' ? {name: 'Фейк ведучої', avatar: '!'} : null);
  return `<div class="revealRow revealGrid">
    <div class="avatarLine authorSide">${av(author, 'big')}<b>${author ? escapeHtml(author.name) : 'Невідомо'}</b></div>
    <div><span class="tag ${r.type === 'correct' ? 'correct' : r.type === 'fake' ? 'fake' : ''}">${r.type === 'correct' ? 'Правильна' : r.type === 'fake' ? 'Фейк' : 'Гравець'}</span><div class="answerBig">${escapeHtml(r.text)}</div></div>
    <div><b>Голосували:</b><div class="votersAvatars">${r.voters.length ? r.voters.map(v => `<div class="avatarLine voterMini">${av(v, 'small')}<span>${escapeHtml(v.name)}</span></div>`).join('') : '<p class="muted">Ніхто</p>'}</div></div>
  </div>`;
}

function scoreHtml(players) {
  const arr = [...players].sort((a, b) => b.score - a.score);
  return `<h2>Таблиця гравців</h2>` + arr.map((p, i) => `<div class="playerRow rank${i + 1}"><div class="avatarLine">${av(p)}<b>${i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : i + 1 + '.'} ${escapeHtml(p.name)}</b></div><b>${p.score}</b></div>`).join('');
}

function escapeHtml(text) { return String(text ?? '').replace(/[&<>'"]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#039;','"':'&quot;'}[m])); }

setInterval(api, 2500);
api(true);
