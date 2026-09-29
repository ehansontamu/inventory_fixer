import os
import sys
import unittest
from unittest.mock import Mock, call, patch


os.environ.setdefault("FILTERED_JSON_URL", "https://example.test/filtered.json")
os.environ.setdefault("BC_STORE_ID", "store-id")
os.environ.setdefault("BC_AUTH_TOKEN", "token")
try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    sys.modules["requests"] = Mock()

import inventory_fixer


class InventoryFixerTests(unittest.TestCase):
    def test_discontinued_category_detection_accepts_numbers_and_strings(self):
        self.assertTrue(inventory_fixer.is_in_discontinued_category(
            {"BigCommerceCategoryIds": [26, "50"]}
        ))
        self.assertFalse(inventory_fixer.is_in_discontinued_category(
            {"BigCommerceCategoryIds": [26, 35]}
        ))

    @patch("inventory_fixer.requests.put")
    def test_disable_discontinued_product_updates_both_settings(self, put):
        put.return_value.raise_for_status = Mock()

        self.assertTrue(inventory_fixer.disable_discontinued_product(123))

        put.assert_called_once_with(
            "https://api.bigcommerce.com/stores/store-id/v3/catalog/products/123",
            headers=inventory_fixer.BC_HEADERS,
            json={"availability": "disabled", "inventory_tracking": "none"},
            timeout=inventory_fixer.HTTP_TIMEOUT,
        )

    @patch("inventory_fixer.send_absolute_adjustment")
    @patch("inventory_fixer.write_json_file")
    @patch("inventory_fixer.disable_discontinued_product", return_value=True)
    @patch("inventory_fixer.toggle_product_tracking")
    @patch("inventory_fixer.find_variant_and_product_by_sku", return_value=(10, 20))
    @patch("inventory_fixer.load_filtered_response")
    def test_discontinued_product_is_disabled_and_not_adjusted(
        self, load, find, toggle, disable, write_json, send_adjustment
    ):
        load.return_value = [{
            "sku": "DMP2726H",
            "Closeout": "Y",
            "BigCommerceCategoryIds": [49, 51],
            "Qty": 24,
            "bc_status9": 1,
            "bc_status7": 0,
            "quantityOnPurchaseOrder": 50,
        }]

        inventory_fixer.process_closeout_inventory()

        disable.assert_called_once_with(20)
        toggle.assert_not_called()
        send_adjustment.assert_not_called()
        self.assertEqual(find.call_count, 1)
        self.assertEqual(
            write_json.call_args_list[0],
            call(
                inventory_fixer.OUTPUT_ADJUST_JSON,
                {"reason": inventory_fixer.ADJUSTMENT_REASON, "items": []},
            ),
        )
        toggle_audit = write_json.call_args_list[1].args[1]
        self.assertEqual(toggle_audit[0]["mode"], "none")
        self.assertEqual(toggle_audit[0]["availability"], "disabled")
        self.assertTrue(toggle_audit[0]["toggled"])


if __name__ == "__main__":
    unittest.main()
