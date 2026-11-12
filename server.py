import os
import json
import time
import uuid
import hashlib
import secrets
import threading
import queue
import requests

from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import (
    Flask, request, jsonify, render_template, g,
    make_response
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__, template_folder=BASE_DIR)
if os.environ.get('TRUST_PROXY') == '1':
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY",
    secrets.token_hex(32)
)

app.config["SQLALCHEMY_DATABASE_URI"] = (
    "sqlite:///" + os.path.join(os.environ.get("AI_DATA_DIR",os.path.join(BASE_DIR,"instance")), "chat.db")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "connect_args": {
        "check_same_thread": False,
        "timeout": 60,
    }
}

os.makedirs(os.environ.get('AI_DATA_DIR',os.path.join(BASE_DIR,'instance')),exist_ok=True)
db = SQLAlchemy(app)


def env_int(name, default):
    raw = os.environ.get(name, str(default)).split("#", 1)[0].strip()
    return int(raw)


def env_float(name, default):
    raw = os.environ.get(name, str(default)).split("#", 1)[0].strip()
    return float(raw)


def env_timeout(name, default=None):
    raw = os.environ.get(name, "" if default is None else str(default))
    raw = raw.split("#", 1)[0].strip().lower()
    if raw in ("", "none", "false", "0"):
        return None
    return float(raw)


OLLAMA_URL = os.environ.get(
    "OLLAMA_URL", "http://127.0.0.1:11434"
).rstrip("/")

OLLAMA_CHAT_URL = OLLAMA_URL + "/api/chat"

# None = no client-side HTTP timeout.
# Ollama can generate for tens of minutes on this hardware.
OLLAMA_TIMEOUT = env_timeout("OLLAMA_TIMEOUT", None)

API_TIMEOUT = env_timeout("API_TIMEOUT", 1800)

MAX_TASK_SECONDS = env_int("MAX_TASK_SECONDS", 14400)

NUM_PREDICT = env_int("NUM_PREDICT", 1024)
TEMPERATURE = env_float("TEMPERATURE", 0.7)
OLLAMA_NUM_CTX = env_int("OLLAMA_NUM_CTX", 24576)
KEEP_ALIVE = os.environ.get(
    "OLLAMA_KEEP_ALIVE", "10m"
).split("#", 1)[0].strip()

MAX_CONTEXT_MESSAGES = env_int("MAX_CONTEXT_MESSAGES", 30)
MAX_PROMPT_CHARS = env_int("MAX_PROMPT_CHARS", 12000)
MAX_SEARCH_RESULTS = env_int("MAX_SEARCH_RESULTS", 5)

TASK_RETENTION_SECONDS = env_int(
    "TASK_RETENTION_SECONDS", 7 * 24 * 3600
)

SESSION_DAYS = env_int("SESSION_DAYS", 30)
SESSION_COOKIE_NAME = os.environ.get(
    "SESSION_COOKIE_NAME", "ai_session"
)

# Automatically use Secure cookies when the public site is HTTPS.
# Set SESSION_COOKIE_SECURE=false only for plain HTTP local testing.
SESSION_COOKIE_SECURE = os.environ.get(
    "SESSION_COOKIE_SECURE", "true"
).lower() in ("1", "true", "yes", "on")

SEARXNG_URL = os.environ.get(
    "SEARXNG_URL", "http://127.0.0.1:8080"
).rstrip("/")

API_KEY = os.environ.get("MINECRAFT_API_KEY", "").strip()
INTERNAL_KEY = os.environ.get(
    "INTERNAL_API_KEY", ""
).strip()

DEFAULT_MODEL = os.environ.get(
    "OLLAMA_MODEL", "qwen3:4b"
).strip()

