const box = document.getElementById('playerState');
let last = null;
let isTyping = false;
let lastPhase = null;

async function api() {
  const r = await fetch('/api/state/' + window.INVITE_CODE);
  const s = await r.json();
  last = s;
  render(s);
}

function timer(dead) {
  if (!dead) return '';

  const deadline = Number(dead);
  const now = Math.floor(Date.now() / 1000);
  const left = Math.max(0, deadline - now);

  return `<div class="timer">${left} сек</div>`;
}

function qblock(s) {
  const q = s.question;
  if (!q) return '<p>Чекаємо питання.</p>';

  return `
    <div class="pill">Раунд ${q.round_no} · питання ${s.game.current_q + 1}</div>
    <div class="question">${q.text}</div>
    ${q.photo_url ? `<img class="photo" src="${q.photo_url}">` : ''}
  `;
}

function render(s) {
  const p = s.game.phase;

  // Не перемальовуємо поле, поки гравець пише відповідь.
  // Інакше textarea може скидати текст.
  if (isTyping && p === 'answering' && lastPhase === 'answering') {
    updateOnlyTimer(s);
    return;
  }

  lastPhase = p;
  let html = qblock(s);

  if (p === 'lobby' || p === 'setup') {
    html = '<p>Чекаємо старт гри.</p>';
  }

  if (p === 'question_preview') {
    html += `<p class="muted">Подивіться питання, подумайте, відповіді ще не відкриті.</p>`;
  }

  if (p === 'answering') {
    const currentValue = document.getElementById('ans')?.value || '';

    html += `
      <div id="timerHolder">${timer(s.game.answer_deadline)}</div>
      <textarea id="ans" placeholder="Твій варіант відповіді">${escapeHtml(currentValue)}</textarea>
      <button onclick="sendAnswer()">Відправити</button>
      <p id="answerMessage" class="muted"></p>
    `;
  }

  if (p === 'preview') {
    html += `
      <h2>Варіанти</h2>
      <p class="muted">Поки тільки читаємо. Голосування ще не почалось.</p>
      ${s.options.map((o, i) => `<button class="option" disabled>${i + 1}. ${escapeHtml(o.text)}</button>`).join('')}
    `;
  }

  if (p === 'voting') {
    html += `
      <div id="timerHolder">${timer(s.game.vote_deadline)}</div>
      <h2>Голосування</h2>
    `;

    html += s.options.map((o, i) => {
      const isOwn = o.type === 'player' && Number(o.player_id) === Number(s.player_id);

      if (isOwn) {
        return `<button class="option ownOption" disabled>${i + 1}. ${escapeHtml(o.text)} — твоя відповідь</button>`;
      }

      return `<button class="option" onclick="vote('${o.id}')">${i + 1}. ${escapeHtml(o.text)}</button>`;
    }).join('');
  }

  if (p === 'results') {
    html += `
      <h2>Відкриття відповідей</h2>
      ${s.revealed.length ? s.revealed.map(revealHtml).join('') : '<p class="muted">Ведуча відкриває відповіді по черзі.</p>'}
      ${s.game.scoreboard_visible ? scoreHtml(s.players) : ''}
    `;
  }

  if (p === 'finished') {
    html = '<h2>Фінал</h2>' + scoreHtml(s.players);
  }

  box.innerHTML = html;

  const ans = document.getElementById('ans');
  if (ans) {
    ans.addEventListener('focus', () => isTyping = true);
    ans.addEventListener('input', () => isTyping = true);
    ans.addEventListener('blur', () => isTyping = false);
  }
}

function updateOnlyTimer(s) {
  const holder = document.getElementById('timerHolder');
  if (!holder) return;

  const deadline = s.game.phase === 'answering' ? s.game.answer_deadline : s.game.vote_deadline;
  holder.innerHTML = timer(deadline);
}

async function sendAnswer() {
  const answer = document.getElementById('ans')?.value || '';

  const r = await fetch('/api/answer/' + window.INVITE_CODE, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({answer})
  });

  const data = await r.json();
  const msg = document.getElementById('answerMessage');

  if (data.ok) {
    if (msg) msg.textContent = 'Відповідь збережено ✅';
  } else {
    if (msg) msg.textContent = data.message || 'Не вдалося зберегти відповідь';
  }

  isTyping = false;
  api();
}

async function vote(option_id) {
  const r = await fetch('/api/vote/' + window.INVITE_CODE, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({option_id})
  });

  const data = await r.json();

  if (!data.ok && data.message) {
    alert(data.message);
  }

  api();
}

function av(p, size = '') {
  const v = (p && p.avatar) || '';

  if (v.startsWith('/static/') || v.startsWith('http')) {
    return `<span class="avatar ${size}" style="background-image:url('${v}')"></span>`;
  }

  return `<span class="avatar ${size}">${v || ((p && p.name) || '?')[0]}</span>`;
}

function revealHtml(r) {
  const author = r.type === 'correct'
    ? {name: 'Правильна відповідь', avatar: '✓'}
    : r.type === 'fake'
      ? r.author || {name: 'Фейк ведучої', avatar: '!'}
      : r.author;

  return `
    <div class="revealRow revealGrid">
      <div class="avatarLine">
        ${av(author, 'big')}
        <b>${author ? escapeHtml(author.name) : 'Невідомо'}</b>
      </div>

      <div>
        <span class="tag ${r.type === 'correct' ? 'correct' : r.type === 'fake' ? 'fake' : ''}">
          ${r.type === 'correct' ? 'Правильна' : r.type === 'fake' ? 'Фейк' : 'Гравець'}
        </span>
        <div class="answerBig">${escapeHtml(r.text)}</div>
      </div>

      <div>
        <b>Голосували:</b>
        ${r.voters.length
          ? `<div class="votersAvatars">${r.voters.map(v => `<div class="avatarLine smallVoter">${av(v, 'small')} <span>${escapeHtml(v.name)}</span></div>`).join('')}</div>`
          : '<p class="muted">Ніхто</p>'
        }
      </div>
    </div>
  `;
}

function scoreHtml(players) {
  const arr = [...players].sort((a, b) => b.score - a.score);

  return `
    <h2>Таблиця гравців</h2>
    ${arr.map((p, i) => `
      <div class="playerRow rank${i + 1}">
        <div class="avatarLine">
          ${av(p)}
          <b>${i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : i + 1 + '.'} ${escapeHtml(p.name)}</b>
        </div>
        <b>${p.score}</b>
      </div>
    `).join('')}
  `;
}

function escapeHtml(text) {
  return String(text ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

setInterval(() => {
  if (!isTyping) api();
}, 1500);

setInterval(() => {
  if (last && !isTyping) render(last);
}, 500);

api();
