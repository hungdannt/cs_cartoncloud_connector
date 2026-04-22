import logging

from odoo import fields, models, api
from ..cartoncloud_client import CartonCloudClient

_logger = logging.getLogger(__name__)


class StockQuant(models.Model):
    _inherit = "stock.quant"

    last_cartoncloud_sync_date = fields.Datetime(string="Last CartonCloud Sync Date", readonly=True)
    cartoncloud_product_uuid = fields.Char(string="CartonCloud Product UUID", readonly=True)

    @api.model
    def cron_cartoncloud_sync_inventory(self):
        """
        Cron job to sync inventory from CartonCloud to Odoo.
        Uses CartonCloud Report API (STOCK_ON_HAND) to fetch inventory levels.
        """
        # Only apply for TOA International Pty company
        toa_company = self.env['res.company'].search([('name', '=', 'TOA International Pty Ltd')], limit=1)
            
        # Get all active tenants for TOA company
        tenants = self.env['cartoncloud.tenant'].search([
            ('company_id', '=', toa_company.id),
            ('active', '=', True)
        ])
        
        if not tenants:
            _logger.warning("CartonCloud: No tenants configured for inventory sync")
            return
            
        for tenant in tenants:
            try:
                _logger.info(f"CartonCloud: Syncing inventory for tenant {tenant.name}")
                client = CartonCloudClient(self.env, tenant)
                customer_uuid = client._get_customer_uuid()
                warehouse_uuid = client._get_warehouse_uuid()
                
                report_params = {
                    "pageSize": 200000,
                }
                
                if customer_uuid:
                    report_params["customer"] = {"id": customer_uuid}

                if warehouse_uuid:
                    report_params["warehouse"] = {"id": warehouse_uuid}

                report_payload = {
                    "type": "STOCK_ON_HAND",
                    "parameters": report_params
                }
                
                report_response = client.request(
                    "POST",
                    client.tenant_path("/report-runs"),
                    json=report_payload,
                    headers={"Content-Type": "application/json", "Accept-Version": "1"},
                )
                
                if not isinstance(report_response, dict):
                    _logger.error(f"CartonCloud: Expected dict from report API, got {type(report_response)}")
                    continue
                
                self._process_cartoncloud_inventory_report(report_response, tenant.name)
                
            except Exception as e:
                _logger.exception(f"CartonCloud: Failed to sync inventory for tenant {tenant.name}: {str(e)}")
                continue

    def _process_cartoncloud_inventory_report(self, report_response: dict, tenant_name: str):
        """Process inventory report from CartonCloud"""
        if not isinstance(report_response, dict) or not report_response.get("id"):
            _logger.error(f"CartonCloud: Failed to create report run for tenant {tenant_name}")
            return
        
        report_run_id = report_response["id"]
        
        # Get the tenant that was used for this report
        tenant = self.env['cartoncloud.tenant'].search([('name', '=', tenant_name)], limit=1)
        if not tenant:
            _logger.error(f"CartonCloud: Could not find tenant {tenant_name}")
            return
            
        client = CartonCloudClient(self.env, tenant)
        
        import time
        time.sleep(2)
        
        report_data = client.request(
            "GET",
            client.tenant_path(f"/report-runs/{report_run_id}"),
            headers={"Accept-Version": "1"},
        )
        
        if not isinstance(report_data, dict):
            _logger.error(f"CartonCloud: Expected dict for report data, got {type(report_data)}")
            return
        
        rows = report_data.get("items", [])
        if not rows:
            _logger.info(f"CartonCloud: No inventory data in report for tenant {tenant_name}")
            return
        
        for row in rows:
            self._process_cartoncloud_inventory_item(row, tenant)

    def _process_cartoncloud_inventory_item(self, row: dict, tenant):
        """Process a single inventory row from CartonCloud STOCK_ON_HAND report"""
        if not isinstance(row, dict):
            return

        details = row.get("details", {})
        if not isinstance(details, dict):
            return

        product_data = details.get("product", {})
        if not isinstance(product_data, dict):
            return

        product_refs = product_data.get("references", {})
        product_code = product_refs.get("code") if isinstance(product_refs, dict) else None
        
        measures = row.get("measures", {})
        quantity = measures.get("quantity", 0) if isinstance(measures, dict) else 0

        product = self.env["product.product"].browse()

        if product_code:
            product = self.env["product.product"].search(
                [("default_code", "=", product_code)], limit=1
            )

        warehouse = tenant.warehouse_id
        if not warehouse:
            _logger.warning(f"CartonCloud: No warehouse configured for tenant {tenant.name}")
            return False

        location = warehouse.lot_stock_id

        quants = self.search([
            ("product_id", "=", product.id),
            ("location_id", "=", location.id),
        ])

        if not quants:
            return

        main_quant = quants.sorted(lambda q: q.quantity, reverse=True)[0]
        other_quants = quants - main_quant

        other_total = sum(other_quants.mapped("quantity"))
        new_main_qty = quantity - other_total

        if new_main_qty < 0:
            _logger.warning(
                f"CartonCloud qty {quantity} < existing other lots total {other_total} "
                f"for {product.default_code}"
            )
            return

        if main_quant.quantity != new_main_qty:
            old_qty = main_quant.quantity
            main_quant.sudo().write({
                "inventory_quantity": new_main_qty,
                "inventory_quantity_set": True,
            })
            main_quant.sudo().action_apply_inventory()
            _logger.info(
                f"CartonCloud: Updated inventory for {product.default_code} "
                f"from {old_qty} to {new_main_qty} in {warehouse.name}"
            )

            _logger.info(
                f"CartonCloud Sync: {product.default_code} "
                f"Lot {main_quant.lot_id.name if main_quant.lot_id else 'NO LOT'} "
                f"{old_qty} -> {new_main_qty}"
            )