# Ollama's Qwen3 releases normally use 0.6b rather than 0.8b.
MODEL_CONFIG = {
    "qwen3:8b": {
        "name": "Думающая",
        "description": "Самая умная. Лучше для сложных задач и рассуждений.",
        "speed": "самая медленная",
        "average": "≈ 35–45 мин",
        "order": 1,
    },
    "qwen3:4b": {
        "name": "Умная",
        "description": "Хороший баланс качества и скорости.",
        "speed": "медленная",
        "average": "≈ 20 мин",
        "order": 2,
    },
    "qwen3:1.7b": {
        "name": "Быстрая",
        "description": "Быстрее 4B, но слабее на сложных задачах.",
        "speed": "быстрая",
        "average": "≈ 7–10 мин",
        "order": 3,
    },
    "qwen3:0.6b": {
        "name": "Флеш",
        "description": "Для простых вопросов и минимальной задержки.",
        "speed": "самая быстрая",
        "average": "≈ 2–4 мин",
        "order": 4,
    },
}

if DEFAULT_MODEL not in MODEL_CONFIG:
    DEFAULT_MODEL = "qwen3:4b"


# ============================================================
# DATABASE
# ============================================================

os.makedirs(os.environ.get("AI_DATA_DIR",os.path.join(BASE_DIR, "instance")), exist_ok=True)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(
        db.String(80), unique=True, nullable=False, index=True
    )
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )

    chats = db.relationship(
        "Chat", backref="user", lazy=True,
        cascade="all, delete-orphan"
    )
    sessions = db.relationship(
        "Session", backref="user", lazy=True,
        cascade="all, delete-orphan"
    )


class Session(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(
        db.String(64), unique=True, nullable=False, index=True
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"),
        nullable=False, index=True
    )
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )
    last_seen = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )
    expires_at = db.Column(
        db.DateTime, nullable=False, index=True
    )


class Chat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"),
        nullable=False, index=True
    )
    name = db.Column(
        db.String(100), default="Новый чат", nullable=False
    )
    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )

    messages = db.relationship(
        "Message", backref="chat", lazy=True,
        cascade="all, delete-orphan"
    )


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(
        db.Integer, db.ForeignKey("chat.id"),
        nullable=False, index=True
    )
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )


class Task(db.Model):
    """
    Persistent task state.

    This is the important change compared with the old version:
    task state is NOT kept only in RAM. Therefore a page reload does
    not make the client forget the request, and a normal Flask restart
    can recover queued/processing tasks.
    """
    id = db.Column(db.String(36), primary_key=True)

    user_id = db.Column(
        db.Integer, nullable=True, index=True
    )
    chat_id = db.Column(
        db.Integer, nullable=True, index=True
    )

    prompt = db.Column(db.Text, nullable=False)
    model = db.Column(db.String(100), nullable=False)

    selected_message_ids = db.Column(
        db.Text, nullable=False, default="[]"
    )

    use_search = db.Column(
        db.Boolean, nullable=False, default=False
    )
    api_request = db.Column(
        db.Boolean, nullable=False, default=False
    )

    status = db.Column(
        db.String(20), nullable=False, default="queued", index=True
    )

    result = db.Column(db.Text, nullable=True)
    error = db.Column(db.Text, nullable=True)

    created_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False
    )
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)

    elapsed = db.Column(db.Float, nullable=True)


if os.environ.get("AI_WEBAPP_NO_WORKER") != "1":
    with app.app_context():
        db.create_all()


# ============================================================
# AUTHENTICATION
# ============================================================

def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user):
    raw = secrets.token_urlsafe(48)
    now = datetime.utcnow()

    db.session.add(
        Session(
            token_hash=token_hash(raw),
            user_id=user.id,
            created_at=now,
            last_seen=now,
            expires_at=now + timedelta(days=SESSION_DAYS),
        )
    )
    db.session.commit()
    return raw


def get_current_session():
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw:
        return None

    s = Session.query.filter_by(
        token_hash=token_hash(raw)
    ).first()

    if not s:
        return None

    now = datetime.utcnow()

    if s.expires_at <= now:
        db.session.delete(s)
        db.session.commit()
        return None

    # Sliding expiration.
    s.last_seen = now
    s.expires_at = now + timedelta(days=SESSION_DAYS)
    db.session.commit()

    return s


