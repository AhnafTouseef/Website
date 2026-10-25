from flask import (Flask, Blueprint, render_template, request, redirect, url_for,
                   flash, abort, session)
from markupsafe import Markup, escape
import csv
import difflib
import hmac
import io
import json
import logging
import os
import platform
import random
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime
from importlib import metadata

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,   # hard cap for any request body (uploads are capped at 5 MB each)
)


# ---------------------------------------------------------------------------
# Logging: warnings go to the terminal; everything is also kept (de-duplicated)
# in memory so the admin "system" page can show it.
# ---------------------------------------------------------------------------

class _LogBook(logging.Handler):
    """Remembers recent log lines, collapsing repeats into one line with a counter."""

    def __init__(self, limit=300):
        super().__init__()
        self.limit = limit
        self._book_lock = threading.Lock()
        self.entries = {}

    def emit(self, record):
        key = (record.levelname, record.getMessage())
        with self._book_lock:
            entry = self.entries.pop(key, None)
            if entry is None:
                entry = {"level": key[0], "msg": key[1], "count": 0}
            entry["count"] += 1
            entry["last"] = record.created
            self.entries[key] = entry          # re-insert = most recent last
            while len(self.entries) > self.limit:
                self.entries.pop(next(iter(self.entries)))

    def recent(self):
        with self._book_lock:
            return list(reversed(list(self.entries.values())))


log = logging.getLogger("belajar")
log.setLevel(logging.INFO)
log.propagate = False
_logbook = _LogBook()
log.addHandler(_logbook)
_console = logging.StreamHandler()
_console.setLevel(logging.WARNING)
_console.setFormatter(logging.Formatter("%(levelname)s [belajar] %(message)s"))
log.addHandler(_console)


# ---------------------------------------------------------------------------
# Secrets. Nothing is hardcoded:
#   SECRET_KEY      env var, else secret_key.txt next to app.py (random, auto-created)
#   ADMIN_PASSWORD  env var, else admin_password.txt next to app.py (random, auto-created
#                   on first run and printed once in the terminal)
# ---------------------------------------------------------------------------

def _load_secret(env_name, filename, make):
    value = os.environ.get(env_name, "").strip()
    if value:
        return value, "env"
    path = os.path.join(app.root_path, filename)
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.readline().strip()
        if value:
            return value, "file"
    except OSError:
        pass
    value = make()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
        return value, "created"
    except OSError as exc:
        log.warning("Could not save %s (%s); the value only lives until the server restarts", filename, exc)
        return value, "memory"


app.secret_key, _ = _load_secret("SECRET_KEY", "secret_key.txt", lambda: secrets.token_hex(32))
ADMIN_PASSWORD, _pw_source = _load_secret("ADMIN_PASSWORD", "admin_password.txt", lambda: secrets.token_urlsafe(12))


def _normalize_prefix(raw):
    raw = (raw or "").strip()
    if not re.fullmatch(r"/?[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*/?", raw):
        return "/_console"
    return "/" + raw.strip("/")


# The admin lives at a path that is not linked anywhere. Change it with the ADMIN_URL_PREFIX env var.
ADMIN_PREFIX = _normalize_prefix(os.environ.get("ADMIN_URL_PREFIX"))

if _pw_source in ("created", "memory"):
    print(
        "\n  ADMIN CONSOLE  http://127.0.0.1:5001" + ADMIN_PREFIX + "/"
        "\n  Password (shown once, saved in admin_password.txt): " + ADMIN_PASSWORD + "\n",
        flush=True,
    )

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")

pipeline_courses = [
    {"title": "Data Analytics with Python", "estimate": "Est. Q4 2026", "icon": "📊"},
    {"title": "Web Development (HTML/CSS/JS)", "estimate": "Est. Q4 2026", "icon": "🌐"},
    {"title": "UI/UX Design Fundamentals", "estimate": "Est. Q4 2026", "icon": "🎨"},
    {"title": "Digital Marketing & SEO", "estimate": "Est. 2027", "icon": "📣"},
    {"title": "Excel & Google Sheets for Work", "estimate": "Est. 2027", "icon": "🧮"},
    {"title": "Public Speaking & Communication", "estimate": "Est. 2027", "icon": "🎤"}
]

faq_questions = [
    {"question": "Do I need any prior programming experience?", "answer": "No. The course is built for beginners and starts with the fundamentals, so you can join even if you have never coded before."},
    {"question": "What's the time commitment per week?", "answer": "Expect 6–8 hours per week for live sessions, practice, and project work. We keep the schedule manageable for busy students."},
    {"question": "Are the sessions recorded?", "answer": "Yes. Every live session is recorded and shared with the cohort so you can catch up at your own pace."},
    {"question": "What if I fall behind?", "answer": "We offer support through our community and Q&A sessions, plus recordings so you can review lessons at any time."},
    {"question": "Is there a refund policy?", "answer": "Yes. We offer a refund window for early sign-ups. Contact us directly if you need help and we will respond quickly."},
    {"question": "Will you expand to other cities or do offline classes?", "answer": "We are focused on online delivery for now, but we plan to explore local meetups and hybrid formats as we grow."},
    {"question": "How is this different from free YouTube tutorials?", "answer": "Belajar.id is cohort-based, mentor-led, and project-focused. You get guided learning, community accountability, and real feedback."},
    {"question": "Can I pay in installments?", "answer": "We are working on installment options and will share details with early registrants. Reach out if you are interested."}
]


