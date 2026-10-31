from flask import (Flask, Blueprint, render_template, request, redirect, url_for,
                   flash, abort, session, jsonify, Response, send_file, send_from_directory)
from markupsafe import Markup, escape
import csv
import difflib
import functools
import hmac
import io
import json
import logging
import mimetypes
import os
import platform
import random
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
import zlib
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from importlib import metadata

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Name. LEGACY: this project began as "Belajar.id" (its original working name, still the name of
# the old export manifest and of old zips, which the importer keeps accepting). The public brand
# is SITE_NAME: change it here, or with the SITE_NAME environment variable, and every page follows.
# ---------------------------------------------------------------------------
LEGACY_NAMES = ("belajar", "Belajar.id")
SITE_NAME = os.environ.get("SITE_NAME", "").strip() or "Skillwaves"

# ---------------------------------------------------------------------------
# Folders. static/ holds the site's skeleton only (static/css, static/js). Everything that is
# content (courses, instructors, webinars, pictures, logos) lives in content/ and is served
# at /media/. Old installs that still keep content in static/ are moved over at start-up.
# ---------------------------------------------------------------------------
CONTENT_DIR = os.path.join(app.root_path, "content")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,  # hard cap for any request body (each upload is capped at 5 MB)
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


log = logging.getLogger("skillwaves")
log.setLevel(logging.INFO)
log.propagate = False
_logbook = _LogBook()
log.addHandler(_logbook)
_console = logging.StreamHandler()
_console.setLevel(logging.WARNING)
_console.setFormatter(logging.Formatter("%(levelname)s [skillwaves] %(message)s"))
log.addHandler(_console)


LEGACY_CONTENT_NAMES = ("course", "instructors", "webinars", "images", "logo", "webinar.jpg")


def _migrate_legacy_layout():
    """Old layout: content inside static/. Move it into content/ once (nothing is overwritten or deleted)."""
    old_root = app.static_folder
    moved = []
    os.makedirs(CONTENT_DIR, exist_ok=True)
    for name in LEGACY_CONTENT_NAMES:
        src = os.path.join(old_root, name)
        if not os.path.lexists(src) or os.path.islink(src):
            continue
        dst = os.path.join(CONTENT_DIR, name)
        try:
            os.makedirs(CONTENT_DIR, exist_ok=True)
            if os.path.isdir(src):
                os.makedirs(dst, exist_ok=True)
                for entry in sorted(os.listdir(src)):
                    target = os.path.join(dst, entry)
                    if os.path.lexists(target):
                        if entry != ".gitkeep":
                            log.warning("Legacy layout: static/%s/%s was left where it is because content/%s/%s already exists", name, entry, name, entry)
                        continue
                    shutil.move(os.path.join(src, entry), target)
                    moved.append(f"{name}/{entry}")
                if not os.listdir(src) or os.listdir(src) == [".gitkeep"]:
                    shutil.rmtree(src)
            elif not os.path.lexists(dst):
                shutil.move(src, dst)
                moved.append(name)
        except OSError as exc:
            log.warning("Legacy layout: could not move static/%s into content/ (%s)", name, exc)
    if moved:
        log.warning("Legacy layout: moved %d item(s) from static/ to content/ (static/ now holds css and js only)", len(moved))


_migrate_legacy_layout()


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
    {"question": "How is this different from free YouTube tutorials?", "answer": f"{SITE_NAME} is cohort-based, mentor-led, and project-focused. You get guided learning, community accountability, and real feedback."},
    {"question": "Can I pay in installments?", "answer": "We are working on installment options and will share details with early registrants. Reach out if you are interested."}
]


# ---------------------------------------------------------------------------
# Folder-based content loading
#
#   content/course/<course-folder>/meta.json        course details
#   content/course/<course-folder>/curriculum.csv   week,topic rows
#   content/course/<course-folder>/thumbnail.<ext>  course image
#   content/instructors/<person-folder>/profile.json   name, role, bio, order
#   content/instructors/<person-folder>/photo.<ext>    profile image
#
# Everything is read on each request, so adding or editing a folder shows up
# on the next page refresh without restarting the server.
# ---------------------------------------------------------------------------

def _courses_dir():
    return os.path.join(CONTENT_DIR, "course")


def _instructors_dir():
    return os.path.join(CONTENT_DIR, "instructors")


def _trash_dir():
    # Outside content/ on purpose, so deleted courses are not downloadable from the website.
    return os.path.join(app.root_path, "_trash")


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


def _is_true(value):
    return value is True or _text(value).lower() in ("true", "yes", "1", "on")


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


# ---------------------------------------------------------------------------
# Discounts. meta.json keeps the intended price as text ("Tk 500.00") plus an optional
# "discount" percentage. The final price is computed with whole-number maths only, and the
# admin editor's live preview (JavaScript) uses the very same steps, so they always agree.
# ---------------------------------------------------------------------------

_PRICE_NUM = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")


def _split_price(text):
    """'Tk 1,500.50/mo' -> ('Tk ', 150050, 2, True, '/mo') = prefix, value scaled by 10**decimals, decimals, uses commas, suffix."""
    m = _PRICE_NUM.search(text or "")
    if not m:
        return None
    whole, frac = m.group(1), (m.group(2) or "")
    return (text[:m.start()], int(whole.replace(",", "") + frac[1:]), max(len(frac) - 1, 0),
            "," in whole, text[m.end():])


def parse_discount(raw):
    """20, '20', '20%', '12.5 %' -> hundredths of a percent (2000, 1250). Empty or 0 -> None. Bad input -> ValueError."""
    if raw is None or raw is False:
        return None
    if raw is True:
        raise ValueError("Discount must be a number like 20 or 12.5.")
    text = str(raw).replace("%", "").strip()
    if text == "":
        return None
    # plain digits only (the editor's live preview uses exactly the same rule)
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        raise ValueError("Discount must be a number like 20 or 12.5.")
    whole, _, frac = text.partition(".")
    if frac[2:].strip("0"):
        raise ValueError("Discount can have at most 2 decimal places.")
    d100 = int(whole) * 100 + int((frac + "00")[:2])
    if d100 >= 10000:
        raise ValueError("Discount must be between 0 and 99.99 percent.")
    return d100 or None


def price_with_discount(price_text, d100):
    """Price text with `d100` hundredths of a percent taken off, same currency text and number style; None if impossible."""
    parts = _split_price(price_text)
    if parts is None or not d100:
        return None
    prefix, scaled, decimals, commas, suffix = parts
    final = (scaled * (10000 - d100) + 5000) // 10000          # half-up rounding, integers only
    whole, frac = divmod(final, 10 ** decimals)
    body = f"{whole:,}" if commas else str(whole)
    if decimals:
        body += "." + str(frac).zfill(decimals)
    return f"{prefix}{body}{suffix}"


def format_percent(d100):
    """2000 -> '20', 1250 -> '12.5'."""
    text = f"{Decimal(d100) / 100:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _discount_info(meta):
    """(final_price_text or '', percent_label or '', problem or ''). Problem is a message for the admin diagnostics."""
    price = _text(meta.get("price"))
    try:
        d100 = parse_discount(meta.get("discount"))
    except ValueError as exc:
        return "", "", str(exc)
    if not d100:
        return "", "", ""
    final = price_with_discount(price, d100)
    if final is None:
        return "", "", "discount is set but price has no number in it, so no discount can be shown"
    return final, format_percent(d100), ""


# Soft background colours for the cards on the Courses page. Assigned from the folder name, so a
# course keeps its colour between page loads, and a card never matches the ones right next to it.
CARD_TINTS = ["#e8f1ff", "#e8f8ee", "#fff1de", "#f3e8ff", "#ffe8ee",
              "#e4f6f8", "#fffbd9", "#eaf0ff", "#eef8dc", "#ffe9e0"]


def assign_tints(course_list):
    used = []
    for c in course_list:
        idx = zlib.crc32(c["slug"].encode("utf-8")) % len(CARD_TINTS)
        while idx in used[-3:]:
            idx = (idx + 1) % len(CARD_TINTS)
        used.append(idx)
        c["tint"] = CARD_TINTS[idx]
    return course_list


# ---------------------------------------------------------------------------
# Instructor profiles: content/instructors/<folder>/profile.json (+ photo.<ext>, cv.pdf)
#
# Only name is really needed. Everything else is optional and a section is simply not shown
# on the public profile page when it is empty (a fresh graduate has no job experience, and
# that is fine). Hand-edited JSON is cleaned on load, so a typo can never break a page.
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[^@\s<>\"']+$")
PHONE_RE = re.compile(r"^\+?[\d\s().-]{5,25}$")
BARE_URL_RE = re.compile(r"^(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:[/?#]\S*)?$")

# repeatable entries: section -> field names. The first field is the headline of the entry.
ENTRY_FIELDS = {
    "education": ("degree", "institution", "period", "details"),
    "experience": ("role", "organization", "period", "details"),
    "certifications": ("name", "issuer", "year", "link"),
    "achievements": ("title", "year", "details", "link"),
}
SOCIAL_SUGGESTIONS = ["LinkedIn", "GitHub", "Facebook", "Instagram", "X (Twitter)", "YouTube", "Telegram",
                      "WhatsApp", "Google Scholar", "ResearchGate", "Behance", "Medium", "Portfolio"]
PROFILE_KEYS = ["name", "role", "bio", "biography", "teaching_approach", "location", "email", "phone",
                "website", "socials", "expertise", "languages", "education", "experience",
                "certifications", "achievements", "sections", "order", "published", "founder", "photo_style"]
CV_NAME = "cv.pdf"


def clean_url(text):
    """('https://…' or '', error). Accepts a bare domain like github.com/me; refuses other schemes (javascript:, data:, …)."""
    text = _text(text)
    if not text:
        return "", None
    if URL_RE.match(text):
        return text, None
    if "://" not in text and ":" not in text.split("/")[0] and BARE_URL_RE.match(text):
        return "https://" + text, None
    return "", "must be a web address starting with http:// or https://"


def _entries(value, fields):
    """List of dicts with exactly `fields`; entries without their headline are dropped."""
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            item = {fields[0]: item}
        if not isinstance(item, dict):
            continue
        entry = {f: _text(item.get(f)) for f in fields}
        if "link" in entry:
            entry["link"] = clean_url(entry["link"])[0]
        if entry[fields[0]]:
            out.append(entry)
    return out


def _socials(value):
    out = []
    if isinstance(value, dict):                       # {"linkedin": "https://…"} also works
        value = [{"platform": str(k), "url": v} for k, v in value.items()]
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        url = clean_url(item.get("url"))[0]
        if url:
            out.append({"platform": _text(item.get("platform")) or "Link", "url": url})
    return out


def _sections(value):
    out = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        items = item.get("items")
        items = _text_list(items.splitlines() if isinstance(items, str) else items)
        if _text(item.get("title")) and items:
            out.append({"title": _text(item.get("title")), "items": items})
    return out


def _instructor_view(folder, data, path):
    """One instructor as the templates see it (all values already cleaned)."""
    photo = _find_image(path, "photo")
    cv_path = os.path.join(path, CV_NAME)
    email = _text(data.get("email"))
    phone = _text(data.get("phone"))
    person = {
        "slug": folder,
        "name": _text(data.get("name")) or folder.replace("-", " ").replace("_", " ").title(),
        "role": _text(data.get("role")),
        "bio": _text(data.get("bio")),
        "biography": _text(data.get("biography")),
        "teaching_approach": _text(data.get("teaching_approach")),
        "photo": f"instructors/{folder}/{photo}" if photo else None,
        "photo_style": data.get("photo_style"),
        "order": _int(data.get("order")),
        "published": not _is_false(data.get("published", True)),
        "founder": _is_true(data.get("founder")),
        "location": _text(data.get("location")),
        "email": email if EMAIL_RE.match(email) else "",
        "phone": phone if PHONE_RE.match(phone) else "",
        "website": clean_url(data.get("website"))[0],
        "socials": _socials(data.get("socials")),
        "expertise": _text_list(data.get("expertise")),
        "languages": _text_list(data.get("languages")),
        "cv": f"instructors/{folder}/{CV_NAME}" if os.path.isfile(cv_path) else None,
        "cv_size": os.path.getsize(cv_path) if os.path.isfile(cv_path) else 0,
        "sections": _sections(data.get("sections")),
    }
    for key, fields in ENTRY_FIELDS.items():
        person[key] = _entries(data.get(key), fields)
    person["phone_link"] = re.sub(r"[^\d+]", "", phone) if person["phone"] else ""
    return person


# what a profile can hold, for the "how complete is it" checklist (label, test)
PROFILE_CHECKS = [
    ("photo", "Photo", lambda p: bool(p["photo"])),
    ("bio", "Short description", lambda p: bool(p["bio"])),
    ("biography", "Biography", lambda p: bool(p["biography"])),
    ("contact", "Contact or links", lambda p: bool(p["email"] or p["phone"] or p["website"] or p["socials"])),
    ("cv", "CV", lambda p: bool(p["cv"])),
    ("expertise", "Expertise", lambda p: bool(p["expertise"])),
    ("education", "Education", lambda p: bool(p["education"])),
    ("experience", "Experience", lambda p: bool(p["experience"])),
    ("more", "Certificates / projects", lambda p: bool(p["certifications"] or p["achievements"] or p["sections"])),
]


def load_instructors(include_hidden=False):
    """Every instructor folder as a dict (see _instructor_view). Hidden ones only when include_hidden."""
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
        person = _instructor_view(folder, data, path)
        if person["published"] or include_hidden:
            people.append(person)
    people.sort(key=lambda p: (p["order"], p["name"].casefold()))
    return people


@app.template_filter("paragraphs")
def _paragraphs(text):
    """Split free text into paragraphs at blank lines (single line breaks stay inside a paragraph)."""
    return [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]


@app.template_filter("initials")
def _initials(name):
    parts = [p for p in re.split(r"\s+", name or "") if p]
    return "".join(p[0] for p in (parts[:1] + parts[-1:] if len(parts) > 1 else parts)).upper()


def _instructor_index(instructors):
    index = {}
    for person in instructors:
        index.setdefault(_norm(person["name"]), person)
        index.setdefault(_norm(person["slug"].replace("-", " ").replace("_", " ")), person)
    return index


# ---------------------------------------------------------------------------
# Curriculum = the instructor's own sheet, shown exactly as written.
#
# curriculum.csv can have ANY columns and ANY rows. Nothing is mapped, grouped or renamed:
# the first row is the table header, every other row is shown as it is. The only tidying is
# dropping empty rows at the top/bottom and columns that have no content at all.
# ---------------------------------------------------------------------------

MAX_SHEET_COLS = 40


def _cell_text(value):
    return "" if value is None else str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def normalize_grid(rows):
    """Clean list of equal-length rows of strings, or [] when the sheet has no content."""
    grid = [[_cell_text(c) for c in row] for row in rows]
    while grid and not any(grid[-1]):
        grid.pop()
    while grid and not any(grid[0]):
        grid.pop(0)
    if not grid:
        return []
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    keep = [j for j in range(width) if any(r[j] for r in grid)]
    return [[r[j] for j in keep] for r in grid]


def parse_sheet(rows):
    """Table for the public page: {'header': [...], 'rows': [[{'text', 'cont'}]], 'ncols', 'nrows'} or None.

    `cont` marks an empty cell sitting under a filled cell of the same column: Excel users merge cells
    that way, so the page draws it joined to the cell above instead of as a separate empty box.
    """
    grid = normalize_grid(rows)
    if not grid:
        return None
    header, body = grid[0], grid[1:]
    chain = [False] * len(header)
    table = []
    for row in body:
        if not any(row):
            table.append([{"text": "", "cont": False} for _ in row])
            chain = [False] * len(header)
            continue
        cells = []
        for j, text in enumerate(row):
            cont = (not text) and chain[j]
            cells.append({"text": text, "cont": cont})
            chain[j] = bool(text) or cont
        table.append(cells)
    return {"header": header, "rows": table, "ncols": len(header), "nrows": len(body)}


def _load_curriculum(path):
    if not os.path.isfile(path):
        log.warning("Missing file: %s", path)
        return None
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return parse_sheet(csv.reader(fh))
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        log.error("Could not read %s: %s", path, exc)
        return None


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


@app.template_filter("cell")
def _cell_filter(text):
    """A sheet cell as safe HTML: line breaks kept, web addresses turned into links."""
    text = text or ""
    out, pos = [], 0
    for m in _URL_IN_TEXT.finditer(text):
        url = m.group(0).rstrip(".,;:!?)")
        end = m.start() + len(url)
        out.append(escape(text[pos:m.start()]))
        out.append(Markup('<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>') % (url, url))
        pos = end
    out.append(escape(text[pos:]))
    return Markup("").join(out).replace("\n", Markup("<br>"))


def load_courses():
    """Every published course folder, sorted by 'order' then title."""
    everyone = load_instructors(include_hidden=True)
    index = _instructor_index([p for p in everyone if p["published"]])
    hidden = _instructor_index([p for p in everyone if not p["published"]])
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
            if person is None and _norm(name) in hidden:
                continue                  # a hidden instructor is not shown anywhere on the public site
            if person is None:
                log.warning("Course '%s' lists instructor '%s' but no matching folder in content/instructors", folder, name)
                person = {"slug": None, "name": name, "role": "", "bio": "", "photo": None, "order": 100}
            team.append(person)

        thumb = _find_image(path, "thumbnail")
        subtitle = _text(meta.get("subtitle"))
        added_ts, added_label, _ = _added_info(meta, os.path.join(path, "meta.json"))
        final_price, discount_label, problem = _discount_info(meta)
        if problem:
            log.warning("Course '%s': %s", folder, problem)
        courses.append({
            "slug": folder,
            "title": _text(meta.get("title")) or folder.replace("-", " ").replace("_", " ").title(),
            "tagline": _text(meta.get("tagline")),
            "subtitle": subtitle,
            "description": _text(meta.get("description")) or subtitle,
            "duration": _text(meta.get("duration")),
            "capacity": _text(meta.get("capacity")),
            "certificate": _text(meta.get("certificate")),
            # `price` is what a visitor pays (discounted when a discount is set);
            # `price_original` / `discount` are only filled for discounted courses.
            "price": final_price or _text(meta.get("price")),
            "price_original": _text(meta.get("price")) if final_price else "",
            "discount": discount_label,
            "start_date": _text(meta.get("start_date")),
            "enroll_link": _text(meta.get("enroll_link")),
            "webinar_link": _text(meta.get("webinar_link")),
            "features": _text_list(meta.get("features")),
            "learn": _text_list(meta.get("what_you_will_learn")),
            "instructors": team,
            "curriculum": _load_curriculum(os.path.join(path, "curriculum.csv")),
            "thumbnail": f"course/{folder}/{thumb}" if thumb else None,
            "thumb_style": meta.get("thumb_style"),
            "order": _int(meta.get("order")),
            "added": added_label,
            "added_ts": added_ts,
        })
    courses.sort(key=lambda c: (c["order"], c["title"].casefold()))
    return courses


def newest_courses(all_courses, limit):
    """The `limit` most recently added courses, newest first (same day: the higher 'order' counts as newer)."""
    return sorted(all_courses, key=lambda c: (-c["added_ts"], -c["order"], c["title"].casefold()))[:limit]


# ---------------------------------------------------------------------------
# Time. The site runs on Bangladesh time (UTC+6, no daylight saving) and never trusts a
# visitor's device clock: every "has it started yet?" decision is made here, on the server.
#
# The server asks an online time service every few minutes from a background thread (a page
# request never waits for it) and remembers how far its own clock is from the real time. If a
# service is down it tries the next one; if all are down it keeps the last known correction,
# and with none at all it falls back to the server clock. The admin "system" page shows which.
# ---------------------------------------------------------------------------

BD_TZ = timezone(timedelta(hours=6), "BST")
TIME_SYNC_EVERY = 600            # seconds between successful checks
TIME_RETRY_EVERY = 60            # seconds between attempts while no service could be reached
TIME_STALE_AFTER = 3600          # a correction older than this is shown as "stale" in the admin
TIME_HTTP_TIMEOUT = 6


def _http_open(url, method="GET"):
    request_ = urllib.request.Request(url, method=method, headers={"User-Agent": "skillwaves-site/1.0"})
    return urllib.request.urlopen(request_, timeout=TIME_HTTP_TIMEOUT)


