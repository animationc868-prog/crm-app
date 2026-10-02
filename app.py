import os
import sqlite3
import secrets
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import Flask, request, jsonify, session, redirect

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================

app.secret_key = os.getenv("SESSION_SECRET", "CHANGE_THIS_SECRET")

DB = os.getenv("DATABASE_PATH", "customer_growth.db")

SELAR_PRODUCT_URL = os.getenv(
    "SELAR_PRODUCT_URL",
    "https://selar.com/9u69r59d57"
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

CREATOR_EMAIL = os.getenv(
    "CREATOR_EMAIL",
    "dayotimileyin831@gmail.com"
)

CREATOR_PASSWORD = os.getenv(
    "CREATOR_PASSWORD",
    "CHANGE_THIS_PASSWORD"
)

PLANS = {
    "monthly": {
        "name": "1 Month",
        "price": 19,
        "days": 31
    },
    "six_months": {
        "name": "6 Months",
        "price": 79,
        "days": 183
    },
    "yearly": {
        "name": "1 Year",
        "price": 149,
        "days": 365
    }
}


# ============================================================
# DATABASE
# ============================================================

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
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan TEXT NOT NULL,
        status TEXT NOT NULL,
        access_code TEXT UNIQUE,
        starts_at TEXT,
        expires_at TEXT,
        payment_reference TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        email TEXT,
        phone TEXT,
        company TEXT,
        status TEXT DEFAULT 'NEW',
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS webhook_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT UNIQUE,
        payload TEXT,
        created_at TEXT NOT NULL
    );
    """)

    conn.commit()
    conn.close()


init_db()


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now(timezone.utc)


def now_text():
    return now().isoformat()


def hash_password(password):
    salt = secrets.token_bytes(16)

    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt,
        210000
    )

    return salt.hex() + ":" + key.hex()


def check_password(password, stored):
    try:
        salt_hex, key_hex = stored.split(":")

        new_key = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            bytes.fromhex(salt_hex),
            210000
        )

        return hmac.compare_digest(
            new_key.hex(),
            key_hex
        )

    except Exception:
        return False


def generate_access_code():
    while True:

        code = (
            "AGC-"
            + secrets.token_hex(3).upper()
            + "-"
            + secrets.token_hex(3).upper()
        )

        conn = db()

        exists = conn.execute(
            "SELECT id FROM subscriptions WHERE access_code=?",
            (code,)
        ).fetchone()

        conn.close()

        if not exists:
            return code


def current_user():
    uid = session.get("user_id")

    if not uid:
        return None

    conn = db()

    user = conn.execute(
        """
        SELECT id,name,business,email,created_at
        FROM users
        WHERE id=?
        """,
        (uid,)
    ).fetchone()

    conn.close()

    return user


def active_subscription(user_id):

    conn = db()

    sub = conn.execute(
        """
        SELECT *
        FROM subscriptions
        WHERE user_id=?
        AND status='active'
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,)
    ).fetchone()

    if sub and sub["expires_at"]:

        try:
            expires = datetime.fromisoformat(
                sub["expires_at"]
            )

            if expires <= now():

                conn.execute(
                    """
                    UPDATE subscriptions
                    SET status='expired'
                    WHERE id=?
                    """,
                    (sub["id"],)
                )

                conn.commit()
                sub = None

        except Exception:
            pass

    conn.close()

    return sub


def login_required(fn):

    @wraps(fn)
    def wrapper(*args, **kwargs):

        if not current_user():
            return jsonify({
                "ok": False,
                "error": "Login required"
            }), 401

        return fn(*args, **kwargs)

    return wrapper


def paid_required(fn):

    @wraps(fn)
    def wrapper(*args, **kwargs):

        user = current_user()

        if not user:
            return jsonify({
                "ok": False,
                "error": "Login required"
            }), 401

        sub = active_subscription(user["id"])

        if not sub:
            return jsonify({
                "ok": False,
                "error": "Active subscription required",
                "locked": True
            }), 403

        return fn(*args, **kwargs)

    return wrapper


# ============================================================
# PLAN DETECTION
# ============================================================

def detect_plan(value):

    if not value:
        return None

    text = str(value).lower().strip()

    # Six months
    if (
        "six" in text
        or "6 month" in text
        or "6_month" in text
        or "79" in text
    ):
        return "six_months"

    # One year
    if (
        "year" in text
        or "12 month" in text
        or "12_month" in text
        or "149" in text
    ):
        return "yearly"

    # One month
    if (
        "month" in text
        or "monthly" in text
        or "19" in text
    ):
        return "monthly"

    return None


def find_value(data, names):

    if not isinstance(data, dict):
        return None

    lower = {
        str(k).lower(): v
        for k, v in data.items()
    }

    for name in names:

        if name.lower() in lower:
            return lower[name.lower()]

    return None


# ============================================================
# HOME / FRONTEND
# ============================================================

