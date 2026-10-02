import os
import sqlite3
import hashlib
import json
from datetime import datetime, timedelta, timezone
from functools import wraps
import requests
from flask import Flask, request, jsonify, session, redirect, render_template_string, Response
import csv
import io

app = Flask(__name__)

# ==========================================
# CONFIGURATION
# ==========================================
app.secret_key = os.getenv("SESSION_SECRET", "growthcrm_production_key_2026")
DB = os.getenv("DATABASE_PATH", "customer_growth.db")
SELAR_PRODUCT_URL = os.getenv("SELAR_PRODUCT_URL", "https://selar.com/9u69r59d57")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

CREATOR_EMAIL = os.getenv("CREATOR_EMAIL", "dayotimileyin831@gmail.com")
CREATOR_PASSWORD = os.getenv("CREATOR_PASSWORD", "CHANGE_THIS_PASSWORD")

PLANS = {
    "monthly": {"name": "1 Month", "price": 19, "days": 31},
    "six_months": {"name": "6 Months", "price": 79, "days": 183},
    "yearly": {"name": "1 Year", "price": 149, "days": 365}
}

# ==========================================
# DATABASE SETUP
# ==========================================
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        business TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        plan TEXT NOT NULL,
        status TEXT NOT NULL,
        access_code TEXT UNIQUE NOT NULL,
        starts_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        payment_reference TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        status TEXT DEFAULT 'New',
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        client_name TEXT NOT NULL,
        date_time TEXT NOT NULL,
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS business_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        currency TEXT DEFAULT 'USD',
        tax_rate REAL DEFAULT 0.0,
        invoice_footer TEXT,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS webhook_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT UNIQUE NOT NULL,
        payload TEXT,
        received_at TEXT NOT NULL
    );
    """)
    conn.commit()
    conn.close()

init_db()

# ==========================================
# HELPERS & DECORATORS
# ==========================================
def now_text():
    return datetime.now(timezone.utc).isoformat()

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def generate_access_code():
    return "CRM-" + hashlib.sha256(os.urandom(16)).hexdigest()[:10].upper()

def current_user():
    if "user_id" not in session:
        return None
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    return user

def active_subscription(user_id):
    conn = db()
    sub = conn.execute(
        "SELECT * FROM subscriptions WHERE user_id = ? AND status = 'active' AND expires_at > ? ORDER BY id DESC LIMIT 1",
        (user_id, now_text())
    ).fetchone()
    conn.close()
    return sub

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper

def paid_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect("/login")
        if user["email"] == CREATOR_EMAIL:
            return f(*args, **kwargs)
        sub = active_subscription(user["id"])
        if not sub:
            return redirect("/plans")
        return f(*args, **kwargs)
    return wrapper

# ==========================================
# ROUTES
# ==========================================

@app.route("/")
def home():
    return render_template_string(LANDING_PAGE_HTML, plans=PLANS, selar_url=SELAR_PRODUCT_URL)

@app.route("/plans")
@login_required
def plans():
    return render_template_string(PLANS_HTML, plans=PLANS, selar_url=SELAR_PRODUCT_URL)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name")
        business = request.form.get("business")
        email = request.form.get("email")
        password = hash_password(request.form.get("password"))
        
        conn = db()
        try:
            conn.execute("INSERT INTO users (name, business, email, password, created_at) VALUES (?, ?, ?, ?, ?)",
                         (name, business, email, password, now_text()))
            user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO business_settings (user_id, currency, tax_rate, invoice_footer) VALUES (?, 'USD', 0.0, ?)",
                         (user_id, f"Thank you for doing business with {business}!"))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return "Email already registered. <a href='/login'>Login here</a>"
        
        conn.close()
        session["user_id"] = user_id
        return redirect("/dashboard")
    return render_template_string(REGISTER_HTML)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password_raw = request.form.get("password")
        
        if email == CREATOR_EMAIL and password_raw == CREATOR_PASSWORD:
            session["creator"] = True
            return redirect("/creator")
            
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE email = ? AND password = ?", (email, hash_password(password_raw))).fetchone()
        conn.close()
        if user:
            session["user_id"] = user["id"]
            return redirect("/dashboard")
        return "Invalid credentials. <a href='/login'>Try again</a>"
    return render_template_string(LOGIN_HTML)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/dashboard")
@paid_required
def dashboard():
    user = current_user()
    sub = active_subscription(user["id"]) if user["email"] != CREATOR_EMAIL else None
    
    conn = db()
    leads = conn.execute("SELECT * FROM leads WHERE user_id = ? ORDER BY id DESC", (user["id"],)).fetchall()
    appointments = conn.execute("SELECT * FROM appointments WHERE user_id = ? ORDER BY date_time ASC", (user["id"],)).fetchall()
    settings = conn.execute("SELECT * FROM business_settings WHERE user_id = ?", (user["id"],)).fetchone()
    conn.close()
    
    total_leads = len(leads)
    new_leads = sum(1 for l in leads if l["status"] == "New")
    closed_deals = sum(1 for l in leads if l["status"] == "Closed")
    
    return render_template_string(DASHBOARD_HTML, user=user, sub=sub, leads=leads, 
                                  appointments=appointments, settings=settings,
                                  total_leads=total_leads, new_leads=new_leads, closed_deals=closed_deals)

@app.route("/leads/add", methods=["POST"])
@paid_required
def add_lead():
    user = current_user()
    conn = db()
    conn.execute("INSERT INTO leads (user_id, name, phone, email, status, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (user["id"], request.form.get("name"), request.form.get("phone"), request.form.get("email"), 
                  request.form.get("status", "New"), request.form.get("notes"), now_text()))
    conn.commit()
    conn.close()
    return redirect("/dashboard")

@app.route("/leads/delete/<int:lead_id>", methods=["POST"])
@paid_required
def delete_lead(lead_id):
    user = current_user()
    conn = db()
    conn.execute("DELETE FROM leads WHERE id = ? AND user_id = ?", (lead_id, user["id"]))
    conn.commit()
    conn.close()
    return redirect("/dashboard")

@app.route("/appointments/add", methods=["POST"])
@paid_required
def add_appointment():
    user = current_user()
    conn = db()
    conn.execute("INSERT INTO appointments (user_id, client_name, date_time, notes, created_at) VALUES (?, ?, ?, ?, ?)",
                 (user["id"], request.form.get("client_name"), request.form.get("date_time"), request.form.get("notes"), now_text()))
    conn.commit()
    conn.close()
    return redirect("/dashboard")

@app.route("/settings/update", methods=["POST"])
@paid_required
def update_settings():
    user = current_user()
    conn = db()
    conn.execute("UPDATE business_settings SET currency = ?, tax_rate = ?, invoice_footer = ? WHERE user_id = ?",
                 (request.form.get("currency"), request.form.get("tax_rate", 0.0), request.form.get("invoice_footer"), user["id"]))
    conn.commit()
    conn.close()
    return redirect("/dashboard")

@app.route("/export/csv")
@paid_required
def export_csv():
    user = current_user()
    conn = db()
    leads = conn.execute("SELECT name, phone, email, status, notes, created_at FROM leads WHERE user_id = ?", (user["id"],)).fetchall()
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name", "Phone", "Email", "Status", "Notes", "Created At"])
    for l in leads:
        writer.writerow([l["name"], l["phone"], l["email"], l["status"], l["notes"], l["created_at"]])
    
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=leads_export.csv"})

@app.route("/ai-reply", methods=["POST"])
@paid_required
def ai_reply():
    data = request.get_json() or {}
    prompt = data.get("prompt", "Write a professional follow-up message to a client.")
    
    if not GEMINI_API_KEY:
        return jsonify({"reply": "AI service is not configured with an API key."})
        
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}
        payload = {"contents": [{"parts": [{"text": f"Act as a professional CRM assistant. {prompt}"}]}]}
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        res_data = res.json()
        reply = res_data["candidates"][0]["content"]["parts"][0]["text"]
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"reply": f"Error communicating with AI: {str(e)}"})

@app.route("/activate", methods=["POST"])
@login_required
def activate_subscription():
    user = current_user()
    code = request.form.get("access_code", "").strip()
    
    conn = db()
    sub = conn.execute("SELECT * FROM subscriptions WHERE access_code = ? AND (user_id IS NULL OR user_id = ?)", (code, user["id"])).fetchone()
    if sub:
        conn.execute("UPDATE subscriptions SET user_id = ?, status = 'active' WHERE id = ?", (user["id"], sub["id"]))
        conn.commit()
        conn.close()
        return redirect("/dashboard")
    conn.close()
    return "Invalid access code. <a href='/plans'>Back to Plans</a>"

@app.route("/api/webhooks/selar", methods=["POST"])
def selar_webhook():
    payload = request.get_json(silent=True) or {}
    event_id = str(payload.get("event_id") or payload.get("order_id") or os.urandom(8).hex())
    
    conn = db()
    already = conn.execute("SELECT id FROM webhook_events WHERE event_key = ?", (event_id,)).fetchone()
    if already:
        conn.close()
        return jsonify({"ok": True, "duplicate": True})
        
    conn.execute("INSERT INTO webhook_events (event_key, payload, received_at) VALUES (?, ?, ?)",
                 (event_id, json.dumps(payload), now_text()))
    
    email = payload.get("customer_email") or payload.get("email")
    plan_key = payload.get("product_key", "monthly")
    ref = payload.get("order_id") or payload.get("reference")
    
    plan_info = PLANS.get(plan_key, PLANS["monthly"])
    days = plan_info["days"]
    
    user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    user_id = user["id"] if user else None
    
    access_code = generate_access_code()
    starts_at = now_text()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    
    conn.execute(
        "INSERT INTO subscriptions (user_id, plan, status, access_code, starts_at, expires_at, payment_reference) VALUES (?, ?, 'active', ?, ?, ?, ?)",
        (user_id, plan_key, access_code, starts_at, expires_at, ref)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "access_code": access_code})

@app.route("/creator")
def creator_login():
    if session.get("creator"):
        conn = db()
        users = conn.execute("SELECT u.*, s.plan, s.status, s.access_code, s.expires_at FROM users u LEFT JOIN subscriptions s ON s.user_id = u.id").fetchall()
        conn.close()
        return render_template_string(CREATOR_HTML, users=users)
    return redirect("/login")


# ==========================================
# HTML TEMPLATES
# ==========================================

LANDING_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><title>GrowthCRM - SaaS Lead Pipeline & AI Assistant</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 font-sans">
    <header class="border-b border-slate-800 p-6 flex justify-between items-center max-w-6xl mx-auto">
        <h1 class="text-xl font-bold tracking-wide text-emerald-400">GrowthCRM</h1>
        <div class="space-x-4">
            <a href="/login" class="text-slate-300 hover:text-white">Login</a>
            <a href="/register" class="bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-semibold px-4 py-2 rounded-lg">Get Started</a>
        </div>
    </header>
    <main class="max-w-5xl mx-auto px-6 py-16 text-center">
        <h2 class="text-4xl md:text-6xl font-extrabold tracking-tight mb-6">Convert More Leads With <span class="text-emerald-400">AI & Automated CRM</span></h2>
        <p class="text-slate-400 text-lg max-w-2xl mx-auto mb-10">Manage pipelines, appointments, follow-ups, and generate high-converting client responses instantly using Gemini AI.</p>
        <a href="/register" class="bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold px-8 py-4 rounded-xl text-lg shadow-lg shadow-emerald-500/20">Start Your Free Trial</a>
        
        <div class="mt-24 grid md:grid-cols-3 gap-8 text-left">
            {% for key, plan in plans.items() %}
            <div class="border border-slate-800 bg-slate-900/50 p-8 rounded-2xl flex flex-col justify-between">
                <div>
                    <h3 class="text-xl font-bold mb-2">{{ plan.name }}</h3>
                    <div class="text-3xl font-extrabold text-emerald-400 mb-4">${{ plan.price }}</div>
                    <p class="text-slate-400 text-sm mb-6">Full access to CRM, lead pipeline, reports, AI assistant, and appointments.</p>
                </div>
                <a href="{{ selar_url }}" target="_blank" class="block text-center bg-slate-800 hover:bg-emerald-500 hover:text-slate-950 transition font-semibold py-3 rounded-xl">Purchase via Selar</a>
            </div>
            {% endfor %}
        </div>
    </main>
</body>
</html>
"""

