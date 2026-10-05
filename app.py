import os
import sqlite3
import hashlib
import json
import csv
import io
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, render_template_string, request, redirect, url_for, session, Response, jsonify
import requests

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-key-change-this")

CREATOR_EMAIL = "timileyinwebspecialist@gmail.com"
CREATOR_PASSWORD = "adminpassword123"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-1.5-flash"

SELAR_PRODUCT_URL = "https://selar.com/9u69r59d57"

PLANS = {
    "monthly": {"name": "1 Month Plan", "price": 19, "days": 30},
    "six_months": {"name": "6 Month Plan", "price": 79, "days": 180},
    "yearly": {"name": "1 Year Plan", "price": 149, "days": 365}
}

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

def db():
    if TURSO_DATABASE_URL and TURSO_AUTH_TOKEN:
        import libsql_experimental as libsql
        url = TURSO_DATABASE_URL.replace("libsql://", "https://")
        return libsql.connect(database=url, auth_token=TURSO_AUTH_TOKEN)
    else:
        conn = sqlite3.connect("database.db")
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    conn = db()
    if hasattr(conn, "execute") and not hasattr(conn, "cursor"):
        statements = [
            """CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, business TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, created_at TEXT NOT NULL);""",
            """CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT NOT NULL, status TEXT NOT NULL, access_code TEXT UNIQUE NOT NULL, starts_at TEXT NOT NULL, expires_at TEXT NOT NULL, payment_reference TEXT, FOREIGN KEY(user_id) REFERENCES users(id));""",
            """CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL, phone TEXT, email TEXT, status TEXT DEFAULT 'New', notes TEXT, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));""",
            """CREATE TABLE IF NOT EXISTS appointments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, client_name TEXT NOT NULL, date_time TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));""",
            """CREATE TABLE IF NOT EXISTS business_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE NOT NULL, currency TEXT DEFAULT 'USD', tax_rate REAL DEFAULT 0.0, invoice_footer TEXT, FOREIGN KEY(user_id) REFERENCES users(id));""",
            """CREATE TABLE IF NOT EXISTS webhook_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL, payload TEXT, received_at TEXT NOT NULL);"""
        ]
        for stmt in statements:
            conn.execute(stmt)
    else:
        with conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, business TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS subscriptions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan TEXT NOT NULL, status TEXT NOT NULL, access_code TEXT UNIQUE NOT NULL, starts_at TEXT NOT NULL, expires_at TEXT NOT NULL, payment_reference TEXT, FOREIGN KEY(user_id) REFERENCES users(id));
                CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL, phone TEXT, email TEXT, status TEXT DEFAULT 'New', notes TEXT, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
                CREATE TABLE IF NOT EXISTS appointments (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, client_name TEXT NOT NULL, date_time TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
                CREATE TABLE IF NOT EXISTS business_settings (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE NOT NULL, currency TEXT DEFAULT 'USD', tax_rate REAL DEFAULT 0.0, invoice_footer TEXT, FOREIGN KEY(user_id) REFERENCES users(id));
                CREATE TABLE IF NOT EXISTS webhook_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL, payload TEXT, received_at TEXT NOT NULL);
            """)

init_db()

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
    sub = conn.execute("SELECT * FROM subscriptions WHERE user_id = ? AND status = 'active' AND expires_at > ? ORDER BY id DESC LIMIT 1", (user_id, now_text())).fetchone()
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
@app.route("/")    
def home():
    return render_template_string(LANDING_PAGE_HTML, plans=PLANS, selar_url=SELAR_PRODUCT_URL)

@app.route("/plans")
@login_required
def plans():
    return render_template_string(
        PLANS_HTML, 
        plans=PLANS, 
        monthly_url="https://selar.com/9u69r59d57",
        semiannual_url="https://selar.com/011h9610d1",
        yearly_url="https://selar.com/x952197d1u"
    )

@app.route("/welcome")
def welcome():
    new_code = generate_access_code()
    plan_type = request.args.get("plan", "monthly")

    if plan_type == "yearly":
        plan_name = "Annual Plan"
        expires = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
    elif plan_type == "semiannual":
        plan_name = "6-Month Plan"
        expires = (datetime.now() + timedelta(days=180)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        plan_name = "Monthly Plan"
        expires = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    conn = db()
    conn.execute(
        "INSERT INTO subscriptions (user_id, plan, status, access_code, starts_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (None, plan_name, "active", new_code, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), expires)
    )
    conn.commit()
    conn.close()

    return render_template_string(WELCOME_HTML, code=new_code, plan=plan_name)

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    if request.method == "POST":
        name = request.form.get("name")
        business = request.form.get("business")
        email = request.form.get("email")
        password_raw = request.form.get("password")
        access_code = request.form.get("access_code").strip()
        
        conn = db()
        sub = conn.execute("SELECT * FROM subscriptions WHERE access_code = ?", (access_code,)).fetchone()
        if not sub:
            conn.close()
            error = "Invalid access code. Please check your Selar purchase receipt."
            return render_template_string(REGISTER_HTML, error=error)
            
        hashed_pw = hash_password(password_raw)
        
        try:
            conn.execute("INSERT INTO users (name, business, email, password, created_at) VALUES (?, ?, ?, ?, ?)",
                         (name, business, email, hashed_pw, now_text()))
            conn.commit()
            user_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            
            conn.execute("UPDATE subscriptions SET user_id = ? WHERE access_code = ?", (user_id, access_code))
            conn.commit()
            conn.close()
            
            session["user_id"] = user_id
            session["user_name"] = name
            return redirect(url_for("dashboard"))
            
        except sqlite3.IntegrityError:
            conn.close()
            error = "This email is already registered. Please login instead."
            
    return render_template_string(REGISTER_HTML, error=error)

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")
        
        if email == CREATOR_EMAIL and password == CREATOR_PASSWORD:
            session["creator"] = True
            return redirect(url_for("creator_login"))
            
        conn = db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        
        if not user:
            error = "This account doesn't exist. Please purchase a plan and register with your access code."
        elif user["password"] != password:
            error = "Incorrect password. Please try again."
        else:
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            conn.close()
            return redirect(url_for("dashboard"))
            
        conn.close()
            
    return render_template_string(LOGIN_HTML, error=error)

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
        
        if "candidates" in res_data and res_data["candidates"]:
            reply = res_data["candidates"][0]["content"]["parts"][0]["text"]
        else:
            reply = "Error: Model returned an unexpected response format."
            
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
    LANDING_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>GrowthCRM - SaaS Lead Pipeline & AI Assistant</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-950 text-slate-100 font-sans">
    <header class="border-b border-slate-800 p-6 flex justify-between items-center max-w-6xl mx-auto">
        <h1 class="text-xl font-bold tracking-wide text-emerald-400">GrowthCRM</h1>
        <div class="space-x-4">
            <a href="/login" class="text-slate-300 hover:text-white text-sm">Login</a>
            <a href="/register" class="bg-slate-800 hover:bg-slate-700 text-white font-semibold px-4 py-2 rounded-lg text-sm">Register with Code</a>
        </div>
    </header>
    <main class="max-w-5xl mx-auto px-6 py-16 text-center">
        <h2 class="text-4xl md:text-6xl font-extrabold tracking-tight mb-6">Convert More Leads With <span class="text-emerald-400">AI & Automated CRM</span></h2>
        <p class="text-slate-400 text-lg max-w-2xl mx-auto mb-6">Manage pipelines, appointments, follow-ups, and generate high-converting client responses instantly using Gemini AI.</p>
        <div class="inline-block bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 px-4 py-2 rounded-xl text-sm font-medium mb-12">
             Premium Software: Choose a plan below, purchase via Selar, and use your access code to register.
        </div>
        
        <div class="grid md:grid-cols-3 gap-8 text-left">
            {% for key, plan in plans.items() %}
            <div class="border border-slate-800 bg-slate-900/50 p-8 rounded-2xl flex flex-col justify-between">
                <div>
                    <h3 class="text-xl font-bold mb-2">{{ plan.name }}</h3>
                    <p class="text-slate-400 text-sm mb-4">Full access to CRM, lead pipeline, reports, AI assistant, and appointments.</p>
                    <p class="text-2xl font-bold text-white mb-6">${{ plan.price }}</p>
                </div>
                {% if key == 'monthly' %}
                <a href="https://selar.com/9u69r59d57" target="_blank" class="block text-center bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2 rounded-lg">Buy Now via Selar</a>
                {% elif key == 'six_months' %}
                <a href="https://selar.com/011h9610d1" target="_blank" class="block text-center bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2 rounded-lg">Buy Now via Selar</a>
                {% elif key == 'yearly' %}
                <a href="https://selar.com/x952197d1u" target="_blank" class="block text-center bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2 rounded-lg">Buy Now via Selar</a>
                {% endif %}
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
<body class="bg-slate-950 text-slate-100 font-sans flex items-center justify-center min-h-screen py-10">
    <div class="bg-slate-900 border border-slate-800 p-8 rounded-2xl w-full max-w-md shadow-xl">
        <h2 class="text-2xl font-bold mb-2 text-emerald-400 text-center">Activate Your Account</h2>
        <p class="text-slate-400 text-sm text-center mb-6">Enter your details and the access code received after your Selar purchase.</p>
        
        {% if error %}
        <div class="bg-red-500/10 border border-red-500/30 text-red-400 p-3 rounded-xl text-sm mb-6 text-center">
            {{ error }}
        </div>
        {% endif %}

        <form method="POST" class="space-y-4">
            <div>
                <label class="block text-sm text-slate-400 mb-1">Full Name</label>
                <input type="text" name="name" required class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-2.5 text-white focus:outline-none focus:border-emerald-500">
            </div>
            <div>
                <label class="block text-sm text-slate-400 mb-1">Business Name</label>
                <input type="text" name="business" required class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-2.5 text-white focus:outline-none focus:border-emerald-500">
            </div>
            <div>
                <label class="block text-sm text-slate-400 mb-1">Email Address</label>
                <input type="email" name="email" required class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-2.5 text-white focus:outline-none focus:border-emerald-500">
            </div>
            <div>
                <label class="block text-sm text-slate-400 mb-1">Password</label>
                <input type="password" name="password" required class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-2.5 text-white focus:outline-none focus:border-emerald-500">
            </div>
            <div>
                <label class="block text-sm text-slate-400 mb-1">Selar Access Code</label>
                <input type="text" name="access_code" required placeholder="e.g. CRM-XXXXXX" class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-2.5 text-white focus:outline-none focus:border-emerald-500">
            </div>
            <button type="submit" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-3 rounded-xl transition">Complete Registration</button>
        </form>
        <p class="text-center text-sm text-slate-400 mt-6">Already registered? <a href="/login" class="text-emerald-400 hover:underline">Login here</a></p>
    </div>
</body>
</html>
"""

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Login - GrowthCRM</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-950 text-slate-100 font-sans flex items-center justify-center h-screen">
    <div class="bg-slate-900 border border-slate-800 p-8 rounded-2xl w-full max-w-md shadow-xl">
        <h2 class="text-2xl font-bold mb-6 text-emerald-400 text-center">Login to GrowthCRM</h2>
        
        {% if error %}
        <div class="bg-red-500/10 border border-red-500/30 text-red-400 p-3 rounded-xl text-sm mb-6 text-center">
            {{ error }}
        </div>
        {% endif %}

        <form method="POST" class="space-y-4">
            <div>
                <label class="block text-sm text-slate-400 mb-1">Email Address</label>
                <input type="email" name="email" required class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-2.5 text-white focus:outline-none focus:border-emerald-500">
            </div>
            <div>
                <label class="block text-sm text-slate-400 mb-1">Password</label>
                <input type="password" name="password" required class="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-2.5 text-white focus:outline-none focus:border-emerald-500">
            </div>
            <button type="submit" class="w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-3 rounded-xl transition">Login</button>
        </form>
        <p class="text-center text-sm text-slate-400 mt-6">Don&#39;t have an access code? <a href="/" class="text-emerald-400 hover:underline">Purchase a plan</a></p>
    </div>