@app.route("/")
def home():

    return HTML


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "ok": True,
        "service": "AI Customer Growth System"
    })


# ============================================================
# PLANS
# ============================================================

@app.get("/api/plans")
def plans():

    return jsonify({
        "ok": True,
        "plans": PLANS
    })


# ============================================================
# REGISTER
# ============================================================

@app.post("/api/register")
def register():

    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()
    business = str(data.get("business", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    if not name or not business or not email or not password:
        return jsonify({
            "ok": False,
            "error": "Complete all fields"
        }), 400

    if len(password) < 6:
        return jsonify({
            "ok": False,
            "error": "Password must be at least 6 characters"
        }), 400

    conn = db()

    existing = conn.execute(
        "SELECT id FROM users WHERE email=?",
        (email,)
    ).fetchone()

    if existing:
        conn.close()

        return jsonify({
            "ok": False,
            "error": "An account with this email already exists"
        }), 409

    cur = conn.execute(
        """
        INSERT INTO users
        (name,business,email,password_hash,created_at)
        VALUES (?,?,?,?,?)
        """,
        (
            name,
            business,
            email,
            hash_password(password),
            now_text()
        )
    )

    conn.commit()

    user_id = cur.lastrowid

    conn.close()

    session["user_id"] = user_id

    return jsonify({
        "ok": True,
        "message": "Account created"
    })


# ============================================================
# LOGIN
# ============================================================

@app.post("/api/login")
def login():

    data = request.get_json(silent=True) or {}

    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    conn = db()

    user = conn.execute(
        "SELECT * FROM users WHERE email=?",
        (email,)
    ).fetchone()

    conn.close()

    if not user or not check_password(
        password,
        user["password_hash"]
    ):
        return jsonify({
            "ok": False,
            "error": "Invalid email or password"
        }), 401

    session["user_id"] = user["id"]

    return jsonify({
        "ok": True
    })


# ============================================================
# LOGOUT
# ============================================================

@app.post("/api/logout")
def logout():

    session.clear()

    return jsonify({
        "ok": True
    })


# ============================================================
# CURRENT ACCOUNT
# ============================================================

@app.get("/api/me")
def me():

    user = current_user()

    if not user:
        return jsonify({
            "ok": True,
            "logged_in": False
        })

    sub = active_subscription(user["id"])

    return jsonify({
        "ok": True,
        "logged_in": True,
        "user": dict(user),
        "subscription": dict(sub) if sub else None
    })


# ============================================================
# CHECKOUT
# ============================================================

@app.post("/api/checkout")
@login_required
def checkout():

    data = request.get_json(silent=True) or {}

    plan = detect_plan(data.get("plan"))

    if plan not in PLANS:
        return jsonify({
            "ok": False,
            "error": "Invalid subscription plan"
        }), 400

    session["checkout_plan"] = plan

    return jsonify({
        "ok": True,
        "checkout_url": SELAR_PRODUCT_URL,
        "plan": PLANS[plan]
    })


# ============================================================
# PAYMENT STATUS
# ============================================================

@app.get("/api/payment-status")
@login_required
def payment_status():

    user = current_user()

    sub = active_subscription(user["id"])

    if not sub:

        return jsonify({
            "ok": True,
            "active": False
        })

    return jsonify({
        "ok": True,
        "active": True,
        "subscription": dict(sub)
    })


# ============================================================
# ACTIVATE ACCESS CODE
# ============================================================

@app.post("/api/activate")
@login_required
def activate():

    data = request.get_json(silent=True) or {}

    supplied = str(
        data.get("code", "")
    ).strip().upper()

    user = current_user()

    conn = db()

    sub = conn.execute(
        """
        SELECT *
        FROM subscriptions
        WHERE access_code=?
        """,
        (supplied,)
    ).fetchone()

    if not sub:
        conn.close()

        return jsonify({
            "ok": False,
            "error": "Invalid access code"
        }), 404

    if sub["user_id"] != user["id"]:
        conn.close()

        return jsonify({
            "ok": False,
            "error": "This access code belongs to another account"
        }), 403

    if sub["status"] != "active":
        conn.close()

        return jsonify({
            "ok": False,
            "error": "This subscription is not active"
        }), 403

    conn.close()

    return jsonify({
        "ok": True,
        "message": "Access activated"
    })


# ============================================================
# LEADS
# ============================================================

@app.get("/api/leads")
@paid_required
def get_leads():

    user = current_user()

    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM leads
        WHERE user_id=?
        ORDER BY id DESC
        """,
        (user["id"],)
    ).fetchall()

    conn.close()

    return jsonify({
        "ok": True,
        "leads": [dict(x) for x in rows]
    })


@app.post("/api/leads")
@paid_required
def add_lead():

    data = request.get_json(silent=True) or {}

    name = str(data.get("name", "")).strip()

    if not name:
        return jsonify({
            "ok": False,
            "error": "Lead name is required"
        }), 400

    user = current_user()

    conn = db()

    cur = conn.execute(
        """
        INSERT INTO leads
        (user_id,name,email,phone,company,status,notes,created_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            user["id"],
            name,
            data.get("email"),
            data.get("phone"),
            data.get("company"),
            data.get("status", "NEW"),
            data.get("notes"),
            now_text()
        )
    )

    conn.commit()

    lead_id = cur.lastrowid

    conn.close()

    return jsonify({
        "ok": True,
        "id": lead_id
    })


@app.delete("/api/leads/<int:lead_id>")
@paid_required
def delete_lead(lead_id):

    user = current_user()

    conn = db()

    conn.execute(
        """
        DELETE FROM leads
        WHERE id=? AND user_id=?
        """,
        (
            lead_id,
            user["id"]
        )
    )

    conn.commit()
    conn.close()

    return jsonify({
        "ok": True
    })


# ============================================================
# GEMINI AI
# ============================================================

@app.post("/api/ai/reply")
@paid_required
def ai_reply():

    if not GEMINI_API_KEY:

        return jsonify({
            "ok": False,
            "error": "Gemini API is not configured yet"
        }), 503

    data = request.get_json(silent=True) or {}

    message = str(
        data.get("message", "")
    ).strip()

    business = str(
        data.get("business", "")
    ).strip()

    if not message:
        return jsonify({
            "ok": False,
            "error": "Enter the customer's message"
        }), 400

    prompt = f"""
You are an AI customer communication assistant.

Business:
{business}

Customer message:
{message}

Write a professional, friendly and natural reply.

Do not claim discounts, prices, appointments,
refunds or promises that were not provided.

Keep the reply concise.
"""

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        + GEMINI_MODEL
        + ":generateContent"
    )

    try:

        response = requests.post(
            url,
            params={
                "key": GEMINI_API_KEY
            },
            json={
                "contents": [
                    {
                        "parts": [
                            {
                                "text": prompt
                            }
                        ]
                    }
                ]
            },
            timeout=30
        )

        if response.status_code != 200:

            return jsonify({
                "ok": False,
                "error": "Gemini request failed"
            }), 502

        result = response.json()

        text = (
            result
            .get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )

        return jsonify({
            "ok": True,
            "reply": text
        })

    except Exception:

        return jsonify({
            "ok": False,
            "error": "AI service temporarily unavailable"
        }), 502


