"""
AI Chat Platform — Serverless-ready, single file (app.py).

Всё в одном файле: HTML/CSS/JS шаблоны, модели, роуты, AI-клиент.
Подходит для деплоя на любой WSGI-serverless:
  - Vercel  (vercel.json -> @vercel/python, builds src=app.py)
  - Railway / Render / Fly.io
  - AWS Lambda + API Gateway (через WSGI-обёртку)
  - Google Cloud Run

ENV-переменные (все опциональны — есть dev-фоллбэки):
  SECRET_KEY            — ключ для подписи cookie-сессий Flask
  DATABASE_URL          — строка подключения к PostgreSQL
  ANTHROPIC_API_KEY     — ключ Anthropic SDK
  ANTHROPIC_BASE_URL    — кастомный base_url провайдера
  FILES_ROOT            — путь к папке для сохранения кода (по умолчанию /tmp/ai-chat-files)
  PORT                  — порт для локального запуска (default 5000)

Локальный запуск:
    pip install -r requirements.txt
    python app.py
"""

import os
import re
import time
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, render_template_string, request, jsonify,
    redirect, url_for, session, flash,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from anthropic import Anthropic
from sqlalchemy.exc import OperationalError


# ============== Config ==============
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get(
    'SECRET_KEY',
    'dev-secret-change-me-please-7f3a9c2b8e1d4f6a',
)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL',
    'postgresql://bothost_db_06a851292493:ToKKxst8x1doT6bVcHgfjr7AV8czk0jGA86XlKI0zyo'
    '@node1.pghost.ru:15918/bothost_db_06a851292493',
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 280,
}
# Жёсткий лимит загрузки — 10 МБ на запрос
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

db = SQLAlchemy(app)


# ============== Допустимые типы файлов для загрузки ==============
ALLOWED_TEXT_EXT = {
    '.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs',
    '.go', '.rs', '.java', '.kt', '.rb', '.php',
    '.c', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.cs', '.m', '.mm', '.swift',
    '.html', '.htm', '.css', '.scss', '.sass', '.less',
    '.md', '.markdown', '.rst', '.txt', '.log',
    '.json', '.jsonc', '.json5', '.yml', '.yaml', '.toml', '.ini', '.cfg', '.conf',
    '.sh', '.bash', '.zsh', '.ps1', '.bat', '.cmd',
    '.sql', '.graphql', '.gql', '.proto',
    '.env', '.gitignore', '.gitattributes', '.editorconfig',
    '.xml', '.svg', '.csv', '.tsv',
    '.dockerfile', 'dockerfile',
    '.lua', '.r', '.scala', '.clj', '.ex', '.exs', '.elm', '.dart',
    '.vue', '.svelte',
}
ALLOWED_MIME_PREFIXES = ('text/', 'application/json', 'application/xml',
                          'application/javascript', 'application/x-yaml')

# Сколько максимум файлов может держать один юзер
MAX_FILES_PER_USER = 20
# Сколько символов из каждого файла пихаем в контекст AI
MAX_FILE_CHARS_IN_CONTEXT = 60_000


# ============== AI Client ==============
AI_CLIENT = Anthropic(
    base_url=os.environ.get('ANTHROPIC_BASE_URL', 'https://api.smartapi.shop'),
    api_key=os.environ.get('ANTHROPIC_API_KEY', 'sk-smart-3XD55m5XyNjpez1edNzGkuaqvnnXs6qKm1pf5hQqHEA'),
    timeout=60.0,
)

MODELS = [
    ("sonnet-4.6",       "Sonnet 4.6"),
    ("deepseek-v4-flash","DeepSeek V4 Flash"),
    ("mimo-v2.5",        "Mimo V2.5"),
    ("minimax-m3",       "MiniMax M3"),
]
MODEL_IDS = {key: key for key, _ in MODELS}

# В serverless (Vercel, Lambda) писать можно только в /tmp — там ephemeral.
# Для persistent-хранилища замените на S3 / Vercel Blob / GCS и пробросьте через FILES_ROOT.
FILES_ROOT = os.environ.get(
    'FILES_ROOT',
    '/tmp/ai-chat-files' if os.path.isdir('/tmp') else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'files'
    ),
)
os.makedirs(FILES_ROOT, exist_ok=True)


# ============== Models ==============
class User(db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    messages      = db.relationship(
        'ChatMessage', backref='user', lazy=True, cascade='all, delete-orphan',
    )


class ChatMessage(db.Model):
    __tablename__ = 'chat_messages'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    role       = db.Column(db.String(20), nullable=False)
    content    = db.Column(db.Text, nullable=False)
    model      = db.Column(db.String(64))
    elapsed    = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class UploadedFile(db.Model):
    """Файл, загруженный юзером в текущую сессию. Содержимое
    инжектится в system-prompt AI при следующем сообщении."""
    __tablename__ = 'uploaded_files'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    filename   = db.Column(db.String(512), nullable=False)
    mime_type  = db.Column(db.String(128))
    size_bytes = db.Column(db.Integer, nullable=False)
    content    = db.Column(db.Text, nullable=False)  # до 10 МБ, PostgreSQL TEXT тянет 1 ГБ
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


# ============== Helpers ==============
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'auth_required'}), 401
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper


