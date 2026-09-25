"""
OrderFlow AI — backend v2.0
FastAPI + SQLite/PostgreSQL + bcrypt Auth + JWT Refresh Tokens + WebSocket live sync.
Admin Dashboard + Seller KYC + Support Chat + Gemini AI + PDF Invoices + Razorpay

Run:
    pip install -r requirements.txt
    export JWT_SECRET="something-long-and-random"
    export GEMINI_API_KEY="your-gemini-key"          # optional
    export RAZORPAY_KEY_ID="rzp_test_xxx"            # optional
    export RAZORPAY_KEY_SECRET="xxx"                 # optional
    export RESEND_API_KEY="re_xxx"                   # optional
    export DATABASE_URL="postgresql://..."           # optional, falls back to SQLite
    export ALLOWED_ORIGINS="https://yourapp.onrender.com,http://localhost:8000"
    uvicorn main:app --reload --port 8000
"""

import os
import json
import time
import uuid
import hmac
import hashlib
import io
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Header, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from passlib.context import CryptContext
import jwt

# ---------------------------------------------------------------- config

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-please-change-in-production")
JWT_ALGO = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7     # 7 days (generous for dev; tighten in prod)
REFRESH_TOKEN_EXPIRE_DAYS = 30

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "noreply@orderflow.ai")

DATABASE_URL = os.environ.get("DATABASE_URL", "")  # empty = use SQLite

_ALLOWED = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000,https://orderflow-ai.onrender.com"
)
ALLOWED_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

FRONTEND_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
)

# ---------------------------------------------------------------- password hashing
import bcrypt

def hash_pw(pw: str) -> str:
    pw_bytes = pw.strip().encode("utf-8")[:72]
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")

def verify_pw(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    # Support legacy SHA-256 hashes from v1 (migration path)
    if len(hashed) == 64 and not hashed.startswith("$2"):
        import hashlib
        return hashlib.sha256(plain.strip().encode("utf-8")).hexdigest() == hashed
    try:
        pw_bytes = plain.strip().encode("utf-8")[:72]
        hashed_bytes = hashed.strip().encode("utf-8")
        return bcrypt.checkpw(pw_bytes, hashed_bytes)
    except Exception:
        return False

# ---------------------------------------------------------------- database

DB_PATH = os.path.join(os.path.dirname(__file__), "orderflow.db")

def db():
    """Return a database connection. Uses PostgreSQL if DATABASE_URL is set, else SQLite."""
    if DATABASE_URL:
        try:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(DATABASE_URL)
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            return conn
        except Exception as e:
            print(f"[WARN] PostgreSQL connection failed ({e}), falling back to SQLite")

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _exec(conn, sql: str, params=()):
    """Execute helper compatible with both sqlite3 and psycopg2."""
    if DATABASE_URL:
        sql = sql.replace("?", "%s")
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


def _fetchone(conn, sql: str, params=()):
    cur = _exec(conn, sql, params)
    row = cur.fetchone()
    return dict(row) if row else None


def _fetchall(conn, sql: str, params=()):
    cur = _exec(conn, sql, params)
    return [dict(r) for r in cur.fetchall()]


def generate_id(role: str, conn) -> str:
    import random
    prefix_map = {"seller": 1, "customer": 7, "admin": 9}
    prefix = prefix_map.get(role, 8)
    for _ in range(200):
        candidate = f"{prefix}{random.randint(100000, 999999)}"
        exists = _fetchone(conn, "SELECT 1 FROM users WHERE id=?", (candidate,))
        if not exists:
            return candidate
    return str(uuid.uuid4())[:7]


def init_db():
    conn = db()

    # -- users --
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            google_sub TEXT,
            email TEXT,
            name TEXT,
            password_hash TEXT DEFAULT '',
            picture TEXT,
            role TEXT,
            status TEXT DEFAULT 'active',
            brand_name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            state TEXT DEFAULT '',
            brand_description TEXT DEFAULT '',
            product_category TEXT DEFAULT '',
            shipping_mode TEXT DEFAULT '',
            stock_address TEXT DEFAULT '',
            order_acceptance TEXT DEFAULT 'auto',
            rto_mode TEXT DEFAULT 'marketplace',
            store_logo TEXT DEFAULT '',
            store_banner TEXT DEFAULT '',
            brand_color TEXT DEFAULT '#6c63ff',
            rejection_reason TEXT DEFAULT '',
            verified_at TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            is_first_admin INTEGER DEFAULT 0
        )
    """)

    # -- refresh tokens --
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS refresh_tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT,
            expires_at TEXT,
            created_at TEXT
        )
    """)

    # -- products --
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT,
            price REAL,
            stock INTEGER,
            category TEXT,
            seller_id TEXT,
            seller_name TEXT DEFAULT '',
            icon TEXT DEFAULT '📦',
            description TEXT DEFAULT ''
        )
    """)

    # -- orders --
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            customer_id TEXT,
            customer_name TEXT,
            seller_id TEXT,
            seller_name TEXT DEFAULT '',
            items TEXT,
            amount REAL,
            status TEXT,
            eta TEXT,
            delay_reason TEXT DEFAULT '',
            payment_method TEXT DEFAULT 'UPI',
            payment_status TEXT DEFAULT 'Paid',
            razorpay_order_id TEXT DEFAULT '',
            razorpay_payment_id TEXT DEFAULT '',
            coupon_code TEXT DEFAULT '',
            discount_amount REAL DEFAULT 0.0,
            shipping_address TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT
        )
    """)

    # -- coupons --
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS coupons (
            id TEXT PRIMARY KEY,
            code TEXT UNIQUE,
            discount_type TEXT,
            discount_value REAL,
            min_order REAL,
            seller_id TEXT DEFAULT 'all',
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        )
    """)

    # -- order messages (customer <-> seller) --
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS order_messages (
            id TEXT PRIMARY KEY,
            order_id TEXT,
            sender_id TEXT,
            sender_name TEXT,
            sender_role TEXT,
            message TEXT,
            timestamp TEXT
        )
    """)

    # -- support tickets (customer/seller -> admin) --
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS support_tickets (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            user_name TEXT,
            user_role TEXT,
            category TEXT,
            description TEXT,
            rating INTEGER DEFAULT 0,
            status TEXT DEFAULT 'open',
            admin_reply TEXT DEFAULT '',
            resolved_by TEXT DEFAULT '',
            created_at TEXT,
            resolved_at TEXT DEFAULT ''
        )
    """)

    # -- support chat messages (ticket-scoped) --
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS support_messages (
            id TEXT PRIMARY KEY,
            ticket_id TEXT,
            sender_id TEXT,
            sender_name TEXT,
            sender_role TEXT,
            message TEXT,
            timestamp TEXT
        )
    """)

    # Dynamic column migrations for existing DBs
    try:
        for col, defn in [
            ("status", "TEXT DEFAULT 'active'"),
            ("brand_name", "TEXT DEFAULT ''"),
            ("phone", "TEXT DEFAULT ''"),
            ("state", "TEXT DEFAULT ''"),
            ("brand_description", "TEXT DEFAULT ''"),
            ("product_category", "TEXT DEFAULT ''"),
            ("shipping_mode", "TEXT DEFAULT ''"),
            ("stock_address", "TEXT DEFAULT ''"),
            ("order_acceptance", "TEXT DEFAULT 'auto'"),
            ("rto_mode", "TEXT DEFAULT 'marketplace'"),
            ("store_logo", "TEXT DEFAULT ''"),
            ("store_banner", "TEXT DEFAULT ''"),
            ("brand_color", "TEXT DEFAULT '#6c63ff'"),
            ("rejection_reason", "TEXT DEFAULT ''"),
            ("verified_at", "TEXT DEFAULT ''"),
            ("is_first_admin", "INTEGER DEFAULT 0"),
        ]:
            try:
                _exec(conn, f"ALTER TABLE users ADD COLUMN {col} {defn}")
            except Exception:
                pass

        for col, defn in [("description", "TEXT DEFAULT ''")]:
            try:
                _exec(conn, f"ALTER TABLE products ADD COLUMN {col} {defn}")
            except Exception:
                pass

        for col, defn in [
            ("razorpay_order_id", "TEXT DEFAULT ''"),
            ("razorpay_payment_id", "TEXT DEFAULT ''"),
        ]:
            try:
                _exec(conn, f"ALTER TABLE orders ADD COLUMN {col} {defn}")
            except Exception:
                pass
    except Exception:
        pass

    # Seed default sellers & customers (kept for demo / quick-test)
    default_users = [
        ("1001001", "TechNova Electronics", "technova@seller.orderflow.ai", "seller123", "seller",
         "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=100&h=100&fit=crop"),
        ("1002002", "Aura Home Living", "aurahome@seller.orderflow.ai", "seller123", "seller",
         "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=100&h=100&fit=crop"),
        ("1003003", "Titanium Fitness Gear", "fitgear@seller.orderflow.ai", "seller123", "seller",
         "https://images.unsplash.com/photo-1517841905240-472988babdf9?w=100&h=100&fit=crop"),
        ("7001001", "Alex Rivera", "alex@customer.orderflow.ai", "customer123", "customer",
         "https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=100&h=100&fit=crop"),
        ("7002002", "Priya Sharma", "priya@customer.orderflow.ai", "customer123", "customer",
         "https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=100&h=100&fit=crop"),
    ]
    for uid, uname, uemail, upw, urole, upic in default_users:
        existing = _fetchone(conn, "SELECT id, password_hash FROM users WHERE id=?", (uid,))
        if not existing:
            _exec(conn,
                """INSERT INTO users (id, name, email, password_hash, role, picture, status, created_at)
                   VALUES (?,?,?,?,?,?,'active',?)""",
                (uid, uname, uemail, hash_pw(upw), urole, upic, datetime.utcnow().isoformat()))
        else:
            # Upgrade legacy SHA-256 hashes to bcrypt on restart
            ph = existing.get("password_hash", "") if isinstance(existing, dict) else existing["password_hash"]
            if ph and len(ph) == 64 and not ph.startswith("$2"):
                _exec(conn, "UPDATE users SET password_hash=? WHERE id=?", (hash_pw(upw), uid))

    # Seed products
    seller_seed_products = [
        ("1001001", "TechNova Electronics", [
            ("Wireless Earbuds Pro", 2499, 40, "Electronics", "🎧"),
            ("Neon Mechanical Keyboard", 4599, 15, "Electronics", "⌨️"),
        ]),
        ("1002002", "Aura Home Living", [
            ("Aurora Desk Lamp", 1299, 25, "Home", "💡"),
            ("Cosmic Ceramic Mug", 349, 100, "Home", "☕"),
        ]),
        ("1003003", "Titanium Fitness Gear", [
            ("Glow Yoga Mat", 999, 30, "Fitness", "🧘"),
            ("Titanium Water Bottle", 799, 60, "Fitness", "🧴"),
        ]),
    ]
    for sid, sname, prod_list in seller_seed_products:
        for pname, price, stock, cat, icon in prod_list:
            existing = _fetchone(conn, "SELECT id FROM products WHERE name=? AND seller_id=?", (pname, sid))
            if not existing:
                _exec(conn,
                    "INSERT INTO products (id, name, price, stock, category, seller_id, seller_name, icon) VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4())[:8].upper(), pname, price, stock, cat, sid, sname, icon))

    # Seed default coupons
    default_coupons = [
        ("COUP-W50", "WELCOME50", "flat", 50.0, 200.0, "all"),
        ("COUP-F15", "FESTIVE15", "percent", 15.0, 500.0, "all"),
        ("COUP-FS", "FREESHIP", "flat", 40.0, 300.0, "all"),
        ("COUP-TECH", "TECH10", "percent", 10.0, 1000.0, "1001001"),
    ]
    for cid, code, dtype, dval, min_ord, sid in default_coupons:
        if not _fetchone(conn, "SELECT id FROM coupons WHERE code=?", (code,)):
            _exec(conn,
                "INSERT INTO coupons (id, code, discount_type, discount_value, min_order, seller_id, is_active, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (cid, code, dtype, dval, min_ord, sid, 1, datetime.utcnow().isoformat()))

    conn.commit()
    conn.close()


# ---------------------------------------------------------------- app

app = FastAPI(title="OrderFlow AI v2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)


@app.get("/")
def serve_index():
    if os.path.exists(FRONTEND_FILE):
        return FileResponse(FRONTEND_FILE)
    return {"message": "OrderFlow AI API running. Visit /docs for Swagger."}


@app.get("/manifest.json")
def serve_manifest():
    manifest_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "manifest.json"))
    if os.path.exists(manifest_path):
        return FileResponse(manifest_path, media_type="application/manifest+json")
    raise HTTPException(404, "manifest.json not found")


@app.get("/sw.js")
def serve_sw():
    sw_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "sw.js"))
    if os.path.exists(sw_path):
        return FileResponse(sw_path, media_type="application/javascript")
    raise HTTPException(404, "sw.js not found")