# ============================================================
# CREATOR LOGIN
# ============================================================

@app.post("/api/creator/login")
def creator_login():

    data = request.get_json(silent=True) or {}

    email = str(
        data.get("email", "")
    ).strip().lower()

    password = str(
        data.get("password", "")
    )

    if (
        email == CREATOR_EMAIL.lower()
        and password == CREATOR_PASSWORD
    ):

        session["creator"] = True

        return jsonify({
            "ok": True
        })

    return jsonify({
        "ok": False,
        "error": "Invalid creator credentials"
    }), 401


# ============================================================
# CREATOR CUSTOMERS
# ============================================================

@app.get("/api/creator/customers")
def creator_customers():

    if not session.get("creator"):

        return jsonify({
            "ok": False,
            "error": "Creator login required"
        }), 401

    conn = db()

    rows = conn.execute(
        """
        SELECT
            u.id,
            u.name,
            u.business,
            u.email,
            u.created_at,
            s.plan,
            s.status,
            s.access_code,
            s.starts_at,
            s.expires_at,
            s.payment_reference
        FROM users u
        LEFT JOIN subscriptions s
        ON s.user_id=u.id
        ORDER BY u.id DESC
        """
    ).fetchall()

    conn.close()

    return jsonify({
        "ok": True,
        "customers": [dict(x) for x in rows]
    })


# ============================================================
# SELAR WEBHOOK
# ============================================================

@app.post("/api/webhooks/selar")
def selar_webhook():

    payload = request.get_json(
        silent=True
    ) or {}

    # Save event safely so duplicate events
    # do not create duplicate subscriptions.

    event_id = (
        request.headers.get("X-Selar-Event-ID")
        or request.headers.get("X-Event-ID")
        or payload.get("id")
        or payload.get("event_id")
        or payload.get("reference")
        or secrets.token_hex(16)
    )

    event_id = str(event_id)

    conn = db()

    already = conn.execute(
        """
        SELECT id
        FROM webhook_events
        WHERE event_key=?
        """,
        (event_id,)
    ).fetchone()

    if already:

        conn.close()

        return jsonify({
            "ok": True,
            "duplicate": True
        })

    # --------------------------------------------------------
    # Find buyer information from common payload locations.
    # The exact Selar payload will be confirmed when connected.
    # --------------------------------------------------------

    email = (
        find_value(
            payload,
            [
                "email",
                "customer_email",
                "buyer_email"
            ]
        )
    )