@app.before_request
def identify_user():
    session = get_current_session()

    if session:
        g.current_user = db.session.get(User, session.user_id)
    else:
        g.current_user = None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.current_user:
            return jsonify(error="Authentication required"), 401
        return fn(*args, **kwargs)

    return wrapper


def set_session_cookie(response, raw):
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="Lax",
        path="/",
    )
    return response


def clear_session_cookie(response):
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
    )
    return response


# ============================================================
# QUEUE
# ============================================================

task_queue = queue.Queue()
worker_lock = threading.Lock()


def enqueue_existing_tasks():
    """
    Recover tasks after a server restart.
    Processing tasks are returned to queued state because their
    previous in-memory worker no longer exists.
    """
    with app.app_context():
        processing = Task.query.filter_by(
            status="processing"
        ).all()

        for task in processing:
            task.status = "queued"
            task.started_at = None
            task.elapsed = None

        db.session.commit()

        queued = Task.query.filter_by(
            status="queued"
        ).order_by(Task.created_at.asc()).all()

        for task in queued:
            task_queue.put(task.id)

        print(
            f"[QUEUE] recovered {len(queued)} queued task(s)",
            flush=True
        )


def cleanup_tasks():
    cutoff = datetime.utcnow() - timedelta(
        seconds=TASK_RETENTION_SECONDS
    )

    with app.app_context():
        old = Task.query.filter(
            Task.status.in_(["done", "error"]),
            Task.finished_at.isnot(None),
            Task.finished_at < cutoff,
        ).all()

        for task in old:
            db.session.delete(task)

        if old:
            db.session.commit()


def add_task(
    user_id,
    chat_id,
    prompt,
    model,
    selected=None,
    api_request=False,
    use_search=False,
):
    tid = str(uuid.uuid4())

    task = Task(
        id=tid,
        user_id=user_id,
        chat_id=chat_id,
        prompt=prompt,
        model=model,
        selected_message_ids=json.dumps(
            selected or [],
            ensure_ascii=False
        ),
        api_request=api_request,
        use_search=use_search,
        status="queued",
    )

    with app.app_context():
        db.session.add(task)
        db.session.commit()

    task_queue.put(tid)
    return tid


def queue_position(task_id):
    """
    Queue position is calculated from DB order.
    It survives page reloads.
    """
    with app.app_context():
        task = db.session.get(Task, task_id)
        if not task or task.status != "queued":
            return 0

        count = Task.query.filter(
            Task.status == "queued",
            Task.created_at < task.created_at,
        ).count()

        return count + 1


# ============================================================
# OLLAMA / SEARCH
# ============================================================

def installed_models():
    try:
        response = requests.get(
            OLLAMA_URL + "/api/tags",
            timeout=5,
        )

        if not response.ok:
            return set()

        return {
            item.get("name")
            for item in response.json().get("models", [])
            if item.get("name")
        }

    except Exception:
        return set()


def web_search(query):
    """
    Deliberately deterministic web search.

    We do NOT ask a second model to decide whether to search.
    When the user enables Internet, the server searches the exact
    user request through SearXNG and then gives the results to Qwen.

    This makes the behaviour much easier to understand and debug.
    """
    try:
        response = requests.get(
            SEARXNG_URL + "/search",
            params={
                "q": query,
                "format": "json",
                "categories": "general",
                "safesearch": 1,
            },
            timeout=20,
        )

        if response.status_code != 200:
            return {
                "ok": False,
                "text": (
                    f"Поисковый сервер вернул HTTP "
                    f"{response.status_code}."
                ),
            }

        data = response.json()
        results = data.get("results", [])[:MAX_SEARCH_RESULTS]

        if not results:
            return {
                "ok": False,
                "text": "Поисковый сервер не вернул результатов.",
            }

        lines = []

        for i, item in enumerate(results, 1):
            title = str(
                item.get("title", "Без названия")
            ).strip()

            snippet = str(
                item.get("content") or item.get("snippet", "")
            ).strip()

            url = str(
                item.get("url", "")
            ).strip()

            lines.append(
                f"[{i}] {title}\n"
                f"URL: {url}\n"
                f"Описание: {snippet}"
            )

        return {
            "ok": True,
            "text": "\n\n".join(lines),
        }

    except Exception as exc:
        return {
            "ok": False,
            "text": f"Ошибка обращения к SearXNG: {exc}",
        }


