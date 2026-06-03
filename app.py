import os, sqlite3, json, secrets, random, time
from datetime import datetime
from functools import wraps
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
DB_PATH = os.environ.get('DB_PATH', 'variants_game.db')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
GAME_PASSWORD = os.environ.get('GAME_PASSWORD', 'game123')
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}

ROUNDS = [6, 6, 5]
ANSWER_SECONDS = 60
VOTE_SECONDS = 45


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload(file):
    if not file or not getattr(file, 'filename', ''):
        return ''
    name = secure_filename(file.filename)
    if not name or '.' not in name:
        return ''
    ext = name.rsplit('.', 1)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return ''
    fname = f"{secrets.token_urlsafe(10)}.{ext}"
    path = os.path.join(UPLOAD_FOLDER, fname)
    file.save(path)
    return url_for('static', filename=f'uploads/{fname}')


def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    conn = g.pop('db', None)
    if conn:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS games (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      invite_code TEXT UNIQUE NOT NULL,
      title TEXT DEFAULT 'Гра Варіанти',
      host_avatar TEXT DEFAULT '',
      status TEXT DEFAULT 'setup',
      current_q INTEGER DEFAULT 0,
      phase TEXT DEFAULT 'setup',
      answer_deadline INTEGER,
      vote_deadline INTEGER,
      scoreboard_visible INTEGER DEFAULT 0,
      created_at TEXT NOT NULL,
      finished_at TEXT
    );
    CREATE TABLE IF NOT EXISTS questions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      game_id INTEGER NOT NULL,
      q_order INTEGER NOT NULL,
      round_no INTEGER NOT NULL,
      text TEXT NOT NULL,
      correct_answer TEXT NOT NULL,
      fake_answer TEXT NOT NULL,
      photo_url TEXT DEFAULT '',
      options_json TEXT DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS players (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      game_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      avatar TEXT DEFAULT '',
      score INTEGER DEFAULT 0,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS answers (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      game_id INTEGER NOT NULL,
      question_id INTEGER NOT NULL,
      player_id INTEGER NOT NULL,
      text TEXT NOT NULL,
      UNIQUE(question_id, player_id)
    );
    CREATE TABLE IF NOT EXISTS votes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      game_id INTEGER NOT NULL,
      question_id INTEGER NOT NULL,
      player_id INTEGER NOT NULL,
      option_id TEXT NOT NULL,
      UNIQUE(question_id, player_id)
    );
    CREATE TABLE IF NOT EXISTS revealed (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      game_id INTEGER NOT NULL,
      question_id INTEGER NOT NULL,
      option_id TEXT NOT NULL,
      UNIQUE(question_id, option_id)
    );
    CREATE TABLE IF NOT EXISTS points (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      game_id INTEGER NOT NULL,
      question_id INTEGER NOT NULL,
      player_id INTEGER NOT NULL,
      points INTEGER NOT NULL,
      UNIQUE(question_id, player_id)
    );
    ''')
    # migration for older local DBs
    cols = [row[1] for row in c.execute('PRAGMA table_info(games)').fetchall()]
    if 'host_avatar' not in cols:
        c.execute('ALTER TABLE games ADD COLUMN host_avatar TEXT DEFAULT ""')
    conn.commit(); conn.close()


init_db()


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('admin_ok'):
            return redirect(url_for('host_login'))
        return fn(*args, **kwargs)
    return wrapper


def current_game():
    return db().execute("SELECT * FROM games WHERE status!='archived' ORDER BY id DESC LIMIT 1").fetchone()


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


@app.route('/')
def index():
    return redirect(url_for('play_home'))


@app.route('/host', methods=['GET','POST'])
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
    session.clear(); return redirect(url_for('host_login'))


@app.route('/admin')
@admin_required
def admin():
    game = current_game()
    history = db().execute('SELECT * FROM games ORDER BY id DESC LIMIT 10').fetchall()
    return render_template('admin.html', game=game, history=history, rounds=ROUNDS)


@app.route('/admin/new', methods=['POST'])
@admin_required
def admin_new():
    title = request.form.get('title') or 'Гра Варіанти'
    host_avatar = save_upload(request.files.get('host_avatar'))
    code = make_invite()
    conn = db()
    cur = conn.execute(
        'INSERT INTO games(invite_code,title,host_avatar,status,phase,created_at) VALUES(?,?,?,?,?,?)',
        (code, title, host_avatar, 'setup', 'setup', datetime.now().strftime('%Y-%m-%d %H:%M'))
    )
    conn.commit()
    return redirect(url_for('admin_game', game_id=cur.lastrowid))



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


@app.route('/admin/game/<int:game_id>')
@admin_required
def admin_game(game_id):
    game = db().execute('SELECT * FROM games WHERE id=?',(game_id,)).fetchone()
    qs = get_questions(game_id)
    ps = players(game_id)
    invite = request.host_url.rstrip('/') + url_for('invite', code=game['invite_code'])
    return render_template('admin_game.html', game=game, questions=qs, players=ps, invite=invite, rounds=ROUNDS)


@app.route('/admin/game/<int:game_id>/questions', methods=['POST'])
@admin_required
def save_questions(game_id):
    data = request.form
    conn = db()
    conn.execute('DELETE FROM questions WHERE game_id=?',(game_id,))
    order = 0
    for i in range(sum(ROUNDS)):
        text = data.get(f'q_{i}', '').strip()
        correct = data.get(f'c_{i}', '').strip()
        fake = data.get(f'f_{i}', '').strip()
        photo = save_upload(request.files.get(f'pfile_{i}')) or data.get(f'p_{i}', '').strip()
        if text:
            round_no = 1 if i < 6 else 2 if i < 12 else 3
            conn.execute(
                'INSERT INTO questions(game_id,q_order,round_no,text,correct_answer,fake_answer,photo_url) VALUES(?,?,?,?,?,?,?)',
                (game_id, order, round_no, text, correct, fake, photo)
            )
            order += 1
    conn.execute('UPDATE games SET current_q=0, phase="lobby", status="active" WHERE id=?',(game_id,))
    conn.commit()
    return redirect(url_for('admin_game', game_id=game_id))


@app.route('/admin/game/<int:game_id>/action', methods=['POST'])
@admin_required
def game_action(game_id):
    action = request.form.get('action')
    conn = db(); game = conn.execute('SELECT * FROM games WHERE id=?',(game_id,)).fetchone(); q = get_current_question(game)
    if not q: return redirect(url_for('admin_game', game_id=game_id))
    now = int(time.time())
    if action == 'show_question':
        conn.execute('UPDATE games SET phase="question_preview", answer_deadline=NULL, vote_deadline=NULL, scoreboard_visible=0 WHERE id=?',(game_id,))
    elif action == 'start_answers':
        conn.execute('UPDATE games SET phase="answering", answer_deadline=? WHERE id=?',(now+ANSWER_SECONDS, game_id))
    elif action == 'show_options':
        build_options_for_question(game_id, q['id'])
        conn.execute('UPDATE games SET phase="preview", answer_deadline=NULL WHERE id=?',(game_id,))
    elif action == 'start_voting':
        conn.execute('UPDATE games SET phase="voting", vote_deadline=? WHERE id=?',(now+VOTE_SECONDS, game_id))
    elif action == 'finish_voting':
        calculate_points(game_id, q['id'])
        conn.execute('UPDATE games SET phase="results", vote_deadline=NULL WHERE id=?',(game_id,))
    elif action == 'show_scoreboard':
        conn.execute('UPDATE games SET scoreboard_visible=1 WHERE id=?',(game_id,))
    elif action == 'next_question':
        next_idx = game['current_q'] + 1
        total = len(get_questions(game_id))
        if next_idx >= total:
            conn.execute('UPDATE games SET phase="finished", status="finished", finished_at=? WHERE id=?',(datetime.now().strftime('%Y-%m-%d %H:%M'), game_id))
        else:
            conn.execute('UPDATE games SET current_q=?, phase="question_preview", scoreboard_visible=0 WHERE id=?',(next_idx, game_id))
    conn.commit()
    return redirect(url_for('admin_game', game_id=game_id))


@app.route('/admin/game/<int:game_id>/reveal', methods=['POST'])
@admin_required
def reveal(game_id):
    option_id = request.form.get('option_id')
    game = db().execute('SELECT * FROM games WHERE id=?',(game_id,)).fetchone(); q = get_current_question(game)
    if q and option_id:
        db().execute('INSERT OR IGNORE INTO revealed(game_id,question_id,option_id) VALUES(?,?,?)',(game_id,q['id'],option_id)); db().commit()
    return redirect(url_for('admin_game', game_id=game_id))


@app.route('/play')
def play_home():
    return render_template('play_home.html')


@app.route('/invite/<code>', methods=['GET','POST'])
def invite(code):
    game = get_game_by_code(code)
    if not game: return render_template('message.html', title='Гри немає', text='Такого інвайту не існує.')
    if request.method == 'POST':
        if request.form.get('game_password') != GAME_PASSWORD:
            return render_template('invite.html', game=game, error='Неправильний пароль гри')
        name = request.form.get('name','').strip()
        avatar = save_upload(request.files.get('avatar_file')) or request.form.get('avatar','').strip()
        if not name: return render_template('invite.html', game=game, error='Введи імʼя')
        cur = db().execute('INSERT INTO players(game_id,name,avatar,created_at) VALUES(?,?,?,?)',(game['id'],name,avatar,datetime.now().strftime('%H:%M')))
        db().commit(); session[f'player_{game["id"]}'] = cur.lastrowid
        return redirect(url_for('game_play', code=code))
    return render_template('invite.html', game=game)


@app.route('/game/<code>')
def game_play(code):
    game = get_game_by_code(code)
    if not game: return render_template('message.html', title='Гри немає', text='Такого інвайту не існує.')
    pid = session.get(f'player_{game["id"]}')
    if not pid: return redirect(url_for('invite', code=code))
    player = db().execute('SELECT * FROM players WHERE id=?',(pid,)).fetchone()
    return render_template('player_game.html', game=game, player=player)


@app.route('/api/state/<code>')
def api_state(code):
    game = get_game_by_code(code)
    if not game: return jsonify({'error':'not found'}),404
    q = get_current_question(game)
    return jsonify(pack_state(game, q, public=True))


@app.route('/api/admin_state/<int:game_id>')
@admin_required
def api_admin_state(game_id):
    game = db().execute('SELECT * FROM games WHERE id=?',(game_id,)).fetchone(); q=get_current_question(game)
    return jsonify(pack_state(game,q,public=False))


@app.route('/api/answer/<code>', methods=['POST'])
def api_answer(code):
    game = get_game_by_code(code)
    pid = session.get(f'player_{game["id"]}') if game else None

    if not game or not pid:
        return jsonify({'ok': False})

    now = int(time.time())

    if game['phase'] != 'answering':
        return jsonify({'ok': False, 'message': 'Зараз не етап відповідей'})

    if game['answer_deadline'] and now > int(game['answer_deadline']):
        return jsonify({'ok': False, 'message': 'Час на відповідь вийшов'})

    payload = request.get_json(silent=True) or {}
    text = (payload.get('answer') or '').strip()

    if text:
        db().execute(
            'INSERT OR REPLACE INTO answers(game_id,question_id,player_id,text) VALUES(?,?,?,?)',
            (game['id'], get_current_question(game)['id'], pid, text)
        )
        db().commit()

    return jsonify({'ok': True})


@app.route('/api/vote/<code>', methods=['POST'])
def api_vote(code):
    game = get_game_by_code(code)
    pid = session.get(f'player_{game["id"]}') if game else None

    if not game or not pid:
        return jsonify({'ok': False})

    if game['phase'] != 'voting':
        return jsonify({'ok': False, 'message': 'Зараз не голосування'})

    q = get_current_question(game)
    if not q:
        return jsonify({'ok': False})

    payload = request.get_json(silent=True) or {}
    opt = payload.get('option_id')

    options = json.loads(q['options_json'] or '[]')
    chosen = next((o for o in options if o['id'] == opt), None)

    if not chosen:
        return jsonify({'ok': False, 'message': 'Такого варіанту немає'})

    # Заборона голосувати за свою відповідь
    if chosen.get('type') == 'player' and int(chosen.get('player_id') or 0) == int(pid):
        return jsonify({'ok': False, 'message': 'Не можна голосувати за свою відповідь'})

    db().execute(
        'INSERT OR REPLACE INTO votes(game_id,question_id,player_id,option_id) VALUES(?,?,?,?)',
        (game['id'], q['id'], pid, opt)
    )
    db().commit()

    return jsonify({'ok': True})



def build_options_for_question(game_id, qid):
    q = db().execute('SELECT * FROM questions WHERE id=?',(qid,)).fetchone()
    opts=[]
    for a in db().execute('SELECT * FROM answers WHERE question_id=?',(qid,)).fetchall():
        opts.append({'id':f'p_{a["player_id"]}','type':'player','text':a['text'],'player_id':a['player_id']})
    if q['correct_answer']:
        opts.append({'id':'correct','type':'correct','text':q['correct_answer'],'player_id':None})
    if q['fake_answer']:
        opts.append({'id':'fake','type':'fake','text':q['fake_answer'],'player_id':None})
    random.shuffle(opts)
    db().execute('UPDATE questions SET options_json=? WHERE id=?',(json.dumps(opts,ensure_ascii=False),qid)); db().commit()


def calculate_points(game_id,qid):
    if db().execute('SELECT 1 FROM points WHERE question_id=? LIMIT 1',(qid,)).fetchone(): return
    q = db().execute('SELECT * FROM questions WHERE id=?',(qid,)).fetchone(); opts=json.loads(q['options_json'] or '[]'); byid={o['id']:o for o in opts}
    pts={p['id']:0 for p in players(game_id)}
    for v in db().execute('SELECT * FROM votes WHERE question_id=?',(qid,)).fetchall():
        o=byid.get(v['option_id'])
        if not o: continue
        if o['type']=='correct': pts[v['player_id']]=pts.get(v['player_id'],0)+2
        elif o['type']=='fake': pts[v['player_id']]=pts.get(v['player_id'],0)-1
        elif o['type']=='player' and o.get('player_id') != v['player_id']:
            pts[o['player_id']]=pts.get(o['player_id'],0)+1
    for pid,p in pts.items():
        db().execute('INSERT OR REPLACE INTO points(game_id,question_id,player_id,points) VALUES(?,?,?,?)',(game_id,qid,pid,p))
        db().execute('UPDATE players SET score=score+? WHERE id=?',(p,pid))
    db().commit()


def pack_state(game,q,public=True):
    ps=[dict(p) for p in players(game['id'])]
    opts=json.loads(q['options_json'] or '[]') if q else []
    revealed_ids=[r['option_id'] for r in db().execute('SELECT option_id FROM revealed WHERE question_id=?',(q['id'],)).fetchall()] if q else []
    revealed=[]
    for o in opts:
        if o['id'] in revealed_ids:
            voters=[]
            for v in db().execute('SELECT * FROM votes WHERE question_id=? AND option_id=?',(q['id'],o['id'])).fetchall():
                p=db().execute('SELECT * FROM players WHERE id=?',(v['player_id'],)).fetchone()
                if p: voters.append(dict(p))
            author = None
            if o['type'] == 'player':
                p = db().execute('SELECT * FROM players WHERE id=?', (o.get('player_id'),)).fetchone()
                author = dict(p) if p else None
            elif o['type'] == 'fake':
                author = {'name': 'Фейк ведучої', 'avatar': game['host_avatar'] or '!'}
            elif o['type'] == 'correct':
                author = {'name': 'Правильна відповідь', 'avatar': '✓'}
            revealed.append({**o, 'author': author, 'voters': voters})
    live_answers=[]; live_votes=[]
    if q:
        for a in db().execute('SELECT * FROM answers WHERE question_id=?',(q['id'],)).fetchall():
            p=db().execute('SELECT * FROM players WHERE id=?',(a['player_id'],)).fetchone()
            if p: live_answers.append({'player':dict(p),'answer':a['text']})
        for v in db().execute('SELECT * FROM votes WHERE question_id=?',(q['id'],)).fetchall():
            p=db().execute('SELECT * FROM players WHERE id=?',(v['player_id'],)).fetchone(); opt=next((o for o in opts if o['id']==v['option_id']),None)
            if p: live_votes.append({'player':dict(p),'option_text': opt['text'] if opt else ''})
    return {
        'game':dict(game),
        'player_id': session.get(f'player_{game["id"]}'),
        'question':dict(q) if q else None,
        'players':ps,
        'options':[{'id':o['id'],'text':o['text'],'type':o.get('type'),'player_id':o.get('player_id')} for o in opts],
        'revealed':revealed,
        'revealed_ids':revealed_ids,
        'live_answers':live_answers if not public else [],
        'live_votes':live_votes if not public else [],
        'points':[dict(x) for x in db().execute('SELECT players.name, points.points FROM points JOIN players ON players.id=points.player_id WHERE question_id=?',(q['id'],)).fetchall()] if q else []
    }


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT',5000)))