init_db()

# ---------------------------------------------------------------- JWT auth

def make_jwt(user: dict, token_type: str = "access") -> str:
    if token_type == "refresh":
        exp = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    else:
        exp = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": user["id"] if "id" in user else user["sub"],
        "role": user["role"],
        "name": user["name"],
        "email": user.get("email", ""),
        "type": token_type,
        "exp": exp,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired — please refresh")
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid token")
    if payload.get("type") == "refresh":
        raise HTTPException(401, "Cannot use refresh token as access token")
    return payload


def require_admin(user=Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    # Verify the admin account is active
    conn = db()
    row = _fetchone(conn, "SELECT status FROM users WHERE id=?", (user["sub"],))
    conn.close()
    if not row or row["status"] != "active":
        raise HTTPException(403, "Your admin account is not active")
    return user


# ---------------------------------------------------------------- admin bootstrap (first-run only)

class AdminSetupBody(BaseModel):
    name: str
    email: str
    password: str


@app.get("/admin/setup")
def admin_setup_status():
    conn = db()
    count = _fetchone(conn, "SELECT COUNT(*) as c FROM users WHERE role='admin'", ())
    conn.close()
    c = count["c"] if count else 0
    if c > 0:
        raise HTTPException(403, "Admin already configured. Setup is disabled.")
    return {"available": True, "message": "No admin exists yet. POST to /admin/setup to create the first admin."}


@app.post("/admin/setup")
def create_first_admin(body: AdminSetupBody):
    if len(body.name.strip()) < 2:
        raise HTTPException(400, "Name must be at least 2 characters")
    if len(body.password.strip()) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if "@" not in body.email:
        raise HTTPException(400, "Invalid email address")

    conn = db()
    count = _fetchone(conn, "SELECT COUNT(*) as c FROM users WHERE role='admin'", ())
    c = count["c"] if count else 0
    if c > 0:
        conn.close()
        raise HTTPException(403, "Admin already exists. This endpoint is disabled.")

    new_id = generate_id("admin", conn)
    pic = f"https://api.dicebear.com/7.x/identicon/svg?seed={body.name.strip()}"
    now = datetime.utcnow().isoformat()

    _exec(conn,
        """INSERT INTO users (id, name, email, password_hash, role, picture, status, is_first_admin, created_at)
           VALUES (?,?,?,?,'admin',?,'active',1,?)""",
        (new_id, body.name.strip(), body.email.strip(), hash_pw(body.password), pic, now))
    conn.commit()
    user = {"id": new_id, "name": body.name.strip(), "email": body.email.strip(), "role": "admin"}
    conn.close()

    access_token = make_jwt(user, "access")
    return {
        "message": f"✅ First admin created! Your Admin ID is: {new_id}. Save this securely.",
        "admin_id": new_id,
        "access_token": access_token,
    }


# ---------------------------------------------------------------- WebSocket connection manager

class ConnectionManager:
    def __init__(self):
        self.by_role: dict[str, list[WebSocket]] = {"customer": [], "seller": [], "admin": []}
        self.by_user: dict[str, list[WebSocket]] = {}

    async def connect(self, ws: WebSocket, role: str, user_id: str):
        await ws.accept()
        self.by_role.setdefault(role, []).append(ws)
        self.by_user.setdefault(user_id, []).append(ws)

    def disconnect(self, ws: WebSocket, role: str, user_id: str):
        if ws in self.by_role.get(role, []):
            self.by_role[role].remove(ws)
        if ws in self.by_user.get(user_id, []):
            self.by_user[user_id].remove(ws)

    async def broadcast_role(self, role: str, message: dict):
        dead = []
        for ws in self.by_role.get(role, []):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.by_role.get(role, []):
                self.by_role[role].remove(ws)

    async def broadcast_all(self, message: dict):
        for role in ("customer", "seller", "admin"):
            await self.broadcast_role(role, message)

    async def send_user(self, user_id: str, message: dict):
        dead = []
        for ws in self.by_user.get(user_id, []):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.by_user.get(user_id, []):
                self.by_user[user_id].remove(ws)


manager = ConnectionManager()


@app.websocket("/ws/{role}/{user_id}")
async def ws_endpoint(websocket: WebSocket, role: str, user_id: str):
    await manager.connect(websocket, role, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, role, user_id)


# ---------------------------------------------------------------- auth endpoints

class LoginBody(BaseModel):
    id: str
    password: str
    role: str


@app.post("/api/auth/login")
def login(body: LoginBody):
    clean_id = body.id.strip()
    role = body.role.strip().lower()

    # Admin login uses 9-digit IDs; seller 1-digit prefix; customer 7-digit prefix
    if role == "admin":
        if not clean_id.isdigit() or len(clean_id) != 7:
            raise HTTPException(400, "Admin ID must be exactly 7 digits")
    elif role in ("customer", "seller"):
        if not clean_id.isdigit() or len(clean_id) != 7:
            raise HTTPException(400, "ID must be exactly 7 digits")
    else:
        raise HTTPException(400, "Role must be customer, seller, or admin")

    conn = db()
    row = _fetchone(conn, "SELECT * FROM users WHERE id=? AND role=?", (clean_id, role))
    if not row:
        conn.close()
        raise HTTPException(401, f"No {role} account found with ID '{clean_id}'")

    if row.get("status") == "pending_verification":
        conn.close()
        raise HTTPException(403, "Your seller account is pending admin verification. Please wait for approval.")

    if row.get("status") == "pending_admin_approval":
        conn.close()
        raise HTTPException(403, "Your admin account is pending approval from the existing admin.")

    if row.get("status") in ("rejected", "suspended"):
        reason = row.get("rejection_reason", "")
        conn.close()
        raise HTTPException(403, f"Account {'rejected' if row['status'] == 'rejected' else 'suspended'}. {reason}")

    if not verify_pw(body.password, row.get("password_hash", "")):
        conn.close()
        raise HTTPException(401, "Invalid password")

    # Upgrade legacy hash on successful login
    if row.get("password_hash", "") and len(row["password_hash"]) == 64:
        _exec(conn, "UPDATE users SET password_hash=? WHERE id=?", (hash_pw(body.password), clean_id))
        conn.commit()

    conn.close()
    access_token = make_jwt(row, "access")
    refresh_token = make_jwt(row, "refresh")

    # Store refresh token
    _store_refresh_token(refresh_token, row["id"])

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": _safe_user(row),
    }