def extract_code_blocks(text: str) -> list:
    pattern = re.compile(r"```(?:[a-zA-Z0-9_+\-]*)\n(.*?)```", re.DOTALL)
    return [m.group(1).rstrip() for m in pattern.finditer(text)]


def safe_filename(name: str):
    name = (name or '').strip().replace('\\', '/')
    if not name or name.startswith('/'):
        return None
    parts = [p for p in name.split('/') if p and p not in ('.', '..')]
    if not parts:
        return None
    for p in parts:
        if not re.match(r'^[A-Za-z0-9._\-]+$', p):
            return None
    return '/'.join(parts)


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} КБ"
    return f"{n/1024/1024:.2f} МБ"


def is_allowed_upload(filename: str, mime: str) -> bool:
    """Проверяем расширение и MIME. Пускаем только текстовые/код."""
    base = (filename or '').lower()
    ext = '.' + base.rsplit('.', 1)[-1] if '.' in base else ''
    if ext in ALLOWED_TEXT_EXT:
        return True
    if base in ('dockerfile', 'makefile', 'rakefile', 'procfile'):
        return True
    if mime and any(mime.startswith(p) for p in ALLOWED_MIME_PREFIXES):
        return True
    return False


def build_files_context(user_id: int) -> str:
    """Собирает блок system-prompt из загруженных файлов юзера."""
    files = (UploadedFile.query
             .filter_by(user_id=user_id)
             .order_by(UploadedFile.created_at.asc())
             .all())
    if not files:
        return ""
    blocks = []
    for f in files:
        body = f.content or ""
        if len(body) > MAX_FILE_CHARS_IN_CONTEXT:
            body = body[:MAX_FILE_CHARS_IN_CONTEXT] + \
                   f"\n... (обрезано: показано {MAX_FILE_CHARS_IN_CONTEXT} из {len(f.content)} символов)"
        blocks.append(
            f"===== Файл: {f.filename} ({human_size(f.size_bytes)}) =====\n{body}"
        )
    return "\n\n".join(blocks)


# ============== Lazy DB init (serverless-safe) ==============
# Не дёргаем create_all() на импорт модуля — serverless функции не должны
# блокировать cold start побочными эффектами. Создаём таблицы лениво
# один раз на инстанс.
_db_initialized = False
_db_init_lock_until = 0.0


def ensure_db():
    """Ленивая инициализация таблиц. Идемпотентна, не падает если БД недоступна."""
    global _db_initialized, _db_init_lock_until
    if _db_initialized:
        return
    # простая защита от шторма ретраев в случае если БД ещё не поднялась
    if time.time() < _db_init_lock_until:
        return
    try:
        with app.app_context():
            db.create_all()
        _db_initialized = True
        print("[OK] DB tables ready", flush=True)
    except OperationalError as e:
        print(f"[WARN] DB init OperationalError: {e}", flush=True)
        _db_init_lock_until = time.time() + 30  # ретрай через 30с
    except Exception as e:
        print(f"[WARN] DB init error: {e}", flush=True)
        _db_init_lock_until = time.time() + 30


@app.before_request
def _ensure_db_before_request():
    ensure_db()


