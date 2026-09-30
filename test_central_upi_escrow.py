import sys
import os
import json

# Ensure utf-8 output on windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Add backend directory to sys.path
backend_dir = os.path.join(os.path.dirname(__file__), "OrderFlow-AI", "backend")
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from fastapi.testclient import TestClient
import main

client = TestClient(main.app)

def test_central_upi_escrow_system():
    print("🚀 Starting Central UPI Escrow System Verification Tests...")

    # 1. Test Public Escrow Config Endpoint
    print("\n--- 1. Testing GET /api/platform/escrow-config ---")
    resp = client.get("/api/platform/escrow-config")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    cfg = resp.json()
    print("Public Escrow Config:", cfg)
    assert cfg["central_upi_id"] == "ttcreations2.0@ybl", f"Expected ttcreations2.0@ybl, got {cfg['central_upi_id']}"
    assert cfg["commission_percent"] == 8.0, f"Expected 8.0%, got {cfg['commission_percent']}"
    print("✅ Step 1 passed: Central UPI ID is ttcreations2.0@ybl and Commission is 8.0%")

    # 2. Login as Admin
    print("\n--- 2. Logging in as Admin ---")
    login_resp = client.post("/api/auth/login", json={
        "id": "956673",
        "password": "Soundar@52122",
        "role": "admin"
    })
    assert login_resp.status_code == 200, f"Admin login failed: {login_resp.text}"
    admin_token = login_resp.json()["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    print("✅ Step 2 passed: Admin logged in successfully")

    # 3. Test Admin Platform Config GET & POST
    print("\n--- 3. Testing GET & POST /api/admin/platform-config ---")
    adm_cfg_resp = client.get("/api/admin/platform-config", headers=admin_headers)
    assert adm_cfg_resp.status_code == 200
    adm_cfg = adm_cfg_resp.json()
    assert adm_cfg["central_upi_id"] == "ttcreations2.0@ybl"
    assert adm_cfg["commission_percent"] == 8.0
    print("Admin Config:", adm_cfg)

    # Test updating and restoring platform config
    update_resp = client.post("/api/admin/platform-config", headers=admin_headers, json={
        "central_upi_id": "ttcreations2.0@ybl",
        "platform_payee_name": "OrderFlow Escrow (TT Creations)",
        "commission_percent": 8.0
    })
    assert update_resp.status_code == 200
    print("✅ Step 3 passed: Admin platform config verified and functional")

    # 4. Test UPI Payment Intent Generation
    print("\n--- 4. Testing POST /api/payments/upi/intent ---")
    intent_resp = client.post("/api/payments/upi/intent", headers=admin_headers, json={
        "amount": 1000.0,
        "order_ref": "ORD_TEST_ESCROW_123"
    })
    assert intent_resp.status_code == 200, f"Intent creation failed: {intent_resp.text}"
    intent_data = intent_resp.json()
    print("UPI Intent Data:", json.dumps(intent_data, indent=2))
    assert intent_data["is_central_escrow"] is True
    assert intent_data["upi_id"] == "ttcreations2.0@ybl"
    assert intent_data["commission_percent"] == 8.0
    assert intent_data["platform_fee"] == 80.0
    assert intent_data["seller_payout"] == 920.0
    assert "pa=ttcreations2.0@ybl" in intent_data["upi_string"]
    print("✅ Step 4 passed: UPI Intent routes to ttcreations2.0@ybl with 8% cut (₹80 fee, ₹920 payout)")

    # 5. Customer Login & Create Order
    print("\n--- 5. Customer Login & Placing Order with Escrow Split ---")
    cust_login = client.post("/api/auth/login", json={
        "id": "7001001",
        "password": "customer123",
        "role": "customer"
    })
    assert cust_login.status_code == 200, f"Customer login failed: {cust_login.text}"
    cust_token = cust_login.json()["access_token"]
    cust_headers = {"Authorization": f"Bearer {cust_token}"}

    # Get a product from seller 1001001
    products_resp = client.get("/api/products")
    assert products_resp.status_code == 200
    prods = products_resp.json()
    target_prod = next((p for p in prods if p.get("seller_id") == "1001001"), prods[0])
    print(f"Target product: {target_prod['name']} (Price: ₹{target_prod['price']}, Seller: {target_prod.get('seller_id')})")

    # Create Order
    order_create_resp = client.post("/api/orders", headers=cust_headers, json={
        "items": [{"product_id": target_prod["id"], "name": target_prod["name"], "price": target_prod["price"], "qty": 1}],
        "payment_method": "UPI",
        "shipping_address": "42 Cyber Road, Bengaluru",
        "phone": "+91 98765 43210"
    })
    assert order_create_resp.status_code == 200, f"Order creation failed: {order_create_resp.text}"
    resp_body = order_create_resp.json()
    new_order = resp_body.get("order") or resp_body.get("orders", [{}])[0]
    print(f"New Order Created: ID={new_order['id']}, Amount=₹{new_order['amount']}, Status={new_order['status']}")
    print(f"Platform Fee (8%): ₹{new_order.get('platform_fee')}, Seller Net Payout (92%): ₹{new_order.get('seller_payout')}")
    print(f"Escrow Payout Status: {new_order.get('payout_status')}")

    order_id = new_order["id"]
    order_total = float(new_order["amount"])
    expected_fee = round(order_total * 0.08, 2)
    expected_payout = round(order_total - expected_fee, 2)
    assert abs(new_order.get("platform_fee", 0) - expected_fee) < 0.05
    assert abs(new_order.get("seller_payout", 0) - expected_payout) < 0.05
    assert new_order.get("payout_status") == "in_escrow"
    print("✅ Step 5 passed: Order successfully created with 'in_escrow' payout status and 8% commission split")

    # 6. Seller Login & Check Settlements
    print("\n--- 6. Seller Login & Settlements Hub Verification ---")
    seller_login = client.post("/api/auth/login", json={
        "id": "1001001",
        "password": "seller123",
        "role": "seller"
    })
    assert seller_login.status_code == 200, f"Seller login failed: {seller_login.text}"
    seller_token = seller_login.json()["access_token"]
    seller_headers = {"Authorization": f"Bearer {seller_token}"}

    settlements_resp = client.get("/api/seller/settlements", headers=seller_headers)
    assert settlements_resp.status_code == 200, f"Settlements fetch failed: {settlements_resp.text}"
    settlements = settlements_resp.json()
    summary = settlements["summary"]
    print("Seller Settlements Overview:")
    print(f"  Gross Revenue: ₹{summary['total_gross']}")
    print(f"  8% Platform Fee Total: ₹{summary['total_platform_fees']}")
    print(f"  In Escrow Balance: ₹{summary['in_escrow']}")
    print(f"  Ready for Payout: ₹{summary['eligible_payout']}")
    print(f"  Total Disbursed: ₹{summary['total_disbursed']}")
    print(f"  Bank Account: {summary.get('bank_name')} - {summary.get('bank_account_no')}")
    assert summary["in_escrow"] > 0
    print("✅ Step 6 passed: Seller settlements hub correctly reflects in-escrow funds")

    # 7. Update Seller Payout / Bank Details
    print("\n--- 7. Testing Seller Payout Details Update ---")
    payout_update_resp = client.patch("/api/seller/payout-details", headers=seller_headers, json={
        "upi_id": "soundar.store@okhdfcbank",
        "bank_account_no": "918237465012",
        "bank_ifsc": "HDFC0001234",
        "bank_name": "HDFC Bank Ltd"
    })
    assert payout_update_resp.status_code == 200
    print("Updated Seller Payout Details:", payout_update_resp.json())
    print("✅ Step 7 passed: Seller payout and bank details updated")

    # 8. Complete the Order & Check Automatic Payout Eligibility Transition
    print("\n--- 8. Completing Order & Checking Escrow Transition to 'eligible_for_payout' ---")
    status_update_resp = client.patch(f"/api/orders/{order_id}/status", headers=seller_headers, json={
        "status": "Completed"
    })
    assert status_update_resp.status_code == 200
    updated_order = status_update_resp.json()
    print(f"Order {order_id} Status Updated to: {updated_order['status']}")
    print(f"Order Escrow Payout Status: {updated_order.get('payout_status')}")
    assert updated_order.get("payout_status") == "eligible_for_payout", f"Expected eligible_for_payout, got {updated_order.get('payout_status')}"
    print("✅ Step 8 passed: Completed order automatically transitioned to 'eligible_for_payout'")

    # 9. Admin Treasury Verification & 1-Click Disbursal
    print("\n--- 9. Admin Treasury Hub & Disbursal ---")
    treasury_resp = client.get("/api/admin/treasury", headers=admin_headers)
    assert treasury_resp.status_code == 200
    treasury = treasury_resp.json()
    treasury_summary = treasury["summary"]
    print("Admin Treasury Overview:")
    print(f"  Central UPI ID: {treasury_summary['central_upi_id']}")
    print(f"  Platform Commission Earned: ₹{treasury_summary['total_commission_earned']}")
    print(f"  Total Escrow Holding: ₹{treasury_summary['total_escrow_holding']}")
    print(f"  Pending Disbursals Count: {len(treasury['pending_disbursals'])}")
    print(f"  Disbursed History Count: {len(treasury['disbursed_history'])}")

    # Find our completed order in pending_disbursals
    pending_item = next((item for item in treasury["pending_disbursals"] if item["order_id"] == order_id), None)
    assert pending_item is not None, f"Order {order_id} not found in pending disbursals!"
    print(f"Found pending disbursal for order {order_id}: Net Payout = ₹{pending_item['seller_payout']}")

    # Disburse Payout
    disburse_resp = client.post("/api/admin/payouts/disburse", headers=admin_headers, json={
        "order_id": order_id,
        "utr_number": "UTR_TEST_VERIFY_9999",
        "notes": "Verified settlement test run"
    })
    assert disburse_resp.status_code == 200, f"Disbursal failed: {disburse_resp.text}"
    disbursed_result = disburse_resp.json()
    print("Disbursal Result:", disbursed_result)
    assert disbursed_result["success"] is True
    assert disbursed_result["order_id"] == order_id
    assert disbursed_result["utr_number"] == "UTR_TEST_VERIFY_9999"
    print("✅ Step 9 passed: 1-Click Admin Disbursal executed with custom UTR and verified in DB")

    # 10. Final Verification on Seller Settlements
    print("\n--- 10. Final Verification of Seller Settlements Post-Disbursal ---")
    final_settlements_resp = client.get("/api/seller/settlements", headers=seller_headers)
    assert final_settlements_resp.status_code == 200
    final_settlements = final_settlements_resp.json()
    print(f"Post-Disbursal Disbursed Total: ₹{final_settlements['summary']['total_disbursed']}")
    disbursed_order_entry = next((o for o in final_settlements["settlements"] if o["order_id"] == order_id), None)
    assert disbursed_order_entry is not None
    assert disbursed_order_entry["payout_status"] == "disbursed"
    assert disbursed_order_entry["disbursed_utr"] == "UTR_TEST_VERIFY_9999"
    print(f"Order {order_id} in seller settlement ledger: Status={disbursed_order_entry['payout_status']}, UTR={disbursed_order_entry['disbursed_utr']}")
    print("✅ Step 10 passed: Seller ledger reflects disbursed settlement and UTR")

    print("\n🎉 ========================================================")
    print("🎉 ALL 10 CENTRAL UPI ESCROW & SETTLEMENT TESTS PASSED 100%!")
    print("🎉 ========================================================\n")

if __name__ == "__main__":
    test_central_upi_escrow_system()
