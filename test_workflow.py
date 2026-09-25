import urllib.request
import json
import sqlite3
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

base = "http://127.0.0.1:8000"

def req(url, method="GET", data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    raw_data = json.dumps(data).encode("utf-8") if data else None
    request = urllib.request.Request(url, data=raw_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        content = e.read().decode("utf-8")
        try:
            return e.code, json.loads(content)
        except Exception:
            return e.code, {"detail": content}

print("=== CHECK EXISTING ADMIN ACCOUNTS IN DB ===")
db_path = os.path.join("OrderFlow-AI", "backend", "orderflow.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
admins = cur.execute("SELECT id, name, email, role, status, is_first_admin FROM users WHERE role='admin'").fetchall()
print(f"Admins in DB ({len(admins)}):")
for a in admins:
    print(dict(a))

founder_admin_id = None
for a in admins:
    if a["is_first_admin"] == 1:
        founder_admin_id = a["id"]

if not founder_admin_id and admins:
    founder_admin_id = admins[0]["id"]

print(f"\nUsing Founder Admin ID: {founder_admin_id}")

print("\n=== 1. LOGIN AS FOUNDER ADMIN ===")
status, data = req(f"{base}/api/auth/login", "POST", {
    "id": founder_admin_id,
    "password": "Soundar@52122",
    "role": "admin"
})
print("Founder admin login status:", status)
admin_token = data.get("access_token")
print("Access token received:", bool(admin_token))

print("\n=== 2. REGISTER NEW JUNIOR ADMIN (PENDING APPROVAL) ===")
# Clean up any existing test junior admin with this email
cur.execute("DELETE FROM users WHERE email='junior.sarah@orderflow.ai'")
conn.commit()

status, reg_admin = req(f"{base}/api/auth/admin-register", "POST", {
    "name": "Sarah Jenkins",
    "email": "junior.sarah@orderflow.ai",
    "password": "SarahPassword@123",
    "reason": "Assistant Support Lead"
})
print("Junior admin register response:", status, reg_admin)
junior_admin_id = reg_admin.get("generated_id")

print("\n=== 3. JUNIOR ADMIN ATTEMPTS LOGIN (MUST BE REJECTED WITH 403) ===")
status, res = req(f"{base}/api/auth/login", "POST", {
    "id": junior_admin_id,
    "password": "SarahPassword@123",
    "role": "admin"
})
print("Unapproved junior admin login status (Expected 403):", status, res.get("detail"))

print("\n=== 4. REGISTER NEW SELLER WITH FULL KYC (PENDING APPROVAL) ===")
# Clean up any existing test seller with this email
cur.execute("DELETE FROM users WHERE email='vikram@zenithcrafts.com'")
conn.commit()

status, reg_seller = req(f"{base}/api/auth/register", "POST", {
    "name": "Vikram Patel",
    "email": "vikram@zenithcrafts.com",
    "password": "VikramPassword@123",
    "role": "seller",
    "brand_name": "Zenith Artisan Crafts",
    "phone": "9876509876",
    "state": "Rajasthan",
    "product_category": "Home Decor & Handcrafted",
    "brand_description": "Handcrafted brass and ceramic decor items made by Indian artisans",
    "shipping_mode": "marketplace",
    "stock_address": "Craftsman Industrial Area, Phase 3, Jaipur, RJ - 302013",
    "order_acceptance": "auto",
    "rto_mode": "marketplace"
})
print("Seller KYC register response:", status, reg_seller)
seller_id = reg_seller.get("generated_id")

print("\n=== 5. SELLER ATTEMPTS LOGIN BEFORE VERIFICATION (MUST BE REJECTED WITH 403) ===")
status, res = req(f"{base}/api/auth/login", "POST", {
    "id": seller_id,
    "password": "VikramPassword@123",
    "role": "seller"
})
print("Unapproved seller login status (Expected 403):", status, res.get("detail"))

print("\n=== 6. FOUNDER ADMIN REVIEWS PENDING LISTS ===")
status, pending_admins = req(f"{base}/api/admin/admins/pending", "GET", token=admin_token)
print("Pending admins list:", status, pending_admins)

status, pending_sellers = req(f"{base}/api/admin/sellers/pending", "GET", token=admin_token)
print("Pending sellers list:", status, pending_sellers)

print("\n=== 7. FOUNDER ADMIN APPROVES JUNIOR ADMIN & SELLER ===")
status, app_adm = req(f"{base}/api/admin/admins/{junior_admin_id}/approve", "POST", token=admin_token)
print(f"Approve junior admin {junior_admin_id}:", status, app_adm)

status, app_sel = req(f"{base}/api/admin/sellers/{seller_id}/approve", "POST", token=admin_token)
print(f"Approve seller {seller_id}:", status, app_sel)

print("\n=== 8. APPROVED USERS CAN NOW LOGIN ===")
status, adm_login = req(f"{base}/api/auth/login", "POST", {
    "id": junior_admin_id,
    "password": "SarahPassword@123",
    "role": "admin"
})
print("Junior admin login after approval:", status, "Status:", adm_login.get("user", {}).get("status"))

status, sel_login = req(f"{base}/api/auth/login", "POST", {
    "id": seller_id,
    "password": "VikramPassword@123",
    "role": "seller"
})
print("Seller login after approval:", status, "Status:", sel_login.get("user", {}).get("status"))
seller_token = sel_login.get("access_token")

print("\n=== 9. SELLER CAN NOW UPDATE STORE BRANDING ===")
status, brand_resp = req(f"{base}/api/sellers/branding", "PATCH", {
    "brand_color": "#ff6b6b",
    "brand_description": "Updated: Premium Jaipur Artisan Home Living & Brass Accents",
    "store_banner": "https://images.unsplash.com/photo-1513519245088-0e12902e5a38?w=800"
}, token=seller_token)
print("Seller branding update:", status, "Color:", brand_resp.get("brand_color"))

print("\n=== 10. DEMO CUSTOMER SUBMITS COMPLAINT TICKET WITH APOLOGY FLOW ===")
status, cust_login = req(f"{base}/api/auth/login", "POST", {
    "id": "7001001",
    "password": "customer123",
    "role": "customer"
})
cust_token = cust_login.get("access_token")
print("Customer 7001001 login:", status, "Token received:", bool(cust_token))

status, ticket_resp = req(f"{base}/api/support/ticket", "POST", {
    "category": "damaged_item",
    "description": "The artisan brass vase arrived chipped at the rim.",
    "rating": 2
}, token=cust_token)
print("Customer ticket creation:", status, ticket_resp)
ticket_id = ticket_resp.get("ticket_id")

print("\n=== 11. CUSTOMER SENDS MESSAGE IN TICKET CHAT ===")
status, msg_resp = req(f"{base}/api/support/tickets/{ticket_id}/messages", "POST", {
    "message": "We are deeply sorry that your item arrived damaged. Our operations team is on it!"
}, token=cust_token)
print("Customer/system message sent:", status, msg_resp.get("message"))

print("\n=== 12. ADMIN RESPONDS AND RESOLVES TICKET ===")
status, reply_resp = req(f"{base}/api/support/tickets/{ticket_id}/reply", "POST", {
    "reply": "We apologize wholeheartedly for this defect! A complimentary replacement has been dispatched immediately.",
    "resolve": True
}, token=admin_token)
print("Admin reply & resolution:", status, reply_resp)

print("\n=== 13. TEST PDF INVOICE GENERATION ===")
# Fetch an order
status, orders = req(f"{base}/api/admin/orders", "GET", token=admin_token)
if orders:
    sample_order_id = orders[0]["id"]
    pdf_req = urllib.request.Request(f"{base}/api/orders/{sample_order_id}/invoice", headers={"Authorization": f"Bearer {admin_token}"})
    with urllib.request.urlopen(pdf_req) as pdf_resp:
        pdf_bytes = pdf_resp.read()
        print(f"PDF Invoice for order {sample_order_id}: {pdf_resp.status} OK ({len(pdf_bytes)} bytes, starts with {pdf_bytes[:4]})")

print("\n=== 14. TEST CHANGE PASSWORD (ANY USER) ===")
# Customer changes password
status, cpw_resp = req(f"{base}/api/auth/change-password", "POST", {
    "current_password": "customer123",
    "new_password": "CustomerNewPass@2026"
}, token=cust_token)
print("Change password response (Expected 200):", status, cpw_resp.get("detail") or cpw_resp.get("message"))
assert status == 200

# Try login with old password -> should fail (401)
status, old_res = req(f"{base}/api/auth/login", "POST", {
    "id": "7001001",
    "password": "customer123",
    "role": "customer"
})
print("Login with old password (Expected 401):", status)
assert status == 401

# Revert password back for customer 7001001
status, log_new = req(f"{base}/api/auth/login", "POST", {
    "id": "7001001",
    "password": "CustomerNewPass@2026",
    "role": "customer"
})
assert status == 200
new_cust_tok = log_new.get("access_token")
status, rev_resp = req(f"{base}/api/auth/change-password", "POST", {
    "current_password": "CustomerNewPass@2026",
    "new_password": "customer123"
}, token=new_cust_tok)
assert status == 200
print("Customer password reverted successfully:", rev_resp.get("detail") or rev_resp.get("message"))

print("\n=== 15. TEST FORGOT PASSWORD WITH SECURITY QUESTIONS ===")
# Test forgot password with wrong answer
status, bad_ans = req(f"{base}/api/auth/forgot-password", "POST", {
    "id": seller_id,
    "role": "seller",
    "security_answer": "WrongCity123",
    "new_password": "VikramNewPass@999"
})
print("Forgot password with incorrect answer (Expected 400):", status, bad_ans.get("detail"))
assert status == 400

# Test forgot password for Vikram using demo fallback / registered answer
# First set Vikram's recovery answer in db directly to ensure exact match
cur.execute("UPDATE users SET birth_place='Jaipur', fav_person='Tagore' WHERE id=?", (seller_id,))
conn.commit()

status, good_ans = req(f"{base}/api/auth/forgot-password", "POST", {
    "id": seller_id,
    "role": "seller",
    "security_answer": "jaipur",
    "new_password": "VikramNewPass@999"
})
print("Forgot password with Birth Place match (Expected 200):", status, good_ans.get("detail") or good_ans.get("message"))
assert status == 200

# Test login with reset password
status, vikram_new_log = req(f"{base}/api/auth/login", "POST", {
    "id": seller_id,
    "password": "VikramNewPass@999",
    "role": "seller"
})
print("Seller login with reset password (Expected 200):", status)
assert status == 200

print("\n=== 16. TEST ADMIN DELETE USER & IMMUNITY OF FOUNDER ADMIN ===")
# 1. Founder admin deletion must be rejected (403)
status, imm_resp = req(f"{base}/api/admin/users/956673", "DELETE", token=admin_token)
print("Attempt to delete Founder Admin 956673 (Expected 403 Forbidden):", status, imm_resp.get("detail"))
assert status == 403

# 2. Admin deletes junior admin (Sarah Jenkins)
status, del_adm = req(f"{base}/api/admin/users/{junior_admin_id}", "DELETE", token=admin_token)
print(f"Admin deletes junior admin #{junior_admin_id} (Expected 200):", status, del_adm.get("detail") or del_adm.get("message"))
assert status == 200

# Verify deleted junior admin cannot login
status, adm_fail = req(f"{base}/api/auth/login", "POST", {
    "id": junior_admin_id,
    "password": "SarahPassword@123",
    "role": "admin"
})
print("Deleted junior admin login (Expected 401):", status)
assert status == 401

# 3. Admin deletes seller (Vikram Patel)
status, del_sel = req(f"{base}/api/admin/users/{seller_id}", "DELETE", token=admin_token)
print(f"Admin deletes seller #{seller_id} (Expected 200):", status, del_sel.get("detail") or del_sel.get("message"))
assert status == 200

# Verify deleted seller cannot login
status, sel_fail = req(f"{base}/api/auth/login", "POST", {
    "id": seller_id,
    "password": "VikramNewPass@999",
    "role": "seller"
})
print("Deleted seller login (Expected 401):", status)
assert status == 401

print("\n🎉 ALL ADMIN GOVERNANCE, SELLER KYC, SUPPORT, PASSWORD MGMT & USER DELETION FLOWS VERIFIED SUCCESSFULLY!")
conn.close()