# ============== CSS (один блок для всех страниц) ==============
STYLE_CSS = r"""
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Helvetica Neue", Arial, sans-serif;
  background: #0f0f1a;
  color: #e6e6f0;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}
code, pre { font-family: "SF Mono", Monaco, Consolas, "Courier New", monospace; }

.app {
  display: flex; flex-direction: column; height: 100vh;
  max-width: 1000px; margin: 0 auto; background: #0f0f1a;
}

.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 20px; border-bottom: 1px solid #1f1f2e;
  background: #15152a; flex-shrink: 0;
}
.brand { font-weight: 700; font-size: 18px; letter-spacing: .2px; }
.topbar-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.model-label { color: #9d9dbf; font-size: 13px; }
.topbar-right .user { color: #9d9dbf; font-size: 13px; }
.topbar-right select {
  background: #1f1f2e; color: #e6e6f0; border: 1px solid #2a2a44;
  border-radius: 8px; padding: 6px 10px; font-size: 13px; cursor: pointer;
}
.topbar-right select:focus { outline: none; border-color: #7c3aed; }

.btn {
  background: #2a2a44; color: #e6e6f0; border: 1px solid #353560;
  border-radius: 8px; padding: 7px 14px; cursor: pointer; font-size: 13px;
  text-decoration: none; display: inline-block;
  transition: all .15s ease; font-family: inherit;
}
.btn:hover { background: #353560; }
.btn:active { transform: translateY(1px); }
.btn.primary {
  background: linear-gradient(135deg, #7c3aed, #4f46e5);
  border: none; color: #fff; font-weight: 600;
}
.btn.primary:hover { filter: brightness(1.12); }
.btn.primary:disabled { opacity: .5; cursor: not-allowed; filter: grayscale(.4); }
.btn.ghost { background: transparent; }
.btn.tiny { padding: 4px 10px; font-size: 12px; }

.messages {
  flex: 1; overflow-y: auto; padding: 20px;
  display: flex; flex-direction: column; gap: 14px;
  scrollbar-width: thin; scrollbar-color: #2a2a44 transparent;
}
.messages::-webkit-scrollbar { width: 8px; }
.messages::-webkit-scrollbar-thumb { background: #2a2a44; border-radius: 4px; }
.messages::-webkit-scrollbar-track { background: transparent; }

.empty {
  margin: auto; text-align: center; color: #9d9dbf; max-width: 460px;
}
.empty h2 { margin-bottom: 10px; color: #e6e6f0; }
.empty p { line-height: 1.5; }
.empty code {
  background: #2a2a44; padding: 1px 6px; border-radius: 4px; color: #c4b5fd;
}

.msg {
  max-width: 85%; padding: 12px 14px; border-radius: 12px; line-height: 1.5;
  background: #1a1a2e; border: 1px solid #232342; word-wrap: break-word;
}
.msg.user { align-self: flex-end; background: #2d1f5c; border-color: #4f3a9c; }
.msg.assistant { align-self: flex-start; }
.msg-head {
  display: flex; justify-content: space-between; font-size: 11px;
  color: #9d9dbf; margin-bottom: 6px;
  text-transform: uppercase; letter-spacing: .5px;
}
.msg-head .role { font-weight: 600; }
.msg-head .timer { color: #7c3aed; font-variant-numeric: tabular-nums; }
.msg-body { font-size: 14px; }
.msg-body p { margin: 6px 0; }
.msg-body p:first-child { margin-top: 0; }
.msg-body p:last-child { margin-bottom: 0; }
.msg-body h1, .msg-body h2, .msg-body h3 { margin: 10px 0 6px; line-height: 1.3; }
.msg-body h1 { font-size: 18px; }
.msg-body h2 { font-size: 16px; }
.msg-body h3 { font-size: 15px; }
.msg-body ul, .msg-body ol { margin: 6px 0 6px 22px; }
.msg-body li { margin: 2px 0; }
.msg-body a { color: #a78bfa; }
.msg-body pre {
  background: #0d0d1a; border-radius: 8px; padding: 12px; overflow-x: auto;
  margin: 8px 0; border: 1px solid #232342;
}
.msg-body pre code { font-size: 13px; background: transparent !important; padding: 0; }
.msg-body :not(pre) > code {
  background: #2a2a44; padding: 1px 6px; border-radius: 4px;
  font-size: 13px; color: #c4b5fd;
}
.msg-body blockquote {
  border-left: 3px solid #7c3aed; padding-left: 10px;
  color: #c4b5fd; margin: 8px 0;
}
.msg-actions {
  margin-top: 8px; display: flex; gap: 6px;
  opacity: .7; transition: opacity .15s;
}
.msg:hover .msg-actions { opacity: 1; }

.note {
  align-self: center; color: #7c3aed; font-size: 12px;
  padding: 4px 12px; background: #1f1f2e;
  border: 1px dashed #4f3a9c; border-radius: 6px;
}

.thinking {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 20px; color: #9d9dbf; font-size: 13px; flex-shrink: 0;
}
.thinking.hidden { display: none; }
.dots { display: inline-flex; gap: 4px; }
.thinking .dot {
  width: 7px; height: 7px; background: #7c3aed;
  border-radius: 50%; animation: bounce 1.2s infinite;
}
.thinking .dot:nth-child(2) { animation-delay: .2s; }
.thinking .dot:nth-child(3) { animation-delay: .4s; }
.thinking-text { color: #c4b5fd; }
.thinking-timer {
  color: #7c3aed; margin-left: auto;
  font-variant-numeric: tabular-nums; font-weight: 600;
}
@keyframes bounce {
  0%, 80%, 100% { transform: scale(.6); opacity: .4; }
  40% { transform: scale(1); opacity: 1; }
}

.composer {
  border-top: 1px solid #1f1f2e; padding: 14px 20px;
  background: #15152a; flex-shrink: 0;
}
#input {
  width: 100%; background: #1a1a2e; color: #e6e6f0;
  border: 1px solid #2a2a44; border-radius: 10px;
  padding: 10px 12px; font-size: 14px; resize: vertical;
  min-height: 60px; font-family: inherit; line-height: 1.5;
}
#input:focus { outline: none; border-color: #7c3aed; }
#input::placeholder { color: #6d6d8a; }
.composer-row { display: flex; gap: 8px; margin-top: 8px; align-items: stretch; }
#filename {
  flex: 1; background: #1a1a2e; color: #e6e6f0;
  border: 1px solid #2a2a44; border-radius: 8px;
  padding: 8px 12px; font-size: 13px;
  font-family: "SF Mono", Monaco, Consolas, monospace;
}
#filename:focus { outline: none; border-color: #7c3aed; }
#filename::placeholder { color: #6d6d8a; }

.attachments {
  display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px;
}
.attachments:empty { display: none; }
.chip {
  background: #1f1f2e; border: 1px solid #353560; border-radius: 16px;
  padding: 4px 6px 4px 10px; font-size: 12px;
  display: inline-flex; align-items: center; gap: 6px;
  color: #c4b5fd; max-width: 100%;
}
.chip .chip-name {
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: 200px;
}
.chip .chip-size { color: #9d9dbf; font-size: 11px; }
.chip button {
  background: transparent; border: none; color: #ff8da0;
  cursor: pointer; padding: 0 4px; font-size: 14px; line-height: 1;
  border-radius: 50%;
}
.chip button:hover { background: #2a2a44; }

.composer.dragover {
  background: #1a1a2e; box-shadow: inset 0 0 0 2px #7c3aed;
}
.composer.uploading { opacity: .8; pointer-events: none; }
.upload-progress {
  position: fixed; top: 20px; right: 20px; z-index: 1000;
  background: #15152a; border: 1px solid #7c3aed; border-radius: 8px;
  padding: 10px 16px; font-size: 13px; color: #c4b5fd;
  box-shadow: 0 4px 12px rgba(0,0,0,.4);
}

.auth-page {
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh; padding: 20px;
}
.auth-card {
  background: #15152a; border: 1px solid #232342; border-radius: 16px;
  padding: 32px; width: 100%; max-width: 380px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, .4);
}
.auth-card h1 { font-size: 24px; margin-bottom: 6px; }
.auth-card p.subtitle { color: #9d9dbf; font-size: 13px; margin-bottom: 22px; }
.auth-card label {
  display: block; font-size: 11px; color: #9d9dbf;
  margin: 14px 0 6px; text-transform: uppercase;
  letter-spacing: .8px; font-weight: 600;
}
.auth-card input {
  width: 100%; background: #1a1a2e; color: #e6e6f0;
  border: 1px solid #2a2a44; border-radius: 8px;
  padding: 10px 12px; font-size: 14px; font-family: inherit;
}
.auth-card input:focus { outline: none; border-color: #7c3aed; }
.auth-card button { width: 100%; margin-top: 22px; padding: 11px; font-size: 14px; }
.auth-card .alt { text-align: center; font-size: 13px; color: #9d9dbf; margin-top: 16px; }
.auth-card .alt a { color: #a78bfa; text-decoration: none; font-weight: 500; }
.auth-card .alt a:hover { text-decoration: underline; }

.flash {
  background: #3b1d3b; border: 1px solid #6b2a6b; color: #ffb3ff;
  padding: 9px 12px; border-radius: 8px; font-size: 13px; margin-bottom: 12px;
}

@media (max-width: 640px) {
  .topbar { padding: 10px 14px; flex-wrap: wrap; gap: 8px; }
  .topbar-right { gap: 6px; font-size: 12px; }
  .messages { padding: 14px; }
  .msg { max-width: 92%; }
  .composer { padding: 10px 14px; }
  .composer-row { flex-direction: column; }
  .auth-card { padding: 24px; }
}
"""

