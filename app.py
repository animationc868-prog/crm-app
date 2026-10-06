import os, re, json, hmac, hashlib, secrets, sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urljoin

import requests
from flask import Flask, request, jsonify, session, render_template_string

try:
    from libsql_client import create_client_sync
except Exception:
    create_client_sync = None

APP_NAME = "TIMILEYINGROWTHCRM"
CREATOR_EMAIL = os.getenv("CREATOR_EMAIL", "dayotimileyin831@gmail.com").lower().strip()
# Set CREATOR_PASSWORD in Render Environment Variables. Never put the real password in GitHub.
CREATOR_PASSWORD = os.getenv("CREATOR_PASSWORD", "")
CONTACT_EMAIL = "dayotimileyin831@gmail.com"
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
DB_PATH = os.getenv("DATABASE_PATH", "timileyingrowthcrm.db")
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

PLANS = {
    "monthly": {"name": "1 Month", "price": 19, "days": 31, "url": "https://selar.com/9u69r59d57"},
    "six_months": {"name": "6 Months", "price": 79, "days": 183, "url": "https://selar.com/011h9610d1"},
    "yearly": {"name": "1 Year", "price": 149, "days": 365, "url": "https://selar.com/x952197d1u"},
}

app = Flask(__name__)
app.secret_key = SESSION_SECRET
DB = None


def now():
    return datetime.now(timezone.utc)


def iso():
    return now().isoformat()


