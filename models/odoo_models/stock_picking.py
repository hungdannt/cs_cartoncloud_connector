import logging
import json

from odoo import _, fields, models, api
from odoo.tools import html2plaintext
from odoo.exceptions import UserError

from ..cartoncloud_client import CartonCloudClient

_logger = logging.getLogger(__name__)



class StockPicking(models.Model):
    _inherit = "stock.picking"

    cartoncloud_consignment_uuid = fields.Char(string="CartonCloud Consignment UUID", copy=False)
    cartoncloud_consignment_status = fields.Char(string="CartonCloud Consignment Status", copy=False, readonly=True)
    cartoncloud_inbound_order_uuid = fields.Char(string="CartonCloud Inbound Order UUID", copy=False)
    cartoncloud_inbound_order_status = fields.Char(string="CartonCloud Inbound Order Status", copy=False, readonly=True)
    cartoncloud_last_poll_message = fields.Char(string="CartonCloud Last Poll Message", copy=False, readonly=True)
    cartoncloud_tenant_id = fields.Many2one("cartoncloud.tenant", string="CartonCloud Tenant", readonly=True)
    cartoncloud_internal_so_id = fields.Many2one("sale.order", string="Internal SO", copy=False, readonly=True)
    cartoncloud_internal_po_id = fields.Many2one("purchase.order", string="Internal PO", copy=False, readonly=True)


    def _cartoncloud_get_source_warehouse(self):
        self.ensure_one()
        if self.location_id and self.location_id.warehouse_id:
            return self.location_id.warehouse_id
        if self.picking_type_id and self.picking_type_id.warehouse_id:
            return self.picking_type_id.warehouse_id
        return False

    def _cartoncloud_get_destination_warehouse(self):
        self.ensure_one()
        if self.location_dest_id and self.location_dest_id.warehouse_id:
            return self.location_dest_id.warehouse_id
        return False

    def _cartoncloud_get_tenant_by_warehouse(self, warehouse):
        if not warehouse:
            return None
        return self.env["cartoncloud.tenant"].get_tenant_by_warehouse(warehouse.id)

    def _cartoncloud_get_instructions_text(self) -> str:
        self.ensure_one()
        return (html2plaintext(self.note) or "").strip()

    def _cartoncloud_build_inbound_order_payload_for_internal_transfer(self, tenant):
        self.ensure_one()

        warehouse_uuid = tenant.default_warehouse_uuid
        customer_uuid = tenant.default_customer_uuid

        items = []
        for move in self.move_ids_without_package:
            if move.state in ("done", "cancel"):
                continue
            if not move.product_uom_qty:
                continue
            code = move.product_id.default_code if move.product_id else ""
            items.append(
                {
                    "details": {
                        "product": {
                            "references": {
                                "code": code or "",
                            }
                        }
                    },
                    "measures": {
                        "quantity": float(move.product_uom_qty),
                    },
                }
            )

        instructions = self._cartoncloud_get_instructions_text()
        payload = {
            "type": "INBOUND",
            "status": "DRAFT",
            "references": {"customer": self.name},
            "details": {
                "instructions": instructions,
            },
            "items": items,
        }

        if customer_uuid:
            payload["customer"] = {"id": customer_uuid}
        if warehouse_uuid:
            payload["warehouse"] = {"id": warehouse_uuid}

        return payload

    def _cartoncloud_internal_create_sale_order(self, *, src_wh, dest_wh, tenant_src, tenant_dest):
        self.ensure_one()

        dest_partner = tenant_dest.partner_id
        if not dest_partner:
            raise UserError(_("Missing destination tenant partner."))

        so_lines = []
        for move in self.move_ids_without_package:
            if move.state in ("cancel",):
                continue
            if not move.product_uom_qty:
                continue
            so_lines.append(
                (
                    0,
                    0,
                    {
                        "product_id": move.product_id.id,
                        "product_uom_qty": move.product_uom_qty,
                        "product_uom": move.product_uom.id,
                        "name": move.product_id.display_name,
                        "price_unit": 0.0,
                    },
                )
            )

        so_vals = {
            "partner_id": dest_partner.id,
            "warehouse_id": src_wh.id,
            "origin": self.name,
            "client_order_ref": self.name,
            "company_id": self.company_id.id,
            "order_line": so_lines,
        }
        so = self.env["sale.order"].sudo().create(so_vals)
        so.state = "sale"
        self.sudo().write({"cartoncloud_internal_so_id": so.id})
        return so

    def _cartoncloud_internal_push_outbound_from_so(self, so, tenant_src):
        """Push SO to CartonCloud as outbound order and link consignment UUID back to this internal picking."""
        self.ensure_one()

        if self.cartoncloud_consignment_uuid:
            return self.cartoncloud_consignment_uuid

        if not tenant_src:
            raise UserError(_("Missing source tenant for outbound push."))

        client = CartonCloudClient(self.env, tenant_src)

        try:
            if not so.cartoncloud_outbound_order_uuid:
                payload = so._cartoncloud_build_outbound_order_payload()
                # Internal transfer reference should always be the picking name
                if isinstance(payload, dict):
                    payload.setdefault("references", {})
                    payload["references"]["customer"] = self.name
                response = client.request(
                    "POST",
                    client.tenant_path("/outbound-orders"),
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if isinstance(response, dict) and response.get("id"):
                    so.sudo().write(
                        {
                            "cartoncloud_outbound_order_uuid": response.get("id"),
                            "cartoncloud_outbound_last_message": "Synced",
                            "cartoncloud_tenant_id": tenant_src.id,
                        }
                    )
                else:
                    raise UserError(_("CartonCloud response missing outbound order id"))

            # Search for consignment by SO reference
            search_payload = {
                "condition": {
                    "type": "OrCondition",
                    "conditions": [
                        {
                            "type": "AndCondition",
                            "conditions": [
                                {
                                    "type": "TextComparisonCondition",
                                    "field": {"type": "ValueField", "value": "reference"},
                                    "value": {"type": "ValueField", "value": self.name},
                                    "method": "EQUAL_TO",
                                }
                            ],
                        }
                    ],
                }
            }

            import time

            time.sleep(3)
            consignment_response = client.request(
                "POST",
                client.tenant_path("/consignments/search"),
                json=search_payload,
                headers={"Content-Type": "application/json"},
            )

            consignment_uuid = False
            if isinstance(consignment_response, list) and consignment_response:
                first = consignment_response[0]
                if isinstance(first, dict):
                    consignment_uuid = first.get("id")

            if consignment_uuid:
                self.sudo().write(
                    {
                        "cartoncloud_consignment_uuid": consignment_uuid,
                        "cartoncloud_tenant_id": tenant_src.id,
                        "cartoncloud_last_poll_message": "Outbound synced",
                    }
                )
            return consignment_uuid
        except Exception as e:
            self.cartoncloud_last_poll_message = str(e)
            raise

    def _cartoncloud_internal_create_purchase_order(self, *, src_wh, dest_wh, tenant_src, tenant_dest):
        self.ensure_one()

        src_partner = tenant_src.partner_id
        if not src_partner:
            raise UserError(_("Missing source tenant partner."))

        po_lines = []
        for move in self.move_ids_without_package:
            if move.state in ("cancel",):
                continue
            if not move.product_uom_qty:
                continue
            po_lines.append(
                (
                    0,
                    0,
                    {
                        "product_id": move.product_id.id,
                        "product_qty": move.product_uom_qty,
                        "product_uom": move.product_uom.id,
                        "name": move.product_id.display_name,
                        "price_unit": 0.0,
                    },
                )
            )

        po_vals = {
            "partner_id": src_partner.id,
            "origin": self.name,
            "company_id": self.company_id.id,
            "order_line": po_lines,
        }
        in_type = getattr(dest_wh, "in_type_id", False)
        if in_type:
            po_vals["picking_type_id"] = in_type.id
        po = self.env["purchase.order"].sudo().create(po_vals)
        po.state = "purchase"
        self.sudo().write({"cartoncloud_internal_po_id": po.id})
        return po

    def _cartoncloud_internal_push_inbound_for_po(self, po, tenant_dest):
        self.ensure_one()

        if self.cartoncloud_inbound_order_uuid:
            return self.cartoncloud_inbound_order_uuid

        payload = self._cartoncloud_build_inbound_order_payload_for_internal_transfer(tenant_dest)
        client = CartonCloudClient(self.env, tenant_dest)

        try:
            response = client.request(
                "POST",
                client.tenant_path("/inbound-orders"),
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except Exception as e:
            self.cartoncloud_last_poll_message = str(e)
            raise

        if not isinstance(response, dict) or not response.get("id"):
            self.cartoncloud_last_poll_message = "CartonCloud response missing inbound order id"
            raise UserError(_("CartonCloud response missing inbound order id"))

        self.sudo().write(
            {
                "cartoncloud_inbound_order_uuid": response.get("id"),
                "cartoncloud_tenant_id": tenant_dest.id,
                "cartoncloud_last_poll_message": "Inbound synced",
            }
        )
        return response.get("id")

    def action_cartoncloud_send_internal_transfer(self):
        """Smart button: create internal SO then push outbound order to CartonCloud.

        Flow:
        - Create SO (if missing)
        - Push SO outbound to CartonCloud (destination delivery)
        - Link returned consignment UUID to this internal picking
        The inbound part happens only after consignment is delivered.
        """
        for picking in self:
            if picking.picking_type_id.code != "internal":
                picking.cartoncloud_last_poll_message = "Skipped (not internal transfer)"
                continue

            if picking.cartoncloud_consignment_uuid:
                picking.cartoncloud_last_poll_message = "Already synced (Outbound)"
                continue

            src_wh = picking._cartoncloud_get_source_warehouse()
            dest_wh = picking._cartoncloud_get_destination_warehouse()

            tenant_src = picking._cartoncloud_get_tenant_by_warehouse(src_wh)
            tenant_dest = picking._cartoncloud_get_tenant_by_warehouse(dest_wh)

            if not tenant_src or not tenant_dest:
                picking.cartoncloud_last_poll_message = "Skipped (missing tenant on source/destination warehouse)"
                continue

            so = picking.cartoncloud_internal_so_id
            if not so:
                try:
                    so = picking._cartoncloud_internal_create_sale_order(
                        src_wh=src_wh,
                        dest_wh=dest_wh,
                        tenant_src=tenant_src,
                        tenant_dest=tenant_dest,
                    )
                except Exception as e:
                    picking.cartoncloud_last_poll_message = str(e)
                    continue

            try:
                picking._cartoncloud_internal_push_outbound_from_so(so, tenant_src)
            except Exception as e:
                picking.cartoncloud_last_poll_message = str(e)
                continue


    def _cartoncloud_apply_inbound_order_payload(self, payload: dict):
        self.ensure_one()
        status = payload.get("status")

        if status:
            self.write({"cartoncloud_inbound_order_status": status})

        if status == "ALLOCATED" and self.state not in ("done", "cancel"):
            try:
                self.button_validate()
            except Exception:
                self.cartoncloud_last_poll_message = "Failed to validate internal transfer (Inbound)"


    def _cartoncloud_is_dispatched(self, status: str | None) -> bool:
        if not status:
            return False
        s = status.strip().upper()
        return s in {
            "COLLECTED",
            "IN_TRANSIT_PICK_DELIVERY",
            "IN_TRANSIT_WAREHOUSE_DELIVERY",
            "WITH_ON_FORWARDER",
        }

    def _cartoncloud_is_delivered(self, status: str | None) -> bool:
        if not status:
            return False
        s = status.strip().upper()
        return s in {"DELIVERED", "POINT_TO_POINT_DELIVERED"}

    def _cartoncloud_send_tracking_email(self):
        """Send tracking email when consignment is dispatched/in transit."""
        template = self.env.ref(
            "cs_cartoncloud_connector.mail_template_cartoncloud_tracking", raise_if_not_found=False
        )
        for picking in self:
            if not template:
                continue
            if not picking.partner_id or not picking.partner_id.email:
                continue
            if not picking.cartoncloud_consignment_uuid:
                continue
            template.send_mail(picking.id, force_send=True)

    def _cartoncloud_send_delivered_email(self):
        """Send delivered email when consignment is marked as delivered."""
        template = self.env.ref(
            "cs_cartoncloud_connector.mail_template_cartoncloud_delivered", raise_if_not_found=False
        )
        for picking in self:
            if not template:
                continue
            if not picking.partner_id or not picking.partner_id.email:
                continue
            if not picking.cartoncloud_consignment_uuid:
                continue
            template.send_mail(picking.id, force_send=True)

    def _cartoncloud_apply_consignment_payload(self, payload: dict):
        status = payload.get("status")

        vals = {}
        if status:
            vals["cartoncloud_consignment_status"] = status
        if vals:
            self.write(vals)

        if self._cartoncloud_is_dispatched(status) and self.state not in ("done", "cancel"):
            try:
                self._cartoncloud_send_tracking_email()
            except Exception:
                self.cartoncloud_last_poll_message = "Failed to send tracking email"
        
        if self._cartoncloud_is_delivered(status) and self.state not in ("done"):
            if self.picking_type_id and self.picking_type_id.code == "internal":
                # For internal transfers: delivered outbound triggers inbound order creation,
                # validation happens only when inbound order becomes ALLOCATED.
                try:
                    src_wh = self._cartoncloud_get_source_warehouse()
                    dest_wh = self._cartoncloud_get_destination_warehouse()
                    tenant_src = self._cartoncloud_get_tenant_by_warehouse(src_wh)
                    tenant_dest = self._cartoncloud_get_tenant_by_warehouse(dest_wh)

                    if not tenant_src or not tenant_dest:
                        self.cartoncloud_last_poll_message = "Skipped inbound creation (missing tenant on source/destination warehouse)"
                        return

                    po = self.cartoncloud_internal_po_id
                    if not po:
                        po = self._cartoncloud_internal_create_purchase_order(
                            src_wh=src_wh,
                            dest_wh=dest_wh,
                            tenant_src=tenant_src,
                            tenant_dest=tenant_dest,
                        )

                    self._cartoncloud_internal_push_inbound_for_po(po, tenant_dest)
                except Exception as e:
                    self.cartoncloud_last_poll_message = f"Failed to create/push inbound order: {str(e)}"
                return

            try:
                self.button_validate()
                self._cartoncloud_send_delivered_email()
            except Exception:
                self.cartoncloud_last_poll_message = "Failed to mark picking as done or send delivered email"

    @api.model
    def cron_cartoncloud_poll_consignments(self):
        """
        Cron job to poll consignment tracking from CartonCloud.
        Creates a single cs.cartoncloud.raw record with ALL pickings to poll.
        The actual API calls happen in cron_process_queue (batch processing).
        """
        # Only apply for TOA International Pty company
        toa_company =  self.env['res.company'].search([('name', '=', 'TOA International Pty Ltd')],limit=1)
        base_domain = [("state", "=", "assigned"), ("company_id", "=", toa_company.id)]

        pickings = self.env['stock.picking'].sudo().search(
            base_domain + [("cartoncloud_consignment_uuid", "!=", False)],
        )
        if not pickings:
            return

        Raw = self.env["cs.cartoncloud.raw"].sudo()

        raw_data_lines = []
        for picking in pickings:
            raw_data_lines.append((0, 0, {
                "name": picking.name,
                "uuid": picking.cartoncloud_consignment_uuid,
                "request_type": "consignments_poll",
                "picking_id": picking.id,
                "state": "draft",
            }))

        if raw_data_lines:
            # Group pickings by tenant to create separate raw records for each tenant
            pickings_by_tenant = {}
            for picking in pickings:
                tenant = picking.cartoncloud_tenant_id or picking.sale_id.cartoncloud_tenant_id
                if not tenant:
                    # Try to get tenant by warehouse
                    if picking.picking_type_id and picking.picking_type_id.warehouse_id:
                        tenant = self.env['cartoncloud.tenant'].get_tenant_by_warehouse(picking.picking_type_id.warehouse_id.id)
                
                if not tenant:
                    _logger.warning(f"CartonCloud: Skipping picking {picking.name} - no tenant configured")
                    continue
                    
                if tenant not in pickings_by_tenant:
                    pickings_by_tenant[tenant] = []
                pickings_by_tenant[tenant].append(picking)
            
            for tenant, tenant_pickings in pickings_by_tenant.items():
                tenant_raw_lines = []
                for picking in tenant_pickings:
                    tenant_raw_lines.append((0, 0, {
                        "name": picking.name,
                        "uuid": picking.cartoncloud_consignment_uuid,
                        "request_type": "consignments_poll",
                        "picking_id": picking.id,
                        "state": "draft",
                    }))
                
                Raw.create({
                    "name": f"Consignments Poll {tenant.name} {fields.Datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    "raw_data_type": "consignments_poll",
                    "cartoncloud_tenant_id": tenant.id,
                    "raw_data_line": tenant_raw_lines,
                    "state": "draft",
                })
                _logger.info(f"CartonCloud: Created poll queue for tenant {tenant.name} with {len(tenant_raw_lines)} pickings")

    @api.model
    def cron_cartoncloud_poll_inbound_orders(self):
        toa_company = self.env["res.company"].search([("name", "=", "TOA International Pty Ltd")], limit=1)
        base_domain = [
            ("state", "in", ("assigned", "confirmed")),
            ("company_id", "=", toa_company.id),
            ("picking_type_id.code", "=", "internal"),
            ("cartoncloud_inbound_order_uuid", "!=", False),
        ]

        pickings = self.env["stock.picking"].sudo().search(base_domain)
        if not pickings:
            return
        pickings_by_tenant = {}
        for picking in pickings:
            tenant = picking.cartoncloud_tenant_id
            pickings_by_tenant.setdefault(tenant, []).append(picking)

        for tenant, tenant_pickings in pickings_by_tenant.items():
            client = CartonCloudClient(self.env, tenant)
            for picking in tenant_pickings:
                uuid = picking.cartoncloud_inbound_order_uuid
                if not uuid:
                    continue
                try:
                    payload = client.request(
                        "GET",
                        client.tenant_path(f"/inbound-orders/{uuid}"),
                        headers={"Prefer": "return=no-items"},
                    )
                except Exception as e:
                    picking.cartoncloud_last_poll_message = str(e)
                    continue

                if not isinstance(payload, dict):
                    picking.cartoncloud_last_poll_message = "Unexpected inbound order response"
                    continue

                picking._cartoncloud_apply_inbound_order_payload(payload)
