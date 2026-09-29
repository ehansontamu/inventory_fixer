#!/usr/bin/env python3
import os
import json
import requests

# ─── ENV HELPER ──────────────────────────────────────────────────────────────
def _env(name: str, default=None, required: bool = True):
    val = os.environ.get(name, default)
    if required and (val is None or str(val).strip() == ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

# ─── CONFIG (ALL SENSITIVE VALUES COME FROM ENV VARS) ────────────────────────
FILTERED_JSON_URL  = _env("FILTERED_JSON_URL")

SCRIPT_DIR         = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ADJUST_JSON = os.path.join(SCRIPT_DIR, "to_push_inventory.json")
OUTPUT_TOGGLE_JSON = os.path.join(SCRIPT_DIR, "to_toggle_tracking.json")

BC_BASE_URL        = _env("BC_BASE_URL", default="https://api.bigcommerce.com/stores", required=False)
BC_STORE_ID        = _env("BC_STORE_ID")
BC_AUTH_TOKEN      = _env("BC_AUTH_TOKEN")

BC_HEADERS         = {
    "X-Auth-Token": BC_AUTH_TOKEN,
    "Accept":       "application/json",
    "Content-Type": "application/json"
}

BC_LOCATION_ID     = int(_env("BC_LOCATION_ID", default="1", required=False))
ADJUSTMENT_REASON  = _env("ADJUSTMENT_REASON", default="Closeout inventory sync", required=False)
DISCONTINUED_CATEGORY_IDS = {49, 50, 51, 52}

HTTP_TIMEOUT       = (5, 30)  # (connect, read)

# ─── HELPERS ────────────────────────────────────────────────────────────────
def load_filtered_response():
    try:
        resp = requests.get(FILTERED_JSON_URL, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            print("[WARN] filteredResponse.json was not a JSON array.")
            return []
        return data
    except Exception as e:
        print(f"[ERROR] Failed to load filteredResponse.json: {e}")
        return []

def find_variant_and_product_by_sku(sku):
    url = f"{BC_BASE_URL}/{BC_STORE_ID}/v3/catalog/variants"
    try:
        resp = requests.get(url, headers=BC_HEADERS, params={"sku": sku}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if not data:
            return None, None
        variant = data[0]
        return variant.get("id"), variant.get("product_id")
    except Exception as e:
        print(f"   [ERROR] Variant lookup failed for SKU={sku}: {e}")
        return None, None

def get_variants_for_product(product_id):
    url = f"{BC_BASE_URL}/{BC_STORE_ID}/v3/catalog/variants"
    try:
        resp = requests.get(url, headers=BC_HEADERS, params={"product_id": product_id}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        print(f"   [ERROR] Failed to fetch variants for product_id={product_id}: {e}")
        return []

def toggle_product_tracking(product_id, mode):
    url = f"{BC_BASE_URL}/{BC_STORE_ID}/v3/catalog/products/{product_id}"
    payload = {"inventory_tracking": mode}
    try:
        resp = requests.put(url, headers=BC_HEADERS, json=payload, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"   [ERROR] Toggle tracking failed (product_id={product_id}, mode={mode}): {e}")
        return False

def disable_discontinued_product(product_id):
    url = f"{BC_BASE_URL}/{BC_STORE_ID}/v3/catalog/products/{product_id}"
    payload = {
        "availability": "disabled",
        "inventory_tracking": "none"
    }
    try:
        resp = requests.put(url, headers=BC_HEADERS, json=payload, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"   [ERROR] Failed to disable discontinued product_id={product_id}: {e}")
        return False

def is_in_discontinued_category(item):
    category_ids = item.get("BigCommerceCategoryIds", [])
    if not isinstance(category_ids, (list, tuple, set)):
        category_ids = [category_ids]

    for category_id in category_ids:
        try:
            if int(category_id) in DISCONTINUED_CATEGORY_IDS:
                return True
        except (TypeError, ValueError):
            continue
    return False

def write_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"[OK] Wrote {path}")
    except Exception as e:
        print(f"[ERROR] Failed to write {path}: {e}")

def send_absolute_adjustment(adjustment_items):
    url = f"{BC_BASE_URL}/{BC_STORE_ID}/v3/inventory/adjustments/absolute"
    body = {"reason": ADJUSTMENT_REASON, "items": adjustment_items}
    try:
        resp = requests.put(url, headers=BC_HEADERS, json=body, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"   [ERROR] Absolute adjustment failed: {e}")
        return False

def process_closeout_inventory():
    data = load_filtered_response()
    if not data:
        print("[WARN] No valid items. Writing empty inspection JSONs.")
        write_json_file(OUTPUT_ADJUST_JSON, {"reason": ADJUSTMENT_REASON, "items": []})
        write_json_file(OUTPUT_TOGGLE_JSON, [])
        return

    toggle_list = []
    adjustment_items = []
    product_variants_map = {}
    toggled_products = {}
    disabled_products = set()

    # Discontinued-category membership takes precedence over closeout inventory
    # updates. Disable these products first so another SKU for the same product
    # cannot turn tracking back on later in this run.
    for item in data:
        sku = (item.get("sku") or "").strip()
        if not sku or not is_in_discontinued_category(item):
            continue

        print(f"-> SKU={sku}: discontinued category -> disabling purchases and inventory tracking")

        variant_id, product_id = find_variant_and_product_by_sku(sku)
        if not variant_id or not product_id:
            toggle_list.append({
                "sku": sku,
                "product_id": None,
                "mode": "none",
                "availability": "disabled",
                "toggled": False,
                "reason": "not found"
            })
            continue

        if product_id in disabled_products:
            continue

        ok = disable_discontinued_product(product_id)
        disabled_products.add(product_id)
        toggle_list.append({
            "sku": sku,
            "product_id": product_id,
            "mode": "none",
            "availability": "disabled",
            "toggled": ok,
            "reason": None if ok else "disable failed"
        })

    for item in data:
        sku = (item.get("sku") or "").strip()
        if not sku:
            continue
        if is_in_discontinued_category(item):
            continue
        if str(item.get("Closeout", "")).upper() != "Y":
            continue

        try:
            qty = int(item.get("Qty", 0))
        except:
            qty = 0

        try:
            bc9 = int(item.get("bc_status9", 0))
        except:
            bc9 = 0

        try:
            bc7 = int(item.get("bc_status7", 0))
        except:
            bc7 = 0

        try:
            qty_on_po = int(item.get("quantityOnPurchaseOrder", 0))
        except:
            qty_on_po = 0

        final_qty = qty - bc9 - bc7 + qty_on_po
        if final_qty < 0:
            final_qty = 0

        print(
            f"-> SKU={sku}: Qty={qty}, bc_status9={bc9}, bc_status7={bc7}, "
            f"quantityOnPurchaseOrder={qty_on_po} → final={final_qty}"
        )

        variant_id, product_id = find_variant_and_product_by_sku(sku)
        if not variant_id or not product_id:
            toggle_list.append({
                "sku": sku,
                "product_id": None,
                "mode": None,
                "toggled": False,
                "reason": "not found"
            })
            continue

        if product_id in disabled_products:
            print(f"   [INFO] Skipping inventory adjustment for disabled product_id={product_id}.")
            continue

        if product_id not in product_variants_map:
            product_variants_map[product_id] = get_variants_for_product(product_id)

        is_multi = len(product_variants_map[product_id]) > 1

        if is_multi:
            desired_mode = "variant"
            if product_id not in toggled_products:
                ok = toggle_product_tracking(product_id, desired_mode)
                toggled_products[product_id] = desired_mode
                toggle_list.append({
                    "sku": sku,
                    "product_id": product_id,
                    "mode": desired_mode,
                    "toggled": ok,
                    "reason": None if ok else "toggle failed"
                })
            adjustment_items.append({
                "location_id": BC_LOCATION_ID,
                "variant_id": variant_id,
                "quantity": final_qty
            })
        else:
            desired_mode = "product"
            if product_id not in toggled_products:
                ok = toggle_product_tracking(product_id, desired_mode)
                toggled_products[product_id] = desired_mode
                toggle_list.append({
                    "sku": sku,
                    "product_id": product_id,
                    "mode": desired_mode,
                    "toggled": ok,
                    "reason": None if ok else "toggle failed"
                })
            adjustment_items.append({
                "location_id": BC_LOCATION_ID,
                "sku": sku,
                "quantity": final_qty
            })

    write_json_file(OUTPUT_ADJUST_JSON, {"reason": ADJUSTMENT_REASON, "items": adjustment_items})
    write_json_file(OUTPUT_TOGGLE_JSON, toggle_list)

    if adjustment_items:
        if send_absolute_adjustment(adjustment_items):
            print(f"[OK] Sent absolute adjustment for {len(adjustment_items)} item(s).")
        else:
            print("[ERROR] Failed to send absolute adjustment.")
    else:
        print("[INFO] Nothing to adjust this run.")

# ─── MAIN (SINGLE RUN) ───────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Inventory Fixer (single run) ===")
    process_closeout_inventory()
    print("=== Done ===")