</body>
</html>
"""

PLANS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><title>Activate Subscription</title><script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 p-8">
    <div class="max-w-5xl mx-auto bg-slate-900 border border-slate-800 p-8 rounded-2xl">
        <h2 class="text-2xl font-bold text-emerald-400 mb-2">Subscription Required</h2>
        <p class="text-slate-400 mb-8">Please choose your plan below to unlock full access to your CRM pipeline.</p>
        
        <div class="grid md:grid-cols-3 gap-6 mb-8">
            <div class="bg-slate-950 border border-slate-800 p-6 rounded-xl flex flex-col justify-between">
                <div>
                    <h3 class="text-xl font-bold text-emerald-400 mb-2">1 Month</h3>
                    <p class="text-slate-400 text-sm mb-4">Full access to CRM, lead pipeline, reports, AI assistant, and appointments.</p>
                    <p class="text-2xl font-bold text-white mb-6">$19</p>
                </div>
                <a href="https://selar.com/9u69r59d57" target="_blank" class="block text-center bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2 rounded-lg">Buy Now via Selar</a>
            </div>

            <div class="bg-slate-950 border border-slate-800 p-6 rounded-xl flex flex-col justify-between">
                <div>
                    <h3 class="text-xl font-bold text-emerald-400 mb-2">6 Months</h3>
                    <p class="text-slate-400 text-sm mb-4">Full access to CRM, lead pipeline, reports, AI assistant, and appointments.</p>
                    <p class="text-2xl font-bold text-white mb-6">$79</p>
                </div>
                <a href="https://selar.com/011h9610d1" target="_blank" class="block text-center bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2 rounded-lg">Buy Now via Selar</a>
            </div>

            <div class="bg-slate-950 border border-slate-800 p-6 rounded-xl flex flex-col justify-between">
                <div>
                    <h3 class="text-xl font-bold text-emerald-400 mb-2">1 Year</h3>
                    <p class="text-slate-400 text-sm mb-4">Full access to CRM, lead pipeline, reports, AI assistant, and appointments.</p>
                    <p class="text-2xl font-bold text-white mb-6">$149</p>
                </div>
                <a href="https://selar.com/x952197d1u" target="_blank" class="block text-center bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-2 rounded-lg">Buy Now via Selar</a>
            </div>
        </div>

        <form action="/activate" method="POST" class="border-t border-slate-800 pt-6">
            <label class="text-sm text-slate-400 block mb-2">Have an access code from Selar? Enter it here:</label>
            <div class="flex gap-2">
                <input type="text" name="access_code" placeholder="CRW-XXXXXXXX" required class="flex-1 bg-slate-950 border border-slate-800 rounded-lg px-4 py-2 text-white">
                <button type="submit" class="bg-slate-800 hover:bg-slate-700 px-6 py-2 rounded-lg font-semibold text-slate-200">Activate Code</button>
            </div>
        </form>
    </div>
</body>
</html>
"""