# ---------------------------------------------------------------------------
# Folder-based content loading
#
#   static/course/<course-folder>/meta.json        course details
#   static/course/<course-folder>/curriculum.csv   week,topic rows
#   static/course/<course-folder>/thumbnail.<ext>  course image
#   static/instructors/<person-folder>/profile.json   name, role, bio, order
#   static/instructors/<person-folder>/photo.<ext>    profile image
#
# Everything is read on each request, so adding or editing a folder shows up
# on the next page refresh without restarting the server.
# ---------------------------------------------------------------------------

def _courses_dir():
    return os.path.join(app.static_folder, "course")


def _instructors_dir():
    return os.path.join(app.static_folder, "instructors")


def _trash_dir():
    # Outside static/ on purpose, so deleted courses are not downloadable from the website.
    return os.path.join(os.path.dirname(app.static_folder), "_trash")


def _subfolders(root):
    """Sorted content folders inside root (ignores files and names starting with . or _)."""
    if not os.path.isdir(root):
        return []
    return [
        name for name in sorted(os.listdir(root))
        if os.path.isdir(os.path.join(root, name)) and not name.startswith((".", "_"))
    ]


def _read_json_checked(path):
    """Return (data, error). error is None when the file is a valid JSON object."""
    if not os.path.isfile(path):
        return None, "file not found"
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, f"invalid JSON: {exc}"
    if not isinstance(data, dict):
        return None, "must contain a JSON object ({ ... })"
    return data, None


def _read_json(path):
    """Return parsed JSON object, or None (and log why) if missing, invalid or not an object."""
    data, err = _read_json_checked(path)
    if err == "file not found":
        log.warning("Missing file: %s", path)
    elif err:
        log.error("Could not read %s: %s", path, err)
    return data


def _read_text(path):
    """File contents (BOM stripped) or None when missing or unreadable."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _find_image(folder, stem):
    """Filename of '<stem>.<image ext>' inside folder (case-insensitive), or None."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    for name in names:
        base, ext = os.path.splitext(name)
        if base.lower() == stem and ext.lower() in IMAGE_EXTS:
            return name
    return None


def _text(value):
    return "" if value is None else str(value).strip()


