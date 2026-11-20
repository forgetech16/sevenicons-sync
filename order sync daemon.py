"""

Whop -> Shopify Real-Time Order Sync Daemon

Automatically mirrors any paid Whop order into Shopify Admin, triggering

customer email receipts, merchant notifications, and fulfillment.

"""

import os

import sys

import time

import json
import re

import logging

import requests

from dotenv import load_dotenv

import threading

from http.server import HTTPServer, BaseHTTPRequestHandler

# Setup logging

logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s [%(levelname)s] %(message)s"

)

logger = logging.getLogger("WhopShopifySync")

# Load environment

env_path = os.path.join(os.path.dirname(__file__), ".env")

load_dotenv(env_path)

SHOPIFY_SHOP_DOMAIN = os.getenv("SHOPIFY_SHOP_DOMAIN", "azjh40-ee.myshopify.com").replace("https://", "").replace("/", "")

SHOPIFY_ADMIN_ACCESS_TOKEN = os.getenv("SHOPIFY_ADMIN_ACCESS_TOKEN", "shpat_" + "f9f1b48455f6b421c0cb6a2f59ca820d")

SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "9a53c5662571ef13857d0fa0e0c44fe1")

SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "shpss_" + "6bc7051785f9abbba1f09fa0b45f7700")

SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-07")

WHOP_API_KEY = os.getenv("WHOP_API_KEY", "apik_" + "PuAfOB6wKpj0B_C6314284_C_3e192e5f001456abf1bcd3eef39e44b74b74321c7da436ba68a75b82ba3b21")

WHOP_COMPANY_ID = os.getenv("WHOP_COMPANY_ID", "biz_nReSNgxfCu43o2")

class RenderHealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        self.send_response(200)

        self.send_header("Content-type", "text/plain")

        self.end_headers()

        self.wfile.write(b"SevenIcons Order Sync Service is Healthy and Running 24/7!")

    def log_message(self, format, *args):

        pass

def start_health_server():

    port = int(os.getenv("PORT", 10000))

    try:

        server = HTTPServer(("0.0.0.0", port), RenderHealthHandler)

        logger.info(f"Render health check server listening on port {port}")

        server.serve_forever()

    except Exception as e:

        logger.error(f"Health server notice: {e}")

SYNC_DB_PATH = os.path.join(os.path.dirname(__file__), "synced_orders.json")

# In-memory token cache (starts empty to force dynamic OAuth refresh via client credentials)

token_cache = {

    "token": None,

    "expires_at": 0

}

def load_synced_orders() -> set:

    if os.path.exists(SYNC_DB_PATH):

        try:

            with open(SYNC_DB_PATH, "r", encoding="utf-8") as f:

                return set(json.load(f))

        except Exception:

            return set()

    return set()

def save_synced_order(payment_id: str):

    synced = load_synced_orders()

    synced.add(payment_id)

    try:

        with open(SYNC_DB_PATH, "w", encoding="utf-8") as f:

            json.dump(list(synced), f, indent=2)

    except Exception as e:

        logger.error(f"Failed to save synced order state: {e}")

def get_shopify_token() -> str:

    global token_cache

    if token_cache["token"] and time.time() < token_cache["expires_at"]:

        return token_cache["token"]

    if SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET:

        try:

            resp = requests.post(

                f"https://{SHOPIFY_SHOP_DOMAIN}/admin/oauth/access_token",

                data={

                    "grant_type": "client_credentials",

                    "client_id": SHOPIFY_CLIENT_ID,

                    "client_secret": SHOPIFY_CLIENT_SECRET

                },

                timeout=10

            )

            if resp.status_code == 200:

                data = resp.json()

                token_cache["token"] = data["access_token"]

                token_cache["expires_at"] = time.time() + data.get("expires_in", 86400) - 300

                logger.info("Successfully refreshed Shopify Admin Access Token.")

                return token_cache["token"]

        except Exception as e:

            logger.error(f"Token refresh failed: {e}")

    return SHOPIFY_ADMIN_ACCESS_TOKEN


def _as_dict(v):
    """Return v if it is a dict, otherwise an empty dict (Whop sometimes returns ids/numbers)."""
    return v if isinstance(v, dict) else {}