def dt(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
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
    def __init__(self):
        self.turso = bool(TURSO_URL and TURSO_TOKEN and create_client_sync)
        self.client = None
        if self.turso:
            try:
                self.client = create_client_sync(TURSO_URL, auth_token=TURSO_TOKEN)
            except Exception as e:
                print(f"Database connection error: {e}")
                self.turso = False
        self.schema()

    def execute(self, sql, args=()):
        if self.turso:
            return self.client.execute(sql, args)
        con = sqlite3.connect(DB_PATH)
        try:
            cur = con.execute(sql, args)
            con.commit()
            return cur
        finally:
            con.close()

    def one(self, sql, args=()):
        if self.turso:
            r = self.client.execute(sql, args)
            return dict(zip(r.columns, r.rows[0])) if r.rows else None
        con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
        try:
            r = con.execute(sql, args).fetchone()
            return dict(r) if r else None
        finally: con.close()

    def all(self, sql, args=()):
        if self.turso:
            r = self.client.execute(sql, args)
            return [dict(zip(r.columns, x)) for x in r.rows]
        con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
        try:
            return [dict(x) for x in con.execute(sql, args).fetchall()]
        finally: con.close()

    def schema(self):
        for q in [
            "CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,business TEXT NOT NULL,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,email TEXT NOT NULL,plan TEXT NOT NULL,access_code TEXT NOT NULL UNIQUE,starts_at TEXT NOT NULL,expires_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',selar_reference TEXT,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS leads(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,name TEXT NOT NULL,email TEXT,phone TEXT,company TEXT,website TEXT,source TEXT,stage TEXT DEFAULT 'New',score INTEGER DEFAULT 0,deal_value REAL DEFAULT 0,notes TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,lead_id INTEGER,title TEXT NOT NULL,appointment_at TEXT NOT NULL,status TEXT DEFAULT 'Scheduled',notes TEXT,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS followups(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,lead_id INTEGER,due_at TEXT NOT NULL,channel TEXT DEFAULT 'Email',message TEXT,status TEXT DEFAULT 'Pending',created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,title TEXT NOT NULL,due_at TEXT,priority TEXT DEFAULT 'Medium',status TEXT DEFAULT 'Pending',created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS settings(user_id INTEGER PRIMARY KEY,business_name TEXT,business_email TEXT,phone TEXT,website TEXT,industry TEXT,description TEXT,updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,name TEXT NOT NULL,email TEXT,rating INTEGER NOT NULL,review TEXT NOT NULL,created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS webhook_events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT UNIQUE NOT NULL,reference TEXT,email TEXT,payload TEXT,created_at TEXT NOT NULL)",
        ]: self.execute(q)


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
    if not s: return {"active": False, "status": "inactive"}
    p = PLANS.get(s["plan"], {})
    exp = dt(s["expires_at"]); days = max(0, (exp-now()).days) if exp else 0
    return {"active": s["status"] == "active" and days >= 0, "status": s["status"], "plan": s["plan"], "plan_name": p.get("name", s["plan"]), "price": p.get("price"), "access_code": s["access_code"], "starts_at": s["starts_at"], "expires_at": s["expires_at"], "days_remaining": days, "selar_reference": s.get("selar_reference")}


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
        if not session.get("creator"): return jsonify(ok=False,error="Creator login required"),403
        return fn(*a, **k)
    return w


def plan_value(v):
    s = str(v or "").strip().lower()
    aliases = {"monthly":"monthly","month":"monthly","1 month":"monthly","19":"monthly","$19":"monthly","six months":"six_months","6 months":"six_months","six_months":"six_months","79":"six_months","$79":"six_months","yearly":"yearly","year":"yearly","1 year":"yearly","12 months":"yearly","149":"yearly","$149":"yearly"}
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

@app.post("/api/register")
def register():
    d=request.get_json(silent=True) or {}; n=str(d.get("name","")).strip(); b=str(d.get("business","")).strip(); e=email(d.get("email")); p=str(d.get("password",""))
    if not n or not b or not e or len(p)<8: return jsonify(ok=False,error="Name, business, valid email and an 8+ character password are required."),400
    if db().one("SELECT id FROM users WHERE lower(email)=?",(e,)): return jsonify(ok=False,error="This email is already registered. Please log in."),409
    try: db().execute("INSERT INTO users(name,business,email,password_hash,created_at) VALUES(?,?,?,?,?)",(n,b,e,password_hash(p),iso()))
    except Exception: return jsonify(ok=False,error="This email is already registered."),409
    u=db().one("SELECT id,name,business,email,created_at FROM users WHERE lower(email)=?",(e,)); s=sub_for(e)
    if s: db().execute("UPDATE subscriptions SET user_id=? WHERE id=?",(u["id"],s["id"]))
    session["uid"]=u["id"]
    return jsonify(ok=True,user=user_json(u))

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
    
@app.route("/welcome")
def welcome():
    customer_email = request.args.get("email", "")
    reference = request.args.get("reference", "") or request.args.get("trxref", "")
    
    # Return a clean HTML response with their details so it never crashes
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Welcome to TIMILEYINGROWTHCRM</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: sans-serif; background: #0b0f19; color: #fff; text-align: center; padding: 50px 20px; }}
            .card {{ background: #161e2e; max-width: 500px; margin: 0 auto; padding: 30px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }}
            h2 {{ color: #10b981; }}
            a {{ display: inline-block; margin-top: 20px; background: #10b981; color: #fff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>Payment Successful! 🎉</h2>
            <p>Thank you for subscribing to TIMILEYINGROWTHCRM.</p>
            <p><strong>Email:</strong> {customer_email}</p>
            <p><strong>Reference:</strong> {reference}</p>
            <a href="/">Go to Login & Dashboard</a>
        </div>
    </body>
    </html>
    """
    
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
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    try:
        x=requests.post(url,json={"contents":[{"parts":[{"text":prompt}]}]},timeout=45);x.raise_for_status();j=x.json();text=j["candidates"][0]["content"]["parts"][0]["text"];return jsonify(ok=True,text=text)
    except Exception as e:return jsonify(ok=False,error="Gemini request failed.",details=str(e)[:300]),502

@app.get("/api/settings")
@paid
def get_settings():return jsonify(ok=True,settings=db().one("SELECT * FROM settings WHERE user_id=?",(user()["id"],)) or {})

@app.put("/api/settings")
@paid
def put_settings():
    u=user();d=request.get_json(silent=True) or {};db().execute("INSERT INTO settings(user_id,business_name,business_email,phone,website,industry,description,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET business_name=excluded.business_name,business_email=excluded.business_email,phone=excluded.phone,website=excluded.website,industry=excluded.industry,description=excluded.description,updated_at=excluded.updated_at",(u["id"],d.get("business_name",""),d.get("business_email",u["email"]),d.get("phone",""),d.get("website",""),d.get("industry",""),d.get("description",""),iso()));return jsonify(ok=True)

@app.get("/api/reviews")
def reviews():return jsonify(ok=True,reviews=db().all("SELECT name,rating,review,created_at FROM reviews ORDER BY id DESC LIMIT 12"))

@app.post("/api/reviews")
def review():
    d=request.get_json(silent=True) or {};n=str(d.get("name","")).strip();r=str(d.get("review","")).strip();rating=max(1,min(5,int(d.get("rating",5))))
    if not n or not r:return jsonify(ok=False,error="Name and review are required."),400
    u=user();db().execute("INSERT INTO reviews(user_id,name,email,rating,review,created_at) VALUES(?,?,?,?,?,?)",(u["id"] if u else None,n,email(d.get("email")),rating,r,iso()));return jsonify(ok=True,message="Thank you for your review.")

@app.post("/api/contact")
def contact():
    d=request.get_json(silent=True) or {};payload=json.dumps({"name":d.get("name",""),"email":d.get("email",""),"message":d.get("message","")});db().execute("INSERT INTO webhook_events(event_key,reference,email,payload,created_at) VALUES(?,?,?,?,?)",("contact:"+secrets.token_hex(10),"contact",email(d.get("email")),payload,iso()));return jsonify(ok=True,message=f"Message received. For urgent help email {CONTACT_EMAIL}.")

@app.post("/api/webhooks/selar")
def selar_webhook():
    raw=request.get_data(cache=True);sig=request.headers.get("X-Selar-Signature","");secret=os.getenv("SELAR_WEBHOOK_SECRET","").strip()
    if secret and sig:
        expected=hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,sig):return jsonify(ok=False,error="Invalid webhook signature."),401
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
    session["creator"]=True;return jsonify(ok=True)

@app.post("/api/creator/logout")
def creator_logout():session.pop("creator",None);return jsonify(ok=True)

@app.get("/api/creator/customers")
@creator
def customers():return jsonify(ok=True,customers=db().all("SELECT u.id,u.name,u.business,u.email,u.created_at,s.plan,s.access_code,s.starts_at,s.expires_at,s.status,s.selar_reference FROM users u LEFT JOIN subscriptions s ON s.id=(SELECT s2.id FROM subscriptions s2 WHERE s2.user_id=u.id ORDER BY s2.id DESC LIMIT 1) ORDER BY u.id DESC"))

@app.post("/api/creator/extend")
@creator
def extend():
    d=request.get_json(silent=True) or {};e=email(d.get("email"));p=plan_value(d.get("plan"));u=db().one("SELECT id,name FROM users WHERE lower(email)=?",(e,))
    if not u or not p:return jsonify(ok=False,error="Customer or plan not found."),400
    return jsonify(ok=True,subscription=sub_json(activate(e,p,"admin-extension",u["id"])))

@app.post("/api/creator/disable")
@creator
def disable():
    e=email((request.get_json(silent=True) or {}).get("email"));db().execute("UPDATE subscriptions SET status='disabled' WHERE lower(email)=?",(e,));return jsonify(ok=True)

HTML=r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TIMILEYINGROWTHCRM</title><style>
*{box-sizing:border-box}body{margin:0;font-family:Inter,Arial,sans-serif;background:#07111f;color:#eef6ff}a{color:#65e5ff}button{cursor:pointer}.wrap{width:min(1150px,92%);margin:auto}.nav{height:68px;display:flex;align-items:center;justify-content:space-between}.brand{font-weight:900}.brand i{color:#45ddff;font-style:normal}.top{position:sticky;top:0;background:#07111fee;border-bottom:1px solid #203a55;z-index:9}.links{display:flex;gap:16px;align-items:center}.links a{color:#a6bad0;text-decoration:none;font-size:14px}.hero{text-align:center;padding:80px 0}.hero h1{font-size:clamp(42px,7vw,76px);line-height:1;margin:20px auto;max-width:900px}.grad{background:linear-gradient(90deg,#fff,#55ddff,#8b8cff);color:transparent;background-clip:text}.hero p,.muted{color:#9bb0c7;line-height:1.7}.badge{display:inline-block;border:1px solid #24506d;border-radius:999px;padding:7px 11px;color:#80e9ff;background:#0a2034;font-size:12px;font-weight:800}.btn{border:0;border-radius:11px;padding:12px 17px;background:#48dcff;color:#04101b;font-weight:800}.ghost{background:#10243b;color:#fff;border:1px solid #29445e}.actions{display:flex;justify-content:center;gap:10px;flex-wrap:wrap}.section{padding:55px 0}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:17px}.card{background:#0d1d31;border:1px solid #203b58;border-radius:18px;padding:20px}.card p{color:#9bb0c7;line-height:1.6}.locked{position:relative;overflow:hidden;opacity:.65}.locked:after{content:'🔒 Premium — subscribe to unlock';position:absolute;inset:0;background:#06101ddd;display:flex;align-items:center;justify-content:center;font-weight:900}.pricing .card{display:flex;flex-direction:column}.price{font-size:42px;font-weight:900}.pricecard ul{color:#9bb0c7;line-height:2;padding-left:20px}.pricecard .btn{margin-top:auto}.featured{border-color:#45dcff;transform:translateY(-4px)}.reviews{grid-template-columns:repeat(3,1fr)}.stars{color:#ffd166}.footer{border-top:1px solid #203b58;padding:35px 0;color:#8fa6bd}.modal{display:none;position:fixed;inset:0;background:#000b;z-index:30;align-items:center;justify-content:center;padding:15px}.modal.on{display:flex}.box{width:min(650px,100%);max-height:92vh;overflow:auto;background:#0b1b2e;border:1px solid #28445f;border-radius:20px;padding:22px}.close{float:right;background:none;border:0;color:#fff;font-size:25px}.form{display:grid;grid-template-columns:1fr 1fr;gap:12px}label{font-size:13px;color:#abc0d4}input,textarea,select{width:100%;margin-top:5px;background:#071525;color:#fff;border:1px solid #29445e;border-radius:10px;padding:11px}textarea{min-height:110px}.full{grid-column:1/-1}.msg{display:none;background:#102c44;padding:10px;border-radius:9px;margin:10px 0}.app{display:none}.app.on{display:block}.tabs{display:flex;gap:7px;flex-wrap:wrap;margin:20px 0}.tabs button{padding:9px 12px;background:#10243b;border:1px solid #29445e;color:#bcd0e4;border-radius:9px}.tabs .active{background:#1d4968;color:#fff}.pane{display:none}.pane.on{display:block}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.kpi{background:#0d2034;border:1px solid #203b58;border-radius:15px;padding:17px}.kpi b{font-size:28px;display:block;margin-top:7px}.table{overflow:auto}table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid #203b58;text-align:left;font-size:13px}.small{font-size:12px;color:#8fa6bd}@media(max-width:800px){.grid,.reviews,.kpis{grid-template-columns:1fr 1fr}.links{display:none}}@media(max-width:520px){.grid,.reviews,.kpis,.form{grid-template-columns:1fr}.full{grid-column:auto}.hero{padding:55px 0}.hero h1{font-size:44px}}
</style></head><body>
<header class="top"><div class="wrap nav"><div class="brand">TIMILEYIN<i>GROWTHCRM</i></div><div class="links"><a href="#features">Features</a><a href="#pricing">Pricing</a><a href="#reviews">Reviews</a><a href="#contact">Support</a><button class="btn ghost" onclick="modal('login')">Login</button></div></div></header>
<main id="landing"><section class="hero"><div class="wrap"><span class="badge">AI-POWERED CUSTOMER GROWTH PLATFORM</span><h1>Turn leads into <span class="grad">paying customers.</span></h1><p>One workspace for leads, pipeline, AI sales coaching, website audits, appointments, follow-ups and business analytics.</p><div class="actions"><button class="btn" onclick="document.getElementById('pricing').scrollIntoView({behavior:'smooth'})">Start Growing</button><button class="btn ghost" onclick="modal('login')">I already have an account</button></div><p class="small">Private customer accounts • Subscription protected • AI-assisted sales</p></div></section>
<section class="section" id="features"><div class="wrap"><h2>Everything your business needs</h2><p class="muted">Premium tools are locked until a subscription is active.</p><div class="grid"><div class="card locked"><h3>👥 Leads & Customers</h3><p>Contacts, business details, stages, scores, notes and history.</p></div><div class="card locked"><h3>📊 Sales Pipeline</h3><p>Track opportunities from New to Won with deal values.</p></div><div class="card locked"><h3>🤖 AI Sales Coach</h3><p>Generate tailored outreach, follow-ups and sales strategies.</p></div><div class="card locked"><h3>🌐 Website Audit</h3><p>Check response speed, mobile viewport, forms, booking signals and links.</p></div><div class="card locked"><h3>📅 Appointments</h3><p>Schedule meetings and customer follow-ups.</p></div><div class="card locked"><h3>📈 Analytics</h3><p>See leads, hot prospects, pipeline, revenue and conversion.</p></div></div></div></section>
<section class="section" id="pricing"><div class="wrap"><h2>Choose your plan</h2><p class="muted">Each button opens its matching Selar checkout.</p><div class="grid pricing"><div class="card pricecard"><span class="badge">STARTER</span><h3>1 Month</h3><div class="price">$19</div><ul><li>Full CRM</li><li>AI Sales Coach</li><li>Website Audit</li><li>Appointments</li><li>Analytics</li></ul><button class="btn" onclick="buy('monthly')">Get 1 Month</button></div><div class="card pricecard featured"><span class="badge">POPULAR</span><h3>6 Months</h3><div class="price">$79</div><ul><li>Full CRM</li><li>AI Sales Coach</li><li>Website Audit</li><li>Appointments</li><li>Analytics</li></ul><button class="btn" onclick="buy('six_months')">Get 6 Months</button></div><div class="card pricecard"><span class="badge">BEST VALUE</span><h3>1 Year</h3><div class="price">$149</div><ul><li>Full CRM</li><li>AI Sales Coach</li><li>Website Audit</li><li>Appointments</li><li>Analytics</li></ul><button class="btn" onclick="buy('yearly')">Get 1 Year</button></div></div></div></section>
<section class="section" id="reviews"><div class="wrap"><h2>Customer reviews</h2><p class="muted">Customers can leave their feedback here.</p><div id="reviewList" class="grid reviews"></div><button class="btn ghost" onclick="modal('review')">Leave a Review</button></div></section>
<section class="section" id="contact"><div class="wrap"><div class="card"><h2>Need help?</h2><p>Email <b>dayotimileyin831@gmail.com</b> for account, payment or CRM issues.</p><button class="btn" onclick="modal('contact')">Contact Support</button></div></div></section></main>

<div class="modal" id="login"><div class="box"><button class="close" onclick="closeModal('login')">×</button><h2>Welcome back</h2><p class="muted">Log in to your account.</p><div id="loginMsg" class="msg"></div><div class="form"><div class="full"><label>Email</label><input id="le" type="email"></div><div class="full"><label>Password</label><input id="lp" type="password"></div><div class="full"><button class="btn" onclick="login()">Login</button></div></div><hr style="border-color:#203b58"><button class="btn ghost" onclick="closeModal('login');modal('register')">Create account</button></div></div>
<div class="modal" id="register"><div class="box"><button class="close" onclick="closeModal('register')">×</button><h2>Create your account</h2><p class="muted">Use the same email used for your Selar purchase.</p><div id="regMsg" class="msg"></div><div class="form"><div><label>Name</label><input id="rn"></div><div><label>Business</label><input id="rb"></div><div class="full"><label>Email</label><input id="re" type="email"></div><div class="full"><label>Password (8+ characters)</label><input id="rp" type="password"></div><div class="full"><button class="btn" onclick="register()">Create Account</button></div></div></div></div>
<div class="modal" id="review"><div class="box"><button class="close" onclick="closeModal('review')">×</button><h2>Leave a review</h2><div id="reviewMsg" class="msg"></div><div class="form"><div><label>Name</label><input id="rvn"></div><div><label>Email</label><input id="rve"></div><div><label>Rating</label><select id="rvr"><option>5</option><option>4</option><option>3</option><option>2</option><option>1</option></select></div><div class="full"><label>Review</label><textarea id="rvt"></textarea></div><div class="full"><button class="btn" onclick="reviewSubmit()">Publish Review</button></div></div></div></div>
<div class="modal" id="contact"><div class="box"><button class="close" onclick="closeModal('contact')">×</button><h2>Contact Support</h2><p class="muted">Or email <b>dayotimileyin831@gmail.com</b> directly.</p><div class="form"><div><label>Name</label><input id="cn"></div><div><label>Email</label><input id="ce"></div><div class="full"><label>Message</label><textarea id="ct"></textarea></div><div class="full"><button class="btn" onclick="contactSend()">Send</button></div></div></div></div>

<div id="appui" class="app"><header class="top"><div class="wrap nav"><div class="brand">TIMILEYIN<i>GROWTHCRM</i></div><button class="btn ghost" onclick="logout()">Logout</button></div></header><div class="wrap"><div class="card" style="margin-top:25px"><span class="badge">PRIVATE WORKSPACE</span><h2 id="welcome"></h2><p id="subtext" class="muted"></p><div class="card"><span class="small">ACCESS CODE</span><h3 id="code"></h3></div></div><div class="tabs"><button class="active" onclick="tab('dash',this)">Dashboard</button><button onclick="tab('leads',this)">Leads</button><button onclick="tab('pipeline',this)">Pipeline</button><button onclick="tab('ai',this)">AI Coach</button><button onclick="tab('audit',this)">Website Audit</button><button onclick="tab('appointments',this)">Appointments</button><button onclick="tab('followups',this)">Follow-ups</button><button onclick="tab('tasks',this)">Tasks</button><button onclick="tab('settings',this)">Settings</button></div>
<section id="dash" class="pane on"><div class="kpis"><div class="kpi">Leads<b id="k1">0</b></div><div class="kpi">Hot<b id="k2">0</b></div><div class="kpi">Pipeline<b id="k3">$0</b></div><div class="kpi">Revenue<b id="k4">$0</b></div></div><div class="grid" style="margin-top:15px"><div class="card"><h3>Conversion</h3><b id="k5">0%</b></div><div class="card"><h3>Appointments</h3><b id="k6">0</b></div><div class="card"><h3>Subscription</h3><p id="k7" class="muted"></p></div></div></section>
<section id="leads" class="pane"><div class="card"><h2>Leads</h2><div class="form"><div><label>Name</label><input id="ln"></div><div><label>Company</label><input id="lc"></div><div><label>Email</label><input id="lm"></div><div><label>Phone</label><input id="lp2"></div><div><label>Website</label><input id="lw"></div><div><label>Deal value</label><input id="lv" type="number"></div><div><label>Stage</label><select id="ls"><option>New</option><option>Contacted</option><option>Qualified</option><option>Proposal</option><option>Negotiation</option><option>Won</option><option>Lost</option></select></div><div><label>Score</label><input id="lsc" type="number" value="0"></div><div class="full"><label>Notes</label><textarea id="lnt"></textarea></div><div class="full"><button class="btn" onclick="addLead()">Add Lead</button></div></div></div><div class="card" style="margin-top:15px"><div class="table"><table><thead><tr><th>Name</th><th>Company</th><th>Stage</th><th>Score</th><th>Value</th><th></th></tr></thead><tbody id="lt"></tbody></table></div></div></section>
<section id="pipeline" class="pane"><div class="card"><h2>Sales Pipeline</h2><div id="pipe" class="grid"></div></div></section>
<section id="ai" class="pane"><div class="card"><h2>🤖 AI Sales Coach</h2><div class="form"><div><label>Your business</label><input id="ab"></div><div><label>Prospect</label><input id="ap"></div><div><label>Service</label><input id="as"></div><div><label>Channel</label><select id="ac"><option>Email</option><option>WhatsApp</option><option>LinkedIn</option></select></div><div class="full"><label>Goal</label><input id="ag" value="Get a reply and start a conversation"></div><div class="full"><button class="btn" onclick="aiRun()">Generate</button></div></div><div id="ao" class="card" style="display:none;margin-top:15px;white-space:pre-wrap"></div></div></section>
<section id="audit" class="pane"><div class="card"><h2>🌐 Website Audit</h2><div class="form"><div class="full"><label>Website URL</label><input id="au" placeholder="https://example.com"></div><div class="full"><button class="btn" onclick="auditRun()">Run Audit</button></div></div><div id="auditOut" style="margin-top:15px"></div></div></section>
<section id="appointments" class="pane"><div class="card"><h2>📅 Appointments</h2><div class="form"><div><label>Title</label><input id="apt"></div><div><label>Date/time</label><input id="apd" type="datetime-local"></div><div class="full"><label>Notes</label><textarea id="apn"></textarea></div><div class="full"><button class="btn" onclick="addAppt()">Save</button></div></div><div id="apl" style="margin-top:15px"></div></div></section>
<section id="followups" class="pane"><div class="card"><h2>🔔 Follow-ups</h2><div class="form"><div><label>Due</label><input id="fd" type="datetime-local"></div><div><label>Channel</label><select id="fc"><option>Email</option><option>WhatsApp</option><option>Phone</option></select></div><div class="full"><label>Message</label><textarea id="fm"></textarea></div><div class="full"><button class="btn" onclick="addFollow()">Schedule</button></div></div><div id="fl" style="margin-top:15px"></div></div></section>
<section id="tasks" class="pane"><div class="card"><h2>✅ Tasks</h2><div class="form"><div><label>Task</label><input id="tt"></div><div><label>Priority</label><select id="tp"><option>High</option><option>Medium</option><option>Low</option></select></div><div><label>Due</label><input id="td" type="datetime-local"></div><div><button class="btn" style="margin-top:21px" onclick="addTask()">Add</button></div></div><div id="tl" style="margin-top:15px"></div></div></section>
<section id="settings" class="pane"><div class="card"><h2>⚙️ Business Settings</h2><div class="form"><div><label>Business</label><input id="sb"></div><div><label>Email</label><input id="se"></div><div><label>Phone</label><input id="sp"></div><div><label>Website</label><input id="sw"></div><div><label>Industry</label><input id="si"></div><div class="full"><label>Description</label><textarea id="sd"></textarea></div><div class="full"><button class="btn" onclick="saveSettings()">Save</button></div></div></div></section></div></div></div>
<footer class="footer"><div class="wrap">TIMILEYINGROWTHCRM • Support: <a href="mailto:dayotimileyin831@gmail.com">dayotimileyin831@gmail.com</a></div></footer>
<script>
const PL={monthly:{url:'https://selar.com/9u69r59d57',name:'1 Month'},six_months:{url:'https://selar.com/011h9610d1',name:'6 Months'},yearly:{url:'https://selar.com/x952197d1u',name:'1 Year'}};let U=null;
function modal(id){document.getElementById(id).classList.add('on')}function closeModal(id){document.getElementById(id).classList.remove('on')}function msg(id,t){let x=document.getElementById(id);x.textContent=t;x.style.display='block'}function esc(x){return String(x??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;')}
async function api(u,o={}){let r=await fetch(u,{credentials:'same-origin',headers:{'Content-Type':'application/json'},...o});let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw Error(d.error||'Request failed');return d}
function buy(p){location.href=PL[p].url}
async function register(){try{let d=await api('/api/register',{method:'POST',body:JSON.stringify({name:rn.value,business:rb.value,email:re.value,password:rp.value})});U=d.user;closeModal('register');openApp()}catch(e){msg('regMsg',e.message)}}
async function login(){try{let d=await api('/api/login',{method:'POST',body:JSON.stringify({email:le.value,password:lp.value})});if(!d.user.subscription.active){msg('loginMsg','No active subscription. Choose a plan below.');return}U=d.user;closeModal('login');openApp()}catch(e){msg('loginMsg',e.message)}}
async function logout(){await api('/api/logout',{method:'POST'});location.reload()}
async function openApp(){let d=await api('/api/me');if(!d.authenticated||!d.user.subscription.active)return;U=d.user;landing.style.display='none';appui.classList.add('on');welcome.textContent='Welcome, '+U.name;subtext.textContent=U.subscription.plan_name+' • '+U.subscription.days_remaining+' days remaining';code.textContent=U.subscription.access_code;k7.textContent=U.subscription.plan_name+' expires '+new Date(U.subscription.expires_at).toLocaleDateString();await Promise.all([dash(),loadLeads(),appts(),follows(),tasks(),settings()])}
async function dash(){let d=(await api('/api/dashboard')).dashboard;k1.textContent=d.total_leads;k2.textContent=d.hot_leads;k3.textContent='$'+Number(d.pipeline_value).toLocaleString();k4.textContent='$'+Number(d.revenue).toLocaleString();k5.textContent=d.conversion_rate+'%';k6.textContent=d.appointments}
async function loadLeads(){let a=(await api('/api/leads')).leads;lt.innerHTML='';let s={};a.forEach(x=>{s[x.stage]=(s[x.stage]||0)+1;lt.innerHTML+=`<tr><td>${esc(x.name)}</td><td>${esc(x.company)}</td><td>${esc(x.stage)}</td><td>${x.score}</td><td>$${Number(x.deal_value||0).toLocaleString()}</td><td><button class="btn" onclick="delLead(${x.id})">Delete</button></td></tr>`});pipe.innerHTML=Object.entries(s).map(([k,v])=>`<div class="card"><h3>${esc(k)}</h3><b style="font-size:34px">${v}</b><p>Lead(s)</p></div>`).join('')}
async function addLead(){try{await api('/api/leads',{method:'POST',body:JSON.stringify({name:ln.value,company:lc.value,email:lm.value,phone:lp2.value,website:lw.value,deal_value:lv.value,stage:ls.value,score:lsc.value,notes:lnt.value})});await loadLeads();await dash()}catch(e){alert(e.message)}}async function delLead(i){if(confirm('Delete this lead?')){await api('/api/leads/'+i,{method:'DELETE'});await loadLeads();await dash()}}
async function aiRun(){ao.style.display='block';ao.textContent='Generating...';try{ao.textContent=(await api('/api/ai/sales-coach',{method:'POST',body:JSON.stringify({business:ab.value,prospect:ap.value,service:as.value,channel:ac.value,goal:ag.value})})).text}catch(e){ao.textContent=e.message}}
async function auditRun(){auditOut.innerHTML='<div class="card">Auditing...</div>';try{let a=(await api('/api/website-audit',{method:'POST',body:JSON.stringify({url:au.value})})).audit;auditOut.innerHTML=`<div class="card"><h3>${esc(a.title||'Website Audit')}</h3><p>Reachable: <b>${a.reachable?'Yes':'No'}</b></p><p>Status: ${a.status_code||'—'} • Response: ${a.response_ms||'—'} ms</p><p>Mobile viewport: ${a.mobile_viewport?'Detected':'Not detected'} • HTTPS: ${a.https?'Yes':'No'} • Booking: ${a.booking_features?'Detected':'Not detected'}</p><p>Forms: ${a.forms} • Links checked: ${a.links_checked}</p><h4>Issues</h4><ul>${a.issues.map(x=>'<li>'+esc(x)+'</li>').join('')||'<li>No major automated issue found.</li>'}</ul></div>`}catch(e){auditOut.textContent=e.message}}
async function addAppt(){try{await api('/api/appointments',{method:'POST',body:JSON.stringify({title:apt.value,appointment_at:apd.value,notes:apn.value})});await appts();await dash()}catch(e){alert(e.message)}}async function appts(){let a=(await api('/api/appointments')).appointments;apl.innerHTML=a.map(x=>`<div class="card" style="margin-bottom:8px"><b>${esc(x.title)}</b><br><span class="small">${esc(x.appointment_at)}</span></div>`).join('')||'<p class="muted">No appointments.</p>'}
async function addFollow(){await api('/api/followups',{method:'POST',body:JSON.stringify({due_at:fd.value,channel:fc.value,message:fm.value})});await follows()}async function follows(){let a=(await api('/api/followups')).followups;fl.innerHTML=a.map(x=>`<div class="card" style="margin-bottom:8px"><b>${esc(x.channel)}</b> • ${esc(x.due_at)}<br>${esc(x.message||'')}</div>`).join('')||'<p class="muted">No follow-ups.</p>'}
async function addTask(){await api('/api/tasks',{method:'POST',body:JSON.stringify({title:tt.value,due_at:td.value,priority:tp.value})});await tasks()}async function tasks(){let a=(await api('/api/tasks')).tasks;tl.innerHTML=a.map(x=>`<div class="card" style="margin-bottom:8px"><b>${esc(x.title)}</b> • ${esc(x.priority)}<br>${esc(x.due_at||'')}</div>`).join('')||'<p class="muted">No tasks.</p>'}
async function settings(){let s=(await api('/api/settings')).settings||{};sb.value=s.business_name||'';se.value=s.business_email||'';sp.value=s.phone||'';sw.value=s.website||'';si.value=s.industry||'';sd.value=s.description||''}async function saveSettings(){await api('/api/settings',{method:'PUT',body:JSON.stringify({business_name:sb.value,business_email:se.value,phone:sp.value,website:sw.value,industry:si.value,description:sd.value})});alert('Settings saved.')}
function tab(id,b){document.querySelectorAll('.pane').forEach(x=>x.classList.remove('on'));document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));document.getElementById(id).classList.add('on');b.classList.add('active')}
async function loadReviews(){let a=(await api('/api/reviews')).reviews;reviewList.innerHTML=a.length?a.map(x=>`<div class="card"><div class="stars">${'★'.repeat(x.rating)}</div><h3>${esc(x.name)}</h3><p>“${esc(x.review)}”</p></div>`).join(''):'<div class="card"><div class="stars">★★★★★</div><p>Your review could be here.</p></div>'}async function reviewSubmit(){try{let d=await api('/api/reviews',{method:'POST',body:JSON.stringify({name:rvn.value,email:rve.value,rating:rvr.value,review:rvt.value})});msg('reviewMsg',d.message);loadReviews()}catch(e){msg('reviewMsg',e.message)}}async function contactSend(){let d=await api('/api/contact',{method:'POST',body:JSON.stringify({name:cn.value,email:ce.value,message:ct.value})});alert(d.message)}
loadReviews();(async()=>{try{let d=await api('/api/me');if(d.authenticated&&d.user.subscription.active){U=d.user;openApp()}}catch(e){}})();
</script></body></html>'''

@app.get("/")
def home():
    return render_template_string(HTML)

with app.app_context():
    db()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
