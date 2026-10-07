import os
import hashlib
import secrets
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, session, redirect

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "timileyin-growth-crm-secret-key-2026")

# --- Database Setup ---
try:
    from database import db
except ImportError:
    import sqlite3
    class DBWrapper:
        def __init__(self):
            self.conn = sqlite3.connect("crm.db", check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        def one(self, query, params=()):
            cur = self.conn.cursor()
            cur.execute(query, params)
            row = cur.fetchone()
            return dict(row) if row else None
        def execute(self, query, params=()):
            cur = self.conn.cursor()
            cur.execute(query, params)
            self.conn.commit()
            return cur
    db_instance = DBWrapper()
    def db():
        return db_instance

# --- Helper Functions ---
def email(val):
    return val.strip().lower() if val else ""

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def password_ok(password, hashed):
    return hash_password(password) == hashed

def now():
    return datetime.utcnow()

def user_json(u):
    if not u:
        return None
    return {
        "id": u.get("id"),
        "name": u.get("name"),
        "business": u.get("business"),
        "email": u.get("email"),
        "created_at": str(u.get("created_at", ""))
    }

PLANS = {
    "monthly": {"days": 31, "price": "NGN 5,000"}
}

# --- Subscription Activation Helper ---
def activate(email_value, plan, reference="", uid=None):
    e = email(email_value)
    start = now()
    old = db().one("SELECT * FROM subscriptions WHERE lower(email)=?", (e,))
    
    if old and old["status"] == "active":
        try:
            x = datetime.fromisoformat(old["expires_at"])
            if x and x > start:
                start = x
        except Exception:
            pass
            
    exp = start + timedelta(days=PLANS.get(plan, PLANS["monthly"])["days"])
    code = old["access_code"] if old and old.get("access_code") else "SELAR-" + secrets.token_hex(4).upper()
    
    if old:
        db().execute(
            "UPDATE subscriptions SET user_id=?, plan=?, status=?, starts_at=?, expires_at=?, access_code=? WHERE lower(email)=?",
            (uid, plan, "active", start.isoformat(), exp.isoformat(), code, e)
        )
    else:
        db().execute(
            "INSERT INTO subscriptions (user_id, email, plan, access_code, status, starts_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, e, plan, code, "active", start.isoformat(), exp.isoformat())
        )
    return db().one("SELECT * FROM subscriptions WHERE lower(email)=?", (e,))

# --- API Routes ---
@app.get("/health")
def health():
    try:
        db().one("SELECT 1 AS ok")
        db_type = "turso" if getattr(db(), "turso", False) else "sqlite"
    except Exception:
        db_type = "unknown"
    return jsonify(ok=True, app="TIMILEYINGROWTHCRM", database=db_type)

@app.get("/api/plans")
def plans():
    return jsonify(ok=True, plans=PLANS)

@app.post("/api/register")
def register():
    d = request.get_json(silent=True) or {}
    n = str(d.get("name", "")).strip()
    b = str(d.get("business", "")).strip()
    e = email(d.get("email"))
    p = str(d.get("password", ""))
    
    if not n or not b or not e or len(p) < 8:
        return jsonify(ok=False, error="Name, business, valid email, and password (min 8 chars) are required."), 400
        
    if db().one("SELECT id FROM users WHERE lower(email)=?", (e,)):
        return jsonify(ok=False, error="This email is already registered."), 400
        
    try:
        db().execute(
            "INSERT INTO users (name, business, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (n, b, e, hash_password(p), now().isoformat())
        )
    except Exception as err:
        return jsonify(ok=False, error=str(err)), 400
        
    u = db().one("SELECT id, name, business, email, created_at FROM users WHERE lower(email)=?", (e,))
    if u:
        db().execute("UPDATE subscriptions SET user_id=? WHERE lower(email)=?", (u["id"], e))
        session["uid"] = u["id"]
        
    return jsonify(ok=True, user=user_json(u))

@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    e = email(d.get("email"))
    p = str(d.get("password", ""))
    row = db().one("SELECT * FROM users WHERE lower(email)=?", (e,))
    
    if not row or not password_ok(p, row["password_hash"]):
        return jsonify(ok=False, error="Incorrect email or password."), 401
        
    session["uid"] = row["id"]
    u = {k: row[k] for k in ("id", "name", "business", "email", "created_at")}
    return jsonify(ok=True, user=user_json(u))

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)