def _text_list(value):
    """Accept a list of strings (or a single string) and return clean non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [_text(item) for item in value if _text(item)]


def _int(value, default=100):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_false(value):
    return value is False or _text(value).lower() in ("false", "no", "0", "off")


def _norm(name):
    return " ".join(_text(name).split()).casefold()


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_date(text):
    """datetime for a 'YYYY-MM-DD' string, else None."""
    if not DATE_RE.match(text or ""):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None


def _added_info(meta, meta_path):
    """(timestamp, 'YYYY-MM-DD', came_from_file) for deciding which course is the newest.

    Uses the 'added' date in meta.json. Without one it falls back to the file's modified time,
    which is why the admin console always writes an explicit date.
    """
    parsed = _valid_date(_text(meta.get("added")))
    if parsed:
        return parsed.timestamp(), parsed.strftime("%Y-%m-%d"), False
    try:
        ts = os.path.getmtime(meta_path)
    except OSError:
        return 0.0, "", True
    return ts, datetime.fromtimestamp(ts).strftime("%Y-%m-%d"), True


def load_instructors():
    """Every instructor folder as a dict: name, role, bio, photo, slug."""
    root = _instructors_dir()
    people = []
    for folder in _subfolders(root):
        path = os.path.join(root, folder)
        profile_path = os.path.join(path, "profile.json")
        if os.path.isfile(profile_path):
            data = _read_json(profile_path)
            if data is None:          # broken JSON: skip this person, error already logged
                continue
        else:
            log.warning("Instructor folder '%s' has no profile.json; using the folder name", folder)
            data = {}
        photo = _find_image(path, "photo")
        people.append({
            "slug": folder,
            "name": _text(data.get("name")) or folder.replace("-", " ").replace("_", " ").title(),
            "role": _text(data.get("role")),
            "bio": _text(data.get("bio")),
            "photo": f"instructors/{folder}/{photo}" if photo else None,
            "order": _int(data.get("order")),
        })
    people.sort(key=lambda p: (p["order"], p["name"].casefold()))
    return people


def _instructor_index(instructors):
    index = {}
    for person in instructors:
        index.setdefault(_norm(person["name"]), person)
        index.setdefault(_norm(person["slug"].replace("-", " ").replace("_", " ")), person)
    return index


_WEEK_HEADERS = ("week", "week_range", "weeks", "title")


def _parse_curriculum(rows):
    """rows: iterable of lists (csv rows). Columns: week, topic. Blank week = same week as the row above."""
    rows = list(rows)
    if not rows:
        return []
    first = _norm(rows[0][0]) if rows[0] else ""
    if first in _WEEK_HEADERS:          # a file without a header row is fine too
        rows = rows[1:]
    groups = {}
    current = None
    for row in rows:
        if not row or not any(cell.strip() for cell in row):
            continue
        week = row[0].strip() if len(row) > 0 else ""
        topic = row[1].strip() if len(row) > 1 else ""
        if week:
            current = week
        if current is None:
            continue
        bucket = groups.setdefault(current, [])
        if topic:
            bucket.append(topic)
    return [{"week_range": week, "details": details} for week, details in groups.items()]


def _load_curriculum(path):
    if not os.path.isfile(path):
        log.warning("Missing file: %s", path)
        return []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return _parse_curriculum(csv.reader(fh))
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        log.error("Could not read %s: %s", path, exc)
        return []


def load_courses(instructors=None):
    """Every published course folder, sorted by 'order' then title."""
    if instructors is None:
        instructors = load_instructors()
    index = _instructor_index(instructors)
    root = _courses_dir()
    courses = []
    for folder in _subfolders(root):
        path = os.path.join(root, folder)
        meta = _read_json(os.path.join(path, "meta.json"))
        if meta is None:              # not a course folder, or broken JSON (already logged)
            continue
        if _is_false(meta.get("published", True)):
            continue

        team = []
        for name in _text_list(meta.get("instructors")):
            person = index.get(_norm(name))
            if person is None:
                log.warning("Course '%s' lists instructor '%s' but no matching folder in static/instructors", folder, name)
                person = {"slug": None, "name": name, "role": "", "bio": "", "photo": None, "order": 100}
            team.append(person)

        thumb = _find_image(path, "thumbnail")
        subtitle = _text(meta.get("subtitle"))
        added_ts, added_label, _ = _added_info(meta, os.path.join(path, "meta.json"))
        courses.append({
            "slug": folder,
            "title": _text(meta.get("title")) or folder.replace("-", " ").replace("_", " ").title(),
            "tagline": _text(meta.get("tagline")),
            "subtitle": subtitle,
            "description": _text(meta.get("description")) or subtitle,
            "duration": _text(meta.get("duration")),
            "capacity": _text(meta.get("capacity")),
            "certificate": _text(meta.get("certificate")),
            "price": _text(meta.get("price")),
            "start_date": _text(meta.get("start_date")),
            "enroll_link": _text(meta.get("enroll_link")),
            "webinar_link": _text(meta.get("webinar_link")),
            "features": _text_list(meta.get("features")),
            "learn": _text_list(meta.get("what_you_will_learn")),
            "instructors": team,
            "curriculum": _load_curriculum(os.path.join(path, "curriculum.csv")),
            "thumbnail": f"course/{folder}/{thumb}" if thumb else None,
            "order": _int(meta.get("order")),
            "added": added_label,
            "added_ts": added_ts,
        })
    courses.sort(key=lambda c: (c["order"], c["title"].casefold()))
    return courses


def newest_courses(all_courses, limit):
    """The `limit` most recently added courses, newest first (same day: the higher 'order' counts as newer)."""
    return sorted(all_courses, key=lambda c: (-c["added_ts"], -c["order"], c["title"].casefold()))[:limit]


@app.context_processor
def inject_random_logo():
    """Inject a random logo filename from static/logo into all templates as `logo`.

    Falls back to a sensible name if the folder is missing or empty.
    """
    logos_dir = os.path.join(app.static_folder or 'static', 'logo')
    try:
        files = [f for f in os.listdir(logos_dir) if os.path.splitext(f)[1].lower() in ('.png', '.jpg', '.jpeg', '.svg', '.webp', '.gif')]
    except Exception:
        files = []

    if files:
        chosen = random.choice(files)
    else:
        # sensible default; templates should handle missing file gracefully
        chosen = ''

    return {'logo': chosen}


@app.route("/")
def home():
    return render_template(
        "home.html",
        featured_courses=newest_courses(load_courses(), 2),
        pipeline_courses=pipeline_courses,
        show_banner=True
    )

@app.route("/courses")
def courses():
    return render_template(
        "courses.html",
        courses=load_courses(),
        pipeline_courses=pipeline_courses
    )

# "/course" (no slug) opens the first course, so existing links and the header "Enroll Now" button keep working.
@app.route("/course", defaults={"slug": None})
@app.route("/course/<slug>")
def course_details(slug):
    all_courses = load_courses()
    if slug is None:
        if not all_courses:
            flash("No courses are open right now. Check back soon!", "error")
            return redirect(url_for("courses"))
        course = all_courses[0]
    else:
        course = next((c for c in all_courses if c["slug"] == slug), None)
        if course is None:
            abort(404)
    return render_template("course_details.html", course=course)

@app.route("/webinar", methods=["GET", "POST"])
def webinar():
    if request.method == "POST":
        name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip()
        university = request.form.get("university", "").strip()
        major = request.form.get("major", "").strip()
        if not name or not email:
            flash("Please provide your name and email to reserve your spot.", "error")
        else:
            flash(f"Thanks, {name}! Your webinar seat is reserved. Check {email} for the Zoom link.", "success")
        return redirect(url_for("webinar"))
    return render_template("webinar.html")

@app.route("/about")
def about():
    return render_template("about.html", team_members=load_instructors())

@app.route("/faq", methods=["GET", "POST"])
def faq():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        message = request.form.get("message", "").strip()
        if not name or not email or not message:
            flash("Please fill out your name, email, and message before sending.", "error")
        else:
            flash("Message received! We will reply within a few hours.", "success")
        return redirect(url_for("faq"))
    return render_template("faq.html", faq_questions=faq_questions)


# ===========================================================================
# ADMIN CONSOLE  (not linked from the public site; protected by a password)
#
#   * every page except /login needs a valid session (8 h idle timeout)
#   * every POST needs the CSRF token of the session
#   * 5 wrong passwords from one IP = 5 minute lock
#   * "delete" moves the course to _trash/ (next to app.py) so it can be restored
#   * every write is atomic (temp file + rename) so a crash can't leave half a file
# ===========================================================================

admin_bp = Blueprint("admin", __name__, url_prefix=ADMIN_PREFIX)

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED_SLUGS = {"new"}
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
TRASH_RE = re.compile(r"^(?P<slug>[^/\\\x00]+?)__(?P<ts>\d{8}-\d{6})(?:-\d+)?$")

STRING_KEYS = ["title", "tagline", "subtitle", "description", "duration", "capacity",
               "certificate", "price", "start_date", "enroll_link", "webinar_link"]
LIST_KEYS = ["features", "what_you_will_learn", "instructors"]
META_KEYS = STRING_KEYS + LIST_KEYS + ["order", "added", "published"]

ADMIN_IDLE_SECONDS = 8 * 3600
UPLOAD_LIMIT = 5 * 1024 * 1024
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_SECONDS = 300
_login_state = {}
_login_lock = threading.Lock()


# ---------- template filters ----------

@app.template_filter("filesize")
def _filesize(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


@app.template_filter("fmt_ts")
def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "—"


@app.template_filter("ago")
def _ago(ts):
    if not ts:
        return "—"
    seconds = max(0, int(time.time() - ts))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


_JSON_TOKEN = re.compile(r'("(?:\\.|[^"\\])*")(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?')


@app.template_filter("json_hl")
def _json_hl(text):
    """Very small JSON syntax highlighter. Everything is HTML-escaped."""
    text = text or ""
    out, pos = [], 0
    for m in _JSON_TOKEN.finditer(text):
        out.append(escape(text[pos:m.start()]))
        if m.group(1):
            css = "j-key" if m.group(2) else "j-str"
            out.append(Markup('<span class="%s">%s</span>%s') % (css, m.group(1), m.group(2) or ""))
        elif m.group(0) in ("true", "false", "null"):
            out.append(Markup('<span class="j-lit">%s</span>') % m.group(0))
        else:
            out.append(Markup('<span class="j-num">%s</span>') % m.group(0))
        pos = m.end()
    out.append(escape(text[pos:]))
    return Markup("").join(out)


# ---------- auth / csrf ----------

def _csrf_token():
    token = session.get("csrf")
    if not token:
        token = session["csrf"] = secrets.token_hex(16)
    return token


def _admin_session_valid():
    ts = session.get("admin_ts")
    if not session.get("admin") or not ts:
        return False
    if time.time() - ts > ADMIN_IDLE_SECONDS:
        session.clear()
        return False
    session["admin_ts"] = time.time()
    return True


def _login_wait(ip):
    """Seconds left on a lockout for this IP (0 = not locked)."""
    with _login_lock:
        state = _login_state.get(ip)
        if state and state["until"] > time.time():
            return int(state["until"] - time.time()) + 1
    return 0


def _login_failed(ip):
    with _login_lock:
        state = _login_state.setdefault(ip, {"n": 0, "until": 0})
        state["n"] += 1
        if state["n"] >= _LOGIN_MAX_FAILS:
            state["until"] = time.time() + _LOGIN_LOCK_SECONDS
            state["n"] = 0


def _login_ok(ip):
    with _login_lock:
        _login_state.pop(ip, None)


def _safe_next(target):
    """Only follow ?next= when it stays inside the admin console."""
    if target and target.startswith(ADMIN_PREFIX + "/") and "//" not in target[1:] and "\\" not in target:
        return target
    return None


@admin_bp.before_request
def _admin_guard():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
            abort(400, "CSRF check failed. Reload the page and try again.")
    if request.endpoint == "admin.login":
        return None
    if not _admin_session_valid():
        target = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("admin.login", next=target))
    return None


@admin_bp.after_request
def _admin_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@admin_bp.context_processor
def _admin_context():
    logged_in = bool(session.get("admin"))
    return {
        "csrf_token": _csrf_token(),
        "trash_count": len(_trash_items()) if logged_in else 0,
        "debug": app.debug,
        "admin_since": session.get("admin_since"),
    }


# ---------- file helpers ----------

def _atomic_write(path, data, encoding="utf-8"):
    """Write str (or bytes) to path via a temp file in the same folder, then rename over it."""
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        if isinstance(data, bytes):
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        else:
            with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
                fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _tree_stats(path):
    """(total bytes, file count, newest mtime) of everything inside path."""
    size = count = 0
    newest = 0.0
    for dirpath, _, names in os.walk(path):
        for name in names:
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            size += st.st_size
            count += 1
            newest = max(newest, st.st_mtime)
    return size, count, newest


def _sniff_image(data):
    """Detect the real image type from the first bytes (the filename is not trusted). SVG is refused on purpose."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _slug_error(slug):
    if not slug:
        return "Folder name (slug) is required."
    if len(slug) > 60:
        return "Folder name is too long (max 60 characters)."
    if not SLUG_RE.match(slug):
        return "Folder name may only use lowercase letters, digits and single hyphens (e.g. web-dev-basics)."
    if slug in RESERVED_SLUGS:
        return f"'{slug}' is reserved; pick another folder name."
    return None


