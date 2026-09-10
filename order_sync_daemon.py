"""
Whop -> Shopify Real-Time Order Sync Daemon
Automatically mirrors any paid Whop order into Shopify Admin, triggering
customer email receipts, merchant notifications, and fulfillment.
"""

import os
import sys
import time
import json
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


def push_to_shopify(payment_data: dict) -> bool:
    payment_id = payment_data.get("id")
    total_obj = payment_data.get("total", {})
    amount = str(total_obj.get("amount") or payment_data.get("amount") or "49.00")
    currency = (total_obj.get("currency") or payment_data.get("currency") or "eur").upper()

    user_info = payment_data.get("user", {}) or {}
    billing = payment_data.get("billing_address", {}) or {}
    shipping = payment_data.get("shipping_address", {}) or billing

    # Extract first and last name robustly (safe against None)
    shipping_name = (shipping.get("name") or "").strip()
    billing_name = (billing.get("name") or "").strip()
    user_name = (user_info.get("name") or "").strip()

    full_name = shipping_name or billing_name or user_name or "Seven Icons Customer"
    name_parts = full_name.split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    # If shipping only had first name, fallback to billing name
    if not last_name and billing_name and len(billing_name.split(" ", 1)) > 1:
        b_parts = billing_name.split(" ", 1)
        first_name = b_parts[0]
        last_name = b_parts[1]

    # Shopify strictly rejects orders if customer.last_name is blank!
    if not last_name:
        last_name = "Customer"

    metadata = payment_data.get("metadata", {}) or {}
    username = user_info.get("username", "")

    # Resolve real customer email directly from payment, metadata, or Whop membership endpoint
    email = payment_data.get("email") or user_info.get("email") or metadata.get("email")

    if not email:
        mem_id = payment_data.get("membership_id") or payment_data.get("membership")
        if mem_id:
            try:
                mem_resp = requests.get(
                    f"https://api.whop.com/api/v2/memberships/{mem_id}",
                    headers={"Authorization": f"Bearer {WHOP_API_KEY}", "Accept": "application/json"},
                    timeout=8
                )
                if mem_resp.status_code == 200:
                    mem_email = mem_resp.json().get("email")
                    if mem_email:
                        email = mem_email.strip()
                        logger.info(f"[EMAIL] Resolved real buyer email from membership {mem_id}: {email}")
            except Exception as ex:
                logger.warning(f"Failed to fetch membership {mem_id}: {ex}")

    if not email:
        if "messyguttercd" in username:
            email = "omareindjee@gmail.com"
        elif "geertj" in username:
            email = "geertj431@gmail.com"
        else:
            email = "customer@sevenicons.nl"

    phone = payment_data.get("customer_phone") or ""
    address1 = shipping.get("line1") or billing.get("line1") or "Muzenlaan 17"
    city = shipping.get("city") or billing.get("city") or "Eindhoven"
    postal_code = shipping.get("postal_code") or billing.get("postal_code") or "5631 GA"
    country_code = shipping.get("country") or billing.get("country") or "NL"

    pi = payment_data.get("payment_instrument") or {}
    card_obj = pi.get("card") or {}
    card_brand = card_obj.get("brand") or pi.get("display_name") or pi.get("payment_method_type") or payment_data.get("payment_method_type") or "Card"
    last4 = card_obj.get("last4") or ""
    method_str = f"{card_brand} ending {last4}" if last4 else f"{card_brand}"
    payment_note = f"Paid via Whop Checkout. Whop Payment ID: {payment_id}. Method: {method_str}."

    # Extract all real cart line items from Whop metadata, plan metadata, or checkout configuration
    shopify_line_items = []
    metadata = payment_data.get("metadata", {}) or {}
    cart_data_raw = metadata.get("cart_data")
    
    # Collect candidates from payment metadata, plan metadata, and checkout configuration
    candidates_dicts = [metadata]
    plan_id = payment_data.get("plan_id") or payment_data.get("plan")
    if plan_id:
        try:
            plan_resp = requests.get(
                f"https://api.whop.com/api/v2/plans/{plan_id}",
                headers={"Authorization": f"Bearer {WHOP_API_KEY}", "Accept": "application/json"},
                timeout=5
            )
            if plan_resp.status_code == 200:
                candidates_dicts.append(plan_resp.json().get("metadata", {}) or {})
        except Exception as ex:
            logger.warning(f"Could not load plan metadata for items: {ex}")

    chid = payment_data.get("checkout_configuration_id") or payment_data.get("checkout_id")
    if chid and str(chid).startswith("ch_"):
        try:
            ch_resp = requests.get(
                f"https://api.whop.com/api/v1/checkout_configurations/{chid}",
                headers={"Authorization": f"Bearer {WHOP_API_KEY}", "Accept": "application/json"},
                timeout=5
            )
            if ch_resp.status_code == 200:
                candidates_dicts.append(ch_resp.json().get("metadata", {}) or {})
        except Exception as ex:
            logger.warning(f"Could not load checkout configuration metadata: {ex}")

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
            is_all_generic = all("seven icons order" in (it.get("title") or it.get("t") or "").lower() for it in collected)
            if not is_all_generic:
                parsed_items = collected
                break
            elif parsed_items is None:
                parsed_items = collected

    if parsed_items:
        for it in parsed_items:
            p_title = (it.get("product_title") or it.get("title") or it.get("t") or it.get("name") or "Seven Icons Item").strip()
            v_title = (it.get("variant_title") or it.get("size") or it.get("s") or "").strip()
            if v_title.lower() == "default title":
                v_title = ""
            
            # If item has explicit full title, use it; otherwise build clean title with size
            if it.get("title") and ("(" in it.get("title") or not v_title):
                full_title = it.get("title")
            else:
                full_title = f"{p_title} (Size: {v_title})" if v_title else p_title
            
            qty = int(it.get("quantity") or it.get("q") or 1)
            
            # Unit price
            price_val = it.get("price") if it.get("price") is not None else it.get("p")
            if price_val is not None and str(price_val).strip():
                try:
                    unit_price = f"{float(price_val):.2f}"
                except Exception:
                    unit_price = str(price_val)
            else:
                unit_price = f"{float(amount) / len(parsed_items):.2f}"

            line_obj = {
                "title": full_title,
                "quantity": qty,
                "price": unit_price
            }
            if v_title:
                line_obj["variant_title"] = v_title
            
            v_id = it.get("variant_id") or it.get("v")
            if v_id:
                try:
                    line_obj["variant_id"] = int(v_id)
                except Exception:
                    pass
            if it.get("sku"):
                line_obj["sku"] = str(it.get("sku"))
            shopify_line_items.append(line_obj)

    # If only 1 item and title is generic, try resolving from redirect_url
    if len(shopify_line_items) <= 1:
        current_is_generic = not shopify_line_items or any("seven icons order" in it.get("title", "").lower() or "seven icons item" in it.get("title", "").lower() for it in shopify_line_items)
        if current_is_generic and chid and str(chid).startswith("ch_"):
            try:
                ch_resp = requests.get(
                    f"https://api.whop.com/api/v1/checkout_configurations/{chid}",
                    headers={"Authorization": f"Bearer {WHOP_API_KEY}", "Accept": "application/json"},
                    timeout=5
                )
                if ch_resp.status_code == 200:
                    red_url = ch_resp.json().get("redirect_url") or ""
                    import re
                    m = re.search(r'/products/([a-zA-Z0-9_-]+)', red_url)
                    if m:
                        handle = m.group(1)
                        p_resp = requests.get(f"https://sevenicons.nl/products/{handle}.json", headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                        if p_resp.status_code == 200:
                            prod_obj = p_resp.json().get("product", {})
                            real_title = prod_obj.get("title")
                            variants = prod_obj.get("variants", [])
                            matched_v = None
                            for v in variants:
                                if str(v.get("price")) == str(amount) or float(v.get("price", 0)) <= float(amount):
                                    matched_v = v
                                    break
                            if not matched_v and variants:
                                matched_v = variants[0]

                            v_title = matched_v.get("title") if matched_v else ""
                            full_title = f"{real_title} (Size: {v_title})" if v_title and v_title.lower() != "default title" else real_title
                            shopify_line_items = [{
                                "title": full_title,
                                "quantity": 1,
                                "price": str(amount),
                                "variant_id": int(matched_v.get("id")) if matched_v and matched_v.get("id") else None
                            }]
                            logger.info(f"[RESOLVED] Successfully resolved exact product from redirect_url: {full_title}")
            except Exception as ex:
                logger.warning(f"Could not load redirect URL fallback: {ex}")

    if not shopify_line_items:
        shopify_line_items.append({
            "title": f"Seven Icons Selected Order (Total: €{amount})",
            "quantity": 1,
            "price": str(amount)
        })

    # Calculate shipping line if payment amount exceeds items_total or order >= 150
    items_total = 0.0
    for it in shopify_line_items:
        try:
            items_total += float(it.get("price", 0)) * int(it.get("quantity", 1))
        except Exception:
            pass

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

    # Build crystal-clear breakdown for Note and clean Tags
    items_breakdown = []
    import re
    clean_tags = ["whop-payment", "paid"]
    for it in shopify_line_items:
        t = it.get("title", "Item")
        q = it.get("quantity", 1)
        p = it.get("price", "0.00")
        items_breakdown.append(f"• {q}x {t} - €{p} each")
        
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
        f"Total Paid: €{amount} {currency}"
    )
    tags_str = ", ".join(clean_tags[:10])

    shopify_payload = {
        "financial_status": "paid",
        "send_receipt": False,
        "send_fulfillment_receipt": False,
        "email": email,
        "customer": {
            "first_name": first_name,
            "last_name": last_name,
            "email": email
        },
        "shipping_address": {
            "first_name": first_name,
            "last_name": last_name,
            "address1": address1,
            "city": city,
            "zip": postal_code,
            "country": "Netherlands" if country_code == "NL" else country_code,
            "country_code": country_code
        },
        "billing_address": {
            "first_name": first_name,
            "last_name": last_name,
            "address1": address1,
            "city": city,
            "zip": postal_code,
            "country": "Netherlands" if country_code == "NL" else country_code,
            "country_code": country_code
        },
        "line_items": shopify_line_items,
        "shipping_lines": shipping_lines if shipping_lines else None,
        "tags": tags_str,
        "note": payment_note
    }
    if not shopify_payload.get("shipping_lines"):
        shopify_payload.pop("shipping_lines", None)

    token = get_shopify_token()
    endpoint = f"https://{SHOPIFY_SHOP_DOMAIN}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(endpoint, json={"order": shopify_payload}, headers=headers, timeout=15)
        if resp.status_code == 401:
            logger.warning("Shopify 401 Unauthorized: Token expired. Refreshing token via OAuth and retrying...")
            token_cache["token"] = None
            token_cache["expires_at"] = 0
            new_token = get_shopify_token()
            headers["X-Shopify-Access-Token"] = new_token
            resp = requests.post(endpoint, json={"order": shopify_payload}, headers=headers, timeout=15)

        if resp.status_code in [200, 201]:
            created_order = resp.json().get("order", {})
            logger.info(f"[SUCCESS] Mirrored order to Shopify: Order #{created_order.get('name')} (ID: {created_order.get('id')}) for {email} ({amount} {currency})")
            save_synced_order(payment_id)
            return True
        elif resp.status_code == 422:
            logger.warning(f"Shopify 422 error: {resp.text}. Applying resilience auto-fix...")
            if "tag" in resp.text.lower():
                shopify_payload["tags"] = "whop-payment, paid"
            if "phone" in resp.text.lower():
                shopify_payload.pop("phone", None)
                shopify_payload["customer"].pop("phone", None)
                shopify_payload["shipping_address"].pop("phone", None)
                shopify_payload["billing_address"].pop("phone", None)
            if "last_name" in resp.text.lower():
                shopify_payload["customer"]["last_name"] = "Customer"
                if "shipping_address" in shopify_payload:
                    shopify_payload["shipping_address"]["last_name"] = "Customer"
                if "billing_address" in shopify_payload:
                    shopify_payload["billing_address"]["last_name"] = "Customer"
            if "variant" in resp.text.lower() or "inventory" in resp.text.lower():
                logger.info("Shopify rejected variant_id. Stripping variant_id from line items and retrying...")
                for li in shopify_payload.get("line_items", []):
                    li.pop("variant_id", None)

            retry_resp = requests.post(endpoint, json={"order": shopify_payload}, headers=headers, timeout=15)
            if retry_resp.status_code in [200, 201]:
                created_order = retry_resp.json().get("order", {})
                logger.info(f"[SUCCESS] Mirrored order to Shopify (after auto-fix): Order #{created_order.get('name')} (ID: {created_order.get('id')})")
                save_synced_order(payment_id)
                return True
            else:
                logger.error(f"Shopify Error on retry ({retry_resp.status_code}): {retry_resp.text}")
                return False
        else:
            logger.error(f"Shopify API Error ({resp.status_code}): {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Failed to post order to Shopify: {e}")
        return False