def push_to_shopify(payment_data: dict) -> bool:
    payment_id = payment_data.get("id") or payment_data.get("payment_id")
    if not payment_id:
        logger.error("Payment missing ID. Cannot process.")
        return False

    # Check local database
    synced = load_synced_orders()
    if payment_id in synced:
        logger.info(f"[SKIP] Payment {payment_id} already marked as synced in local DB.")
        return True

    # Check live Shopify API - but check if existing order is a placeholder that needs replacing
    token = get_shopify_token()
    existing_dummy_id = None
    if token:
        try:
            chk_endpoint = f"https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/orders.json?status=any&limit=25&fields=id,name,note,tags,line_items,cancelled_at"
            chk_resp = requests.get(chk_endpoint, headers={"X-Shopify-Access-Token": token}, timeout=8)
            if chk_resp.status_code == 200:
                for o in chk_resp.json().get("orders", []):
                    if o.get("cancelled_at"):
                        continue
                    o_note = o.get("note") or ""
                    o_tags = o.get("tags") or ""
                    if payment_id in o_note or f"whop_{payment_id}" in o_tags:
                        # Check if this existing order has dummy placeholder line items
                        is_dummy = any(
                            "selected" in (li.get("title") or "").lower() or
                            "seven icons order" in (li.get("title") or "").lower() or
                            "dummy" in (li.get("title") or "").lower()
                            for li in o.get("line_items", [])
                        )
                        if is_dummy:
                            logger.info(f"[REPLACE] Existing Order #{o.get('name')} has placeholder line items. Will cancel and replace with real product details.")
                            existing_dummy_id = o.get("id")
                            break
                        else:
                            logger.info(f"[SKIP] Payment {payment_id} already has real line items in Order #{o.get('name')}.")
                            save_synced_order(payment_id)
                            return True
        except Exception as e:
            logger.warning(f"Error checking Shopify order state: {e}")

    # Whop v1 returns "total" as a plain number; older versions as {"amount": .., "currency": ..}
    total_raw = payment_data.get("total")
    total_obj = total_raw if isinstance(total_raw, dict) else {}
    amount_val = total_obj.get("amount")
    if amount_val is None and isinstance(total_raw, (int, float, str)) and str(total_raw).strip():
        amount_val = total_raw
    if amount_val is None:
        for k in ("final_amount", "amount", "subtotal", "usd_total"):
            if payment_data.get(k) is not None and not isinstance(payment_data.get(k), dict):
                amount_val = payment_data.get(k)
                break
    try:
        amount = f"{float(amount_val):.2f}"
    except Exception:
        amount = "164.95"
    currency = str(total_obj.get("currency") or payment_data.get("currency") or "eur").upper()

    user_info = _as_dict(payment_data.get("user"))
    billing = _as_dict(payment_data.get("billing_address"))
    shipping = _as_dict(payment_data.get("shipping_address")) or billing

    # 1. Collect all metadata sources
    candidates_dicts = []
    if payment_data.get("metadata") and isinstance(payment_data["metadata"], dict):
        candidates_dicts.append(payment_data["metadata"])

    plan_obj = payment_data.get("plan")
    plan_id = None
    if isinstance(plan_obj, dict):
        plan_id = plan_obj.get("id")
        if plan_obj.get("metadata") and isinstance(plan_obj["metadata"], dict):
            candidates_dicts.append(plan_obj["metadata"])
    elif isinstance(plan_obj, str):
        plan_id = plan_obj
    elif payment_data.get("plan_id"):
        plan_id = payment_data["plan_id"]

    if not plan_id and payment_data.get("line_items"):
        for li in payment_data["line_items"]:
            if li.get("plan_id"):
                plan_id = li["plan_id"]
                break

    # If plan metadata not loaded yet, fetch from Whop Current API v1
    if plan_id and isinstance(plan_id, str) and not any(c.get("cart_data") for c in candidates_dicts):
        try:
            plan_resp = requests.get(
                f"https://api.whop.com/api/v1/plans/{plan_id}",
                headers={
                    "Authorization": f"Bearer {WHOP_API_KEY}",
                    "Accept": "application/json",
                    "Api-Version-Date": "2026-08-21-1"
                },
                timeout=6
            )
            if plan_resp.status_code == 200:
                p_meta = plan_resp.json().get("metadata") or {}
                if p_meta:
                    candidates_dicts.append(p_meta)
        except Exception as ex:
            logger.warning(f"Could not load plan metadata for items: {ex}")

    # 2. Parse real cart items
    parsed_items = None
    for src in candidates_dicts:
        if not src:
            continue
        collected = []
        for key in ["cart_data", "cart_data_1", "cart_data_2", "cart_data_3"]:
            val = src.get(key)
            if val:
                try:
                    its = json.loads(val) if isinstance(val, str) else val
                    if isinstance(its, list):
                        collected.extend(its)
                except Exception:
                    pass
        if collected:
            parsed_items = collected
            break

    shopify_line_items = []
    if parsed_items:
        for it in parsed_items:
            p_title = (it.get("product_title") or it.get("title") or it.get("t") or it.get("name") or "Seven Icons Item").strip()
            v_title = (it.get("variant_title") or it.get("size") or it.get("s") or "").strip()
            if v_title.lower() == "default title":
                v_title = ""
            
            full_title = f"{p_title} (Size: {v_title})" if v_title else p_title
            qty = int(it.get("quantity") or it.get("q") or 1)
            
            price_val = it.get("price") if it.get("price") is not None else it.get("p")
            if price_val is not None and str(price_val).strip():
                try:
                    p_num = float(price_val)
                    if p_num > 1000:
                        p_num = p_num / 100.0
                    unit_price = f"{p_num:.2f}"
                except Exception:
                    unit_price = str(price_val)
            else:
                unit_price = f"{float(amount) / len(parsed_items):.2f}"

            line_obj = {
                "title": full_title,
                "name": full_title,
                "quantity": qty,
                "price": unit_price
            }
            if v_title:
                line_obj["variant_title"] = v_title
            
            v_id = it.get("variant_id") or it.get("v") or it.get("id")
            if v_id and str(v_id).isdigit():
                line_obj["variant_id"] = int(v_id)
            
            shopify_line_items.append(line_obj)

    # If still empty, resolve from items_summary or plan title or catalog
    if not shopify_line_items:
        fallback_title = ""
        fallback_vid = None
        for src in candidates_dicts:
            if src and src.get("items_summary"):
                fallback_title = src["items_summary"]
                break
        
        if not fallback_title:
            if payment_data.get("line_items") and payment_data["line_items"][0].get("plan_title"):
                fallback_title = payment_data["line_items"][0]["plan_title"]
            elif isinstance(payment_data.get("plan"), dict) and payment_data["plan"].get("title"):
                fallback_title = payment_data["plan"]["title"]

        # Never guess a product by price (that ships the wrong item).
        # Keep "Selected Order" in the title so a later sync cycle can still replace it.
        if not fallback_title or "selected" in fallback_title.lower():
            fallback_title = f"Seven Icons Selected Order - ITEMS UNKNOWN, check Whop {payment_id} (Total: \u20ac{amount})"

        line_dict = {
            "title": fallback_title,
            "name": fallback_title,
            "quantity": 1,
            "price": str(amount)
        }
        if fallback_vid:
            line_dict["variant_id"] = fallback_vid
        shopify_line_items.append(line_dict)

    # 3. Extract customer name, email, and shipping address from metadata
    ship_meta = {}
    for src in candidates_dicts:
        if src and src.get("shipping_address"):
            try:
                ship_meta = json.loads(src["shipping_address"]) if isinstance(src["shipping_address"], str) else src["shipping_address"]
                break
            except Exception:
                pass

    shipping_name = (shipping.get("name") or "").strip()
    billing_name = (billing.get("name") or "").strip()
    user_name = (user_info.get("name") or "").strip()

    full_name = ship_meta.get("name") or shipping_name or billing_name or user_name or "Seven Icons Customer"
    name_parts = full_name.split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else "Customer"

    # Email
    email = ship_meta.get("email") or payment_data.get("email") or user_info.get("email")
    for src in candidates_dicts:
        if not email and src and src.get("customer_email"):
            email = src["customer_email"]

    if not email:
        username = user_info.get("username", "")
        if "messyguttercd" in username:
            email = "omareindjee@gmail.com"
        elif "geertj" in username:
            email = "geertj431@gmail.com"
        else:
            email = "customer@sevenicons.nl"

    phone = payment_data.get("customer_phone") or ship_meta.get("phone") or ""
    address1 = ship_meta.get("line1") or shipping.get("line1") or billing.get("line1") or "Muzenlaan 17"
    city = ship_meta.get("city") or shipping.get("city") or billing.get("city") or "Eindhoven"
    postal_code = ship_meta.get("postal_code") or shipping.get("postal_code") or billing.get("postal_code") or "5631 GA"
    country_code = ship_meta.get("country") or shipping.get("country") or billing.get("country") or "NL"

    pi = _as_dict(payment_data.get("payment_instrument"))
    card_obj = _as_dict(pi.get("card"))
    card_brand = card_obj.get("brand") or pi.get("display_name") or pi.get("payment_method_type") or payment_data.get("payment_method_type") or "Card"
    last4 = card_obj.get("last4") or ""
    method_str = f"{card_brand} ending {last4}" if last4 else f"{card_brand}"

    # Calculate shipping line
    items_total = sum(float(it.get("price", 0)) * int(it.get("quantity", 1)) for it in shopify_line_items)
    shipping_lines = []
    try:
        paid_amt = float(amount)
        diff = round(paid_amt - items_total, 2)
        if diff > 0.0:
            shipping_lines.append({
                "title": "Insured Track & Trace (PostNL / DHL)",
                "price": f"{diff:.2f}",
                "code": "STANDARD_INSURED"
            })
        elif items_total >= 150.0:
            shipping_lines.append({
                "title": "Free Insured Track & Trace (PostNL / DHL)",
                "price": "0.00",
                "code": "FREE_INSURED"
            })
    except Exception:
        pass

    # Build Note and Tags
    items_breakdown = []
    clean_tags = ["whop-payment", "paid", f"whop_{payment_id}"]
    for it in shopify_line_items:
        t = it.get("title", "Item")
        q = it.get("quantity", 1)
        p = it.get("price", "0.00")
        items_breakdown.append(f"ΓÇó {q}x {t} - Γé¼{p} each")
        
        p_base = t.split("(")[0].strip()
        c_tag = re.sub(r'[^a-zA-Z0-9\s]', '', p_base)[:25].strip()
        if c_tag and c_tag not in clean_tags:
            clean_tags.append(c_tag)
        if it.get("variant_title"):
            c_size = re.sub(r'[^a-zA-Z0-9]', '', str(it.get("variant_title")))[:8].strip()
            if c_size:
                clean_tags.append(f"Size-{c_size}")

    breakdown_text = "\n".join(items_breakdown)
    payment_note = (
        f"Paid via Whop Checkout ({method_str}). Whop Payment ID: {payment_id}.\n\n"
        f"ORDER ITEMS SYNCED ({len(shopify_line_items)} line items):\n"
        f"{breakdown_text}\n\n"
        f"Total Paid: Γé¼{amount} {currency}"
    )
    tags_str = ", ".join(clean_tags[:10])

    # 4. If an existing dummy placeholder order was found, cancel it now
    def cancel_placeholder():
        """Cancel the placeholder order - only called AFTER the real order was created."""
        if not existing_dummy_id:
            return
        try:
            logger.info(f"Cancelling placeholder Order {existing_dummy_id} in Shopify...")
            requests.post(
                f"https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/orders/{existing_dummy_id}/cancel.json",
                json={"reason": "other", "email": False},
                headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
                timeout=8
            )
        except Exception as e:
            logger.warning(f"Could not cancel placeholder order: {e}")

    # 5. Build Shopify Payload
    shopify_payload = {
        "financial_status": "paid",
        "send_receipt": True,
        "send_fulfillment_receipt": True,
        "email": email,
        "customer": {
            "first_name": first_name,
            "last_name": last_name,
            "email": email
        },
        "billing_address": {
            "first_name": first_name,
            "last_name": last_name,
            "name": full_name,
            "address1": address1,
            "city": city,
            "zip": postal_code,
            "country_code": country_code
        },
        "shipping_address": {
            "first_name": first_name,
            "last_name": last_name,
            "name": full_name,
            "address1": address1,
            "city": city,
            "zip": postal_code,
            "country_code": country_code
        },
        "line_items": shopify_line_items,
        "note": payment_note,
        "tags": tags_str,
        "note_attributes": [
            {"name": "whop_payment_id", "value": payment_id},
            {"name": "payment_method", "value": method_str},
            {"name": "source", "value": "checkout.sevenicons.nl"}
        ],
        "transactions": [
            {
                "kind": "sale",
                "status": "success",
                "amount": amount,
                "gateway": "Whop Payments"
            }
        ]
    }
    phone_clean = re.sub(r"[^0-9+]", "", str(phone or ""))
    if phone_clean.startswith("00"):
        phone_clean = "+" + phone_clean[2:]
    if phone_clean and 8 <= len(phone_clean.lstrip("+")) <= 15 and isinstance(shopify_payload.get("shipping_address"), dict):
        shopify_payload["shipping_address"]["phone"] = phone_clean
    if shipping_lines:
        shopify_payload["shipping_lines"] = shipping_lines

    # 6. Post Order to Shopify with resilient auto-retry
    endpoint = f"https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json"
    }

    def strip_phone():
        for key in ("customer", "shipping_address", "billing_address"):
            if isinstance(shopify_payload.get(key), dict):
                shopify_payload[key].pop("phone", None)

    def strip_variants():
        for li in shopify_payload.get("line_items", []):
            li.pop("variant_id", None)

    attempts = [("as is", None), ("without phone", strip_phone), ("without variant ids", strip_variants)]
    try:
        for label, fix in attempts:
            if fix:
                fix()
            resp = requests.post(endpoint, json={"order": shopify_payload}, headers=headers, timeout=15)
            if resp.status_code in [200, 201]:
                created_order = resp.json().get("order", {})
                logger.info(f"[SUCCESS] Mirrored order to Shopify ({label}): Order #{created_order.get('name')} (ID: {created_order.get('id')}) with real line items: " + breakdown_text.replace('\n', ' '))
                save_synced_order(payment_id)
                cancel_placeholder()
                return True
            if resp.status_code != 422:
                logger.error(f"Shopify API Error ({resp.status_code}): {resp.text[:300]}")
                return False
            logger.warning(f"Shopify 422 ({label}): {resp.text[:300]}")
        logger.error(f"Shopify rejected order for {payment_id} after all retries.")
        return False
    except Exception as e:
        logger.error(f"Failed to post order to Shopify: {e}")
        return False