def selected_context(chat_id, user_id, ids):
    if not chat_id or not user_id or not ids:
        return ""

    try:
        ids = list(dict.fromkeys(
            int(x) for x in ids
        ))[:MAX_CONTEXT_MESSAGES]
    except (TypeError, ValueError):
        return ""

    with app.app_context():
        chat = Chat.query.filter_by(
            id=chat_id,
            user_id=user_id,
        ).first()

        if not chat:
            return ""

        messages = Message.query.filter(
            Message.chat_id == chat_id,
            Message.id.in_(ids),
        ).order_by(
            Message.timestamp.asc(),
            Message.id.asc(),
        ).all()

        result = []

        for message in messages:
            role = (
                "Пользователь"
                if message.role == "user"
                else "Ассистент"
            )

            result.append(
                f"{role}: {message.content[:MAX_PROMPT_CHARS]}"
            )

        return "\n\n".join(result)


def build_messages(task, search_text=None):
    prompt = task.prompt[:MAX_PROMPT_CHARS]

    messages = []

    system = (
        "Ты — локальный ИИ-помощник. "
        "Отвечай на русском, если пользователь пишет по-русски. "
        "Не выдумывай факты. Если данных недостаточно, прямо скажи об этом. "
        "Не утверждай, что видел или проверил то, чего фактически не видел."
    )

    if search_text:
        system += (
            "\n\nПользователь включил поиск в интернете. "
            "Ниже приведены результаты поиска. "
            "Используй их как источники, но не считай их автоматически истинными. "
            "Если источники противоречат друг другу, укажи это. "
            "Не придумывай сведения, которых нет в результатах. "
            "В конце ответа добавь раздел 'Источники' и укажи URL, "
            "которые реально присутствуют в результатах поиска.\n\n"
            "=== РЕЗУЛЬТАТЫ ПОИСКА ===\n"
            f"{search_text}\n"
            "=== КОНЕЦ РЕЗУЛЬТАТОВ ==="
        )

    messages.append({
        "role": "system",
        "content": system,
    })

    ids = []

    try:
        ids = json.loads(
            task.selected_message_ids or "[]"
        )
    except Exception:
        ids = []

    context = selected_context(
        task.chat_id,
        task.user_id,
        ids,
    )

    if context:
        messages.append({
            "role": "system",
            "content": (
                "Пользователь специально выбрал следующие сообщения "
                "из текущего чата как контекст. "
                "Используй только этот выбранный контекст; "
                "весь остальной чат автоматически в контекст НЕ добавляется.\n\n"
                "=== ВЫБРАННЫЙ КОНТЕКСТ ===\n"
                f"{context}\n"
                "=== КОНЕЦ ВЫБРАННОГО КОНТЕКСТА ==="
            ),
        })

    messages.append({
        "role": "user",
        "content": prompt,
    })

    return messages