def _course_path(slug):
    """Absolute path of an existing course folder, or 404. Refuses anything that could escape static/course."""
    root = _courses_dir()
    if (not slug or slug in (".", "..") or slug.startswith((".", "_"))
            or slug != os.path.basename(slug) or "\\" in slug or "\x00" in slug):
        abort(404)
    path = os.path.join(root, slug)
    if not os.path.isdir(path) or os.path.dirname(os.path.realpath(path)) != os.path.realpath(root):
        abort(404)
    return path


def _unique_slug(base, root=None):
    root = root or _courses_dir()
    if not os.path.exists(os.path.join(root, base)):
        return base
    n = 2
    while os.path.exists(os.path.join(root, f"{base}-{n}")):
        n += 1
    return f"{base}-{n}"


def _curriculum_to_csv(groups):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["week", "topic"])
    for group in groups:
        if not group["details"]:
            writer.writerow([group["week_range"], ""])
        for i, topic in enumerate(group["details"]):
            writer.writerow([group["week_range"] if i == 0 else "", topic])
    return buf.getvalue()


def _dump_meta(meta):
    ordered = {k: meta[k] for k in META_KEYS if k in meta}
    ordered.update({k: v for k, v in meta.items() if k not in ordered})   # keep hand-added keys
    return json.dumps(ordered, indent=4, ensure_ascii=False) + "\n"