def _store_refresh_token(token: str, user_id: str):
    conn = db()
    _exec(conn, "DELETE FROM refresh_tokens WHERE user_id=? AND expires_at < ?",
          (user_id, datetime.utcnow().isoformat()))
    _exec(conn,
        "INSERT INTO refresh_tokens (token, user_id, expires_at, created_at) VALUES (?,?,?,?)",
        (token, user_id,
         (datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)).isoformat(),
         datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def _safe_user(row: dict) -> dict:
    """Return user dict without sensitive fields."""
    return {k: v for k, v in row.items() if k not in ("password_hash", "google_sub")}


class RefreshBody(BaseModel):
    refresh_token: str


@app.post("/api/auth/refresh")
def refresh_token_endpoint(body: RefreshBody):
    try:
        payload = jwt.decode(body.refresh_token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Refresh token expired — please login again")
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid refresh token")

    if payload.get("type") != "refresh":
        raise HTTPException(401, "Not a refresh token")

    conn = db()
    stored = _fetchone(conn, "SELECT * FROM refresh_tokens WHERE token=?", (body.refresh_token,))
    conn.close()
    if not stored:
        raise HTTPException(401, "Refresh token revoked or not found")

    # Issue new access token
    user_row = _fetchone(db(), "SELECT * FROM users WHERE id=?", (payload["sub"],))
    if not user_row:
        raise HTTPException(401, "User not found")

    new_access = make_jwt(user_row, "access")
    return {"access_token": new_access}


class LogoutBody(BaseModel):
    refresh_token: Optional[str] = None


@app.post("/api/auth/logout")
def logout(body: LogoutBody):
    if body.refresh_token:
        conn = db()
        _exec(conn, "DELETE FROM refresh_tokens WHERE token=?", (body.refresh_token,))
        conn.commit()
        conn.close()
    return {"success": True}


class RegisterBody(BaseModel):
    name: str
    password: str
    role: str
    email: Optional[str] = None
    # Seller KYC fields
    brand_name: Optional[str] = ""
    phone: Optional[str] = ""
    state: Optional[str] = ""
    brand_description: Optional[str] = ""
    product_category: Optional[str] = ""
    shipping_mode: Optional[str] = ""
    stock_address: Optional[str] = ""
    order_acceptance: Optional[str] = "auto"
    rto_mode: Optional[str] = "marketplace"


@app.post("/api/auth/register")
async def register(body: RegisterBody):
    role = body.role.strip().lower()
    if role not in ("customer", "seller"):
        raise HTTPException(400, "Role must be customer or seller")
    if len(body.name.strip()) < 2:
        raise HTTPException(400, "Name must be at least 2 characters")
    if len(body.password.strip()) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")

    # Sellers MUST complete KYC fields
    if role == "seller":
        if not body.brand_name or not body.brand_name.strip():
            raise HTTPException(400, "Brand/store name is required for sellers")
        if not body.phone or not body.phone.strip():
            raise HTTPException(400, "Phone number is required for sellers")
        if not body.state or not body.state.strip():
            raise HTTPException(400, "State is required for sellers")
        if not body.stock_address or not body.stock_address.strip():
            raise HTTPException(400, "Warehouse/stock address is required for sellers")

    conn = db()
    new_id = generate_id(role, conn)
    email = body.email.strip() if (body.email and body.email.strip()) else f"{new_id}@{role}.orderflow.ai"
    pw_hash = hash_pw(body.password)
    now = datetime.utcnow().isoformat()
    pic = f"https://api.dicebear.com/7.x/identicon/svg?seed={body.name.strip()}"

    # Sellers start as pending_verification; customers go active immediately
    status = "pending_verification" if role == "seller" else "active"

    _exec(conn,
        """INSERT INTO users (id, name, email, password_hash, role, picture, status,
                              brand_name, phone, state, brand_description, product_category,
                              shipping_mode, stock_address, order_acceptance, rto_mode, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (new_id, body.name.strip(), email, pw_hash, role, pic, status,
         (body.brand_name or "").strip(),
         (body.phone or "").strip(),
         (body.state or "").strip(),
         (body.brand_description or "").strip(),
         (body.product_category or "").strip(),
         (body.shipping_mode or "").strip(),
         (body.stock_address or "").strip(),
         body.order_acceptance or "auto",
         body.rto_mode or "marketplace",
         now))
    conn.commit()
    conn.close()

    # Notify admins of new seller pending verification
    if role == "seller":
        await manager.broadcast_role("admin", {
            "type": "new_seller_verification",
            "seller_id": new_id,
            "seller_name": body.name.strip(),
            "brand_name": body.brand_name,
        })
        _send_email_async(
            email, "OrderFlow — Registration Received",
            f"Hi {body.name.strip()},\n\nYour seller account ({new_id}) has been submitted for verification. "
            f"We'll notify you once the admin reviews your application.\n\nOrderFlow Team"
        )
        return {
            "message": "✅ Seller registration submitted! Your account is pending admin verification. "
                       f"Your Seller ID is: {new_id}. You will receive an email once approved.",
            "generated_id": new_id,
            "status": "pending_verification",
        }

    user_row = {"id": new_id, "name": body.name.strip(), "email": email, "role": role, "picture": pic}
    access_token = make_jwt(user_row, "access")
    refresh_token = make_jwt(user_row, "refresh")
    _store_refresh_token(refresh_token, new_id)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user": user_row,
        "generated_id": new_id,
        "message": f"✅ Account created! Your 7-digit Customer ID is: {new_id}",
    }


class AdminRegisterBody(BaseModel):
    name: str
    email: str
    password: str
    reason: Optional[str] = ""  # Why they want admin access


@app.post("/api/auth/admin-register")
async def admin_register(body: AdminRegisterBody):
    """Apply to become an admin — requires existing admin approval."""
    if len(body.name.strip()) < 2:
        raise HTTPException(400, "Name must be at least 2 characters")
    if len(body.password.strip()) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if "@" not in body.email:
        raise HTTPException(400, "Invalid email")

    conn = db()
    # Must have at least one active admin
    existing_admin = _fetchone(conn, "SELECT 1 FROM users WHERE role='admin' AND status='active'", ())
    if not existing_admin:
        conn.close()
        raise HTTPException(400, "No admin exists yet. Use /admin/setup to create the first admin.")

    new_id = generate_id("admin", conn)
    pic = f"https://api.dicebear.com/7.x/identicon/svg?seed={body.name.strip()}"
    now = datetime.utcnow().isoformat()

    _exec(conn,
        """INSERT INTO users (id, name, email, password_hash, role, picture, status,
                              brand_description, created_at)
           VALUES (?,?,?,?,'admin',?,'pending_admin_approval',?,?)""",
        (new_id, body.name.strip(), body.email.strip(), hash_pw(body.password), pic,
         (body.reason or "").strip(), now))
    conn.commit()
    conn.close()

    await manager.broadcast_role("admin", {
        "type": "new_admin_request",
        "admin_id": new_id,
        "name": body.name.strip(),
        "email": body.email.strip(),
    })

    return {
        "message": f"Admin application submitted. Your ID {new_id} is awaiting approval.",
        "generated_id": new_id,
        "status": "pending_admin_approval",
    }


@app.get("/api/auth/quick-profiles")
def quick_profiles():
    conn = db()
    rows = _fetchall(conn, "SELECT id, name, email, role, picture, status FROM users ORDER BY id ASC")
    conn.close()
    sellers = [r for r in rows if r["role"] == "seller" and r.get("status") == "active"]
    customers = [r for r in rows if r["role"] == "customer" and r.get("status") == "active"]
    return {"sellers": sellers, "customers": customers}


# ---------------------------------------------------------------- admin dashboard endpoints

@app.get("/api/admin/stats")
def admin_stats(admin=Depends(require_admin)):
    conn = db()
    total_users = _fetchone(conn, "SELECT COUNT(*) as c FROM users WHERE role IN ('customer','seller')", ())
    total_orders = _fetchone(conn, "SELECT COUNT(*) as c FROM orders", ())
    total_revenue = _fetchone(conn, "SELECT SUM(amount) as s FROM orders WHERE status != 'Cancelled'", ())
    pending_sellers = _fetchone(conn, "SELECT COUNT(*) as c FROM users WHERE role='seller' AND status='pending_verification'", ())
    pending_admins = _fetchone(conn, "SELECT COUNT(*) as c FROM users WHERE role='admin' AND status='pending_admin_approval'", ())
    open_tickets = _fetchone(conn, "SELECT COUNT(*) as c FROM support_tickets WHERE status='open'", ())
    total_sellers = _fetchone(conn, "SELECT COUNT(*) as c FROM users WHERE role='seller' AND status='active'", ())
    total_customers = _fetchone(conn, "SELECT COUNT(*) as c FROM users WHERE role='customer'", ())
    recent_orders = _fetchall(conn, "SELECT * FROM orders ORDER BY created_at DESC LIMIT 10")
    conn.close()

    for o in recent_orders:
        try:
            o["items"] = json.loads(o["items"])
        except Exception:
            o["items"] = []

    return {
        "total_users": (total_users or {}).get("c", 0),
        "total_sellers": (total_sellers or {}).get("c", 0),
        "total_customers": (total_customers or {}).get("c", 0),
        "total_orders": (total_orders or {}).get("c", 0),
        "total_revenue": round((total_revenue or {}).get("s") or 0, 2),
        "pending_seller_verifications": (pending_sellers or {}).get("c", 0),
        "pending_admin_approvals": (pending_admins or {}).get("c", 0),
        "open_support_tickets": (open_tickets or {}).get("c", 0),
        "recent_orders": recent_orders,
    }


@app.get("/api/admin/sellers/pending")
def admin_pending_sellers(admin=Depends(require_admin)):
    conn = db()
    rows = _fetchall(conn,
        "SELECT * FROM users WHERE role='seller' AND status='pending_verification' ORDER BY created_at DESC")
    conn.close()
    return [_safe_user(r) for r in rows]


@app.post("/api/admin/sellers/{seller_id}/approve")
async def admin_approve_seller(seller_id: str, admin=Depends(require_admin)):
    conn = db()
    row = _fetchone(conn, "SELECT * FROM users WHERE id=? AND role='seller'", (seller_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Seller not found")
    now = datetime.utcnow().isoformat()
    _exec(conn, "UPDATE users SET status='active', verified_at=?, rejection_reason='' WHERE id=?", (now, seller_id))
    conn.commit()
    conn.close()

    _send_email_async(
        row.get("email", ""),
        "OrderFlow — Seller Account Approved! 🎉",
        f"Hi {row['name']},\n\nGreat news! Your seller account ({seller_id}) has been approved. "
        f"You can now log in and start listing your products.\n\nOrderFlow Team"
    )
    await manager.send_user(seller_id, {"type": "account_approved", "message": "Your seller account has been approved!"})
    return {"success": True, "seller_id": seller_id, "status": "active"}


class RejectBody(BaseModel):
    reason: str = "Does not meet our seller requirements at this time."


@app.post("/api/admin/sellers/{seller_id}/reject")
async def admin_reject_seller(seller_id: str, body: RejectBody, admin=Depends(require_admin)):
    conn = db()
    row = _fetchone(conn, "SELECT * FROM users WHERE id=? AND role='seller'", (seller_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Seller not found")
    _exec(conn, "UPDATE users SET status='rejected', rejection_reason=? WHERE id=?",
          (body.reason.strip(), seller_id))
    conn.commit()
    conn.close()

    _send_email_async(
        row.get("email", ""),
        "OrderFlow — Seller Application Update",
        f"Hi {row['name']},\n\nWe regret to inform you that your seller application ({seller_id}) was not approved "
        f"at this time.\n\nReason: {body.reason}\n\nYou may re-apply after addressing these concerns.\n\nOrderFlow Team"
    )
    return {"success": True, "seller_id": seller_id, "status": "rejected"}


@app.get("/api/admin/users")
def admin_all_users(
    role: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    admin=Depends(require_admin)
):
    conn = db()
    query = "SELECT * FROM users WHERE 1=1"
    params = []
    if role:
        query += " AND role=?"
        params.append(role)
    if status:
        query += " AND status=?"
        params.append(status)
    if search:
        query += " AND (name LIKE ? OR email LIKE ? OR id LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term])
    query += " ORDER BY created_at DESC"
    rows = _fetchall(conn, query, tuple(params))
    conn.close()
    return [_safe_user(r) for r in rows]


@app.post("/api/admin/users/{user_id}/suspend")
async def admin_suspend_user(user_id: str, body: RejectBody, admin=Depends(require_admin)):
    conn = db()
    row = _fetchone(conn, "SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "User not found")
    # Protect first admin
    if row.get("is_first_admin") == 1:
        conn.close()
        raise HTTPException(403, "Cannot suspend the founding admin account")
    _exec(conn, "UPDATE users SET status='suspended', rejection_reason=? WHERE id=?",
          (body.reason.strip(), user_id))
    conn.commit()
    conn.close()
    await manager.send_user(user_id, {"type": "account_suspended", "message": body.reason})
    return {"success": True, "user_id": user_id, "status": "suspended"}


@app.post("/api/admin/users/{user_id}/activate")
async def admin_activate_user(user_id: str, admin=Depends(require_admin)):
    conn = db()
    _exec(conn, "UPDATE users SET status='active', rejection_reason='' WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    await manager.send_user(user_id, {"type": "account_activated", "message": "Your account has been reactivated."})
    return {"success": True, "user_id": user_id, "status": "active"}


@app.get("/api/admin/orders")
def admin_all_orders(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    admin=Depends(require_admin)
):
    conn = db()
    query = "SELECT * FROM orders WHERE 1=1"
    params = []
    if status and status != "All":
        query += " AND status=?"
        params.append(status)
    if search:
        query += " AND (id LIKE ? OR customer_name LIKE ? OR seller_name LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term])
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = _fetchall(conn, query, tuple(params))
    conn.close()
    for r in rows:
        try:
            r["items"] = json.loads(r["items"])
        except Exception:
            r["items"] = []
    return rows


@app.patch("/api/admin/orders/{order_id}/status")
async def admin_override_order_status(order_id: str, body, admin=Depends(require_admin)):
    conn = db()
    row = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Order not found")
    now = datetime.utcnow().isoformat()
    new_status = body if isinstance(body, str) else getattr(body, "status", "")
    _exec(conn, "UPDATE orders SET status=?, updated_at=? WHERE id=?", (new_status, now, order_id))
    conn.commit()
    updated = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    conn.close()
    if updated:
        updated["items"] = json.loads(updated.get("items", "[]"))
        await manager.send_user(updated["customer_id"], {"type": "status_update", "order": updated})
        await manager.send_user(updated["seller_id"], {"type": "status_update", "order": updated})
    return updated


@app.get("/api/admin/admins/pending")
def admin_pending_admins(admin=Depends(require_admin)):
    conn = db()
    rows = _fetchall(conn,
        "SELECT * FROM users WHERE role='admin' AND status='pending_admin_approval' ORDER BY created_at DESC")
    conn.close()
    return [_safe_user(r) for r in rows]


@app.post("/api/admin/admins/{target_id}/approve")
async def admin_approve_admin(target_id: str, admin=Depends(require_admin)):
    conn = db()
    row = _fetchone(conn, "SELECT * FROM users WHERE id=? AND role='admin'", (target_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Admin applicant not found")
    now = datetime.utcnow().isoformat()
    _exec(conn, "UPDATE users SET status='active', verified_at=? WHERE id=?", (now, target_id))
    conn.commit()
    conn.close()
    _send_email_async(
        row.get("email", ""),
        "OrderFlow — Admin Access Approved",
        f"Hi {row['name']},\n\nYour admin application has been approved. "
        f"You can now log in with your Admin ID: {target_id}\n\nOrderFlow Team"
    )
    await manager.send_user(target_id, {"type": "account_approved", "message": "Your admin access has been approved!"})
    return {"success": True, "admin_id": target_id, "status": "active"}


@app.post("/api/admin/admins/{target_id}/reject")
def admin_reject_admin(target_id: str, body: RejectBody, admin=Depends(require_admin)):
    conn = db()
    row = _fetchone(conn, "SELECT * FROM users WHERE id=? AND role='admin'", (target_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Admin applicant not found")
    _exec(conn, "UPDATE users SET status='rejected', rejection_reason=? WHERE id=?",
          (body.reason.strip(), target_id))
    conn.commit()
    conn.close()
    _send_email_async(
        row.get("email", ""),
        "OrderFlow — Admin Application Update",
        f"Hi {row['name']},\n\nYour admin application was not approved.\nReason: {body.reason}\n\nOrderFlow Team"
    )
    return {"success": True, "admin_id": target_id, "status": "rejected"}


# ---------------------------------------------------------------- support tickets & chat

class TicketBody(BaseModel):
    category: str
    description: str
    rating: Optional[int] = 0


@app.post("/api/support/ticket")
async def create_ticket(body: TicketBody, user=Depends(get_current_user)):
    conn = db()
    tid = str(uuid.uuid4())[:8].upper()
    now = datetime.utcnow().isoformat()
    _exec(conn,
        """INSERT INTO support_tickets (id, user_id, user_name, user_role, category, description, rating, status, created_at)
           VALUES (?,?,?,?,?,?,?,'open',?)""",
        (tid, user["sub"], user["name"], user["role"],
         body.category.strip(), body.description.strip(), body.rating or 0, now))
    conn.commit()
    conn.close()

    # Notify all admins
    await manager.broadcast_role("admin", {
        "type": "new_support_ticket",
        "ticket_id": tid,
        "user_name": user["name"],
        "user_role": user["role"],
        "category": body.category,
    })
    return {"ticket_id": tid, "status": "open", "message": "Your support request has been submitted. An agent will respond shortly."}


@app.get("/api/support/tickets")
def get_tickets(status: Optional[str] = None, admin=Depends(require_admin)):
    conn = db()
    if status:
        rows = _fetchall(conn, "SELECT * FROM support_tickets WHERE status=? ORDER BY created_at DESC", (status,))
    else:
        rows = _fetchall(conn, "SELECT * FROM support_tickets ORDER BY created_at DESC")
    conn.close()
    return rows


@app.get("/api/support/my-tickets")
def my_tickets(user=Depends(get_current_user)):
    conn = db()
    rows = _fetchall(conn, "SELECT * FROM support_tickets WHERE user_id=? ORDER BY created_at DESC", (user["sub"],))
    conn.close()
    return rows


class AdminReplyBody(BaseModel):
    reply: str
    resolve: Optional[bool] = False


@app.post("/api/support/tickets/{ticket_id}/reply")
async def admin_reply_ticket(ticket_id: str, body: AdminReplyBody, admin=Depends(require_admin)):
    conn = db()
    row = _fetchone(conn, "SELECT * FROM support_tickets WHERE id=?", (ticket_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Ticket not found")

    now = datetime.utcnow().isoformat()
    new_status = "resolved" if body.resolve else "in_progress"
    _exec(conn,
        "UPDATE support_tickets SET admin_reply=?, status=?, resolved_by=?, resolved_at=? WHERE id=?",
        (body.reply.strip(), new_status, admin["sub"], now if body.resolve else "", ticket_id))
    conn.commit()
    conn.close()

    # Send reply to the user via WebSocket
    await manager.send_user(row["user_id"], {
        "type": "support_reply",
        "ticket_id": ticket_id,
        "reply": body.reply.strip(),
        "status": new_status,
    })
    return {"success": True, "ticket_id": ticket_id, "status": new_status}


class SupportMessageBody(BaseModel):
    message: str


@app.post("/api/support/tickets/{ticket_id}/messages")
async def send_support_message(ticket_id: str, body: SupportMessageBody, user=Depends(get_current_user)):
    conn = db()
    ticket = _fetchone(conn, "SELECT * FROM support_tickets WHERE id=?", (ticket_id,))
    if not ticket:
        conn.close()
        raise HTTPException(404, "Ticket not found")
    # Only the ticket owner or an admin can message
    if user["role"] != "admin" and ticket["user_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")

    mid = str(uuid.uuid4())[:8].upper()
    now = datetime.utcnow().isoformat()
    msg_obj = {
        "id": mid, "ticket_id": ticket_id,
        "sender_id": user["sub"], "sender_name": user["name"],
        "sender_role": user["role"], "message": body.message.strip(), "timestamp": now,
    }
    _exec(conn,
        "INSERT INTO support_messages (id, ticket_id, sender_id, sender_name, sender_role, message, timestamp) VALUES (?,?,?,?,?,?,?)",
        (mid, ticket_id, user["sub"], user["name"], user["role"], body.message.strip(), now))
    conn.commit()
    conn.close()

    # Push to the other party
    if user["role"] == "admin":
        await manager.send_user(ticket["user_id"], {"type": "support_message", "message": msg_obj})
    else:
        await manager.broadcast_role("admin", {"type": "support_message", "message": msg_obj})

    return msg_obj


@app.get("/api/support/tickets/{ticket_id}/messages")
def get_support_messages(ticket_id: str, user=Depends(get_current_user)):
    conn = db()
    ticket = _fetchone(conn, "SELECT * FROM support_tickets WHERE id=?", (ticket_id,))
    if not ticket:
        conn.close()
        raise HTTPException(404, "Ticket not found")
    if user["role"] != "admin" and ticket["user_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")
    rows = _fetchall(conn, "SELECT * FROM support_messages WHERE ticket_id=? ORDER BY timestamp ASC", (ticket_id,))
    conn.close()
    return rows


# ---------------------------------------------------------------- AI (Gemini)

class AIDescribeBody(BaseModel):
    name: str
    category: str
    price: Optional[float] = None


@app.post("/api/ai/describe-product")
def ai_describe_product(body: AIDescribeBody, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can use AI product descriptions")
    if not GEMINI_API_KEY:
        return {"description": f"Premium quality {body.name} in the {body.category} category. "
                               f"{'Priced at ₹' + str(body.price) + '.' if body.price else ''} "
                               f"Excellent craftsmanship and reliable performance.",
                "note": "AI unavailable — set GEMINI_API_KEY for real AI descriptions"}
    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            f"Write a compelling 2-sentence product description for an Indian marketplace listing.\n"
            f"Product: {body.name}\nCategory: {body.category}"
            f"{f', Price: ₹{body.price}' if body.price else ''}\n"
            f"Be enthusiastic, highlight key benefits, and keep it under 60 words."
        )
        response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return {"description": response.text.strip()}
    except Exception as e:
        return {"description": f"Quality {body.name} for the discerning {body.category} buyer.",
                "error": str(e)}


@app.get("/api/ai/demand-insights/{seller_id}")
def ai_demand_insights(seller_id: str, user=Depends(get_current_user)):
    if user["role"] == "seller" and user["sub"] != seller_id:
        raise HTTPException(403, "Can only view your own insights")
    conn = db()
    orders = _fetchall(conn, "SELECT * FROM orders WHERE seller_id=? AND status != 'Cancelled'", (seller_id,))
    products = _fetchall(conn, "SELECT * FROM products WHERE seller_id=?", (seller_id,))
    conn.close()

    item_counts = {}
    for o in orders:
        items = json.loads(o.get("items", "[]"))
        for it in items:
            item_counts[it["name"]] = item_counts.get(it["name"], 0) + it["qty"]

    top_items = sorted(item_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    low_stock = [p for p in products if p["stock"] < 10]

    if not GEMINI_API_KEY:
        return {
            "top_selling": [{"name": n, "units_sold": q} for n, q in top_items],
            "low_stock_alert": [{"name": p["name"], "stock": p["stock"]} for p in low_stock],
            "ai_recommendation": "Enable GEMINI_API_KEY for AI-powered demand predictions.",
        }

    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        summary = f"Top selling: {top_items}. Low stock: {[(p['name'], p['stock']) for p in low_stock]}"
        prompt = (
            f"You are an inventory advisor for a small Indian online seller.\n"
            f"Data: {summary}\n"
            f"Give 3 concise bullet-point recommendations to optimise stock and revenue. Keep it under 80 words."
        )
        response = client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return {
            "top_selling": [{"name": n, "units_sold": q} for n, q in top_items],
            "low_stock_alert": [{"name": p["name"], "stock": p["stock"]} for p in low_stock],
            "ai_recommendation": response.text.strip(),
        }
    except Exception as e:
        return {
            "top_selling": [{"name": n, "units_sold": q} for n, q in top_items],
            "low_stock_alert": [{"name": p["name"], "stock": p["stock"]} for p in low_stock],
            "ai_recommendation": "AI temporarily unavailable.",
            "error": str(e),
        }


# ---------------------------------------------------------------- PDF invoice

@app.get("/api/orders/{order_id}/invoice")
def download_invoice(order_id: str, user=Depends(get_current_user)):
    conn = db()
    row = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Order not found")
    if user["role"] == "seller" and row["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")
    if user["role"] == "customer" and row["customer_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")

    seller_row = _fetchone(conn, "SELECT * FROM users WHERE id=?", (row["seller_id"],)) or {}
    customer_row = _fetchone(conn, "SELECT * FROM users WHERE id=?", (row["customer_id"],)) or {}
    conn.close()

    items = json.loads(row.get("items", "[]"))
    subtotal = sum(i["price"] * i["qty"] for i in items)
    discount = float(row.get("discount_amount") or 0)
    net = max(0.0, subtotal - discount)
    gst_rate = 18.0
    taxable = round(net / (1 + gst_rate / 100), 2)
    gst_amt = round(net - taxable, 2)
    cgst = round(gst_amt / 2, 2)
    sgst = round(gst_amt / 2, 2)
    inv_num = f"INV-{(row.get('created_at') or '')[:10].replace('-', '')}-{order_id}"

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.units import mm

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=20*mm, leftMargin=20*mm,
                                topMargin=15*mm, bottomMargin=15*mm)
        styles = getSampleStyleSheet()
        brand_color = colors.HexColor("#6c63ff")
        story = []

        # Header
        story.append(Paragraph("<b>OrderFlow AI</b>", ParagraphStyle("brand", fontSize=22,
                                textColor=brand_color, spaceAfter=2)))
        story.append(Paragraph("GST Tax Invoice", ParagraphStyle("sub", fontSize=13,
                                textColor=colors.grey, spaceAfter=10)))
        story.append(Spacer(1, 4*mm))

        # Invoice meta table
        meta = [
            ["Invoice No.", inv_num, "Order ID", order_id],
            ["Date", (row.get("created_at") or "")[:10], "Payment", row.get("payment_method", "UPI")],
            ["Seller", seller_row.get("name", row.get("seller_name", "")),
             "Customer", customer_row.get("name", row.get("customer_name", ""))],
            ["Seller ID", row.get("seller_id", ""), "Customer ID", row.get("customer_id", "")],
        ]
        meta_tbl = Table(meta, colWidths=[40*mm, 65*mm, 40*mm, 65*mm])
        meta_tbl.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TEXTCOLOR", (0, 0), (0, -1), colors.grey),
            ("TEXTCOLOR", (2, 0), (2, -1), colors.grey),
            ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
            ("FONTNAME", (3, 0), (3, -1), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(meta_tbl)
        story.append(Spacer(1, 6*mm))

        # Items table
        item_data = [["#", "Product", "Qty", "Unit Price (₹)", "Total (₹)"]]
        for idx, it in enumerate(items, 1):
            item_data.append([str(idx), it["name"], str(it["qty"]),
                              f"{it['price']:.2f}", f"{it['price'] * it['qty']:.2f}"])
        item_tbl = Table(item_data, colWidths=[10*mm, 80*mm, 15*mm, 40*mm, 40*mm])
        item_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), brand_color),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f3ff")]),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
            ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(item_tbl)
        story.append(Spacer(1, 4*mm))

        # Financials
        fin_data = [
            ["Subtotal", f"₹{subtotal:.2f}"],
            ["Discount", f"-₹{discount:.2f}"],
            ["Taxable Value", f"₹{taxable:.2f}"],
            ["CGST (9%)", f"₹{cgst:.2f}"],
            ["SGST (9%)", f"₹{sgst:.2f}"],
            ["Grand Total", f"₹{row.get('amount', net):.2f}"],
        ]
        fin_tbl = Table(fin_data, colWidths=[130*mm, 50*mm])
        fin_tbl.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, -1), (-1, -1), brand_color),
            ("FONTSIZE", (0, -1), (-1, -1), 11),
            ("LINEABOVE", (0, -1), (-1, -1), 1, brand_color),
        ]))
        story.append(fin_tbl)
        story.append(Spacer(1, 6*mm))
        story.append(Paragraph("Thank you for shopping with OrderFlow AI! 🙏",
                                ParagraphStyle("thanks", fontSize=9, textColor=colors.grey)))

        doc.build(story)
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=invoice_{order_id}.pdf"},
        )

    except ImportError:
        # Fallback: return invoice data as JSON if reportlab not installed
        return {
            "invoice_number": inv_num,
            "order_id": order_id,
            "items": items,
            "financials": {
                "subtotal": subtotal, "discount": discount,
                "taxable_value": taxable, "cgst": cgst, "sgst": sgst,
                "grand_total": row.get("amount", net),
            },
            "note": "Install reportlab for PDF: pip install reportlab"
        }


# ---------------------------------------------------------------- Razorpay payments

class RazorpayOrderBody(BaseModel):
    amount: float  # in INR
    currency: str = "INR"
    order_ref: Optional[str] = ""  # internal order ID


@app.post("/api/payments/create-order")
def create_razorpay_order(body: RazorpayOrderBody, user=Depends(get_current_user)):
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return {
            "razorpay_order_id": f"mock_{uuid.uuid4().hex[:12]}",
            "amount": int(body.amount * 100),
            "currency": body.currency,
            "key_id": "rzp_test_mock",
            "note": "Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET env vars for live payments",
        }
    try:
        import razorpay
        client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        rz_order = client.order.create({
            "amount": int(body.amount * 100),  # paise
            "currency": body.currency,
            "receipt": body.order_ref or str(uuid.uuid4())[:8],
        })
        return {
            "razorpay_order_id": rz_order["id"],
            "amount": rz_order["amount"],
            "currency": rz_order["currency"],
            "key_id": RAZORPAY_KEY_ID,
        }
    except Exception as e:
        raise HTTPException(500, f"Razorpay error: {e}")


class RazorpayVerifyBody(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    internal_order_id: Optional[str] = ""


@app.post("/api/payments/verify")
async def verify_razorpay_payment(body: RazorpayVerifyBody, user=Depends(get_current_user)):
    if RAZORPAY_KEY_SECRET:
        expected = hmac.new(
            RAZORPAY_KEY_SECRET.encode(),
            f"{body.razorpay_order_id}|{body.razorpay_payment_id}".encode(),
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, body.razorpay_signature):
            raise HTTPException(400, "Payment signature verification failed")

    # Update order payment status
    if body.internal_order_id:
        conn = db()
        now = datetime.utcnow().isoformat()
        _exec(conn,
            "UPDATE orders SET payment_status='Paid', razorpay_order_id=?, razorpay_payment_id=?, updated_at=? WHERE id=?",
            (body.razorpay_order_id, body.razorpay_payment_id, now, body.internal_order_id))
        conn.commit()
        updated = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (body.internal_order_id,))
        conn.close()
        if updated:
            updated["items"] = json.loads(updated.get("items", "[]"))
            await manager.send_user(updated["customer_id"], {"type": "payment_confirmed", "order": updated})
            await manager.send_user(updated["seller_id"], {"type": "payment_confirmed", "order": updated})

    return {"success": True, "payment_id": body.razorpay_payment_id, "status": "Paid"}


# ---------------------------------------------------------------- email helper (async-style using resend)

def _send_email_async(to: str, subject: str, body_text: str):
    """Fire-and-forget email sending. Silently fails if RESEND_API_KEY not set."""
    if not RESEND_API_KEY or not to or "@" not in to:
        return
    try:
        import resend
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [to],
            "subject": subject,
            "text": body_text,
        })
    except Exception as e:
        print(f"[Email] Failed to send to {to}: {e}")


# ---------------------------------------------------------------- products & inventory (unchanged + seller gating)

class NewProduct(BaseModel):
    name: str
    price: float
    stock: int
    category: str = "General"
    icon: str = "📦"
    description: str = ""


class StockUpdate(BaseModel):
    stock: Optional[int] = None
    delta: Optional[int] = None


@app.get("/api/products")
def list_products(seller_id: Optional[str] = None):
    conn = db()
    if seller_id and seller_id != "all":
        rows = _fetchall(conn, "SELECT * FROM products WHERE seller_id=? ORDER BY name ASC", (seller_id,))
    else:
        rows = _fetchall(conn, "SELECT * FROM products ORDER BY name ASC")
    conn.close()
    return rows


@app.post("/api/products")
async def create_product(body: NewProduct, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can add products")
    # Ensure seller is verified
    conn = db()
    seller_row = _fetchone(conn, "SELECT status FROM users WHERE id=?", (user["sub"],))
    if seller_row and seller_row.get("status") != "active":
        conn.close()
        raise HTTPException(403, "Your seller account must be approved before listing products")
    if body.price <= 0:
        conn.close()
        raise HTTPException(400, "Price must be greater than 0")
    if body.stock < 0:
        conn.close()
        raise HTTPException(400, "Stock cannot be negative")

    pid = str(uuid.uuid4())[:8].upper()
    seller_name = user.get("name") or "Verified Seller"
    _exec(conn,
        "INSERT INTO products (id, name, price, stock, category, seller_id, seller_name, icon, description) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, body.name.strip(), body.price, body.stock, body.category.strip(),
         user["sub"], seller_name, body.icon.strip() or "📦", body.description.strip()))
    conn.commit()
    product = _fetchone(conn, "SELECT * FROM products WHERE id=?", (pid,))
    conn.close()
    await manager.broadcast_all({"type": "product_added", "product": product})
    return product


@app.delete("/api/products/{product_id}")
async def delete_product(product_id: str, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can delete products")
    conn = db()
    row = _fetchone(conn, "SELECT * FROM products WHERE id=?", (product_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Product not found")
    if row["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "You can only delete your own products")
    _exec(conn, "DELETE FROM products WHERE id=?", (product_id,))
    conn.commit()
    conn.close()
    await manager.broadcast_all({"type": "product_deleted", "product_id": product_id})
    return {"success": True, "product_id": product_id}


@app.patch("/api/products/{product_id}/stock")
async def update_product_stock(product_id: str, body: StockUpdate, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can update stock")
    conn = db()
    row = _fetchone(conn, "SELECT * FROM products WHERE id=?", (product_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Product not found")
    if row["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "You can only update your own products")

    curr_stock = row["stock"]
    if body.stock is not None:
        new_stock = max(0, body.stock)
    elif body.delta is not None:
        new_stock = max(0, curr_stock + body.delta)
    else:
        conn.close()
        raise HTTPException(400, "Provide either 'stock' or 'delta'")

    _exec(conn, "UPDATE products SET stock=? WHERE id=?", (new_stock, product_id))
    conn.commit()
    updated = _fetchone(conn, "SELECT * FROM products WHERE id=?", (product_id,))
    conn.close()
    await manager.broadcast_all({"type": "inventory_update", "product": updated})
    return updated


# ---------------------------------------------------------------- orders (unchanged logic, admin can see all)

class OrderItem(BaseModel):
    product_id: str
    name: str
    price: float
    qty: int


class NewOrder(BaseModel):
    items: List[OrderItem]
    seller_id: Optional[str] = None
    payment_method: Optional[str] = "UPI"
    shipping_address: Optional[str] = "Standard Customer Address"
    coupon_code: Optional[str] = ""
    discount_amount: Optional[float] = 0.0


STATUS_FLOW = ["Placed", "Processing", "Delayed", "Completed"]


@app.post("/api/orders")
async def create_order(body: NewOrder, user=Depends(get_current_user)):
    if user["role"] != "customer":
        raise HTTPException(403, "Only customers can place orders")
    if not body.items:
        raise HTTPException(400, "Order must contain at least one item")

    conn = db()
    products_map = {}
    for item in body.items:
        row = _fetchone(conn, "SELECT * FROM products WHERE id=?", (item.product_id,))
        if not row:
            conn.close()
            raise HTTPException(404, f"Product '{item.name}' not found")
        if row["stock"] < item.qty:
            conn.close()
            raise HTTPException(400, f"Insufficient stock for '{item.name}'. Only {row['stock']} available.")
        products_map[item.product_id] = row

    seller_groups = {}
    for item in body.items:
        p = products_map[item.product_id]
        sid = p["seller_id"]
        sname = p.get("seller_name") or "Verified Seller"
        if sid not in seller_groups:
            seller_groups[sid] = {"seller_name": sname, "items": []}
        seller_groups[sid]["items"].append((item, p))

    now = datetime.utcnow().isoformat()
    created_orders = []
    updated_products = []

    raw_cart_total = sum(item.price * item.qty for item in body.items)
    total_discount = max(0.0, float(body.discount_amount or 0.0))
    p_method = (body.payment_method or "UPI").strip()
    p_status = "Pending COD" if p_method.upper() == "COD" else "Paid"
    s_addr = (body.shipping_address or "Standard Shipping Address").strip()
    c_code = (body.coupon_code or "").strip().upper()

    for sid, group in seller_groups.items():
        s_items = group["items"]
        s_name = group["seller_name"]
        s_raw_amount = sum(item.price * item.qty for item, _ in s_items)
        s_discount = round((s_raw_amount / raw_cart_total) * total_discount, 2) if total_discount > 0 and raw_cart_total > 0 else 0.0
        order_amount = max(0.0, round(s_raw_amount - s_discount, 2))
        order_id = str(uuid.uuid4())[:8].upper()
        eta = (datetime.utcnow() + timedelta(minutes=45)).strftime("%H:%M")

        for item, _ in s_items:
            _exec(conn, "UPDATE products SET stock = stock - ? WHERE id = ?", (item.qty, item.product_id))
            p_row = _fetchone(conn, "SELECT * FROM products WHERE id=?", (item.product_id,))
            if p_row:
                updated_products.append(p_row)

        serialized_items = [item.dict() for item, _ in s_items]
        order = {
            "id": order_id, "customer_id": user["sub"], "customer_name": user["name"],
            "seller_id": sid, "seller_name": s_name, "items": serialized_items,
            "amount": order_amount, "status": "Placed", "eta": eta, "delay_reason": "",
            "payment_method": p_method, "payment_status": p_status,
            "coupon_code": c_code if s_discount > 0 else "", "discount_amount": s_discount,
            "shipping_address": s_addr, "created_at": now, "updated_at": now,
        }

        _exec(conn,
            """INSERT INTO orders (id, customer_id, customer_name, seller_id, seller_name, items, amount,
               status, eta, delay_reason, payment_method, payment_status, coupon_code,
               discount_amount, shipping_address, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, user["sub"], user["name"], sid, s_name, json.dumps(serialized_items),
             order_amount, "Placed", eta, "", p_method, p_status,
             c_code if s_discount > 0 else "", s_discount, s_addr, now, now))
        created_orders.append(order)
        await manager.send_user(sid, {"type": "new_order", "order": order})

    conn.commit()
    conn.close()
    for p in updated_products:
        await manager.broadcast_all({"type": "inventory_update", "product": p})

    return {
        "orders": created_orders,
        "order": created_orders[0] if len(created_orders) == 1 else None,
        "message": f"Successfully placed {len(created_orders)} order(s) with {len(seller_groups)} seller(s).",
    }


@app.get("/api/orders")
def list_orders(
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = 0,
    user=Depends(get_current_user)
):
    conn = db()
    query = "SELECT * FROM orders WHERE "
    params = []

    if user["role"] == "seller":
        query += "seller_id=? "
        params.append(user["sub"])
    elif user["role"] == "admin":
        query += "1=1 "
    else:
        query += "customer_id=? "
        params.append(user["sub"])

    if status and status != "All":
        query += "AND status=? "
        params.append(status)
    if search:
        query += "AND (id LIKE ? OR customer_name LIKE ? OR seller_name LIKE ? OR items LIKE ?) "
        term = f"%{search}%"
        params.extend([term, term, term, term])

    query += "ORDER BY created_at DESC "
    if limit is not None:
        query += "LIMIT ? OFFSET ? "
        params.extend([limit, offset or 0])

    rows = _fetchall(conn, query, tuple(params))
    conn.close()
    for r in rows:
        try:
            r["items"] = json.loads(r["items"])
        except Exception:
            r["items"] = []
    return rows


class StatusUpdate(BaseModel):
    status: str


@app.patch("/api/orders/{order_id}/status")
async def update_status(order_id: str, body: StatusUpdate, user=Depends(get_current_user)):
    conn = db()
    row = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Order not found")

    is_seller = user["role"] == "seller" and row["seller_id"] == user["sub"]
    is_customer = user["role"] == "customer" and row["customer_id"] == user["sub"]
    is_admin = user["role"] == "admin"

    if not is_seller and not is_admin and not (is_customer and body.status == "Cancelled"):
        conn.close()
        raise HTTPException(403, "Unauthorized to update status for this order")

    if body.status not in STATUS_FLOW + ["Cancelled"]:
        conn.close()
        raise HTTPException(400, "Invalid status")
    if row["status"] == "Cancelled":
        conn.close()
        raise HTTPException(400, "Order is already cancelled")

    now = datetime.utcnow().isoformat()
    _exec(conn, "UPDATE orders SET status=?, updated_at=? WHERE id=?", (body.status, now, order_id))

    restored_products = []
    if body.status == "Cancelled":
        items = json.loads(row["items"])
        for itm in items:
            _exec(conn, "UPDATE products SET stock = stock + ? WHERE id = ?", (itm["qty"], itm["product_id"]))
            p_row = _fetchone(conn, "SELECT * FROM products WHERE id=?", (itm["product_id"],))
            if p_row:
                restored_products.append(p_row)

    conn.commit()
    updated = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    conn.close()
    updated["items"] = json.loads(updated["items"])

    await manager.send_user(updated["customer_id"], {"type": "status_update", "order": updated})
    await manager.send_user(updated["seller_id"], {"type": "status_update", "order": updated})
    for p in restored_products:
        await manager.broadcast_all({"type": "inventory_update", "product": p})
    return updated


class DelayOrderBody(BaseModel):
    delay_minutes: int = 30
    reason: str = "Logistics delay"


@app.post("/api/orders/{order_id}/delay")
async def delay_order(order_id: str, body: DelayOrderBody, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can delay orders")
    conn = db()
    row = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Order not found")
    if row["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "You can only delay your own store's orders")
    if row["status"] in ("Completed", "Cancelled"):
        conn.close()
        raise HTTPException(400, f"Cannot delay a {row['status']} order")

    try:
        curr_eta_str = row.get("eta", "")
        if ":" in curr_eta_str:
            parts = curr_eta_str.split(":")
            now_dt = datetime.utcnow()
            eta_dt = now_dt.replace(hour=int(parts[0]), minute=int(parts[1]))
            if eta_dt < now_dt:
                eta_dt += timedelta(days=1)
            new_eta = (eta_dt + timedelta(minutes=body.delay_minutes)).strftime("%H:%M")
        else:
            new_eta = (datetime.utcnow() + timedelta(minutes=body.delay_minutes)).strftime("%H:%M")
    except Exception:
        new_eta = (datetime.utcnow() + timedelta(minutes=body.delay_minutes)).strftime("%H:%M")

    now = datetime.utcnow().isoformat()
    _exec(conn, "UPDATE orders SET status='Delayed', eta=?, delay_reason=?, updated_at=? WHERE id=?",
          (new_eta, body.reason.strip() or "Logistics delay", now, order_id))
    conn.commit()
    updated = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    conn.close()
    updated["items"] = json.loads(updated["items"])
    await manager.send_user(updated["customer_id"], {"type": "status_update", "order": updated})
    await manager.send_user(updated["seller_id"], {"type": "status_update", "order": updated})
    return updated


@app.post("/api/orders/{order_id}/accept-payment")
async def accept_payment(order_id: str, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can verify payment")
    conn = db()
    row = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    if not row or row["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")
    now = datetime.utcnow().isoformat()
    _exec(conn, "UPDATE orders SET payment_status='Paid (Verified by Seller)', updated_at=? WHERE id=?", (now, order_id))
    conn.commit()
    updated = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    conn.close()
    updated["items"] = json.loads(updated["items"])
    await manager.send_user(updated["customer_id"], {"type": "payment_update", "order": updated})
    await manager.send_user(updated["seller_id"], {"type": "payment_update", "order": updated})
    return updated


# ---------------------------------------------------------------- invoice data (JSON for frontend rendering)

@app.get("/api/orders/{order_id}/invoice-data")
def get_invoice_data(order_id: str, user=Depends(get_current_user)):
    conn = db()
    row = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Order not found")
    if user["role"] == "seller" and row["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")
    if user["role"] == "customer" and row["customer_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")

    seller_row = _fetchone(conn, "SELECT id, name, email FROM users WHERE id=?", (row["seller_id"],))
    customer_row = _fetchone(conn, "SELECT id, name, email FROM users WHERE id=?", (row["customer_id"],))
    conn.close()

    items = json.loads(row["items"])
    subtotal = sum(i["price"] * i["qty"] for i in items)
    discount = float(row.get("discount_amount") or 0)
    net = max(0.0, subtotal - discount)
    gst_rate = 18.0
    taxable = round(net / (1 + gst_rate / 100), 2)
    gst_amt = round(net - taxable, 2)

    seller = seller_row or {"id": row["seller_id"], "name": row.get("seller_name", ""), "email": ""}
    customer = customer_row or {"id": row["customer_id"], "name": row.get("customer_name", ""), "email": ""}

    return {
        "invoice_number": f"INV-{(row.get('created_at') or '')[:10].replace('-', '')}-{order_id}",
        "order": {**row, "items": items},
        "seller": seller, "customer": customer,
        "financials": {
            "subtotal": subtotal, "discount": discount, "taxable_value": taxable,
            "cgst": round(gst_amt / 2, 2), "sgst": round(gst_amt / 2, 2),
            "gst_total": gst_amt, "grand_total": row.get("amount", net),
        },
    }


# ---------------------------------------------------------------- order chat (customer <-> seller)

class SendMessageBody(BaseModel):
    message: str


@app.get("/api/orders/{order_id}/messages")
def get_order_messages(order_id: str, user=Depends(get_current_user)):
    conn = db()
    order = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    if not order:
        conn.close()
        raise HTTPException(404, "Order not found")
    if user["role"] == "seller" and order["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")
    if user["role"] == "customer" and order["customer_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")
    rows = _fetchall(conn, "SELECT * FROM order_messages WHERE order_id=? ORDER BY timestamp ASC", (order_id,))
    conn.close()
    return rows


@app.post("/api/orders/{order_id}/messages")
async def send_order_message(order_id: str, body: SendMessageBody, user=Depends(get_current_user)):
    clean_text = body.message.strip()
    if not clean_text:
        raise HTTPException(400, "Message cannot be empty")
    conn = db()
    order = _fetchone(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
    if not order:
        conn.close()
        raise HTTPException(404, "Order not found")
    if user["role"] == "seller" and order["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")
    if user["role"] == "customer" and order["customer_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")

    mid = str(uuid.uuid4())[:8].upper()
    now = datetime.utcnow().isoformat()
    msg_obj = {
        "id": mid, "order_id": order_id, "sender_id": user["sub"],
        "sender_name": user["name"], "sender_role": user["role"],
        "message": clean_text, "timestamp": now,
    }
    _exec(conn,
        "INSERT INTO order_messages (id, order_id, sender_id, sender_name, sender_role, message, timestamp) VALUES (?,?,?,?,?,?,?)",
        (mid, order_id, user["sub"], user["name"], user["role"], clean_text, now))
    conn.commit()
    conn.close()

    recipient_id = order["seller_id"] if user["role"] == "customer" else order["customer_id"]
    await manager.send_user(recipient_id, {"type": "chat_message", "message": msg_obj})
    await manager.send_user(user["sub"], {"type": "chat_message", "message": msg_obj})
    return msg_obj


# ---------------------------------------------------------------- coupons (unchanged)

class CreateCouponBody(BaseModel):
    code: str
    discount_type: str = "percent"
    discount_value: float
    min_order: float = 0.0


class ValidateCouponBody(BaseModel):
    code: str
    order_amount: float


@app.get("/api/coupons")
def list_coupons(user=Depends(get_current_user)):
    conn = db()
    if user["role"] == "seller":
        rows = _fetchall(conn,
            "SELECT * FROM coupons WHERE seller_id=? OR seller_id='all' ORDER BY created_at DESC", (user["sub"],))
    else:
        rows = _fetchall(conn, "SELECT * FROM coupons WHERE is_active=1 ORDER BY created_at DESC")
    conn.close()
    return rows


@app.post("/api/coupons")
def create_coupon(body: CreateCouponBody, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can create coupons")
    clean_code = body.code.strip().upper()
    if len(clean_code) < 3:
        raise HTTPException(400, "Coupon code must be at least 3 characters")
    if body.discount_value <= 0:
        raise HTTPException(400, "Discount value must be > 0")
    if body.discount_type == "percent" and body.discount_value > 90:
        raise HTTPException(400, "Percentage discount cannot exceed 90%")
    if body.discount_type not in ("percent", "flat"):
        raise HTTPException(400, "discount_type must be 'percent' or 'flat'")

    conn = db()
    if _fetchone(conn, "SELECT 1 FROM coupons WHERE code=?", (clean_code,)):
        conn.close()
        raise HTTPException(400, f"Coupon '{clean_code}' already exists")

    cid = str(uuid.uuid4())[:8].upper()
    now = datetime.utcnow().isoformat()
    _exec(conn,
        "INSERT INTO coupons (id, code, discount_type, discount_value, min_order, seller_id, is_active, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (cid, clean_code, body.discount_type, body.discount_value, max(0.0, body.min_order), user["sub"], 1, now))
    conn.commit()
    row = _fetchone(conn, "SELECT * FROM coupons WHERE id=?", (cid,))
    conn.close()
    return row


@app.delete("/api/coupons/{coupon_id}")
def delete_coupon(coupon_id: str, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can delete coupons")
    conn = db()
    row = _fetchone(conn, "SELECT * FROM coupons WHERE id=?", (coupon_id,))
    if not row:
        conn.close()
        raise HTTPException(404, "Coupon not found")
    if row["seller_id"] != user["sub"] and row["seller_id"] != "all":
        conn.close()
        raise HTTPException(403, "You can only delete your own coupons")
    _exec(conn, "DELETE FROM coupons WHERE id=?", (coupon_id,))
    conn.commit()
    conn.close()
    return {"success": True, "coupon_id": coupon_id}


@app.post("/api/coupons/validate")
def validate_coupon(body: ValidateCouponBody):
    clean_code = body.code.strip().upper()
    conn = db()
    row = _fetchone(conn, "SELECT * FROM coupons WHERE code=? AND is_active=1", (clean_code,))
    conn.close()
    if not row:
        raise HTTPException(404, f"Invalid or expired promo code '{clean_code}'")
    if body.order_amount < row["min_order"]:
        raise HTTPException(400, f"Code '{clean_code}' requires a minimum order of ₹{row['min_order']:.2f}")

    if row["discount_type"] == "percent":
        discount = round((body.order_amount * row["discount_value"]) / 100.0, 2)
    else:
        discount = min(row["discount_value"], body.order_amount)

    return {
        "valid": True, "code": clean_code,
        "discount_type": row["discount_type"], "discount_value": row["discount_value"],
        "discount_amount": discount, "final_amount": max(0.0, round(body.order_amount - discount, 2)),
        "message": f"Coupon '{clean_code}' applied! You saved ₹{discount:.2f}",
    }


# ---------------------------------------------------------------- analytics (unchanged)

@app.get("/api/analytics/seller")
def get_seller_analytics(user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can view analytics")
    conn = db()
    seller_id = user["sub"]
    orders = _fetchall(conn, "SELECT * FROM orders WHERE seller_id=?", (seller_id,))
    conn.close()

    for o in orders:
        try:
            o["items"] = json.loads(o["items"])
        except Exception:
            o["items"] = []

    now = datetime.utcnow()
    current_year_month = now.strftime("%Y-%m")
    current_month_name = now.strftime("%B %Y")

    this_month = [o for o in orders if (o.get("created_at") or "").startswith(current_year_month)]
    this_month_completed = [o for o in this_month if o["status"] == "Completed"]
    this_month_non_cancelled = [o for o in this_month if o["status"] != "Cancelled"]
    this_month_revenue = sum(o["amount"] for o in this_month_non_cancelled)

    total_revenue = sum(o["amount"] for o in orders if o["status"] != "Cancelled")
    completed_revenue = sum(o["amount"] for o in orders if o["status"] == "Completed")
    pending_orders = len([o for o in orders if o["status"] in ("Placed", "Processing", "Delayed")])
    completed_orders = len([o for o in orders if o["status"] == "Completed"])
    cancelled_orders = len([o for o in orders if o["status"] == "Cancelled"])
    delayed_orders = len([o for o in orders if o["status"] == "Delayed"])

    days_in_month = {}
    for o in this_month_non_cancelled:
        day_str = (o.get("created_at") or "")[:10]
        days_in_month[day_str] = days_in_month.get(day_str, 0) + o["amount"]

    avg_order_value = (this_month_revenue / len(this_month_non_cancelled)) if this_month_non_cancelled else 0
    fulfillment_rate = (len(this_month_completed) / len(this_month) * 100) if this_month else 100

    return {
        "seller_id": seller_id,
        "current_month_name": current_month_name,
        "this_month_revenue": this_month_revenue,
        "this_month_orders": len(this_month),
        "this_month_completed": len(this_month_completed),
        "this_month_aov": round(avg_order_value, 2),
        "fulfillment_rate": round(fulfillment_rate, 1),
        "total_revenue": total_revenue,
        "completed_revenue": completed_revenue,
        "pending_count": pending_orders,
        "completed_count": completed_orders,
        "cancelled_count": cancelled_orders,
        "delayed_count": delayed_orders,
        "total_orders_count": len(orders),
        "daily_breakdown": [{"date": k, "amount": v} for k, v in sorted(days_in_month.items())],
    }


@app.get("/api/analytics/customers")
def get_seller_customers(user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can view customer analytics")
    conn = db()
    orders = _fetchall(conn, "SELECT * FROM orders WHERE seller_id=?", (user["sub"],))
    conn.close()

    cust_map = {}
    for d in orders:
        cid = d["customer_id"]
        if cid not in cust_map:
            cust_map[cid] = {"id": cid, "name": d["customer_name"],
                              "orders_count": 0, "total_spent": 0, "last_order": d["created_at"]}
        cust_map[cid]["orders_count"] += 1
        if d["status"] != "Cancelled":
            cust_map[cid]["total_spent"] += d["amount"]

    customers = sorted(cust_map.values(), key=lambda c: c["total_spent"], reverse=True)
    return {"total_unique_customers": len(customers), "customers": customers}


# ---------------------------------------------------------------- seller profile (branding)

class SellerBrandingBody(BaseModel):
    brand_color: Optional[str] = None
    store_logo: Optional[str] = None
    store_banner: Optional[str] = None
    brand_name: Optional[str] = None
    brand_description: Optional[str] = None


@app.patch("/api/sellers/branding")
def update_seller_branding(body: SellerBrandingBody, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can update their branding")
    conn = db()
    updates = []
    params = []
    if body.brand_color:
        updates.append("brand_color=?")
        params.append(body.brand_color.strip())
    if body.store_logo:
        updates.append("store_logo=?")
        params.append(body.store_logo.strip())
    if body.store_banner:
        updates.append("store_banner=?")
        params.append(body.store_banner.strip())
    if body.brand_name:
        updates.append("brand_name=?")
        params.append(body.brand_name.strip())
    if body.brand_description:
        updates.append("brand_description=?")
        params.append(body.brand_description.strip())

    if updates:
        params.append(user["sub"])
        _exec(conn, f"UPDATE users SET {', '.join(updates)} WHERE id=?", tuple(params))
        conn.commit()
    row = _fetchone(conn, "SELECT * FROM users WHERE id=?", (user["sub"],))
    conn.close()
    return _safe_user(row)


@app.get("/api/sellers/{seller_id}/profile")
def get_seller_profile(seller_id: str):
    conn = db()
    row = _fetchone(conn, "SELECT * FROM users WHERE id=? AND role='seller'", (seller_id,))
    conn.close()
    if not row:
        raise HTTPException(404, "Seller not found")
    return _safe_user(row)


# ---------------------------------------------------------------- platform config (admin)

@app.get("/api/admin/platform-config")
def get_platform_config(admin=Depends(require_admin)):
    return {
        "gemini_enabled": bool(GEMINI_API_KEY),
        "razorpay_enabled": bool(RAZORPAY_KEY_ID),
        "email_enabled": bool(RESEND_API_KEY),
        "database": "postgresql" if DATABASE_URL else "sqlite",
        "allowed_origins": ALLOWED_ORIGINS,
    }