def ollama_chat(task, messages):
    payload = {
        "model": task.model,
        "messages": messages,
        "stream": True,
        "keep_alive": KEEP_ALIVE,
        "options": {
            "num_predict": NUM_PREDICT,
            "temperature": TEMPERATURE,
            "num_ctx": OLLAMA_NUM_CTX,
        },
    }

    started = time.time()
    parts = []
    last_error = None

    # Two attempts. The second attempt is particularly useful after
    # switching models because Ollama may still be loading the model.
    for attempt in range(2):
        parts = []
        try:
            with requests.post(
                OLLAMA_CHAT_URL,
                json=payload,
                stream=True,
                timeout=OLLAMA_TIMEOUT,
            ) as response:

                if response.status_code != 200:
                    body = response.text[:1000]
                    last_error = (
                        f"Ollama HTTP {response.status_code}: {body}"
                    )

                    if attempt == 0 and response.status_code in (
                        408, 429, 500, 502, 503, 504
                    ):
                        time.sleep(3)
                        continue

                    break

                for line in response.iter_lines(
                    decode_unicode=True,
                    chunk_size=1,
                ):
                    if time.time() - started > MAX_TASK_SECONDS:
                        raise RuntimeError("Generation exceeded the configured time limit")
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if data.get("error"):
                        last_error = str(data["error"])

                    message = data.get("message") or {}
                    content = message.get("content")

                    if isinstance(content, str) and content:
                        parts.append(content)

                    if data.get("done"):
                        break

                if parts:
                    result = "".join(parts).strip()

                    if result:
                        return (
                            result,
                            time.time() - started,
                        )

                if not last_error:
                    last_error = (
                        "Ollama завершила запрос без текстового результата."
                    )

        except requests.exceptions.Timeout as exc:
            last_error = (
                "Ожидание Ollama превысило таймаут: "
                f"{exc}"
            )

        except requests.RequestException as exc:
            last_error = (
                "Ошибка соединения с Ollama: "
                f"{exc}"
            )

        except Exception as exc:
            last_error = f"Неожиданная ошибка Ollama: {exc}"

        if attempt == 0:
            time.sleep(3)

    raise RuntimeError(
        last_error or "Ollama не вернула текстовый ответ."
    )


# ============================================================
# WORKER
# ============================================================

def worker():
    print("[WORKER] AI worker started", flush=True)

    while True:
        tid = task_queue.get()

        if tid is None:
            task_queue.task_done()
            break

        try:
            cleanup_tasks()

            with app.app_context():
                task = db.session.get(Task, tid)

                if not task:
                    continue

                if task.status not in ("queued", "processing"):
                    continue

                task.status = "processing"
                task.started_at = datetime.utcnow()
                task.error = None
                db.session.commit()

                use_search = bool(task.use_search)

                print(
                    f"[WORKER] task={tid} "
                    f"model={task.model} "
                    f"search={use_search}",
                    flush=True,
                )

                search_text = None

                if use_search:
                    search = web_search(task.prompt)

                    if search["ok"]:
                        search_text = search["text"]
                    else:
                        # Search failure is not silently converted
                        # into fake information.
                        search_text = (
                            "ПОИСК НЕ УДАЛСЯ.\n"
                            + search["text"]
                        )

                messages = build_messages(
                    task,
                    search_text=search_text,
                )

                result, elapsed = ollama_chat(
                    task,
                    messages,
                )

                if not result.strip():
                    raise RuntimeError(
                        "Модель вернула пустой ответ. "
                        "Запрос будет считаться ошибочным, "
                        "а не подменяться фиктивным текстом."
                    )

                task.status = "done"
                task.result = result
                task.error = None
                task.finished_at = datetime.utcnow()
                task.elapsed = elapsed

                # Save assistant message for normal web chat.
                if not task.api_request and task.chat_id:
                    chat = Chat.query.filter_by(
                        id=task.chat_id,
                        user_id=task.user_id,
                    ).first()

                    if chat:
                        db.session.add(
                            Message(
                                chat_id=chat.id,
                                role="assistant",
                                content=result,
                            )
                        )

                db.session.commit()

                print(
                    f"[WORKER] task={tid} DONE "
                    f"in {elapsed:.1f}s",
                    flush=True,
                )

        except Exception as exc:
            import traceback
            traceback.print_exc()

            try:
                with app.app_context():
                    task = db.session.get(Task, tid)

                    if task:
                        task.status = "error"
                        task.result = None
                        task.error = str(exc)
                        task.finished_at = datetime.utcnow()

                        if task.started_at:
                            task.elapsed = (
                                task.finished_at - task.started_at
                            ).total_seconds()

                        db.session.commit()

            except Exception:
                import traceback
                traceback.print_exc()

        finally:
            task_queue.task_done()


if os.environ.get("AI_WEBAPP_NO_WORKER") != "1":
    enqueue_existing_tasks()
    threading.Thread(
        target=worker,
        daemon=True,
        name="ai-worker",
    ).start()


# ============================================================
# HELPERS
# ============================================================

def json_data():
    return request.get_json(silent=True) or {}


