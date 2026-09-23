# OrderFlow AI — MVP Core

A working slice of the full brief: **VisionOS neon-glassmorphism UI with real animation**, **Google Sign-In** for both Customer and Seller, and **true live sync** — when a customer places an order it is pushed over a WebSocket and appears on the seller's screen with zero refresh; when a seller advances an order's status, it's pushed back to that customer's live tracking timeline.

## What's actually built
- FastAPI backend (`backend/main.py`): SQLite storage, Google ID-token verification, JWT sessions, REST for products/orders, WebSocket broadcast for live sync.
- Single-page frontend (`frontend/index.html`): animated aurora background, glass cards, glowing floating tabs, Customer flow (shop → cart → checkout → live order tracking with an animated timeline + confetti on completion), Seller flow (animated KPI counters, a live orders table, a Kanban board).

## What's not built yet (intentionally, to keep this shippable)
Admin dashboard, invoices/PDF export, revenue charts, notifications center, settings/profile pages, multi-seller routing, product/inventory CRUD UI, mobile bottom-nav. The backend and live-sync pattern here are built so each of those is an incremental addition, not a rewrite — say which one you want next and it can be added the same way.

## 1. Google OAuth setup (needed for real sign-in)
1. Go to https://console.cloud.google.com/apis/credentials
2. Create an **OAuth client ID** → Application type: **Web application**.
3. Under "Authorized JavaScript origins" add the origin you'll open the frontend from, e.g. `http://localhost:5500` (or wherever you serve `frontend/index.html` — it must be `http`/`https`, not `file://`, for Google Sign-In to work).
4. Copy the generated Client ID.
5. Paste it into **two** places:
   - `frontend/index.html` → `CONFIG.GOOGLE_CLIENT_ID`
   - Backend env var `GOOGLE_CLIENT_ID` (below)

## 2. Run the backend
```bash
cd backend
pip install -r requirements.txt --break-system-packages
export GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export JWT_SECRET="pick-something-long-and-random"
uvicorn main:app --reload --port 8000
```
This creates `orderflow.db` (SQLite) automatically and seeds a demo seller with 6 products on first run.

## 3. Serve the frontend
Google Sign-In needs a real origin, not `file://`. Easiest option:
```bash
cd frontend
python3 -m http.server 5500
```
Then open `http://localhost:5500` in your browser (make sure this matches the origin you added in step 1.3).

## 4. Try the live sync
1. Open the app in **two browser windows** — sign in as **Customer** in one, **Seller** in the other (any Google account works for both; role is just what you pick on the login screen).
2. In the Customer window: add a product to the cart, place the order.
3. Watch the Seller window's "Live Orders" tab — the order appears instantly with a toast, no refresh.
4. In the Seller window, click "Mark Processing" then "Mark Completed" on that order.
5. Watch the Customer window's "My Orders" tab — the timeline animates forward live, and confetti fires on completion.

## Notes
- CORS is wide open (`*`) for local dev — lock this down before deploying.
- The demo assigns all orders to a single seller (`demo-seller`) for simplicity; multi-seller assignment is a small extension to `create_order`/`list_orders` in `main.py`.
- `JWT_SECRET` and `GOOGLE_CLIENT_ID` should never be committed — use environment variables or a `.env` file in real deployment.