HLJS_CSS = (
    "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/"
    "atom-one-dark.min.css"
)


# ============== HTML-шаблоны ==============
LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — AI Чат</title>
<style>{{ STYLE_CSS }}</style>
<link rel="stylesheet" href="{{ HLJS_CSS }}">
</head>
<body>
<div class="auth-page">
  <form class="auth-card" method="POST" action="{{ url_for('login') }}">
    <h1>👋 С возвращением</h1>
    <p class="subtitle">Войдите, чтобы продолжить</p>
    {% with messages = get_flashed_messages() %}
      {% if messages %}{% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endif %}
    {% endwith %}
    <label>Логин</label>
    <input type="text" name="username" autocomplete="username" required autofocus>
    <label>Пароль</label>
    <input type="password" name="password" autocomplete="current-password" required>
    <button type="submit" class="btn primary">Войти</button>
    <p class="alt">Нет аккаунта? <a href="{{ url_for('register') }}">Зарегистрироваться</a></p>
  </form>
</div>
</body>
</html>
"""

REGISTER_HTML = """\
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Регистрация — AI Чат</title>
<style>{{ STYLE_CSS }}</style>
<link rel="stylesheet" href="{{ HLJS_CSS }}">
</head>
<body>
<div class="auth-page">
  <form class="auth-card" method="POST" action="{{ url_for('register') }}">
    <h1>🚀 Регистрация</h1>
    <p class="subtitle">Создайте аккаунт, чтобы начать</p>
    {% with messages = get_flashed_messages() %}
      {% if messages %}{% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endif %}
    {% endwith %}
    <label>Логин</label>
    <input type="text" name="username" minlength="3" autocomplete="username" required autofocus>
    <label>Пароль</label>
    <input type="password" name="password" minlength="4" autocomplete="new-password" required>
    <button type="submit" class="btn primary">Создать аккаунт</button>
    <p class="alt">Уже зарегистрированы? <a href="{{ url_for('login') }}">Войти</a></p>
  </form>
