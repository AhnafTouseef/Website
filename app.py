from flask import Flask, render_template, request, redirect, url_for, flash
import os
import random

app = Flask(__name__)
app.secret_key = "change-me-to-a-secure-key"

active_course = {
    "title": "Python for Beginners: From Zero to Real Projects",
    "subtitle": "A hands-on 8-week program built for students who want practical skills fast.",
    "duration": "8 Weeks",
    "capacity": "Max 30 Students",
    "certificate": "Certificate",
    "price": "Tk 500.00",
    "status": "Available Now",
    "tagline": "Enrolling Now · Cohort 1",
    "start_date": "July 21, 2024",
    "features": [
        "8 weeks live sessions",
        "Direct instructor access",
        "Final project feedback",
        "Community access"
    ]
}

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

team_members = [
    {
        "name": "Ahnaf Tauseef Khan",
        "role": "Co-Founder & Lead Instructor",
        "bio": "B.Sc. Petroleum and Mining Engineering — Jashore University of Science and Technology. Self taught developer who built Python pipelines and automation systems.",
        "avatar": "R"
    },
    {
        "name": "Sadman Sakib Shumon",
        "role": "Co-Founder & Co-Instructor",
        "bio": "B.Sc. Petroleum and Mining Engineering — Jashore University of Science and Technology. Experienced in data analytics consulting and practical reporting workflows.",
        "avatar": "D"
    }
]


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

curriculum = [
    {"Link": "https://forms.gle/eRr2Sk4pQzuaT9U86"},
    {"week_range": "Week 1–2: Python Basics", 
     "details": ["Variables & data types", "Control flow", "Functions", "Debugging"]},

    {"week_range": "Week 3–4: Data Structures", "details": ["Lists, dictionaries, sets", "Working with text", "Organizing code"]},

    {"week_range": "Week 5–6: Libraries & Practical Tools", "details": ["Pandas for data", "File handling", "Simple automation scripts"]},

    {"week_range": "Week 7–8: Final Project", "details": ["Build your portfolio project", "Present your solution", "Get feedback from instructors"]}
]

@app.route("/")
def home():
    return render_template(
        "home.html",
        active_course=active_course,
        pipeline_courses=pipeline_courses,
        show_banner=True
    )

@app.route("/courses")
def courses():
    return render_template(
        "courses.html",
        active_course=active_course,
        pipeline_courses=pipeline_courses
    )

@app.route("/course")
def course_details():
    return render_template(
        "course_details.html",
        active_course=active_course,
        curriculum=curriculum,
        team_members=team_members
    )

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
    return render_template("about.html", team_members=team_members)

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

if __name__ == "__main__":
    app.run(debug=True, port=5001)