def owned_chat(chat_id):
    if not g.current_user:
        return None

    return Chat.query.filter_by(
        id=chat_id,
        user_id=g.current_user.id,
    ).first()


def task_visible_to_current_user(task):
    if not task:
        return False

    if task.api_request:
        return False

    return (
        g.current_user is not None
        and task.user_id == g.current_user.id
    )


def task_json(task):
    position = 0

    if task.status == "queued":
        position = queue_position(task.id)

    out = {
        "task_id": task.id,
        "status": task.status,
        "position": position,
        "error": task.error,
        "model": task.model,
        "model_label": MODEL_CONFIG.get(
            task.model, {}
        ).get("name", task.model),
        "created_at": (
            task.created_at.timestamp()
            if task.created_at else None
        ),
        "started_at": (
            task.started_at.timestamp()
            if task.started_at else None
        ),
        "elapsed": None,
    }

    if task.status == "processing" and task.started_at:
        out["elapsed"] = int(
            (datetime.utcnow() - task.started_at).total_seconds()
        )

    if task.status in ("done", "error"):
        out["elapsed"] = task.elapsed

    if task.status == "done":
        out["result"] = task.result
        out["finished_at"] = (
            task.finished_at.timestamp()
            if task.finished_at else None
        )

    return out


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    with app.app_context():
        queued = Task.query.filter_by(status="queued").count()
        processing = Task.query.filter_by(status="processing").count()

    try:
        ollama_ok = requests.get(
            OLLAMA_URL + "/api/tags",
            timeout=3,
        ).ok
    except Exception:
        ollama_ok = False

    return jsonify(
        status="ok",
        ollama=OLLAMA_URL,
        ollama_reachable=ollama_ok,
        model=DEFAULT_MODEL,
        queued=queued,
        processing=processing,
    )


@app.route("/models")
@login_required
def models():
    installed = installed_models()

    arr = []

    for model_id, info in sorted(
        MODEL_CONFIG.items(),
        key=lambda x: x[1]["order"],
    ):
        arr.append({
            "id": model_id,
            "label": info["name"],
            "description": info["description"],
            "speed": info["speed"],
            "average": info["average"],
            "installed": model_id in installed,
        })

    return jsonify(
        models=arr,
        default=DEFAULT_MODEL,
        warning=(
            "После переключения модели первый запрос может быть "
            "намного дольше обычного: Ollama должна загрузить новую "
            "модель в память. Это особенно заметно на больших моделях."
        ),
    )


# ---------------- AUTH ----------------

@app.route("/auth/register", methods=["POST"])
def register():
    data = json_data()

    username = str(
        data.get("username", "")
    ).strip()

    password = str(
        data.get("password", "")
    )

    if not username or not password:
        return jsonify(
            error="Введите логин и пароль."
        ), 400

    if len(username) < 3 or len(username) > 80:
        return jsonify(
            error="Логин должен содержать от 3 до 80 символов."
        ), 400

    if len(password) < 12:
        return jsonify(
            error="Пароль должен быть не короче 12 символов."
        ), 400

    if User.query.filter_by(
        username=username
    ).first():
        return jsonify(
            error="Пользователь уже существует."
        ), 400

    user = User(
        username=username,
        password_hash=generate_password_hash(password),
    )

    db.session.add(user)
    db.session.commit()

    return jsonify(
        message="Аккаунт создан."
    ), 201


@app.route("/auth/login", methods=["POST"])
def login():
    data = json_data()

    username = str(
        data.get("username", "")
    ).strip()

    password = str(
        data.get("password", "")
    )

    user = User.query.filter_by(
        username=username
    ).first()

    if not user or not check_password_hash(
        user.password_hash,
        password,
    ):
        return jsonify(
            error="Неверный логин или пароль."
        ), 401

    # Delete only the current browser's previous session.
    old_token = request.cookies.get(
        SESSION_COOKIE_NAME
    )

    if old_token:
        old_session = Session.query.filter_by(
            token_hash=token_hash(old_token)
        ).first()

        if old_session:
            db.session.delete(old_session)
            db.session.commit()

    raw = create_session(user)

    response = make_response(
        jsonify(
            message="Вход выполнен.",
            user={
                "id": user.id,
                "username": user.username,
            },
        )
    )

    return set_session_cookie(response, raw)


