from dateutil.relativedelta import relativedelta
from psycopg2 import sql

from odoo import api, fields, models


class CartonCloudRawDataText(models.Model):
    _name = "cs.cartoncloud.raw.data.text"
    _description = "CartonCloud Raw Data Text"

    raw_data = fields.Text(string="Raw Data")

    def _auto_init(self):
        res = super()._auto_init()
        cr = self._cr

        cr.execute("SHOW server_version_num")
        pg_ver = int(cr.fetchone()[0])

        tbl = sql.Identifier(self._table)

        if pg_ver >= 140000:
            try:
                cr.execute(sql.SQL("ALTER TABLE {t} ALTER COLUMN raw_data SET COMPRESSION lz4").format(t=tbl))
            except Exception:
                cr.execute(sql.SQL("ALTER TABLE {t} ALTER COLUMN raw_data SET COMPRESSION pglz").format(t=tbl))
        else:
            cr.execute(sql.SQL("ALTER TABLE {t} ALTER COLUMN raw_data SET STORAGE EXTENDED").format(t=tbl))

        return res

    @api.model
    def cron_clean_raw_data(self):
        today = fields.Date.today()
        records = self.search([("create_date", "<", today - relativedelta(months=1))])
        records.unlink()