def _time_from_timeapi():
    with _http_open("https://timeapi.io/api/time/current/zone?timeZone=Asia%2FDhaka") as resp:
        data = json.loads(resp.read(65536).decode("utf-8"))
    local = datetime.strptime(str(data["dateTime"])[:19], "%Y-%m-%dT%H:%M:%S")
    return local.replace(tzinfo=BD_TZ).astimezone(timezone.utc)


def _time_from_worldtimeapi():
    with _http_open("https://worldtimeapi.org/api/timezone/Asia/Dhaka") as resp:
        data = json.loads(resp.read(65536).decode("utf-8"))
    return datetime.fromtimestamp(int(data["unixtime"]), timezone.utc)


def _time_from_http_date():
    with _http_open("https://www.google.com/", "HEAD") as resp:
        stamp = resp.headers["Date"]
    return parsedate_to_datetime(stamp).astimezone(timezone.utc)


# tried in this order; a test can replace the list
TIME_PROVIDERS = [
    ("timeapi.io", _time_from_timeapi),
    ("worldtimeapi.org", _time_from_worldtimeapi),
    ("HTTP Date header (google.com)", _time_from_http_date),
]


class _Clock:
    """Real time = system clock + a correction learned from an online time service."""

    def __init__(self):
        self.offset = 0.0
        self.source = None            # name of the service that gave the current correction
        self.synced_at = None         # system time.time() of the last good check
        self.last_try = None
        self.errors = []              # [(service, message)] from the last failed round
        self.fake = None              # tests only: a datetime or a function returning one
        self._started = False
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def enabled(self):
        return os.environ.get("TIME_SYNC", "on").strip().lower() not in ("off", "0", "false", "no")

    def now(self):
        """The current moment as an aware UTC datetime."""
        fake = self.fake
        if fake is not None:
            return fake() if callable(fake) else fake
        return datetime.fromtimestamp(time.time() + self.offset, timezone.utc)

    def sync(self):
        """Ask the services in order until one answers. Returns True when the correction was updated."""
        self.last_try = time.time()
        errors = []
        for name, fetch in list(TIME_PROVIDERS):
            started = time.time()
            try:
                moment = fetch()
                finished = time.time()
                if moment.tzinfo is None or moment.year < 2024:
                    raise ValueError("implausible time %r" % (moment,))
            except Exception as exc:                  # any network or parsing problem: try the next one
                errors.append((name, str(exc) or exc.__class__.__name__))
                continue
            with self._lock:
                self.offset = moment.timestamp() - (started + finished) / 2
                self.source, self.synced_at, self.errors = name, finished, errors
            return True
        with self._lock:
            self.errors = errors
        log.warning("Time check failed on every service (%s); using the last known time",
                    "; ".join(f"{n}: {m}" for n, m in errors))
        return False

    def _loop(self):
        while not self._stop.is_set():
            ok = self.sync()
            self._stop.wait(TIME_SYNC_EVERY if ok else TIME_RETRY_EVERY)

    def ensure_started(self):
        if self._started or not self.enabled():
            return
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._loop, name="time-sync", daemon=True).start()

    def status(self):
        with self._lock:
            offset, source, synced_at, errors = self.offset, self.source, self.synced_at, list(self.errors)
        if self.fake is not None:
            state, note = "fake", "a fixed test time is in use"
        elif not self.enabled():
            state, note = "off", "online time check is switched off (TIME_SYNC=off); the server clock is used"
        elif synced_at is None:
            state, note = "waiting", "no online time service has answered yet; the server clock is used meanwhile"
        elif time.time() - synced_at > TIME_STALE_AFTER:
            state, note = "stale", "the last online check is old; its correction is still applied"
        else:
            state, note = "ok", "synced with an online time service"
        return {"state": state, "note": note, "source": source, "offset": offset, "errors": errors,
                "synced_at": synced_at, "last_try": self.last_try}


TIME = _Clock()


def now_bd():
    """The current moment in Bangladesh time (aware datetime)."""
    return TIME.now().astimezone(BD_TZ)


@app.before_request
def _start_clock():
    TIME.ensure_started()


# ---------------------------------------------------------------------------
# Static file URLs that change when the file changes, so a replaced logo or image shows up at
# once instead of the browser's cached copy.
# ---------------------------------------------------------------------------

def asset_url(filename):
    """URL of a site file: css/ and js/ come from static/, everything else from content/ (served at /media/)."""
    code = filename.startswith(("css/", "js/"))
    url = url_for("static" if code else "media", filename=filename)
    try:
        stamp = int(os.stat(os.path.join(app.static_folder if code else CONTENT_DIR, filename)).st_mtime)
    except OSError:
        return url
    return f"{url}?v={stamp}"


MEDIA_HIDDEN_NAMES = {"curriculum.csv"}
MEDIA_HIDDEN_EXTS = {".json"}


@app.route("/media/<path:filename>")
def media(filename):
    """Public pictures, logos, CVs and other files from content/. The data files that describe the
    content (.json, curriculum.csv) and anything starting with a dot are never served."""
    name = os.path.basename(filename)
    if (name.startswith(".") or any(seg.startswith(".") for seg in filename.split("/"))
            or name.lower() in MEDIA_HIDDEN_NAMES or os.path.splitext(name)[1].lower() in MEDIA_HIDDEN_EXTS):
        abort(404)
    return send_from_directory(CONTENT_DIR, filename)


@app.after_request
def _svg_headers(response):
    """An SVG opened directly (not through <img>) must not be able to run anything."""
    if request.path.lower().endswith(".svg"):
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
        response.headers["X-Content-Type-Options"] = "nosniff"
    elif request.path.startswith(("/static/", "/media/")):
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ---------------------------------------------------------------------------
# Webinars
#
#   content/webinars/<folder>/webinar.json   title, date, time, links, agenda, ...
#   content/webinars/<folder>/cover.<ext>    optional picture (content/webinar.jpg when missing)
#
# A webinar only has a START time: nobody knows beforehand how long it will run. So
#   upcoming  = before the start time (the join link is NOT shown yet)
#   started   = the start time has passed (the join link is shown)
# and it stays on the site until someone hides or deletes it in the admin console. Nothing is
# removed automatically. A webinar without a date is listed last as "date to be announced".
# ---------------------------------------------------------------------------

WEBINAR_DEFAULT_COVER = "webinar.jpg"
WEBINAR_KEYS = ["title", "subtitle", "description", "date", "time", "platform", "join_link", "rsvp_link",
                "capacity", "agenda", "speakers", "courses", "published", "cover_style"]
TIME_RE = re.compile(r"^(?:[01]?\d|2[0-3]):[0-5]\d$")
HERO_WEBINARS = 3                 # most webinar slides shown in the home slider (after the brand slide)


def _webinars_dir():
    return os.path.join(CONTENT_DIR, "webinars")


def _webinar_start(date_text, time_text):
    """Aware Bangladesh datetime for a date ('YYYY-MM-DD') and time ('HH:MM'), else None."""
    day = _valid_date(date_text)
    if not day or not TIME_RE.match(time_text or ""):
        return None
    hour, minute = (int(part) for part in time_text.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=BD_TZ)


def _clock12(moment):
    return moment.strftime("%I:%M %p").lstrip("0")


def _agenda(value):
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            item = {"item": item}
        if isinstance(item, dict) and _text(item.get("item")):
            out.append({"time": _text(item.get("time")), "item": _text(item.get("item"))})
    return out


def _course_titles():
    """{folder: title} of every published course (used to link webinars to courses)."""
    titles = {}
    for folder in _subfolders(_courses_dir()):
        meta = _read_json_checked(os.path.join(_courses_dir(), folder, "meta.json"))[0]
        if meta is not None and not _is_false(meta.get("published", True)):
            titles[folder] = _text(meta.get("title")) or folder
    return titles


def _webinar_lookups():
    everyone = load_instructors(include_hidden=True)
    return {
        "visible": _instructor_index([p for p in everyone if p["published"]]),
        "hidden": _instructor_index([p for p in everyone if not p["published"]]),
        "courses": _course_titles(),
    }


def _webinar_view(folder, data, path, now, lookups):
    """One webinar as the templates see it. `data` is the parsed webinar.json (cleaned here)."""
    date_text, time_text = _text(data.get("date")), _text(data.get("time"))
    start = _webinar_start(date_text, time_text)
    if start is None:
        status, starts_in, started_for = "tba", None, None
    elif now < start:
        status, starts_in, started_for = "upcoming", int((start - now).total_seconds()) + 1, None
    else:
        status, starts_in, started_for = "started", None, int((now - start).total_seconds())

    speakers = []
    for name in _text_list(data.get("speakers")):
        person = lookups["visible"].get(_norm(name))
        if person is None and _norm(name) in lookups["hidden"]:
            continue                                    # a hidden instructor is not shown anywhere public
        speakers.append(person or {"slug": None, "name": name, "role": "", "photo": None})
    linked = [{"slug": s, "title": lookups["courses"][s]} for s in _text_list(data.get("courses")) if s in lookups["courses"]]

    cover = _find_image(path, "cover")
    description = _text(data.get("description"))
    title = _text(data.get("title")) or folder.replace("-", " ").replace("_", " ").title()
    view = {
        "slug": folder,
        "title": title,
        "subtitle": _text(data.get("subtitle")),
        "description": description,
        "paragraphs": _paragraphs(description),
        "date": date_text if start else "",
        "time": time_text if start else "",
        "start": start,
        "status": status,
        "starts_in": starts_in,
        "started_for": started_for,
        "platform": _text(data.get("platform")),
        "join_link": clean_url(data.get("join_link"))[0],
        "rsvp_link": clean_url(data.get("rsvp_link"))[0],
        "capacity": _text(data.get("capacity")),
        "agenda": _agenda(data.get("agenda")),
        "speakers": speakers,
        "courses": linked,
        "cover": f"webinars/{folder}/{cover}" if cover else WEBINAR_DEFAULT_COVER,
        "cover_style": data.get("cover_style"),
        "has_cover": bool(cover),
        "published": not _is_false(data.get("published", True)),
    }
    if start:
        view.update({
            "weekday": start.strftime("%A"), "day": str(start.day), "month": start.strftime("%B"),
            "month_short": start.strftime("%b").upper(), "weekday_short": start.strftime("%a").upper(),
            "year": str(start.year), "time_label": _clock12(start),
            "date_label": f"{start.strftime('%A')}, {start.day} {start.strftime('%B %Y')}",
        })
        view["when_label"] = f"{view['date_label']} · {view['time_label']}"
    else:
        view.update({"weekday": "", "day": "", "month": "", "month_short": "TBA", "weekday_short": "",
                     "year": "", "time_label": "", "date_label": "Date to be announced", "when_label": "Date to be announced"})
    view["join_open"] = status == "started" and bool(view["join_link"])
    return view


def _webinar_order(view):
    return (view["start"] is None, view["start"].timestamp() if view["start"] else 0, view["title"].casefold())


def load_webinars(include_hidden=False):
    """Published webinars, soonest first (dated ones, then the 'to be announced' ones)."""
    root, now, lookups, found = _webinars_dir(), now_bd(), _webinar_lookups(), []
    for folder in _subfolders(root):
        path = os.path.join(root, folder)
        data = _read_json(os.path.join(path, "webinar.json"))
        if data is None:
            continue
        view = _webinar_view(folder, data, path, now, lookups)
        if view["published"] or include_hidden:
            found.append(view)
    found.sort(key=_webinar_order)
    return found


def webinars_for_course(slug, webinars):
    return [w for w in webinars if any(c["slug"] == slug for c in w["courses"])]


# ---------------------------------------------------------------------------
# Branding: logo files, the home slider's brand slide, and how they are aligned and scaled.
#
#   content/logo/branding.json          the settings (written by the admin "branding" page)
#   content/logo/<file>                 header / footer logos (png, jpg, webp, gif or svg)
#   content/images/brand-slide.<ext>    the brand slide picture (content/webinar.jpg until one is uploaded)
# ---------------------------------------------------------------------------

BRAND_FILE = "branding.json"
LOGO_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")
SLIDE_RATIOS = {"3/2": "3 / 2", "4/3": "4 / 3", "16/9": "16 / 9", "1/1": "1 / 1", "4/5": "4 / 5"}
BRAND_ALIGN = ("left", "center", "right")
FOOTER_STYLES = ("plain", "plate", "white")
SLIDE_FITS = ("cover", "contain")
SLIDE_SHAPES = ("rounded", "square", "circle")
BRAND_DEFAULTS = {
    "header": {"logo": "", "height": 64, "height_mobile": 48, "align": "left"},
    "footer": {"logo": "", "height": 64, "height_mobile": 48, "align": "left", "style": "plain"},
    "slide": {
        "fit": "cover", "x": 50, "y": 50, "zoom": 100, "shape": "rounded", "ratio": "3/2", "side": "right", "tilt": True,
        "badge": "First Cohort · Python Programming",
        "headline": "Learn Skills That Actually Matter",
        "highlight": "Actually",
        "text": ("We're two recent graduates who believe practical skills shouldn't cost a fortune or take years to acquire. "
                 "Join a cohort that teaches real-world Python for students ready to build projects and launch faster."),
        "primary_label": "Explore the Course",
        "secondary_label": "Meet your instructors",
    },
}
BRAND_LIMITS = {"height": (16, 160), "height_mobile": (12, 120), "x": (0, 100), "y": (0, 100), "zoom": (100, 300)}


def _logo_dir():
    return os.path.join(CONTENT_DIR, "logo")


def _images_dir():
    return os.path.join(CONTENT_DIR, "images")


def _logo_files():
    try:
        names = sorted(os.listdir(_logo_dir()))
    except OSError:
        return []
    return [n for n in names if os.path.splitext(n)[1].lower() in LOGO_EXTS and not n.startswith(".")
            and os.path.isfile(os.path.join(_logo_dir(), n))]


def _clamp(value, key, default):
    low, high = BRAND_LIMITS[key]
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return default


def _choice(value, options, default):
    return value if value in options else default


# ---------------------------------------------------------------------------
# Image style: the same controls for every photo on the site (course thumbnail, instructor photo,
# webinar cover, logos, hero picture). A style is a small dict saved next to the picture's other settings;
# only what differs from the default is ever written, and a picture without a style looks exactly as before.
# ---------------------------------------------------------------------------

IMG_FITS = ("cover", "contain")
IMG_BSTYLES = ("none", "solid", "dashed", "dotted", "double")
IMG_SHADOWS = ("none", "soft", "strong", "glow")
IMG_RMODES = ("default", "custom", "circle")
IMG_LIMITS = {"x": (0, 100), "y": (0, 100), "zoom": (100, 300), "bw": (0, 20), "radius": (0, 80)}
IMG_CSS_MAX = 600
IMG_CSS_DECLS = 20
IMG_PARTS = {                    # which groups of controls make sense for each kind of picture
    "thumb": ("fit", "focus", "zoom", "border", "css"),       # course cards keep one shape, so no corners/shadow
    "photo": ("fit", "focus", "zoom", "border", "radius", "shadow", "css"),
    "cover": ("fit", "focus", "zoom", "border", "radius", "shadow", "css"),
    "logo": ("border", "radius", "shadow", "css"),
    "slide": ("border", "shadow", "css"),                      # the hero picture has its own shape control
}
IMG_FOCUS = {"photo": (50, 0)}   # instructor photos have always been cropped from the top
_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_CSS_PROP = re.compile(r"^-?[a-z][a-z0-9-]*$")
_CSS_VALUE = re.compile(r"^[A-Za-z0-9\s.,%#()+\-*/!_]+$")
_CSS_BAD_CHARS = re.compile(r"[{}<>\\@`\x00-\x08\x0b-\x1f]")
_CSS_BAD_WORDS = ("url(", "image-set(", "expression(", "javascript:", "vbscript:", "data:", "-moz-binding", "behavior",
                  "attr(", "element(", "paint(", "image(", "cross-fade(", "/*", "*/", "src(", "env(")
_CSS_BAD_PROPS = {"content", "behavior", "-moz-binding", "cursor", "list-style-image", "mask-image", "border-image-source",
                  "background-image", "filter-url"}


def img_defaults(kind):
    x, y = IMG_FOCUS.get(kind, (50, 50))
    return {"fit": "cover", "x": x, "y": y, "zoom": 100, "bstyle": "none", "bw": 2, "bcolor": "#f59e0b",
            "rmode": "default", "radius": 16, "shadow": "none", "css": ""}


def _img_int(value, key, default):
    low, high = IMG_LIMITS[key]
    try:
        return max(low, min(high, int(float(value))))
    except (TypeError, ValueError, OverflowError):
        return default


def clean_custom_css(text):
    """(clean declarations, error). Only `property: value;` lines are accepted, so the text can restyle the picture
    but cannot open a rule, load anything from elsewhere or cover the page."""
    text = (text or "").replace("\r", "").strip()
    if not text:
        return "", None
    if len(text) > IMG_CSS_MAX:
        return None, f"the custom CSS is longer than {IMG_CSS_MAX} characters."
    if _CSS_BAD_CHARS.search(text):
        return None, "the custom CSS may only contain plain 'property: value;' lines (no braces, @rules, backslashes or tags)."
    squeezed = re.sub(r"\s+", "", text.lower())
    for word in _CSS_BAD_WORDS:
        if word in squeezed:
            return None, f"the custom CSS may not use '{word.rstrip('(:')}' (nothing can be loaded from elsewhere)."
    out = []
    for raw in re.split(r"[;\n]+", text):
        decl = raw.strip()
        if not decl:
            continue
        if ":" not in decl:
            return None, f"'{decl[:40]}' is not a 'property: value' pair."
        prop, _, value = decl.partition(":")
        prop, value = prop.strip().lower(), re.sub(r"\s+", " ", value.strip())
        if not _CSS_PROP.match(prop):
            return None, f"'{prop[:40]}' is not a valid CSS property name."
        if not value or not _CSS_VALUE.match(value):
            return None, f"the value of '{prop}' contains characters that are not allowed."
        if prop in _CSS_BAD_PROPS:
            return None, f"'{prop}' is not allowed here."
        low = value.lower()
        if prop == "position" and low.split()[0] not in ("static", "relative"):
            return None, "'position' may only be static or relative (the picture must stay where it is)."
        if prop == "z-index":
            try:
                if not -1 <= int(low) <= 10:
                    raise ValueError
            except ValueError:
                return None, "'z-index' must be a whole number from -1 to 10."
        out.append(f"{prop}: {value}")
    if len(out) > IMG_CSS_DECLS:
        return None, f"use at most {IMG_CSS_DECLS} CSS lines."
    return "; ".join(out) + ";" if out else "", None


def clean_img_style(raw, kind):
    """A full, valid style for this kind of picture. Anything missing, unknown or out of range falls back to the default."""
    d = img_defaults(kind)
    raw = raw if isinstance(raw, dict) else {}
    parts = IMG_PARTS[kind]
    out = dict(d)
    if "fit" in parts:
        out["fit"] = _choice(raw.get("fit"), IMG_FITS, d["fit"])
    if "focus" in parts:
        out["x"], out["y"] = _img_int(raw.get("x", d["x"]), "x", d["x"]), _img_int(raw.get("y", d["y"]), "y", d["y"])
    if "zoom" in parts:
        out["zoom"] = _img_int(raw.get("zoom", d["zoom"]), "zoom", d["zoom"])
    if "border" in parts:
        out["bstyle"] = _choice(raw.get("bstyle"), IMG_BSTYLES, d["bstyle"])
        out["bw"] = _img_int(raw.get("bw", d["bw"]), "bw", d["bw"])
        color = _text(raw.get("bcolor"))
        out["bcolor"] = color.lower() if _HEX_COLOR.match(color) else d["bcolor"]
    if "radius" in parts:
        out["rmode"] = _choice(raw.get("rmode"), IMG_RMODES, d["rmode"])
        out["radius"] = _img_int(raw.get("radius", d["radius"]), "radius", d["radius"])
    if "shadow" in parts:
        out["shadow"] = _choice(raw.get("shadow"), IMG_SHADOWS, d["shadow"])
        if out["shadow"] == "glow" and "border" not in parts:
            out["shadow"] = "soft"
        color = _text(raw.get("bcolor"))
        if "border" not in parts and _HEX_COLOR.match(color):
            out["bcolor"] = color.lower()
    if "css" in parts:
        css, err = clean_custom_css(_text(raw.get("css")))
        out["css"] = css if err is None else ""          # a stored value that is not valid any more is simply ignored
    return out