@app.route("/auth/me")
def auth_me():
    if not g.current_user:
        return jsonify(
            authenticated=False
        ), 401

    return jsonify(
        authenticated=True,
        user={
            "id": g.current_user.id,
            "username": g.current_user.username,
        },
    )


@app.route("/auth/logout", methods=["POST"])
def logout():
    raw = request.cookies.get(
        SESSION_COOKIE_NAME
    )

    if raw:
        session = Session.query.filter_by(
            token_hash=token_hash(raw)
        ).first()

        if session:
            db.session.delete(session)
            db.session.commit()

    return clear_session_cookie(
        make_response(
            jsonify(message="Вы вышли из аккаунта.")
        )
    )


# ---------------- CHATS ----------------

@app.route("/chats")
@login_required
def chats():
    result = Chat.query.filter_by(
        user_id=g.current_user.id
    ).order_by(
        Chat.created_at.desc()
    ).all()

    return jsonify([
        {
            "id": chat.id,
            "name": chat.name,
            "created_at": chat.created_at.isoformat(),
        }
        for chat in result
    ])


@app.route("/chats", methods=["POST"])
@login_required
def create_chat():
    data = json_data()

    name = str(
        data.get("name", "Новый чат")
    ).strip()[:100] or "Новый чат"

    chat = Chat(
        user_id=g.current_user.id,
        name=name,
    )

    db.session.add(chat)
    db.session.commit()

    return jsonify(
        id=chat.id,
        name=chat.name,
    ), 201


@app.route("/chats/<int:chat_id>")
@login_required
def get_chat(chat_id):
    chat = owned_chat(chat_id)

    if not chat:
        return jsonify(
            error="Чат не найден."
        ), 404

    messages = Message.query.filter_by(
        chat_id=chat.id
    ).order_by(
        Message.timestamp.asc(),
        Message.id.asc(),
    ).all()

    return jsonify([
        {
            "id": message.id,
            "role": message.role,
            "content": message.content,
            "timestamp": message.timestamp.isoformat(),
        }
        for message in messages
    ])


@app.route(
    "/chats/<int:chat_id>/messages",
    methods=["POST"]
)
@login_required
def send_message(chat_id):
    chat = owned_chat(chat_id)

    if not chat:
        return jsonify(
            error="Чат не найден."
        ), 404

    data = json_data()

    prompt = str(
        data.get("prompt", "")
    ).strip()

    model = str(
        data.get("model", DEFAULT_MODEL)
    ).strip()

    selected_ids = data.get(
        "context_message_ids",
        []
    )

    use_search = bool(
        data.get("use_search", False)
    )

    if not prompt:
        return jsonify(
            error="Введите сообщение."
        ), 400

    if len(prompt) > MAX_PROMPT_CHARS:
        return jsonify(
            error=(
                "Сообщение слишком длинное. "
                f"Максимум {MAX_PROMPT_CHARS} символов."
            )
        ), 400

    if model not in MODEL_CONFIG:
        return jsonify(
            error="Неизвестная модель."
        ), 400

    if not isinstance(selected_ids, list):
        selected_ids = []

    try:
        selected_ids = list(dict.fromkeys(
            int(x) for x in selected_ids
        ))[:MAX_CONTEXT_MESSAGES]
    except (TypeError, ValueError):
        selected_ids = []

    if selected_ids:
        count = Message.query.filter(
            Message.chat_id == chat.id,
            Message.id.in_(selected_ids),
        ).count()

        if count != len(selected_ids):
            return jsonify(
                error=(
                    "Одно или несколько выбранных сообщений "
                    "не принадлежат этому чату."
                )
            ), 400

    # User message is saved immediately.
    user_message = Message(
        chat_id=chat.id,
        role="user",
        content=prompt,
    )

    db.session.add(user_message)
    db.session.commit()

    task_id = add_task(
        user_id=g.current_user.id,
        chat_id=chat.id,
        prompt=prompt,
        model=model,
        selected=selected_ids,
        use_search=use_search,
        api_request=False,
    )

    return jsonify(
        task_id=task_id,
        model=model,
        model_label=MODEL_CONFIG[model]["name"],
        warning=(
            "Первый запрос после смены модели может быть "
            "намного дольше из-за загрузки модели в память."
        ),
    )


