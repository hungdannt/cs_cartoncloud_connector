import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

class AccountMove(models.Model):
    _inherit = "account.move"

    payment_link = fields.Char(
        string="Payment Link",
        store=True,
        compute='_compute_payment_link',    
    )
    cartoncloud_outbound_order_uuid = fields.Char(string="CartonCloud Outbound Order UUID", copy=False, readonly=True)

    @api.depends('invoice_origin')
    def _compute_payment_link(self):
        for move in self:
            move.payment_link = False
            if move.move_type == 'out_invoice' and move.invoice_origin:
                order = self.env['sale.order'].search([('name', '=', move.invoice_origin)], limit=1)
                if order:
                    move.payment_link = order.payment_link

    @api.depends('amount_residual', 'move_type', 'state', 'company_id')
    def _compute_payment_state(self):
        super()._compute_payment_state()
        for invoice in self:
            if invoice.move_type == 'out_invoice' and invoice.cartoncloud_outbound_order_uuid and invoice.payment_state == 'paid':
                template = self.env.ref(
                    'cs_cartoncloud_connector.mail_template_cartoncloud_payment_done',
                    raise_if_not_found=False
                )
                if template:
                    template.send_mail(invoice.id, force_send=True)

    def _get_email_from_by_currency(self):
        """Get sender email based on company currency (AU or HK)."""
        self.ensure_one()
        currency_code = self.company_id.currency_id.name if self.company_id.currency_id else ""
        
        if currency_code == "AUD":
            return "Auaccounts@hellotoa.com"
        elif currency_code == "HKD":
            return "Accounts@hellotoa.com"

    def _send_reminder_email(self, template_xmlid):
        self.ensure_one()

        template = self.env.ref(template_xmlid, raise_if_not_found=False)
        if not template:
            return

        template.send_mail(
            self.id,
            force_send=True,
            email_layout_xmlid='mail.mail_notification_light',
        )

    def _get_mail_template(self):
        if all(move.move_type == 'out_invoice' for move in self):
            return 'cs_cartoncloud_connector.email_template_edi_invoice_custom'
        return super()._get_mail_template()

    def _cron_send_ordermentum_invoice_reminders(self):
        today = fields.Date.today()

        invoices = self.search([
            ('cartoncloud_outbound_order_uuid', '!=', False),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('amount_residual', '>', 0),
            ('invoice_date_due', '!=', False),
            ('payment_state', '=', 'not_paid'),
        ])

        for inv in invoices:
            days_overdue = (today - inv.invoice_date_due).days

            if days_overdue == 0:
                inv._send_reminder_email('cs_cartoncloud_connector.mail_template_cartoncloud_invoice_due')

            elif days_overdue == 7:
                inv._send_reminder_email('cs_cartoncloud_connector.mail_template_cartoncloud_invoice_overdue_7')

            elif days_overdue == 14:
                inv._send_reminder_email('cs_cartoncloud_connector.mail_template_cartoncloud_invoice_overdue_14')

            elif days_overdue == 30:
                inv._send_reminder_email('cs_cartoncloud_connector.mail_template_cartoncloud_invoice_overdue_30')
