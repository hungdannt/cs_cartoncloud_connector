import json
import logging

from odoo import _, fields, models
from odoo.exceptions import ValidationError, UserError
from werkzeug import urls
from odoo.addons.payment import utils as payment_utils

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    cartoncloud_tenant_id = fields.Many2one(
        "cartoncloud.tenant",
        string="CartonCloud Tenant",
        help="Select which CartonCloud tenant to use for this order",
        domain="[('active', '=', True), ('company_id', 'in', [company_id, False])]"
    )
    
    instruction = fields.Char(string="Instructions")
    cartoncloud_outbound_order_uuid = fields.Char(string="CartonCloud Outbound Order UUID", copy=False, readonly=True)
    cartoncloud_outbound_last_message = fields.Char(string="CartonCloud Outbound Last Message", copy=False, readonly=True)

    payment_link = fields.Char(
        string="Payment Link",
        copy=False,
        readonly=True,
    )

    def _cartoncloud_get_tenant(self):
        """Get the tenant for this order based on warehouse"""
        self.ensure_one()
        
        # Only apply for TOA International Pty company
        if not self.env['cartoncloud.tenant'].is_toa_company():
            return None
            
        if not self.cartoncloud_tenant_id:
            # Auto-select tenant based on warehouse
            if self.warehouse_id:
                tenant = self.env['cartoncloud.tenant'].get_tenant_by_warehouse(self.warehouse_id.id)
                if tenant:
                    self.cartoncloud_tenant_id = tenant
                else:
                    return None
        return self.cartoncloud_tenant_id

    def _get_email_from_by_currency(self):
        """Get sender email based on company currency (AU or HK)."""
        self.ensure_one()
        currency_code = self.company_id.currency_id.name if self.company_id.currency_id else ""
        
        if currency_code == "AUD":
            return "Auaccounts@hellotoa.com"
        elif currency_code == "HKD":
            return "Accounts@hellotoa.com"

    def _cartoncloud_get_default_customer_uuid(self) -> str:
        tenant = self._cartoncloud_get_tenant()
        if tenant and tenant.default_customer_uuid:
            return tenant.default_customer_uuid

    def _cartoncloud_get_default_warehouse_uuid(self) -> str:
        tenant = self._cartoncloud_get_tenant()
        if tenant and tenant.default_warehouse_uuid:
            return tenant.default_warehouse_uuid

    def _cartoncloud_build_outbound_order_payload(self) -> dict:
        customer_uuid = self._cartoncloud_get_default_customer_uuid()
        warehouse_uuid = self._cartoncloud_get_default_warehouse_uuid()

        ship_to = self.partner_shipping_id
        address = {
            "contactName": ship_to.name or "",
            "address1": ship_to.street or "",
        }
        if ship_to.zip:
            address["postcode"] = ship_to.zip
        if ship_to.phone or ship_to.mobile:
            address["phone"] = ship_to.phone or ship_to.mobile
        if ship_to.city:
            address["city"] = ship_to.city
        if ship_to.country_id and ship_to.country_id.code:
            address["country"] = {"iso2Code": ship_to.country_id.code}
        if ship_to.state_id and ship_to.state_id.code:
            address["state"] = {"code": ship_to.state_id.code}
        if ship_to.street2:
            address["address2"] = ship_to.street2
        if ship_to.email:
            address["email"] = ship_to.email
        deliver = {
            "address": address,
            "instructions": self.instruction or ""
        }

    
        items = []
        for line in self.order_line:
            if line.display_type:
                continue
            if not line.product_uom_qty:
                continue
            code = line.product_id.default_code if line.product_id else ""
            item = {
                "details": {
                    "product": {
                        "references": {
                            "code": code or "",
                        }
                    }
                },
                "measures": {
                    "quantity": float(line.product_uom_qty),
                },
            }
            items.append(item)

        payload = {
            "type": "OUTBOUND",
            "references": {
                "customer": self.name,
            },
            "customer": {"id": customer_uuid},
            "warehouse": {"id": warehouse_uuid},
            "details": {
                "deliver": deliver,
                "urgent": False,
            },
            "items": items,
        }
        return payload


    def _cartoncloud_push_outbound_immediate(self):
        """Push outbound order to CartonCloud immediately and link consignment UUID to picking"""
        from ..cartoncloud_client import CartonCloudClient
        
        self.ensure_one()
        
        if self.cartoncloud_outbound_order_uuid:
            _logger.info(f"CartonCloud: Order {self.name} already synced")
            return
        
        tenant = self._cartoncloud_get_tenant()
        if not tenant:
            raise ValidationError(_("No CartonCloud tenant configured for this order."))
        
        payload = self._cartoncloud_build_outbound_order_payload()
        client = CartonCloudClient(self.env, tenant)
        
        try:
            response = client.request(
                "POST",
                client.tenant_path("/outbound-orders"),
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        except Exception as e:
            self.cartoncloud_outbound_last_message = f"API Error: {str(e)}"
            raise
        
        if not isinstance(response, dict) or not response.get("id"):
            self.cartoncloud_outbound_last_message = "CartonCloud response missing id"
            raise ValidationError(_("CartonCloud response missing outbound order id"))
        
        outbound_order_uuid = response.get("id")
        
        self.cartoncloud_outbound_order_uuid = outbound_order_uuid
        self.cartoncloud_outbound_last_message = "Synced"
        
        # Search for consignment by SO reference
        consignment_uuid = None
        try:
            search_payload = {
                "condition": {
                    "type": "OrCondition",
                    "conditions": [
                        {
                            "type": "AndCondition",
                            "conditions": [
                                {
                                    "type": "TextComparisonCondition",
                                    "field": {
                                        "type": "ValueField",
                                        "value": "reference"
                                    },
                                    "value": {
                                        "type": "ValueField",
                                        "value": self.name
                                    },
                                    "method": "EQUAL_TO"
                                }
                            ]
                        }
                    ]
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
            
            if isinstance(consignment_response, list) and consignment_response:
                first_consignment = consignment_response[0]
                if isinstance(first_consignment, dict):
                    consignment_uuid = first_consignment.get("id")
                    _logger.info(f"CartonCloud: Found consignment {consignment_uuid} for SO {self.name}")
        except Exception as e:
            _logger.warning(f"CartonCloud: Failed to search consignment for SO {self.name}: {str(e)}")
        
        # Link consignment UUID to outgoing picking
        if consignment_uuid:
            outgoing_pickings = self.picking_ids.filtered(
                lambda p: p.picking_type_id.code == "outgoing" and p.state not in ("cancel", "done")
            )
            for picking in outgoing_pickings:
                picking.cartoncloud_consignment_uuid = consignment_uuid
                picking.cartoncloud_last_poll_message = "Linked from SO"
                _logger.info(f"CartonCloud: Linked consignment {consignment_uuid} to picking {picking.name}")
        
        _logger.info(f"CartonCloud: Pushed order {self.name}, outbound UUID: {outbound_order_uuid}, consignment UUID: {consignment_uuid}")

    def action_confirm(self):
        for order in self:
            if not order._cartoncloud_get_tenant():
                continue

            try:
                if not order.cartoncloud_outbound_order_uuid:
                    order._cartoncloud_push_outbound_immediate()
                    order.action_generate_payment_link()

                if not order.ordermentum_order_id:
                    order.send_order_confirmation()

            except Exception as e:
                order.cartoncloud_outbound_last_message = str(e)
                _logger.exception(
                    f"CartonCloud: Failed to push outbound order {order.name}: {e}"
                )
                raise UserError(f"Can't push order to CartonCloud: {e}")

        return super().action_confirm()

    def _prepare_invoice(self):
        """This method used set a flag if the invoice is created from CartonCloud order.
        """
        inv_val = super()._prepare_invoice()
        if self.cartoncloud_outbound_order_uuid and not self.ordermentum_order_id:
            inv_val.update(
                {
                    "cartoncloud_outbound_order_uuid": self.cartoncloud_outbound_order_uuid,
                }
            )
        return inv_val

    def action_generate_payment_link(self):
        for order in self:
            amount = order.amount_total
            currency = order.currency_id
            partner = order.partner_id
            company = order.company_id

            access_token = payment_utils.generate_access_token(
                partner.id,
                amount,
                currency.id,
            )

            base_url = order.get_base_url()

            params = {
                'amount': amount,
                'access_token': access_token,
                'currency_id': currency.id,
                'partner_id': partner.id,
                'company_id': company.id,
            }

            order.payment_link = f"{base_url}/payment/pay?{urls.url_encode(params)}"

    def send_order_confirmation(self):
        """Send order confirmation email."""
        template = self.env.ref(
            "cs_cartoncloud_connector.cartoncloud_order_confirmed", raise_if_not_found=False
        )
        if not template:
            return
        if not self.partner_id or not self.partner_id.email:
            return
        template.send_mail(self.id, force_send=True)