@app.get("/api/me")
def me():
    uid = session.get("uid")
    u = db().one("SELECT id, name, business, email, created_at FROM users WHERE id=?", (uid,)) if uid else None
    return jsonify(ok=True, authenticated=bool(u), user=user_json(u) if u else None)

@app.get("/api/payment-status")
def payment_status():
    e = email(request.args.get("email"))
    if not e:
        uid = session.get("uid")
        u = db().one("SELECT email FROM users WHERE id=?", (uid,)) if uid else None
        if u:
            e = u["email"]
    sub = db().one("SELECT * FROM subscriptions WHERE lower(email)=? ORDER BY id DESC LIMIT 1", (e,)) if e else None
    return jsonify(ok=True, subscription=sub)

@app.post("/api/activate")
def activate_code():
    uid = session.get("uid")
    if not uid:
        return jsonify(ok=False, error="Login required"), 401
    d = request.get_json(silent=True) or {}
    code = str(d.get("access_code", "")).strip().upper()
    u = db().one("SELECT email FROM users WHERE id=?", (uid,))
    if not u:
        return jsonify(ok=False, error="User not found"), 404
        
    s = db().one("SELECT * FROM subscriptions WHERE access_code=? AND lower(email)=?", (code, u["email"]))
    if not s or not sub_json(s).get("active"):
        return jsonify(ok=False, error="Invalid or expired access code."), 400
        
    sub = activate(u["email"], s["plan"], uid=uid)
    return jsonify(ok=True, subscription=sub)

# --- Welcome Route ---
@app.route("/welcome", methods=["GET", "POST"])
def welcome():
    error = None
    if request.method == "POST":
        try:
            name = request.form.get("name", "").strip()
            business = request.form.get("business", "").strip()
            raw_email = request.form.get("email", "").strip()
            e = email(raw_email)
            password = request.form.get("password", "").strip()
            
            if not name or not business or not e or not password:
                error = "Please fill in all fields."
            else:
                existing = db().one("SELECT * FROM users WHERE lower(email)=?", (e,))
                if existing:
                    session["uid"] = existing["id"]
                    user_id = existing["id"]
                else:
                    db().execute(
                        "INSERT INTO users (name, business, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                        (name, business, e, hash_password(password), now().isoformat())
                    )
                    new_user = db().one("SELECT * FROM users WHERE lower(email)=?", (e,))
                    session["uid"] = new_user["id"]
                    user_id = new_user["id"]
                
                activate(e, "monthly", uid=user_id)
                return redirect("/")
        except Exception as err:
            error = f"Error: {str(err)}"
            
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Welcome - TIMILEYINGROWTHCRM</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: sans-serif; background: #0b0f19; color: #fff; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }}
            .card {{ background: #161e2e; width: 100%; max-width: 420px; padding: 30px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }}
            h2 {{ color: #10b981; text-align: center; margin-top: 0; }}
            p {{ text-align: center; color: #9ca3af; font-size: 14px; margin-bottom: 24px; }}
            .error {{ background: #ef4444; color: #fff; padding: 10px; border-radius: 6px; text-align: center; margin-bottom: 15px; font-size: 14px; }}
            label {{ display: block; margin-bottom: 6px; font-size: 13px; color: #d1d5db; }}
            input {{ width: 100%; padding: 10px 12px; margin-bottom: 16px; background: #0b0f19; border: 1px solid #374151; color: #fff; border-radius: 6px; box-sizing: border-box; }}
            button {{ width: 100%; background: #10b981; color: #fff; border: none; padding: 12px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 15px; }}
            button:hover {{ background: #059669; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>Payment Successful! 🎉</h2>
            <p>Thank you for subscribing to TIMILEYINGROWTHCRM. Enter your details below to activate your subscription and access your dashboard.</p>
            {f'<div class="error">{error}</div>' if error else ''}
            <form method="POST">
                <label>Full Name</label>
                <input type="text" name="name" required placeholder="Enter your name">
                
                <label>Business Name</label>
                <input type="text" name="business" required placeholder="Enter your business name">
                
                <label>Email Address</label>
                <input type="email" name="email" required placeholder="Enter your email">
                
                <label>Password</label>
                <input type="password" name="password" required placeholder="Enter your password">
                
                <button type="submit">Access Dashboard</button>
            </form>
        </div>
    </body>
    </html>
    """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))