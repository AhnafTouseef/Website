# Belajar.id Flask Website

A Flask-based frontend for the Belajar.id learning platform.

## Run locally

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5001`

## Course cards and thumbnails

Every course thumbnail is shown in a fixed **16:9** frame and cropped to fill it, so no image can change a card's size. Use a 16:9 image (e.g. 1280x720) to avoid cropping. To change the shape for the whole site, edit `--thumb-ratio` at the top of `static/css/main.css`.

## Adding content (no code changes needed)

Courses and instructors are read from folders on every page load, so just add or edit files and refresh the browser.

### Add a course: `static/course/<course-folder>/`

The folder name becomes the course URL (`/course/<course-folder>`), so use lowercase letters and hyphens.

| File | Purpose |
|---|---|
| `meta.json` | Course details (see below) |
| `curriculum.csv` | Week-by-week topics |
| `thumbnail.jpg` | Course image (`.png`, `.webp`, `.jpeg` also work) |

`meta.json` keys (all optional except you should set `title`):

```json
{
    "title": "Course title",
    "tagline": "Enrolling Now · Cohort 1",
    "subtitle": "One-line summary shown on cards",
    "description": "Longer text shown at the top of the course page",
    "duration": "8 Weeks",
    "capacity": "Max 30 Students",
    "certificate": "Certificate",
    "price": "Tk 500.00",
    "start_date": "July 21, 2024",
    "enroll_link": "https://forms.gle/...",
    "webinar_link": "",
    "features": ["shown in the price box"],
    "what_you_will_learn": ["shown as ticks"],
    "instructors": ["Exact Name As In profile.json"],
    "order": 1,
    "added": "2026-10-25",
    "published": true
}
```

- `webinar_link` empty = the "Try free webinar first" link opens the site's own `/webinar` page.
- `order`: lower numbers first. The first course is also what the header "Enroll Now" button opens.
- `added`: date the course was added (`YYYY-MM-DD`). The home page shows the **2 newest** courses by this date; the Courses page shows all of them in a grid ordered by `order`. If `added` is missing, the file's modified time is used, so set it. The admin console fills it in automatically.
- `published: false` hides a course without deleting its folder.
- Folders starting with `_` or `.` are ignored.

`curriculum.csv` has two columns, `week` and `topic`, one topic per row. Leave `week` blank to keep the same week as the row above:

```csv
week,topic
Week 1–2: Python Basics,Variables & data types
,Control flow
Week 3–4: Data Structures,"Lists, dictionaries, sets"
```

If you edit it in Excel, save as **CSV UTF-8**.

### Add an instructor: `static/instructors/<person-folder>/`

| File | Purpose |
|---|---|
| `profile.json` | `{"name": "...", "role": "...", "bio": "...", "order": 1}` |
| `photo.jpg` | Profile image (`.png`, `.webp`, `.jpeg` also work) |

A course picks instructors by `name` (case and extra spaces don't matter). Instructors also appear on the About page.

Problems (bad JSON, unknown instructor name, missing curriculum) never crash the site; they are printed in the terminal where `python app.py` is running.

## Admin console (hidden)

A developer-style control panel for courses. It is not linked from the public site.

- URL: `http://127.0.0.1:5001/_console/` (change the path with the `ADMIN_URL_PREFIX` environment variable, e.g. `ADMIN_URL_PREFIX=ops-7f3k`)
- Password: on first start a random password is generated, saved in `admin_password.txt` and printed once in the terminal. To choose your own, set the `ADMIN_PASSWORD` environment variable or edit that file.
- `secret_key.txt` (session signing key) is generated the same way, or set `SECRET_KEY`. Keep both files private; `.gitignore` already excludes them.

What it does:

| Page | Purpose |
|---|---|
| courses | every folder in `static/course/` incl. hidden and broken ones; stats, health warnings, raw `meta.json` / `curriculum.csv`, file list; edit, publish/hide, duplicate, delete |
| + new / edit | form for all `meta.json` fields, curriculum CSV editor with live parsed preview, thumbnail upload, live `meta.json` preview, Ctrl+S to save |
| trash | deleted courses are moved to `_trash/` (outside `static/`), restorable or deletable forever |
| system | versions, paths, registered routes, instructors/logos found, recent log |

Safety notes:

- Login is rate-limited (5 wrong passwords = 5 minute lock) and sessions expire after 8 hours idle. Every action needs a CSRF token.
- Uploaded thumbnails are checked by their real content (JPG/PNG/WEBP/GIF only, max 5 MB); the file name you upload is ignored.
- The folder name (slug) is fixed after creation. Editing a course keeps any extra keys you added to `meta.json` by hand.
- Instructors and the pipeline/FAQ lists are not editable here: instructors are folders, the other two live in `app.py`.
- If you deploy this publicly: use HTTPS, turn off `debug=True` in the last line of `app.py`, and set `ADMIN_PASSWORD` / `SECRET_KEY` as environment variables.
