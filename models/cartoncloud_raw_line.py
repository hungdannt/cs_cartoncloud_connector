import json
import logging

from odoo import fields, models

from .cartoncloud_client import CartonCloudClient


_logger = logging.getLogger(__name__)


class CartonCloudRawLine(models.Model):
    _name = "cs.cartoncloud.raw.line"
    _description = "CartonCloud Raw Line"

    name = fields.Char(string="Reference", store=True)
    raw_id = fields.Many2one("cs.cartoncloud.raw", string="Raw Id", ondelete="cascade")
    raw_data_text_id = fields.Many2one("cs.cartoncloud.raw.data.text", ondelete="set null", copy=False)
    raw_data = fields.Text(related="raw_data_text_id.raw_data")

    request_type = fields.Selection(
        [
            ("consignments_search", "Consignments Search"),
            ("consignments_get", "Consignments Get"),
            ("consignments_poll", "Consignments Poll"),
            ("outbound_orders_add", "Outbound Orders Add"),
            ("none", "None"),
        ],
        string="Operation Type",
        copy=False,
        index=True,
        default="none",
    )

    remark = fields.Char(string="Remarks")
    uuid = fields.Char(string="CartonCloud UUID", help="Consignment UUID for polling")
    picking_id = fields.Many2one("stock.picking", string="Picking", ondelete="set null")
    sale_order_id = fields.Many2one("sale.order", string="Sale Order", ondelete="set null")

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("progressing", "Progressing"),
            ("done", "Done"),
            ("cancel", "Cancelled"),
            ("warning", "Warning"),
            ("error", "Error"),
        ],
        string="Status",
        copy=False,
        index=True,
        default="draft",
    )

    def _get_payload(self) -> dict:
        if not self.raw_data:
            return {}
        try:
            payload = json.loads(self.raw_data)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _write_payload(self, payload: dict):
        RawText = self.env["cs.cartoncloud.raw.data.text"].sudo()
        raw_data_text = RawText.create({"raw_data": json.dumps(payload, default=str)})
        self.write({"raw_data_text_id": raw_data_text.id})

    def process_line(self):
        for line in self:
            if line.state not in ("draft", "progressing", "error"):
                continue

            if line.request_type == "consignments_poll":
                line._process_consignments_poll()
                continue

            if line.request_type != "outbound_orders_add":
                continue

            if not line.sale_order_id:
                line.write({"state": "error", "remark": "Missing sale_order_id"})
                continue

            order = line.sale_order_id
            if getattr(order, "cartoncloud_outbound_order_uuid", False):
                line.write({"state": "done", "remark": "Already synced"})
                continue

            payload = line._get_payload()
            if not payload:
                line.write({"state": "error", "remark": "Missing payload"})
                continue

            line.write({"state": "progressing"})

            # Get tenant from raw record or sale order
            tenant = line.raw_id.cartoncloud_tenant_id or order.cartoncloud_tenant_id
            client = CartonCloudClient(line.env, tenant)
            try:
                response = client.request(
                    "POST",
                    client.tenant_path("/outbound-orders"),
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            except Exception as e:
                merged = {"request": payload, "error": str(e)}
                line._write_payload(merged)
                line.write({"state": "error", "remark": str(e)})
                continue

            merged = {"request": payload, "response": response}
            line._write_payload(merged)

            if not isinstance(response, dict) or not response.get("id"):
                line.write({"state": "error", "remark": "CartonCloud response missing id"})
                order.write({"cartoncloud_outbound_last_message": "CartonCloud response missing id"})
                continue

            order.write(
                {
                    "cartoncloud_outbound_order_uuid": response.get("id"),
                    "cartoncloud_outbound_last_message": "Synced",
                }
            )
            line.write({"state": "done", "remark": "Synced"})

    def _process_consignments_poll(self):
        """Process consignments_poll: fetch consignment detail from CartonCloud and update picking."""
        self.ensure_one()

        if not self.picking_id:
            self.write({"state": "error", "remark": "Missing picking_id"})
            return

        picking = self.picking_id
        uuid = self.uuid or picking.cartoncloud_consignment_uuid
        if not uuid:
            self.write({"state": "error", "remark": "Missing consignment UUID"})
            return

        self.write({"state": "progressing"})

        # Get tenant from picking or raw record
        tenant = picking.cartoncloud_tenant_id or self.raw_id.cartoncloud_tenant_id
        client = CartonCloudClient(self.env, tenant)
        try:
            payload = client.request(
                "GET",
                client.tenant_path(f"/consignments/{uuid}"),
                headers={"Prefer": "return=no-items"},
            )
        except Exception as e:
            self._write_payload({"uuid": uuid, "error": str(e)})
            self.write({"state": "error", "remark": str(e)})
            picking.write({"cartoncloud_last_poll_message": str(e)})
            return

        self._write_payload({"uuid": uuid, "response": payload})

        if not isinstance(payload, dict):
            self.write({"state": "error", "remark": "Unexpected response type"})
            picking.write({"cartoncloud_last_poll_message": "Unexpected response type"})
            return

        picking._cartoncloud_apply_consignment_payload(payload)
        picking.write({"cartoncloud_last_poll_message": "OK"})
        self.write({"state": "done", "remark": "OK"})

    def unlink(self):
        self.raw_data_text_id.unlink()
        super().unlink()