REGISTER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Register - GrowthCRM</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-950 text-slate-100 flex items-center justify-center min-h-screen">
    <div class="bg-slate-900 border border-slate-800 p-8 rounded-2xl w-full max-w-md shadow-xl">
        <h2 class="text-2xl font-bold mb-6 text-emerald-400 text-center">Create Your Account</h2>
        <form method="POST" class="space-y-4">
            <div><label class="text-sm text-slate-400">Full Name</label><input type="text" name="name" required class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg mt-1"></div>
            <div><label class="text-sm text-slate-400">Business Name</label><input type="text" name="business" required class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg mt-1"></div>
            <div><label class="text-sm text-slate-400">Email Address</label><input type="email" name="email" required class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg mt-1"></div>
            <div><label class="text-sm text-slate-400">Password</label><input type="password" name="password" required class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg mt-1"></div>
            <button type="submit" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold p-3 rounded-lg mt-4">Register</button>
        </form>
        <p class="text-center text-sm text-slate-400 mt-6">Already have an account? <a href="/login" class="text-emerald-400">Login</a></p>
    </div>
</body>
</html>
"""

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Login - GrowthCRM</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-950 text-slate-100 flex items-center justify-center min-h-screen">
    <div class="bg-slate-900 border border-slate-800 p-8 rounded-2xl w-full max-w-md shadow-xl">
        <h2 class="text-2xl font-bold mb-6 text-emerald-400 text-center">Welcome Back</h2>
        <form method="POST" class="space-y-4">
            <div><label class="text-sm text-slate-400">Email Address</label><input type="email" name="email" required class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg mt-1"></div>
            <div><label class="text-sm text-slate-400">Password</label><input type="password" name="password" required class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg mt-1"></div>
            <button type="submit" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold p-3 rounded-lg mt-4">Login</button>
        </form>
        <p class="text-center text-sm text-slate-400 mt-6">Don't have an account? <a href="/register" class="text-emerald-400">Register</a></p>
    </div>
</body>
</html>
"""

PLANS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Activate Subscription</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-950 text-slate-100 p-8">
    <div class="max-w-2xl mx-auto bg-slate-900 border border-slate-800 p-8 rounded-2xl">
        <h2 class="text-2xl font-bold text-emerald-400 mb-4">Subscription Required</h2>
        <p class="text-slate-400 mb-6">Please purchase your plan on Selar to activate full access to your CRM pipeline, appointments, and AI tools.</p>
        <a href="{{ selar_url }}" target="_blank" class="block text-center bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-3 rounded-xl mb-8">Pay via Selar</a>
        
        <form action="/activate" method="POST" class="border-t border-slate-800 pt-6">
            <label class="text-sm text-slate-400 block mb-2">Have an access code from Selar? Enter it here:</label>
            <div class="flex gap-2">
                <input type="text" name="access_code" placeholder="CRM-XXXXXXXXXX
