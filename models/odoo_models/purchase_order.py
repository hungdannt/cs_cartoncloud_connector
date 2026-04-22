import logging
from odoo import _, fields, models, api
from odoo.exceptions import UserError
from odoo.tools import html2plaintext
from ..cartoncloud_client import CartonCloudClient

_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    cartoncloud_po_uuid = fields.Char(string="CartonCloud PO UUID", copy=False, readonly=True)
    cartoncloud_last_sync_date = fields.Datetime(string="Last Sync Date", copy=False, readonly=True)
    cartoncloud_sync_status = fields.Char(string="Sync Status", copy=False, readonly=True)

    cartoncloud_tenant_id = fields.Many2one(
        "cartoncloud.tenant",
        string="CartonCloud Tenant",
        help="Select which CartonCloud tenant to use for this purchase order",
        domain="[('active', '=', True), ('company_id', 'in', [company_id, False])]"
    )


    @api.model
    def cron_cartoncloud_sync_purchase_orders(self):
        """
        Cron job to sync purchase order inbound information from CartonCloud.
        Syncs received products information to Odoo.
        Uses CartonCloud's /inbound-orders/search endpoint (Purchase Orders).
        """
        # Only apply for TOA International Pty company
        toa_company = self.env['res.company'].search([('name', '=', 'TOA International Pty Ltd')], limit=1)
        tenants = self.env['cartoncloud.tenant'].search([('company_id', '=', toa_company.id), ('active', '=', True)])
        if not tenants:
            _logger.warning("CartonCloud: No tenants configured for PO sync")
            return
            
        for tenant in tenants:
            try:
                _logger.info(f"CartonCloud: Syncing POs for tenant {tenant.name}")
                client = CartonCloudClient(self.env, tenant)
                customer_uuid = client._get_customer_uuid()
                
                if not customer_uuid:
                    _logger.warning(f"CartonCloud: No customer UUID configured for tenant {tenant.name}")
                    continue

                # Search inbound orders by customer using search endpoint
                search_body = {
                    "condition": {
                        "type": "AndCondition",
                        "conditions": [
                            {
                                "type": "TextComparisonCondition",
                                "field": {
                                    "type": "JsonField",
                                    "pointer": "/customer/id"
                                },
                                "value": {
                                    "type": "ValueField",
                                    "value": customer_uuid
                                },
                                "method": "EQUAL_TO"
                            }
                        ]
                    }
                }

                response = client.request(
                    "POST",
                    client.tenant_path("/inbound-orders/search"),
                    json=search_body,
                    headers={"Accept-Version": "1"},
                )

                if not isinstance(response, list):
                    _logger.error(f"CartonCloud: Expected list of inbound orders, got {type(response)}")
                    continue

                for po_data in response:
                    self._process_cartoncloud_purchase_order(po_data)

            except Exception as e:
                _logger.exception(
                    f"CartonCloud: Failed to sync inbound orders for tenant {tenant.name}: {str(e)}"
                )
                continue

    @api.model
    def cron_cartoncloud_poll_inbound_orders_purchase(self):
        """Poll CartonCloud inbound order status for Purchase Orders by UUID.

        Flow:
        - For POs that already have cartoncloud_po_uuid (inbound order id in CartonCloud)
        - GET /inbound-orders/{uuid}
        - If status == ALLOCATED: auto validate related receipts (set qty_done then button_validate)
        """
        if not self.env["cartoncloud.tenant"].is_toa_company():
            return

        toa_company = self.env["res.company"].search(
            [("name", "=", "TOA International Pty Ltd")], limit=1
        )
        if not toa_company:
            return

        domain = [
            ("company_id", "=", toa_company.id),
            ("state", "in", ("purchase", "done")),
            ("cartoncloud_po_uuid", "!=", False),
        ]
        pos = self.env["purchase.order"].sudo().search(domain)
        if not pos:
            return

        pos_by_tenant = {}
        for po in pos:
            tenant = po._cartoncloud_get_tenant()
            if not tenant:
                continue
            pos_by_tenant.setdefault(tenant, []).append(po)

        for tenant, tenant_pos in pos_by_tenant.items():
            client = CartonCloudClient(self.env, tenant)
            for po in tenant_pos:
                uuid = po.cartoncloud_po_uuid
                if not uuid:
                    continue
                try:
                    payload = client.request(
                        "GET",
                        client.tenant_path(f"/inbound-orders/{uuid}"),
                        headers={"Prefer": "return=no-items"},
                    )
                except Exception as e:
                    po.sudo().write(
                        {
                            "cartoncloud_last_sync_date": fields.Datetime.now(),
                            "cartoncloud_sync_status": f"API Error: {str(e)}",
                        }
                    )
                    continue

                if not isinstance(payload, dict):
                    po.sudo().write(
                        {
                            "cartoncloud_last_sync_date": fields.Datetime.now(),
                            "cartoncloud_sync_status": "Unexpected inbound order response",
                        }
                    )
                    continue

                po._cartoncloud_apply_inbound_order_payload(payload)

    def _cartoncloud_apply_inbound_order_payload(self, payload: dict):
        self.ensure_one()

        status = payload.get("status")
        vals = {"cartoncloud_last_sync_date": fields.Datetime.now()}
        if status:
            vals["cartoncloud_sync_status"] = status
        self.sudo().write(vals)

        if status != "ALLOCATED":
            return

        for picking in self.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")):
            try:
                picking.button_validate()
            except Exception as e:
                self.sudo().write({"cartoncloud_sync_status": f"Validate Error: {str(e)}"})
                continue

    def _cartoncloud_get_tenant(self):
        """Get the tenant for this PO based on warehouse."""
        self.ensure_one()

        if not self.env["cartoncloud.tenant"].is_toa_company():
            return None

        if self.cartoncloud_tenant_id:
            return self.cartoncloud_tenant_id

        warehouse = False
        if getattr(self, "picking_type_id", False) and self.picking_type_id.warehouse_id:
            warehouse = self.picking_type_id.warehouse_id

        if warehouse:
            tenant = self.env["cartoncloud.tenant"].get_tenant_by_warehouse(warehouse.id)
            if tenant:
                self.cartoncloud_tenant_id = tenant
                return tenant

        return None

    def _cartoncloud_build_inbound_order_payload(self) -> dict:
        self.ensure_one()

        tenant = self._cartoncloud_get_tenant()
        if not tenant:
            raise UserError(_("No CartonCloud tenant configured for this purchase order."))

        client = CartonCloudClient(self.env, tenant)
        customer_uuid = client._get_customer_uuid()
        warehouse_uuid = client._get_warehouse_uuid()

        if not customer_uuid:
            raise UserError(_("Missing default customer UUID on CartonCloud tenant."))
        if not warehouse_uuid:
            raise UserError(_("Missing default warehouse UUID on CartonCloud tenant."))

        items = []
        for line in self.order_line:
            if line.display_type:
                continue
            if not line.product_qty:
                continue
            code = line.product_id.default_code if line.product_id else ""
            items.append(
                {
                    "details": {"product": {"references": {"code": code or ""}}},
                    "measures": {"quantity": float(line.product_qty)},
                }
            )

        if not items:
            raise UserError(_("No lines with quantity to push to CartonCloud."))

        notes_plain = (html2plaintext(self.notes or "") or "").strip()
        customer_reference = f"{notes_plain} | {self.name}" if notes_plain else self.name

        payload = {
            "type": "INBOUND",
            "status": "DRAFT",
            "references": {
                "customer": customer_reference,
            },
            "customer": {"id": customer_uuid},
            "warehouse": {"id": warehouse_uuid},
            "details": {
                "urgent": False,
            },
            "items": items,
        }
        return payload

    def action_cartoncloud_push_inbound_order(self):
        for po in self:
            if po.cartoncloud_po_uuid:
                raise UserError(_("This PO has already been pushed to CartonCloud."))

            tenant = po._cartoncloud_get_tenant()
            if not tenant:
                raise UserError(_("No CartonCloud tenant configured for this purchase order."))

            payload = po._cartoncloud_build_inbound_order_payload()
            client = CartonCloudClient(po.env, tenant)

            try:
                response = client.request(
                    "POST",
                    client.tenant_path("/inbound-orders"),
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            except Exception as e:
                po.cartoncloud_sync_status = f"API Error: {str(e)}"
                po.cartoncloud_last_sync_date = fields.Datetime.now()
                raise UserError(_("Can't push PO to CartonCloud: %s") % str(e))

            if not isinstance(response, dict) or not response.get("id"):
                po.cartoncloud_sync_status = "CartonCloud response missing id"
                raise UserError(_("CartonCloud response missing inbound order id"))

            po.cartoncloud_po_uuid = response.get("id")
            po.cartoncloud_last_sync_date = fields.Datetime.now()
            po.cartoncloud_sync_status = "Pushed"

        return True

    def _process_cartoncloud_purchase_order(self, po_data: dict):
        """Process a single purchase order from CartonCloud"""

        po_uuid = po_data.get("id")
        references = po_data.get("references", {})
        po_reference = references.get("customer") if isinstance(references, dict) else None
        
        if not po_uuid:
            return
        
        po = self.search([
            ("origin", "=", po_reference)
        ], limit=1)
        
        if not po:
            return
        
        po.cartoncloud_last_sync_date = fields.Datetime.now()
        
        self._sync_received_products(po, po_data)

    def _sync_received_products(self, po, po_data: dict):
        """
        Sync received products information to Odoo.
        Auto-validate receipts when Lot Number and Ordered Quantity exactly match.
        """
        from collections import defaultdict

        items = po_data.get("items", [])
        filtered_items = [
            i for i in items
            if i.get("measures", {}).get("quantity", 0) > 0
        ]

        item_qty_map = defaultdict(list)

        for i in filtered_items:
            code = i["details"]["product"]["references"]["code"]
            qty = i["measures"]["quantity"]
            item_qty_map[code].append(qty)

        for picking in po.picking_ids.filtered(lambda p: p.state not in ("done", "cancel")):
            can_validate = True

            for move in picking.move_ids:
                product_code = move.product_id.default_code
                move_qty = move.product_uom_qty

                if product_code not in item_qty_map:
                    can_validate = False
                    break

                if move_qty not in item_qty_map[product_code]:
                    can_validate = False
                    break

            if can_validate:
                picking.button_validate()
                po.action_create_invoice()
