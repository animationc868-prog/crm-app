import os, re, json, hmac, hashlib, secrets, sqlite3, smtplib
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urljoin
from email.message import EmailMessage

import requests
from flask import Flask, request, jsonify, session, render_template_string

APP_NAME = "TIMILEYINGROWTHCRM"
CREATOR_EMAIL = os.getenv("CREATOR_EMAIL", "dayotimileyin831@gmail.com").lower().strip()
# Set CREATOR_PASSWORD in Render Environment Variables. Never put the real password in GitHub.
CREATOR_PASSWORD = os.getenv("CREATOR_PASSWORD", "")
CONTACT_EMAIL = "supporttimilehingrowth@gmail.com"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
CREATOR_PATH = os.getenv("CREATOR_PATH", "/tgr-master-831").strip() or "/tgr-master-831"
if not CREATOR_PATH.startswith("/"): CREATOR_PATH = "/" + CREATOR_PATH

SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_FROM = os.getenv("RESEND_FROM", "onboarding@resend.dev").strip()


def send_access_code_email(to_email, code, plan_name, expires_at):
    """Send through Resend HTTPS API; report API acceptance separately from delivery."""
    if not RESEND_API_KEY:
        return False, "Email was NOT sent: add RESEND_API_KEY in Render."
    payload = {
        "from": RESEND_FROM,
        "to": [to_email],
        "subject": "Your TIMILEYINGROWTHCRM personal access code",
        "text": (
            "Thank you for your TIMILEYINGROWTHCRM subscription.\n\n"
            f"Your personal access code is: {code}\nPlan: {plan_name}\n"
            f"Subscription expiry: {expires_at}\n\n"
            "Open TIMILEYINGROWTHCRM, choose Register Account, and use this exact email and code.\n\n"
            f"Need help? Contact {CONTACT_EMAIL}.\n\nTIMILEYINGROWTHCRM Support"
        ),
    }
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json=payload, timeout=20,
        )
        if response.ok:
            result = response.json() if response.content else {}
            return True, "Email accepted by Resend (ID: " + str(result.get("id", "created")) + "). Check the recipient inbox/spam."
        app.logger.error("Resend email API rejected request: HTTP %s: %s", response.status_code, response.text[:1000])
        return False, f"Email was NOT sent: Resend returned HTTP {response.status_code}. Check Render logs and Resend sender/domain verification."
    except Exception:
        app.logger.exception("Resend access-code email request failed")
        return False, "Email was NOT sent: could not reach Resend API. Check Render logs."


def send_password_reset_email(to_email, reset_url):
    """Send a time-limited password-reset link through the configured Resend account."""
    if not RESEND_API_KEY:
        return False, "Password reset email is not configured. Please contact support."
    payload = {
        "from": RESEND_FROM,
        "to": [to_email],
        "subject": "Reset your TIMILEYINGROWTHCRM password",
        "text": (
            "We received a request to reset your TIMILEYINGROWTHCRM password.\n\n"
            f"Use this one-time link within 30 minutes: {reset_url}\n\n"
            "If you did not request this, you can ignore this email.\n\n"
            f"Support: {CONTACT_EMAIL}\nTIMILEYINGROWTHCRM Support"
        ),
    }
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json=payload, timeout=20,
        )
        if response.ok:
            return True, "Email accepted by Resend; delivery is not guaranteed."
        app.logger.error("Resend password-reset email rejected: HTTP %s: %s", response.status_code, response.text[:1000])
        return False, "Password reset email could not be sent. Please contact support."
    except Exception:
        app.logger.exception("Resend password-reset email request failed")
        return False, "Password reset email could not be sent. Please contact support."


PLANS = {
    "monthly": {"name": "1 Month", "price": 29, "days": 31, "url": "https://selar.com/f167zzc299"},
    "six_months": {"name": "6 Months", "price": 129, "days": 183, "url": "https://selar.com/1212127215"},
    "yearly": {"name": "1 Year", "price": 199, "days": 365, "url": "https://selar.com/56r7979796"},
}

app = Flask(__name__)
if not SESSION_SECRET:
    raise RuntimeError("Missing SESSION_SECRET. Set a persistent random value in Render Environment Variables.")
app.secret_key = SESSION_SECRET
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
DB = None


def now():
    return datetime.now(timezone.utc)


def iso():
    return now().isoformat()


def dt(v):
    """Parse stored timestamps consistently as timezone-aware UTC values."""
    try:
        parsed = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def email(v):
    return str(v or "").strip().lower()


def password_hash(password):
    salt = secrets.token_bytes(16)
    rounds = 200000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return f"pbkdf2${rounds}${salt.hex()}${digest.hex()}"


def password_ok(password, stored):
    try:
        alg, rounds, salt, digest = stored.split("$", 3)
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return alg == "pbkdf2" and hmac.compare_digest(got, digest)
    except Exception:
        return False