# ---------------- TASK STATUS ----------------

@app.route("/task/<task_id>")
@login_required
def task_status_route(task_id):
    # IMPORTANT: do not push a second app context here.
    # The request already has an application/request context, and g.current_user
    # is attached to that context. Pushing app.app_context() here creates a new
    # g object without current_user and causes:
    # AttributeError: current_user
    task = db.session.get(Task, task_id)

    if not task:
        return jsonify(
            error="Задача не найдена или срок её хранения истёк."
        ), 404

    if not task_visible_to_current_user(task):
        return jsonify(
            error="Задача не найдена."
        ), 404

    return jsonify(task_json(task))


# ---------------- MINECRAFT API ----------------

@app.route("/api/generate", methods=["POST"])
def api_generate():
    if not API_KEY:
        return jsonify(
            error="MINECRAFT_API_KEY is not configured"
        ), 503

    if request.headers.get("X-API-Key", "") != API_KEY:
        return jsonify(
            error="Unauthorized"
        ), 401

    data = json_data()

    prompt = str(
        data.get("prompt", "")
    ).strip()

    model = str(
        data.get("model", DEFAULT_MODEL)
    ).strip()

    if not prompt:
        return jsonify(
            error="Prompt required"
        ), 400

    if len(prompt) > MAX_PROMPT_CHARS:
        return jsonify(
            error="Prompt too long"
        ), 400

    if model not in MODEL_CONFIG:
        return jsonify(
            error="Unknown model"
        ), 400

    task_id = add_task(
        user_id=None,
        chat_id=None,
        prompt=prompt,
        model=model,
        api_request=True,
        use_search=False,
    )

    started = time.time()

    while True:
        with app.app_context():
            task = db.session.get(
                Task, task_id
            )

            if task and task.status in (
                "done", "error"
            ):
                if task.status == "error":
                    return jsonify(
                        error=task.error or "Generation error"
                    ), 500

                return jsonify(
                    response=task.result or "",
                    model=task.model,
                    model_label=MODEL_CONFIG[
                        task.model
                    ]["name"],
                    elapsed=task.elapsed,
                )

        if (
            API_TIMEOUT is not None
            and time.time() - started > API_TIMEOUT
        ):
            return jsonify(
                error="API timeout",
                message=(
                    "Генерация продолжается на сервере."
                ),
                task_id=task_id,
            ), 504

        time.sleep(0.5)


# ---------------- INTERNAL RECOVERY ----------------

@app.route(
    "/internal/process-pending",
    methods=["POST"]
)
def process_pending():
    if not INTERNAL_KEY or request.headers.get(
        "X-Internal-Key", ""
    ) != INTERNAL_KEY:
        return jsonify(
            error="Unauthorized"
        ), 401

    added = []

    with app.app_context():
        messages = db.session.query(Message).all()

        for message in messages:
            if message.role != "user":
                continue

            # Do not recreate a task when an existing task is attached
            # to this user message. This endpoint is kept only for
            # compatibility with the old installation.
            continue

    return jsonify(
        tasks_added=len(added),
        tasks=added,
    )




@app.route("/api/task/<task_id>")
def api_task_status(task_id):
    if not API_KEY:
            return jsonify(
                error="MINECRAFT_API_KEY is not configured"
            ), 503
    if request.headers.get("X-API-Key", "") != API_KEY:
            return jsonify(
                error="Unauthorized"
            ), 401
    task = db.session.get(Task, task_id)
    if not task or not task.api_request:
        return jsonify(error="Task not found"), 404
    return jsonify(task_json(task))


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST","127.0.0.1"),
        port=5000,
        debug=False,
    )