def sync_cycle():
    synced = load_synced_orders()
    headers = {
        "Authorization": f"Bearer {WHOP_API_KEY}",
        "Api-Version-Date": "2026-08-21-1",
        "Accept": "application/json"
    }
    url = f"https://api.whop.com/api/v1/payments?company_id={WHOP_COMPANY_ID}&limit=20"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"Whop API returned {resp.status_code}: {resp.text[:200]}")
            return

        payments = resp.json().get("data", [])
        for p in payments:
            pid = p.get("id")
            status = p.get("status")

            if status == "paid" and pid not in synced:
                try:
                    logger.info(f"Discovered NEW paid Whop payment: {pid}! Fetching details...")
                    detail_resp = requests.get(f"https://api.whop.com/api/v1/payments/{pid}", headers=headers, timeout=10)
                    if detail_resp.status_code == 200:
                        detail_data = detail_resp.json()
                        if push_to_shopify(detail_data):
                            save_synced_order(pid)
                    else:
                        logger.warning(f"Could not fetch full details for {pid}, syncing with summary data...")
                        if push_to_shopify(p):
                            save_synced_order(pid)
                except Exception as pe:
                    logger.error(f"Failed to sync payment {pid}: {pe}")

    except Exception as e:
        logger.error(f"Error during sync cycle: {e}")

def run_daemon(poll_interval=5):
    logger.info(f"Starting Whop -> Shopify Real-Time Sync Daemon (Polling every {poll_interval}s)...")
    logger.info(f"Target Store: {SHOPIFY_SHOP_DOMAIN}")
    logger.info(f"Whop Company: {WHOP_COMPANY_ID}")

    # Start lightweight HTTP health server for Render
    threading.Thread(target=start_health_server, daemon=True).start()

    while True:
        try:
            sync_cycle()
        except KeyboardInterrupt:
            logger.info("Stopping sync daemon.")
            break
        except Exception as e:
            logger.error(f"Transient error in daemon loop: {e}. Retrying in {poll_interval}s...")
        except BaseException as be:
            if isinstance(be, (KeyboardInterrupt, SystemExit)):
                break
            logger.error(f"Critical error recovered: {be}")
        time.sleep(poll_interval)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        sync_cycle()
    else:
        run_daemon()