class Database:
    """Turso HTTP pipeline client (avoids libsql WebSocket handshake failures)."""
    def __init__(self):
        if not TURSO_URL or not TURSO_TOKEN:
            raise RuntimeError(
                "Persistent database is not configured. Set TURSO_DATABASE_URL and "
                "TURSO_AUTH_TOKEN in Render Environment Variables."
            )
        url = TURSO_URL.strip()
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        elif url.startswith("wss://"):
            url = "https://" + url[len("wss://"):]
        elif not url.startswith("https://"):
            url = "https://" + url.lstrip("/")
        self.api_url = url.rstrip("/") + "/v2/pipeline"
        self.headers = {
            "Authorization": f"Bearer {TURSO_TOKEN}",
            "Content-Type": "application/json",
        }
        self.turso = True
        self.schema()

    @staticmethod
    def _arg(value):
        if value is None:
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "integer", "value": "1" if value else "0"}
        if isinstance(value, int):
            return {"type": "integer", "value": str(value)}
        if isinstance(value, float):
            return {"type": "float", "value": str(value)}
        return {"type": "text", "value": str(value)}

    @staticmethod
    def _cell(cell):
        kind = cell.get("type")
        value = cell.get("value")
        if kind == "null" or ("value" not in cell and "base64" not in cell):
            return None
        if kind == "integer":
            try: return int(value)
            except (TypeError, ValueError): return value
        if kind == "float":
            try: return float(value)
            except (TypeError, ValueError): return value
        if "base64" in cell:
            return cell["base64"]
        return value

    def execute(self, sql, args=()):
        # Normalize webhook writes for databases created by earlier app versions.
        # Older schemas may have a required received_at column; always provide it.
        if re.match(r"\s*INSERT\s+INTO\s+webhook_events\b", sql, re.I):
            values = tuple(args or ())
            if len(values) == 5:
                values = values + (iso(),)
            if len(values) != 6:
                raise RuntimeError(
                    f"webhook_events insert expected 6 values after compatibility handling; got {len(values)}"
                )
            sql = ("INSERT INTO webhook_events "
                   "(event_key,reference,email,payload,created_at,received_at) "
                   "VALUES(?,?,?,?,?,?)")
            args = values
        payload = {"requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": [self._arg(a) for a in args]}},
            {"type": "close"},
        ]}
        try:
            resp = requests.post(self.api_url, headers=self.headers, json=payload, timeout=20)
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not connect to Turso HTTP API: {exc}") from exc
        if not resp.ok:
            raise RuntimeError(f"Turso HTTP Error {resp.status_code}: {resp.text[:1000]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError("Turso returned a non-JSON response.") from exc
        results = data.get("results") or []
        if not results:
            raise RuntimeError(f"Turso returned an empty result: {data}")
        first = results[0]
        if first.get("type") == "error":
            detail = first.get("error", {})
            raise RuntimeError(detail.get("message", str(detail)))
        response = first.get("response", {})
        if response.get("type") == "error":
            detail = response.get("error", {})
            raise RuntimeError(detail.get("message", str(detail)))
        result = response.get("result", {})
        columns = [col.get("name", "") for col in result.get("cols", [])]
        rows = [[self._cell(cell) for cell in row] for row in result.get("rows", [])]

        class ResultSet:
            def __init__(self, columns, rows):
                self.columns = columns
                self.rows = rows

        return ResultSet(columns, rows)

    def one(self, sql, args=()):
        result = self.execute(sql, args)
        return dict(zip(result.columns, result.rows[0])) if result.rows else None

    def all(self, sql, args=()):
        result = self.execute(sql, args)
        return [dict(zip(result.columns, row)) for row in result.rows]

    def schema(self):
        for query in [
            "CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,business TEXT NOT NULL,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,email TEXT NOT NULL,plan TEXT NOT NULL,access_code TEXT NOT NULL UNIQUE,starts_at TEXT NOT NULL,expires_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',selar_reference TEXT,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,name TEXT NOT NULL,email TEXT,phone TEXT,company TEXT,website TEXT,source TEXT,stage TEXT DEFAULT 'New',score INTEGER DEFAULT 0,deal_value REAL DEFAULT 0,notes TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,lead_id INTEGER,title TEXT NOT NULL,appointment_at TEXT NOT NULL,status TEXT DEFAULT 'Scheduled',notes TEXT,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS followups(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,lead_id INTEGER,due_at TEXT NOT NULL,channel TEXT DEFAULT 'Email',message TEXT,status TEXT DEFAULT 'Pending',created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,title TEXT NOT NULL,due_at TEXT,priority TEXT DEFAULT 'Medium',status TEXT DEFAULT 'Pending',created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS settings(user_id INTEGER PRIMARY KEY,business_name TEXT,business_email TEXT,phone TEXT,website TEXT,industry TEXT,description TEXT,updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS booking_settings(user_id INTEGER PRIMARY KEY,slug TEXT UNIQUE NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,working_days TEXT NOT NULL DEFAULT '1,2,3,4,5',start_time TEXT NOT NULL DEFAULT '09:00',end_time TEXT NOT NULL DEFAULT '17:00',duration INTEGER NOT NULL DEFAULT 30,updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,name TEXT NOT NULL,email TEXT,rating INTEGER NOT NULL,review TEXT NOT NULL,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS webhook_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE NOT NULL,reference TEXT,email TEXT,payload TEXT,created_at TEXT NOT NULL,received_at TEXT NOT NULL DEFAULT '')",
            "CREATE TABLE IF NOT EXISTS creator_code_history(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT NOT NULL,plan TEXT NOT NULL,access_code TEXT NOT NULL,expires_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',email_sent INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,customer_name TEXT NOT NULL DEFAULT '',created_by TEXT NOT NULL DEFAULT 'Creator',ip_address TEXT NOT NULL DEFAULT '',email_status TEXT NOT NULL DEFAULT 'Not sent')",
            "CREATE TABLE IF NOT EXISTS password_reset_tokens(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,token_hash TEXT NOT NULL UNIQUE,expires_at TEXT NOT NULL,used_at TEXT,created_at TEXT NOT NULL)",
        ]:
            self.execute(query)
        for col, definition in [
            ("received_at", "TEXT NOT NULL DEFAULT ''"),
            ("customer_name", "TEXT NOT NULL DEFAULT ''"),
            ("created_by", "TEXT NOT NULL DEFAULT 'Creator'"),
            ("ip_address", "TEXT NOT NULL DEFAULT ''"),
            ("email_status", "TEXT NOT NULL DEFAULT 'Not sent'"),
        ]:
            try:
                self.execute(f"ALTER TABLE creator_code_history ADD COLUMN {col} {definition}")
            except Exception:
                pass


def db():
    global DB
    if DB is None: DB = Database()
    return DB


def user():
    uid = session.get("uid")
    return db().one("SELECT id,name,business,email,created_at FROM users WHERE id=?", (uid,)) if uid else None


def make_code():
    while True:
        c = "TGR-" + secrets.token_hex(3).upper() + "-" + secrets.token_hex(3).upper()
        if not db().one("SELECT id FROM subscriptions WHERE access_code=?", (c,)): return c


def sub_for(email_value):
    r = db().one("SELECT * FROM subscriptions WHERE lower(email)=? ORDER BY id DESC LIMIT 1", (email(email_value),))
    if not r: return None
    exp = dt(r["expires_at"])
    if r["status"] == "active" and exp and exp > now(): return r
    if r["status"] == "active": db().execute("UPDATE subscriptions SET status='expired' WHERE id=?", (r["id"],))
    return dict(r, status="expired")


def sub_json(s):
    if not s: return {"active": False, "status": "inactive", "seconds_remaining": 0, "days_remaining": 0}
    p = PLANS.get(s["plan"], {})
    exp = dt(s["expires_at"])
    seconds = max(0, int((exp-now()).total_seconds())) if exp else 0
    days = seconds // 86400
    active = s["status"] == "active" and bool(exp and exp > now())
    status = s["status"]
    if status == "active" and not active:
        status = "expired"
    return {"active": active, "status": status, "plan": s["plan"], "plan_name": p.get("name", s["plan"]), "price": p.get("price"), "access_code": s["access_code"], "starts_at": s["starts_at"], "expires_at": s["expires_at"], "days_remaining": days, "seconds_remaining": seconds, "selar_reference": s.get("selar_reference")}


def user_json(u): return {**u, "subscription": sub_json(sub_for(u["email"]))}


def paid(fn):
    @wraps(fn)
    def w(*a, **k):
        u = user()
        if not u: return jsonify(ok=False,error="Login required",code="LOGIN_REQUIRED"),401
        s = sub_for(u["email"])
        if not s or not sub_json(s)["active"]: return jsonify(ok=False,error="Active subscription required",code="SUBSCRIPTION_REQUIRED"),402
        return fn(*a, **k)
    return w


def creator(fn):
    @wraps(fn)
    def w(*a, **k):
        if not session.get("creator") or email(session.get("creator_email")) != CREATOR_EMAIL: return ("Not found", 404)
        return fn(*a, **k)
    return w


def plan_value(v):
    s = str(v or "").strip().lower()
    aliases = {"monthly":"monthly","month":"monthly","1 month":"monthly","29":"monthly","$29":"monthly","six months":"six_months","6 months":"six_months","six_months":"six_months","129":"six_months","$129":"six_months","yearly":"yearly","year":"yearly","1 year":"yearly","12 months":"yearly","199":"yearly","$199":"yearly"}
    return aliases.get(s)


def deep(obj, keys):
    keys = {x.lower() for x in keys}
    if isinstance(obj, dict):
        for k,v in obj.items():
            if str(k).lower() in keys and v not in (None,""): return v
        for v in obj.values():
            x=deep(v,keys)
            if x not in (None,""): return x
    if isinstance(obj,list):
        for v in obj:
            x=deep(v,keys)
            if x not in (None,""): return x
    return None


def webhook_data(p):
    product=deep(p,{"product","product_name","productName","plan","plan_name","planName","item_name","itemName"})
    amount=deep(p,{"amount","price","total","total_amount","totalAmount"})
    plan=plan_value(product)
    if not plan:
        try: plan=plan_value(str(float(str(amount).replace("$","").replace(",",""))))
        except Exception: pass
    return {"email":email(deep(p,{"email","buyer_email","customer_email","buyerEmail","customerEmail"})),"name":str(deep(p,{"name","buyer_name","customer_name","buyerName","customerName"}) or ""),"reference":str(deep(p,{"reference","transaction_reference","transaction_id","transactionId","order_id","orderId","id"}) or ""),"plan":plan,"status":str(deep(p,{"status","payment_status","paymentStatus","event","event_type","eventType"}) or "").lower()}


def activate(email_value, plan, reference="", uid=None):
    e=email(email_value); old=sub_for(e); start=now()
    if old and old["status"]=="active":
        x=dt(old["expires_at"])
        if x and x>start: start=x
    exp=start+timedelta(days=PLANS[plan]["days"])
    code=old["access_code"] if old else make_code()
    if old:
        db().execute("UPDATE subscriptions SET user_id=?,plan=?,starts_at=?,expires_at=?,status='active',selar_reference=? WHERE id=?",(uid or old.get("user_id"),plan,iso(),exp.isoformat(),reference,old["id"]))
    else:
        db().execute("INSERT INTO subscriptions(user_id,email,plan,access_code,starts_at,expires_at,status,selar_reference,created_at) VALUES(?,?,?,?,?,?,'active',?,?)",(uid,e,plan,code,iso(),exp.isoformat(),reference,iso()))
    return sub_for(e)


@app.get("/health")
def health():
    db().one("SELECT 1 AS ok")
    return jsonify(ok=True,app=APP_NAME,database="turso" if db().turso else "sqlite")

@app.get("/api/plans")
def plans(): return jsonify(ok=True,plans=PLANS)

@app.post("/api/retrieve-code")
def retrieve_code():
    # The Selar webhook creates the unique code after verified payment.
    # The code is bound to the buyer email, which registration must also match.
    d=request.get_json(silent=True) or {}
    e=email(d.get("email"))
    if not e:
        return jsonify(ok=False,error="Enter the email address used for your Selar purchase."),400
    s=db().one("SELECT * FROM subscriptions WHERE lower(email)=? ORDER BY id DESC LIMIT 1",(e,))
    if not s or not sub_json(s)["active"] or not s.get("selar_reference"):
        return jsonify(ok=False,error="We have not received a verified active Selar payment for this email yet. Please wait briefly and try again."),404
    return jsonify(ok=True,access_code=s["access_code"],plan=PLANS.get(s["plan"],{}).get("name",s["plan"]))

@app.post("/api/register")
def register():
    d=request.get_json(silent=True) or {}; n=str(d.get("name","")).strip(); b=str(d.get("business","")).strip(); e=email(d.get("email")); p=str(d.get("password",""))
    if not n or not b or not e or len(p)<8: return jsonify(ok=False,error="Name, business, valid email and an 8+ character password are required."),400
    # Require the unique code issued for this buyer's verified Selar subscription.
    code=str(d.get("access_code","")).strip().upper()
    s=db().one("SELECT * FROM subscriptions WHERE access_code=? AND lower(email)=? ORDER BY id DESC LIMIT 1",(code,e)) if code else None
    if not s or not sub_json(s)["active"]:
        return jsonify(ok=False,error="Invalid code, email mismatch, or inactive subscription. Check the email and code supplied by the Creator."),403
    existing=db().one("SELECT id,name,business,email,created_at FROM users WHERE lower(email)=?",(e,))
    if existing:
        # Possession of the current, email-issued access code proves control of
        # this subscription email. Let an existing customer recover access here.
        db().execute("UPDATE users SET password_hash=? WHERE id=?",(password_hash(p),existing["id"]))
        db().execute("UPDATE subscriptions SET user_id=? WHERE id=?",(existing["id"],s["id"]))
        session["uid"]=existing["id"]
        u=db().one("SELECT id,name,business,email,created_at FROM users WHERE id=?",(existing["id"],))
        return jsonify(ok=True,user=user_json(u),account_recovered=True,message="Existing account verified. Your password has been updated and you are now logged in.")
    try: db().execute("INSERT INTO users(name,business,email,password_hash,created_at) VALUES(?,?,?,?,?)",(n,b,e,password_hash(p),iso()))
    except Exception: return jsonify(ok=False,error="Could not create this account. Please try logging in or use Forgot Password."),409
    u=db().one("SELECT id,name,business,email,created_at FROM users WHERE lower(email)=?",(e,))
    db().execute("UPDATE subscriptions SET user_id=? WHERE id=?",(u["id"],s["id"]))
    session["uid"]=u["id"]
    return jsonify(ok=True,user=user_json(u))

@app.post("/api/password/forgot")
def forgot_password():
    # Always return a generic response to avoid revealing registered email addresses.
    d = request.get_json(silent=True) or {}
    e = email(d.get("email"))
    generic = "If an account exists for that email, a password-reset link will be sent shortly. Check your inbox and spam folder."
    if not e or len(e) > 320 or not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", e):
        return jsonify(ok=True, message=generic)
    row = db().one("SELECT id,email FROM users WHERE lower(email)=?", (e,))
    if row:
        # Invalidate outstanding links before issuing a fresh one.
        db().execute("UPDATE password_reset_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL", (iso(), row["id"]))
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        expiry = (now() + timedelta(minutes=30)).isoformat()
        db().execute("INSERT INTO password_reset_tokens(user_id,token_hash,expires_at,used_at,created_at) VALUES(?,?,?,NULL,?)", (row["id"], token_hash, expiry, iso()))
        base = PUBLIC_BASE_URL or request.url_root.rstrip("/")
        reset_url = base + "/reset-password?token=" + raw_token
        sent, detail = send_password_reset_email(e, reset_url)
        if not sent:
            app.logger.warning("Password reset email not sent for user id %s: %s", row["id"], detail)
    return jsonify(ok=True, message=generic)

@app.get("/reset-password")
def reset_password_page():
    token = str(request.args.get("token", ""))
    if len(token) > 200:
        token = ""
    return render_template_string(r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reset password • TIMILEYINGROWTHCRM</title><style>*{box-sizing:border-box}body{margin:0;padding:18px;background:#0A0A0A;color:#FFF;font-family:Arial,sans-serif}.card{max-width:480px;margin:7vh auto;padding:26px;background:#111;border:1px solid #2A2A2A;border-radius:16px}h1{font-size:25px}p{color:#B5B5B5;line-height:1.6}label{display:block;margin:14px 0 6px}input{width:100%;padding:13px;border-radius:9px;border:1px solid #333;background:#1E1E1E;color:#FFF;font-size:16px}button,.back{display:block;width:100%;margin-top:16px;padding:13px;border:0;border-radius:9px;background:#D4AF37;color:#000;font-weight:800;text-align:center;text-decoration:none;cursor:pointer}.msg{white-space:pre-wrap;line-height:1.5;margin-top:12px}</style></head><body><main class="card"><h1>Reset your password</h1><p>Choose a new password of at least 8 characters. This link expires after 30 minutes and can be used once.</p><div id="msg" class="msg" role="status"></div><form id="f"><label for="p1">New password</label><input id="p1" type="password" minlength="8" autocomplete="new-password" required><label for="p2">Confirm new password</label><input id="p2" type="password" minlength="8" autocomplete="new-password" required><button id="submit" type="submit">Save new password</button></form><a class="back" href="/">Back to login</a></main><script>const token=__TOKEN__;const form=document.getElementById('f'),msg=document.getElementById('msg'),btn=document.getElementById('submit');if(!token){form.style.display='none';msg.textContent='This reset link is missing or invalid. Request a new one from the login screen.'}form.addEventListener('submit',async e=>{e.preventDefault();if(document.getElementById('p1').value!==document.getElementById('p2').value){msg.textContent='The passwords do not match.';return}btn.disabled=true;btn.textContent='Saving…';try{const r=await fetch('/api/password/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token,password:document.getElementById('p1').value})});const d=await r.json();if(!r.ok)throw Error(d.error||'Could not reset password');form.style.display='none';msg.textContent='Password updated successfully. Return to the main page and log in with your new password.'}catch(x){msg.textContent=x.message}finally{btn.disabled=false;btn.textContent='Save new password'}});</script></body></html>'''.replace("__TOKEN__", json.dumps(token)))

@app.post("/api/password/reset")
def reset_password():
    d = request.get_json(silent=True) or {}
    token = str(d.get("token", ""))
    password = str(d.get("password", ""))
    if len(password) < 8:
        return jsonify(ok=False, error="Password must contain at least 8 characters."), 400
    if not token or len(token) > 200:
        return jsonify(ok=False, error="This reset link is invalid or expired. Request a new link."), 400
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    rec = db().one("SELECT * FROM password_reset_tokens WHERE token_hash=? AND used_at IS NULL", (token_hash,))
    expiry = dt(rec["expires_at"]) if rec else None
    if not rec or not expiry or expiry <= now():
        return jsonify(ok=False, error="This reset link is invalid, expired, or already used. Request a new link."), 400
    db().execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash(password), rec["user_id"]))
    db().execute("UPDATE password_reset_tokens SET used_at=? WHERE id=? AND used_at IS NULL", (iso(), rec["id"]))
    session.pop("uid", None)
    return jsonify(ok=True, message="Password updated.")

@app.post("/api/login")
def login():
    d=request.get_json(silent=True) or {}; e=email(d.get("email")); p=str(d.get("password","")); row=db().one("SELECT * FROM users WHERE lower(email)=?",(e,))
    if not row or not password_ok(p,row["password_hash"]): return jsonify(ok=False,error="Incorrect email or password."),401
    session["uid"]=row["id"]; u={k:row[k] for k in ("id","name","business","email","created_at")}
    return jsonify(ok=True,user=user_json(u))

@app.post("/api/logout")
def logout(): session.clear(); return jsonify(ok=True)

@app.get("/api/me")
def me():
    u=user(); return jsonify(ok=True,authenticated=bool(u),user=user_json(u) if u else None)

@app.get("/api/payment-status")
def payment_status():
    e=email(request.args.get("email")) or (user() or {}).get("email",""); return jsonify(ok=True,subscription=sub_json(sub_for(e)))

@app.post("/api/activate")
def activate_code():
    u=user(); d=request.get_json(silent=True) or {}; code=str(d.get("access_code","")).upper().strip()
    if not u:return jsonify(ok=False,error="Login required"),401
    s=db().one("SELECT * FROM subscriptions WHERE access_code=? AND lower(email)=? ORDER BY id DESC LIMIT 1",(code,u["email"]))
    if not s or not sub_json(s)["active"]:return jsonify(ok=False,error="Invalid or expired access code."),402
    return jsonify(ok=True,subscription=sub_json(s))

@app.get("/api/leads")
@paid
def leads(): return jsonify(ok=True,leads=db().all("SELECT * FROM leads WHERE user_id=? ORDER BY id DESC",(user()["id"],)))

@app.post("/api/leads")
@paid
def add_lead():
    u=user(); d=request.get_json(silent=True) or {}; n=str(d.get("name","")).strip()
    if not n:return jsonify(ok=False,error="Lead name is required."),400
    db().execute("INSERT INTO leads(user_id,name,email,phone,company,website,source,stage,score,deal_value,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(u["id"],n,d.get("email",""),d.get("phone",""),d.get("company",""),d.get("website",""),d.get("source","Manual"),d.get("stage","New"),int(d.get("score",0) or 0),float(d.get("deal_value",0) or 0),d.get("notes",""),iso(),iso()))
    return jsonify(ok=True)

@app.delete("/api/leads/<int:i>")
@paid
def delete_lead(i): db().execute("DELETE FROM leads WHERE id=? AND user_id=?",(i,user()["id"])); return jsonify(ok=True)

@app.get("/api/dashboard")
@paid
def dashboard():
    uid=user()["id"]; total=db().one("SELECT COUNT(*) n FROM leads WHERE user_id=?",(uid,))["n"]; hot=db().one("SELECT COUNT(*) n FROM leads WHERE user_id=? AND score>=70",(uid,))["n"]; won=db().one("SELECT COUNT(*) n FROM leads WHERE user_id=? AND lower(stage)='won'",(uid,))["n"]; pipe=db().one("SELECT COALESCE(SUM(deal_value),0) n FROM leads WHERE user_id=? AND lower(stage) NOT IN ('won','lost')",(uid,))["n"]; rev=db().one("SELECT COALESCE(SUM(deal_value),0) n FROM leads WHERE user_id=? AND lower(stage)='won'",(uid,))["n"]; ap=db().one("SELECT COUNT(*) n FROM appointments WHERE user_id=? AND status='Scheduled'",(uid,))["n"]
    return jsonify(ok=True,dashboard={"total_leads":total,"hot_leads":hot,"won":won,"pipeline_value":pipe,"revenue":rev,"conversion_rate":round(won/total*100,1) if total else 0,"appointments":ap})

@app.post("/api/appointments")
@paid
def add_appt():
    u=user(); d=request.get_json(silent=True) or {}
    if not d.get("title") or not d.get("appointment_at"):return jsonify(ok=False,error="Title and date/time are required."),400
    db().execute("INSERT INTO appointments(user_id,lead_id,title,appointment_at,status,notes,created_at) VALUES(?,?,?,?,?,?,?)",(u["id"],d.get("lead_id"),d["title"],d["appointment_at"],d.get("status","Scheduled"),d.get("notes",""),iso())); return jsonify(ok=True)

@app.get("/api/appointments")
@paid
def get_appt():return jsonify(ok=True,appointments=db().all("SELECT * FROM appointments WHERE user_id=? ORDER BY appointment_at",(user()["id"],)))

@app.post("/api/followups")
@paid
def add_follow():
    u=user();d=request.get_json(silent=True) or {};db().execute("INSERT INTO followups(user_id,lead_id,due_at,channel,message,status,created_at) VALUES(?,?,?,?,?,'Pending',?)",(u["id"],d.get("lead_id"),d.get("due_at",""),d.get("channel","Email"),d.get("message",""),iso()));return jsonify(ok=True)

@app.get("/api/followups")
@paid
def get_follow():return jsonify(ok=True,followups=db().all("SELECT * FROM followups WHERE user_id=? ORDER BY due_at",(user()["id"],)))

@app.post("/api/tasks")
@paid
def add_task():
    u=user();d=request.get_json(silent=True) or {};db().execute("INSERT INTO tasks(user_id,title,due_at,priority,status,created_at) VALUES(?,?,?,?, 'Pending',?)",(u["id"],d.get("title",""),d.get("due_at",""),d.get("priority","Medium"),iso()));return jsonify(ok=True)

@app.get("/api/tasks")
@paid
def get_tasks():return jsonify(ok=True,tasks=db().all("SELECT * FROM tasks WHERE user_id=? ORDER BY due_at",(user()["id"],)))


def audit_site(url):
    if not re.match(r"^https?://",url,re.I):url="https://"+url
    r={"url":url,"reachable":False,"status_code":None,"response_ms":None,"title":"","mobile_viewport":False,"https":url.lower().startswith("https://"),"booking_features":False,"forms":0,"links_checked":0,"broken_links":[],"issues":[],"recommendations":["Check mobile usability.","Improve page speed and image optimization.","Use a clear call-to-action.","Make contact/booking easy to find.","Review SEO titles, descriptions and local business information."]}
    t=datetime.now().timestamp()
    try:
        x=requests.get(url,timeout=15,headers={"User-Agent":"TIMILEYINGROWTHCRM-Audit/1.0"},allow_redirects=True); html=x.text[:200000]; r["response_ms"]=round((datetime.now().timestamp()-t)*1000);r["status_code"]=x.status_code;r["reachable"]=x.ok
        m=re.search(r"<title[^>]*>(.*?)</title>",html,re.I|re.S);r["title"]=re.sub(r"\s+"," ",m.group(1)).strip() if m else ""
        r["mobile_viewport"]=bool(re.search(r'<meta[^>]+name=[\"\']viewport',html,re.I));r["forms"]=len(re.findall(r"<form\b",html,re.I));low=html.lower();r["booking_features"]=any(w in low for w in ("appointment","book now","booking","schedule","reserve"))
        if not r["https"]:r["issues"].append("HTTPS was not used in the submitted URL.")
        if not r["mobile_viewport"]:r["issues"].append("Mobile viewport meta tag was not detected.")
        if r["response_ms"]>3000:r["issues"].append("Initial response was over 3 seconds.")
        if r["forms"]==0:r["issues"].append("No HTML form was detected on the page.")
        if not r["booking_features"]:r["issues"].append("No obvious booking/appointment signal was detected.")
        links=re.findall(r'href=[\"\']([^\"\']+)',html,re.I); links=[urljoin(x.url,z) for z in links if z.startswith("/")][:30]+[z for z in links if z.startswith("http")][:30];r["links_checked"]=min(len(links),20)
        for z in links[:20]:
            try:
                q=requests.head(z,timeout=5,allow_redirects=True,headers={"User-Agent":"TIMILEYINGROWTHCRM-Audit/1.0"})
                if q.status_code>=400:r["broken_links"].append({"url":z,"status":q.status_code})
            except Exception:pass
        if r["broken_links"]:r["issues"].append(f"{len(r['broken_links'])} checked link(s) returned an error.")
    except Exception as e:r["issues"].append("The website could not be fully reached: "+str(e))
    return r

@app.post("/api/website-audit")
@paid
def website_audit():
    d=request.get_json(silent=True) or {};u=str(d.get("url","")).strip()
    if not u:return jsonify(ok=False,error="Website URL is required."),400
    return jsonify(ok=True,audit=audit_site(u))

@app.post("/api/ai/sales-coach")
@paid
def ai():
    if not GEMINI_KEY:return jsonify(ok=False,error="GEMINI_API_KEY is not configured in Render Environment."),503
    d=request.get_json(silent=True) or {};prompt=f'''You are the ethical AI Sales Coach inside {APP_NAME}. Create a concise personalized outreach message and 3 strategy tips. Never invent facts or claim results not provided. Business: {d.get("business","")} Prospect: {d.get("prospect","")} Service: {d.get("service","")} Channel: {d.get("channel","email")} Goal: {d.get("goal","get a reply")}.'''
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    try:
        x=requests.post(url,params={"key":GEMINI_KEY},json={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.7,"maxOutputTokens":900}},timeout=45)
        if not x.ok:
            try: detail=x.json().get("error",{}).get("message",x.text)
            except Exception: detail=x.text
            app.logger.error("Gemini API error HTTP %s: %s",x.status_code,str(detail)[:500])
            if x.status_code==400: hint="Check the selected Gemini model and request settings."
            elif x.status_code==401 or x.status_code==403: hint="Check that GEMINI_API_KEY is a valid Google AI Studio API key and that the API is enabled."
            elif x.status_code==404: hint="The configured Gemini model was not found. Set GEMINI_MODEL to gemini-2.5-flash in Render."
            elif x.status_code==429: hint="Gemini quota or rate limit reached. Check Google AI Studio usage and billing/quota."
            else: hint="Check the Gemini API key, model setting, quota, and Render logs."
            return jsonify(ok=False,error="Gemini could not generate a reply.",details=str(detail)[:300],help=hint),502
        j=x.json(); candidates=j.get("candidates") or []
        if not candidates: return jsonify(ok=False,error="Gemini returned no answer. Check safety settings or quota and try again."),502
        answer="".join(part.get("text","") for part in candidates[0].get("content",{}).get("parts",[]) if part.get("text"))
        if not answer.strip(): return jsonify(ok=False,error="Gemini returned an empty answer. Please try again."),502
        return jsonify(ok=True,text=answer)
    except requests.Timeout:return jsonify(ok=False,error="Gemini took too long to respond. Please try again."),504
    except requests.RequestException as e:
        app.logger.exception("Gemini network request failed")
        return jsonify(ok=False,error="Render could not reach the Gemini API. Check Render logs and outbound connectivity."),502
    except Exception:
        app.logger.exception("Unexpected Gemini response error")
        return jsonify(ok=False,error="Gemini returned an unexpected response. Check Render logs."),502

@app.get("/api/settings")
@paid
def get_settings():return jsonify(ok=True,settings=db().one("SELECT * FROM settings WHERE user_id=?",(user()["id"],)) or {})

@app.put("/api/settings")
@paid
def put_settings():
    u=user();d=request.get_json(silent=True) or {};db().execute("INSERT INTO settings(user_id,business_name,business_email,phone,website,industry,description,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET business_name=excluded.business_name,business_email=excluded.business_email,phone=excluded.phone,website=excluded.website,industry=excluded.industry,description=excluded.description,updated_at=excluded.updated_at",(u["id"],d.get("business_name",""),d.get("business_email",u["email"]),d.get("phone",""),d.get("website",""),d.get("industry",""),d.get("description",""),iso()));return jsonify(ok=True)

@app.get("/api/reviews")
def reviews():
    actual = db().all("SELECT name,rating,review,created_at FROM reviews ORDER BY id DESC LIMIT 30")
    # Only reviews actually submitted by signed-in customers are shown.
    return jsonify(ok=True, reviews=actual)

@app.get("/api/review-status")
def review_status():
    u = user()
    if not u:
        return jsonify(ok=True, authenticated=False, submitted=False)
    prior = db().one("SELECT id FROM reviews WHERE user_id=? LIMIT 1", (u["id"],))
    return jsonify(ok=True, authenticated=True, submitted=bool(prior))

@app.post("/api/reviews")
def review():
    u = user()
    if not u:
        return jsonify(ok=False,error="Please log in to submit a customer review."),401
    prior = db().one("SELECT id FROM reviews WHERE user_id=? LIMIT 1", (u["id"],))
    if prior:
        return jsonify(ok=False,error="You've already submitted a review. Thank you!"),409
    d=request.get_json(silent=True) or {}
    n=str(u.get("name") or d.get("name") or "").strip()
    r=str(d.get("review") or "").strip()
    try: rating=max(1,min(5,int(d.get("rating",5))))
    except (TypeError,ValueError): rating=5
    if not n or not r:
        return jsonify(ok=False,error="Please enter your review before submitting."),400
    db().execute("INSERT INTO reviews(user_id,name,email,rating,review,created_at) VALUES(?,?,?,?,?,?)",(u["id"],n,u["email"],rating,r,iso()))
    return jsonify(ok=True,message="Thank you! Your review has been saved. Your review reminder is now permanently disabled for your account.",submitted=True)

@app.post("/api/contact")
def contact():
    d=request.get_json(silent=True) or {};payload=json.dumps({"name":d.get("name",""),"email":d.get("email",""),"message":d.get("message","")});db().execute("INSERT INTO webhook_events(event_key,reference,email,payload,created_at) VALUES(?,?,?,?,?)",("contact:"+secrets.token_hex(10),"contact",email(d.get("email")),payload,iso()));return jsonify(ok=True,message=f"Message received. For urgent help email {CONTACT_EMAIL}.")

@app.post("/api/webhooks/selar")
def selar_webhook():
    raw=request.get_data(cache=True);sig=request.headers.get("X-Selar-Signature","");secret=os.getenv("SELAR_WEBHOOK_SECRET","").strip()
    if secret:
        expected=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
        if not sig or not hmac.compare_digest(expected,sig):return jsonify(ok=False,error="Invalid webhook signature."),401
    try:p=request.get_json(silent=True) or json.loads(raw.decode())
    except Exception:return jsonify(ok=False,error="Invalid JSON."),400
    x=webhook_data(p);e=x["email"];plan=x["plan"];ref=x["reference"] or secrets.token_hex(12)
    if not e or not plan:return jsonify(ok=False,error="Buyer email or plan could not be determined from the Selar event.",received=x),400
    if any(z in x["status"] for z in ("failed","cancelled","canceled","refunded","reversed")):return jsonify(ok=True,message="Non-successful event ignored.")
    key="selar:"+ref
    if db().one("SELECT id FROM webhook_events WHERE event_key=?",(key,)):return jsonify(ok=True,message="Event already processed.")
    u=db().one("SELECT id FROM users WHERE lower(email)=?",(e,));s=activate(e,plan,ref,u["id"] if u else None)
    db().execute("INSERT INTO webhook_events(event_key,reference,email,payload,created_at) VALUES(?,?,?,?,?)",(key,ref,e,json.dumps(p),iso()))
    return jsonify(ok=True,message="Subscription activated.",subscription=sub_json(s))

@app.post("/api/creator/login")
def creator_login():
    d=request.get_json(silent=True) or {};e=email(d.get("email"));p=str(d.get("password",""))
    if not CREATOR_PASSWORD:return jsonify(ok=False,error="Set CREATOR_PASSWORD in Render Environment first."),503
    if e!=CREATOR_EMAIL or not hmac.compare_digest(p,CREATOR_PASSWORD):return jsonify(ok=False,error="Invalid creator credentials."),401
    session.clear();session["creator"]=True;session["creator_email"]=e;session["creator_last_login"]=iso();return jsonify(ok=True,last_login=session["creator_last_login"])

@app.post("/api/creator/logout")
def creator_logout():session.clear();return jsonify(ok=True)

@app.get("/api/creator/customers")
@creator
def customers():
    rows=db().all("SELECT s.id,u.id AS user_id,COALESCE(u.name,(SELECT h.customer_name FROM creator_code_history h WHERE lower(h.email)=lower(s.email) AND h.customer_name<>'' ORDER BY h.id DESC LIMIT 1),'Not registered yet') AS name,COALESCE(u.business,'—') AS business,s.email,COALESCE(u.created_at,s.created_at) AS created_at,s.plan,s.access_code,s.starts_at,s.expires_at,s.status,s.selar_reference FROM subscriptions s LEFT JOIN users u ON u.id=s.user_id ORDER BY s.id DESC")
    history=db().all("SELECT id,email,plan,access_code,expires_at,status,email_sent,created_at,customer_name,created_by,ip_address,email_status FROM creator_code_history ORDER BY id DESC LIMIT 200")
    # Revenue is an estimate from currently recorded Selar-linked subscription records; manual codes are excluded.
    paid_rows=[x for x in rows if x.get("selar_reference") and not str(x.get("selar_reference")).startswith("CREATOR-")]
    revenue=sum(float(PLANS.get(x.get("plan"),{}).get("price",0)) for x in paid_rows)
    active_rows=[x for x in rows if x.get("status")=="active" and dt(x.get("expires_at")) and dt(x.get("expires_at"))>now()]
    mrr=sum(float(PLANS.get(x.get("plan"),{}).get("price",0))/(1 if x.get("plan")=="monthly" else 6 if x.get("plan")=="six_months" else 12) for x in active_rows)
    expiring=sum(1 for x in active_rows if (dt(x.get("expires_at"))-now()).total_seconds()<=3*86400)
    return jsonify(ok=True,customers=rows,history=history,metrics={"revenue_estimate":round(revenue,2),"active":len(active_rows),"expired":sum(1 for x in rows if x.get("status")=="expired"),"expiring_3_days":expiring,"mrr_estimate":round(mrr,2)},last_login=session.get("creator_last_login"))

@app.post("/api/creator/generate-code")
@creator
def creator_generate_code():
    d=request.get_json(silent=True) or {}; e=email(d.get("email"))
    # Codes may only be generated for an existing subscription record. This avoids
    # turning an unverified checkout redirect into a paid subscription.
    s=db().one("SELECT * FROM subscriptions WHERE lower(email)=? ORDER BY id DESC LIMIT 1",(e,))
    if not s: return jsonify(ok=False,error="No subscription record exists yet. Check the Selar purchase before manually approving it."),404
    code=s.get("access_code")
    if not code:
        code=make_code(); db().execute("UPDATE subscriptions SET access_code=? WHERE id=?",(code,s["id"]))
    return jsonify(ok=True,email=e,access_code=code,plan=PLANS.get(s["plan"],{}).get("name",s["plan"]),status=s["status"],expires_at=s["expires_at"],message="Only send this after confirming the actual purchase in Selar.")

@app.post("/api/creator/issue-customer-code")
@creator
def creator_issue_customer_code():
    """Creator issues a personal access code for the entered email and selected plan."""
    d=request.get_json(silent=True) or {}
    e=email(d.get("email")); plan=plan_value(d.get("plan") or "monthly"); customer_name=str(d.get("name") or "").strip()[:120]
    if not e or "@" not in e:
        return jsonify(ok=False,error="Enter the customer's valid email address."),400
    if not plan:
        return jsonify(ok=False,error="Choose a valid subscription plan."),400
    u=db().one("SELECT id FROM users WHERE lower(email)=?",(e,))
    if u and customer_name: db().execute("UPDATE users SET name=? WHERE id=?",(customer_name,u["id"]))
    old=db().one("SELECT * FROM subscriptions WHERE lower(email)=? ORDER BY id DESC LIMIT 1",(e,))
    start=now()
    if old and old.get("status")=="active":
        previous=dt(old.get("expires_at"))
        if previous and previous>start:
            start=previous
    exp=start+timedelta(days=PLANS[plan]["days"])
    old_code=str(old.get("access_code") or "") if old else ""
    # A revoked/disabled code must NEVER be reused or emailed again.
    if old and (old.get("status") in ("disabled", "revoked") or old_code.upper().startswith("REVOKED-")):
        code=make_code()
    else:
        code=old_code or make_code()
    # Record this as a Creator-issued code, not as a verified Selar webhook payment.
    ref="CREATOR-ISSUED-"+secrets.token_hex(5).upper()
    if old:
        db().execute("UPDATE subscriptions SET user_id=?,plan=?,access_code=?,starts_at=?,expires_at=?,status='active',selar_reference=? WHERE id=?",(u["id"] if u else old.get("user_id"),plan,code,iso(),exp.isoformat(),ref,old["id"]))
    else:
        db().execute("INSERT INTO subscriptions(user_id,email,plan,access_code,starts_at,expires_at,status,selar_reference,created_at) VALUES(?,?,?,?,?,?,'active',?,?)",(u["id"] if u else None,e,plan,code,iso(),exp.isoformat(),ref,iso()))
    sent, email_message = send_access_code_email(e, code, PLANS[plan]["name"], exp.isoformat())
    db().execute("INSERT INTO creator_code_history(email,plan,access_code,expires_at,status,email_sent,created_at,customer_name,created_by,ip_address,email_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(e,plan,code,exp.isoformat(),"active",1 if sent else 0,iso(),customer_name,"Creator",request.headers.get("X-Forwarded-For",request.remote_addr or "")[:120],"Accepted by email provider" if sent else "Failed / not sent"))
    db().execute("INSERT INTO webhook_events(event_key,reference,email,payload,created_at) VALUES(?,?,?,?,?)",
        ("creator-code:"+secrets.token_hex(10), ref, e, json.dumps({"type":"creator_code_issued","plan":plan,"amount":0,"source":"Creator","email_sent":sent,"email_status":"Accepted by provider" if sent else "Failed / not sent"}), iso()))
    return jsonify(ok=True,email=e,access_code=code,plan=PLANS[plan]["name"],expires_at=exp.isoformat(),existing_user=bool(u),email_sent=sent,email_message=email_message,message="Customer code created. " + email_message)

@app.post("/api/creator/resend-email")
@creator
def creator_resend_email():
    d=request.get_json(silent=True) or {}; e=email(d.get("email")); code=str(d.get("access_code") or "").strip()
    s=db().one("SELECT * FROM subscriptions WHERE lower(email)=? ORDER BY id DESC LIMIT 1",(e,))
    if not e or not s or (code and s.get("access_code")!=code):
        return jsonify(ok=False,error="The current access code for this customer could not be verified."),404
    plan=s.get("plan") if s.get("plan") in PLANS else "monthly"
    sent, detail=send_access_code_email(e,s.get("access_code"),PLANS[plan]["name"],s.get("expires_at"))
    status="Accepted by email provider" if sent else "Failed / not sent"
    db().execute("UPDATE creator_code_history SET email_sent=?,email_status=? WHERE email=? AND access_code=?",(1 if sent else 0,status,e,s.get("access_code")))
    db().execute("INSERT INTO webhook_events(event_key,reference,email,payload,created_at) VALUES(?,?,?,?,?)",("creator-resend:"+secrets.token_hex(8),"ACCESS-CODE-RESEND",e,json.dumps({"type":"access_code_email_resend","source":"Creator","email_status":status}),iso()))
    return jsonify(ok=sent,message=("Email request accepted by provider. This does not guarantee inbox delivery." if sent else "Email could not be sent. Check the email service configuration and Render logs."))

@app.post("/api/creator/delete-code")
@creator
def creator_delete_code():
    d=request.get_json(silent=True) or {}
    e=email(d.get("email")); code=str(d.get("access_code") or "").strip()
    sid=d.get("subscription_id")
    if not e:
        return jsonify(ok=False,error="Customer email is required."),400
    if sid:
        s=db().one("SELECT * FROM subscriptions WHERE id=? AND lower(email)=?",(sid,e))
    elif code:
        s=db().one("SELECT * FROM subscriptions WHERE lower(email)=? AND access_code=? ORDER BY id DESC LIMIT 1",(e,code))
    else:
        s=db().one("SELECT * FROM subscriptions WHERE lower(email)=? ORDER BY id DESC LIMIT 1",(e,))
    if not s:
        return jsonify(ok=False,error="That subscription was not found. Refresh the Creator page and try again."),404
    old_code=str(s.get("access_code") or "")
    revoked_code="REVOKED-"+secrets.token_hex(12).upper()
    db().execute("UPDATE subscriptions SET access_code=?,status='disabled' WHERE id=?",(revoked_code,s["id"]))
    db().execute("UPDATE creator_code_history SET status='revoked' WHERE lower(email)=lower(?) AND access_code=?",(e,old_code))
    stamp=iso()
    db().execute(
        "INSERT INTO webhook_events(event_key,reference,email,payload,created_at,received_at) VALUES(?,?,?,?,?,?)",
        ("creator-code-deleted:"+secrets.token_hex(10),"CREATOR-CODE-REVOKED",e,
         json.dumps({"type":"creator_code_deleted","subscription_id":s["id"]}),stamp,stamp)
    )
    return jsonify(ok=True,message="Subscription revoked successfully.",subscription_id=s["id"])

@app.get("/api/creator/events")
@creator
def creator_events():
    rows=db().all("SELECT event_key,reference,email,payload,created_at FROM webhook_events ORDER BY id DESC LIMIT 100")
    events=[]
    for x in rows:
        try: payload=json.loads(x.get("payload") or "{}")
        except Exception: payload={"details":str(x.get("payload") or "")}
        typ=payload.get("type") or ("Payment event" if str(x.get("event_key","")).startswith("selar:") else "CRM event")
        plan=payload.get("plan")
        amount=payload.get("amount")
        if amount is None and plan in PLANS: amount=PLANS[plan]["price"] if str(x.get("event_key","")).startswith("selar:") else None
        events.append({**x,"details":payload,"event":typ,"amount":amount,"source":payload.get("source") or ("Selar Webhook" if str(x.get("event_key","")).startswith("selar:") else "CRM"),"status":payload.get("email_status") or ("Recorded" if x.get("event_key") else "Unknown")})
    return jsonify(ok=True,events=events,last_login=session.get("creator_last_login"))

@app.post("/api/creator/clear-logs")
@creator
def creator_clear_logs():
    """Clear event diagnostics and revoked-code history only; retain active customers and users."""
    d=request.get_json(silent=True) or {}
    clear_events=bool(d.get("events", True))
    clear_revoked=bool(d.get("revoked", True))
    if not clear_events and not clear_revoked:
        return jsonify(ok=False,error="Choose at least one log type to clear."),400
    if clear_events:
        db().execute("DELETE FROM webhook_events")
    if clear_revoked:
        db().execute("DELETE FROM creator_code_history WHERE lower(status)='revoked' OR upper(access_code) LIKE 'REVOKED-%'")
        db().execute("DELETE FROM subscriptions WHERE lower(status) IN ('disabled','revoked') OR upper(access_code) LIKE 'REVOKED-%'")
    return jsonify(ok=True,message="Selected diagnostic logs and revoked-code records cleared. Active customers and accounts were kept.")

@app.post("/api/creator/test-webhook")
@creator
def creator_test_webhook():
    # Diagnostic only: does not simulate a paid order or grant access.
    configured=bool(os.getenv("SELAR_WEBHOOK_SECRET", "").strip())
    return jsonify(ok=True,configured=configured,message="Webhook endpoint is reachable. This diagnostic does not create a payment or grant a subscription.",endpoint=request.host_url.rstrip("/")+"/api/webhooks/selar")

@app.post("/api/creator/extend")
@creator
def extend():
    d=request.get_json(silent=True) or {};e=email(d.get("email"));p=plan_value(d.get("plan"));s=db().one("SELECT * FROM subscriptions WHERE lower(email)=? ORDER BY id DESC LIMIT 1",(e,))
    if not s or not p:return jsonify(ok=False,error="Customer subscription or plan not found."),400
    current=now(); expiry=dt(s.get("expires_at"));start=expiry if expiry and expiry>current else current
    new_exp=start+timedelta(days=PLANS[p]["days"])
    db().execute("UPDATE subscriptions SET expires_at=?,status='active' WHERE id=?",(new_exp.isoformat(),s["id"]))
    db().execute("INSERT INTO webhook_events(event_key,reference,email,payload,created_at) VALUES(?,?,?,?,?)",("creator-extend:"+secrets.token_hex(8),"MANUAL-EXTENSION",e,json.dumps({"type":"subscription_extended","plan":p,"source":"Creator","days_added":PLANS[p]["days"]}),iso()))
    return jsonify(ok=True,expires_at=new_exp.isoformat(),message="Subscription expiry extended.")

@app.post("/api/creator/disable")
@creator
def disable():
    d=request.get_json(silent=True) or {}
    e=email(d.get("email")); sid=d.get("subscription_id")
    if not e: return jsonify(ok=False,error="Customer email is required."),400
    if sid:
        result=db().execute("UPDATE subscriptions SET status='disabled' WHERE id=? AND lower(email)=?",(sid,e))
    else:
        result=db().execute("UPDATE subscriptions SET status='disabled' WHERE id=(SELECT id FROM subscriptions WHERE lower(email)=? ORDER BY id DESC LIMIT 1)",(e,))
    return jsonify(ok=True,message="Customer access deactivated.",subscription_id=sid)

@app.post("/api/creator/activate")
@creator
def admin_activate():
    d=request.get_json(silent=True) or {}; e=email(d.get("email")); sid=d.get("subscription_id")
    s=db().one("SELECT * FROM subscriptions WHERE id=? AND lower(email)=?",(sid,e)) if sid else db().one("SELECT * FROM subscriptions WHERE lower(email)=? ORDER BY id DESC LIMIT 1",(e,))
    if not s: return jsonify(ok=False,error="No subscription found for this customer."),404
    exp=dt(s.get("expires_at")); current=now()
    if not exp or exp <= current:
        # Reactivating an expired account also grants a fresh period matching its existing plan.
        plan=s.get("plan") if s.get("plan") in PLANS else "monthly"
        start=current; exp=start+timedelta(days=PLANS[plan]["days"])
        db().execute("UPDATE subscriptions SET starts_at=?,expires_at=?,status='active' WHERE id=?",(start.isoformat(),exp.isoformat(),s["id"]))
    else:
        db().execute("UPDATE subscriptions SET status='active' WHERE id=?",(s["id"],))
    return jsonify(ok=True,message="Customer access activated.",subscription=sub_json(sub_for(e)))

HTML=r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TIMILEYINGROWTHCRM</title><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Playfair+Display:wght@600;700;800&display=swap" rel="stylesheet"><style>
*{box-sizing:border-box}body{margin:0;font-family:Inter,Arial,sans-serif;background:#FAFAF9;color:#0F0F0F}a{color:#0F0F0F}button{cursor:pointer}.wrap{width:min(1150px,92%);margin:auto}.nav{height:68px;display:flex;align-items:center;justify-content:space-between}.brand{font-weight:900;font-family:Inter,Arial,sans-serif;letter-spacing:.2px}.brand i{color:#D4AF37;font-style:normal}.top{position:sticky;top:0;background:#FAFAF9f5;border-bottom:1px solid #EAEAEA;z-index:9}.links{display:flex;gap:16px;align-items:center}.links a{color:#0F0F0F;text-decoration:none;font-size:14px}.hero{text-align:center;padding:80px 0}.hero h1{font-family:'Playfair Display',Georgia,serif;font-size:clamp(42px,7vw,76px);line-height:1;margin:20px auto;max-width:900px}.grad{color:#D4AF37}.hero p,.muted{color:#555555;line-height:1.7}.badge{display:inline-block;border:1px solid #EAEAEA;border-radius:999px;padding:7px 11px;color:#0F0F0F;background:#F1F1EF;font-size:12px;font-weight:800}.btn{border:0;border-radius:11px;padding:12px 17px;background:#111111;color:#FFFFFF;font-weight:800}.ghost{background:#FAFAF9;color:#111111;border:1px solid #EAEAEA}.actions{display:flex;justify-content:center;gap:10px;flex-wrap:wrap}.section{padding:55px 0}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:17px}.card{background:#0F0F0F;color:#FAFAF9;border:1px solid #EAEAEA;border-radius:16px;padding:32px}.card p{color:#D0D0CE;line-height:1.6}.locked{position:relative;overflow:hidden;opacity:.8}.locked:after{content:'Premium — subscribe to unlock';position:absolute;inset:0;background:#0F0F0Fee;display:flex;align-items:center;justify-content:center;font-weight:900;color:#D4AF37}.pricing .card{display:flex;flex-direction:column}.price{font-size:42px;font-weight:900;color:#D4AF37}.pricecard ul{color:#D0D0CE;line-height:2;padding-left:20px}.pricecard .btn{margin-top:auto}.featured{border-color:#D4AF37;transform:translateY(-4px)}.reviews{grid-template-columns:repeat(3,1fr)}.stars{color:#D4AF37}.footer{border-top:1px solid #EAEAEA;padding:35px 0;color:#555555}.modal{display:none;position:fixed;inset:0;background:#0009;z-index:30;align-items:center;justify-content:center;padding:15px}.modal.on{display:flex}.box{width:min(650px,100%);max-height:92vh;overflow:auto;background:#FAFAF9;color:#0F0F0F;border:1px solid #EAEAEA;border-radius:16px;padding:32px}.close{float:right;background:none;border:0;color:#0F0F0F;font-size:25px}.form{display:grid;grid-template-columns:1fr 1fr;gap:12px}label{font-size:13px;color:#444444}input,textarea,select{width:100%;margin-top:5px;background:#1E1E1E;color:#FFFFFF;border:1px solid #2A2A2A;border-radius:10px;padding:12px}input:focus,textarea:focus,select:focus{border-color:#D4AF37;outline:none}input::placeholder,textarea::placeholder{color:#6B6B6B}textarea{min-height:110px}.full{grid-column:1/-1}.msg{display:none;background:#F1F1EF;color:#0F0F0F;padding:10px;border-radius:9px;margin:10px 0}.app{display:none}.app.on{display:block}.app .btn:not(.ghost){background:#D4AF37;color:#000;font-weight:700}.app .btn.ghost{background:transparent;color:#FFF;border:1px solid #2A2A2A}body.authenticated-workspace header.top{display:none!important}body.creator-mode{background:#0F0F0F!important;color:#F5F5F5!important}body.creator-mode #creatorPanel{background:#0F0F0F}.tabs{display:flex;gap:7px;flex-wrap:wrap;margin:20px 0}.tabs button{padding:9px 12px;background:#0F0F0F;border:1px solid #EAEAEA;color:#FAFAF9;border-radius:9px}.tabs .active{background:#D4AF37;color:#0F0F0F}.pane{display:none}.pane.on{display:block}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.kpi{background:#0F0F0F;color:#FAFAF9;border:1px solid #EAEAEA;border-radius:16px;padding:20px}.kpi b{font-size:28px;display:block;margin-top:7px;color:#D4AF37}.table,.table-wrapper{overflow-x:auto}table{width:100%;min-width:800px;border-collapse:collapse;font-size:13px}td{white-space:nowrap;max-width:150px;overflow:hidden;text-overflow:ellipsis}th,td{padding:10px;border-bottom:1px solid #EAEAEA;text-align:left;font-size:13px}th{color:#D4AF37}.small{font-size:12px;color:#777777}.admin-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:15px 0}.admin-stat{background:#0F0F0F;color:#FAFAF9;border:1px solid #EAEAEA;border-radius:16px;padding:20px}.admin-stat strong{display:block;font-size:25px;margin-top:6px;color:#D4AF37}.status-pill{display:inline-block;border-radius:999px;padding:5px 9px;font-size:11px;font-weight:800;text-transform:uppercase}.status-active{background:#e4f3e8;color:#176b35}.status-expired{background:#fff0d8;color:#754b00}.status-disabled{background:#fde7e7;color:#8a1e1e}.admin-actions{display:flex;gap:6px;flex-wrap:wrap}.admin-actions .btn{padding:8px 10px;font-size:12px}.countdown{font-variant-numeric:tabular-nums;color:#D4AF37;font-weight:800}.expiry-alert{border:1px solid #D4AF37;background:#fff8df;color:#4c3b00;border-radius:12px;padding:13px;margin:12px 0}.expiry-alert.urgent{border-color:#a53c52;background:#fde7e7;color:#8a1e1e}@media(max-width:520px){.admin-summary{grid-template-columns:1fr}}@media(max-width:800px){.grid,.reviews,.kpis{grid-template-columns:1fr 1fr}.links{display:none}}@media(max-width:520px){.grid,.reviews,.kpis,.form{grid-template-columns:1fr}.full{grid-column:auto}.hero{padding:55px 0}.hero h1{font-size:44px}}

.creator-shell{width:min(1250px,94%);margin:0 auto 50px;color:#F5F5F5}.creator-shell,.creator-shell *{box-sizing:border-box}.creator-shell h1,.creator-shell h2,.creator-shell h3{color:#fff}.creator-shell h1{font-size:clamp(27px,4vw,40px);margin:5px 0}.creator-shell h2{font-size:20px;margin:0 0 8px}.creator-shell p{margin:6px 0;color:#bdbdbd;line-height:1.55}.creator-top,.creator-section-head{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}.creator-eyebrow{font-size:11px;letter-spacing:.14em;font-weight:800;color:#D4AF37}.creator-top{margin-bottom:20px}.creator-top p{max-width:720px}.creator-shell .creator-card{background:#1A1A1A;border:1px solid #2A2A2A;border-radius:16px;padding:22px;margin:16px 0}.creator-shell .master-card{display:flex;justify-content:space-between;align-items:center;gap:15px;flex-wrap:wrap;background:#0F0F0F;border:1px solid #D4AF37;border-radius:16px;padding:22px;margin:16px 0}.master-card h3{margin:8px 0}.master-badge{border:1px solid #2A2A2A;border-radius:999px;padding:9px 12px;font-size:11px;color:#D4AF37;font-weight:800}.creator-btn{border:1px solid #2A2A2A;border-radius:10px;padding:11px 14px;background:#fff;color:#111;font-weight:800;cursor:pointer}.creator-btn.gold{background:#D4AF37;border-color:#D4AF37;color:#111}.creator-btn.secondary{background:#1A1A1A;color:#fff;border-color:#444}.creator-form{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;align-items:end;margin-top:16px}.creator-shell label{display:block;color:#d0d0d0;font-size:12px;font-weight:700}.creator-shell input,.creator-shell select{width:100%;min-width:0;background:#0F0F0F;color:#fff;border:1px solid #383838;border-radius:10px;padding:12px;margin-top:6px;font:inherit}.creator-shell .creator-muted{color:#aaa;font-size:13px}.creator-submit{display:flex}.creator-submit .creator-btn{width:100%}.creator-result{margin-top:14px;padding:15px;border-radius:12px;background:#0F0F0F;border:1px solid #333;color:#fff}.creator-result button{margin:7px 6px 0 0}.creator-filters{display:grid;grid-template-columns:minmax(0,1fr) 190px;gap:10px;margin:15px 0}.creator-shell .admin-summary{grid-template-columns:repeat(5,minmax(0,1fr))}.creator-shell .admin-stat{background:#0F0F0F;border:1px solid #2A2A2A}.creator-shell .admin-stat strong{color:#D4AF37}.creator-shell .table{border:1px solid #2A2A2A;border-radius:12px}.creator-shell table{min-width:720px}.creator-shell th,.creator-shell td{border-bottom:1px solid #2A2A2A;color:#e7e7e7}.creator-shell th{color:#D4AF37}.creator-shell .status-pill{white-space:nowrap}.creator-shell .small{color:#aaa}.creator-private-note{color:#ff7777!important;font-size:11px!important}.creator-tools{display:flex;gap:8px;flex-wrap:wrap}.creator-footer{padding:18px 3px;color:#aaa;font-size:12px}.creator-footer a{color:#D4AF37}.creator-event-row{display:grid;grid-template-columns:150px 1.2fr 1.5fr 90px 120px 130px;gap:10px;padding:12px;border-bottom:1px solid #2A2A2A;font-size:12px;overflow-wrap:anywhere}.creator-event-head{color:#D4AF37;font-weight:800}.creator-code-hidden{letter-spacing:2px}.creator-reveal{background:transparent;border:1px solid #444;color:#D4AF37;border-radius:7px;padding:4px 7px;cursor:pointer}.creator-customer-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.creator-customer{background:#0F0F0F;border:1px solid #2A2A2A;border-radius:13px;padding:16px;min-width:0}.creator-customer-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.creator-customer strong,.creator-customer span{overflow-wrap:anywhere}.creator-customer .creator-actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:14px}.creator-customer .creator-actions button{font-size:11px;padding:8px 9px}.creator-shell .status-active{background:#d9fbe5;color:#11632c}.creator-shell .status-expired{background:#fff0d8;color:#754b00}.creator-shell .status-disabled{background:#fde7e7;color:#8a1e1e}
@media(max-width:900px){.creator-form{grid-template-columns:repeat(2,minmax(0,1fr))}.creator-shell .admin-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.creator-event-row{grid-template-columns:1fr 1fr}.creator-event-head{display:none}}
@media(max-width:560px){.creator-form,.creator-filters,.creator-customer-grid{grid-template-columns:1fr}.creator-shell .admin-summary{grid-template-columns:1fr 1fr}.creator-shell .creator-card{padding:15px}.creator-top .creator-btn{width:100%}.creator-event-row{grid-template-columns:1fr}.creator-event-row span:before{font-weight:800;color:#D4AF37;display:inline-block;margin-right:8px}}

.pricecard{border-radius:16px;padding:32px;box-shadow:0 2px 20px rgba(0,0,0,.03);transition:transform .3s,box-shadow .3s}
.pricecard:hover{transform:translateY(-10px);box-shadow:0 20px 40px rgba(0,0,0,.12)}
.pricecard .price-cta{display:block;width:100%;box-sizing:border-box;margin:24px 0 10px;padding:16px 32px;border-radius:12px;background:#FFD700;color:#000;font-weight:800;box-shadow:0 4px 15px rgba(255,215,0,.4);transition:transform .3s,background .3s}
.pricecard .price-cta:hover{background:#fff;color:#000;transform:scale(1.05)}
.pricecard small{display:block;text-align:center;opacity:.8}.pricecard .save-note{font-weight:800;color:#D4AF37}
.pricecard.featured{background:#1E40AF;color:#fff}.pricecard.featured .badge{color:#000;background:#FFD700}.pricecard.featured .save-note{color:#fff}
.pricecard .price span{font-size:.45em;font-weight:600}
</style></head><body>
<header class="top"><div class="wrap nav"><div class="brand">TIMILEYIN<i>GROWTHCRM</i></div><div class="links"><a href="#features">Features</a><a href="#pricing">Pricing</a><a href="#reviews">Reviews</a><a href="#contact">Support</a><button class="btn ghost" onclick="modal('login')">Login</button><button class="btn ghost" onclick="location.href='/register'">Register Account</button></div></div></header>
<main id="landing"><section class="hero"><div class="wrap"><span class="badge">AI-POWERED CUSTOMER GROWTH PLATFORM</span><h1>Turn leads into <span class="grad">paying customers.</span></h1><p>One workspace for leads, pipeline, AI sales coaching, website audits, appointments, follow-ups and business analytics.</p><div class="actions"><button class="btn" onclick="document.getElementById('pricing').scrollIntoView({behavior:'smooth'})">Start Growing</button><button class="btn ghost" onclick="modal('login')">I already have an account</button></div><p class="small">Private customer accounts • Subscription protected • AI-assisted sales</p></div></section>
<section class="section" id="features"><div class="wrap"><h2>Everything your business needs</h2><p class="muted">Premium tools are locked until a subscription is active.</p><div class="grid"><div class="card locked"><h3>👥 Leads & Customers</h3><p>Contacts, business details, stages, scores, notes and history.</p></div><div class="card locked"><h3>📊 Sales Pipeline</h3><p>Track opportunities from New to Won with deal values.</p></div><div class="card locked"><h3>🤖 AI Sales Coach</h3><p>Generate tailored outreach, follow-ups and sales strategies.</p></div><div class="card locked"><h3>🌐 Website Audit</h3><p>Check response speed, mobile viewport, forms, booking signals and links.</p></div><div class="card locked"><h3>📅 Appointments</h3><p>Schedule meetings and customer follow-ups.</p></div><div class="card locked"><h3>📈 Analytics</h3><p>See leads, hot prospects, pipeline, revenue and conversion.</p></div></div></div></section>
<section class="section" id="pricing"><div class="wrap"><h2>Choose your plan</h2><p class="muted">Each button opens its matching Selar checkout.</p><div class="grid pricing"><div class="card pricecard"><span class="badge">STARTER</span><h3>Starter</h3><div class="price">$29<span>/month</span></div><p>1 Month · 3 Sub-Accounts</p><ul><li>CRM &amp; Pipelines</li><li>Booking Calendars</li><li>Forms, Surveys &amp; Quizzes</li><li>Email Marketing</li><li>Payments &amp; Invoicing</li><li>Dashboards &amp; Domain Management</li></ul><button class="btn price-cta" onclick="buy('monthly')">Get Started →</button><small>Instant access after payment confirmation</small></div><div class="card pricecard featured"><span class="badge">POPULAR</span><h3>Popular</h3><div class="price">$129</div><p>6 Months · Only $21.50/month</p><p class="save-note">Save $45 · MOST POPULAR</p><ul><li>Everything in Starter</li><li>Website &amp; Funnel Builder</li><li>Unified Conversations</li><li>Workflow Automation</li><li>Social Media &amp; Reputation Management</li><li>Courses, Communities &amp; Mobile App</li><li>Unlimited Contacts</li></ul><button class="btn price-cta" onclick="buy('six_months')">Get Started →</button><small>Instant access after payment confirmation</small></div><div class="card pricecard"><span class="badge">BEST VALUE</span><h3>Best Value</h3><div class="price">$199</div><p>1 Year · Only $16.50/month</p><p class="save-note">Save $149</p><ul><li>Everything in Popular</li><li>Unlimited Sub-Accounts</li><li>Conversation, Content &amp; Funnel AI*</li><li>Reviews &amp; Voice AI*</li><li>Inbound/outbound calling*</li><li>Prospecting &amp; Ad Manager</li><li>Advanced API &amp; SaaS Mode</li></ul><button class="btn price-cta" onclick="buy('yearly')">Get Started →</button><small>Instant access after payment confirmation</small></div></div></div></section>
<section class="section" id="reviews"><div class="wrap"><h2>Reviews & feedback</h2><p class="muted">Only genuine customer-submitted feedback appears here.</p><div id="reviewList" class="grid reviews"></div><button class="btn ghost" onclick="modal('review')">Leave a Review</button></div></section>
<section class="section" id="contact"><div class="wrap"><div class="card"><h2>Need help?</h2><p>For account, payment or CRM issues, email <a href="mailto:supporttimilehingrowth@gmail.com?subject=TIMILEYINGROWTH%20CRM%20Support" style="color:#D4AF37;text-decoration:underline;font-weight:800">supporttimilehingrowth@gmail.com</a>.</p><a class="btn" href="mailto:supporttimilehingrowth@gmail.com?subject=TIMILEYINGROWTH%20CRM%20Support" style="display:inline-block;text-decoration:none">Email Support</a></div></div></section></main>

<div class="modal" id="login"><div class="box"><button class="close" onclick="closeModal('login')">×</button><h2>Welcome back</h2><p class="muted">Log in to your account.</p><div id="loginMsg" class="msg"></div><div class="form"><div class="full"><label>Email</label><input id="le" type="email"></div><div class="full"><label>Password</label><input id="lp" type="password"></div><div class="full"><button class="btn" onclick="login()">Login</button><button type="button" class="btn ghost" style="margin-top:8px" onclick="forgotPassword()">Forgot Password?</button></div></div><hr style="border-color:#203b58"><button class="btn ghost" onclick="location.href='/register'">Create account</button></div></div>
<div class="modal" id="register"><div class="box"><button class="close" onclick="closeModal('register')">×</button><h2>Create your account</h2><p class="muted">Use your subscription email and current access code. If this email already has an account, this securely updates its password and signs you in.</p><div id="regMsg" class="msg"></div><div class="form"><div><label>Name</label><input id="rn"></div><div><label>Business</label><input id="rb"></div><div class="full"><label>Email</label><input id="re" type="email"></div><div class="full"><label>Your personal subscription code</label><input id="rc" autocomplete="off" placeholder="TGR-XXXXXX-XXXXXX"></div><div class="full"><label>Password (8+ characters)</label><input id="rp" type="password"></div><div class="full"><button class="btn" onclick="register()">Create Account</button></div></div></div></div>
<div class="modal" id="review"><div class="box"><button class="close" onclick="closeModal('review')">×</button><h2>Leave a review</h2><div id="reviewMsg" class="msg"></div><div class="form"><div><label>Name</label><input id="rvn"></div><div><label>Email</label><input id="rve"></div><div><label>Rating</label><select id="rvr"><option>5</option><option>4</option><option>3</option><option>2</option><option>1</option></select></div><div class="full"><label>Review</label><textarea id="rvt"></textarea></div><div class="full"><button class="btn" onclick="reviewSubmit()">Publish Review</button></div></div></div></div>
<div class="modal" id="contact"><div class="box"><button class="close" onclick="closeModal('contact')">×</button><h2>Contact Support</h2><p class="muted">Or email <b>supporttimilehingrowth@gmail.com</b> directly.</p><div class="form"><div><label>Name</label><input id="cn"></div><div><label>Email</label><input id="ce"></div><div class="full"><label>Message</label><textarea id="ct"></textarea></div><div class="full"><button class="btn" onclick="contactSend()">Send</button></div></div></div></div>

<div class="modal" id="creatorLogin"><div class="box"><button class="close" onclick="closeModal('creatorLogin')">×</button><h2>Creator Command Center</h2><p class="muted">Private creator access. Sign in with your authorised creator email and Render-configured password.</p><div id="creatorMsg" class="msg"></div><div class="form"><div class="full"><label>Creator email</label><input id="creatorEmail" type="email" autocomplete="username" value="dayotimileyin831@gmail.com"></div><div class="full"><label>Creator password</label><input id="creatorPassword" type="password" autocomplete="current-password"></div><div class="full"><button class="btn" onclick="creatorLogin()">Enter Command Center</button></div></div></div></div>
<div id="creatorPanel" class="creator-shell" style="display:none;padding-top:25px"><div class="creator-top"><div><div class="creator-eyebrow">PRIVATE CONTROL ROOM</div><h1>Creator Command Center</h1><p>Manage customer access, subscriptions, email status and recorded system events.</p></div><button class="creator-btn secondary" onclick="creatorLogout()">Exit Command Center</button></div><div class="master-card"><div><span class="creator-eyebrow">MASTER ACCESS</span><h3>Creator access is protected</h3><p>Never share your creator login or access codes. The password remains in Render environment variables.</p></div><div class="master-badge">● SECURE SESSION</div></div><div class="creator-card"><h2>Generate customer access</h2><p class="creator-muted">Issue a code only after confirming payment or for a deliberate manual grant. Existing payment and access-code flows remain unchanged.</p><div class="creator-form"><div><label for="testEmail">Customer email</label><input id="testEmail" type="email" placeholder="customer@example.com"></div><div><label for="testPlan">Subscription plan</label><select id="testPlan"><option value="monthly">1 Month — $29</option><option value="six_months">6 Months — $129</option><option value="yearly">1 Year — $199</option></select></div><div class="creator-submit"><button class="creator-btn gold" onclick="createTestCode()">Generate access code</button></div></div><div id="testCodeResult" class="creator-result" style="display:none;overflow-wrap:anywhere"></div></div><div class="creator-card"><div class="creator-section-head"><div><h2>Customers & subscriptions</h2><p class="creator-muted">Search, filter, manage and export customer records.</p></div><button class="creator-btn secondary" onclick="exportCreatorCustomers()">Export CSV</button></div><div class="creator-filters"><input id="creatorSearch" placeholder="Search name, email or access code" oninput="renderCreatorCustomers()"><select id="creatorFilter" onchange="renderCreatorCustomers()"><option value="all">All statuses</option><option value="active">Active</option><option value="expired">Expired</option><option value="lifetime">Lifetime</option><option value="disabled">Disabled</option></select></div><div id="creatorContent">Loading customers…</div></div><div class="creator-card"><div class="creator-section-head"><div><h2>All Access Codes Log</h2><p class="creator-private-note">PRIVATE — Never share this log. Access codes are sensitive.</p></div><button class="creator-btn secondary" onclick="clearCreatorLogs('revoked')">Clear revoked-code history</button></div><div id="creatorHistory"></div></div><div class="creator-card"><div class="creator-section-head"><div><h2>Recent payment & support events</h2><p class="creator-muted">Recorded webhook and CRM events. This is not a live payment-provider delivery receipt.</p></div><div class="creator-tools"><button class="creator-btn secondary" onclick="loadCreatorEvents()">Refresh events</button><button class="creator-btn secondary" onclick="clearCreatorLogs('events')">Clear event logs</button><button class="creator-btn gold" onclick="testSelarWebhook()">Test Selar Webhook</button></div></div><div id="creatorEvents"></div><p id="webhookTestResult" class="creator-muted" aria-live="polite"></p></div><div class="creator-footer">Last creator login: <span id="creatorLastLogin">This session</span> · Support: <a href="mailto:supporttimilehingrowth@gmail.com?subject=TIMILEYINGROWTH%20CRM%20Support">supporttimilehingrowth@gmail.com</a></div></div>
<div id="appui" class="app"><header class="top"><div class="wrap nav"><div class="brand">TIMILEYIN<i>GROWTHCRM</i></div><button class="btn ghost" onclick="logout()">Logout</button></div></header><div class="wrap"><div class="card" style="margin-top:25px"><span class="badge">PRIVATE WORKSPACE</span><h2 id="welcome"></h2><p id="subtext" class="muted"></p><div id="expiryNotice" class="expiry-alert" style="display:none" role="status"></div><div class="card"><span class="small">ACCESS CODE</span><h3 id="code"></h3></div></div><div class="tabs"><button class="active" onclick="tab('dash',this)">Dashboard</button><button onclick="tab('leads',this)">Leads</button><button onclick="tab('pipeline',this)">Pipeline</button><button onclick="tab('ai',this)">AI Coach</button><button onclick="tab('audit',this)">Website Audit</button><button onclick="tab('appointments',this)">Appointments</button><button onclick="tab('followups',this)">Follow-ups</button><button onclick="tab('tasks',this)">Tasks</button><button onclick="tab('settings',this)">Settings</button></div>
<section id="dash" class="pane on"><div class="kpis"><div class="kpi">Leads<b id="k1">0</b></div><div class="kpi">Hot<b id="k2">0</b></div><div class="kpi">Pipeline<b id="k3">$0</b></div><div class="kpi">Revenue<b id="k4">$0</b></div></div><div class="grid" style="margin-top:15px"><div class="card"><h3>Conversion</h3><b id="k5">0%</b></div><div class="card"><h3>Appointments</h3><b id="k6">0</b></div><div class="card"><h3>Subscription</h3><p id="k7" class="muted"></p></div></div><div class="card" id="dailyReviewPrompt" style="margin-top:16px;border-color:#39708b;display:none"><h3>⭐ Share your feedback</h3><p class="muted">Please share your experience with TIMILEYINGROWTH CRM. We will remind you once per day until you submit a review.</p><div class="actions" style="justify-content:flex-start"><button class="btn" onclick="openReviewFromWorkspace()">Write a review</button><button class="btn ghost" onclick="dismissReviewToday()">Not today</button></div></div></section>
<section id="leads" class="pane"><div class="card"><h2>Leads</h2><div class="form"><div><label>Name</label><input id="ln"></div><div><label>Company</label><input id="lc"></div><div><label>Email</label><input id="lm"></div><div><label>Phone</label><input id="lp2"></div><div><label>Website</label><input id="lw"></div><div><label>Deal value</label><input id="lv" type="number"></div><div><label>Stage</label><select id="ls"><option>New</option><option>Contacted</option><option>Qualified</option><option>Proposal</option><option>Negotiation</option><option>Won</option><option>Lost</option></select></div><div><label>Score</label><input id="lsc" type="number" value="0"></div><div class="full"><label>Notes</label><textarea id="lnt"></textarea></div><div class="full"><button class="btn" onclick="addLead()">Add Lead</button></div></div></div><div class="card" style="margin-top:15px"><div class="table"><table><thead><tr><th>Name</th><th>Company</th><th>Stage</th><th>Score</th><th>Value</th><th></th></tr></thead><tbody id="lt"></tbody></table></div></div></section>
<section id="pipeline" class="pane"><div class="card"><h2>Sales Pipeline</h2><div id="pipe" class="grid"></div></div></section>
<section id="ai" class="pane"><div class="card"><h2>🤖 AI Sales Coach</h2><div class="form"><div><label>Your business</label><input id="ab"></div><div><label>Prospect</label><input id="ap"></div><div><label>Service</label><input id="as"></div><div><label>Channel</label><select id="ac"><option>Email</option><option>WhatsApp</option><option>LinkedIn</option></select></div><div class="full"><label>Goal</label><input id="ag" value="Get a reply and start a conversation"></div><div class="full"><button class="btn" onclick="aiRun()">Generate</button></div></div><div id="ao" class="card" style="display:none;margin-top:15px;white-space:pre-wrap"></div></div></section>
<section id="audit" class="pane"><div class="card"><h2>🌐 Website Audit</h2><div class="form"><div class="full"><label>Website URL</label><input id="au" placeholder="https://example.com"></div><div class="full"><button class="btn" onclick="auditRun()">Run Audit</button></div></div><div id="auditOut" style="margin-top:15px"></div></div></section>
<section id="appointments" class="pane"><div class="card"><h2>📅 Appointments</h2><div class="form"><div><label>Title</label><input id="apt"></div><div><label>Date/time</label><input id="apd" type="datetime-local"></div><div class="full"><label>Notes</label><textarea id="apn"></textarea></div><div class="full"><button class="btn" onclick="addAppt()">Save</button></div></div><div id="apl" style="margin-top:15px"></div></div></section>
<section id="followups" class="pane"><div class="card"><h2>🔔 Follow-ups</h2><div class="form"><div><label>Due</label><input id="fd" type="datetime-local"></div><div><label>Channel</label><select id="fc"><option>Email</option><option>WhatsApp</option><option>Phone</option></select></div><div class="full"><label>Message</label><textarea id="fm"></textarea></div><div class="full"><button class="btn" onclick="addFollow()">Schedule</button></div></div><div id="fl" style="margin-top:15px"></div></div></section>
<section id="tasks" class="pane"><div class="card"><h2>✅ Tasks</h2><div class="form"><div><label>Task</label><input id="tt"></div><div><label>Priority</label><select id="tp"><option>High</option><option>Medium</option><option>Low</option></select></div><div><label>Due</label><input id="td" type="datetime-local"></div><div><button class="btn" style="margin-top:21px" onclick="addTask()">Add</button></div></div><div id="tl" style="margin-top:15px"></div></div></section>
<section id="settings" class="pane"><div class="card"><h2>⚙️ Business Settings</h2><div class="form"><div><label>Business</label><input id="sb"></div><div><label>Email</label><input id="se"></div><div><label>Phone</label><input id="sp"></div><div><label>Website</label><input id="sw"></div><div><label>Industry</label><input id="si"></div><div class="full"><label>Description</label><textarea id="sd"></textarea></div><div class="full"><button class="btn" onclick="saveSettings()">Save business details</button></div></div></div><div class="card" style="margin-top:16px"><h2>📅 Public appointment booking</h2><p class="muted">Create a public link customers can use without logging in. Bookings are saved to this workspace and create a new lead.</p><div class="form"><div class="full"><label>Booking link name</label><input id="bslug" placeholder="your-business-name"></div><div><label>Opening time</label><input id="bstart" type="time" value="09:00"></div><div><label>Closing time</label><input id="bend" type="time" value="17:00"></div><div><label>Appointment duration</label><select id="bduration"><option value="15">15 minutes</option><option value="30">30 minutes</option><option value="60">60 minutes</option></select></div><div><label>Booking status</label><select id="benabled"><option value="true">Enabled</option><option value="false">Disabled</option></select></div><div class="full"><label>Working days</label><div style="display:flex;gap:12px;flex-wrap:wrap"><label style="display:flex;gap:5px;align-items:center;font-weight:500"><input type="checkbox" class="bday" value="0"  style="width:auto">{day}</label><label style="display:flex;gap:5px;align-items:center;font-weight:500"><input type="checkbox" class="bday" value="1" checked style="width:auto">{day}</label><label style="display:flex;gap:5px;align-items:center;font-weight:500"><input type="checkbox" class="bday" value="2" checked style="width:auto">{day}</label><label style="display:flex;gap:5px;align-items:center;font-weight:500"><input type="checkbox" class="bday" value="3" checked style="width:auto">{day}</label><label style="display:flex;gap:5px;align-items:center;font-weight:500"><input type="checkbox" class="bday" value="4" checked style="width:auto">{day}</label><label style="display:flex;gap:5px;align-items:center;font-weight:500"><input type="checkbox" class="bday" value="5" checked style="width:auto">{day}</label><label style="display:flex;gap:5px;align-items:center;font-weight:500"><input type="checkbox" class="bday" value="6"  style="width:auto">{day}</label></div></div><div class="full"><button class="btn" onclick="saveBookingSettings()">Save booking settings</button><p class="muted">Put this link in your Instagram bio.</p><div id="bookingLinkResult" class="msg" style="display:block">Save your settings to create your public booking link.</div><div class="actions" style="justify-content:flex-start"><button class="btn" type="button" onclick="copyBookingLink()">Copy Booking Link</button><button class="btn ghost" type="button" onclick="previewBookingLink()">Preview</button></div></div></div></div></section></div></div></div>
<footer class="footer"><div class="wrap">TIMILEYINGROWTH CRM • Support: <a href="mailto:supporttimilehingrowth@gmail.com">supporttimilehingrowth@gmail.com</a></div></footer>
<script>
const PL={monthly:{url:'https://selar.com/f167zzc299',name:'1 Month'},six_months:{url:'https://selar.com/1212127215',name:'6 Months'},yearly:{url:'https://selar.com/56r7979796',name:'1 Year'}};let U=null;
function modal(id){document.getElementById(id).classList.add('on')}function closeModal(id){document.getElementById(id).classList.remove('on')}function msg(id,t){let x=document.getElementById(id);x.textContent=t;x.style.display='block'}function esc(x){return String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')}
async function api(u,o={}){let r=await fetch(u,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...o});let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw Error([d.error,d.help,d.details].filter(Boolean).join(' ' )||'Request failed');return d}
function buy(p){location.href=PL[p].url}
function showPlans(){closeModal('login');closeModal('register');document.getElementById('pricing').scrollIntoView({behavior:'smooth'});}
async function register(){try{let d=await api('/api/register',{method:'POST',body:JSON.stringify({name:rn.value,business:rb.value,email:re.value,password:rp.value,access_code:rc.value})});U=d.user;closeModal('register');if(d.user.subscription&&d.user.subscription.active){await openApp()}else{msg('regMsg','Account created. We could not yet confirm an active subscription for this email. Make sure you used the same email at Selar and that the Selar webhook is configured. Choose a plan below if you have not purchased yet.');showPlans();}}catch(e){msg('regMsg',e.message)}}
async function forgotPassword(){const e=(le.value||'').trim();if(!e){msg('loginMsg','Enter your account email first, then tap Forgot Password?.');return}try{const d=await api('/api/password/forgot',{method:'POST',body:JSON.stringify({email:e})});msg('loginMsg',d.message||'If an account exists for that email, a reset link will be sent shortly. Check inbox and spam.')}catch(x){msg('loginMsg',x.message)}}
async function login(){try{let d=await api('/api/login',{method:'POST',body:JSON.stringify({email:le.value,password:lp.value})});U=d.user;closeModal('login');if(!d.user.subscription.active){const st=d.user.subscription.status||'inactive';const expired=st==='expired';alert(expired?'Your subscription has expired. Your CRM data has not been deleted, but CRM tools are locked until you renew. Choose a plan below and pay on Selar using this same account email. After Selar confirms payment, access should reactivate automatically when the payment confirmation reaches this app. If access does not return after confirmation, contact supporttimilehingrowth@gmail.com.':'Your account does not currently have an active subscription. If you have already paid, allow time for payment confirmation. Choose a plan below only if you still need to purchase or renew. For help, contact supporttimilehingrowth@gmail.com.');showPlans();return}await openApp()}catch(e){msg('loginMsg',e.message)}}
async function logout(){await api('/api/logout',{method:'POST'});location.reload()}
let expiryTicker=null, expiryWarned={};
function maskAccessCode(value){const raw=String(value||'');const parts=raw.split('-');if(parts.length>=3)return parts.slice(0,2).join('-')+'-••••••';if(raw.length>6)return raw.slice(0,6)+'••••••';return '••••••'}
function fmtCountdown(seconds){seconds=Math.max(0,Math.floor(seconds||0));let d=Math.floor(seconds/86400),h=Math.floor((seconds%86400)/3600),m=Math.floor((seconds%3600)/60),sec=seconds%60;return d+'d '+String(h).padStart(2,'0')+'h '+String(m).padStart(2,'0')+'m '+String(sec).padStart(2,'0')+'s'}
function paintExpiry(){if(!U||!U.subscription)return;let s=U.subscription,el=document.getElementById('expiryNotice');if(!s.active){el.style.display='block';el.className='expiry-alert urgent';el.textContent='Your subscription is inactive or expired. Your CRM tools are locked. If you have renewed, contact support or wait for verification.';subtext.textContent='Subscription '+s.status;return}let seconds=Math.max(0,Math.floor((new Date(s.expires_at).getTime()-Date.now())/1000));s.seconds_remaining=seconds;s.days_remaining=Math.floor(seconds/86400);subtext.textContent=s.plan_name+' • Time remaining: '+fmtCountdown(seconds);k7.textContent=s.plan_name+' • '+fmtCountdown(seconds);if(seconds<=7*86400){el.style.display='block';el.className='expiry-alert'+(seconds<=86400?' urgent':'');el.textContent=seconds<=86400?'⚠️ Your subscription expires in '+fmtCountdown(seconds)+'. Please renew now to avoid losing access.':'Reminder: your subscription expires in '+fmtCountdown(seconds)+'. Renew early to avoid interruption.';}else{el.style.display='none'}if(seconds<=0){el.style.display='block';el.className='expiry-alert urgent';el.textContent='Your subscription has expired. Your CRM tools are locked, but your saved data remains. To reactivate, choose a plan on the subscription page and pay through Selar using this same account email. Access returns after payment confirmation. If it does not reactivate, contact supporttimilehingrowth@gmail.com.';setTimeout(()=>location.reload(),2500)}}
async function openApp(){let d=await api('/api/me');if(!d.authenticated||!d.user.subscription.active)return;U=d.user;landing.style.display='none';document.querySelector('header.top').style.display='none';document.body.classList.add('authenticated-workspace');appui.classList.add('on');welcome.textContent='Welcome, '+U.name;code.textContent=maskAccessCode(U.subscription.access_code);paintExpiry();if(expiryTicker)clearInterval(expiryTicker);expiryTicker=setInterval(paintExpiry,1000);showDailyReviewPrompt();await Promise.all([dash(),loadLeads(),appts(),follows(),tasks(),settings()])}
async function dash(){let d=(await api('/api/dashboard')).dashboard;k1.textContent=d.total_leads;k2.textContent=d.hot_leads;k3.textContent='$'+Number(d.pipeline_value).toLocaleString();k4.textContent='$'+Number(d.revenue).toLocaleString();k5.textContent=d.conversion_rate+'%';k6.textContent=d.appointments}
async function loadLeads(){let a=(await api('/api/leads')).leads;lt.innerHTML='';let s={};a.forEach(x=>{s[x.stage]=(s[x.stage]||0)+1;lt.innerHTML+=`<tr><td>${esc(x.name)}</td><td>${esc(x.company)}</td><td>${esc(x.stage)}</td><td>${x.score}</td><td>$${Number(x.deal_value||0).toLocaleString()}</td><td><button class="btn" onclick="delLead(${x.id})">Delete</button></td></tr>`});pipe.innerHTML=Object.entries(s).map(([k,v])=>`<div class="card"><h3>${esc(k)}</h3><b style="font-size:34px">${v}</b><p>Lead(s)</p></div>`).join('')}
async function addLead(){try{await api('/api/leads',{method:'POST',body:JSON.stringify({name:ln.value,company:lc.value,email:lm.value,phone:lp2.value,website:lw.value,deal_value:lv.value,stage:ls.value,score:lsc.value,notes:lnt.value})});await loadLeads();await dash()}catch(e){alert(e.message)}}async function delLead(i){if(confirm('Delete this lead?')){await api('/api/leads/'+i,{method:'DELETE'});await loadLeads();await dash()}}
async function aiRun(){ao.style.display='block';ao.textContent='Generating...';try{ao.textContent=(await api('/api/ai/sales-coach',{method:'POST',body:JSON.stringify({business:ab.value,prospect:ap.value,service:as.value,channel:ac.value,goal:ag.value})})).text}catch(e){ao.textContent=e.message}}
async function auditRun(){auditOut.innerHTML='<div class="card">Auditing...</div>';try{let a=(await api('/api/website-audit',{method:'POST',body:JSON.stringify({url:au.value})})).audit;auditOut.innerHTML=`<div class="card"><h3>${esc(a.title||'Website Audit')}</h3><p>Reachable: <b>${a.reachable?'Yes':'No'}</b></p><p>Status: ${a.status_code||'—'} • Response: ${a.response_ms||'—'} ms</p><p>Mobile viewport: ${a.mobile_viewport?'Detected':'Not detected'} • HTTPS: ${a.https?'Yes':'No'} • Booking: ${a.booking_features?'Detected':'Not detected'}</p><p>Forms: ${a.forms} • Links checked: ${a.links_checked}</p><h4>Issues</h4><ul>${a.issues.map(x=>'<li>'+esc(x)+'</li>').join('')||'<li>No major automated issue found.</li>'}</ul></div>`}catch(e){auditOut.textContent=e.message}}
async function addAppt(){try{await api('/api/appointments',{method:'POST',body:JSON.stringify({title:apt.value,appointment_at:apd.value,notes:apn.value})});await appts();await dash()}catch(e){alert(e.message)}}async function appts(){let a=(await api('/api/appointments')).appointments;apl.innerHTML=a.map(x=>`<div class="card" style="margin-bottom:8px"><b>${esc(x.title)}</b><br><span class="small">${esc(x.appointment_at)}</span></div>`).join('')||'<p class="muted">No appointments.</p>'}
async function addFollow(){await api('/api/followups',{method:'POST',body:JSON.stringify({due_at:fd.value,channel:fc.value,message:fm.value})});await follows()}async function follows(){let a=(await api('/api/followups')).followups;fl.innerHTML=a.map(x=>`<div class="card" style="margin-bottom:8px"><b>${esc(x.channel)}</b> • ${esc(x.due_at)}<br>${esc(x.message||'')}</div>`).join('')||'<p class="muted">No follow-ups.</p>'}
async function addTask(){await api('/api/tasks',{method:'POST',body:JSON.stringify({title:tt.value,due_at:td.value,priority:tp.value})});await tasks()}async function tasks(){let a=(await api('/api/tasks')).tasks;tl.innerHTML=a.map(x=>`<div class="card" style="margin-bottom:8px"><b>${esc(x.title)}</b> • ${esc(x.priority)}<br>${esc(x.due_at||'')}</div>`).join('')||'<p class="muted">No tasks.</p>'}
async function settings(){let s=(await api('/api/settings')).settings||{};sb.value=s.business_name||'';se.value=s.business_email||'';sp.value=s.phone||'';sw.value=s.website||'';si.value=s.industry||'';sd.value=s.description||'';try{let b=await api('/api/booking-settings');b=b.booking;bslug.value=b.slug||'';bstart.value=b.start_time||'09:00';bend.value=b.end_time||'17:00';bduration.value=String(b.duration||30);benabled.value=String(!!b.enabled);document.querySelectorAll('.bday').forEach(x=>x.checked=(b.working_days||'1,2,3,4,5').split(',').includes(x.value));bookingLinkResult.dataset.url=(window.PUBLIC_BOOKING_BASE||location.origin)+'/book/'+b.slug;bookingLinkResult.textContent='Booking link ready. Use Copy Booking Link to copy it or Preview to open it.'}catch(e){bookingLinkResult.textContent='Booking settings could not load: '+e.message}}async function saveSettings(){await api('/api/settings',{method:'PUT',body:JSON.stringify({business_name:sb.value,business_email:se.value,phone:sp.value,website:sw.value,industry:si.value,description:sd.value})});alert('Business details saved.')}async function saveBookingSettings(){try{const days=[...document.querySelectorAll('.bday:checked')].map(x=>Number(x.value));const d=await api('/api/booking-settings',{method:'PUT',body:JSON.stringify({slug:bslug.value,start_time:bstart.value,end_time:bend.value,duration:Number(bduration.value),enabled:benabled.value==='true',working_days:days})});bookingLinkResult.dataset.url=d.url;bookingLinkResult.textContent='Booking link saved. Use Copy Booking Link to copy it or Preview to open it.';bookingLinkResult.style.display='block'}catch(e){bookingLinkResult.textContent=e.message;bookingLinkResult.style.display='block'}}
function currentBookingLink(){return bookingLinkResult.dataset.url||((window.PUBLIC_BOOKING_BASE||location.origin)+'/book/'+(bslug.value||''))}
async function copyBookingLink(){const url=currentBookingLink();if(!url||url.endsWith('/book/')){alert('Save your booking settings first.');return}try{await navigator.clipboard.writeText(url);alert('Booking link copied.')}catch(e){prompt('Copy your booking link:',url)}}
function previewBookingLink(){const url=currentBookingLink();if(!url||url.endsWith('/book/')){alert('Save your booking settings first.');return}window.open(url,'_blank','noopener')}
function tab(id,b){document.querySelectorAll('.pane').forEach(x=>x.classList.remove('on'));document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));document.getElementById(id).classList.add('on');b.classList.add('active')}
function reviewCards(a,all=false){const shown=all?a:a.slice(0,3);return shown.map(x=>`<div class="card"><div class="stars">${'★'.repeat(Number(x.rating)||5)}</div><h3>${esc(x.name)}</h3><span class="badge">CUSTOMER REVIEW</span><p>“${esc(x.review)}”</p></div>`).join('')+(a.length>3?`<div class="full"><button class="btn ghost" type="button" onclick="toggleMoreReviews(this)">View more reviews (${a.length-3} more)</button></div>`:'')}
function toggleMoreReviews(btn){const box=btn.closest('.review-area');if(!box)return;const all=JSON.parse(box.dataset.reviews||'[]');box.innerHTML=reviewCards(all,true);box.dataset.expanded='true'}
function fillReviewArea(el,a){if(!el)return;el.classList.add('review-area');el.dataset.reviews=JSON.stringify(a);el.dataset.expanded='false';el.innerHTML=reviewCards(a,false)}
async function loadReviews(){try{let a=(await api('/api/reviews')).reviews;fillReviewArea(reviewList,a)}catch(e){reviewList.innerHTML='<div class="card">Reviews are temporarily unavailable.</div>'}}
async function loadWorkspaceReviews(){return;}
function openReviewFromWorkspace(){if(!U){modal('login');return}rvn.value=U.name||'';rve.value=U.email||'';modal('review')}
function todayKey(){return new Date().getFullYear()+'-'+String(new Date().getMonth()+1).padStart(2,'0')+'-'+String(new Date().getDate()).padStart(2,'0')}
function reviewDismissKey(){return 'tgr-review-dismiss-'+(U?U.id||U.email:'guest')}
async function showDailyReviewPrompt(){let el=document.getElementById('dailyReviewPrompt');if(!el||!U)return;try{let status=(await api('/api/review-status'));if(status.submitted){el.style.display='none';return}el.style.display=localStorage.getItem(reviewDismissKey())===todayKey()?'none':'block'}catch(e){el.style.display=localStorage.getItem(reviewDismissKey())===todayKey()?'none':'block'}}
function dismissReviewToday(){localStorage.setItem(reviewDismissKey(),todayKey());showDailyReviewPrompt()}
async function reviewSubmit(){try{let d=await api('/api/reviews',{method:'POST',body:JSON.stringify({name:rvn.value,email:rve.value,rating:rvr.value,review:rvt.value})});msg('reviewMsg',d.message);await loadReviews();if(U){localStorage.removeItem(reviewDismissKey());document.getElementById('dailyReviewPrompt').style.display='none';rvt.value='';}}catch(e){msg('reviewMsg',e.message)}}
async function contactSend(){let d=await api('/api/contact',{method:'POST',body:JSON.stringify({name:cn.value,email:ce.value,message:ct.value})});alert(d.message)}
let creatorCustomersCache=[];function jsAttr(v){return JSON.stringify(String(v??'')).replaceAll('\"','&quot;')}
async function creatorLogin(){try{await api('/api/creator/login',{method:'POST',body:JSON.stringify({email:creatorEmail.value,password:creatorPassword.value})});closeModal('creatorLogin');landing.style.display='none';document.querySelector('header.top').style.display='none';document.body.classList.add('authenticated-workspace','creator-mode');appui.classList.remove('on');document.getElementById('creatorPanel').style.display='block';await loadCreatorCustomers();await loadCreatorEvents();}catch(e){msg('creatorMsg',e.message)}}
async function creatorLogout(){try{await api('/api/creator/logout',{method:'POST'})}catch(e){}document.getElementById('creatorPanel').style.display='none';document.body.classList.remove('creator-mode','authenticated-workspace');location.href='/'}
async function createTestCode(){let out=document.getElementById('testCodeResult');out.style.display='block';out.textContent='Generating access code…';try{let d=await api('/api/creator/issue-customer-code',{method:'POST',body:JSON.stringify({email:document.getElementById('testEmail').value,plan:document.getElementById('testPlan').value})});const loginLink=location.origin+'/';out.innerHTML='<b>ACCESS CODE CREATED</b><br>Email: '+esc(d.email)+'<br>Plan: '+esc(d.plan)+'<br>Expires: '+esc(d.expires_at)+'<br><b class="creator-code-value">'+esc(d.access_code)+'</b><br><p>'+esc(d.email_sent?'Email accepted by provider; delivery is not guaranteed.':(d.email_message||'Email was not sent.'))+'</p><button class="creator-btn gold" onclick="copyCreatorText('+jsAttr(d.access_code)+')">Copy Code</button><button class="creator-btn secondary" onclick="copyCreatorText('+jsAttr(loginLink)+')">Copy Login Link</button><button class="creator-btn secondary" onclick="resendCreatorEmail('+jsAttr(d.email)+','+jsAttr(d.access_code)+')">Resend Email</button>';await loadCreatorCustomers();await loadCreatorEvents()}catch(e){out.textContent=e.message}}
async function copyCreatorText(value){try{await navigator.clipboard.writeText(value);alert('Copied.')}catch(e){prompt('Copy this text:',value)}}
async function resendCreatorEmail(e,c){try{let d=await api('/api/creator/resend-email',{method:'POST',body:JSON.stringify({email:e,access_code:c})});alert(d.message||'Email request processed.');await loadCreatorCustomers()}catch(x){alert(x.message)}}
async function deleteIssuedCode(emailValue,codeValue){if(!confirm('Revoke this access code for '+emailValue+'?'))return;try{let d=await api('/api/creator/delete-code',{method:'POST',body:JSON.stringify({email:emailValue,access_code:codeValue})});alert(d.message);await loadCreatorCustomers();await loadCreatorEvents()}catch(e){alert(e.message)}}
async function loadCreatorCustomers(){try{let d=await api('/api/creator/customers');creatorCustomersCache=d.customers||[];const m=d.metrics||{};document.getElementById('creatorLastLogin').textContent=d.last_login||'Current session';const root=document.getElementById('creatorContent');root.innerHTML='<div class="admin-summary"><div class="admin-stat">Revenue estimate<strong>$'+Number(m.revenue_estimate||0).toFixed(2)+'</strong></div><div class="admin-stat">Active<strong>'+Number(m.active||0)+'</strong></div><div class="admin-stat">Expired<strong>'+Number(m.expired||0)+'</strong></div><div class="admin-stat">Expiring in 3 days<strong>'+Number(m.expiring_3_days||0)+'</strong></div><div class="admin-stat">MRR estimate<strong>$'+Number(m.mrr_estimate||0).toFixed(2)+'</strong></div></div><div id="creatorCustomerList"></div>';renderCreatorCustomers();renderCreatorHistory(d.history||[]);startAdminCountdowns()}catch(e){document.getElementById('creatorContent').innerHTML='<div class="creator-result">'+esc(e.message)+'</div>'}}
function renderCreatorCustomers(){const host=document.getElementById('creatorCustomerList');if(!host)return;const q=(document.getElementById('creatorSearch')?.value||'').trim().toLowerCase(),f=document.getElementById('creatorFilter')?.value||'all';const rows=creatorCustomersCache.filter(x=>{const s=x.status||'inactive',exp=dtSafe(x.expires_at),active=s==='active'&&exp>Date.now(),label=active?'active':s==='disabled'?'disabled':'expired';const match=![x.name,x.email,x.access_code].some(v=>String(v||'').toLowerCase().includes(q))?false:true;const filt=f==='all'||(f==='lifetime'&&x.plan==='lifetime')||(f==='active'&&active)||(f==='expired'&&!active&&s!=='disabled')||(f==='disabled'&&s==='disabled');return match&&filt});if(!rows.length){host.innerHTML='<p class="creator-muted">No customers match this search.</p>';return}host.innerHTML='<div class="creator-customer-grid">'+rows.map(x=>{const exp=dtSafe(x.expires_at),sec=exp?Math.max(0,Math.floor((exp-Date.now())/1000)):0,active=x.status==='active'&&exp>Date.now(),label=active?'Active':x.status==='disabled'?'Disabled':'Expired',code=String(x.access_code||'—');return '<article class="creator-customer"><div class="creator-customer-head"><div><strong>'+esc(x.name||'Not registered yet')+'</strong><div class="creator-muted">'+esc(x.email)+'</div></div><span class="status-pill status-'+label.toLowerCase()+'">'+label+'</span></div><p>Plan: <b>'+esc(x.plan||'—')+'</b></p><p>Expiry: '+esc(formatExpiry(x.expires_at))+'</p><p>Time remaining: <b class="countdown" data-expiry="'+esc(x.expires_at||'')+'">'+(sec?fmtCountdown(sec):'Expired / no expiry')+'</b></p><p>Access code: <span class="creator-code-hidden" data-code="'+esc(code)+'">••••••••••</span> <button class="creator-reveal" onclick="revealCreatorCode(this)">◉ Reveal</button></p><div class="creator-actions"><button class="creator-btn gold" onclick="adminExtend('+jsAttr(x.email)+',\'monthly\')">Extend +1 Month</button><button class="creator-btn secondary" onclick="adminExtend('+jsAttr(x.email)+',\'six_months\')">Extend +6 Months</button><button class="creator-btn secondary" onclick="adminToggle('+jsAttr(x.email)+','+jsAttr(label)+','+Number(x.id)+')">'+(active?'Deactivate':'Activate')+'</button><button class="creator-btn secondary" onclick="deleteCustomerCode('+jsAttr(x.email)+','+jsAttr(code)+','+Number(x.id)+')">Revoke</button><button class="creator-btn secondary" type="button" disabled title="Impersonation is not enabled yet">View Workspace (not enabled)</button></div></article>'}).join('')+'</div>'}
function dtSafe(v){const n=v?new Date(v).getTime():0;return Number.isFinite(n)?n:0}
function startAdminCountdowns(){document.querySelectorAll('[data-expiry]').forEach(el=>{if(el.dataset.countdownStarted)return;el.dataset.countdownStarted='1';const update=()=>{const diff=dtSafe(el.dataset.expiry)-Date.now();if(!dtSafe(el.dataset.expiry)){el.textContent='No expiry';return}if(diff<=0){el.textContent='Expired';return}el.textContent=fmtCountdown(Math.floor(diff/1000))};update();setInterval(update,1000)})}
function formatExpiry(value){const n=dtSafe(value);return n?new Date(n).toLocaleString('en-US',{month:'short',day:'numeric',year:'numeric',hour:'2-digit',minute:'2-digit'}):'No expiry'}
function revealCreatorCode(btn){const el=btn.parentElement.querySelector('[data-code]');if(!el)return;const revealed=el.textContent!=='••••••••••';el.textContent=revealed?'••••••••••':el.dataset.code;btn.textContent=revealed?'◉ Reveal':'◉ Hide'}
async function deleteCustomerCode(e,c,id){if(!confirm('Revoke this subscription and invalidate its access code for '+e+'?'))return;try{const d=await api('/api/creator/delete-code',{method:'POST',body:JSON.stringify({email:e,access_code:c,subscription_id:id})});alert(d.message||'Subscription revoked.');await loadCreatorCustomers();await loadCreatorEvents()}catch(err){alert('Revoke failed: '+err.message)}}
function renderCreatorHistory(history){const host=document.getElementById('creatorHistory');if(!host)return;host.innerHTML='<div class="table"><table><thead><tr><th>Created</th><th>Name / Email</th><th>Plan</th><th>Access Code</th><th>Created by</th><th>IP</th><th>Email status</th><th>Status</th><th>Action</th></tr></thead><tbody>'+history.map(h=>'<tr><td>'+esc(h.created_at||'—')+'</td><td>'+esc(h.customer_name||'—')+'<br>'+esc(h.email)+'</td><td>'+esc(h.plan)+'</td><td>'+esc(maskAccessCode(h.access_code))+'</td><td>'+esc(h.created_by||'Creator')+'</td><td>'+esc(h.ip_address||'Not captured')+'</td><td>'+esc(h.email_status||(h.email_sent?'Accepted by provider':'Failed / not sent'))+'</td><td>'+esc(h.status)+'</td><td>'+(h.status==='active'?'<button class="creator-btn secondary" onclick="deleteIssuedCode('+jsAttr(h.email)+','+jsAttr(h.access_code)+')">Revoke</button>':'—')+'</td></tr>').join('')+'</tbody></table></div>'}
function exportCreatorCustomers(){const rows=creatorCustomersCache,cols=['name','email','business','plan','status','starts_at','expires_at','access_code','selar_reference'];const csv=[cols.join(','),...rows.map(r=>cols.map(k=>'"'+String(r[k]??'').replaceAll('"','""')+'"').join(','))].join('\r\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8;'}));a.download='timileyingrowthcrm-customers.csv';a.click();URL.revokeObjectURL(a.href)}
async function testSelarWebhook(){const out=document.getElementById('webhookTestResult');out.textContent='Checking webhook endpoint configuration…';try{const d=await api('/api/creator/test-webhook',{method:'POST',body:JSON.stringify({})});out.textContent=d.message+' Endpoint: '+d.endpoint+' Signature secret configured: '+(d.configured?'Yes':'No. Configure it if your Selar setup supports signature verification.')}catch(e){out.textContent=e.message}}
async function loadCreatorEvents(){try{let d=await api('/api/creator/events');const host=document.getElementById('creatorEvents');host.innerHTML='<div class="creator-event-row creator-event-head"><span>Time</span><span>Event</span><span>Email / Reference</span><span>Amount</span><span>Source</span><span>Status</span></div>'+(d.events||[]).map(x=>'<div class="creator-event-row"><span>'+esc(x.created_at||'—')+'</span><span>'+esc(x.event||x.reference||'Event')+'</span><span>'+esc(x.email||x.reference||'—')+'</span><span>'+(x.amount==null?'—':'$'+Number(x.amount).toFixed(2))+'</span><span>'+esc(x.source||'CRM')+'</span><span>'+esc(x.status||'Recorded')+'</span></div>').join('')||'<p class="creator-muted">No events have been recorded yet.</p>'}catch(e){document.getElementById('creatorEvents').textContent=e.message}}
async function clearCreatorLogs(kind){const label=kind==='events'?'event logs':'revoked-code history and disabled subscription records';if(!confirm('Permanently clear '+label+'? Active customer accounts and active subscriptions will be kept.'))return;try{const d=await api('/api/creator/clear-logs',{method:'POST',body:JSON.stringify(kind==='events'?{events:true,revoked:false}:{events:false,revoked:true})});alert(d.message||'Selected logs cleared.');await loadCreatorCustomers();await loadCreatorEvents()}catch(e){alert(e.message)}}
setInterval(()=>{if(document.getElementById('creatorPanel')?.style.display!=='none'&&document.getElementById('creatorPanel')?.style.display)loadCreatorEvents()},30000);
async function adminToggle(emailValue,label,subscriptionId){try{if(label==='Active'){if(!confirm('Deactivate CRM access for '+emailValue+'?'))return;const d=await api('/api/creator/disable',{method:'POST',body:JSON.stringify({email:emailValue,subscription_id:subscriptionId})});alert(d.message||'Customer access deactivated.')}else{if(!confirm('Activate access for '+emailValue+'? If the previous plan expired, this grants a fresh period matching that plan.'))return;const d=await api('/api/creator/activate',{method:'POST',body:JSON.stringify({email:emailValue,subscription_id:subscriptionId})});alert(d.message||'Customer access activated.')}await loadCreatorCustomers()}catch(e){alert('Subscription action failed: '+e.message)}}
async function adminGenerateCode(emailValue){try{let d=await api('/api/creator/generate-code',{method:'POST',body:JSON.stringify({email:emailValue})});alert('Customer: '+emailValue+'\nPlan: '+d.plan+'\nAccess code: '+d.access_code+'\n\nCopy this code and email it to the customer.')}catch(e){alert(e.message)}}
async function adminExtend(emailValue,plan){if(!confirm('Extend subscription for '+emailValue+'?'))return;try{await api('/api/creator/extend',{method:'POST',body:JSON.stringify({email:emailValue,plan})});await loadCreatorCustomers();alert('Subscription extended.')}catch(e){alert(e.message)}}

loadReviews();(async()=>{try{let d=await api('/api/me');if(d.authenticated&&d.user.subscription.active){U=d.user;openApp()}else if(location.pathname==='/welcome'){modal('register');}}catch(e){if(location.pathname==='/welcome')modal('register');}})();
</script></body></html>'''

@app.get("/api/booking-settings")
@paid
def get_booking_settings():
    u=user()
    b=db().one("SELECT * FROM booking_settings WHERE user_id=?",(u["id"],))
    if not b:
        base=re.sub(r"[^a-z0-9]+","-",(u.get("business") or u.get("name") or "business").lower()).strip("-") or "business"
        slug=base[:45]
        if db().one("SELECT user_id FROM booking_settings WHERE slug=?",(slug,)): slug=f"{slug}-{u['id']}"
        db().execute("INSERT INTO booking_settings(user_id,slug,updated_at) VALUES(?,?,?)",(u["id"],slug,iso()))
        b=db().one("SELECT * FROM booking_settings WHERE user_id=?",(u["id"],))
    return jsonify(ok=True,booking=b,url=(PUBLIC_BASE_URL or request.host_url.rstrip("/"))+"/book/"+b["slug"])

@app.put("/api/booking-settings")
@paid
def put_booking_settings():
    u=user(); d=request.get_json(silent=True) or {}
    slug=re.sub(r"[^a-z0-9-]+","-",str(d.get("slug") or "").lower()).strip("-")[:55]
    if len(slug)<3: return jsonify(ok=False,error="Choose a booking link name with at least 3 letters or numbers."),400
    try: duration=int(d.get("duration",30))
    except Exception: duration=30
    if duration not in (15,30,60): return jsonify(ok=False,error="Appointment duration must be 15, 30 or 60 minutes."),400
    start=str(d.get("start_time","09:00")); end=str(d.get("end_time","17:00"))
    try:
        sh,sm=map(int,start.split(':')); eh,em=map(int,end.split(':'))
        if not (0<=sh<24 and 0<=sm<60 and 0<=eh<24 and 0<=em<60 and eh*60+em>sh*60+sm): raise ValueError()
    except Exception: return jsonify(ok=False,error="Choose valid working hours; closing time must be after opening time."),400
    days=d.get("working_days",[1,2,3,4,5])
    if not isinstance(days,list): days=[1,2,3,4,5]
    days=sorted({int(x) for x in days if str(x).isdigit() and 0<=int(x)<=6})
    if not days: return jsonify(ok=False,error="Select at least one working day."),400
    enabled=1 if d.get("enabled",True) else 0
    try:
        db().execute("INSERT INTO booking_settings(user_id,slug,enabled,working_days,start_time,end_time,duration,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET slug=excluded.slug,enabled=excluded.enabled,working_days=excluded.working_days,start_time=excluded.start_time,end_time=excluded.end_time,duration=excluded.duration,updated_at=excluded.updated_at",(u["id"],slug,enabled,','.join(map(str,days)),start,end,duration,iso()))
    except Exception:
        return jsonify(ok=False,error="That booking link name is already in use. Choose another name."),409
    return jsonify(ok=True,message="Booking settings saved.",url=(PUBLIC_BASE_URL or request.host_url.rstrip("/"))+"/book/"+slug)

@app.get("/book/<slug>")
def public_booking(slug):
    b=db().one("SELECT * FROM booking_settings WHERE slug=? AND enabled=1",(slug,))
    if not b: return "<h1>Booking page not found</h1><p>This booking link may be disabled or incorrect.</p>",404
    owner=db().one("SELECT id,name,business FROM users WHERE id=?",(b["user_id"],))
    if not owner: return "<h1>Booking page unavailable</h1>",404
    settings=db().one("SELECT business_name,phone FROM settings WHERE user_id=?",(owner["id"],)) or {}
    return render_template_string(BOOKING_HTML,slug=slug,business=(settings.get("business_name") or owner.get("business") or owner.get("name")),phone=settings.get("phone") or "",duration=b["duration"])

@app.get("/api/public-booking/<slug>")
def public_booking_info(slug):
    b=db().one("SELECT * FROM booking_settings WHERE slug=? AND enabled=1",(slug,))
    if not b:return jsonify(ok=False,error="Booking link not found or disabled."),404
    owner=db().one("SELECT id,name,business FROM users WHERE id=?",(b["user_id"],))
    st=db().one("SELECT business_name,phone FROM settings WHERE user_id=?",(b["user_id"],)) or {}
    return jsonify(ok=True,business=st.get("business_name") or owner.get("business") or owner.get("name"),phone=st.get("phone") or "",duration=b["duration"],working_days=[int(x) for x in b["working_days"].split(',') if x.strip().isdigit()],start_time=b["start_time"],end_time=b["end_time"])

@app.post("/api/public-booking/<slug>")
def create_public_booking(slug):
    b=db().one("SELECT * FROM booking_settings WHERE slug=? AND enabled=1",(slug,))
    if not b:return jsonify(ok=False,error="Booking link not found or disabled."),404
    d=request.get_json(silent=True) or {}
    name=str(d.get("name") or "").strip()[:120]; phone=str(d.get("phone") or "").strip()[:60]; reason=str(d.get("reason") or "Appointment") .strip()[:500]
    raw=str(d.get("appointment_at") or "").strip()
    if not name or not phone or not raw:return jsonify(ok=False,error="Enter your name, phone number and choose an appointment time."),400
    try:
        when=datetime.fromisoformat(raw.replace('Z','+00:00'))
        if when.tzinfo is not None: when=when.astimezone().replace(tzinfo=None)
    except Exception:return jsonify(ok=False,error="Choose a valid appointment date and time."),400
    if when<=datetime.now():return jsonify(ok=False,error="Choose a future appointment time."),400
    if ((when.weekday()+1)%7) not in [int(x) for x in b["working_days"].split(',') if x.strip().isdigit()]:return jsonify(ok=False,error="That day is outside the business working days."),400
    start=datetime.strptime(b["start_time"],'%H:%M').time(); end=datetime.strptime(b["end_time"],'%H:%M').time()
    if when.time()<start or (when.hour*60+when.minute+b["duration"] > end.hour*60+end.minute):return jsonify(ok=False,error="That time is outside the available working hours."),400
    uid=b["user_id"]; when_text=when.isoformat(timespec='minutes')
    existing=db().all("SELECT appointment_at FROM appointments WHERE user_id=? AND status='Scheduled'",(uid,))
    for row in existing:
        try:
            old=datetime.fromisoformat(str(row["appointment_at"]).replace('Z','+00:00'))
            if old.tzinfo is not None: old=old.astimezone().replace(tzinfo=None)
            if abs((old-when).total_seconds()) < b["duration"]*60:
                return jsonify(ok=False,error="That slot was just booked. Please choose another time."),409
        except Exception: continue
    # A booking creates both a CRM appointment and a lead for the correct workspace.
    note=f"Public booking | Customer: {name} | Phone: {phone} | Reason: {reason}"
    db().execute("INSERT INTO appointments(user_id,lead_id,title,appointment_at,status,notes,created_at) VALUES(?,NULL,?,?, 'Scheduled',?,?)",(uid,f"Booking: {name}",when_text,note,iso()))
    db().execute("INSERT INTO leads(user_id,name,phone,source,stage,score,deal_value,notes,created_at,updated_at) VALUES(?,?,?,'Public booking','New',0,0,?,?,?)",(uid,name,phone,note,iso(),iso()))
    return jsonify(ok=True,message="Your appointment request is booked. Please keep your phone available for confirmation.")

BOOKING_HTML=r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Book an appointment</title><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Playfair+Display:wght@600;700&display=swap" rel="stylesheet"><style>*{box-sizing:border-box}body{margin:0;background:#FAFAF9;color:#0F0F0F;font:16px Inter,Arial,sans-serif}.shell{max-width:650px;margin:40px auto;padding:18px}.panel{background:white;border:1px solid #EAEAEA;border-radius:18px;padding:28px}.brand{font-size:13px;font-weight:800;letter-spacing:.08em}.brand span{color:#D4AF37}.tag{color:#D4AF37;font-weight:700;font-size:12px;text-transform:uppercase;margin-top:26px}h1{font:700 36px 'Playfair Display',Georgia,serif;margin:8px 0}p{color:#555;line-height:1.6}label{display:block;font-size:13px;font-weight:700;margin:15px 0 7px}input,select,textarea{width:100%;padding:13px;border:1px solid #ddd;border-radius:10px;font:inherit;background:#fff;color:#111}button{margin-top:20px;width:100%;border:0;border-radius:10px;padding:14px;background:#111;color:#fff;font-weight:700;font-size:15px;cursor:pointer}.gold{color:#D4AF37}.msg{display:none;margin-top:15px;padding:13px;border-radius:10px;background:#f7f1dd;line-height:1.5}.small{font-size:12px;color:#666}.foot{text-align:center;color:#777;font-size:12px;margin:18px} @media(max-width:480px){.shell{margin:10px auto;padding:10px}.panel{padding:20px}h1{font-size:30px}}</style></head><body><div class="shell"><div class="panel"><div class="brand">TIMILEYIN<span>GROWTHCRM</span></div><div class="tag">Appointment booking</div><h1 id="business">Book a time</h1><p>Choose an available appointment time and enter your contact details. <b class="gold">Duration: {{duration}} minutes</b></p><div id="message" class="msg" role="status"></div><form id="form"><label for="date">Date</label><input id="date" type="date" required><label for="slot">Available time</label><select id="slot" required><option value="">Choose a date first</option></select><label for="name">Your name</label><input id="name" maxlength="120" required autocomplete="name"><label for="phone">Phone number</label><input id="phone" maxlength="60" required autocomplete="tel"><label for="reason">What is the appointment about?</label><textarea id="reason" maxlength="500" rows="3" placeholder="Briefly describe what you need"></textarea><button type="submit">Request appointment</button></form><p class="small">Your details are shared with this business only to manage your appointment.</p></div><div class="foot">Powered by TIMILEYINGROWTHCRM</div></div><script>const slug={{slug|tojson}};let config=null;const dateEl=document.getElementById('date'),slotEl=document.getElementById('slot'),msgEl=document.getElementById('message');function show(t){msgEl.style.display='block';msgEl.textContent=t}function dateLocal(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}async function init(){try{const r=await fetch('/api/public-booking/'+encodeURIComponent(slug));const d=await r.json();if(!r.ok)throw Error(d.error||'Booking unavailable');config=d;document.getElementById('business').textContent=d.business;const now=new Date();dateEl.min=dateLocal(now);dateEl.addEventListener('change',slots);slots()}catch(e){show(e.message)}}function slots(){slotEl.innerHTML='';if(!config||!dateEl.value){slotEl.innerHTML='<option value="">Choose a date first</option>';return}const day=new Date(dateEl.value+'T12:00:00').getDay();if(!config.working_days.includes(day)){slotEl.innerHTML='<option value="">This business is closed on that day</option>';return}const [sh,sm]=config.start_time.split(':').map(Number),[eh,em]=config.end_time.split(':').map(Number);const start=sh*60+sm,end=eh*60+em,dur=Number(config.duration);let count=0;for(let m=start;m+dur<=end;m+=dur){const hh=String(Math.floor(m/60)).padStart(2,'0'),mm=String(m%60).padStart(2,'0'),v=dateEl.value+'T'+hh+':'+mm;const dt=new Date(v);if(dt.getTime()>Date.now()){const o=document.createElement('option');o.value=v;o.textContent=dt.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});slotEl.appendChild(o);count++}}if(!count)slotEl.innerHTML='<option value="">No future times available for this date</option>'}document.getElementById('form').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;b.textContent='Submitting…';try{const r=await fetch('/api/public-booking/'+encodeURIComponent(slug),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:document.getElementById('name').value,phone:document.getElementById('phone').value,reason:document.getElementById('reason').value,appointment_at:slotEl.value})});const d=await r.json();if(!r.ok)throw Error(d.error||'Could not book');show(d.message);document.getElementById('form').reset();slots()}catch(err){show(err.message)}finally{b.disabled=false;b.textContent='Request appointment'}});init();</script></body></html>'''

@app.get("/")
def home():
    return render_template_string(HTML)

@app.get("/tgr-master-831")
def private_creator_entry():
    # Secret path is obscurity only; creator email + password still protect all actions.
    page = HTML.replace("loadReviews();(async()=>{try{let d=await api('/api/me');", "if(location.pathname==='/tgr-master-831')modal('creatorLogin');loadReviews();(async()=>{try{let d=await api('/api/me');")
    return render_template_string(page)

@app.get("/control-room-7x9")
def retired_creator_entry():
    return ("Not found", 404)

@app.get("/welcome")
def welcome():
    # Selar return page: customers should retrieve the access code from their email.
    registration_html = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Check your email • TIMILEYINGROWTH CRM</title><style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:18px;background:#0A0A0A;color:#fff;font-family:Inter,Arial,sans-serif}.card{width:min(520px,100%);padding:28px;background:#111;border:1px solid #2A2A2A;border-radius:18px}.brand{font-weight:900;letter-spacing:.04em;color:#D4AF37}.muted{color:#B5B5B5;line-height:1.7}.notice{margin:18px 0;padding:16px;border:1px solid #2A2A2A;background:#1E1E1E;border-radius:12px;line-height:1.6}.step{margin:10px 0}.btn{display:block;width:100%;margin-top:14px;padding:14px;border:0;border-radius:10px;background:#D4AF37;color:#000;text-align:center;text-decoration:none;font-weight:800}.support{font-size:13px;color:#9A9A9A;margin-top:20px}.support a{color:#D4AF37}</style></head><body><main class="card"><div class="brand">TIMILEYINGROWTH CRM</div><h1>Check your email for your access code</h1><p class="muted">Thank you for your purchase. Your personal access code will be sent to the email address used at Selar after your payment is confirmed.</p><div class="notice"><div class="step"><strong>1.</strong> Open your inbox for the email address used at checkout.</div><div class="step"><strong>2.</strong> Find the TIMILEYINGROWTH CRM access-code email. Check Spam/Junk if it is not in your inbox.</div><div class="step"><strong>3.</strong> Copy the code from the email and use it to create your account.</div></div><a class="btn" href="/register">Continue to create account</a><p class="support">If you need help, contact <a href="mailto:supporttimilehingrowth@gmail.com">supporttimilehingrowth@gmail.com</a>. If payment was just completed, allow a little time for confirmation.</p></main></body></html>'''
    return render_template_string(registration_html)

@app.get("/register")
def registration_page():
    return render_template_string(HTML.replace("</body></html>", "</body></html>")) if False else render_template_string(r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Register • TIMILEYINGROWTHCRM</title><style>*{box-sizing:border-box}body{background:#0A0A0A;color:#FFF;font-family:Inter,Arial,sans-serif;padding:18px;margin:0}.card{max-width:520px;margin:5vh auto;background:#111;padding:28px;border:1px solid #2A2A2A;border-radius:18px}h2{line-height:1.3}p{color:#B5B5B5;line-height:1.6}label{display:block;width:100%;margin:12px 0 5px;color:#DDD;font-size:14px}input{display:block;width:100%;box-sizing:border-box;margin:0 0 8px;padding:12px;background:#1E1E1E;color:#FFF;border:1px solid #2A2A2A;border-radius:10px;font-size:16px}input:focus{border-color:#D4AF37;outline:none}button{display:block;width:100%;padding:14px;background:#D4AF37;color:#000;border:0;border-radius:10px;font-weight:800;font-size:15px;margin-top:16px}.msg{white-space:pre-wrap;color:#FFF}</style></head><body><div class="card"><a href="/" style="display:block;text-align:center;margin:0 0 20px;padding:12px;border:1px solid #D4AF37;border-radius:10px;color:#D4AF37;text-decoration:none;font-weight:800">← Back to Subscription Page</a><h2>TIMILEYINGROWTHCRM — Create account</h2><p>Copy the personal access code from the email we sent you, then enter it below.</p><div id="msg" class="msg"></div><form id="f"><label>Full name</label><input id="name" required><label>Business name</label><input id="business" required><label>Email used at Selar</label><input id="email" type="email" required><label>Personal access code</label><input id="code" required><label>Password (minimum 8 characters)</label><input id="password" type="password" minlength="8" required><button>Create account and continue</button></form></div><script>const q=new URLSearchParams(location.search);const nameInput=document.getElementById('name');const businessInput=document.getElementById('business');const emailInput=document.getElementById('email');const codeInput=document.getElementById('code');const passwordInput=document.getElementById('password');const form=document.getElementById('f');const message=document.getElementById('msg');emailInput.value=q.get('email')||'';codeInput.value=q.get('code')||'';form.addEventListener('submit',async e=>{e.preventDefault();message.textContent='Creating account…';try{const payload={name:nameInput.value.trim(),business:businessInput.value.trim(),email:emailInput.value.trim(),password:passwordInput.value,access_code:codeInput.value.trim()};const r=await fetch('/api/register',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});const d=await r.json();if(!r.ok)throw Error(d.error||'Registration failed');message.textContent='Registration successful. Opening your CRM…';location.href='/'}catch(x){message.textContent=x.message}});</script></body></html>''')

with app.app_context():
    db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