def img_css(style, kind):
    """{'active', 'frame', 'img'}: inline CSS for the frame around the picture (border, corners, shadow, custom lines)
    and for the <img> itself (fit, focus, zoom). `active` is False when nothing differs from the default."""
    d = img_defaults(kind)
    frame, img = [], []
    if style["bstyle"] != "none" and style["bw"] > 0:
        frame.append(f"border: {style['bw']}px {style['bstyle']} {style['bcolor']}")
    if style["rmode"] == "custom":
        frame.append(f"border-radius: {style['radius']}px")
    elif style["rmode"] == "circle":
        frame.append("border-radius: 50%")
    shadow = {"soft": "0 6px 18px rgba(0, 0, 0, 0.28)", "strong": "0 16px 44px rgba(0, 0, 0, 0.5)",
              "glow": f"0 0 26px {style['bcolor']}"}.get(style["shadow"])
    if shadow:
        frame.append(f"box-shadow: {shadow}")
    if style["css"]:
        frame.append(style["css"].rstrip(";"))
    moved = (style["fit"], style["x"], style["y"], style["zoom"]) != (d["fit"], d["x"], d["y"], d["zoom"])
    if moved:
        img.append(f"object-fit: {style['fit']}")
        img.append(f"object-position: {style['x']}% {style['y']}%")
        if style["zoom"] != 100:
            img.append(f"transform: scale({style['zoom'] / 100:.2f})")
            img.append(f"transform-origin: {style['x']}% {style['y']}%")
    return {"active": bool(frame or img), "frame": "; ".join(frame) + (";" if frame else ""),
            "img": "; ".join(img) + (";" if img else ""), "moved": moved}


def read_img_style(form, prefix, kind, errors, label):
    """Style from submitted form fields named <prefix>_fit, <prefix>_x ... Problems are appended to `errors`."""
    raw = {k: form.get(f"{prefix}_{k}") for k in ("fit", "x", "y", "zoom", "bstyle", "bw", "bcolor", "rmode", "radius", "shadow", "css")}
    raw = {k: v for k, v in raw.items() if v is not None}
    if "css" in raw:
        _, why = clean_custom_css(raw["css"])
        if why:
            errors.append(f"{label}: {why}")
            style = clean_img_style(raw, kind)
            if "css" in IMG_PARTS[kind]:
                style["css"] = raw["css"][:IMG_CSS_MAX]        # shown again in the form so it can be corrected; never stored
            return style
    return clean_img_style(raw, kind)


def store_img_style(data, key, style, kind):
    """Write the style into a JSON dict under `key` (only what differs from the default) or remove the key."""
    d = img_defaults(kind)
    keep = {k: v for k, v in style.items() if v != d[k]}
    if keep:
        data[key] = keep
    else:
        data.pop(key, None)


@app.template_global("imgstyle")
def _imgstyle(raw, kind):
    return img_css(clean_img_style(raw, kind), kind)


app.add_template_global(img_defaults, "img_defaults")
app.add_template_global(lambda kind: IMG_PARTS[kind], "img_parts")
app.add_template_global(IMG_LIMITS, "img_limits")
app.add_template_global(IMG_CSS_MAX, "img_css_max")


def clean_branding(raw):
    """Settings as stored/used: unknown keys dropped, numbers clamped, choices checked."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for part in ("header", "footer"):
        src, dflt = raw.get(part), BRAND_DEFAULTS[part]
        src = src if isinstance(src, dict) else {}
        out[part] = {
            "logo": os.path.basename(_text(src.get("logo"))),
            "height": _clamp(src.get("height", dflt["height"]), "height", dflt["height"]),
            "height_mobile": _clamp(src.get("height_mobile", dflt["height_mobile"]), "height_mobile", dflt["height_mobile"]),
            "align": _choice(src.get("align"), BRAND_ALIGN, dflt["align"]),
            "img": clean_img_style(src.get("img"), "logo"),
        }
    footer_src = raw.get("footer") if isinstance(raw.get("footer"), dict) else {}
    out["footer"]["style"] = _choice(footer_src.get("style"), FOOTER_STYLES, "plain")
    src = raw.get("slide") if isinstance(raw.get("slide"), dict) else {}
    dflt = BRAND_DEFAULTS["slide"]
    slide = {
        "fit": _choice(src.get("fit"), SLIDE_FITS, dflt["fit"]),
        "x": _clamp(src.get("x", dflt["x"]), "x", dflt["x"]),
        "y": _clamp(src.get("y", dflt["y"]), "y", dflt["y"]),
        "zoom": _clamp(src.get("zoom", dflt["zoom"]), "zoom", dflt["zoom"]),
        "shape": _choice(src.get("shape"), SLIDE_SHAPES, dflt["shape"]),
        "ratio": _choice(src.get("ratio"), SLIDE_RATIOS, dflt["ratio"]),
        "side": _choice(src.get("side"), ("left", "right"), dflt["side"]),
        "tilt": dflt["tilt"] if "tilt" not in src else not _is_false(src.get("tilt")),
    }
    for key in ("badge", "headline", "highlight", "text", "primary_label", "secondary_label"):
        slide[key] = _text(src.get(key)) if key in src else dflt[key]
    slide["headline"] = slide["headline"] or dflt["headline"]          # the slide always needs a headline
    slide["img"] = clean_img_style(src.get("img"), "slide")
    out["slide"] = slide
    return out


def read_branding():
    """The saved settings (cleaned); defaults when the file does not exist yet."""
    path = os.path.join(_logo_dir(), BRAND_FILE)
    data = _read_json(path) if os.path.isfile(path) else None
    return clean_branding(data)


def _headline_parts(headline, highlight):
    if highlight and headline.count(highlight) >= 1:
        before, _, after = headline.partition(highlight)
        return before, highlight, after
    return headline, "", ""


def load_branding():
    """Settings plus everything the templates need: resolved logo files and image URLs."""
    brand = read_branding()
    files = _logo_files()
    header_file = brand["header"]["logo"] if brand["header"]["logo"] in files else (random.choice(files) if files else None)
    footer_file = brand["footer"]["logo"] if brand["footer"]["logo"] in files else header_file
    brand["header"].update(file=header_file, url=asset_url("logo/" + header_file) if header_file else None,
                           css=img_css(brand["header"]["img"], "logo")["frame"])
    brand["footer"].update(file=footer_file, url=asset_url("logo/" + footer_file) if footer_file else None,
                           css=img_css(brand["footer"]["img"], "logo")["frame"])
    slide = brand["slide"]
    own = _find_image(_images_dir(), "brand-slide")
    image = f"images/{own}" if own else (WEBINAR_DEFAULT_COVER if os.path.isfile(os.path.join(CONTENT_DIR, WEBINAR_DEFAULT_COVER)) else None)
    slide.update(image=image, image_url=asset_url(image) if image else None, custom_image=bool(own),
                 css_ratio=SLIDE_RATIOS[slide["ratio"]], frame_css=img_css(slide["img"], "slide")["frame"], headline_parts=_headline_parts(slide["headline"], slide["highlight"]))
    return brand


@app.context_processor
def site_context():
    return {"brand": load_branding(), "asset": asset_url, "site_name": SITE_NAME}


@app.route("/")
def home():
    webinars = [w for w in load_webinars() if w["start"]][:HERO_WEBINARS]
    people = load_instructors()
    return render_template(
        "home.html",
        founders=[p for p in people if p["founder"]],
        instructor_count=len(people),
        featured_courses=newest_courses(load_courses(), 2),
        pipeline_courses=pipeline_courses,
        hero_webinars=webinars,
        show_banner=True
    )

@app.route("/courses")
def courses():
    return render_template(
        "courses.html",
        courses=assign_tints(load_courses()),
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
    linked = webinars_for_course(course["slug"], load_webinars())
    return render_template("course_details.html", course=course, linked_webinar=(linked[0] if linked else None))

@app.route("/instructor/<slug>")
def instructor_profile(slug):
    people = load_instructors()
    person = next((p for p in people if p["slug"] == slug), None)
    if person is None:
        abort(404)
    taught = [c for c in load_courses() if any(i.get("slug") == slug for i in c["instructors"])]
    return render_template("instructor.html", person=person, courses=assign_tints(taught))

@app.route("/webinar")
def webinar():
    items = load_webinars()
    return render_template("webinar.html", featured=(items[0] if items else None), later=items[1:])

@app.route("/team")
def team():
    """Our team: founding members (ticked "founder" in the admin) first, every other instructor below."""
    people = load_instructors()
    founders = [p for p in people if p["founder"]]
    others = assign_tints([p for p in people if not p["founder"]])
    course_count = len(load_courses())
    return render_template("team.html", founders=founders, instructors=others,
                           people_count=len(people), course_count=course_count)


@app.route("/about")
def about():
    """The old About page address: kept so existing links keep working."""
    return redirect(url_for("team"), code=301)

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
TRASH_INSTRUCTOR_PREFIX = "instructor--"      # course slugs never contain "--", so the two kinds cannot clash
TRASH_RE = re.compile(r"^(?P<slug>[^/\\\x00]+?)__(?P<ts>\d{8}-\d{6})(?:-\d+)?$")

STRING_KEYS = ["title", "tagline", "subtitle", "description", "duration", "capacity",
               "certificate", "price", "start_date", "enroll_link", "webinar_link"]
LIST_KEYS = ["features", "what_you_will_learn", "instructors"]
META_KEYS = STRING_KEYS + LIST_KEYS + ["discount", "order", "added", "published", "thumb_style"]

ADMIN_IDLE_SECONDS = 8 * 3600
UPLOAD_LIMIT = 5 * 1024 * 1024


def _env_mb(name, default):
    try:
        return max(1, int(float(os.environ.get(name, default)))) * 1024 * 1024
    except ValueError:
        return default * 1024 * 1024


BIG_UPLOAD_LIMIT = _env_mb("MAX_UPLOAD_MB", 300)     # one request to the file browser or one import bundle (env MAX_UPLOAD_MB)
UNZIP_MAX_TOTAL = _env_mb("MAX_UNZIP_MB", 1000)      # everything a zip may unpack to, or a download-zip may hold (env MAX_UNZIP_MB)
BIG_ENDPOINTS = {"admin.files_upload", "admin.transfer_import_upload"}
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
    big = request.endpoint in BIG_ENDPOINTS
    if request.method == "POST":
        # a large upload never has its body parsed just to look for the token: that comes in a header
        sent = request.headers.get("X-CSRF-Token", "") if big else (request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", ""))
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
            abort(400, "CSRF check failed. Reload the page and try again.")
    if request.endpoint == "admin.login":
        return None
    if not _admin_session_valid():
        target = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("admin.login", next=target))
    if big and request.method == "POST":
        try:
            request.max_content_length = BIG_UPLOAD_LIMIT      # Flask 3.1+; older versions keep the normal 12 MB cap
        except AttributeError:
            pass
    return None


@admin_bp.after_request
def _admin_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers.setdefault("X-Frame-Options", "DENY")       # the file preview sets SAMEORIGIN itself
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
    """Absolute path of an existing course folder, or 404. Refuses anything that could escape content/course."""
    root = _courses_dir()
    if (not slug or slug in (".", "..") or slug.startswith((".", "_"))
            or slug != os.path.basename(slug) or "\\" in slug or "\x00" in slug):
        abort(404)
    path = os.path.join(root, slug)
    if not os.path.isdir(path) or os.path.dirname(os.path.realpath(path)) != os.path.realpath(root):
        abort(404)
    return path


def _instructor_path(slug):
    """Absolute path of an existing instructor folder, or 404 (same guards as _course_path)."""
    root = _instructors_dir()
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


def _grid_to_csv(grid):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(grid)
    return buf.getvalue()


def _dump_meta(meta):
    ordered = {k: meta[k] for k in META_KEYS if k in meta}
    ordered.update({k: v for k, v in meta.items() if k not in ordered})   # keep hand-added keys
    return json.dumps(ordered, indent=4, ensure_ascii=False) + "\n"


def _remove_thumbnails(path, keep=None, stem="thumbnail"):
    """Delete every <stem>.* image except the file named `keep` (compared by identity, so it is safe on Windows)."""
    keep_path = os.path.join(path, keep) if keep else None
    for name in os.listdir(path):
        base, ext = os.path.splitext(name)
        if base.lower() != stem or ext.lower() not in IMAGE_EXTS:
            continue
        full = os.path.join(path, name)
        if keep_path and os.path.exists(keep_path) and os.path.samefile(full, keep_path):
            continue
        os.remove(full)


# ---------- scanning (what the dashboard shows) ----------

def scan_courses():
    """Every course folder, including hidden and broken ones, with diagnostics."""
    root = _courses_dir()
    instructors = load_instructors(include_hidden=True)
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
            _, _, discount_problem = _discount_info(meta)
            if discount_problem:
                add("warn", discount_problem)
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
                add("warn", f"instructor '{name}' has no matching profile in content/instructors")

        csv_path = os.path.join(path, "curriculum.csv")
        csv_text = _read_text(csv_path)
        rows = cols = 0
        if csv_text is None:
            add("warn" if not os.path.isfile(csv_path) else "error",
                "curriculum.csv not found" if not os.path.isfile(csv_path) else "curriculum.csv is not readable as UTF-8")
        else:
            try:
                sheet = parse_sheet(csv.reader(io.StringIO(csv_text, newline="")))
                if sheet:
                    rows, cols = sheet["nrows"], sheet["ncols"]
                else:
                    add("warn", "curriculum.csv is empty (the Curriculum section is hidden)")
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
        final_price, discount_label, _ = _discount_info(meta) if not err else ("", "", "")

        entries.append({
            "slug": folder,
            "status": status,
            "added": added_label,
            "title": _text(meta.get("title")) or folder,
            "tagline": _text(meta.get("tagline")),
            "price": _text(meta.get("price")),
            "price_final": final_price,
            "discount": discount_label,
            "order": _int(meta.get("order")) if not err else None,
            "rows": rows,
            "cols": cols,
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
        slug = m.group("slug")
        kind = ("instructor" if slug.startswith(TRASH_INSTRUCTOR_PREFIX)
                else "webinar" if slug.startswith(TRASH_WEBINAR_PREFIX)
                else "file" if slug.startswith(TRASH_FILE_PREFIX) else "course")
        if kind == "file":
            origin, _ = _read_json_checked(os.path.join(path, "origin.json"))
            title = "content/" + _text((origin or {}).get("path"))
            slug = _text((origin or {}).get("path"))
        elif kind == "instructor":
            slug = slug[len(TRASH_INSTRUCTOR_PREFIX):]
            meta, _ = _read_json_checked(os.path.join(path, "profile.json"))
            title = _text((meta or {}).get("name"))
        elif kind == "webinar":
            slug = slug[len(TRASH_WEBINAR_PREFIX):]
            meta, _ = _read_json_checked(os.path.join(path, "webinar.json"))
            title = _text((meta or {}).get("title"))
        else:
            meta, _ = _read_json_checked(os.path.join(path, "meta.json"))
            title = _text((meta or {}).get("title"))
        size, count, _ = _tree_stats(path)
        items.append({
            "name": name,
            "kind": kind,
            "slug": slug,
            "title": title or slug,
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
    instructors = load_instructors(include_hidden=True)
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
        "rows": sum(c["rows"] for c in courses_),
        "no_thumb": sum(1 for c in courses_ if c["status"] != "broken" and not c["thumb"]),
        "instructors": len(instructors),
        "bytes": sum(c["size"] for c in courses_),
        "errors": sum(c["n_error"] for c in courses_),
        "warnings": sum(c["n_warn"] for c in courses_),
    }
    return render_template("admin_dashboard.html", courses=courses_, stats=stats, instructors=instructors)


# ---------- curriculum import (csv / xlsx), used by the editor's file drop-zone ----------
#
# The editor sends the chosen file here; the reply is clean CSV text that the page puts into the
# curriculum box so you can review it before saving. Nothing is written to disk by this route.
# .xlsx is read with the standard library only (an .xlsx is a zip of XML files), so there is nothing
# extra to install. Guards: size limits, no DOCTYPE/entity tricks in the XML, only the first sheet,
# only the first two columns.

IMPORT_LIMIT = 2 * 1024 * 1024
XLSX_PART_LIMIT = 8 * 1024 * 1024
IMPORT_MAX_ROWS = 2000
CURRICULUM_TEMPLATE = (
    "Week,Topic,Hours\n"
    "Week 1,Variables & data types,3\n"
    "Week 2,\"Lists, dictionaries, sets\",4\n"
)


def _decode_text(data):
    for encoding in ("utf-8-sig", "cp1252"):      # Excel on Windows saves "CSV" as cp1252
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _csv_rows(data):
    text = _decode_text(data)
    first = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = max((",", ";", "\t"), key=first.count) if any(d in first for d in ",;\t") else ","
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    return rows


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _xml_part(archive, name):
    """Parsed XML of one workbook part, refusing oversized parts and anything with a DOCTYPE/entities."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > XLSX_PART_LIMIT:
        raise ValueError("That workbook is too large to import.")
    raw = archive.read(name)
    lowered = raw.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("That workbook contains unsupported XML and was not imported.")
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        raise ValueError("That workbook could not be read (damaged XML).") from None


def _node_text(node):
    """Text of a shared-string / inline-string node (plain or rich text, phonetic hints ignored)."""
    out = []
    for child in node:
        name = _local(child.tag)
        if name == "t":
            out.append(child.text or "")
        elif name == "r":
            out.extend((t.text or "") for t in child if _local(t.tag) == "t")
    return "".join(out)


def _column_index(ref):
    letters = re.match(r"[A-Za-z]+", ref or "")
    if not letters:
        return None
    n = 0
    for ch in letters.group(0).upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


# ---- how Excel shows numbers and dates (so a cell reads in the file the way it reads in Excel) ----

_BUILTIN_FORMATS = {
    1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%",
    14: "yyyy-mm-dd", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy", 18: "h:mm AM/PM", 19: "h:mm:ss AM/PM",
    20: "h:mm", 21: "h:mm:ss", 22: "yyyy-mm-dd h:mm", 37: "#,##0", 38: "#,##0", 39: "#,##0.00",
    40: "#,##0.00", 45: "mm:ss", 46: "h:mm:ss", 47: "mm:ss.0", 49: "@",
}
for _i in list(range(27, 37)) + list(range(50, 59)):
    _BUILTIN_FORMATS.setdefault(_i, "yyyy-mm-dd")      # locale-specific date formats

_FMT_TOKEN = re.compile(
    r'"[^"]*"|\\.|_.|\*.|\[[^\]]*\]|[AaPp][Mm]/[AaPp][Mm]|[Aa]/[Pp]|[Ee][+-]|[yY]+|[mM]+|[dD]+|[hH]+|[sS]+|.', re.S)
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _fmt_tokens(code):
    out = []
    for m in _FMT_TOKEN.finditer(code or ""):
        t = m.group(0)
        if t[0] == '"':
            out.append(("lit", t[1:-1]))
        elif t[0] == "\\":
            out.append(("lit", t[1:]))
        elif t[0] == "_":
            out.append(("lit", " "))
        elif t[0] == "*":
            continue
        elif t[0] == "[":
            inner = t[1:-1]
            if re.fullmatch(r"[hHmMsS]+", inner):
                out.append((inner[0].lower(), inner.lower()))      # elapsed time: treated like the normal token
            elif inner.startswith("$"):
                out.append(("lit", inner[1:].split("-")[0]))       # [$৳-445] -> ৳
        elif t.lower() in ("am/pm", "a/p"):
            out.append(("ampm", t))
        elif t[0] in "eE" and len(t) == 2:
            out.append(("sci", t))
        elif t[0] in "yY":
            out.append(("y", t.lower()))
        elif t[0] in "mM":
            out.append(("m", t.lower()))
        elif t[0] in "dD":
            out.append(("d", t.lower()))
        elif t[0] in "hH":
            out.append(("h", t.lower()))
        elif t[0] in "sS":
            out.append(("s", t.lower()))
        else:
            out.append(("other", t))
    return out


