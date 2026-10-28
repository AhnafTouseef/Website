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

Home page: the **2 newest** courses appear as compact side-by-side cards; with only 1 course it becomes a single card spanning the whole section. The Courses page gives each card a stable, random-looking pastel background (picked from the course folder name, from the palette `CARD_TINTS` in `app.py`).

## Adding content (no code changes needed)

Courses, instructors and webinars are read from folders on every page load, so just add or edit files and refresh the browser.

### Add a course: `static/course/<course-folder>/`

The folder name becomes the course URL (`/course/<course-folder>`), so use lowercase letters and hyphens.

| File | Purpose |
|---|---|
| `meta.json` | Course details (see below) |
| `curriculum.csv` | The curriculum sheet, any columns (see below) |
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
    "discount": 20,
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

- `discount` (optional): a percentage from 0 to 99.99 (`20` or `12.5`). Leave it out for no discount. The public site then shows the original price grey and struck through, the new price in a vivid red, and an "N% OFF" badge on the card thumbnail and course page; the Enroll button shows the discounted price too. The new price is worked out with whole-number maths and keeps your currency text, commas and decimals (`Tk 1,500.00` at 12.5% = `Tk 1,312.50`). `price` must contain a number for a discount to apply (a warning is logged otherwise). Keep `price` as the normal, undiscounted price.
- `webinar_link` empty = the "Try free webinar first" link opens the site's own `/webinar` page.
- `order`: lower numbers first. The first course is also what the header "Enroll Now" button opens.
- `added`: date the course was added (`YYYY-MM-DD`). The home page shows the **2 newest** courses by this date; the Courses page shows all of them in a grid ordered by `order`. If `added` is missing, the file's modified time is used, so set it. The admin console fills it in automatically.
- `published: false` hides a course without deleting its folder.
- Folders starting with `_` or `.` are ignored.

`curriculum.csv` is **whatever sheet the instructor wrote**: any number of columns, any rows, any wording. The course page shows it exactly as it is, as a table: the first row becomes the table header, every other row is shown in its own order, and nothing is renamed, grouped or reinterpreted. Different instructors can lay their curriculum out differently.

```csv
Month,Session,Date,Hours,Topics covered
Month 1,Intro,05-Nov-2026,3,"Setup
Variables"
,Loops,12-Nov-2026,3,for / while
Month 2,Project kickoff,26-Nov-2026,2,Plan your project
```

- An empty cell under a filled cell is drawn joined to it (like a merged cell in Excel).
- Line breaks inside a cell are kept, and web addresses (`https://…`) become links.
- Only empty rows at the top/bottom and columns with no content at all are dropped. Limits: 2000 rows, 40 columns.
- Sheets with up to 3 columns sit in the main column; wider sheets get the full page width and scroll sideways on phones.
- If you edit it in Excel, save as **CSV UTF-8**, or skip this and upload the `.xlsx` in the admin console.

### Add an instructor: `static/instructors/<person-folder>/`

| File | Purpose |
|---|---|
| `profile.json` | the profile (below) |
| `photo.jpg` | Profile image (`.png`, `.webp`, `.jpeg` also work) |
| `cv.pdf` | optional CV, linked from the profile page |

Only `name` is needed. **Every other key is optional**, and a section is simply not shown on the public profile when it is empty (a fresh graduate has no job experience, and that is fine):

```json
{
    "name": "Jane Doe",
    "role": "Lead Instructor",
    "bio": "Short description for the instructor cards.",
    "biography": "Longer story.\n\nA blank line starts a new paragraph.",
    "teaching_approach": "What students can expect.",
    "location": "Dhaka, Bangladesh",
    "email": "jane@example.com",
    "phone": "+880 1711-000000",
    "website": "https://example.com",
    "socials": [{"platform": "LinkedIn", "url": "https://linkedin.com/in/jane"}],
    "expertise": ["Python", "Data analysis"],
    "languages": ["Bangla", "English"],
    "education": [{"degree": "B.Sc. in CSE", "institution": "KUET", "period": "2019-2023", "details": ""}],
    "experience": [{"role": "Data Analyst", "organization": "Acme", "period": "2023-now", "details": ""}],
    "certifications": [{"name": "AWS SA", "issuer": "AWS", "year": "2024", "link": ""}],
    "achievements": [{"title": "Won a hackathon", "year": "2025", "details": "", "link": ""}],
    "sections": [{"title": "Awards", "items": ["Gold medal", "Best paper"]}],
    "order": 1,
    "published": true
}
```