</div>
</body>
</html>
"""

CHAT_HTML = """\
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Чат — AI</title>
<style>{{ STYLE_CSS }}</style>
<link rel="stylesheet" href="{{ HLJS_CSS }}">
</head>
<body>
<div class="app">
  <header class="topbar">
    <div class="brand">🤖 AI Чат</div>
    <div class="topbar-right">
      <label class="model-label">Модель:</label>
      <select id="model">
        {% for key, label in models %}
          <option value="{{ key }}">{{ label }}</option>
        {% endfor %}
      </select>
      <span class="user">👤 {{ username }}</span>
      <button class="btn ghost" type="button" onclick="clearChat()">🗑 Очистить</button>
      <a class="btn ghost" href="{{ url_for('logout') }}">Выйти</a>
    </div>
  </header>

  <main id="messages" class="messages">
    {% if not messages %}
      <div class="empty">
        <h2>Привет, {{ username }}!</h2>
        <p>Напишите сообщение и выберите модель. Если хотите, чтобы код сохранился в файл — укажите имя файла (например <code>bot.py</code>) перед отправкой.</p>
      </div>
    {% endif %}
    {% for m in messages %}
      <div class="msg {{ m.role }}" data-id="{{ m.id }}">
        <div class="msg-head">
          <span class="role">{{ 'Ты' if m.role == 'user' else (m.model or 'AI') }}</span>
          {% if m.role == 'assistant' and m.elapsed is not none %}
            <span class="timer">⏱ {{ '%.2f'|format(m.elapsed) }}с</span>
          {% endif %}
        </div>
        <div class="msg-body">{{ m.content }}</div>
        {% if m.role == 'assistant' %}
          <div class="msg-actions">
            <button class="btn tiny" type="button" onclick="copyMsg(this)">📋 Копировать</button>
            <button class="btn tiny" type="button" onclick="saveMsg(this)">💾 Сохранить</button>
          </div>
        {% endif %}
      </div>
    {% endfor %}
  </main>

  <div id="thinking" class="thinking hidden">
    <div class="dots"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
    <span class="thinking-text">Думаю...</span>
    <span class="thinking-timer">0.0с</span>
  </div>

  <form id="composer" class="composer" onsubmit="return sendMessage(event)">
    <div id="attachments" class="attachments" hidden></div>
    <textarea id="input" placeholder="Напишите сообщение... (Shift+Enter — новая строка, или перетащи файл сюда)" rows="3" required></textarea>
    <div class="composer-row">
      <input id="filename" type="text" placeholder="💾 Сохранить код AI в файл (например bot.py) — необязательно">
      <label for="file-input" class="btn" title="Загрузить файл (≤10 МБ)">📎 Файл</label>
      <input id="file-input" type="file" hidden>
      <button type="submit" class="btn primary" id="send-btn">Отправить ➤</button>
    </div>
  </form>
</div>