def _format_date(serial, tokens, date1904):
    base = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    dt = base + timedelta(seconds=round(float(serial) * 86400))
    clock = ("y", "m", "d", "h", "s", "ampm")
    twelve = any(k == "ampm" for k, _ in tokens)
    out = []
    for i, (kind, t) in enumerate(tokens):
        if kind == "y":
            out.append(f"{dt.year:04d}" if len(t) > 2 else f"{dt.year % 100:02d}")
        elif kind == "m":
            prev = next((tokens[j][0] for j in range(i - 1, -1, -1) if tokens[j][0] in clock), None)
            nxt = next((tokens[j][0] for j in range(i + 1, len(tokens)) if tokens[j][0] in clock), None)
            if prev == "h" or nxt == "s":
                out.append(f"{dt.minute:02d}" if len(t) > 1 else str(dt.minute))
            elif len(t) == 1:
                out.append(str(dt.month))
            elif len(t) == 2:
                out.append(f"{dt.month:02d}")
            elif len(t) == 3:
                out.append(_MONTHS[dt.month - 1][:3])
            elif len(t) == 4:
                out.append(_MONTHS[dt.month - 1])
            else:
                out.append(_MONTHS[dt.month - 1][0])
        elif kind == "d":
            if len(t) == 1:
                out.append(str(dt.day))
            elif len(t) == 2:
                out.append(f"{dt.day:02d}")
            elif len(t) == 3:
                out.append(_DAYS[dt.weekday()][:3])
            else:
                out.append(_DAYS[dt.weekday()])
        elif kind == "h":
            hour = (dt.hour % 12 or 12) if twelve else dt.hour
            out.append(f"{hour:02d}" if len(t) > 1 else str(hour))
        elif kind == "s":
            out.append(f"{dt.second:02d}" if len(t) > 1 else str(dt.second))
        elif kind == "ampm":
            out.append(("AM" if dt.hour < 12 else "PM") if len(t) > 3 else ("A" if dt.hour < 12 else "P"))
        else:
            out.append(t)
    return "".join(out)


def _general_number(raw):
    try:
        x = float(raw)
    except ValueError:
        return raw
    if x.is_integer() and abs(x) < 1e15:
        return str(int(x))
    return format(x, ".15g")


def _format_number(raw, tokens):
    x = float(raw)
    sections = [[]]
    for tok in tokens:
        if tok == ("other", ";"):
            sections.append([])
        else:
            sections[-1].append(tok)
    chosen, sign = sections[0], ""
    if x < 0:
        if len(sections) > 1:
            chosen = sections[1]
        else:
            sign = "-"
    elif x == 0 and len(sections) > 2:
        chosen = sections[2]
    run = [i for i, (k, t) in enumerate(chosen) if k == "other" and t in "#0?,."]
    if not run:
        return _general_number(raw)
    first, last = run[0], run[-1]
    pattern = "".join(chosen[i][1] for i in range(first, last + 1))
    if any(chosen[i][0] != "other" or chosen[i][1] not in "#0?,." for i in range(first, last + 1)):
        return _general_number(raw)               # something odd inside the number pattern
    percent = any(k == "other" and t == "%" for k, t in chosen)
    int_part, _, dec_part = pattern.partition(".")
    min_dec = dec_part.count("0")
    max_dec = len(dec_part.replace(",", ""))
    value = abs(Decimal(repr(x)))
    if percent:
        value *= 100
    value = value.quantize(Decimal(1).scaleb(-max_dec), rounding=ROUND_HALF_UP)
    text = f"{value:,.{max_dec}f}" if "," in int_part else f"{value:.{max_dec}f}"
    if "." in text:
        whole, frac = text.split(".")
        frac = frac.rstrip("0").ljust(min_dec, "0")
        text = whole + ("." + frac if frac else "")
    zeros = int_part.replace(",", "").count("0")
    if zeros and "," not in text:
        whole, dot, frac = text.partition(".")
        text = whole.zfill(zeros) + dot + frac
    before = "".join(t for _, t in chosen[:first])
    after = "".join(t for _, t in chosen[last + 1:])
    return sign + before + text + after


def _format_cell_number(raw, code, date1904):
    """Number cell as Excel would display it, falling back to the plain number."""
    try:
        float(raw)
    except ValueError:
        return raw
    code = code or "General"
    if code.strip().lower() in ("general", "@", ""):
        return _general_number(raw)
    tokens = _fmt_tokens(code)
    try:
        if any(k == "sci" for k, _ in tokens):
            return _general_number(raw)
        if any(k in ("y", "d", "h", "s", "ampm", "m") for k, _ in tokens):
            return _format_date(raw, tokens, date1904)
        return _format_number(raw, tokens)
    except (ValueError, OverflowError, InvalidOperation):
        return _general_number(raw)


def _xlsx_rows(data):
    """(rows, note) of the first visible-order sheet. Hidden rows/columns are left out, like in Excel."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That file is not a valid .xlsx workbook.") from None
    names = archive.namelist()

    shared = []
    sst = _xml_part(archive, "xl/sharedStrings.xml")
    if sst is not None:
        shared = [_node_text(si) for si in sst if _local(si.tag) == "si"]

    # number formats: cell style index -> format code
    style_codes = []
    styles = _xml_part(archive, "xl/styles.xml")
    if styles is not None:
        custom = {}
        for node in styles.iter():
            if _local(node.tag) == "numFmt":
                custom[node.get("numFmtId")] = node.get("formatCode") or ""
        for xfs in styles.iter():
            if _local(xfs.tag) == "cellXfs":
                for xf in xfs:
                    fid = xf.get("numFmtId") or "0"
                    style_codes.append(custom.get(fid) or _BUILTIN_FORMATS.get(_int(fid, 0), "General"))
    date1904 = False

    # first sheet in tab order, falling back to sheet1.xml
    sheet_path = None
    sheet_names = []
    workbook = _xml_part(archive, "xl/workbook.xml")
    rels = _xml_part(archive, "xl/_rels/workbook.xml.rels")
    if workbook is not None and rels is not None:
        targets = {r.get("Id"): r.get("Target") for r in rels}
        for node in workbook.iter():
            if _local(node.tag) == "workbookPr" and (node.get("date1904") or "").lower() in ("1", "true"):
                date1904 = True
            if _local(node.tag) == "sheet":
                sheet_names.append(node.get("name") or "")
                if sheet_path is None:
                    rid = next((v for k, v in node.attrib.items() if _local(k) == "id"), None)
                    target = targets.get(rid)
                    if target:
                        target = target.lstrip("/")
                        sheet_path = target if target.startswith("xl/") else "xl/" + target
    if sheet_path not in names:
        sheet_path = "xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml" in names else next(
            (n for n in sorted(names) if n.startswith("xl/worksheets/") and n.endswith(".xml")), None)
    if sheet_path is None:
        raise ValueError("No worksheet found. Is this a real .xlsx file?")
    sheet = _xml_part(archive, sheet_path)

    hidden_cols = set()
    for node in sheet.iter():
        if _local(node.tag) == "col" and (node.get("hidden") or "").lower() in ("1", "true"):
            lo, hi = _int(node.get("min"), 0), min(_int(node.get("max"), 0), MAX_SHEET_COLS + 1)
            hidden_cols.update(range(lo - 1, hi))

    cells_by_row = {}
    auto_row = 0
    for row in sheet.iter():
        if _local(row.tag) != "row":
            continue
        auto_row += 1
        rownum = _int(row.get("r"), auto_row)
        auto_row = rownum
        if (row.get("hidden") or "").lower() in ("1", "true"):
            continue
        next_col = 0
        values = {}
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            col = _column_index(cell.get("r"))
            col = next_col if col is None else col
            next_col = col + 1
            if col in hidden_cols:
                continue
            kind = cell.get("t")
            value_node = next((c for c in cell if _local(c.tag) == "v"), None)
            raw = (value_node.text or "") if value_node is not None else ""
            if kind == "s":
                value = shared[int(raw)] if raw.isdigit() and int(raw) < len(shared) else ""
            elif kind == "inlineStr":
                is_node = next((c for c in cell if _local(c.tag) == "is"), None)
                value = _node_text(is_node) if is_node is not None else ""
            elif kind == "b":
                value = "TRUE" if raw == "1" else "FALSE"
            elif kind == "e":
                value = ""
            elif kind in (None, "n"):
                style = _int(cell.get("s"), 0)
                code = style_codes[style] if style < len(style_codes) else "General"
                value = _format_cell_number(raw, code, date1904) if raw != "" else ""
            else:                                  # formula strings, ISO dates
                value = raw
            if value.strip():
                if col >= MAX_SHEET_COLS:
                    raise ValueError(f"More than {MAX_SHEET_COLS} columns. Keep the curriculum sheet narrower.")
                values[col] = value
        if values:
            if rownum > IMPORT_MAX_ROWS:
                raise ValueError(f"More than {IMPORT_MAX_ROWS} rows; split the curriculum into a smaller sheet.")
            cells_by_row[rownum] = values

    rows = []
    if cells_by_row:
        width = max(max(v) for v in cells_by_row.values()) + 1
        for n in range(min(cells_by_row), max(cells_by_row) + 1):
            values = cells_by_row.get(n, {})
            rows.append([values.get(c, "") for c in range(width)])
    note = ""
    if sheet_names:
        note = f"sheet '{sheet_names[0]}'" + (f" (the workbook has {len(sheet_names)} sheets; only the first is imported)" if len(sheet_names) > 1 else "")
    return rows, note


def _import_rows(data):
    """(rows, kind, note) from uploaded bytes. The file's real content decides the type, not its name."""
    if data[:4] == b"PK\x03\x04":
        rows, note = _xlsx_rows(data)
        return rows, "xlsx", note
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise ValueError("Old .xls files are not supported. In Excel use Save As and pick .xlsx or CSV.")
    if b"\x00" in data[:4096]:
        raise ValueError("That looks like a binary file. Upload a .csv or .xlsx file.")
    return _csv_rows(data), "csv", ""


def _grid_error(grid):
    """Message when a sheet is too big to store, else None."""
    if len(grid) > IMPORT_MAX_ROWS:
        return f"The curriculum has more than {IMPORT_MAX_ROWS} rows."
    if grid and len(grid[0]) > MAX_SHEET_COLS:
        return f"The curriculum has more than {MAX_SHEET_COLS} columns."
    return None


@admin_bp.post("/curriculum/import")
def curriculum_import():
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(ok=False, error="Choose a .csv or .xlsx file first."), 400
    data = upload.read(IMPORT_LIMIT + 1)
    if len(data) > IMPORT_LIMIT:
        return jsonify(ok=False, error="That file is larger than 2 MB."), 400
    try:
        rows, kind, note = _import_rows(data)
        grid = normalize_grid(rows)
    except (ValueError, csv.Error) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if not grid:
        return jsonify(ok=False, error="The file has no data in it."), 400
    problem = _grid_error(grid)
    if problem:
        return jsonify(ok=False, error=problem), 400
    log.info("admin: imported curriculum from a %s file (%d rows x %d columns)", kind, len(grid), len(grid[0]))
    return jsonify(ok=True, csv=_grid_to_csv(grid), kind=kind, rows=len(grid) - 1, cols=len(grid[0]), note=note)


@admin_bp.route("/curriculum/template.csv")
def curriculum_template():
    return Response("﻿" + CURRICULUM_TEMPLATE, mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="curriculum-template.csv"'})


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
    logos = _logo_files()
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
        "Static folder (css, js)": app.static_folder,
        "Content folder": CONTENT_DIR,
        "Courses folder": _courses_dir(),
        "Instructors folder": _instructors_dir(),
        "Webinars folder": _webinars_dir(),
        "Trash folder": _trash_dir(),
        "Admin URL prefix": ADMIN_PREFIX,
        "Password source": {"env": "ADMIN_PASSWORD env var", "file": "admin_password.txt"}.get(_pw_source, "admin_password.txt (created this run)"),
        "Upload limit": "5 MB per image / 8 MB per request",
        "Session idle timeout": f"{ADMIN_IDLE_SECONDS // 3600} h",
        "Login lockout": f"{_LOGIN_MAX_FAILS} failures = {_LOGIN_LOCK_SECONDS // 60} min",
        "Server clock": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Site time (Bangladesh)": now_bd().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return render_template("admin_system.html", info=info, routes=routes, logos=logos, clock=TIME.status(),
                           brand_logo=load_branding()["header"]["file"],
                           logs=_logbook.recent(), instructors=load_instructors(include_hidden=True),
                           pipeline_count=len(pipeline_courses), faq_count=len(faq_questions))


# ---------- routes: create / edit course ----------

def _form_from_meta(meta, csv_text, slug, added=""):
    """Initial values for the editor form (all strings, ready to put in inputs)."""
    return {
        "slug": slug or "",
        "added": added,
        "discount": _text(meta.get("discount")),
        **{k: _text(meta.get(k)) for k in STRING_KEYS},
        "features": "\n".join(_text_list(meta.get("features"))),
        "what_you_will_learn": "\n".join(_text_list(meta.get("what_you_will_learn"))),
        "instructors": _text_list(meta.get("instructors")),
        "order": str(meta.get("order", "")) if isinstance(meta.get("order", ""), int) else _text(meta.get("order")),
        "published": not _is_false(meta.get("published", True)) if meta else False,
        "csv": csv_text,
        "thumb_style": clean_img_style(meta.get("thumb_style"), "thumb"),
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
        "discount": f.get("discount", "").strip(),
        **{k: f.get(k, "").strip() for k in STRING_KEYS},
        "features": f.get("features", ""),
        "what_you_will_learn": f.get("what_you_will_learn", ""),
        "instructors": [n.strip() for n in f.getlist("instructors") if n.strip()],
        "order": f.get("order", "").strip(),
        "published": f.get("published") == "on",
        "csv": f.get("curriculum_csv", ""),
    }
    form["thumb_style"] = read_img_style(f, "thumb", "thumb", errors, "Thumbnail")

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
    d100 = None
    try:
        d100 = parse_discount(form["discount"])
    except ValueError as exc:
        errors.append(str(exc))
    if d100 and price_with_discount(form["price"], d100) is None:
        errors.append("A discount needs a price that contains a number (for example Tk 500).")

    grid = []
    try:
        grid = normalize_grid(csv.reader(io.StringIO(form["csv"], newline="")))
        problem = _grid_error(grid)
        if problem:
            errors.append(problem)
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
    if d100:
        meta["discount"] = d100 // 100 if d100 % 100 == 0 else float(Decimal(d100) / 100)
    else:
        meta.pop("discount", None)
    meta["published"] = form["published"]
    store_img_style(meta, "thumb_style", form["thumb_style"], "thumb")
    return form, {
        "slug": form["slug"],
        "meta": meta,
        "grid": grid,
        "upload": upload,
        "remove_thumb": f.get("remove_thumbnail") == "on",
    }, []


def _editor(slug):
    creating = slug is None
    path = None if creating else _course_path(slug)
    instructors = load_instructors(include_hidden=True)
    load_error = None
    existing_meta = {}
    csv_text = ""
    thumb = None
    if not creating:
        existing_meta, load_error = _read_json_checked(os.path.join(path, "meta.json"))
        existing_meta = existing_meta or {}
        csv_text = _read_text(os.path.join(path, "curriculum.csv")) or ""
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
            _atomic_write(os.path.join(target_path, "curriculum.csv"), ("\ufeff" + _grid_to_csv(result["grid"])) if result["grid"] else "", encoding="utf-8")
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
    if slug.startswith(TRASH_FILE_PREFIX):
        origin, _ = _read_json_checked(os.path.join(src, "origin.json"))
        try:
            parts = _fb_parts((origin or {}).get("path"), allow_empty=False)
            target = _fb_abs(parts)
        except FBError as exc:
            flash(f"Cannot restore: {exc.message}", "error")
            return redirect(url_for("admin.trash"))
        _fb_restore(os.path.join(src, "payload"), target)
        shutil.rmtree(src, ignore_errors=True)
        log.info("admin: restored '/%s' from the trash", "/".join(parts))
        flash(f"Restored content/{'/'.join(parts)}." , "success")
        return redirect(url_for("admin.files", path="/".join(parts[:-1])))
    kind = ("instructor" if slug.startswith(TRASH_INSTRUCTOR_PREFIX)
            else "webinar" if slug.startswith(TRASH_WEBINAR_PREFIX) else "course")
    root = {"instructor": _instructors_dir, "webinar": _webinars_dir, "course": _courses_dir}[kind]()
    if kind == "instructor":
        slug = slug[len(TRASH_INSTRUCTOR_PREFIX):]
    elif kind == "webinar":
        slug = slug[len(TRASH_WEBINAR_PREFIX):]
    target = slug if not os.path.exists(os.path.join(root, slug)) else _unique_slug(slug + "-restored", root)
    os.makedirs(root, exist_ok=True)
    shutil.move(src, os.path.join(root, target))
    if kind == "webinar":
        # a restored webinar comes back as a hidden draft: its date has most likely passed, so set it right before publishing
        path = os.path.join(root, target, "webinar.json")
        data, _ = _read_json_checked(path)
        if data is not None:
            data["published"] = False
            _atomic_write(path, _dump_webinar(data))
    log.info("admin: restored '%s' as '%s'", name, target)
    flash(f"Restored as '{target}'." + ("" if target == slug else " The original name was taken.")
          + (" It is a hidden draft: check the date, then publish it." if kind == "webinar" else ""), "success")
    endpoint = {"instructor": "admin.instructor_edit", "webinar": "admin.webinar_edit", "course": "admin.course_edit"}[kind]
    return redirect(url_for(endpoint, slug=target))


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


# ---------------------------------------------------------------------------
# Admin: instructors
#
# There are no instructor accounts. The manager (or an instructor, with the manager's
# permission) signs in to this one console and edits profiles here, so every change goes
# through the same login, CSRF protection and activity log as the courses.
# ---------------------------------------------------------------------------

CV_LIMIT = 5 * 1024 * 1024
ENTRY_LABELS = {
    "education": "Education", "experience": "Experience",
    "certifications": "Certification", "achievements": "Project / achievement",
}
FIELD_LIMITS = {"details": 1000, "link": 300, "year": 40, "period": 60, "url": 300, "platform": 40}


def _dump_profile(data):
    ordered = {k: data[k] for k in PROFILE_KEYS if k in data}
    ordered.update({k: v for k, v in data.items() if k not in ordered})      # keep hand-added keys
    return json.dumps(ordered, indent=4, ensure_ascii=False) + "\n"


def _courses_using(name):
    """Titles of the courses (hidden ones too) that list this instructor by name."""
    titles = []
    for folder in _subfolders(_courses_dir()):
        meta, err = _read_json_checked(os.path.join(_courses_dir(), folder, "meta.json"))
        if meta and any(_norm(n) == _norm(name) for n in _text_list(meta.get("instructors"))):
            titles.append(_text(meta.get("title")) or folder)
    return titles


def scan_instructors():
    """Every instructor folder (hidden and broken ones too) with diagnostics for the list page."""
    root = _instructors_dir()
    entries = []
    for folder in _subfolders(root):
        path = os.path.join(root, folder)
        data, err = _read_json_checked(os.path.join(path, "profile.json"))
        issues = []
        if err == "file not found":
            issues.append({"level": "warn", "text": "no profile.json; the folder name is used as the name"})
            data, err = {}, None
        elif err:
            issues.append({"level": "error", "text": f"profile.json: {err}; this person is ignored by the public site"})
            data = {}
        view = _instructor_view(folder, data or {}, path)
        status = "broken" if err else ("published" if view["published"] else "hidden")
        if not err:
            unknown = [k for k in data if k not in PROFILE_KEYS]
            if unknown:
                issues.append({"level": "info", "text": "unknown key(s) ignored by the site: " + ", ".join(f"'{k}'" for k in unknown)})
            if _text(data.get("email")) and not view["email"]:
                issues.append({"level": "warn", "text": "email does not look valid, so it is not shown"})
            if _text(data.get("phone")) and not view["phone"]:
                issues.append({"level": "warn", "text": "phone number does not look valid, so it is not shown"})
            if _text(data.get("website")) and not view["website"]:
                issues.append({"level": "warn", "text": "website is not a web address, so it is not shown"})
            if not _text(data.get("name")):
                issues.append({"level": "warn", "text": "no name; the folder name is used"})
        checks = [(label, bool(test(view))) for _, label, test in PROFILE_CHECKS]
        used_in = _courses_using(view["name"]) if not err else []
        entries.append({
            "slug": folder, "status": status, "name": view["name"], "role": view["role"], "photo": view["photo"],
            "founder": view["founder"], "order": view["order"], "cv": bool(view["cv"]), "checks": checks,
            "filled": sum(1 for _, ok in checks if ok), "total": len(checks),
            "used_in": used_in, "issues": issues,
            "n_error": sum(1 for i in issues if i["level"] == "error"),
            "n_warn": sum(1 for i in issues if i["level"] == "warn"),
        })
    return entries