def _remove_thumbnails(path, keep=None):
    """Delete every thumbnail.* image except the file named `keep` (compared by identity, so it is safe on Windows)."""
    keep_path = os.path.join(path, keep) if keep else None
    for name in os.listdir(path):
        base, ext = os.path.splitext(name)
        if base.lower() != "thumbnail" or ext.lower() not in IMAGE_EXTS:
            continue
        full = os.path.join(path, name)
        if keep_path and os.path.exists(keep_path) and os.path.samefile(full, keep_path):
            continue
        os.remove(full)


# ---------- scanning (what the dashboard shows) ----------

def scan_courses():
    """Every course folder, including hidden and broken ones, with diagnostics."""
    root = _courses_dir()
    instructors = load_instructors()
    index = _instructor_index(instructors)
    entries = []
    for folder in _subfolders(root):
        path = os.path.join(root, folder)
        meta_path = os.path.join(path, "meta.json")
        meta, err = _read_json_checked(meta_path)
        issues = []

        def add(level, text):
            issues.append({"level": level, "text": text})

        if not SLUG_RE.match(folder):
            add("warn", f"folder name '{folder}' is not URL-friendly (use lowercase-with-hyphens)")

        meta_text = _read_text(meta_path)
        if err:
            add("error", f"meta.json: {err}" + ("; this folder is ignored by the public site" if err == "file not found" else ""))
            meta = {}
        status = "broken" if err else ("hidden" if _is_false(meta.get("published", True)) else "published")
        if status == "hidden":
            add("info", "hidden from the public site (published: false)")

        if not err:
            unknown = [k for k in meta if k not in META_KEYS]
            if unknown:
                hints = []
                for key in unknown:
                    close = difflib.get_close_matches(key, META_KEYS, n=1, cutoff=0.7)
                    hints.append(f"'{key}'" + (f" (did you mean '{close[0]}'?)" if close else ""))
                add("info", "unknown key(s) in meta.json are ignored by the site: " + ", ".join(hints))
            if not _text(meta.get("title")):
                add("warn", "no title; the folder name is used instead")
            if not _text(meta.get("enroll_link")):
                add("warn", "no enroll_link; the Enroll buttons are hidden")
            for key in ("enroll_link", "webinar_link"):
                value = _text(meta.get(key))
                if value and not URL_RE.match(value):
                    add("warn", f"{key} is not an http(s) URL: {value}")
            if not _text(meta.get("price")):
                add("info", "no price set")
            if _text(meta.get("added")) and not _valid_date(_text(meta.get("added"))):
                add("warn", f"'added' must look like 2026-10-26 (got '{_text(meta.get('added'))}'); the file's modified time is used instead")
            elif not _text(meta.get("added")):
                add("info", "no 'added' date; the file's modified time decides which courses count as recent (open and save it to fix the date)")
            for key in LIST_KEYS:
                if key in meta and not isinstance(meta[key], list):
                    add("warn", f"'{key}' should be a list like [\"a\", \"b\"]")
            if "order" in meta and not isinstance(meta["order"], int):
                add("warn", "'order' should be a whole number")
            if "published" in meta and not isinstance(meta["published"], bool):
                add("warn", "'published' should be true or false (no quotes)")
            if not _text_list(meta.get("features")):
                add("info", "no features (price box will be empty)")
            if not _text_list(meta.get("what_you_will_learn")):
                add("info", "no what_you_will_learn items (that section is hidden)")

        names = _text_list(meta.get("instructors")) if not err else []
        if not err and not names:
            add("info", "no instructors listed")
        for name in names:
            if index.get(_norm(name)) is None:
                add("warn", f"instructor '{name}' has no matching profile in static/instructors")

        csv_path = os.path.join(path, "curriculum.csv")
        csv_text = _read_text(csv_path)
        weeks = topics = 0
        if csv_text is None:
            add("warn" if not os.path.isfile(csv_path) else "error",
                "curriculum.csv not found" if not os.path.isfile(csv_path) else "curriculum.csv is not readable as UTF-8")
        else:
            try:
                groups = _parse_curriculum(csv.reader(io.StringIO(csv_text, newline="")))
                weeks = len(groups)
                topics = sum(len(g["details"]) for g in groups)
                if not groups:
                    add("warn", "curriculum.csv has no weeks (the Curriculum section is hidden)")
            except csv.Error as exc:
                add("error", f"curriculum.csv: {exc}")

        thumb = _find_image(path, "thumbnail")
        if not thumb:
            add("warn", "no thumbnail image (thumbnail.jpg / .png / .webp)")

        files = []
        for dirpath, _, names_ in os.walk(path):
            for name in sorted(names_):
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                files.append({"name": os.path.relpath(full, path).replace(os.sep, "/"), "size": st.st_size, "mtime": st.st_mtime})
        files.sort(key=lambda f: f["name"])

        added_label = _added_info(meta, meta_path)[1] if not err else ""

        entries.append({
            "slug": folder,
            "status": status,
            "added": added_label,
            "title": _text(meta.get("title")) or folder,
            "tagline": _text(meta.get("tagline")),
            "price": _text(meta.get("price")),
            "order": _int(meta.get("order")) if not err else None,
            "weeks": weeks,
            "topics": topics,
            "instructor_names": names,
            "thumb": f"course/{folder}/{thumb}" if thumb else None,
            "files": files,
            "size": sum(f["size"] for f in files),
            "mtime": max((f["mtime"] for f in files), default=0),
            "issues": issues,
            "n_error": sum(1 for i in issues if i["level"] == "error"),
            "n_warn": sum(1 for i in issues if i["level"] == "warn"),
            "meta_text": (meta_text or "")[:20000],
            "csv_text": (csv_text or "")[:6000],
        })

    # same 'order' on two published courses makes the sort order ambiguous
    seen = {}
    for e in entries:
        if e["status"] == "published":
            seen.setdefault(e["order"], []).append(e)
    for order, group in seen.items():
        if len(group) > 1:
            for e in group:
                e["issues"].append({"level": "info", "text": f"order {order} is shared with another published course (sorted by title)"})
    return entries