WELCOME_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Welcome - GrowthCRM</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-950 text-slate-100 font-sans flex items-center justify-center min-h-screen">
    <div class="bg-slate-900 border border-slate-800 p-8 rounded-2xl w-full max-w-lg text-center shadow-xl">
        <h2 class="text-2xl font-bold text-emerald-400 mb-2">Payment Successful!</h2>
        <p class="text-slate-400 text-sm mb-6">Thank you for purchasing the <strong>{{ plan }}</strong>.</p>
        <div class="bg-slate-950 border border-slate-800 p-4 rounded-xl mb-6">
            <p class="text-xs text-slate-400 mb-1">Your Access Code:</p>
            <p class="text-xl font-mono text-emerald-400 font-bold">{{ code }}</p>
        </div>
        <p class="text-xs text-slate-400 mb-6">Please copy this code. You will need it to register or activate your account.</p>
        <a href="/register" class="block w-full bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold py-3 rounded-xl transition">Proceed to Registration</a>
    </div>
</body>
</html>
"""
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><title>Dashboard - GrowthCRM</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100">
    <div class="flex min-h-screen">
        <aside class="w-64 border-r border-slate-800 p-6 flex flex-col justify-between hidden md:flex">
            <div>
                <h1 class="text-xl font-bold text-emerald-400 mb-8">GrowthCRM</h1>
                <nav class="space-y-2 text-sm">
                    <a href="/dashboard" class="block px-4 py-2 rounded-lg bg-slate-800 text-white font-medium">Dashboard & Leads</a>
                    <a href="/export/csv" class="block px-4 py-2 rounded-lg text-slate-400 hover:bg-slate-900">Export CSV</a>
                    <a href="/plans" class="block px-4 py-2 rounded-lg text-slate-400 hover:bg-slate-900">Subscription</a>
                </nav>
            </div>
            <a href="/logout" class="text-red-400 hover:text-red-300 text-sm">Logout</a>
        </aside>

        <main class="flex-1 p-8 overflow-y-auto">
            <div class="flex justify-between items-center mb-8">
                <div>
                    <h2 class="text-2xl font-bold">Welcome, {{ user.name }}</h2>
                    <p class="text-slate-400 text-sm">Business: {{ user.business }} {% if sub %}| Subscription Active{% endif %}</p>
                </div>
                <div class="flex gap-4 items-center">
                    <a href="/export/csv" class="bg-slate-800 hover:bg-slate-700 text-xs px-3 py-2 rounded-lg font-semibold md:hidden">Export CSV</a>
                    <a href="/logout" class="text-red-400 text-sm">Logout</a>
                </div>
            </div>

            <div class="grid md:grid-cols-3 gap-6 mb-8">
                <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl">
                    <p class="text-slate-400 text-sm">Total Leads</p>
                    <p class="text-3xl font-extrabold mt-2">{{ total_leads }}</p>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl">
                    <p class="text-slate-400 text-sm">New Leads</p>
                    <p class="text-3xl font-extrabold mt-2 text-blue-400">{{ new_leads }}</p>
                </div>
                <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl">
                    <p class="text-slate-400 text-sm">Closed Deals</p>
                    <p class="text-3xl font-extrabold mt-2 text-emerald-400">{{ closed_deals }}</p>
                </div>
            </div>

            <div class="grid lg:grid-cols-2 gap-8 mb-8">
                <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl">
                    <h3 class="text-lg font-bold mb-4">Add New Lead</h3>
                    <form action="/leads/add" method="POST" class="space-y-4">
                        <div class="grid grid-cols-2 gap-4">
                            <input type="text" name="name" placeholder="Client Name" required class="bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm">
                            <input type="text" name="phone" placeholder="Phone Number" class="bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm">
                        </div>
                        <div class="grid grid-cols-2 gap-4">
                            <input type="email" name="email" placeholder="Email Address" class="bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm">
                            <select name="status" class="bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm">
                                <option value="New">New</option>
                                <option value="Contacted">Contacted</option>
                                <option value="Closed">Closed</option>
                            </select>
                        </div>
                        <textarea name="notes" placeholder="Notes..." class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm h-20"></textarea>
                        <button type="submit" class="bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold px-4 py-2 rounded-lg text-sm">Add Lead</button>
                    </form>
                </div>

                <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl flex flex-col justify-between">
                    <div>
                        <h3 class="text-lg font-bold mb-4">AI Reply Assistant (Gemini)</h3>
                        <textarea id="aiPrompt" placeholder="Ask AI to write a follow-up email or message..." class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm mb-4 h-20"></textarea>
                        <button onclick="generateAI()" class="bg-blue-600 hover:bg-blue-700 text-white font-bold px-4 py-2 rounded-lg text-sm">Generate AI Response</button>
                    </div>
                    <div id="aiResult" class="mt-4 p-3 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-300 min-h-[50px]"></div>
                </div>
            </div>

            <div class="grid lg:grid-cols-2 gap-8 mb-8">
                <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl">
                    <h3 class="text-lg font-bold mb-4">Schedule Appointment</h3>
                    <form action="/appointments/add" method="POST" class="space-y-4">
                        <input type="text" name="client_name" placeholder="Client Name" required class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm">
                        <input type="datetime-local" name="date_time" required class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm">
                        <textarea name="notes" placeholder="Appointment agenda..." class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm h-16"></textarea>
                        <button type="submit" class="bg-emerald-500 hover:bg-emerald-600 text-slate-950 font-bold px-4 py-2 rounded-lg text-sm">Save Appointment</button>
                    </form>
                </div>

                <div class="bg-slate-900 border border-slate-800 p-6 rounded-2xl">
                    <h3 class="text-lg font-bold mb-4">Business Settings & Invoice Footer</h3>
                    <form action="/settings/update" method="POST" class="space-y-4">
                        <div class="grid grid-cols-2 gap-4">
                            <input type="text" name="currency" value="{{ settings.currency }}" placeholder="Currency (USD, NGN)" required class="bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm">
                            <input type="number" step="0.01" name="tax_rate" value="{{ settings.tax_rate }}" placeholder="Tax Rate %" class="bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm">
                        </div>
                        <input type="text" name="invoice_footer" value="{{ settings.invoice_footer }}" placeholder="Invoice Footer Note" class="w-full bg-slate-950 border border-slate-800 p-3 rounded-lg text-sm">
                        <button type="submit" class="bg-slate-800 hover:bg-slate-700 text-white font-bold px-4 py-2 rounded-lg text-sm">Update Settings</button>
                    </form>
                </div>
            </div>

            <div class="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden mb-8">
                <div class="p-6 border-b border-slate-800"><h3 class="text-lg font-bold">Lead Pipeline</h3></div>
                <div class="overflow-x-auto">
                    <table class="w-full text-left border-collapse">
                        <thead>
                            <tr class="border-b border-slate-800 text-xs text-slate-400 uppercase">
                                <th class="p-4">Name</th><th class="p-4">Phone</th><th class="p-4">Email</th><th class="p-4">Status</th><th class="p-4">Actions</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-800 text-sm">
                            {% for lead in leads %}
                            <tr>
                                <td class="p-4 font-medium">{{ lead.name }}</td>
                                <td class="p-4 text-slate-400">{{ lead.phone }}</td>
                                <td class="p-4 text-slate-400">{{ lead.email }}</td>
                                <td class="p-4"><span class="px-2 py-1 rounded text-xs bg-slate-800">{{ lead.status }}</span></td>
                                <td class="p-4">
                                    <form action="/leads/delete/{{ lead.id }}" method="POST">
                                        <button type="submit" class="text-red-400 hover:text-red-300 text-xs">Delete</button>
                                    </form>
                                </td>
                            </tr>
                            {% else %}
                            <tr><td colspan="5" class="p-6 text-center text-slate-500">No leads added yet.</td></tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </main>
    </div>
    <script>
    async function generateAI() {
        const prompt = document.getElementById('aiPrompt').value;
        const resDiv = document.getElementById('aiResult');
        resDiv.innerText = "Generating response with Gemini AI...";
        const response = await fetch('/ai-reply', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({prompt})
        });
        const data = await response.json();
        resDiv.innerText = data.reply;
    }
    </script>
</body>
</html>
"""

CREATOR_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Creator Admin - GrowthCRM</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-950 text-slate-100 p-8">
    <div class="max-w-6xl mx-auto">
        <h1 class="text-2xl font-bold text-emerald-400 mb-6">Creator / Admin Panel</h1>
        <div class="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden">
            <div class="p-6 border-b border-slate-800"><h3 class="text-lg font-bold">All Registered Users & Subscriptions</h3></div>
            <table class="w-full text-left text-sm">
                <thead>
                    <tr class="border-b border-slate-800 text-xs text-slate-400 uppercase">
                        <th class="p-4">Name</th><th class="p-4">Business</th><th class="p-4">Email</th><th class="p-4">Plan</th><th class="p-4">Access Code</th>
                    </tr>
                </thead>
                <tbody class="divide-y divide-slate-800">
                    {% for u in users %}
                    <tr>
                        <td class="p-4">{{ u.name }}</td>
                        <td class="p-4">{{ u.business }}</td>
                        <td class="p-4 text-slate-400">{{ u.email }}</td>
                        <td class="p-4">{{ u.plan or 'None' }}</td>
                        <td class="p-4 text-emerald-400 font-mono text-xs">{{ u.access_code or 'N/A' }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