def _rename_in_courses(old, new):
    """After an instructor is renamed, point every course at the new name. Returns how many courses changed."""
    changed = 0
    for folder in _subfolders(_courses_dir()):
        meta_path = os.path.join(_courses_dir(), folder, "meta.json")
        meta, err = _read_json_checked(meta_path)
        if meta is None or not isinstance(meta.get("instructors"), list):
            continue
        names = [new if isinstance(n, str) and _norm(n) == _norm(old) else n for n in meta["instructors"]]
        if names == meta["instructors"]:
            continue
        if not _valid_date(_text(meta.get("added"))):
            meta["added"] = _added_info(meta, meta_path)[1]          # rewriting must not make an old course look new
        meta["instructors"] = list(dict.fromkeys(names))
        _atomic_write(meta_path, _dump_meta(meta))
        changed += 1
    return changed


def _form_rows(prefix, fields, limit=30):
    """Rows of a repeatable section, in the order they appear in the form (blank rows dropped)."""
    pattern = re.compile(r"^%s-(\d+)-(%s)$" % (re.escape(prefix), "|".join(fields)))
    rows, order = {}, []
    for key in request.form.keys():
        m = pattern.match(key)
        if not m:
            continue
        if m.group(1) not in rows:
            rows[m.group(1)] = {}
            order.append(m.group(1))
        rows[m.group(1)][m.group(2)] = request.form.get(key, "").strip()
    result = [{f: rows[i].get(f, "") for f in fields} for i in order]
    return [r for r in result if any(r.values())][:limit]


def _tags(text):
    seen, out = set(), []
    for part in re.split(r"[\n,]", text or ""):
        part = part.strip()
        if part and part.casefold() not in seen:
            seen.add(part.casefold())
            out.append(part)
    return out


def _raw_rows(value, fields):
    """Rows for the form from stored JSON, without dropping anything the person typed."""
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            item = {fields[0]: item}
        if isinstance(item, dict):
            out.append({f: _text(item.get(f)) for f in fields})
    return out


def _instructor_form_from_data(data, slug, order=""):
    form = {"slug": slug or ""}
    for key in ("name", "role", "bio", "biography", "teaching_approach", "location", "email", "phone", "website"):
        form[key] = _text(data.get(key))
    form["order"] = str(data["order"]) if isinstance(data.get("order"), int) else (str(order) if order != "" else _text(data.get("order")))
    form["published"] = (not _is_false(data.get("published", True))) if data else False
    form["founder"] = _is_true(data.get("founder"))
    form["expertise"] = "\n".join(_text_list(data.get("expertise")))
    form["languages"] = "\n".join(_text_list(data.get("languages")))
    form["photo_style"] = clean_img_style(data.get("photo_style"), "photo")
    socials = data.get("socials")
    if isinstance(socials, dict):
        socials = [{"platform": k, "url": v} for k, v in socials.items()]
    form["socials"] = _raw_rows(socials, ("platform", "url"))
    for key, fields in ENTRY_FIELDS.items():
        form[key] = _raw_rows(data.get(key), fields)
    sections = []
    for item in data.get("sections") if isinstance(data.get("sections"), list) else []:
        if isinstance(item, dict):
            items = item.get("items")
            sections.append({"title": _text(item.get("title")),
                             "items": items if isinstance(items, str) else "\n".join(_text_list(items))})
    form["sections"] = sections
    return form


def _read_instructor_submission(creating, existing, own_slug):
    """Validate the posted form. Returns (form_for_rerender, result_or_None, errors)."""
    f = request.form
    errors = []
    form = {"slug": f.get("slug", "").strip().lower() if creating else own_slug or ""}
    for key in ("name", "role", "bio", "biography", "teaching_approach", "location", "email", "phone", "website", "order"):
        form[key] = f.get(key, "").strip()
    form["published"] = f.get("published") == "on"
    form["founder"] = f.get("founder") == "on"
    form["expertise"] = f.get("expertise", "")
    form["languages"] = f.get("languages", "")
    form["photo_style"] = read_img_style(f, "photo", "photo", errors, "Photo")

    def cap(label, text, limit):
        if len(text) > limit:
            errors.append(f"{label} is too long (max {limit} characters).")

    if creating:
        err = _slug_error(form["slug"])
        if err:
            errors.append(err)
        elif os.path.exists(os.path.join(_instructors_dir(), form["slug"])):
            errors.append(f"An instructor folder named '{form['slug']}' already exists.")
    if not form["name"]:
        errors.append("Name is required.")
    cap("Name", form["name"], 80)
    cap("Role", form["role"], 120)
    cap("Short description", form["bio"], 500)
    cap("Biography", form["biography"], 8000)
    cap("Teaching approach", form["teaching_approach"], 3000)
    cap("Location", form["location"], 100)
    if form["name"]:
        for other in load_instructors(include_hidden=True):
            if other["slug"] != (own_slug if not creating else None) and _norm(other["name"]) == _norm(form["name"]):
                errors.append(f"Another instructor is already called '{other['name']}'. Courses refer to instructors by name, so names must be different.")
                break
    order = None
    if form["order"]:
        try:
            order = int(form["order"])
        except ValueError:
            errors.append("Order must be a whole number.")
    if form["email"] and not EMAIL_RE.match(form["email"]):
        errors.append("Email does not look like an email address.")
    if form["phone"] and not PHONE_RE.match(form["phone"]):
        errors.append("Phone may only contain digits, spaces, + ( ) - . (5 to 25 characters).")
    if form["website"]:
        url, problem = clean_url(form["website"])
        if problem:
            errors.append(f"Website {problem}.")
        else:
            form["website"] = url
    cap("Website", form["website"], 300)

    tags = {"expertise": _tags(form["expertise"]), "languages": _tags(form["languages"])}
    for key, label in (("expertise", "Expertise"), ("languages", "Languages")):
        if len(tags[key]) > 40 or any(len(t) > 60 for t in tags[key]):
            errors.append(f"{label}: at most 40 items, each up to 60 characters.")

    form["socials"] = _form_rows("socials", ("platform", "url"))
    for n, row in enumerate(form["socials"], 1):
        if not row["url"]:
            errors.append(f"Link #{n}: add the web address for '{row['platform']}' or remove the row.")
            continue
        url, problem = clean_url(row["url"])
        if problem:
            errors.append(f"Link #{n} {problem}.")
        else:
            row["url"] = url
        row["platform"] = row["platform"] or "Link"
        cap(f"Link #{n} name", row["platform"], 40)
    for key, fields in ENTRY_FIELDS.items():
        form[key] = _form_rows(key, fields)
        for n, row in enumerate(form[key], 1):
            label = f"{ENTRY_LABELS[key]} #{n}"
            if not row[fields[0]]:
                errors.append(f"{label}: fill in the {fields[0].replace('_', ' ')} or remove the row.")
            for fld in fields:
                cap(f"{label} {fld}", row[fld], FIELD_LIMITS.get(fld, 150))
            if row.get("link"):
                url, problem = clean_url(row["link"])
                if problem:
                    errors.append(f"{label} link {problem}.")
                else:
                    row["link"] = url
    form["sections"] = _form_rows("sections", ("title", "items"), limit=12)
    for n, row in enumerate(form["sections"], 1):
        if not row["title"] or not row["items"]:
            errors.append(f"Extra section #{n}: it needs both a title and at least one item (or remove it).")
        cap(f"Extra section #{n} title", row["title"], 80)
        cap(f"Extra section #{n} items", row["items"], 3000)

    photo = cv = None
    file = request.files.get("photo")
    if file and file.filename:
        data = file.read(UPLOAD_LIMIT + 1)
        if len(data) > UPLOAD_LIMIT:
            errors.append("Photo is larger than 5 MB.")
        elif _sniff_image(data) is None:
            errors.append("Photo must be a real JPG, PNG, WEBP or GIF image (SVG is not accepted).")
        else:
            photo = (_sniff_image(data), data)
    file = request.files.get("cv")
    if file and file.filename:
        data = file.read(CV_LIMIT + 1)
        if len(data) > CV_LIMIT:
            errors.append("CV is larger than 5 MB.")
        elif b"%PDF-" not in data[:1024]:
            errors.append("CV must be a real PDF file.")
        else:
            cv = data

    if errors:
        return form, None, errors

    profile = dict(existing or {})
    profile["name"] = form["name"]
    for key in ("role", "bio", "biography", "teaching_approach", "location", "email", "phone", "website"):
        if form[key]:
            profile[key] = form[key]
        else:
            profile.pop(key, None)
    for key, value in (("expertise", tags["expertise"]), ("languages", tags["languages"]),
                       ("socials", form["socials"]), ("sections", [
                           {"title": r["title"], "items": _lines(r["items"])} for r in form["sections"]])):
        if value:
            profile[key] = value
        else:
            profile.pop(key, None)
    for key in ENTRY_FIELDS:
        if form[key]:
            profile[key] = form[key]
        else:
            profile.pop(key, None)
    profile["order"] = order if order is not None else 100
    profile["published"] = form["published"]
    if form["founder"]:
        profile["founder"] = True
    else:
        profile.pop("founder", None)
    store_img_style(profile, "photo_style", form["photo_style"], "photo")
    return form, {
        "profile": profile, "slug": form["slug"], "photo": photo, "cv": cv,
        "remove_photo": f.get("remove_photo") == "on", "remove_cv": f.get("remove_cv") == "on",
    }, []


def _instructor_editor(slug):
    creating = slug is None
    path = None if creating else _instructor_path(slug)
    existing, load_error = {}, None
    if not creating:
        existing, load_error = _read_json_checked(os.path.join(path, "profile.json"))
        if load_error == "file not found":
            existing, load_error = {}, None
        existing = existing or {}
    old_name = _text(existing.get("name")) or (slug or "")
    errors, status = [], 200
    if request.method == "POST":
        form, result, errors = _read_instructor_submission(creating, existing, slug)
        if result is not None:
            target = result["slug"] if creating else slug
            target_path = os.path.join(_instructors_dir(), target)
            if creating:
                os.makedirs(target_path)
            _atomic_write(os.path.join(target_path, "profile.json"), _dump_profile(result["profile"]))
            if result["photo"]:
                ext, data = result["photo"]
                _atomic_write(os.path.join(target_path, "photo" + ext), data)
                _remove_thumbnails(target_path, keep="photo" + ext, stem="photo")
            elif result["remove_photo"]:
                _remove_thumbnails(target_path, stem="photo")
            cv_path = os.path.join(target_path, CV_NAME)
            if result["cv"]:
                _atomic_write(cv_path, result["cv"])
            elif result["remove_cv"] and os.path.isfile(cv_path):
                os.remove(cv_path)
            renamed = 0
            if not creating and _norm(old_name) != _norm(result["profile"]["name"]):
                renamed = _rename_in_courses(old_name, result["profile"]["name"])
            log.info("admin: %s instructor '%s'", "created" if creating else "saved", target)
            if creating:
                flash(f"Created '{target}' as a draft. Tick 'published' when the profile is ready.", "success")
            else:
                msg = f"Saved '{target}'. The public site already shows the new version."
                if renamed:
                    msg += f" Renamed in {renamed} course(s) too."
                flash(msg, "success")
            return redirect(url_for("admin.instructor_edit", slug=target))
        status = 400
    else:
        if creating:
            taken = [p["order"] for p in load_instructors(include_hidden=True)]
            form = _instructor_form_from_data({}, "", (max(taken) + 1) if taken else 1)
            form["published"] = False
        else:
            form = _instructor_form_from_data(existing, slug)
    view = _instructor_view(slug, existing, path) if not creating else None
    photo = _find_image(path, "photo") if path else None
    return render_template(
        "admin_instructor_edit.html",
        creating=creating, slug=slug, form=form, errors=errors, load_error=load_error,
        photo=(f"instructors/{slug}/{photo}" if photo else None),
        cv=(f"instructors/{slug}/{CV_NAME}" if path and os.path.isfile(os.path.join(path, CV_NAME)) else None),
        cv_size=(os.path.getsize(os.path.join(path, CV_NAME)) if path and os.path.isfile(os.path.join(path, CV_NAME)) else 0),
        used_in=(_courses_using(old_name) if not creating else []),
        public_visible=(not creating and not load_error and form["published"]),
        social_suggestions=SOCIAL_SUGGESTIONS, entry_fields=ENTRY_FIELDS,
        checks=[(key, label) for key, label, _ in PROFILE_CHECKS],
    ), status


@admin_bp.route("/instructors")
def instructors_list():
    entries = scan_instructors()
    stats = {
        "total": len(entries),
        "published": sum(1 for e in entries if e["status"] == "published"),
        "hidden": sum(1 for e in entries if e["status"] == "hidden"),
        "broken": sum(1 for e in entries if e["status"] == "broken"),
    }
    return render_template("admin_instructors.html", people=entries, stats=stats)


@admin_bp.route("/instructor/new", methods=["GET", "POST"])
def instructor_new():
    return _instructor_editor(None)


@admin_bp.route("/instructor/<slug>", methods=["GET", "POST"])
def instructor_edit(slug):
    return _instructor_editor(slug)


