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
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

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
            created_at TEXT,
            updated_at TEXT
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

    # 3. Create a separate order per seller so each seller only receives & fulfills their own items!
    for sid, group in seller_groups.items():
        s_items = group["items"]
        s_name = group["seller_name"]
        order_amount = sum(item.price * item.qty for item, _ in s_items)
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
            "created_at": now,
            "updated_at": now,
        }

        conn.execute(
            "INSERT INTO orders (id, customer_id, customer_name, seller_id, seller_name, items, amount, status, eta, delay_reason, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, user["sub"], user["name"], sid, s_name, json.dumps(serialized_items), order_amount, "Placed", eta, "", now, now),
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
