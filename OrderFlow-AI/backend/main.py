"""
OrderFlow AI — backend
FastAPI + SQLite + Google Sign-In + WebSocket live order sync.

Run:
    pip install -r requirements.txt --break-system-packages
    export GOOGLE_CLIENT_ID="your-google-oauth-client-id.apps.googleusercontent.com"
    export JWT_SECRET="something-long-and-random"
    uvicorn main:app --reload --port 8000
"""

import os
import json
import time
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Header
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import jwt

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "REPLACE_WITH_YOUR_GOOGLE_CLIENT_ID")
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
JWT_ALGO = "HS256"
DB_PATH = os.path.join(os.path.dirname(__file__), "orderflow.db")
FRONTEND_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html"))

app = FastAPI(title="OrderFlow AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def serve_index():
    if os.path.exists(FRONTEND_FILE):
        return FileResponse(FRONTEND_FILE)
    return {"message": "OrderFlow AI API is running. Visit /docs for Swagger documentation."}

# ---------------------------------------------------------------- database

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_pw(pw: str) -> str:
    import hashlib
    return hashlib.sha256(pw.strip().encode()).hexdigest()


def generate_7digit_id(role: str, conn) -> str:
    import random
    prefix = 1 if role == "seller" else 7
    for _ in range(100):
        candidate = f"{prefix}{random.randint(100000, 999999)}"
        exists = conn.execute("SELECT 1 FROM users WHERE id=?", (candidate,)).fetchone()
        if not exists:
            return candidate
    return str(uuid.uuid4())[:7]


def init_db():
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            google_sub TEXT,
            email TEXT,
            name TEXT,
            password_hash TEXT DEFAULT '',
            picture TEXT,
            role TEXT,
            created_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT,
            price REAL,
            stock INTEGER,
            category TEXT,
            seller_id TEXT,
            seller_name TEXT DEFAULT '',
            icon TEXT DEFAULT '📦'
        );

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
            coupon_code TEXT DEFAULT '',
            discount_amount REAL DEFAULT 0.0,
            shipping_address TEXT DEFAULT '',
            created_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS coupons (
            id TEXT PRIMARY KEY,
            code TEXT UNIQUE,
            discount_type TEXT,
            discount_value REAL,
            min_order REAL,
            seller_id TEXT DEFAULT 'all',
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS order_messages (
            id TEXT PRIMARY KEY,
            order_id TEXT,
            sender_id TEXT,
            sender_name TEXT,
            sender_role TEXT,
            message TEXT,
            timestamp TEXT
        );
        """
    )

    # Dynamic migrations
    ucols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "password_hash" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT ''")
    if "created_at" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT DEFAULT ''")

    pcols = [r["name"] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
    if "icon" not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN icon TEXT DEFAULT '📦'")
    if "seller_name" not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN seller_name TEXT DEFAULT ''")

    ocols = [r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "delay_reason" not in ocols:
        conn.execute("ALTER TABLE orders ADD COLUMN delay_reason TEXT DEFAULT ''")
    if "seller_name" not in ocols:
        conn.execute("ALTER TABLE orders ADD COLUMN seller_name TEXT DEFAULT ''")
    if "payment_method" not in ocols:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_method TEXT DEFAULT 'UPI'")
    if "payment_status" not in ocols:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_status TEXT DEFAULT 'Paid'")
    if "coupon_code" not in ocols:
        conn.execute("ALTER TABLE orders ADD COLUMN coupon_code TEXT DEFAULT ''")
    if "discount_amount" not in ocols:
        conn.execute("ALTER TABLE orders ADD COLUMN discount_amount REAL DEFAULT 0.0")
    if "shipping_address" not in ocols:
        conn.execute("ALTER TABLE orders ADD COLUMN shipping_address TEXT DEFAULT ''")

    # Seed default 7-digit Sellers & Customers
    default_users = [
        ("1001001", "TechNova Electronics", "technova@seller.orderflow.ai", "seller123", "seller", "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=100&h=100&fit=crop"),
        ("1002002", "Aura Home Living", "aurahome@seller.orderflow.ai", "seller123", "seller", "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=100&h=100&fit=crop"),
        ("1003003", "Titanium Fitness Gear", "fitgear@seller.orderflow.ai", "seller123", "seller", "https://images.unsplash.com/photo-1517841905240-472988babdf9?w=100&h=100&fit=crop"),
        ("7001001", "Alex Rivera", "alex@customer.orderflow.ai", "customer123", "customer", "https://images.unsplash.com/photo-1535713875002-d1d0cf377fde?w=100&h=100&fit=crop"),
        ("7002002", "Priya Sharma", "priya@customer.orderflow.ai", "customer123", "customer", "https://images.unsplash.com/photo-1494790108377-be9c29b29330?w=100&h=100&fit=crop"),
    ]
    for uid, uname, uemail, upw, urole, upic in default_users:
        conn.execute(
            """INSERT INTO users (id, name, email, password_hash, role, picture, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, password_hash=excluded.password_hash, role=excluded.role, picture=excluded.picture""",
            (uid, uname, uemail, hash_pw(upw), urole, upic, datetime.utcnow().isoformat()),
        )

    # Seed products partitioned across the 3 default sellers
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
            existing = conn.execute("SELECT id FROM products WHERE name=? AND seller_id=?", (pname, sid)).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO products (id, name, price, stock, category, seller_id, seller_name, icon) VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4())[:8].upper(), pname, price, stock, cat, sid, sname, icon),
                )
            else:
                conn.execute(
                    "UPDATE products SET seller_name=?, icon=? WHERE id=?",
                    (sname, icon, existing["id"]),
                )

    # Seed default coupons
    default_coupons = [
        ("COUP-W50", "WELCOME50", "flat", 50.0, 200.0, "all"),
        ("COUP-F15", "FESTIVE15", "percent", 15.0, 500.0, "all"),
        ("COUP-FS", "FREESHIP", "flat", 40.0, 300.0, "all"),
        ("COUP-TECH", "TECH10", "percent", 10.0, 1000.0, "1001001"),
    ]
    for cid, code, dtype, dval, min_ord, sid in default_coupons:
        c_exist = conn.execute("SELECT id FROM coupons WHERE code=?", (code,)).fetchone()
        if not c_exist:
            conn.execute(
                "INSERT INTO coupons (id, code, discount_type, discount_value, min_order, seller_id, is_active, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (cid, code, dtype, dval, min_ord, sid, 1, datetime.utcnow().isoformat())
            )

    conn.commit()
    conn.close()


init_db()

# ---------------------------------------------------------------- auth

def make_jwt(user: dict) -> str:
    payload = {
        "sub": user["id"],
        "role": user["role"],
        "name": user["name"],
        "email": user.get("email", ""),
        "exp": datetime.utcnow() + timedelta(days=7),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    return payload


class LoginBody(BaseModel):
    id: str
    password: str
    role: str


@app.post("/api/auth/login")
def login(body: LoginBody):
    clean_id = body.id.strip()
    if not clean_id.isdigit() or len(clean_id) != 7:
        raise HTTPException(400, "ID must be exactly 7 numeric digits (e.g. 1001001 for seller, 7001001 for customer)")
    if body.role not in ("customer", "seller"):
        raise HTTPException(400, "Role must be customer or seller")

    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id=? AND role=?", (clean_id, body.role)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(401, f"No {body.role} account found with 7-digit ID '{clean_id}'")

    user = dict(row)
    pw_hash = hash_pw(body.password)
    if user["password_hash"] and user["password_hash"] != pw_hash:
        conn.close()
        raise HTTPException(401, "Invalid password. Please check your credentials.")

    conn.close()
    token = make_jwt(user)
    return {"access_token": token, "user": user}


class RegisterBody(BaseModel):
    name: str
    password: str
    role: str
    email: Optional[str] = None


@app.post("/api/auth/register")
def register(body: RegisterBody):
    if body.role not in ("customer", "seller"):
        raise HTTPException(400, "Role must be customer or seller")
    if len(body.name.strip()) < 2:
        raise HTTPException(400, "Name must be at least 2 characters")
    if len(body.password.strip()) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")

    conn = db()
    new_id = generate_7digit_id(body.role, conn)
    email = body.email.strip() if (body.email and body.email.strip()) else f"{new_id}@{body.role}.orderflow.ai"
    pw_hash = hash_pw(body.password)
    now = datetime.utcnow().isoformat()
    pic = f"https://api.dicebear.com/7.x/identicon/svg?seed={body.name.strip()}"

    conn.execute(
        "INSERT INTO users (id, name, email, password_hash, role, picture, created_at) VALUES (?,?,?,?,?,?,?)",
        (new_id, body.name.strip(), email, pw_hash, body.role, pic, now),
    )
    conn.commit()
    user = {"id": new_id, "name": body.name.strip(), "email": email, "role": body.role, "picture": pic}
    conn.close()

    token = make_jwt(user)
    return {
        "access_token": token,
        "user": user,
        "generated_id": new_id,
        "message": f"Account created! Your 7-digit {body.role.capitalize()} ID is: {new_id}"
    }


@app.get("/api/auth/quick-profiles")
def quick_profiles():
    conn = db()
    rows = conn.execute("SELECT id, name, email, role, picture FROM users ORDER BY id ASC").fetchall()
    conn.close()
    sellers = [dict(r) for r in rows if r["role"] == "seller"]
    customers = [dict(r) for r in rows if r["role"] == "customer"]
    return {"sellers": sellers, "customers": customers}


class DemoLoginBody(BaseModel):
    role: str
    id: Optional[str] = None
    name: Optional[str] = None


@app.post("/api/auth/demo")
def demo_login(body: DemoLoginBody):
    if body.role not in ("customer", "seller"):
        raise HTTPException(400, "role must be customer or seller")

    conn = db()
    target_id = body.id.strip() if (body.id and body.id.strip()) else ("1001001" if body.role == "seller" else "7001001")
    row = conn.execute("SELECT * FROM users WHERE id=? AND role=?", (target_id, body.role)).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM users WHERE role=? LIMIT 1", (body.role,)).fetchone()

    user = dict(row)
    conn.close()
    token = make_jwt(user)
    return {"access_token": token, "user": user}


# ---------------------------------------------------------------- websocket live sync

class ConnectionManager:
    def __init__(self):
        self.by_role: dict[str, list[WebSocket]] = {"customer": [], "seller": []}
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
            self.by_role[role].remove(ws)

    async def broadcast_all(self, message: dict):
        await self.broadcast_role("customer", message)
        await self.broadcast_role("seller", message)

    async def send_user(self, user_id: str, message: dict):
        dead = []
        for ws in self.by_user.get(user_id, []):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.by_user[user_id].remove(ws)


manager = ConnectionManager()


@app.websocket("/ws/{role}/{user_id}")
async def ws_endpoint(websocket: WebSocket, role: str, user_id: str):
    await manager.connect(websocket, role, user_id)
    try:
        while True:
            await websocket.receive_text()  # keepalive pings from client
    except WebSocketDisconnect:
        manager.disconnect(websocket, role, user_id)


# ---------------------------------------------------------------- products & inventory

class NewProduct(BaseModel):
    name: str
    price: float
    stock: int
    category: str = "General"
    icon: str = "📦"


class StockUpdate(BaseModel):
    stock: Optional[int] = None
    delta: Optional[int] = None


@app.get("/api/products")
def list_products(seller_id: Optional[str] = None):
    conn = db()
    if seller_id and seller_id != "all":
        rows = conn.execute("SELECT * FROM products WHERE seller_id=? ORDER BY name ASC", (seller_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM products ORDER BY name ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/products")
async def create_product(body: NewProduct, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can add products")
    if body.price <= 0:
        raise HTTPException(400, "Price must be greater than 0")
    if body.stock < 0:
        raise HTTPException(400, "Stock cannot be negative")

    pid = str(uuid.uuid4())[:8].upper()
    seller_id = user["sub"]
    seller_name = user.get("name") or "Verified Seller"

    conn = db()
    conn.execute(
        "INSERT INTO products (id, name, price, stock, category, seller_id, seller_name, icon) VALUES (?,?,?,?,?,?,?,?)",
        (pid, body.name.strip(), body.price, body.stock, body.category.strip(), seller_id, seller_name, body.icon.strip() or "📦"),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    product = dict(row)

    # Real-time broadcast: update product catalog for everyone
    await manager.broadcast_all({"type": "product_added", "product": product})
    return product


@app.delete("/api/products/{product_id}")
async def delete_product(product_id: str, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can delete products")
    conn = db()
    row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Product not found")

    # Ensure seller only deletes their own product
    if row["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "You can only delete products belonging to your store")

    conn.execute("DELETE FROM products WHERE id=?", (product_id,))
    conn.commit()
    conn.close()

    await manager.broadcast_all({"type": "product_deleted", "product_id": product_id})
    return {"success": True, "product_id": product_id}


@app.patch("/api/products/{product_id}/stock")
async def update_product_stock(product_id: str, body: StockUpdate, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can update stock")
    conn = db()
    row = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Product not found")

    if row["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "You can only modify stock for your own store's products")

    curr_stock = row["stock"]
    if body.stock is not None:
        new_stock = max(0, body.stock)
    elif body.delta is not None:
        new_stock = max(0, curr_stock + body.delta)
    else:
        conn.close()
        raise HTTPException(400, "Provide either 'stock' or 'delta'")

    conn.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, product_id))
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone())
    conn.close()

    await manager.broadcast_all({"type": "inventory_update", "product": updated})
    return updated


# ---------------------------------------------------------------- orders

class OrderItem(BaseModel):
    product_id: str
    name: str
    price: float
    qty: int


class NewOrder(BaseModel):
    items: list[OrderItem]
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
    # 1. Fetch all products and validate available stock
    products_map = {}
    for item in body.items:
        row = conn.execute("SELECT * FROM products WHERE id=?", (item.product_id,)).fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"Product '{item.name}' not found")
        p = dict(row)
        if p["stock"] < item.qty:
            available = p["stock"]
            conn.close()
            raise HTTPException(
                400,
                f"Insufficient stock for '{item.name}'. Only {available} available in stock."
            )
        products_map[item.product_id] = p

    # 2. Group items by seller_id (Flipkart-style Multi-Seller Order Splitting)
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

    # Calculate overall cart total to prorate coupon discount across sellers
    raw_cart_total = sum(item.price * item.qty for item in body.items)
    total_discount = max(0.0, float(body.discount_amount or 0.0))
    p_method = (body.payment_method or "UPI").strip()
    p_status = "Pending COD" if p_method.upper() == "COD" else "Paid"
    s_addr = (body.shipping_address or "Standard Shipping Address").strip()
    c_code = (body.coupon_code or "").strip().upper()

    # 3. Create a separate order per seller so each seller only receives & fulfills their own items!
    for sid, group in seller_groups.items():
        s_items = group["items"]
        s_name = group["seller_name"]
        s_raw_amount = sum(item.price * item.qty for item, _ in s_items)

        # Prorate discount for this seller
        if total_discount > 0 and raw_cart_total > 0:
            s_discount = round((s_raw_amount / raw_cart_total) * total_discount, 2)
        else:
            s_discount = 0.0

        order_amount = max(0.0, round(s_raw_amount - s_discount, 2))
        order_id = str(uuid.uuid4())[:8].upper()
        eta = (datetime.utcnow() + timedelta(minutes=45)).strftime("%H:%M")

        # Atomic stock decrement for items in this store
        for item, _ in s_items:
            conn.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (item.qty, item.product_id))
            p_row = conn.execute("SELECT * FROM products WHERE id=?", (item.product_id,)).fetchone()
            if p_row:
                updated_products.append(dict(p_row))

        serialized_items = [item.dict() for item, _ in s_items]
        order = {
            "id": order_id,
            "customer_id": user["sub"],
            "customer_name": user["name"],
            "seller_id": sid,
            "seller_name": s_name,
            "items": serialized_items,
            "amount": order_amount,
            "status": "Placed",
            "eta": eta,
            "delay_reason": "",
            "payment_method": p_method,
            "payment_status": p_status,
            "coupon_code": c_code if s_discount > 0 else "",
            "discount_amount": s_discount,
            "shipping_address": s_addr,
            "created_at": now,
            "updated_at": now,
        }

        conn.execute(
            """INSERT INTO orders (
                id, customer_id, customer_name, seller_id, seller_name, items, amount, status, eta, delay_reason,
                payment_method, payment_status, coupon_code, discount_amount, shipping_address, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order_id, user["sub"], user["name"], sid, s_name, json.dumps(serialized_items), order_amount, "Placed", eta, "",
                p_method, p_status, c_code if s_discount > 0 else "", s_discount, s_addr, now, now
            ),
        )
        created_orders.append(order)

        # Real-time push: SENT ONLY TO THIS PARTICULAR SELLER, not all sellers!
        await manager.send_user(sid, {"type": "new_order", "order": order})

    conn.commit()
    conn.close()

    # Broadcast updated stock to all connected customers & sellers
    for p in updated_products:
        await manager.broadcast_all({"type": "inventory_update", "product": p})

    return {
        "orders": created_orders,
        "order": created_orders[0] if len(created_orders) == 1 else None,
        "message": f"Successfully placed {len(created_orders)} order(s) with {len(seller_groups)} seller(s)."
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
        # Seller strictly sees ONLY their own store's orders!
        query += "seller_id=? "
        params.append(user["sub"])
    else:
        # Customer strictly sees their own placed orders
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

    rows = conn.execute(query, params).fetchall()
    conn.close()

    out = []
    for r in rows:
        d = dict(r)
        d["items"] = json.loads(d["items"])
        out.append(d)
    return out


class StatusUpdate(BaseModel):
    status: str


@app.patch("/api/orders/{order_id}/status")
async def update_status(order_id: str, body: StatusUpdate, user=Depends(get_current_user)):
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Order not found")

    order_dict = dict(row)
    is_seller = user["role"] == "seller" and order_dict["seller_id"] == user["sub"]
    is_customer = user["role"] == "customer" and order_dict["customer_id"] == user["sub"]

    if not is_seller and not (is_customer and body.status == "Cancelled"):
        conn.close()
        raise HTTPException(403, "Unauthorized to update status for this order")

    if body.status not in STATUS_FLOW + ["Cancelled"]:
        conn.close()
        raise HTTPException(400, "Invalid status")

    if order_dict["status"] == "Cancelled":
        conn.close()
        raise HTTPException(400, "Order is already cancelled")

    now = datetime.utcnow().isoformat()
    conn.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (body.status, now, order_id))

    # Automatic stock refund if order is Cancelled
    restored_products = []
    if body.status == "Cancelled":
        items = json.loads(order_dict["items"])
        for itm in items:
            conn.execute("UPDATE products SET stock = stock + ? WHERE id = ?", (itm["qty"], itm["product_id"]))
            p_row = conn.execute("SELECT * FROM products WHERE id=?", (itm["product_id"],)).fetchone()
            if p_row:
                restored_products.append(dict(p_row))

    conn.commit()
    updated = dict(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
    updated["items"] = json.loads(updated["items"])
    conn.close()

    # Real-time WebSocket pushes:
    # 1. To the customer who placed the order
    await manager.send_user(updated["customer_id"], {"type": "status_update", "order": updated})
    # 2. To the particular seller who owns the order
    await manager.send_user(updated["seller_id"], {"type": "status_update", "order": updated})

    # Broadcast replenished inventory if order was cancelled
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
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Order not found")

    order = dict(row)
    if order["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "You can only delay orders belonging to your store")

    if order["status"] in ("Completed", "Cancelled"):
        conn.close()
        raise HTTPException(400, f"Cannot delay an order that is {order['status']}")

    # Calculate new ETA
    try:
        curr_eta_str = order.get("eta", "")
        if ":" in curr_eta_str:
            parts = curr_eta_str.split(":")
            now = datetime.utcnow()
            eta_dt = now.replace(hour=int(parts[0]), minute=int(parts[1]))
            if eta_dt < now:
                eta_dt += timedelta(days=1)
            new_eta_dt = eta_dt + timedelta(minutes=body.delay_minutes)
            new_eta = new_eta_dt.strftime("%H:%M")
        else:
            new_eta = (datetime.utcnow() + timedelta(minutes=body.delay_minutes)).strftime("%H:%M")
    except Exception:
        new_eta = (datetime.utcnow() + timedelta(minutes=body.delay_minutes)).strftime("%H:%M")

    now = datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE orders SET status='Delayed', eta=?, delay_reason=?, updated_at=? WHERE id=?",
        (new_eta, body.reason.strip() or "Logistics delay", now, order_id)
    )
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
    updated["items"] = json.loads(updated["items"])
    conn.close()

    # Real-time push to the customer and this particular seller
    await manager.send_user(updated["customer_id"], {"type": "status_update", "order": updated})
    await manager.send_user(updated["seller_id"], {"type": "status_update", "order": updated})
    return updated


# ---------------------------------------------------------------- payment acceptance

@app.post("/api/orders/{order_id}/accept-payment")
async def accept_payment(order_id: str, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can accept or verify payment")
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Order not found")

    order = dict(row)
    if order["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "You can only manage payments for your own store's orders")

    now = datetime.utcnow().isoformat()
    new_status = "Paid (Verified by Seller)"
    conn.execute("UPDATE orders SET payment_status=?, updated_at=? WHERE id=?", (new_status, now, order_id))
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
    updated["items"] = json.loads(updated["items"])
    conn.close()

    # Real-time push to customer & seller
    await manager.send_user(updated["customer_id"], {"type": "payment_update", "order": updated})
    await manager.send_user(updated["seller_id"], {"type": "payment_update", "order": updated})
    return updated


# ---------------------------------------------------------------- tax invoice & labels

@app.get("/api/orders/{order_id}/invoice-data")
def get_invoice_data(order_id: str, user=Depends(get_current_user)):
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Order not found")
    order = dict(row)
    if user["role"] == "seller" and order["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")
    if user["role"] == "customer" and order["customer_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")

    seller_row = conn.execute("SELECT id, name, email FROM users WHERE id=?", (order["seller_id"],)).fetchone()
    customer_row = conn.execute("SELECT id, name, email FROM users WHERE id=?", (order["customer_id"],)).fetchone()
    conn.close()

    order["items"] = json.loads(order["items"])
    seller = dict(seller_row) if seller_row else {"id": order["seller_id"], "name": order["seller_name"], "email": f"{order['seller_id']}@seller.orderflow.ai"}
    customer = dict(customer_row) if customer_row else {"id": order["customer_id"], "name": order["customer_name"], "email": f"{order['customer_id']}@customer.orderflow.ai"}

    subtotal = sum(i["price"] * i["qty"] for i in order["items"])
    discount = float(order.get("discount_amount") or 0.0)
    net_after_discount = max(0.0, subtotal - discount)
    gst_rate = 18.0
    taxable_val = round(net_after_discount / (1 + gst_rate / 100), 2)
    gst_amt = round(net_after_discount - taxable_val, 2)
    cgst = round(gst_amt / 2, 2)
    sgst = round(gst_amt / 2, 2)

    return {
        "invoice_number": f"INV-{order['created_at'][:10].replace('-', '')}-{order['id']}",
        "order": order,
        "seller": seller,
        "customer": customer,
        "financials": {
            "subtotal": subtotal,
            "discount": discount,
            "taxable_value": taxable_val,
            "cgst": cgst,
            "sgst": sgst,
            "gst_total": gst_amt,
            "grand_total": order["amount"],
        },
        "tax_identifier": f"29AAACT{order['seller_id']}Z5",
        "hsn_sac": "8518 / 9405 / 9506",
        "state_code": "KA-29",
    }


# ---------------------------------------------------------------- order chat (customer <-> seller)

class SendMessageBody(BaseModel):
    message: str


@app.get("/api/orders/{order_id}/messages")
def get_order_messages(order_id: str, user=Depends(get_current_user)):
    conn = db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        conn.close()
        raise HTTPException(404, "Order not found")
    if user["role"] == "seller" and order["seller_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")
    if user["role"] == "customer" and order["customer_id"] != user["sub"]:
        conn.close()
        raise HTTPException(403, "Access denied")

    rows = conn.execute("SELECT * FROM order_messages WHERE order_id=? ORDER BY timestamp ASC", (order_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/orders/{order_id}/messages")
async def send_order_message(order_id: str, body: SendMessageBody, user=Depends(get_current_user)):
    clean_text = body.message.strip()
    if not clean_text:
        raise HTTPException(400, "Message cannot be empty")

    conn = db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
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
        "id": mid,
        "order_id": order_id,
        "sender_id": user["sub"],
        "sender_name": user["name"],
        "sender_role": user["role"],
        "message": clean_text,
        "timestamp": now,
    }
    conn.execute(
        "INSERT INTO order_messages (id, order_id, sender_id, sender_name, sender_role, message, timestamp) VALUES (?,?,?,?,?,?,?)",
        (mid, order_id, user["sub"], user["name"], user["role"], clean_text, now),
    )
    conn.commit()
    conn.close()

    # Real-time WebSocket delivery to both parties
    recipient_id = order["seller_id"] if user["role"] == "customer" else order["customer_id"]
    await manager.send_user(recipient_id, {"type": "chat_message", "message": msg_obj})
    await manager.send_user(user["sub"], {"type": "chat_message", "message": msg_obj})
    return msg_obj


# ---------------------------------------------------------------- coupons

class CreateCouponBody(BaseModel):
    code: str
    discount_type: str = "percent"  # "percent" or "flat"
    discount_value: float
    min_order: float = 0.0


class ValidateCouponBody(BaseModel):
    code: str
    order_amount: float


@app.get("/api/coupons")
def list_coupons(user=Depends(get_current_user)):
    conn = db()
    if user["role"] == "seller":
        rows = conn.execute("SELECT * FROM coupons WHERE seller_id=? OR seller_id='all' ORDER BY created_at DESC", (user["sub"],)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM coupons WHERE is_active=1 ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/coupons")
def create_coupon(body: CreateCouponBody, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can create discount coupons")
    clean_code = body.code.strip().upper()
    if len(clean_code) < 3:
        raise HTTPException(400, "Coupon code must be at least 3 characters")
    if body.discount_value <= 0:
        raise HTTPException(400, "Discount value must be greater than 0")
    if body.discount_type == "percent" and body.discount_value > 90:
        raise HTTPException(400, "Percentage discount cannot exceed 90%")
    if body.discount_type not in ("percent", "flat"):
        raise HTTPException(400, "discount_type must be either 'percent' or 'flat'")

    conn = db()
    existing = conn.execute("SELECT 1 FROM coupons WHERE code=?", (clean_code,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(400, f"Coupon code '{clean_code}' already exists")

    cid = str(uuid.uuid4())[:8].upper()
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO coupons (id, code, discount_type, discount_value, min_order, seller_id, is_active, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (cid, clean_code, body.discount_type, body.discount_value, max(0.0, body.min_order), user["sub"], 1, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM coupons WHERE id=?", (cid,)).fetchone()
    conn.close()
    return dict(row)


@app.delete("/api/coupons/{coupon_id}")
def delete_coupon(coupon_id: str, user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can delete coupons")
    conn = db()
    row = conn.execute("SELECT * FROM coupons WHERE id=?", (coupon_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Coupon not found")
    if row["seller_id"] != user["sub"] and row["seller_id"] != "all":
        conn.close()
        raise HTTPException(403, "You can only delete coupons created by your store")

    conn.execute("DELETE FROM coupons WHERE id=?", (coupon_id,))
    conn.commit()
    conn.close()
    return {"success": True, "coupon_id": coupon_id}


@app.post("/api/coupons/validate")
def validate_coupon(body: ValidateCouponBody):
    clean_code = body.code.strip().upper()
    conn = db()
    row = conn.execute("SELECT * FROM coupons WHERE code=? AND is_active=1", (clean_code,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, f"Invalid or expired promo code '{clean_code}'")

    coupon = dict(row)
    if body.order_amount < coupon["min_order"]:
        raise HTTPException(400, f"Code '{clean_code}' requires a minimum order of ₹{coupon['min_order']:.2f}")

    if coupon["discount_type"] == "percent":
        discount = round((body.order_amount * coupon["discount_value"]) / 100.0, 2)
    else:
        discount = min(coupon["discount_value"], body.order_amount)

    final_amount = max(0.0, round(body.order_amount - discount, 2))
    return {
        "valid": True,
        "code": clean_code,
        "discount_type": coupon["discount_type"],
        "discount_value": coupon["discount_value"],
        "discount_amount": discount,
        "final_amount": final_amount,
        "message": f"Coupon '{clean_code}' applied! You saved ₹{discount:.2f}"
    }


# ---------------------------------------------------------------- analytics

@app.get("/api/analytics/seller")
def get_seller_analytics(user=Depends(get_current_user)):
    if user["role"] != "seller":
        raise HTTPException(403, "Only sellers can view analytics")
    conn = db()
    seller_id = user["sub"]
    # Strictly orders for this specific seller!
    rows = conn.execute("SELECT * FROM orders WHERE seller_id=?", (seller_id,)).fetchall()
    conn.close()

    orders = []
    for r in rows:
        d = dict(r)
        d["items"] = json.loads(d["items"])
        orders.append(d)

    now = datetime.utcnow()
    current_year_month = now.strftime("%Y-%m")
    current_month_name = now.strftime("%B %Y")

    this_month_orders = [o for o in orders if o["created_at"].startswith(current_year_month)]
    this_month_completed = [o for o in this_month_orders if o["status"] == "Completed"]
    this_month_non_cancelled = [o for o in this_month_orders if o["status"] != "Cancelled"]
    this_month_revenue = sum(o["amount"] for o in this_month_non_cancelled)

    total_revenue = sum(o["amount"] for o in orders if o["status"] != "Cancelled")
    completed_revenue = sum(o["amount"] for o in orders if o["status"] == "Completed")
    pending_orders = len([o for o in orders if o["status"] in ("Placed", "Processing", "Delayed")])
    completed_orders = len([o for o in orders if o["status"] == "Completed"])
    cancelled_orders = len([o for o in orders if o["status"] == "Cancelled"])
    delayed_orders = len([o for o in orders if o["status"] == "Delayed"])

    # Daily revenue breakdown for this month
    days_in_month = {}
    for o in this_month_non_cancelled:
        day_str = o["created_at"][:10]
        days_in_month[day_str] = days_in_month.get(day_str, 0) + o["amount"]

    avg_order_value = (this_month_revenue / len(this_month_non_cancelled)) if this_month_non_cancelled else 0
    fulfillment_rate = (len(this_month_completed) / len(this_month_orders) * 100) if this_month_orders else 100

    return {
        "seller_id": seller_id,
        "current_month_name": current_month_name,
        "this_month_revenue": this_month_revenue,
        "this_month_orders": len(this_month_orders),
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
    seller_id = user["sub"]
    rows = conn.execute("SELECT * FROM orders WHERE seller_id=?", (seller_id,)).fetchall()
    conn.close()

    cust_map = {}
    for r in rows:
        d = dict(r)
        cid = d["customer_id"]
        cname = d["customer_name"]
        if cid not in cust_map:
            cust_map[cid] = {
                "id": cid,
                "name": cname,
                "orders_count": 0,
                "total_spent": 0,
                "last_order": d["created_at"]
            }
        cust_map[cid]["orders_count"] += 1
        if d["status"] != "Cancelled":
            cust_map[cid]["total_spent"] += d["amount"]

    customers = sorted(cust_map.values(), key=lambda c: c["total_spent"], reverse=True)
    return {"total_unique_customers": len(customers), "customers": customers}


@app.get("/")
def root():
    return {"status": "OrderFlow AI Multi-Seller Backend Running"}