- `bio` is the short text on cards; `biography` is the long text on the profile page `/instructor/<folder-name>`.
- Links must be `http(s)` web addresses (`github.com/me` is turned into `https://github.com/me`). Anything else (`javascript:` etc.) is dropped. A bad email/phone is not shown.
- `published: false` hides the instructor everywhere on the public site.
- Courses pick instructors by `name` (case and extra spaces don't matter). Course pages and the About page link each instructor card to their profile page.

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
| + new / edit | form for all `meta.json` fields incl. optional discount % with a live "was / now" preview; curriculum import from an Excel (`.xlsx`) or CSV file (drag and drop; first sheet; **all columns and rows are kept exactly as in the sheet**, dates and numbers formatted as Excel shows them, hidden rows/columns left out) with a live table preview and the text box still editable; thumbnail uploader with drag and drop, 16:9 preview, one-click remove with undo; live `meta.json` preview; Ctrl+S to save |
| instructors | the people behind the courses: add, edit, publish/hide, delete (see below) |
| webinars | add, edit, hide and delete live sessions (see below) |
| branding | logo, hero picture and hero text (see below) |
| trash | deleted courses, instructors and webinars are moved to `_trash/` (outside `static/`), restorable or deletable forever |
| system | versions, paths, registered routes, instructors/logos/webinars found, the site clock and its status, recent log |

### Instructors section

- **There are no instructor accounts.** Profiles are created and edited in this one console by the manager, or by an instructor the manager lets use it. Everything goes through the same login, CSRF protection and activity log.
- **+ add instructor** opens a form where only the name is required. Optional sections (each can simply be left empty): photo, short description, biography, how I teach, location, email, phone, website, any number of social/professional links, CV (PDF), expertise tags, languages, education, experience, certifications, projects and achievements, and free-form extra sections (publications, awards, volunteering...). Repeatable lists have **+ add**, up/down and ✕ buttons, and a "profile strength" checklist shows what is filled in.
- New instructors start as drafts (hidden) until you tick **published**.
- Renaming an instructor updates every course that lists the old name. Deleting moves the folder to `_trash/` (restorable); courses that still list the name show it as plain text.
- Uploads are checked by content: photos must be real JPG/PNG/WEBP/GIF (max 5 MB), the CV a real PDF (max 5 MB). Everything you enter is public, so add only what you want shown.

### Webinars section

A webinar lives in `static/webinars/<folder>/webinar.json` (plus an optional `cover.jpg/png/webp/gif`; without one, `static/webinar.jpg` is used). You can create zero, one or many, from the admin or by hand.

| Key | Meaning |
|---|---|
| `title` (required), `subtitle`, `description` | text; blank lines in the description make paragraphs |
| `date` (`YYYY-MM-DD`), `time` (`HH:MM`, 24 h) | the **start** time, always **Bangladesh time (UTC+6)** |
| `platform`, `capacity` | shown as facts; capacity is text only, nothing is enforced |
| `join_link` | revealed on the page at the start time (never present in the page before that) |
| `rsvp_link` | optional "let us know you're coming" link. Anyone can join without it; it is only a headcount |
| `agenda` | list of `{time, item}` |
| `speakers` | instructor names (linked to their profile pages) |
| `courses` | course folder names; a course page shows a "Webinar: ..." link to its soonest linked webinar |
| `published` | `false` hides it everywhere |

How it behaves:

- A webinar only has a start time (nobody knows how long it will run), so there is no "ended" state. It shows **upcoming** until the start time, then **started** with the join link. It stays on the page **until you hide or delete it** in the admin; nothing is removed automatically. There are no recordings and no past list.
- `/webinar` shows the soonest webinar with full details, then every later one as a single full-width notice line ("More details" opens the rest). Later lines carry no join or RSVP links; when one becomes the soonest it moves to the top and gets them. With none planned it says so.
- The home hero is a slider: the brand slide first, then up to 3 webinar slides. With no webinar it is just the brand slide. It autoplays, and pauses on hover, keyboard focus and when the tab is hidden; no autoplay when the visitor prefers reduced motion.
- A legacy `webinar_link` in a course's `meta.json` still wins over the linked webinar.
- Restoring a webinar from the trash brings it back as a draft (hidden) until you tick **published**.

#### Time

The site never trusts the visitor's device clock. The server keeps its own clock corrected from an online time service (tried in order: timeapi.io, worldtimeapi.org, the `Date` header of google.com), re-checked every 10 minutes, retried every minute after a failure. If no service is reachable it keeps the last correction, or uses the server's own clock. The join link reveals itself by the page reloading once, at the moment the server says the webinar starts.

- The **system** page shows the clock status (ok / stale / waiting / off) and the current Bangladesh time.
- Set `TIME_SYNC=off` to skip the online check (e.g. offline development, tests).
- Keep the server's own clock reasonably right too (NTP is on by default on most hosts); it is the fallback.

### Branding section

Logo, hero picture and hero text are managed here, not in code. Settings are saved in `static/logo/branding.json`; if it is missing or damaged the defaults are used.

- **Header logo** and **footer logo**: upload a PNG, JPG, WEBP, GIF or **SVG** (max 5 MB). Set the height (desktop and phone separately) and the alignment (left / center / right). The footer logo can also be shown plain, on a soft plate or on white (useful for dark logos), or left empty to reuse the header logo. Files are saved as `static/logo/header.<ext>` and `static/logo/footer.<ext>`; your own files in that folder are never touched. With no setting at all, the old behaviour (a logo picked from `static/logo/`) still applies.
- **Hero picture** (`static/images/brand-slide.<ext>`, default `static/webinar.jpg`): fit (cover/contain), focus point (x / y), zoom (100 to 300%), shape (rounded / square / circle), ratio (3:2, 4:3, 16:9, 1:1, 4:5), side (left/right) and tilt. Every control has a live preview next to it.
- **Hero text**: badge, headline (with one highlighted word), paragraph and the two button labels. A blank headline falls back to the default.
- Replaced images show up immediately (their URLs carry a version stamp, so browsers never keep the old one).
- **SVG rules** (an SVG can carry code, so it is checked before saving): UTF-8 only, max 1 MB, must be a real `<svg>` with a `viewBox` (or width and height). Scripts, `foreignObject`, animation, `on...` handlers, external references, DOCTYPE/ENTITY and CSS `@import` / external `url()` are refused with the reason shown. Images embedded as `data:` (PNG/JPG/GIF/WEBP) are fine. SVGs are also served with a locked-down security header.

Safety notes:

- Login is rate-limited (5 wrong passwords = 5 minute lock) and sessions expire after 8 hours idle. Every action needs a CSRF token.
- Curriculum import reads `.xlsx` with the Python standard library only (no new packages). Max 2 MB, 2000 rows; files with DTDs/entities are refused. Old `.xls` is not supported: save as `.xlsx` or CSV.
- Uploaded thumbnails are checked by their real content (JPG/PNG/WEBP/GIF only, max 5 MB); the file name you upload is ignored.
- The folder name (slug) is fixed after creation. Editing a course keeps any extra keys you added to `meta.json` by hand.
- The pipeline and FAQ lists are not editable here; they live in `app.py`.
- If you deploy this publicly: use HTTPS, turn off `debug=True` in the last line of `app.py`, and set `ADMIN_PASSWORD` / `SECRET_KEY` as environment variables.