@admin_bp.post("/instructor/<slug>/delete")
def instructor_delete(slug):
    path = _instructor_path(slug)
    data, _ = _read_json_checked(os.path.join(path, "profile.json"))
    used = _courses_using(_text((data or {}).get("name")) or slug)
    os.makedirs(_trash_dir(), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{TRASH_INSTRUCTOR_PREFIX}{slug}__{stamp}"
    dest, n = os.path.join(_trash_dir(), base), 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(_trash_dir(), f"{base}-{n}")
    shutil.move(path, dest)
    log.info("admin: moved instructor '%s' to trash", slug)
    msg = f"Moved '{slug}' to trash. You can restore it from the Trash page."
    if used:
        msg += f" {len(used)} course(s) still list this name and show it as plain text until it is restored."
    flash(msg, "success")
    return redirect(url_for("admin.instructors_list"))


@admin_bp.post("/instructor/<slug>/toggle")
def instructor_toggle(slug):
    path = _instructor_path(slug)
    data, err = _read_json_checked(os.path.join(path, "profile.json"))
    if data is None:
        flash(f"Cannot change '{slug}': profile.json {err}. Open it in the editor to repair it.", "error")
    else:
        now_published = _is_false(data.get("published", True))
        data["published"] = now_published
        _atomic_write(os.path.join(path, "profile.json"), _dump_profile(data))
        log.info("admin: instructor '%s' is now %s", slug, "published" if now_published else "hidden")
        flash(f"'{slug}' is now {'published' if now_published else 'hidden'}.", "success")
    return redirect(url_for("admin.instructors_list"))


# ---------------------------------------------------------------------------
# Admin: webinars
#
# A webinar only has a start time (nobody knows how long it will run), so nothing here is
# automatic: the join link opens when the start time arrives, and the webinar stays on the
# public site until you hide it or move it to the trash.
# ---------------------------------------------------------------------------

TRASH_WEBINAR_PREFIX = "webinar--"          # course slugs never contain "--", so the kinds cannot clash
WEBINAR_LIMITS = {"title": 120, "subtitle": 160, "description": 6000, "platform": 60, "capacity": 80, "item": 200, "time": 30}


def _webinar_path(slug):
    """Absolute path of an existing webinar folder, or 404 (same guards as _course_path)."""
    root = _webinars_dir()
    if (not slug or slug in (".", "..") or slug.startswith((".", "_"))
            or slug != os.path.basename(slug) or "\\" in slug or "\x00" in slug):
        abort(404)
    path = os.path.join(root, slug)
    if not os.path.isdir(path) or os.path.dirname(os.path.realpath(path)) != os.path.realpath(root):
        abort(404)
    return path


def _dump_webinar(data):
    ordered = {k: data[k] for k in WEBINAR_KEYS if k in data}
    ordered.update({k: v for k, v in data.items() if k not in ordered})      # keep hand-added keys
    return json.dumps(ordered, indent=4, ensure_ascii=False) + "\n"


def _span_label(seconds):
    seconds = max(0, int(seconds))
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return "less than a minute"


def scan_webinars():
    """Every webinar folder (hidden and broken ones too) with diagnostics for the list page."""
    root, now, lookups, entries = _webinars_dir(), now_bd(), _webinar_lookups(), []
    for folder in _subfolders(root):
        path = os.path.join(root, folder)
        data, err = _read_json_checked(os.path.join(path, "webinar.json"))
        issues = []
        if err:
            issues.append({"level": "error", "text": f"webinar.json: {err}; this folder is ignored by the public site"})
            entries.append({"slug": folder, "status": "broken", "title": folder, "state": "", "when": "", "note": "",
                            "join": False, "rsvp": False, "courses": [], "cover": False, "issues": issues,
                            "n_error": 1, "n_warn": 0})
            continue
        view = _webinar_view(folder, data, path, now, lookups)
        unknown = [k for k in data if k not in WEBINAR_KEYS]
        if unknown:
            issues.append({"level": "info", "text": "unknown key(s) ignored by the site: " + ", ".join(f"'{k}'" for k in unknown)})
        if (_text(data.get("date")) or _text(data.get("time"))) and view["start"] is None:
            issues.append({"level": "warn", "text": "date or time is not valid (use YYYY-MM-DD and HH:MM), so it is shown as 'date to be announced'"})
        if _text(data.get("join_link")) and not view["join_link"]:
            issues.append({"level": "warn", "text": "join link is not a web address, so it is not shown"})
        if _text(data.get("rsvp_link")) and not view["rsvp_link"]:
            issues.append({"level": "warn", "text": "registration link is not a web address, so it is not shown"})
        missing = [s for s in _text_list(data.get("courses")) if s not in lookups["courses"]]
        if missing:
            issues.append({"level": "warn", "text": "linked course(s) not found or hidden: " + ", ".join(missing)})
        if view["status"] == "started" and not view["join_link"]:
            issues.append({"level": "warn", "text": "it has started but there is no join link yet"})
        if view["status"] == "started":
            issues.append({"level": "info", "text": "the start time has passed; it stays on the site until you hide or delete it"})
        note = ""
        if view["status"] == "upcoming":
            note = "starts in " + _span_label(view["starts_in"])
        elif view["status"] == "started":
            note = "started " + _span_label(view["started_for"]) + " ago"
        entries.append({
            "slug": folder, "status": "published" if view["published"] else "hidden", "title": view["title"],
            "state": view["status"], "when": view["when_label"], "note": note,
            "join": bool(view["join_link"]), "rsvp": bool(view["rsvp_link"]),
            "courses": [c["title"] for c in view["courses"]], "cover": view["has_cover"], "issues": issues,
            "n_error": sum(1 for i in issues if i["level"] == "error"),
            "n_warn": sum(1 for i in issues if i["level"] == "warn"),
            "_order": _webinar_order(view),
        })
    entries.sort(key=lambda e: e.get("_order", (True, 0, e["title"].casefold())))
    return entries


def _webinar_form_from_data(data, slug):
    agenda = _raw_rows(data.get("agenda"), ["time", "item"])
    return {
        "slug": slug or "",
        "title": _text(data.get("title")), "subtitle": _text(data.get("subtitle")),
        "description": _text(data.get("description")),
        "date": _text(data.get("date")), "time": _text(data.get("time")),
        "platform": _text(data.get("platform")), "join_link": _text(data.get("join_link")),
        "rsvp_link": _text(data.get("rsvp_link")), "capacity": _text(data.get("capacity")),
        "agenda": agenda,
        "speakers": _text_list(data.get("speakers")), "courses": _text_list(data.get("courses")),
        "published": not _is_false(data.get("published", True)) if data else False,
        "cover_style": clean_img_style(data.get("cover_style"), "cover"),
    }


def _read_webinar_submission(creating, existing):
    """(form, result, errors). result is None when something is wrong; the form is then shown again."""
    f, errors = request.form, []
    form = {
        "slug": f.get("slug", "").strip().lower() if creating else "",
        **{k: f.get(k, "").strip() for k in ("title", "subtitle", "description", "date", "time", "platform",
                                              "join_link", "rsvp_link", "capacity")},
        "agenda": [{"time": r["time"], "item": r["item"]} for r in _form_rows("agenda", ["time", "item"])],
        "speakers": [s.strip() for s in f.getlist("speakers") if s.strip()],
        "courses": [s.strip() for s in f.getlist("courses") if s.strip()],
        "published": f.get("published") == "on",
    }
    form["cover_style"] = read_img_style(f, "cover", "cover", errors, "Cover picture")
    if creating:
        problem = _slug_error(form["slug"])
        if problem:
            errors.append(problem)
        elif os.path.exists(os.path.join(_webinars_dir(), form["slug"])):
            errors.append(f"A webinar folder named '{form['slug']}' already exists.")
    if not form["title"]:
        errors.append("Title is required.")
    for key, limit in WEBINAR_LIMITS.items():
        if key in form and len(form[key]) > limit:
            errors.append(f"{key.replace('_', ' ').capitalize()} is too long (max {limit} characters).")
    if form["date"] and not _valid_date(form["date"]):
        errors.append("Date is not valid.")
    if form["time"] and not TIME_RE.match(form["time"]):
        errors.append("Time is not valid (use HH:MM, e.g. 10:00).")
    if form["date"] and not form["time"]:
        errors.append("Add the start time too: the join link opens at that moment. Leave both empty for 'date to be announced'.")
    if form["time"] and not form["date"]:
        errors.append("Add the date too, or clear the time.")
    for key, label in (("join_link", "Join link"), ("rsvp_link", "Registration link")):
        cleaned, why = clean_url(form[key])
        if why:
            errors.append(f"{label} {why}.")
        elif cleaned:
            form[key] = cleaned
    if any(len(r["item"]) > WEBINAR_LIMITS["item"] or len(r["time"]) > WEBINAR_LIMITS["time"] for r in form["agenda"]):
        errors.append("An agenda line is too long.")
    if any(r["time"] and not r["item"] for r in form["agenda"]):
        errors.append("Every agenda line with a time also needs text.")
    known_courses = set(_subfolders(_courses_dir()))
    form["courses"] = [s for s in dict.fromkeys(form["courses"]) if s in known_courses]
    form["speakers"] = list(dict.fromkeys(form["speakers"]))

    cover = None
    ext, data, why = _read_upload("cover")
    if why:
        errors.append("Cover picture: " + why)
    elif data:
        cover = (ext, data)
    if errors:
        return form, None, errors

    out = {k: v for k, v in (existing or {}).items() if k not in WEBINAR_KEYS}      # keep hand-added keys
    out["title"] = form["title"]
    for key in ("subtitle", "description", "date", "time", "platform", "join_link", "rsvp_link", "capacity"):
        if form[key]:
            out[key] = form[key]
    if form["agenda"]:
        out["agenda"] = [{"time": r["time"], "item": r["item"]} for r in form["agenda"] if r["item"]]
    if form["speakers"]:
        out["speakers"] = form["speakers"]
    if form["courses"]:
        out["courses"] = form["courses"]
    out["published"] = form["published"]
    store_img_style(out, "cover_style", form["cover_style"], "cover")
    return form, {"data": out, "slug": form["slug"], "cover": cover, "remove_cover": f.get("remove_cover") == "on"}, []


def _webinar_editor(slug):
    creating = slug is None
    path = None if creating else _webinar_path(slug)
    existing, load_error = {}, None
    if not creating:
        existing, load_error = _read_json_checked(os.path.join(path, "webinar.json"))
        if load_error == "file not found":
            existing, load_error = {}, None
        existing = existing or {}
    errors, status = [], 200
    if request.method == "POST":
        form, result, errors = _read_webinar_submission(creating, existing)
        if result is not None:
            target = result["slug"] if creating else slug
            target_path = os.path.join(_webinars_dir(), target)
            os.makedirs(target_path, exist_ok=True)
            _atomic_write(os.path.join(target_path, "webinar.json"), _dump_webinar(result["data"]))
            if result["cover"]:
                ext, data = result["cover"]
                _atomic_write(os.path.join(target_path, "cover" + ext), data)
                _remove_thumbnails(target_path, keep="cover" + ext, stem="cover")
            elif result["remove_cover"]:
                _remove_thumbnails(target_path, stem="cover")
            log.info("admin: %s webinar '%s'", "created" if creating else "saved", target)
            if creating:
                flash(f"Created '{target}'" + ("" if result["data"]["published"] else " as a draft. Tick 'published' when it is ready") + ".", "success")
            else:
                flash(f"Saved '{target}'. The public site already shows the new version.", "success")
            return redirect(url_for("admin.webinar_edit", slug=target))
        status = 400
    else:
        form = _webinar_form_from_data(existing, slug)
        if creating:
            form["published"] = False
    lookups = _webinar_lookups()
    view = _webinar_view(slug, existing, path, now_bd(), lookups) if not creating else None
    people = [p["name"] for p in load_instructors(include_hidden=True)]
    speaker_options = people + [n for n in form["speakers"] if n not in people]
    course_options = [{"slug": s, "title": _text((_read_json_checked(os.path.join(_courses_dir(), s, "meta.json"))[0] or {}).get("title")) or s}
                      for s in _subfolders(_courses_dir())]
    cover = _find_image(path, "cover") if path else None
    return render_template(
        "admin_webinar_edit.html",
        creating=creating, slug=slug, form=form, errors=errors, load_error=load_error, view=view,
        cover=(f"webinars/{slug}/{cover}" if cover else None), default_cover=WEBINAR_DEFAULT_COVER,
        speaker_options=speaker_options, course_options=course_options,
        state_note=(_state_note(view) if view else ""),
        now_label=now_bd().strftime("%A, %d %B %Y, %I:%M %p").replace(" 0", " "),
    ), status


def _state_note(view):
    if view["status"] == "upcoming":
        return "Starts in " + _span_label(view["starts_in"]) + ". The join link stays hidden until then."
    if view["status"] == "started":
        return "Started " + _span_label(view["started_for"]) + " ago. It stays on the site until you hide or delete it."
    return "No date yet, so it is listed last as 'date to be announced'."


@admin_bp.route("/webinars")
def webinars_list():
    entries = scan_webinars()
    stats = {
        "total": len(entries),
        "upcoming": sum(1 for e in entries if e["status"] == "published" and e["state"] == "upcoming"),
        "started": sum(1 for e in entries if e["status"] == "published" and e["state"] == "started"),
        "hidden": sum(1 for e in entries if e["status"] == "hidden"),
        "broken": sum(1 for e in entries if e["status"] == "broken"),
    }
    return render_template("admin_webinars.html", webinars=entries, stats=stats, clock=TIME.status(),
                           now_label=now_bd().strftime("%A, %d %B %Y, %I:%M %p").replace(" 0", " "))


@admin_bp.route("/webinar/new", methods=["GET", "POST"])
def webinar_new():
    return _webinar_editor(None)


@admin_bp.route("/webinar/<slug>", methods=["GET", "POST"])
def webinar_edit(slug):
    return _webinar_editor(slug)


@admin_bp.post("/webinar/<slug>/delete")
def webinar_delete(slug):
    path = _webinar_path(slug)
    os.makedirs(_trash_dir(), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{TRASH_WEBINAR_PREFIX}{slug}__{stamp}"
    dest, n = os.path.join(_trash_dir(), base), 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(_trash_dir(), f"{base}-{n}")
    shutil.move(path, dest)
    log.info("admin: moved webinar '%s' to trash", slug)
    flash(f"Moved '{slug}' to trash. You can restore it from the Trash page.", "success")
    return redirect(url_for("admin.webinars_list"))


@admin_bp.post("/webinar/<slug>/toggle")
def webinar_toggle(slug):
    path = _webinar_path(slug)
    data, err = _read_json_checked(os.path.join(path, "webinar.json"))
    if data is None:
        flash(f"Cannot change '{slug}': webinar.json {err}. Open it in the editor to repair it.", "error")
    else:
        now_published = _is_false(data.get("published", True))
        data["published"] = now_published
        _atomic_write(os.path.join(path, "webinar.json"), _dump_webinar(data))
        log.info("admin: webinar '%s' is now %s", slug, "published" if now_published else "hidden")
        flash(f"'{slug}' is now {'published' if now_published else 'hidden'}.", "success")
    return redirect(url_for("admin.webinars_list"))


# ---------------------------------------------------------------------------
# Admin: branding (logos, the home slider's brand slide)
# ---------------------------------------------------------------------------

SVG_LIMIT = 1024 * 1024
SVG_BLOCKED = {"script", "foreignobject", "iframe", "object", "embed", "link", "animate", "animatemotion",
               "animatetransform", "set", "handler", "listener", "audio", "video", "canvas", "meta", "base"}
_SVG_URL_RE = re.compile(r"url\(\s*['\"]?\s*(?!#)", re.IGNORECASE)
_SVG_DATA_RE = re.compile(r"^data:image/(?:png|jpe?g|gif|webp);base64,", re.IGNORECASE)


def check_svg(data):
    """(data, error). A logo SVG is accepted only when it cannot run code or load anything from elsewhere."""
    if len(data) > SVG_LIMIT:
        return None, "the SVG is larger than 1 MB."
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, "the SVG must be saved as UTF-8 text."
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low or "<?xml-stylesheet" in low:
        return None, "the SVG contains a DOCTYPE, entity or external stylesheet, which is not allowed."
    try:
        root = ET.fromstring(text)
    except Exception as exc:
        return None, f"this is not a valid SVG file ({exc})."
    if _local(root.tag).lower() != "svg":
        return None, "this is not an SVG file."
    if not (root.get("viewBox") or (root.get("width") and root.get("height"))):
        return None, "the SVG needs a viewBox (or a width and height) so it can be scaled. Re-export it from your design tool."
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        name = _local(el.tag).lower()
        if name in SVG_BLOCKED:
            return None, f"the SVG uses <{name}>, which is not allowed (it could run code or load other files)."
        if name == "style" and el.text and (re.search(r"@import|javascript:|expression\(", el.text, re.IGNORECASE) or _SVG_URL_RE.search(el.text)):
            return None, "the SVG's style block loads something from outside, which is not allowed."
        for attr, value in el.attrib.items():
            key, val = _local(attr).lower(), value.strip()
            if key.startswith("on"):
                return None, "the SVG contains event handlers (onclick, onload …), which are not allowed."
            if key == "href" and not (val.startswith("#") or _SVG_DATA_RE.match(val)):
                return None, "the SVG links to something outside itself, which is not allowed."
            if "javascript:" in val.lower() or "@import" in val.lower() or _SVG_URL_RE.search(val):
                return None, "the SVG loads something from outside itself, which is not allowed."
    return data, None


def _read_upload(field, allow_svg=False):
    """(extension, bytes, error) for an optional image upload; (None, None, None) when nothing was chosen."""
    file = request.files.get(field)
    if not file or not file.filename:
        return None, None, None
    data = file.read(UPLOAD_LIMIT + 1)
    if len(data) > UPLOAD_LIMIT:
        return None, None, f"that file is larger than {UPLOAD_LIMIT // (1024 * 1024)} MB."
    ext = _sniff_image(data)
    if ext:
        return ext, data, None
    if allow_svg and b"<svg" in data[:4096].lower():
        clean, why = check_svg(data)
        return (".svg", clean, None) if clean else (None, None, why)
    return None, None, "use a " + ("PNG, JPG, WEBP, GIF or SVG" if allow_svg else "JPG, PNG, WEBP or GIF") + " image."


def _brand_number(form, errors, key, label):
    raw = form.get(key, "").strip()
    try:
        return int(float(raw))
    except ValueError:
        errors.append(f"{label} must be a number.")
        return None


def _read_branding_submission():
    """(settings, uploads, errors). `uploads` maps 'header'/'footer'/'slide' to (ext, bytes)."""
    f, errors = request.form, []
    files = _logo_files()
    settings = {"header": {}, "footer": {}, "slide": {}}
    for part in ("header", "footer"):
        label = part.capitalize()
        box = settings[part]
        choice = os.path.basename(f.get(f"{part}_logo", "").strip())
        box["logo"] = choice if choice in files else ""
        box["height"] = _brand_number(f, errors, f"{part}_height", f"{label} logo height")
        box["height_mobile"] = _brand_number(f, errors, f"{part}_height_mobile", f"{label} logo height on phones")
        box["align"] = f.get(f"{part}_align", "")
        if box["align"] not in BRAND_ALIGN:
            errors.append(f"{label} logo alignment is not valid.")
        box["img"] = read_img_style(f, f"{part}_img", "logo", errors, f"{label} logo")
    settings["footer"]["style"] = f.get("footer_style", "")
    if settings["footer"]["style"] not in FOOTER_STYLES:
        errors.append("Footer logo style is not valid.")
    slide = settings["slide"]
    for key, label in (("x", "Horizontal focus"), ("y", "Vertical focus"), ("zoom", "Zoom")):
        slide[key] = _brand_number(f, errors, f"slide_{key}", label)
    slide["fit"], slide["shape"], slide["ratio"], slide["side"] = (f.get("slide_fit", ""), f.get("slide_shape", ""),
                                                                 f.get("slide_ratio", ""), f.get("slide_side", ""))
    for key, options, label in (("fit", SLIDE_FITS, "Picture fit"), ("shape", SLIDE_SHAPES, "Picture shape"),
                                ("ratio", SLIDE_RATIOS, "Picture frame"), ("side", ("left", "right"), "Picture side")):
        if slide[key] not in options:
            errors.append(f"{label} is not valid.")
    slide["tilt"] = f.get("slide_tilt") == "on"
    slide["img"] = read_img_style(f, "slide_img", "slide", errors, "Slide picture")
    for key, limit in (("badge", 80), ("headline", 120), ("highlight", 60), ("text", 600), ("primary_label", 40), ("secondary_label", 40)):
        slide[key] = f.get(f"slide_{key}", "").strip()
        if len(slide[key]) > limit:
            errors.append(f"Slide {key.replace('_', ' ')} is too long (max {limit} characters).")
    if slide["highlight"] and slide["highlight"] not in slide["headline"]:
        errors.append("The highlighted word must appear in the headline.")

    uploads = {}
    for part, field, svg in (("header", "header_file", True), ("footer", "footer_file", True), ("slide", "slide_image", False)):
        ext, data, why = _read_upload(field, allow_svg=svg)
        if why:
            errors.append({"header": "Header logo", "footer": "Footer logo", "slide": "Slide picture"}[part] + ": " + why)
        elif data:
            uploads[part] = (ext, data)
    return settings, uploads, errors


@admin_bp.route("/branding", methods=["GET", "POST"])
def branding():
    errors, status = [], 200
    current = read_branding()
    if request.method == "POST":
        settings, uploads, errors = _read_branding_submission()
        if not errors:
            os.makedirs(_logo_dir(), exist_ok=True)
            os.makedirs(_images_dir(), exist_ok=True)
            for part in ("header", "footer"):
                if part in uploads:
                    ext, data = uploads[part]
                    _atomic_write(os.path.join(_logo_dir(), part + ext), data)
                    _remove_thumbnails(_logo_dir(), keep=part + ext, stem=part)
                    settings[part]["logo"] = part + ext
            if "slide" in uploads:
                ext, data = uploads["slide"]
                _atomic_write(os.path.join(_images_dir(), "brand-slide" + ext), data)
                _remove_thumbnails(_images_dir(), keep="brand-slide" + ext, stem="brand-slide")
            elif request.form.get("remove_slide_image") == "on":
                _remove_thumbnails(_images_dir(), stem="brand-slide")
            cleaned = clean_branding(settings)
            _atomic_write(os.path.join(_logo_dir(), BRAND_FILE), json.dumps(cleaned, indent=4, ensure_ascii=False) + "\n")
            log.info("admin: saved branding")
            flash("Saved. The public site already shows the new logos and slide.", "success")
            return redirect(url_for("admin.branding"))
        status = 400
        current = clean_branding(settings)
    live = load_branding()
    return render_template(
        "admin_branding.html", form=current, errors=errors, files=_logo_files(), live=live,
        ratios=list(SLIDE_RATIOS), limits=BRAND_LIMITS,
        custom_slide=live["slide"]["custom_image"], slide_image_url=live["slide"]["image_url"],
    ), status


# ===========================================================================
# Admin: file browser and content transfer (export / import)
#
# The file browser works inside content/ only. Code (static/css, static/js) and everything outside
# content/ (app.py, templates, the password and key files) cannot be reached from it.
# Deleting moves things to _trash/ (restorable); nothing here deletes for good.
# ===========================================================================

FB_HIDDEN_TOP = {"css", "js"}                                  # reserved: these names belong to static/ (code), never listed, exported or imported from content/
FB_ROLE_ROOTS = {"course": ("course", "admin.course_edit"),
                 "instructors": ("instructor", "admin.instructor_edit"),
                 "webinars": ("webinar", "admin.webinar_edit")}
FB_PROTECTED = {("course",), ("instructors",), ("webinars",), ("images",), ("logo",)}
FB_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}
FB_DOC_EXTS = {".pdf", ".txt", ".md", ".csv", ".json", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".odt", ".ods", ".odp"}
FB_MEDIA_EXTS = {".mp4", ".webm", ".mov", ".mp3", ".m4a", ".ogg", ".wav"}
FB_ARCHIVE_EXTS = {".zip"}
FB_ALLOWED_EXTS = FB_IMAGE_EXTS | FB_DOC_EXTS | FB_MEDIA_EXTS | FB_ARCHIVE_EXTS
FB_TEXT_EXTS = {".json", ".csv", ".txt", ".md"}
FB_EDIT_LIMIT = 512 * 1024
FB_MAX_NAME = 120
FB_MAX_DEPTH = 12
FB_SEARCH_LIMIT = 300
UNZIP_MAX_ENTRIES = 10000
TRASH_FILE_PREFIX = "file--"
_FB_NAME_BAD = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_FB_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


class FBError(Exception):
    def __init__(self, message, status=400, extra=None):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra or {}


def _fb_api(fn):
    """JSON endpoint wrapper: FBError and OSError become a JSON error the page can show."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            out = fn(*args, **kwargs)
        except FBError as exc:
            return jsonify({"ok": False, "error": exc.message, **exc.extra}), exc.status
        except OSError as exc:
            log.warning("file browser: %s", exc)
            return jsonify({"ok": False, "error": f"The server could not do that ({exc.strerror or exc.__class__.__name__})."}), 500
        return jsonify(out)
    return wrapper


def _fb_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ---------- names and paths ----------

def _fb_clean_name(name):
    """Make any file name safe to store (used for uploaded and unzipped names, which are not trusted)."""
    name = unicodedata.normalize("NFC", name or "")
    name = _FB_NAME_BAD.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()
    if len(name) > FB_MAX_NAME:
        stem, ext = os.path.splitext(name)
        name = stem[:max(1, FB_MAX_NAME - len(ext))] + ext
    if name.split(".")[0].lower() in _FB_RESERVED:
        name = "_" + name
    return name


def _fb_check_name(name):
    """A name typed by the admin: refused when wrong, never silently changed."""
    name = unicodedata.normalize("NFC", (name or "")).strip()
    if not name:
        raise FBError("Type a name.")
    if name.startswith("."):
        raise FBError("A name cannot start with a dot.")
    if _FB_NAME_BAD.search(name):
        raise FBError('A name cannot contain  / \\ : * ? " < > |')
    if name.endswith("."):
        raise FBError("A name cannot end with a dot.")
    if len(name) > FB_MAX_NAME:
        raise FBError(f"That name is longer than {FB_MAX_NAME} characters.")
    if name.split(".")[0].lower() in _FB_RESERVED:
        raise FBError(f"'{name}' is a reserved name on Windows; pick another.")
    return name


def _fb_parts(rel, allow_empty=True):
    """Clean list of path segments for a path relative to content/. Anything unsafe is refused."""
    rel = (rel or "").strip()
    if "\\" in rel or "\x00" in rel:
        raise FBError("Invalid path.")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if len(parts) > FB_MAX_DEPTH:
        raise FBError("That path is too deep.")
    for p in parts:
        if p == ".." or p.startswith(".") or _FB_NAME_BAD.search(p) or len(p) > FB_MAX_NAME:
            raise FBError("Invalid path.")
    if parts and parts[0] in FB_HIDDEN_TOP:
        raise FBError("That folder is not available here.", 403)
    if not parts and not allow_empty:
        raise FBError("Choose a file or folder first.")
    return parts


def _fb_abs(parts, must_exist=False):
    """Absolute path for clean segments. A symbolic link anywhere on the way is refused."""
    root = os.path.realpath(CONTENT_DIR)
    path = os.path.join(root, *parts) if parts else root
    if os.path.realpath(path) != os.path.normpath(path):
        raise FBError("Invalid path.", 403)
    if must_exist and not os.path.exists(path):
        raise FBError("That file or folder does not exist (anymore). Refresh the list.", 404)
    return path


def _fb_dir(parts):
    path = _fb_abs(parts, must_exist=True)
    if not os.path.isdir(path):
        raise FBError("That is not a folder.", 400)
    return path


def _fb_slot_error(full, is_dir):
    """Why something may not live at this path: inside course/, instructors/ and webinars/ every folder is one course,
    person or webinar, so it needs a proper folder name, and loose files do not belong directly there."""
    if len(full) >= 2 and full[0] in FB_ROLE_ROOTS:
        if len(full) == 2 and not is_dir:
            return f"files go inside a {FB_ROLE_ROOTS[full[0]][0]} folder, not directly in '{full[0]}'"
        err = _slug_error(full[1])
        if err:
            return err
    return None


def _fb_ext(name):
    return os.path.splitext(name)[1].lower()


def _fb_kind(ext):
    if ext in FB_IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in FB_ARCHIVE_EXTS:
        return "archive"
    if ext in {".mp4", ".webm", ".mov"}:
        return "video"
    if ext in {".mp3", ".m4a", ".ogg", ".wav"}:
        return "audio"
    if ext in FB_TEXT_EXTS:
        return "text"
    return "doc"


def _fb_verify(ext, head):
    """None when the first bytes fit the extension, else a short reason. A file name is never trusted."""
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        want = {".jpeg": ".jpg"}.get(ext, ext)
        return None if _sniff_image(head) == want else f"it is not a real {ext[1:].upper()} image"
    if ext == ".pdf":
        return None if head.lstrip()[:5] == b"%PDF-" else "it is not a real PDF"
    if ext == ".ico":
        return None if head[:4] == b"\x00\x00\x01\x00" else "it is not a real icon file"
    if ext in {".zip", ".xlsx", ".docx", ".pptx", ".odt", ".ods", ".odp"}:
        return None if head[:2] == b"PK" else "it is not a real zip-based file"
    if ext in {".xls", ".doc", ".ppt"}:
        return None if head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" else "it is not a real Office file"
    return None


def _fb_unique(path, is_dir=False):
    folder, name = os.path.split(path)
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    n = 1
    while True:
        cand = os.path.join(folder, f"{stem} ({n}){ext}")
        if not os.path.exists(cand):
            return cand
        n += 1


def _fb_visible(entry):
    name = entry.name
    return not name.startswith(".") and not _FB_NAME_BAD.search(name) and not entry.is_symlink()


def _fb_entry(parent, entry):
    """Description of one file or folder for the page."""
    st = entry.stat()
    is_dir = entry.is_dir()
    parts = parent + [entry.name]
    rel = "/".join(parts)
    out = {"name": entry.name, "path": rel, "dir": is_dir, "mtime": int(st.st_mtime)}
    if is_dir:
        out["kind"] = "folder"
        out["size"] = 0
        try:
            out["count"] = sum(1 for e in os.scandir(entry.path) if _fb_visible(e))
        except OSError:
            out["count"] = 0
        if len(parts) == 2 and parts[0] in FB_ROLE_ROOTS:
            role, endpoint = FB_ROLE_ROOTS[parts[0]]
            out["role"] = role
            out["edit"] = url_for(endpoint, slug=entry.name)
    else:
        ext = _fb_ext(entry.name)
        out["ext"] = ext
        out["kind"] = _fb_kind(ext)
        out["size"] = st.st_size
        out["url"] = url_for("media", filename=rel)
        out["text"] = ext in FB_TEXT_EXTS and st.st_size <= FB_EDIT_LIMIT
        out["allowed"] = ext in FB_ALLOWED_EXTS
    return out


# ---------- trash ----------

def _fb_trash(parts, force=False):
    """Move one file or folder to _trash/ together with a note of where it came from."""
    if not parts:
        raise FBError("Choose a file or folder first.")
    if not force and tuple(parts) in FB_PROTECTED:
        raise FBError(f"'{parts[-1]}' is a core folder of the site and cannot be deleted.")
    src = _fb_abs(parts, must_exist=True)
    root = _trash_dir()
    os.makedirs(root, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(root, f"{TRASH_FILE_PREFIX}{secrets.token_hex(4)}__{stamp}")
    os.makedirs(dest)
    try:
        _atomic_write(os.path.join(dest, "origin.json"), json.dumps({"path": "/".join(parts), "dir": os.path.isdir(src)}))
        shutil.move(src, os.path.join(dest, "payload"))
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def _fb_restore(payload, target):
    """Put a trashed item back. A folder that exists again is merged into; a clashing file is kept next to the old one."""
    if not os.path.exists(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(payload, target)
    elif os.path.isdir(payload) and os.path.isdir(target):
        for name in sorted(os.listdir(payload)):
            _fb_restore(os.path.join(payload, name), os.path.join(target, name))
        try:
            os.rmdir(payload)
        except OSError:
            pass
    else:
        shutil.move(payload, _fb_unique(target, os.path.isdir(payload)))


# ---------- copy / move ----------

def _fb_place(src, dst, move, policy, st):
    """Put src at dst. Folders merge into a folder of the same name; policy decides what happens to a clashing file."""
    if os.path.islink(src):
        return
    if os.path.isdir(src):
        if os.path.exists(dst) and not os.path.isdir(dst):
            st["skipped"] += 1
            return
        os.makedirs(dst, exist_ok=True)
        for name in sorted(os.listdir(src)):
            if not name.startswith(".tmp-"):
                _fb_place(os.path.join(src, name), os.path.join(dst, name), move, policy, st)
        if move:
            try:
                os.rmdir(src)
            except OSError:
                pass
        return
    if os.path.exists(dst):
        if os.path.isdir(dst) or policy == "skip":
            st["skipped"] += 1
            return
        if policy == "rename":
            dst = _fb_unique(dst)
            st["renamed"] += 1
        else:
            st["replaced"] += 1
    if move:
        try:
            os.replace(src, dst)
        except OSError:
            shutil.move(src, dst)
    else:
        tmp = os.path.join(os.path.dirname(dst), ".tmp-" + secrets.token_hex(4))
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    st["done"] += 1


def _fb_bulk(move):
    body = _fb_body()
    raw = body.get("paths")
    if not isinstance(raw, list) or not raw:
        raise FBError("Choose at least one file or folder.")
    dest_parts = _fb_parts(body.get("dest"))
    dest = _fb_dir(dest_parts)
    policy = body.get("policy", "ask")
    if policy not in ("ask", "replace", "rename", "skip"):
        raise FBError("Unknown conflict choice.")
    plan = []
    for item in raw[:500]:
        parts = _fb_parts(item, allow_empty=False)
        if move and tuple(parts) in FB_PROTECTED:
            raise FBError(f"'{parts[-1]}' is a core folder of the site and cannot be moved.")
        src = _fb_abs(parts, must_exist=True)
        is_dir = os.path.isdir(src)
        if is_dir and dest_parts[:len(parts)] == parts:
            raise FBError(f"'{parts[-1]}' cannot be put inside itself.")
        why = _fb_slot_error(dest_parts + [parts[-1]], is_dir)
        if why:
            raise FBError(f"Cannot put '{parts[-1]}' here: {why}.")
        same = parts[:-1] == dest_parts
        plan.append((src, os.path.join(dest, parts[-1]), parts[-1], is_dir, same))
    conflicts = [name for src, dst, name, is_dir, same in plan if os.path.exists(dst) and not (move and same)
                 and not (not move and same)]
    if conflicts and policy == "ask":
        raise FBError("Some names already exist here.", 409, {"conflicts": conflicts})
    st = {"done": 0, "replaced": 0, "renamed": 0, "skipped": 0}
    for src, dst, name, is_dir, same in plan:
        if move and same:
            continue
        eff = policy
        if os.path.exists(dst):
            if not move and same:
                eff = "rename"                                  # copying into the same folder: make "name (1)"
            if eff == "ask":
                eff = "rename"
            if eff == "skip":
                st["skipped"] += 1
                continue
            if eff == "rename":
                dst = _fb_unique(dst, is_dir)
                if not (not move and same):
                    st["renamed"] += 1
        _fb_place(src, dst, move, "replace" if eff != "skip" else "skip", st)
    return {"ok": True, **st}


# ---------- zip reading ----------

def _zip_scan(zf):
    """(members, skipped, error). members = [(info, [cleaned segments])]. A path that tries to leave the folder (a
    "zip slip"), too many entries, or an absurd compression ratio refuses the whole archive."""
    infos = zf.infolist()
    if len(infos) > UNZIP_MAX_ENTRIES:
        return [], [], f"The zip holds more than {UNZIP_MAX_ENTRIES} entries."
    members, skipped, total = [], [], 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.endswith("/"):
            continue
        segs = [s for s in name.split("/") if s not in ("", ".")]
        if not segs:
            continue
        if name.startswith("/") or any(s == ".." for s in segs) or re.match(r"^[A-Za-z]:", segs[0]):
            return [], [], f"The zip contains an unsafe path ({info.filename!r}), so it was refused."
        if any(s.startswith(".") or s == "__MACOSX" for s in segs):
            continue                                            # macOS / editor leftovers: ignored without a fuss
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            skipped.append((name, "it is a link"))
            continue
        if info.flag_bits & 0x1:
            skipped.append((name, "it is password protected"))
            continue
        ext = _fb_ext(segs[-1])
        if ext not in FB_ALLOWED_EXTS:
            skipped.append((name, f"files of type '{ext or 'none'}' are not allowed"))
            continue
        total += info.file_size
        if info.file_size > 20 * 1024 * 1024 and info.file_size / max(info.compress_size, 1) > 1000:
            return [], [], f"'{segs[-1]}' compresses suspiciously well (a zip bomb?), so the zip was refused."
        if total > UNZIP_MAX_TOTAL:
            return [], [], f"The zip unpacks to more than {UNZIP_MAX_TOTAL // (1024 * 1024)} MB (limit MAX_UNZIP_MB)."
        clean = [_fb_clean_name(s) for s in segs]
        if not all(clean):
            skipped.append((name, "its name is not usable"))
            continue
        members.append((info, clean))
    return members, skipped, None


def _zip_extract(zf, members, base_parts, policy, budget):
    """Write each member under content/<base_parts>/<its segments>. policy: skip | replace | rename."""
    st = {"written": 0, "replaced": 0, "renamed": 0, "kept": 0, "bytes": 0, "skipped": []}
    for info, segs in members:
        label = "/".join(segs)
        full = list(base_parts) + segs
        why = _fb_slot_error(full, False)
        if why:
            st["skipped"].append((label, why))
            continue
        try:
            dst = _fb_abs(full)
            if os.path.isdir(dst):
                st["skipped"].append((label, "a folder has that name"))
                continue
            exists = os.path.exists(dst)
            if exists and policy == "skip":
                st["kept"] += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
        except (FBError, OSError) as exc:
            st["skipped"].append((label, getattr(exc, "message", None) or "a file is in the way"))
            continue
        ext = _fb_ext(segs[-1])
        tmp = os.path.join(os.path.dirname(dst), ".tmp-" + secrets.token_hex(4))
        try:
            with zf.open(info) as src, open(tmp, "wb") as out:
                head = src.read(4096)
                bad = _fb_verify(ext, head)
                if bad:
                    st["skipped"].append((label, bad))
                    out.close()
                    os.unlink(tmp)
                    continue
                if ext == ".svg":
                    data = head + src.read(SVG_LIMIT + 1)
                    clean, reason = check_svg(data)
                    if not clean:
                        st["skipped"].append((label, "SVG refused: " + reason))
                        out.close()
                        os.unlink(tmp)
                        continue
                    out.write(clean)
                    written = len(clean)
                else:
                    out.write(head)
                    written = len(head)
                    while True:
                        chunk = src.read(1024 * 256)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size + 1024 or written > budget["left"]:
                            raise FBError("The zip unpacked to more than it declared; stopped.")
                        out.write(chunk)
            budget["left"] -= written
            if exists and policy == "rename":
                dst = _fb_unique(dst)
                st["renamed"] += 1
            elif exists:
                st["replaced"] += 1
            os.replace(tmp, dst)
            st["written"] += 1
            st["bytes"] += written
        except (zipfile.BadZipFile, zlib.error, EOFError):
            st["skipped"].append((label, "the file inside the zip is damaged"))
        except OSError as exc:
            st["skipped"].append((label, f"could not be written ({exc.strerror or 'disk error'})"))
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return st


def _fb_dest_conflicts(base_parts, members):
    out = []
    for info, segs in members:
        try:
            if os.path.exists(_fb_abs(list(base_parts) + segs)):
                out.append("/".join(segs))
        except FBError:
            continue
    return out


# ---------- page and listing routes ----------

@admin_bp.get("/files")
def files():
    return render_template("admin_files.html", start=request.args.get("path", ""), batch_limit=BIG_UPLOAD_LIMIT,
                           edit_limit=FB_EDIT_LIMIT, allowed=sorted(FB_ALLOWED_EXTS))


@admin_bp.get("/files/api/list")
@_fb_api
def files_list():
    parts = _fb_parts(request.args.get("path"))
    base = _fb_dir(parts)
    q = (request.args.get("q") or "").strip().lower()
    entries, truncated = [], False
    if q:
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            rel = os.path.relpath(dirpath, os.path.realpath(CONTENT_DIR)).replace(os.sep, "/")
            here = [] if rel == "." else rel.split("/")
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and not (not here and d in FB_HIDDEN_TOP))
            try:
                scan = [e for e in os.scandir(dirpath) if _fb_visible(e)]
            except OSError:
                continue
            for e in sorted(scan, key=lambda x: x.name.lower()):
                if q in e.name.lower():
                    item = _fb_entry(here, e)
                    item["where"] = "/".join(here)
                    entries.append(item)
                    if len(entries) >= FB_SEARCH_LIMIT:
                        truncated = True
                        break
            if truncated:
                break
    else:
        for e in os.scandir(base):
            if not _fb_visible(e) or (not parts and e.name in FB_HIDDEN_TOP):
                continue
            entries.append(_fb_entry(parts, e))
    return {"ok": True, "path": "/".join(parts), "entries": entries, "search": bool(q), "truncated": truncated,
            "protected": tuple(parts) in FB_PROTECTED}


@admin_bp.get("/files/api/tree")
@_fb_api
def files_tree():
    parts = _fb_parts(request.args.get("path"))
    base = _fb_dir(parts)
    out = []
    for e in sorted(os.scandir(base), key=lambda x: x.name.lower()):
        if not _fb_visible(e) or not e.is_dir() or (not parts and e.name in FB_HIDDEN_TOP):
            continue
        try:
            more = any(_fb_visible(c) and c.is_dir() for c in os.scandir(e.path))
        except OSError:
            more = False
        out.append({"name": e.name, "path": "/".join(parts + [e.name]), "more": more})
    return {"ok": True, "folders": out}


@admin_bp.get("/files/api/props")
@_fb_api
def files_props():
    parts = _fb_parts(request.args.get("path"), allow_empty=False)
    path = _fb_abs(parts, must_exist=True)
    st = os.stat(path)
    out = {"ok": True, "name": parts[-1], "path": "/".join(parts), "mtime": int(st.st_mtime), "dir": os.path.isdir(path)}
    if out["dir"]:
        size = files_n = folders_n = 0
        for dirpath, dirnames, names in os.walk(path):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            folders_n += len(dirnames)
            for n in names:
                if n.startswith("."):
                    continue
                try:
                    size += os.path.getsize(os.path.join(dirpath, n))
                    files_n += 1
                except OSError:
                    pass
        out.update(size=size, files=files_n, folders=folders_n)
    else:
        out.update(size=st.st_size, url=url_for("media", filename="/".join(parts)), type=_fb_ext(parts[-1]) or "file")
    return out


@admin_bp.post("/files/api/exists")
@_fb_api
def files_exists():
    body = _fb_body()
    dest = _fb_dir(_fb_parts(body.get("dest")))
    names = body.get("names")
    found = []
    for rel in (names if isinstance(names, list) else [])[:5000]:
        segs = [_fb_clean_name(s) for s in str(rel).replace("\\", "/").split("/") if s not in ("", ".")]
        if segs and os.path.isfile(os.path.join(dest, *segs)):
            found.append("/".join(segs))
    return {"ok": True, "exists": found[:200], "count": len(found)}


# ---------- changing things ----------

@admin_bp.post("/files/api/mkdir")
@_fb_api
def files_mkdir():
    body = _fb_body()
    parts = _fb_parts(body.get("path"))
    base = _fb_dir(parts)
    name = _fb_check_name(body.get("name"))
    why = _fb_slot_error(parts + [name], True)
    if why:
        raise FBError(why[0].upper() + why[1:] if why else why)
    target = os.path.join(base, name)
    if os.path.exists(target):
        raise FBError(f"'{name}' already exists here.", 409)
    os.mkdir(target)
    return {"ok": True, "path": "/".join(parts + [name])}


@admin_bp.post("/files/api/newfile")
@_fb_api
def files_newfile():
    body = _fb_body()
    parts = _fb_parts(body.get("path"))
    base = _fb_dir(parts)
    name = _fb_check_name(body.get("name"))
    if _fb_ext(name) not in FB_TEXT_EXTS:
        raise FBError("A new file must end in .txt, .md, .csv or .json.")
    why = _fb_slot_error(parts + [name], False)
    if why:
        raise FBError(why[0].upper() + why[1:])
    target = os.path.join(base, name)
    if os.path.exists(target):
        raise FBError(f"'{name}' already exists here.", 409)
    with open(target, "x", encoding="utf-8") as fh:
        fh.write("{}\n" if name.lower().endswith(".json") else "")
    return {"ok": True, "path": "/".join(parts + [name])}


@admin_bp.post("/files/api/rename")
@_fb_api
def files_rename():
    body = _fb_body()
    parts = _fb_parts(body.get("path"), allow_empty=False)
    if tuple(parts) in FB_PROTECTED:
        raise FBError(f"'{parts[-1]}' is a core folder of the site and cannot be renamed.")
    src = _fb_abs(parts, must_exist=True)
    name = _fb_check_name(body.get("name"))
    is_dir = os.path.isdir(src)
    if not is_dir and _fb_ext(name) not in FB_ALLOWED_EXTS:
        raise FBError(f"Files of type '{_fb_ext(name) or 'none'}' are not allowed here.")
    why = _fb_slot_error(parts[:-1] + [name], is_dir)
    if why:
        raise FBError(why[0].upper() + why[1:])
    dst = os.path.join(os.path.dirname(src), name)
    if os.path.exists(dst) and not os.path.samefile(src, dst):
        raise FBError(f"'{name}' already exists here.", 409)
    os.rename(src, dst)
    return {"ok": True, "path": "/".join(parts[:-1] + [name])}


@admin_bp.post("/files/api/move")
@_fb_api
def files_move():
    return _fb_bulk(True)


@admin_bp.post("/files/api/copy")
@_fb_api
def files_copy():
    return _fb_bulk(False)


@admin_bp.post("/files/api/delete")
@_fb_api
def files_delete():
    raw = _fb_body().get("paths")
    if not isinstance(raw, list) or not raw:
        raise FBError("Choose at least one file or folder.")
    targets = [_fb_parts(p, allow_empty=False) for p in raw[:500]]
    for parts in targets:
        if tuple(parts) in FB_PROTECTED:
            raise FBError(f"'{parts[-1]}' is a core folder of the site and cannot be deleted.")
    for parts in targets:
        _fb_trash(parts)
    log.info("admin: file browser moved %d item(s) to trash", len(targets))
    return {"ok": True, "count": len(targets)}


@admin_bp.post("/files/api/upload")
@_fb_api
def files_upload():
    parts = _fb_parts(request.form.get("path"))
    base = _fb_dir(parts)
    policy = request.form.get("policy", "rename")
    if policy not in ("replace", "rename", "skip"):
        policy = "rename"
    extract = request.form.get("extract") == "1"
    uploads = request.files.getlist("files")
    rels = request.form.getlist("relpath")
    if not uploads:
        raise FBError("No file was received. The upload may be bigger than the limit (MAX_UPLOAD_MB).")
    out = {"ok": True, "saved": 0, "replaced": 0, "renamed": 0, "kept": 0, "skipped": [], "extracted": 0}
    budget = {"left": UNZIP_MAX_TOTAL}
    for i, f in enumerate(uploads):
        rel = (rels[i] if i < len(rels) and rels[i] else f.filename) or ""
        segs = [_fb_clean_name(s) for s in rel.replace("\\", "/").split("/") if s not in ("", ".", "..")]
        segs = [s for s in segs if s]
        label = "/".join(segs) or "(no name)"
        if not segs:
            out["skipped"].append([label, "it has no usable name"])
            continue
        ext = _fb_ext(segs[-1])
        if ext not in FB_ALLOWED_EXTS:
            out["skipped"].append([label, f"files of type '{ext or 'none'}' are not allowed"])
            continue
        head = f.stream.read(4096)
        f.stream.seek(0)
        bad = _fb_verify(ext, head)
        if bad:
            out["skipped"].append([label, bad])
            continue
        if extract and ext == ".zip":
            try:
                with zipfile.ZipFile(f.stream) as zf:
                    members, skipped, err = _zip_scan(zf)
                    if err:
                        out["skipped"].append([label, err])
                        continue
                    stem = _fb_clean_name(os.path.splitext(segs[-1])[0]) or "unzipped"
                    res = _zip_extract(zf, members, parts + segs[:-1] + [stem], policy, budget)
                    out["extracted"] += res["written"]
                    out["replaced"] += res["replaced"]
                    out["renamed"] += res["renamed"]
                    out["kept"] += res["kept"]
                    out["skipped"] += [[n, r] for n, r in skipped + res["skipped"]]
            except zipfile.BadZipFile:
                out["skipped"].append([label, "it is not a readable zip"])
            continue
        full = parts + segs
        why = _fb_slot_error(full, False)
        if why:
            out["skipped"].append([label, why])
            continue
        try:
            dst = _fb_abs(full)
            if os.path.isdir(dst):
                out["skipped"].append([label, "a folder has that name"])
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
        except (FBError, OSError) as exc:
            out["skipped"].append([label, getattr(exc, "message", None) or "a file is in the way"])
            continue
        exists = os.path.exists(dst)
        if exists and policy == "skip":
            out["kept"] += 1
            continue
        tmp = os.path.join(os.path.dirname(dst), ".tmp-" + secrets.token_hex(4))
        try:
            if ext == ".svg":
                data = f.stream.read(SVG_LIMIT + 1)
                clean, reason = check_svg(data)
                if not clean:
                    out["skipped"].append([label, "SVG refused: " + reason])
                    continue
                _atomic_write(tmp, clean)
            else:
                f.save(tmp)
            if exists and policy == "rename":
                dst = _fb_unique(dst)
                out["renamed"] += 1
            elif exists:
                out["replaced"] += 1
            os.replace(tmp, dst)
            out["saved"] += 1
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    log.info("admin: file browser upload into '%s': %d saved, %d skipped", "/".join(parts), out["saved"], len(out["skipped"]))
    return out


@admin_bp.post("/files/api/extract")
@_fb_api
def files_extract():
    body = _fb_body()
    zparts = _fb_parts(body.get("path"), allow_empty=False)
    zpath = _fb_abs(zparts, must_exist=True)
    if _fb_ext(zparts[-1]) != ".zip" or not os.path.isfile(zpath):
        raise FBError("Choose a .zip file.")
    mode = body.get("mode", "folder")
    stem = os.path.splitext(zparts[-1])[0]
    dest_parts = zparts[:-1] if mode == "here" else zparts[:-1] + [_fb_check_name(stem)]
    policy = body.get("policy", "ask")
    if policy not in ("ask", "replace", "rename", "skip"):
        raise FBError("Unknown conflict choice.")
    try:
        zf = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile:
        raise FBError("That file is not a readable zip.")
    with zf:
        members, skipped, err = _zip_scan(zf)
        if err:
            raise FBError(err)
        if not members:
            raise FBError("There is nothing in that zip that can be unpacked here.", 400,
                          {"skipped": [[n, r] for n, r in skipped]})
        conflicts = _fb_dest_conflicts(dest_parts, members)
        if conflicts and policy == "ask":
            raise FBError("Some files already exist there.", 409, {"conflicts": conflicts[:30], "count": len(conflicts)})
        res = _zip_extract(zf, members, dest_parts, "replace" if policy == "ask" else policy, {"left": UNZIP_MAX_TOTAL})
    log.info("admin: unzipped '%s' into '%s' (%d file(s))", "/".join(zparts), "/".join(dest_parts), res["written"])
    return {"ok": True, "dest": "/".join(dest_parts), "written": res["written"], "replaced": res["replaced"],
            "renamed": res["renamed"], "kept": res["kept"], "skipped": [[n, r] for n, r in skipped + res["skipped"]]}


@admin_bp.get("/files/raw")
def files_raw():
    try:
        parts = _fb_parts(request.args.get("path"), allow_empty=False)
        path = _fb_abs(parts, must_exist=True)
    except FBError:
        abort(404)
    ext = _fb_ext(parts[-1])
    if not os.path.isfile(path) or ext not in FB_ALLOWED_EXTS:
        abort(404)
    mime = {".json": "application/json", ".csv": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
            ".md": "text/plain; charset=utf-8", ".svg": "image/svg+xml"}.get(ext) or mimetypes.guess_type(parts[-1])[0] or "application/octet-stream"
    download = request.args.get("dl") == "1"
    resp = send_file(path, mimetype=mime.split(";")[0], as_attachment=download, download_name=parts[-1], conditional=True)
    if mime.startswith("text/plain"):
        resp.headers["Content-Type"] = mime
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    if ext == ".svg":
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox"
    return resp


@admin_bp.get("/files/api/text")
@_fb_api
def files_text_read():
    parts = _fb_parts(request.args.get("path"), allow_empty=False)
    path = _fb_abs(parts, must_exist=True)
    if _fb_ext(parts[-1]) not in FB_TEXT_EXTS or not os.path.isfile(path):
        raise FBError("Only .json, .csv, .txt and .md files can be edited here.")
    if os.path.getsize(path) > FB_EDIT_LIMIT:
        raise FBError(f"That file is larger than {FB_EDIT_LIMIT // 1024} KB; download it to edit it.")
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    except UnicodeDecodeError:
        raise FBError("That file is not UTF-8 text, so it cannot be edited here.")
    return {"ok": True, "text": text, "mtime": os.path.getmtime(path), "name": parts[-1]}


@admin_bp.post("/files/api/text")
@_fb_api
def files_text_save():
    body = _fb_body()
    parts = _fb_parts(body.get("path"), allow_empty=False)
    path = _fb_abs(parts, must_exist=True)
    ext = _fb_ext(parts[-1])
    if ext not in FB_TEXT_EXTS or not os.path.isfile(path):
        raise FBError("Only .json, .csv, .txt and .md files can be edited here.")
    text = body.get("text")
    if not isinstance(text, str):
        raise FBError("Nothing to save.")
    if len(text.encode("utf-8")) > FB_EDIT_LIMIT:
        raise FBError(f"That is larger than {FB_EDIT_LIMIT // 1024} KB.")
    known = body.get("mtime")
    if known is not None and not body.get("force") and abs(os.path.getmtime(path) - float(known)) > 0.01:
        raise FBError("This file was changed by someone else since you opened it.", 409, {"changed": True})
    if ext == ".json":
        try:
            json.loads(text)
        except ValueError as exc:
            raise FBError(f"That is not valid JSON, so it was not saved: {exc}")
    _atomic_write(path, text)
    log.info("admin: edited '%s' in the file browser", "/".join(parts))
    return {"ok": True, "mtime": os.path.getmtime(path)}


# ---------- download as zip ----------

def _fb_zip_items(parts_list):
    """(absolute path, name inside the zip) for every file under the chosen items; hidden files and links are left out."""
    items, total = [], 0
    for parts in parts_list:
        top = _fb_abs(parts, must_exist=True)
        base = parts[-1]
        if os.path.isdir(top):
            items.append((None, base + "/"))
            for dirpath, dirnames, names in os.walk(top, followlinks=False):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and not os.path.islink(os.path.join(dirpath, d)))
                rel = os.path.relpath(dirpath, top).replace(os.sep, "/")
                prefix = base if rel == "." else base + "/" + rel
                if rel != ".":
                    items.append((None, prefix + "/"))
                for n in sorted(names):
                    full = os.path.join(dirpath, n)
                    if n.startswith(".") or os.path.islink(full):
                        continue
                    total += os.path.getsize(full)
                    items.append((full, prefix + "/" + n))
        elif not os.path.islink(top):
            total += os.path.getsize(top)
            items.append((top, base))
        if total > UNZIP_MAX_TOTAL:
            raise FBError(f"That is more than {UNZIP_MAX_TOTAL // (1024 * 1024)} MB; download it in smaller parts.")
    return items


def _temp_download(path, name, mimetype="application/zip"):
    """Send a temporary file as a download and delete it afterwards, even when the browser stops halfway."""
    size = os.path.getsize(path)

    def stream():
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    resp = Response(stream(), mimetype=mimetype)
    resp.headers.set("Content-Disposition", "attachment", filename=name)
    resp.headers["Content-Length"] = str(size)
    return resp


def _zip_response(items, name):
    fd, tmp = tempfile.mkstemp(prefix="skillwaves-zip-", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for full, arc in items:
                if full is None:
                    zf.writestr(zipfile.ZipInfo(arc), b"")
                else:
                    zf.write(full, arc)
    except BaseException:
        os.unlink(tmp)
        raise
    return _temp_download(tmp, name)


@admin_bp.post("/files/zip")
def files_zip():
    try:
        parts_list = [_fb_parts(p, allow_empty=False) for p in request.form.getlist("paths")[:500]]
        if not parts_list:
            raise FBError("Choose at least one file or folder.")
        items = _fb_zip_items(parts_list)
    except FBError as exc:
        abort(400, exc.message)
    name = (parts_list[0][-1] if len(parts_list) == 1 else "skillwaves-files") + ".zip"
    if len(parts_list) > 1:
        name = f"skillwaves-files-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    return _zip_response(items, name)


# ===========================================================================
# Content transfer: one zip with the site's content, to move it to another server
# (the code travels through git; the content never does)
# ===========================================================================

TRANSFER_MANIFEST = "skillwaves-export.json"
LEGACY_TRANSFER_MANIFEST = "belajar-export.json"      # exports made before the rename
TRANSFER_ROOTS = ("content", "static")                # "static" = zips made by the old layout
TRANSFER_PARTS = [
    ("courses", "Courses", "course"),
    ("instructors", "Instructors", "instructors"),
    ("webinars", "Webinars", "webinars"),
    ("images", "Images", "images"),
    ("branding", "Logos and branding settings", "logo"),
    ("other", "Other files in content/ (default webinar picture, anything you added yourself)", None),
]
_TRANSFER_BY_FOLDER = {folder: pid for pid, _, folder in TRANSFER_PARTS if folder}
TRANSFER_IDS = [p[0] for p in TRANSFER_PARTS]


def _transfer_part_of(rel_segs):
    """Which export part a path under content/ belongs to."""
    if not rel_segs or rel_segs[0] in FB_HIDDEN_TOP:
        return None
    return _TRANSFER_BY_FOLDER.get(rel_segs[0], "other") if len(rel_segs) > 1 or rel_segs[0] in _TRANSFER_BY_FOLDER else "other"


def _transfer_walk():
    """(absolute path, segments under content/, size) for every exportable file."""
    root = os.path.realpath(CONTENT_DIR)
    for dirpath, dirnames, names in os.walk(root, followlinks=False):
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/")
        here = [] if rel == "." else rel.split("/")
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and not (not here and d in FB_HIDDEN_TOP)
                             and not os.path.islink(os.path.join(dirpath, d)))
        for n in sorted(names):
            full = os.path.join(dirpath, n)
            if n.startswith(".") or os.path.islink(full) or _fb_ext(n) not in FB_ALLOWED_EXTS or _FB_NAME_BAD.search(n):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            yield full, here + [n], size


def _transfer_stats():
    stats = {pid: {"files": 0, "size": 0} for pid in TRANSFER_IDS}
    for _, segs, size in _transfer_walk():
        pid = _transfer_part_of(segs)
        if pid:
            stats[pid]["files"] += 1
            stats[pid]["size"] += size
    return stats


def _import_dir():
    return os.path.join(app.root_path, "_import")


def _import_clean_old():
    root = _import_dir()
    if not os.path.isdir(root):
        return
    for n in os.listdir(root):
        p = os.path.join(root, n)
        try:
            if time.time() - os.path.getmtime(p) > 24 * 3600:
                os.unlink(p)
        except OSError:
            pass


def _import_path(token):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        abort(404)
    path = os.path.join(_import_dir(), token + ".zip")
    if not os.path.isfile(path):
        abort(404)
    return path


def _import_analyse(zpath):
    """(info, error). info lists what the bundle holds, per part, and how much of it already exists here."""
    try:
        zf = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile:
        return None, "That file is not a readable zip."
    with zf:
        members, skipped, err = _zip_scan(zf)
        if err:
            return None, err
        manifest = None
        manifest_name = next((n for n in (TRANSFER_MANIFEST, LEGACY_TRANSFER_MANIFEST) if n in zf.namelist()), None)
        if manifest_name:
            try:
                manifest = json.loads(zf.read(manifest_name).decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError, KeyError):
                return None, "The export information inside the zip is damaged."
            if not isinstance(manifest, dict) or manifest.get("app") not in ("skillwaves", "belajar") or manifest.get("format") != 1:
                return None, "This zip was not made by this site's Export (or is from an incompatible version)."
        parts = {pid: {"files": 0, "size": 0, "existing": 0} for pid in TRANSFER_IDS}
        usable, odd = [], []
        for info, segs in members:
            if segs[0] not in TRANSFER_ROOTS or len(segs) < 2:
                if segs in ([TRANSFER_MANIFEST], [LEGACY_TRANSFER_MANIFEST]):
                    continue
                odd.append(("/".join(segs), "it is not inside a content/ folder"))
                continue
            rel = segs[1:]
            pid = _transfer_part_of(rel)
            if pid is None:
                odd.append(("/".join(segs), "code files are never imported"))
                continue
            why = _fb_slot_error(rel, False)
            if why:
                odd.append(("/".join(segs), why))
                continue
            parts[pid]["files"] += 1
            parts[pid]["size"] += info.file_size
            try:
                if os.path.exists(_fb_abs(rel)):
                    parts[pid]["existing"] += 1
            except FBError:
                pass
            usable.append((info, rel, pid))
        if not usable:
            return None, "There is no site content in that zip."
    return {"parts": parts, "skipped": [[n, r] for n, r in skipped + odd], "manifest": manifest, "count": len(usable)}, None


@admin_bp.get("/transfer")
def transfer():
    _import_clean_old()
    return render_template("admin_transfer.html", parts=TRANSFER_PARTS, stats=_transfer_stats(), limit=BIG_UPLOAD_LIMIT)


@admin_bp.post("/transfer/export")
def transfer_export():
    chosen = [p for p in request.form.getlist("parts") if p in TRANSFER_IDS]
    if not chosen:
        flash("Tick at least one part to export.", "error")
        return redirect(url_for("admin.transfer"))
    items, counts = [], {pid: 0 for pid in chosen}
    for full, segs, size in _transfer_walk():
        pid = _transfer_part_of(segs)
        if pid in counts:
            items.append((full, "content/" + "/".join(segs)))
            counts[pid] += 1
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    manifest = {"app": "skillwaves", "format": 1, "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "parts": chosen, "files": counts}
    fd, tmp = tempfile.mkstemp(prefix="skillwaves-export-", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(TRANSFER_MANIFEST, json.dumps(manifest, indent=2) + "\n")
            for full, arc in items:
                zf.write(full, arc)
    except BaseException:
        os.unlink(tmp)
        raise
    log.info("admin: exported %d file(s) (%s)", len(items), ", ".join(chosen))
    return _temp_download(tmp, f"skillwaves-content-{stamp}.zip")


@admin_bp.post("/transfer/import/upload")
def transfer_import_upload():
    bundle = request.files.get("bundle")
    if not bundle or not bundle.filename:
        return jsonify({"ok": False, "error": "Choose the zip you exported."}), 400
    os.makedirs(_import_dir(), exist_ok=True)
    token = secrets.token_hex(16)
    path = os.path.join(_import_dir(), token + ".zip")
    bundle.save(path)
    info, err = _import_analyse(path)
    if err:
        os.unlink(path)
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "url": url_for("admin.transfer_import_review", token=token)})


@admin_bp.get("/transfer/import/<token>")
def transfer_import_review(token):
    path = _import_path(token)
    info, err = _import_analyse(path)
    if err:
        flash(err, "error")
        return redirect(url_for("admin.transfer"))
    return render_template("admin_import.html", token=token, info=info, parts=TRANSFER_PARTS)


@admin_bp.post("/transfer/import/<token>/apply")
def transfer_import_apply(token):
    path = _import_path(token)
    mode = request.form.get("mode", "add")
    chosen = [p for p in request.form.getlist("parts") if p in TRANSFER_IDS]
    if mode not in ("add", "overwrite", "replace") or not chosen:
        flash("Choose what to import and how.", "error")
        return redirect(url_for("admin.transfer_import_review", token=token))
    zf = zipfile.ZipFile(path)
    with zf:
        members, skipped, err = _zip_scan(zf)
        if err:
            flash(err, "error")
            return redirect(url_for("admin.transfer"))
        wanted = []
        for info, segs in members:
            if segs[0] in TRANSFER_ROOTS and len(segs) > 1 and _transfer_part_of(segs[1:]) in chosen:
                wanted.append((info, segs[1:]))
        if mode == "replace":
            # the current content of the chosen parts goes to the trash first, so nothing is lost for good
            for pid in chosen:
                folder = next(f for p, _, f in TRANSFER_PARTS if p == pid)
                if folder:
                    target = _fb_abs([folder])
                    if os.path.isdir(target) and any(_fb_visible(e) for e in os.scandir(target)):
                        _fb_trash([folder], force=True)
                    os.makedirs(target, exist_ok=True)
                else:
                    for e in list(os.scandir(_fb_abs([]))):
                        if _fb_visible(e) and e.name not in FB_HIDDEN_TOP and e.name not in _TRANSFER_BY_FOLDER:
                            _fb_trash([e.name], force=True)
        res = _zip_extract(zf, wanted, [], "skip" if mode == "add" else "replace", {"left": UNZIP_MAX_TOTAL})
    try:
        os.unlink(path)
    except OSError:
        pass
    log.info("admin: imported %d file(s) (%s, mode %s), %d skipped", res["written"], ", ".join(chosen), mode, len(res["skipped"]))
    for name, why in res["skipped"][:20]:
        log.warning("import: skipped %s (%s)", name, why)
    msg = f"Imported {res['written']} file(s)"
    if res["replaced"]:
        msg += f", {res['replaced']} replaced"
    if res["kept"]:
        msg += f", {res['kept']} already here and left alone"
    if res["skipped"]:
        msg += f", {len(res['skipped'])} skipped (see the log on the system page)"
    flash(msg + ".", "success" if res["written"] or not res["skipped"] else "error")
    return redirect(url_for("admin.transfer"))



app.register_blueprint(admin_bp)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