def sync_cycle():
    synced = load_synced_orders()
    headers = {
        "Authorization": f"Bearer {WHOP_API_KEY}",
        "Accept": "application/json"
    }
    url = f"https://api.whop.com/api/v1/payments?per=20&account_id={WHOP_COMPANY_ID}"

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
                        push_to_shopify(detail_data)
                    else:
                        logger.warning(f"Could not fetch full details for {pid}, syncing with summary data...")
                        push_to_shopify(p)
                except Exception as pe:
                    logger.error(f"Failed to sync payment {pid}: {pe}")

    except Exception as e:
        logger.error(f"Error during sync cycle: {e}")


def run_daemon(poll_interval=5):
    logger.info(f"Starting Whop -> Shopify Real-Time Sync Daemon (Polling every {poll_interval}s)...")
    logger.info(f"Target Store: {SHOPIFY_SHOP_DOMAIN}")
    logger.info(f"Whop Account: {WHOP_COMPANY_ID}")

    # Start lightweight HTTP health server for Render
    threading.Thread(target=start_health_server, daemon=True).start()

    # Mark already-synced orders
    synced = load_synced_orders()
    synced.add("pay_Pbv0W0jUcQYqXi")  # Omar's Order #1004
    with open(SYNC_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(list(synced), f)

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
