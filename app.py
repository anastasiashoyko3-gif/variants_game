import os
import json
import secrets
import random
import time
import requests
from datetime import datetime
from functools import wraps

import psycopg
from psycopg.rows import dict_row
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

DATABASE_URL = os.environ.get('DATABASE_URL')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
GAME_PASSWORD = os.environ.get('GAME_PASSWORD', 'game123')

UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', os.path.join(app.root_path, 'static', 'uploads'))
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY')
SUPABASE_BUCKET = os.environ.get('SUPABASE_BUCKET', 'uploads')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
ROUNDS = [6, 6, 5]
ANSWER_SECONDS = 60
VOTE_SECONDS = 45
STATE_CACHE = {}
CACHE_TTL = 0.7


class Database:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=()):
        # Allows the old SQLite-style ? placeholders to work with PostgreSQL.
        query = query.replace('?', '%s')
        cur = self.conn.cursor()
        cur.execute(query, params)
        return cur

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def db():
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL не знайдено. Додай його в Render Environment Variables.')

    if 'db' not in g:
        conn = psycopg.connect(DATABASE_URL, sslmode='require', row_factory=dict_row)
        g.db = Database(conn)

    return g.db


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop('db', None)
    if conn:
        conn.close()


def init_db():
    if not DATABASE_URL:
        print('DATABASE_URL не заданий. Таблиці не створені автоматично.')
        return

    conn = psycopg.connect(DATABASE_URL, sslmode='require', row_factory=dict_row)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS games (
      id BIGSERIAL PRIMARY KEY,
      invite_code TEXT UNIQUE NOT NULL,
      title TEXT DEFAULT 'Гра Варіанти',
      host_avatar TEXT DEFAULT '',
      status TEXT DEFAULT 'setup',
      current_q INTEGER DEFAULT 0,
      phase TEXT DEFAULT 'setup',
      answer_deadline BIGINT,
      vote_deadline BIGINT,
      scoreboard_visible INTEGER DEFAULT 0,
      created_at TEXT NOT NULL,
      finished_at TEXT
    );

    CREATE TABLE IF NOT EXISTS questions (
      id BIGSERIAL PRIMARY KEY,
      game_id BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
      q_order INTEGER NOT NULL,
      round_no INTEGER NOT NULL,
      text TEXT NOT NULL,
      correct_answer TEXT NOT NULL,
      fake_answer TEXT NOT NULL,
      photo_url TEXT DEFAULT '',
      options_json TEXT DEFAULT '[]'
    );

    CREATE TABLE IF NOT EXISTS players (
      id BIGSERIAL PRIMARY KEY,
      game_id BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      avatar TEXT DEFAULT '',
      pin TEXT DEFAULT '',
      score INTEGER DEFAULT 0,
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS answers (
      id BIGSERIAL PRIMARY KEY,
      game_id BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
      question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
      player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
      text TEXT NOT NULL,
      UNIQUE(question_id, player_id)
    );

    CREATE TABLE IF NOT EXISTS votes (
      id BIGSERIAL PRIMARY KEY,
      game_id BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
      question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
      player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
      option_id TEXT NOT NULL,
      UNIQUE(question_id, player_id)
    );

    CREATE TABLE IF NOT EXISTS revealed (
      id BIGSERIAL PRIMARY KEY,
      game_id BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
      question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
      option_id TEXT NOT NULL,
      UNIQUE(question_id, option_id)
    );

    CREATE TABLE IF NOT EXISTS points (
      id BIGSERIAL PRIMARY KEY,
      game_id BIGINT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
      question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
      player_id BIGINT NOT NULL REFERENCES players(id) ON DELETE CASCADE,
      points INTEGER NOT NULL,
      UNIQUE(question_id, player_id)
    );
    ''')
    cur.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS pin TEXT DEFAULT '';")
    conn.commit()
    conn.close()


init_db()


def save_upload(file):
    if not file or not getattr(file, 'filename', ''):
        return ''

    name = secure_filename(file.filename)
    if not name or '.' not in name:
        return ''

    ext = name.rsplit('.', 1)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return ''

    filename = f'{secrets.token_urlsafe(16)}.{ext}'

    if SUPABASE_URL and SUPABASE_SERVICE_KEY:
        file_bytes = file.read()
        content_type = file.content_type or 'application/octet-stream'
        file_path = filename
        upload_url = f'{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{file_path}'

        response = requests.post(
            upload_url,
            headers={
                'apikey': SUPABASE_SERVICE_KEY,
                'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
                'Content-Type': content_type,
                'x-upsert': 'true',
            },
            data=file_bytes,
            timeout=30,
        )

        if response.status_code not in (200, 201):
            print('Supabase upload error:', response.status_code, response.text)
            return ''

        return f'{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{file_path}'

    local_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(local_path)
    return url_for('static', filename=f'uploads/{filename}')


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('admin_ok'):
            return redirect(url_for('host_login'))
        return fn(*args, **kwargs)
    return wrapper


def current_game():
    return db().execute("SELECT * FROM games WHERE status!='archived' ORDER BY id DESC LIMIT 1").fetchone()


def get_game(game_id):
    return db().execute('SELECT * FROM games WHERE id=?', (game_id,)).fetchone()


def get_game_by_code(code):
    return db().execute('SELECT * FROM games WHERE invite_code=?', (code,)).fetchone()


def get_questions(game_id):
    return db().execute('SELECT * FROM questions WHERE game_id=? ORDER BY q_order', (game_id,)).fetchall()


def get_current_question(game):
    qs = get_questions(game['id'])
    if not qs or game['current_q'] >= len(qs):
        return None
    return qs[game['current_q']]


def players(game_id):
    return db().execute('SELECT * FROM players WHERE game_id=? ORDER BY score DESC, id ASC', (game_id,)).fetchall()


def make_invite():
    return secrets.token_urlsafe(6).replace('-', '').replace('_', '')[:8]


def seconds_left(deadline):
    if not deadline:
        return None
    return max(0, int(deadline) - int(time.time()))


def clear_game_cache(game_id=None, code=None):
    if game_id is not None:
        STATE_CACHE.pop(f"admin:{game_id}", None)

    if code is not None:
        STATE_CACHE.pop(f"public:{code}", None)

    if game_id is not None:
        game = get_game(game_id)
        if game:
            STATE_CACHE.pop(f"public:{game['invite_code']}", None)


@app.route('/')
def index():
    return redirect(url_for('play_home'))


@app.route('/host', methods=['GET', 'POST'])
def host_login():
    if request.method == 'POST':
        if request.form.get('password') == ADMIN_PASSWORD:
            session['admin_ok'] = True
            return redirect(url_for('admin'))
        return render_template('host_login.html', error='Неправильний пароль')

    if session.get('admin_ok'):
        return redirect(url_for('admin'))

    return render_template('host_login.html')


@app.route('/host/logout')
def host_logout():
    session.clear()
    return redirect(url_for('host_login'))


@app.route('/admin')
@admin_required
def admin():
    game = current_game()
    history = db().execute('SELECT * FROM games ORDER BY id DESC LIMIT 20').fetchall()
    return render_template('admin.html', game=game, history=history, rounds=ROUNDS)


@app.route('/admin/new', methods=['POST'])
@admin_required
def admin_new():
    title = request.form.get('title') or 'Гра Варіанти'
    host_avatar = save_upload(request.files.get('host_avatar')) or request.form.get('host_avatar_text', '').strip()
    code = make_invite()

    conn = db()
    cur = conn.execute(
        'INSERT INTO games(invite_code,title,host_avatar,status,phase,created_at) VALUES(?,?,?,?,?,?) RETURNING id',
        (code, title, host_avatar, 'setup', 'setup', datetime.now().strftime('%Y-%m-%d %H:%M')),
    )
    conn.commit()
    new_game = cur.fetchone()
    return redirect(url_for('admin_game', game_id=new_game['id']))


@app.route('/admin/game/<int:game_id>')
@admin_required
def admin_game(game_id):
    game = get_game(game_id)
    if not game:
        return render_template('message.html', title='Гри немає', text='Такої гри не існує.')

    qs = get_questions(game_id)
    ps = players(game_id)
    invite = request.host_url.rstrip('/') + url_for('invite', code=game['invite_code'])
    return render_template('admin_game.html', game=game, questions=qs, players=ps, invite=invite, rounds=ROUNDS)


@app.route('/admin/game/<int:game_id>/delete', methods=['POST'])
@admin_required
def delete_game(game_id):
    conn = db()
    conn.execute('DELETE FROM votes WHERE game_id=?', (game_id,))
    conn.execute('DELETE FROM answers WHERE game_id=?', (game_id,))
    conn.execute('DELETE FROM revealed WHERE game_id=?', (game_id,))
    conn.execute('DELETE FROM points WHERE game_id=?', (game_id,))
    conn.execute('DELETE FROM questions WHERE game_id=?', (game_id,))
    conn.execute('DELETE FROM players WHERE game_id=?', (game_id,))
    conn.execute('DELETE FROM games WHERE id=?', (game_id,))
    conn.commit()
    return redirect(url_for('admin'))


def save_or_update_questions(game_id, reset_progress=True):
    data = request.form
    conn = db()

    # Editing questions resets gameplay state, because answers/votes/options may no longer match.
    conn.execute('DELETE FROM votes WHERE game_id=?', (game_id,))
    conn.execute('DELETE FROM answers WHERE game_id=?', (game_id,))
    conn.execute('DELETE FROM revealed WHERE game_id=?', (game_id,))
    conn.execute('DELETE FROM points WHERE game_id=?', (game_id,))
    conn.execute('DELETE FROM questions WHERE game_id=?', (game_id,))
    conn.execute('UPDATE players SET score=0 WHERE game_id=?', (game_id,))

    old_game = get_game(game_id)
    host_avatar = (
        save_upload(request.files.get('host_avatar'))
        or data.get('host_avatar_existing', '').strip()
        or (old_game['host_avatar'] if old_game else '')
    )
    title = data.get('title', '').strip() or 'Гра Варіанти'
    conn.execute('UPDATE games SET title=?, host_avatar=? WHERE id=?', (title, host_avatar, game_id))

    order = 0
    for i in range(sum(ROUNDS)):
        text = data.get(f'q_{i}', '').strip()
        correct = data.get(f'c_{i}', '').strip()
        fake = data.get(f'f_{i}', '').strip()
        old_photo = data.get(f'old_p_{i}', '').strip()
        photo = save_upload(request.files.get(f'pfile_{i}')) or data.get(f'p_{i}', '').strip() or old_photo

        if text:
            round_no = 1 if i < 6 else 2 if i < 12 else 3
            conn.execute(
                'INSERT INTO questions(game_id,q_order,round_no,text,correct_answer,fake_answer,photo_url) VALUES(?,?,?,?,?,?,?)',
                (game_id, order, round_no, text, correct, fake, photo),
            )
            order += 1

    if reset_progress:
        conn.execute(
            '''
            UPDATE games
            SET current_q=?, phase=?, status=?, answer_deadline=NULL, vote_deadline=NULL,
                scoreboard_visible=?, finished_at=NULL
            WHERE id=?
            ''',
            (0, 'lobby', 'active', 0, game_id),
        )

    conn.commit()


@app.route('/admin/game/<int:game_id>/questions', methods=['POST'])
@admin_required
def save_questions(game_id):
    save_or_update_questions(game_id, reset_progress=True)
    return redirect(url_for('admin_game', game_id=game_id))


@app.route('/admin/game/<int:game_id>/edit', methods=['POST'])
@admin_required
def edit_game(game_id):
    save_or_update_questions(game_id, reset_progress=True)
    return redirect(url_for('admin_game', game_id=game_id))


@app.route('/admin/game/<int:game_id>/action', methods=['POST'])
@admin_required
def game_action(game_id):
    action = request.form.get('action')
    conn = db()
    game = get_game(game_id)
    if not game:
        return redirect(url_for('admin'))

    q = get_current_question(game)
    if not q:
        return redirect(url_for('admin_game', game_id=game_id))

    now = int(time.time())

    if action == 'show_question':
        conn.execute(
            'UPDATE games SET phase=?, answer_deadline=NULL, vote_deadline=NULL, scoreboard_visible=? WHERE id=?',
            ('question_preview', 0, game_id),
        )
    elif action == 'start_answers':
        conn.execute(
            'UPDATE games SET phase=?, answer_deadline=? WHERE id=?',
            ('answering', now + ANSWER_SECONDS, game_id),
        )
    elif action == 'show_options':
        build_options_for_question(game_id, q['id'])
        conn.execute('UPDATE games SET phase=?, answer_deadline=NULL WHERE id=?', ('preview', game_id))
    elif action == 'start_voting':
        conn.execute(
            'UPDATE games SET phase=?, vote_deadline=? WHERE id=?',
            ('voting', now + VOTE_SECONDS, game_id),
        )
    elif action == 'finish_voting':
        calculate_points(game_id, q['id'])
        conn.execute('UPDATE games SET phase=?, vote_deadline=NULL WHERE id=?', ('results', game_id))
    elif action == 'show_scoreboard':
        conn.execute('UPDATE games SET scoreboard_visible=? WHERE id=?', (1, game_id))
    elif action == 'next_question':
        next_idx = game['current_q'] + 1
        total = len(get_questions(game_id))
        if next_idx >= total:
            conn.execute(
                'UPDATE games SET phase=?, status=?, finished_at=? WHERE id=?',
                ('finished', 'finished', datetime.now().strftime('%Y-%m-%d %H:%M'), game_id),
            )
        else:
            conn.execute(
                'UPDATE games SET current_q=?, phase=?, scoreboard_visible=?, answer_deadline=NULL, vote_deadline=NULL WHERE id=?',
                (next_idx, 'question_preview', 0, game_id),
            )

    conn.commit()
    clear_game_cache(game_id=game_id)
    return redirect(url_for('admin_game', game_id=game_id))


@app.route('/admin/game/<int:game_id>/reveal', methods=['POST'])
@admin_required
def reveal(game_id):
    option_id = request.form.get('option_id')
    game = get_game(game_id)
    q = get_current_question(game) if game else None
    if q and option_id:
        db().execute(
            'INSERT INTO revealed(game_id,question_id,option_id) VALUES(?,?,?) ON CONFLICT(question_id, option_id) DO NOTHING',
            (game_id, q['id'], option_id),
        )
        db().commit()
        clear_game_cache(game_id=game_id)
    return redirect(url_for('admin_game', game_id=game_id))


@app.route('/play')
def play_home():
    return render_template('play_home.html')


@app.route('/invite/<code>', methods=['GET', 'POST'])
def invite(code):
    game = get_game_by_code(code)
    if not game:
        return render_template('message.html', title='Гри немає', text='Такого інвайту не існує.')

    if request.method == 'POST':
        if request.form.get('game_password') != GAME_PASSWORD:
            return render_template('invite.html', game=game, error='Неправильний пароль гри')

        name = request.form.get('name', '').strip()
        pin = request.form.get('pin', '').strip()

        if not name:
            return render_template('invite.html', game=game, error='Введи імʼя')

        if not pin:
            return render_template('invite.html', game=game, error='Введи PIN-код')

        existing = db().execute(
            'SELECT * FROM players WHERE game_id=? AND lower(name)=lower(?) AND pin=? LIMIT 1',
            (game['id'], name, pin),
        ).fetchone()

        if existing:
            session[f'player_{game["id"]}'] = existing['id']
            return redirect(url_for('game_play', code=code))

        avatar = save_upload(request.files.get('avatar_file')) or request.form.get('avatar', '').strip()
        cur = db().execute(
            'INSERT INTO players(game_id,name,avatar,pin,created_at) VALUES(?,?,?,?,?) RETURNING id',
            (game['id'], name, avatar, pin, datetime.now().strftime('%H:%M')),
        )
        db().commit()
        new_player = cur.fetchone()
        session[f'player_{game["id"]}'] = new_player['id']
        return redirect(url_for('game_play', code=code))

    return render_template('invite.html', game=game)


@app.route('/game/<code>')
def game_play(code):
    game = get_game_by_code(code)
    if not game:
        return render_template('message.html', title='Гри немає', text='Такого інвайту не існує.')

    pid = session.get(f'player_{game["id"]}')
    if not pid:
        return redirect(url_for('invite', code=code))

    player = db().execute('SELECT * FROM players WHERE id=?', (pid,)).fetchone()
    return render_template('player_game.html', game=game, player=player)


@app.route('/api/state/<code>')
def api_state(code):
    cache_key = f"public:{code}"
    now = time.time()

    cached = STATE_CACHE.get(cache_key)
    if cached and now - cached["time"] < CACHE_TTL:
        return jsonify(cached["data"])

    game = get_game_by_code(code)
    if not game:
        return jsonify({'error': 'not found'}), 404

    q = get_current_question(game)
    data = pack_state(game, q, public=True)

    STATE_CACHE[cache_key] = {
        "time": now,
        "data": data
    }

    return jsonify(data)

@app.route('/api/admin_state/<int:game_id>')
@admin_required
def api_admin_state(game_id):
    cache_key = f"admin:{game_id}"
    now = time.time()

    cached = STATE_CACHE.get(cache_key)
    if cached and now - cached["time"] < CACHE_TTL:
        return jsonify(cached["data"])

    game = get_game(game_id)
    q = get_current_question(game) if game else None
    data = pack_state(game, q, public=False)

    STATE_CACHE[cache_key] = {
        "time": now,
        "data": data
    }

    return jsonify(data)


@app.route('/api/answer/<code>', methods=['POST'])
def api_answer(code):
    game = get_game_by_code(code)
    pid = session.get(f'player_{game["id"]}') if game else None

    if not game or not pid:
        return jsonify({'ok': False})

    if game['phase'] != 'answering':
        return jsonify({'ok': False, 'message': 'Зараз не етап відповідей'})

    if seconds_left(game['answer_deadline']) == 0:
        return jsonify({'ok': False, 'message': 'Час на відповідь вийшов'})

    q = get_current_question(game)
    if not q:
        return jsonify({'ok': False})

    payload = request.get_json(silent=True) or {}
    text = (payload.get('answer') or '').strip()
    if not text:
        return jsonify({'ok': False, 'message': 'Спочатку напиши відповідь'})

    db().execute(
        'INSERT INTO answers(game_id,question_id,player_id,text) VALUES(?,?,?,?) ON CONFLICT(question_id, player_id) DO UPDATE SET text = EXCLUDED.text',
        (game['id'], q['id'], pid, text),
    )
    db().commit()
    clear_game_cache(game_id=game['id'], code=code)
    return jsonify({'ok': True})


@app.route('/api/vote/<code>', methods=['POST'])
def api_vote(code):
    game = get_game_by_code(code)
    pid = session.get(f'player_{game["id"]}') if game else None

    if not game or not pid:
        return jsonify({'ok': False})

    if game['phase'] != 'voting':
        return jsonify({'ok': False, 'message': 'Зараз не голосування'})

    if seconds_left(game['vote_deadline']) == 0:
        return jsonify({'ok': False, 'message': 'Час голосування вийшов'})

    q = get_current_question(game)
    if not q:
        return jsonify({'ok': False})

    payload = request.get_json(silent=True) or {}
    opt = payload.get('option_id')

    options = json.loads(q['options_json'] or '[]')
    chosen = next((o for o in options if o['id'] == opt), None)
    if not chosen:
        return jsonify({'ok': False, 'message': 'Такого варіанту немає'})

    if chosen.get('type') == 'player' and int(chosen.get('player_id') or 0) == int(pid):
        return jsonify({'ok': False, 'message': 'Не можна голосувати за свою відповідь'})

    db().execute(
        'INSERT INTO votes(game_id,question_id,player_id,option_id) VALUES(?,?,?,?) ON CONFLICT(question_id, player_id) DO UPDATE SET option_id = EXCLUDED.option_id',
        (game['id'], q['id'], pid, opt),
    )
    db().commit()
    clear_game_cache(game_id=game['id'], code=code)
    return jsonify({'ok': True})


def build_options_for_question(game_id, qid):
    q = db().execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
    opts = []

    for a in db().execute('SELECT * FROM answers WHERE question_id=?', (qid,)).fetchall():
        opts.append({'id': f'p_{a["player_id"]}', 'type': 'player', 'text': a['text'], 'player_id': a['player_id']})

    if q['correct_answer']:
        opts.append({'id': 'correct', 'type': 'correct', 'text': q['correct_answer'], 'player_id': None})

    if q['fake_answer']:
        opts.append({'id': 'fake', 'type': 'fake', 'text': q['fake_answer'], 'player_id': None})

    random.shuffle(opts)
    db().execute('UPDATE questions SET options_json=? WHERE id=?', (json.dumps(opts, ensure_ascii=False), qid))
    db().commit()


def calculate_points(game_id, qid):
    if db().execute('SELECT 1 FROM points WHERE question_id=? LIMIT 1', (qid,)).fetchone():
        return

    q = db().execute('SELECT * FROM questions WHERE id=?', (qid,)).fetchone()
    opts = json.loads(q['options_json'] or '[]')
    byid = {o['id']: o for o in opts}
    pts = {p['id']: 0 for p in players(game_id)}

    for v in db().execute('SELECT * FROM votes WHERE question_id=?', (qid,)).fetchall():
        o = byid.get(v['option_id'])
        if not o:
            continue
        if o['type'] == 'correct':
            pts[v['player_id']] = pts.get(v['player_id'], 0) + 2
        elif o['type'] == 'fake':
            pts[v['player_id']] = pts.get(v['player_id'], 0) - 1
        elif o['type'] == 'player' and o.get('player_id') != v['player_id']:
            pts[o['player_id']] = pts.get(o['player_id'], 0) + 1

    for pid, p in pts.items():
        db().execute(
            'INSERT INTO points(game_id,question_id,player_id,points) VALUES(?,?,?,?) ON CONFLICT(question_id, player_id) DO UPDATE SET points = EXCLUDED.points',
            (game_id, qid, pid, p),
        )
        db().execute('UPDATE players SET score=score+? WHERE id=?', (p, pid))

    db().commit()


def pack_state(game, q, public=True):
    ps = [dict(p) for p in players(game['id'])]
    player_id = session.get(f'player_{game["id"]}')
    opts = json.loads(q['options_json'] or '[]') if q else []

    revealed_ids = []
    revealed = []
    live_answers = []
    live_votes = []
    points_rows = []

    if q:
        qid = q['id']
        revealed_rows = db().execute('SELECT option_id FROM revealed WHERE question_id=?', (qid,)).fetchall()
        revealed_ids = [r['option_id'] for r in revealed_rows]

        all_votes = db().execute('SELECT * FROM votes WHERE question_id=?', (qid,)).fetchall()
        all_answers = db().execute('SELECT * FROM answers WHERE question_id=?', (qid,)).fetchall()

        players_by_id = {p['id']: p for p in ps}
        opts_by_id = {o['id']: o for o in opts}

        for a in all_answers:
            p = players_by_id.get(a['player_id'])
            if p:
                live_answers.append({'player': dict(p), 'answer': a['text']})

        for v in all_votes:
            p = players_by_id.get(v['player_id'])
            opt = opts_by_id.get(v['option_id'])
            if p:
                live_votes.append({'player': dict(p), 'option_text': opt['text'] if opt else ''})

        for o in opts:
            if o['id'] not in revealed_ids:
                continue
            voters = []
            for v in all_votes:
                if v['option_id'] == o['id']:
                    p = players_by_id.get(v['player_id'])
                    if p:
                        voters.append(dict(p))

            if o['type'] == 'player':
                author_player = players_by_id.get(o.get('player_id'))
                author = dict(author_player) if author_player else None
            elif o['type'] == 'fake':
                author = {'name': 'Фейк ведучої', 'avatar': game['host_avatar'] or '!'}
            elif o['type'] == 'correct':
                author = {'name': 'Правильна відповідь', 'avatar': '✓'}
            else:
                author = None

            revealed.append({**o, 'author': author, 'voters': voters})

        points_rows = [
            dict(x)
            for x in db().execute(
                '''
                SELECT players.name, points.points
                FROM points
                JOIN players ON players.id = points.player_id
                WHERE points.question_id=?
                ''',
                (qid,),
            ).fetchall()
        ]

    return {
        'server_now': int(time.time()),
        'answer_left': seconds_left(game['answer_deadline']),
        'vote_left': seconds_left(game['vote_deadline']),
        'game': dict(game),
        'player_id': player_id,
        'question': dict(q) if q else None,
        'players': ps,
        'options': [
            {'id': o['id'], 'text': o['text'], 'type': o.get('type'), 'player_id': o.get('player_id')}
            for o in opts
        ],
        'revealed': revealed,
        'revealed_ids': revealed_ids,
        'live_answers': live_answers if not public else [],
        'live_votes': live_votes if not public else [],
        'points': points_rows,
    }


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
