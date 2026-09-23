# OrderFlow AI — Real-Time Multi-Seller Operations & Marketplace

> **A Flipkart-Grade Real-Time Order Management System** featuring **VisionOS Neon-Glassmorphism UI**, native **7-Digit ID + Password Authentication**, **Multi-Seller Order Routing & Isolation**, **Real-Time Stock Auto-Decrement**, **Order Delay ETA Sync**, and **Monthly Revenue Analytics**.

---

## 🌟 Key Features

### 1. 🔑 Native 7-Digit ID Authentication
- **Seller IDs**: 7-digit numeric IDs starting with `1` (e.g., `1001001`, `1002002`, `1003003`).
- **Customer IDs**: 7-digit numeric IDs starting with `7` (e.g., `7001001`, `7002002`).
- **Auto-Generating Registration**: New sellers and customers can register; the system automatically generates a unique 7-digit ID.
- **1-Click Test Profiles**: Pre-filled quick test accounts on the login screen for instant evaluation.

### 2. 🏪 Multi-Seller Order Splitting & Isolation (Flipkart-Style)
- **Store Attribution**: Every product card in the customer shop displays `🏪 Sold by: [Store Name] (ID: #[SellerID])`.
- **Store Filter Chips**: Customers can browse by store or view the combined marketplace.
- **Cart Splitting**: Adding items from multiple stores automatically splits them into individual sub-orders per seller upon checkout.
- **Isolated Order Dispatch**: Each seller strictly sees and manages orders placed for their store. No cross-seller order leakage.
- **New Seller Onboarding**: Newly registered sellers with 0 products see a welcome banner inviting them to list their first product.

### 3. ⚡ Live Real-Time Operations (WebSocket Sync)
- **Instant Stock Auto-Decrement**: When orders are placed, product inventory decrements in real time across all open browser windows.
- **Order Delay Management**: Sellers can delay an order (`+15m`, `+30m`, `+45m`, `+1h`) with custom reasons (*Heavy Rain*, *Packaging Delay*), which immediately updates the customer's live tracking banner with a glowing amber alert and recalculated ETA.
- **Order Cancellation & Stock Refund**: If a customer or seller cancels an order, purchased quantities are automatically refunded to inventory.
- **Harmonic Audio Chimes**: Web Audio API order alerts chime when new orders arrive.

### 4. 📊 Financial Ledger & Monthly Earnings
- Real-time monthly revenue KPI, fulfillment rate %, pending order counters, and Average Order Value (AOV).
- Interactive **Daily Revenue Volume Chart** showing day-by-day sales velocity.
- Top customer repeat-buyer insights.

---

## 🔑 Pre-Configured Test Accounts

| Role | 7-Digit ID | Password | Name / Store | Catalog |
| :--- | :--- | :--- | :--- | :--- |
| **Seller** | `1001001` | `seller123` | **TechNova Electronics** | Headphones, Smartwatches, Keyboards |
| **Seller** | `1002002` | `seller123` | **Aura Home Living** | Ambient Lamps, Ceramic Mug Sets |
| **Seller** | `1003003` | `seller123` | **Titanium Fitness Gear** | Yoga Mats, Dumbbells |
| **Customer** | `7001001` | `customer123` | **Alex Rivera** | Verified Shopper |
| **Customer** | `7002002` | `customer123` | **Priya Sharma** | Verified Shopper |

---

## 🚀 How to Deploy on the Web (Free)

The project is structured so **FastAPI serves both the backend API, WebSockets, and the frontend UI under a single URL**.

### Option 1: Deploy on Render.com (Recommended - 100% Free)

1. Go to **[Render.com](https://render.com/)** and sign in with your GitHub account.
2. Click **New +** → **Web Service**.
3. Select your repository: `soundararajan-1/ORDER-MANAGEMENT-SYSTEM`.
4. Configure the service:
   - **Name**: `orderflow-ai` (or any name you prefer)
   - **Region**: Choose the closest region (e.g. *Singapore* or *Frankfurt*)
   - **Runtime**: `Python 3`
   - **Build Command**:
     ```bash
     pip install -r OrderFlow-AI/backend/requirements.txt
     ```
   - **Start Command**:
     ```bash
     uvicorn OrderFlow-AI.backend.main:app --host 0.0.0.0 --port $PORT
     ```
   - **Instance Type**: `Free`
5. Click **Create Web Service**.
6. Render will build and deploy your app in ~2 minutes and provide a free live URL:
   `https://orderflow-ai.onrender.com`

---

### Option 2: Deploy on Railway.app

1. Go to **[Railway.app](https://railway.app/)** and connect GitHub.
2. Click **New Project** → **Deploy from GitHub repo** → select `ORDER-MANAGEMENT-SYSTEM`.
3. In the project settings, set:
   - **Start Command**: `uvicorn OrderFlow-AI.backend.main:app --host 0.0.0.0 --port $PORT`
4. Click **Generate Domain** under Settings → Networking to get your public HTTPS URL!

---

### Option 3: Deploy with Docker

A production-ready `Dockerfile` is included in the repository. Deploy to any Docker-supported cloud:

```bash
docker build -t orderflow-ai .
docker run -p 8000:8000 orderflow-ai
```
Visit `http://localhost:8000` in your browser.

---

## 💻 Local Development

```bash
# 1. Clone repository
git clone https://github.com/soundararajan-1/ORDER-MANAGEMENT-SYSTEM.git
cd ORDER-MANAGEMENT-SYSTEM/OrderFlow-AI

# 2. Install backend dependencies
cd backend
pip install -r requirements.txt

# 3. Start backend
python -m uvicorn main:app --reload --port 8000

# 4. Open in browser
# Visit http://127.0.0.1:8000 (FastAPI serves frontend directly!)
```

---

## 💡 Recommendations & Future Roadmap

Here are high-impact features recommended for future updates:

1. **Persistent Cloud Database (PostgreSQL / Supabase)**:
   - Connect free managed PostgreSQL (Supabase / Neon) so data persists permanently across container restarts.
2. **Downloadable Tax Invoice & Shipping Labels (PDF)**:
   - Add a button for sellers to generate printable PDF GST invoices with barcodes and order addresses.
3. **Discount Coupons & Promotional Deals**:
   - Allow sellers to create coupon codes (e.g. `SAVE20`, `FREESHIP`) and banner announcements.
4. **Direct Seller-Customer Live Chat**:
   - Order-specific real-time chat over WebSockets to ask about delivery instructions.
5. **Payment Gateway Integration (Test Mode)**:
   - Integrate Razorpay or Stripe sandbox for credit card, UPI, and net-banking checkout.
6. **Dark / Light Theme & Store Branding**:
   - Let sellers upload custom store banners and choose brand highlight colors.
PROJECT OUTCOMES:
<h2>📸 Project Screenshots</h2>

<h3>🔐 Login Page</h3>
<img src="login.png" width="800">

<h3>🛒 Customer Page</h3>
<img src="customerpage.png" width="800">

<h3>👤 Customer Details</h3>
<img src="customer.png" width="800">

<h3>🛍️ Order Management</h3>
<img src="order.png" width="800">

<h3>📦 Order Tracking</h3>
<img src="ordertracking.png" width="800">

<h3>🏪 Seller Dashboard</h3>
<img src="seller.png" width="800">

<h3>⏰ Order Delay Reason</h3>
<img src="delayreason.png" width="800">