<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.0.6/dist/purify.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/core.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/languages/python.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/languages/javascript.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/languages/bash.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/languages/json.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/languages/xml.min.js"></script>
<script>
  if (window.marked) {
    marked.setOptions({ breaks: true, gfm: true, headerIds: false, mangle: false });
    const renderer = new marked.Renderer();
    renderer.code = function(code, infostring) {
      const lang = (infostring || '').trim().split(/\\s+/)[0];
      let highlighted = code;
      if (window.hljs) {
        try {
          if (lang && hljs.getLanguage(lang)) {
            highlighted = hljs.highlight(code, { language: lang, ignoreIllegals: true }).value;
          } else {
            highlighted = hljs.highlightAuto(code).value;
          }
        } catch (e) { highlighted = escapeHtml(code); }
      } else {
        highlighted = escapeHtml(code);
      }
      return '<pre><code class="hljs language-' + escapeHtml(lang || 'plaintext') + '">' + highlighted + '</code></pre>';
    };
    marked.use({ renderer });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
  }

  document.querySelectorAll('.msg-body').forEach(el => {
    const raw = el.textContent;
    const html = window.DOMPurify
      ? DOMPurify.sanitize(marked.parse(raw))
      : marked.parse(raw);
    el.innerHTML = html;
  });

  let thinkingTimer = null;
  let thinkingStart = 0;
  function showThinking() {
    document.getElementById('thinking').classList.remove('hidden');
    document.getElementById('send-btn').disabled = true;
    thinkingStart = Date.now();
    const tEl = document.querySelector('.thinking-timer');
    thinkingTimer = setInterval(() => {
      tEl.textContent = ((Date.now() - thinkingStart) / 1000).toFixed(1) + 'с';
    }, 100);
  }
  function hideThinking() {
    document.getElementById('thinking').classList.add('hidden');
    document.getElementById('send-btn').disabled = false;
    if (thinkingTimer) clearInterval(thinkingTimer);
  }

  function appendMessage(role, content, model, elapsed) {
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + role;
    const head = '<div class="msg-head">'
      + '<span class="role">' + (role === 'user' ? 'Ты' : escapeHtml(model || 'AI')) + '</span>'
      + (elapsed != null ? '<span class="timer">⏱ ' + elapsed.toFixed(2) + 'с</span>' : '')
      + '</div>';
    const html = window.DOMPurify
      ? DOMPurify.sanitize(marked.parse(content || ''))
      : marked.parse(content || '');
    const body = '<div class="msg-body">' + html + '</div>';
    const actions = role === 'assistant'
      ? '<div class="msg-actions">'
        + '<button class="btn tiny" type="button" onclick="copyMsg(this)">📋 Копировать</button>'
        + '<button class="btn tiny" type="button" onclick="saveMsg(this)">💾 Сохранить</button>'
        + '</div>' : '';
    wrap.innerHTML = head + body + actions;
    document.getElementById('messages').appendChild(wrap);
    wrap.scrollIntoView({ behavior: 'smooth', block: 'end' });
    return wrap;
  }

  function appendNote(text) {
    const note = document.createElement('div');
    note.className = 'note';
    note.textContent = text;
    document.getElementById('messages').appendChild(note);
  }

  async function sendMessage(e) {
    e.preventDefault();
    const input = document.getElementById('input');
    const message = input.value.trim();
    if (!message) return false;

    const model = document.getElementById('model').value;
    const filename = document.getElementById('filename').value.trim();

    document.querySelector('.empty')?.remove();

    appendMessage('user', escapeHtml(message), null, null);
    input.value = '';
    showThinking();

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, model, filename }),
      });
      const data = await res.json();
      hideThinking();
      if (!res.ok) {
        appendMessage('assistant', '⚠️ ' + (data.error || 'Ошибка'), model, data.elapsed || null);
        return false;
      }
      appendMessage('assistant', data.response, data.model, data.elapsed);
      if (data.saved_to) {
        appendNote('💾 Сохранено в files/' + data.saved_to);
      }
    } catch (err) {
      hideThinking();
      appendMessage('assistant', '⚠️ ' + err.message, null, null);
    }
    return false;
  }

  function copyMsg(btn) {
    const body = btn.closest('.msg').querySelector('.msg-body');
    navigator.clipboard.writeText(body.innerText).then(() => {
      const orig = btn.textContent;
      btn.textContent = '✅ Скопировано';
      setTimeout(() => btn.textContent = orig, 1200);
    });
  }

  function saveMsg(btn) {
    const body = btn.closest('.msg').querySelector('.msg-body');
    const code = body.querySelector('pre code');
    const content = code ? code.innerText : body.innerText;
    const filename = prompt('Имя файла (например bot.py):', 'code.py');
    if (!filename) return;
    fetch('/api/save_file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, content }),
    })
      .then(r => r.json())
      .then(d => {
        if (d.ok) {
          const orig = btn.textContent;
          btn.textContent = '✅ ' + d.path;
          setTimeout(() => btn.textContent = orig, 1800);
        } else {
          alert('Ошибка: ' + d.error);
        }
      });
  }

  async function clearChat() {
    if (!confirm('Очистить всю историю?')) return;
    await fetch('/api/clear', { method: 'POST' });
    location.reload();
  }

  document.getElementById('input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      document.getElementById('composer').requestSubmit();
    }
  });

  // ============== File upload ==============
  const MAX_FILE_SIZE = 10 * 1024 * 1024;
  const fileInput = document.getElementById('file-input');
  const composerEl = document.getElementById('composer');
  const attachmentsEl = document.getElementById('attachments');

  function showToast(text) {
    let t = document.getElementById('upload-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'upload-toast';
      t.className = 'upload-progress';
      document.body.appendChild(t);
    }
    t.textContent = text;
    clearTimeout(t._hider);
    t._hider = setTimeout(() => t.remove(), 2500);
  }

  async function uploadFile(file) {
    if (file.size > MAX_FILE_SIZE) {
      alert('«' + file.name + '» больше 10 МБ (' + (file.size/1024/1024).toFixed(2) + ' МБ)');
      return;
    }
    const fd = new FormData();
    fd.append('file', file);
    composerEl.classList.add('uploading');
    showToast('⏫ Загружаю ' + file.name + '…');
    try {
      const r = await fetch('/api/upload', { method: 'POST', body: fd });
      const d = await r.json();
      if (!r.ok) { alert('Ошибка: ' + (d.error || '?')); return; }
      showToast('✅ ' + d.filename + ' (' + d.size_human + ')');
      await loadFiles();
    } catch (e) {
      alert('Сеть: ' + e.message);
    } finally {
      composerEl.classList.remove('uploading');
    }
  }

  fileInput.addEventListener('change', e => {
    const f = e.target.files[0];
    if (f) uploadFile(f);
    fileInput.value = '';
  });

  // Drag & drop на всю область композера
  ['dragenter', 'dragover'].forEach(ev =>
    composerEl.addEventListener(ev, e => {
      e.preventDefault(); e.stopPropagation();
      composerEl.classList.add('dragover');
    })
  );
  ['dragleave', 'drop'].forEach(ev =>
    composerEl.addEventListener(ev, e => {
      e.preventDefault(); e.stopPropagation();
      if (ev === 'dragleave' && composerEl.contains(e.relatedTarget)) return;
      composerEl.classList.remove('dragover');
    })
  );
  composerEl.addEventListener('drop', e => {
    const files = Array.from(e.dataTransfer.files || []);
    files.forEach(uploadFile);
  });

  async function loadFiles() {
    try {
      const r = await fetch('/api/files');
      const d = await r.json();
      attachmentsEl.innerHTML = '';
      if (!d.files || d.files.length === 0) {
        attachmentsEl.hidden = true;
        return;
      }
      attachmentsEl.hidden = false;
      d.files.forEach(f => {
        const chip = document.createElement('div');
        chip.className = 'chip';
        chip.title = f.filename;
        chip.innerHTML =
          '<span>📄</span>' +
          '<span class="chip-name">' + escapeHtml(f.filename) + '</span>' +
          '<span class="chip-size">' + escapeHtml(f.size_human) + '</span>' +
          '<button type="button" data-id="' + f.id + '" title="Удалить">✕</button>';
        chip.querySelector('button').onclick = async () => {
          await fetch('/api/files/' + f.id, { method: 'DELETE' });
          loadFiles();
        };
        attachmentsEl.appendChild(chip);
      });
    } catch (e) {
      console.error('loadFiles', e);
    }
  }

  // Подгружаем уже загруженные файлы при загрузке страницы
  loadFiles();
