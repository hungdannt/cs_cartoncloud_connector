from odoo import fields, models


class CartonCloudRaw(models.Model):
    _name = "cs.cartoncloud.raw"
    _description = "CartonCloud Raw Data"

    name = fields.Char(string="Request")
    is_force_create = fields.Boolean(string="Force Create", default=False)
    raw_data_type = fields.Selection(
        [
            ("consignments_search", "Consignments Search"),
            ("consignments_get", "Consignments Get"),
            ("consignments_poll", "Consignments Poll"),
            ("outbound_orders_add", "Outbound Orders Add"),
        ],
        string="Type",
        copy=False,
        index=True,
        default="consignments_get",
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("process", "Processing"),
            ("done", "Done"),
            ("error", "Error"),
            ("cancel", "Cancelled"),
        ],
        string="Status",
        readonly=True,
        copy=False,
        index=True,
        default="draft",
    )
    remarks = fields.Char(string="Remarks")
    raw_data_line = fields.One2many("cs.cartoncloud.raw.line", "raw_id", string="Raw Lines")
    cartoncloud_tenant_id = fields.Many2one("cartoncloud.tenant", string="CartonCloud Tenant", readonly=True)

    def set_done(self):
        for item in self:
            item.write({"state": "done"})

    def set_error(self):
        for item in self:
            item.write({"state": "error"})

    def set_processing(self):
        for item in self:
            item.write({"state": "process"})

    def update_raw_data_status(self, item):
        all_not_done = item.raw_data_line.filtered(lambda l: l.state != "done")
        error_line = item.raw_data_line.filtered(lambda l: l.state == "error")
        cancel_line = item.raw_data_line.filtered(lambda l: l.state == "cancel")
        warning_line = item.raw_data_line.filtered(lambda l: l.state == "warning")

        if error_line:
            item.write({"state": "error"})
        elif cancel_line:
            item.write({"state": "cancel"})
        elif not all_not_done or (len(cancel_line) + len(warning_line) + len(item.raw_data_line.filtered(lambda l: l.state == "done")) == len(item.raw_data_line)):
            item.write({"state": "done"})

    def process_raw_data_queue(self):
        for raw in self:
            if not raw.raw_data_line:
                raw.set_done()
                continue
            raw.set_processing()
            # Process only draft/progressing/error lines (like Shopline)
            lines_to_process = raw.raw_data_line.filtered(lambda l: l.state in ("draft", "progressing", "error"))
            lines_to_process.process_line()
            raw.update_raw_data_status(raw)

    def cron_process_queue(self):
        """
        Process raw data queue in batches of lines (like Shopline pattern).
        Each cron run processes up to target_lines lines from raw records.
        """
        # Only apply for TOA International Pty company
        if not self.env['cartoncloud.tenant'].is_toa_company():
            return
            
        try:
            target_lines = int(
                self.env["ir.config_parameter"].sudo().get_param("cs_cartoncloud_connector.per_queue", default="50")
            )
        except ValueError:
            target_lines = 50

        Raw = self.env["cs.cartoncloud.raw"].sudo()

        # Set raw records with empty lines to done
        Raw.search([("state", "in", ("draft", "process")), ("raw_data_line", "=", False)]).set_done()

        # Get raw records that need processing
        raw_items = Raw.search_read(
            domain=[("state", "in", ("draft", "process"))],
            fields=["id", "raw_data_line"],
            order="id",
        )

        # Calculate how many raw records to process to reach target_lines
        raw_lines_tup = [(raw_item["id"], len(raw_item["raw_data_line"])) for raw_item in raw_items]
        to_process_raw_ids = []
        remaining_lines = target_lines
        
        for (raw_item_id, raw_item_line_count) in raw_lines_tup:
            to_process_raw_ids.append(raw_item_id)
            if raw_item_line_count >= remaining_lines:
                break
            remaining_lines -= raw_item_line_count

        # Process the selected batch
        for item in Raw.browse(to_process_raw_ids):
            try:
                item.process_raw_data_queue()
            except Exception:
                item.write({"state": "error"})
                continue