def _trash_items():
    root = _trash_dir()
    if not os.path.isdir(root):
        return []
    items = []
    for name in sorted(os.listdir(root), reverse=True):
        path = os.path.join(root, name)
        m = TRASH_RE.match(name)
        if not m or name.startswith(".") or not os.path.isdir(path):
            continue
        meta, _ = _read_json_checked(os.path.join(path, "meta.json"))
        size, count, _ = _tree_stats(path)
        items.append({
            "name": name,
            "slug": m.group("slug"),
            "title": _text((meta or {}).get("title")) or m.group("slug"),
            "deleted": datetime.strptime(m.group("ts"), "%Y%m%d-%H%M%S").timestamp(),
            "size": size,
            "files": count,
        })
    return items


def _trash_path(name):
    if not TRASH_RE.match(name or "") or name.startswith(".") or name != os.path.basename(name):
        abort(404)
    path = os.path.join(_trash_dir(), name)
    if not os.path.isdir(path):
        abort(404)
    return path


# ---------- routes: login / logout ----------

@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET" and _admin_session_valid():
        return redirect(url_for("admin.dashboard"))
    error = None
    status = 200
    if request.method == "POST":
        ip = request.remote_addr or "?"
        wait = _login_wait(ip)
        if wait:
            error = f"Too many attempts. Locked for another {wait // 60 + 1} min."
            status = 429
        elif hmac.compare_digest(request.form.get("password", "").encode(), ADMIN_PASSWORD.encode()):
            _login_ok(ip)
            session.clear()
            session["admin"] = True
            session["admin_ts"] = time.time()
            session["admin_since"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            session["csrf"] = secrets.token_hex(16)
            log.info("admin: login from %s", ip)
            return redirect(_safe_next(request.values.get("next")) or url_for("admin.dashboard"))
        else:
            _login_failed(ip)
            log.warning("admin: failed login from %s", ip)
            error = "Access denied."
            status = 401
    return render_template("admin_login.html", error=error), status


@admin_bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


# ---------- routes: dashboard / system / trash ----------

@admin_bp.route("/")
def dashboard():
    courses_ = scan_courses()
    instructors = load_instructors()
    used = {}
    for c in courses_:
        for name in c["instructor_names"]:
            used[_norm(name)] = used.get(_norm(name), 0) + 1
    for person in instructors:
        person["used_in"] = used.get(_norm(person["name"]), 0)
    stats = {
        "total": len(courses_),
        "published": sum(1 for c in courses_ if c["status"] == "published"),
        "hidden": sum(1 for c in courses_ if c["status"] == "hidden"),
        "broken": sum(1 for c in courses_ if c["status"] == "broken"),
        "weeks": sum(c["weeks"] for c in courses_),
        "topics": sum(c["topics"] for c in courses_),
        "no_thumb": sum(1 for c in courses_ if c["status"] != "broken" and not c["thumb"]),
        "instructors": len(instructors),
        "bytes": sum(c["size"] for c in courses_),
        "errors": sum(c["n_error"] for c in courses_),
        "warnings": sum(c["n_warn"] for c in courses_),
    }
    return render_template("admin_dashboard.html", courses=courses_, stats=stats, instructors=instructors)


@admin_bp.route("/trash")
def trash():
    return render_template("admin_trash.html", items=_trash_items())


@admin_bp.route("/system")
def system():
    def version(pkg):
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return "?"
    logos_dir = os.path.join(app.static_folder, "logo")
    try:
        logos = sorted(f for f in os.listdir(logos_dir) if os.path.splitext(f)[1].lower() in IMAGE_EXTS)
    except OSError:
        logos = []
    routes = sorted(
        ({"rule": r.rule, "methods": ", ".join(sorted(r.methods - {"HEAD", "OPTIONS"})), "endpoint": r.endpoint}
         for r in app.url_map.iter_rules() if r.endpoint != "static"),
        key=lambda r: r["rule"],
    )
    info = {
        "Python": sys.version.split()[0] + " (" + platform.python_implementation() + ")",
        "Flask": version("flask"),
        "Werkzeug": version("werkzeug"),
        "Jinja2": version("jinja2"),
        "OS": platform.platform(),
        "Debug mode": "ON" if app.debug else "off",
        "Project folder": app.root_path,
        "Static folder": app.static_folder,
        "Courses folder": _courses_dir(),
        "Instructors folder": _instructors_dir(),
        "Trash folder": _trash_dir(),
        "Admin URL prefix": ADMIN_PREFIX,
        "Password source": {"env": "ADMIN_PASSWORD env var", "file": "admin_password.txt"}.get(_pw_source, "admin_password.txt (created this run)"),
        "Upload limit": "5 MB per image / 8 MB per request",
        "Session idle timeout": f"{ADMIN_IDLE_SECONDS // 3600} h",
        "Login lockout": f"{_LOGIN_MAX_FAILS} failures = {_LOGIN_LOCK_SECONDS // 60} min",
        "Server time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return render_template("admin_system.html", info=info, routes=routes, logos=logos,
                           logs=_logbook.recent(), instructors=load_instructors(),
                           pipeline_count=len(pipeline_courses), faq_count=len(faq_questions))


# ---------- routes: create / edit course ----------

def _form_from_meta(meta, csv_text, slug, added=""):
    """Initial values for the editor form (all strings, ready to put in inputs)."""
    return {
        "slug": slug or "",
        "added": added,
        **{k: _text(meta.get(k)) for k in STRING_KEYS},
        "features": "\n".join(_text_list(meta.get("features"))),
        "what_you_will_learn": "\n".join(_text_list(meta.get("what_you_will_learn"))),
        "instructors": _text_list(meta.get("instructors")),
        "order": str(meta.get("order", "")) if isinstance(meta.get("order", ""), int) else _text(meta.get("order")),
        "published": not _is_false(meta.get("published", True)) if meta else False,
        "csv": csv_text,
    }


def _lines(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _read_submission(creating, existing_meta, added_default):
    """Validate the posted editor form. Returns (form_for_rerender, result_or_None, errors)."""
    f = request.form
    errors = []
    form = {
        "slug": f.get("slug", "").strip().lower() if creating else "",
        "added": f.get("added", "").strip(),
        **{k: f.get(k, "").strip() for k in STRING_KEYS},
        "features": f.get("features", ""),
        "what_you_will_learn": f.get("what_you_will_learn", ""),
        "instructors": [n.strip() for n in f.getlist("instructors") if n.strip()],
        "order": f.get("order", "").strip(),
        "published": f.get("published") == "on",
        "csv": f.get("curriculum_csv", ""),
    }

    if creating:
        err = _slug_error(form["slug"])
        if err:
            errors.append(err)
        elif os.path.exists(os.path.join(_courses_dir(), form["slug"])):
            errors.append(f"A course folder named '{form['slug']}' already exists.")
    if not form["title"]:
        errors.append("Title is required.")
    order = None
    if form["order"]:
        try:
            order = int(form["order"])
        except ValueError:
            errors.append("Order must be a whole number.")
    for key in ("enroll_link", "webinar_link"):
        if form[key] and not URL_RE.match(form[key]):
            errors.append(f"{key} must start with http:// or https:// (or be left empty).")
    if form["added"] and not _valid_date(form["added"]):
        errors.append("Added date must look like 2026-10-26 (year-month-day), or be left empty.")

    groups = []
    try:
        rows = list(csv.reader(io.StringIO(form["csv"], newline="")))
        groups = _parse_curriculum(rows)
        body = rows[1:] if rows and rows[0] and _norm(rows[0][0]) in _WEEK_HEADERS else rows
        if not groups and any(any(cell.strip() for cell in row) for row in body):
            errors.append("Curriculum has rows but no week label. Start with a row like: Week 1: Basics,First topic")
    except csv.Error as exc:
        errors.append(f"Curriculum CSV could not be parsed: {exc}")

    upload = None
    file = request.files.get("thumbnail")
    if file and file.filename:
        data = file.read(UPLOAD_LIMIT + 1)
        if len(data) > UPLOAD_LIMIT:
            errors.append("Thumbnail is larger than 5 MB.")
        else:
            ext = _sniff_image(data)
            if ext is None:
                errors.append("Thumbnail must be a real JPG, PNG, WEBP or GIF image (SVG is not accepted).")
            else:
                upload = (ext, data)

    if errors:
        return form, None, errors

    meta = dict(existing_meta or {})
    for key in STRING_KEYS:
        meta[key] = form[key]
    meta["features"] = _lines(form["features"])
    meta["what_you_will_learn"] = _lines(form["what_you_will_learn"])
    meta["instructors"] = list(dict.fromkeys(form["instructors"]))
    meta["order"] = order if order is not None else 100
    meta["added"] = form["added"] or added_default
    meta["published"] = form["published"]
    return form, {
        "slug": form["slug"],
        "meta": meta,
        "groups": groups,
        "upload": upload,
        "remove_thumb": f.get("remove_thumbnail") == "on",
    }, []


def _editor(slug):
    creating = slug is None
    path = None if creating else _course_path(slug)
    instructors = load_instructors()
    load_error = None
    existing_meta = {}
    csv_text = "week,topic\n"
    thumb = None
    if not creating:
        existing_meta, load_error = _read_json_checked(os.path.join(path, "meta.json"))
        existing_meta = existing_meta or {}
        csv_text = _read_text(os.path.join(path, "curriculum.csv")) or "week,topic\n"
        thumb = _find_image(path, "thumbnail")

    # Date shown in the "added" field. New course: today. Existing: its own date, else the file's modified date.
    if creating:
        added_default = datetime.now().strftime("%Y-%m-%d")
    else:
        added_default = _added_info(existing_meta, os.path.join(path, "meta.json"))[1]

    errors = []
    if request.method == "POST":
        form, result, errors = _read_submission(creating, existing_meta, added_default)
        if result is not None:
            target = result["slug"] if creating else slug
            target_path = os.path.join(_courses_dir(), target)
            if creating:
                os.makedirs(target_path)
            _atomic_write(os.path.join(target_path, "meta.json"), _dump_meta(result["meta"]))
            _atomic_write(os.path.join(target_path, "curriculum.csv"), "\ufeff" + _curriculum_to_csv(result["groups"]), encoding="utf-8")
            if result["upload"]:
                ext, data = result["upload"]
                name = "thumbnail" + ext
                _atomic_write(os.path.join(target_path, name), data)
                _remove_thumbnails(target_path, keep=name)
            elif result["remove_thumb"]:
                _remove_thumbnails(target_path)
            log.info("admin: %s course '%s'", "created" if creating else "saved", target)
            if creating:
                flash(f"Created '{target}' as a draft. Tick 'published' when it is ready.", "success")
            else:
                flash(f"Saved '{target}'. The public site already shows the new version.", "success")
            return redirect(url_for("admin.course_edit", slug=target))
        status = 400
    else:
        status = 200
        if creating:
            taken = [e["order"] for e in scan_courses() if e["order"] is not None]
            form = _form_from_meta({"order": (max(taken) + 1) if taken else 1, "published": False}, csv_text, "", added_default)
            form["published"] = False
        else:
            form = _form_from_meta(existing_meta, csv_text, slug, added_default)

    known = {_norm(p["name"]) for p in instructors}
    extra = [n for n in form["instructors"] if _norm(n) not in known]
    return render_template(
        "admin_edit.html",
        creating=creating, slug=slug, form=form, errors=errors, load_error=load_error,
        instructors=instructors, extra_instructors=extra,
        thumb=(f"course/{slug}/{thumb}" if thumb else None),
        public_visible=(not creating and not load_error and form["published"]),
    ), status


@admin_bp.route("/course/new", methods=["GET", "POST"])
def course_new():
    return _editor(None)


@admin_bp.route("/course/<slug>", methods=["GET", "POST"])
def course_edit(slug):
    return _editor(slug)


# ---------- routes: delete / restore / duplicate / publish toggle ----------

@admin_bp.post("/course/<slug>/delete")
def course_delete(slug):
    path = _course_path(slug)
    os.makedirs(_trash_dir(), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(_trash_dir(), f"{slug}__{stamp}")
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(_trash_dir(), f"{slug}__{stamp}-{n}")
    shutil.move(path, dest)
    log.info("admin: moved course '%s' to trash", slug)
    flash(f"Moved '{slug}' to trash. You can restore it from the Trash page.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.post("/trash/<name>/restore")
def trash_restore(name):
    src = _trash_path(name)
    slug = TRASH_RE.match(name).group("slug")
    target = slug if not os.path.exists(os.path.join(_courses_dir(), slug)) else _unique_slug(slug + "-restored")
    os.makedirs(_courses_dir(), exist_ok=True)
    shutil.move(src, os.path.join(_courses_dir(), target))
    log.info("admin: restored '%s' as '%s'", name, target)
    flash(f"Restored as '{target}'." + ("" if target == slug else " The original name was taken."), "success")
    return redirect(url_for("admin.course_edit", slug=target))


@admin_bp.post("/trash/<name>/purge")
def trash_purge(name):
    shutil.rmtree(_trash_path(name))
    log.info("admin: permanently deleted '%s'", name)
    flash("Deleted permanently.", "success")
    return redirect(url_for("admin.trash"))


@admin_bp.post("/trash/empty")
def trash_empty():
    items = _trash_items()
    for item in items:
        shutil.rmtree(_trash_path(item["name"]))
    log.info("admin: emptied trash (%d item(s))", len(items))
    flash(f"Trash emptied ({len(items)} item(s) deleted permanently).", "success")
    return redirect(url_for("admin.trash"))


@admin_bp.post("/course/<slug>/duplicate")
def course_duplicate(slug):
    path = _course_path(slug)
    base = (re.sub(r"[^a-z0-9-]+", "-", slug.lower()).strip("-") or "course") + "-copy"
    new_slug = _unique_slug(base)
    new_path = os.path.join(_courses_dir(), new_slug)
    shutil.copytree(path, new_path)
    meta, err = _read_json_checked(os.path.join(new_path, "meta.json"))
    if meta is not None:
        meta["published"] = False
        meta["added"] = datetime.now().strftime("%Y-%m-%d")
        meta["title"] = (_text(meta.get("title")) or slug) + " (copy)"
        _atomic_write(os.path.join(new_path, "meta.json"), _dump_meta(meta))
    log.info("admin: duplicated '%s' as '%s'", slug, new_slug)
    flash(f"Duplicated as '{new_slug}' (hidden until you publish it).", "success")
    return redirect(url_for("admin.course_edit", slug=new_slug))


@admin_bp.post("/course/<slug>/toggle")
def course_toggle(slug):
    path = _course_path(slug)
    meta, err = _read_json_checked(os.path.join(path, "meta.json"))
    if meta is None:
        flash(f"Cannot change '{slug}': meta.json {err}. Open it in the editor to repair it.", "error")
    else:
        now_published = _is_false(meta.get("published", True))
        if not _valid_date(_text(meta.get("added"))):
            # pin the date before rewriting the file, so a publish/hide click can't make an old course look new
            meta["added"] = _added_info(meta, os.path.join(path, "meta.json"))[1]
        meta["published"] = now_published
        _atomic_write(os.path.join(path, "meta.json"), _dump_meta(meta))
        log.info("admin: '%s' is now %s", slug, "published" if now_published else "hidden")
        flash(f"'{slug}' is now {'published' if now_published else 'hidden'}.", "success")
    return redirect(url_for("admin.dashboard"))


app.register_blueprint(admin_bp)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