</script>
</body>
</html>
"""


# ============== Routes: auth ==============
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('chat'))
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        if len(username) < 3 or len(password) < 4:
            flash('Логин ≥ 3 символа, пароль ≥ 4 символов')
            return render_template_string(REGISTER_HTML, STYLE_CSS=STYLE_CSS, HLJS_CSS=HLJS_CSS)
        if User.query.filter_by(username=username).first():
            flash('Пользователь уже существует')
            return render_template_string(REGISTER_HTML, STYLE_CSS=STYLE_CSS, HLJS_CSS=HLJS_CSS)

        user = User(
            username=username,
            password_hash=generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()

        session['user_id'] = user.id
        session['username'] = user.username
        return redirect(url_for('chat'))

    return render_template_string(REGISTER_HTML, STYLE_CSS=STYLE_CSS, HLJS_CSS=HLJS_CSS)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect(url_for('chat'))

        flash('Неверный логин или пароль')
        return render_template_string(LOGIN_HTML, STYLE_CSS=STYLE_CSS, HLJS_CSS=HLJS_CSS)

    return render_template_string(LOGIN_HTML, STYLE_CSS=STYLE_CSS, HLJS_CSS=HLJS_CSS)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ============== Routes: chat ==============
@app.route('/chat')
@login_required
def chat():
    messages = (ChatMessage.query
                .filter_by(user_id=session['user_id'])
                .order_by(ChatMessage.created_at.asc())
                .all())
    return render_template_string(
        CHAT_HTML,
        STYLE_CSS=STYLE_CSS,
        HLJS_CSS=HLJS_CSS,
        models=MODELS,
        messages=messages,
        username=session['username'],
    )


@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.get_json(silent=True) or {}
    message  = (data.get('message')  or '').strip()
    model    = (data.get('model')    or 'sonnet-4.6').strip()
    filename = (data.get('filename') or '').strip() or None

    if not message:
        return jsonify({'error': 'Пустое сообщение'}), 400
    if model not in MODEL_IDS:
        return jsonify({'error': 'Неизвестная модель'}), 400

    user_msg = ChatMessage(
        user_id=session['user_id'],
        role='user', content=message, model=model,
    )
    db.session.add(user_msg)
    db.session.commit()

    history = (ChatMessage.query
               .filter_by(user_id=session['user_id'])
               .order_by(ChatMessage.created_at.desc())
               .limit(40).all())
    history = list(reversed(history))
    api_messages = [
        {"role": m.role, "content": m.content}
        for m in history if m.role in ('user', 'assistant')
    ]

    # Собираем system-блок из загруженных файлов юзера
    files_ctx = build_files_context(session['user_id'])
    sys_block = None
    if files_ctx:
        sys_block = (
            "Пользователь загрузил файлы, которые приложены к этому диалогу. "
            "Учитывай их содержимое в ответах. Если файл нерелевантен вопросу — игнорируй. "
            "Не выдумывай содержимое файлов — опирайся строго на то, что ниже.\n\n"
            f"{files_ctx}"
        )

    start = time.time()
    try:
        kwargs = dict(
            model=MODEL_IDS[model],
            max_tokens=4096,
            messages=api_messages,
        )
        if sys_block:
            kwargs["system"] = sys_block
        resp = AI_CLIENT.messages.create(**kwargs)
        elapsed = round(time.time() - start, 2)
        answer = resp.content[0].text
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        return jsonify({'error': f'AI ошибка: {e}', 'elapsed': elapsed}), 500

    asst_msg = ChatMessage(
        user_id=session['user_id'],
        role='assistant', content=answer, model=model, elapsed=elapsed,
    )
    db.session.add(asst_msg)
    db.session.commit()

    saved_to = None
    if filename:
        safe = safe_filename(filename)
        if safe:
            user_dir = os.path.join(FILES_ROOT, str(session['user_id']))
            os.makedirs(user_dir, exist_ok=True)
            full_path = os.path.join(user_dir, safe)
            os.makedirs(os.path.dirname(full_path) or user_dir, exist_ok=True)
            blocks = extract_code_blocks(answer)
            payload = '\n\n'.join(blocks) if blocks else answer
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(payload)
            saved_to = safe

    return jsonify({
        'response': answer,
        'elapsed':  elapsed,
        'model':    model,
        'saved_to': saved_to,
    })


@app.route('/api/save_file', methods=['POST'])
@login_required
def api_save_file():
    data = request.get_json(silent=True) or {}
    filename = (data.get('filename') or '').strip()
    content  = data.get('content') or ''

    if not filename:
        return jsonify({'error': 'Укажите имя файла'}), 400
    safe = safe_filename(filename)
    if not safe:
        return jsonify({'error': 'Недопустимое имя файла'}), 400

    user_dir = os.path.join(FILES_ROOT, str(session['user_id']))
    os.makedirs(user_dir, exist_ok=True)
    full_path = os.path.join(user_dir, safe)
    os.makedirs(os.path.dirname(full_path) or user_dir, exist_ok=True)

    with open(full_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return jsonify({'ok': True, 'path': safe})


@app.route('/api/clear', methods=['POST'])
@login_required
def api_clear():
    ChatMessage.query.filter_by(user_id=session['user_id']).delete()
    UploadedFile.query.filter_by(user_id=session['user_id']).delete()
    db.session.commit()
    return jsonify({'ok': True})


# ============== Routes: file upload ==============
@app.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    """Принимает файл (multipart/form-data, поле 'file'), сохраняет в БД.
    Лимит: 10 МБ на файл, до MAX_FILES_PER_USER файлов на юзера."""
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'Файл не выбран'}), 400

    # Читаем в память, проверяем размер
    raw = f.read()
    size = len(raw)
    if size == 0:
        return jsonify({'error': 'Пустой файл'}), 400
    if size > app.config['MAX_CONTENT_LENGTH']:
        return jsonify({'error': 'Файл больше 10 МБ'}), 413

    if not is_allowed_upload(f.filename, f.mimetype or ''):
        return jsonify({
            'error': 'Тип не поддерживается. Загружай текст/код '
                     '(.py, .js, .md, .json, .txt и т.п.)'
        }), 415

    # Лимит по количеству файлов
    existing = UploadedFile.query.filter_by(user_id=session['user_id']).count()
    if existing >= MAX_FILES_PER_USER:
        # Удаляем самый старый, чтобы впихнуть новый
        oldest = (UploadedFile.query
                  .filter_by(user_id=session['user_id'])
                  .order_by(UploadedFile.created_at.asc())
                  .first())
        if oldest:
            db.session.delete(oldest)
            db.session.commit()

    # Декодируем текст. Если бинарь — отбой.
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        try:
            text = raw.decode('utf-8', errors='replace')
        except Exception:
            return jsonify({'error': 'Не удалось декодировать как UTF-8'}), 415

    # Защита от null-байтов (бывает в бинарях с расширением .txt)
    if '\x00' in text:
        return jsonify({'error': 'Бинарь нельзя загружать как текст'}), 415

    safe = safe_filename(os.path.basename(f.filename)) or 'file.txt'
    rec = UploadedFile(
        user_id=session['user_id'],
        filename=safe,
        mime_type=(f.mimetype or '')[:128],
        size_bytes=size,
        content=text,
    )
    db.session.add(rec)
    db.session.commit()

    return jsonify({
        'ok': True,
        'id': rec.id,
        'filename': rec.filename,
        'size': rec.size_bytes,
        'size_human': human_size(rec.size_bytes),
    })


@app.route('/api/files', methods=['GET'])
@login_required
def api_files_list():
    files = (UploadedFile.query
             .filter_by(user_id=session['user_id'])
             .order_by(UploadedFile.created_at.asc())
             .all())
    return jsonify({
        'files': [{
            'id': f.id,
            'filename': f.filename,
            'size': f.size_bytes,
            'size_human': human_size(f.size_bytes),
            'mime': f.mime_type,
            'created_at': f.created_at.isoformat() if f.created_at else None,
        } for f in files]
    })


@app.route('/api/files/<int:file_id>', methods=['DELETE'])
@login_required
def api_files_delete(file_id):
    f = UploadedFile.query.filter_by(id=file_id, user_id=session['user_id']).first()
    if not f:
        return jsonify({'error': 'Не найден'}), 404
    db.session.delete(f)
    db.session.commit()
    return jsonify({'ok': True})


@app.errorhandler(413)
def _too_large(e):
    return jsonify({'error': 'Файл больше 10 МБ'}), 413


# ============== Healthcheck (полезно для serverless) ==============
@app.route('/healthz')
def healthz():
    return jsonify({'ok': True})


# ============== Local dev entrypoint ==============
if __name__ == '__main__':
    # При локальном запуске пытаемся сразу создать таблицы (для удобства).
    # В serverless-режиме этот блок не выполняется, init идёт лениво.
    try:
        with app.app_context():
            db.create_all()
            print("[OK] DB tables ready (local)", flush=True)
    except Exception as e:
        print(f"[WARN] DB init (local): {e}", flush=True)

    port = int(os.environ.get('PORT', 5000))
    print(f" * Running on http://0.0.0.0:{port}", flush=True)
    app.run(host='0.0.0.0', port=port, debug=False)
