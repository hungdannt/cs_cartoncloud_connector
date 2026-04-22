from odoo import fields, models, api, _
import logging
import time
from typing import Optional

_logger = logging.getLogger(__name__)


class CartonCloudTenant(models.Model):
    _name = "cartoncloud.tenant"
    _description = "CartonCloud Tenant"
    _order = "name"

    name = fields.Char(string="Tenant Name", required=True)
    tenant_uuid = fields.Char(string="Tenant UUID", required=True, help="CartonCloud tenant UUID")
    gateway_host = fields.Char(string="Gateway Host", default="https://api.cartoncloud.com", required=True)
    api_version = fields.Char(string="API Version", default="1", help="CartonCloud API version header (Accept-Version)")
    
    # Authentication
    auth_client_id = fields.Char(string="Auth Client ID", required=True, help="OAuth2 clientId for /uaa/oauth/token")
    auth_client_secret = fields.Char(string="Auth Client Secret", required=True, help="OAuth2 clientSecret for /uaa/oauth/token")
    
    # Token caching
    access_token = fields.Char(string="Access Token", readonly=True)
    access_token_expires_at = fields.Float(string="Token Expires At", readonly=True)
    
    # Defaults
    default_customer_uuid = fields.Char(string="Default Customer UUID", help="Default customer UUID for this tenant")
    default_warehouse_uuid = fields.Char(string="Default Warehouse UUID", help="Default warehouse UUID for this tenant")
    
    # Configuration
    active = fields.Boolean(string="Active", default=True)
    partner_id = fields.Many2one("res.partner", string="Contact", required=True)
    company_id = fields.Many2one("res.company", string="Company", default=lambda self: self.env.company, required=True)
    warehouse_id = fields.Many2one("stock.warehouse", domain = "[('company_id', '=', company_id)]", string="Warehouse", help="Warehouse this tenant is associated with")

    
    @api.model
    def get_tenant_by_warehouse(self, warehouse_id):
        """Get tenant associated with a specific warehouse"""
        tenant = self.search([
            ('warehouse_id', '=', warehouse_id),
            ('active', '=', True)
        ], limit=1)
        
        return tenant
    
    @api.model
    def is_toa_company(self):
        """Check if current company is TOA International Pty Ltd"""
        return self.env.company.name == "TOA International Pty Ltd"

    def get_cached_token(self) -> Optional[str]:
        """Get cached access token if still valid"""
        self.ensure_one()
        if not self.access_token or not self.access_token_expires_at:
            return None
        
        if time.time() >= self.access_token_expires_at:
            return None
        
        return self.access_token

    def cache_token(self, access_token: str, expires_in: Optional[int] = None) -> None:
        """Cache access token with expiration"""
        self.ensure_one()
        self.write({
            'access_token': access_token,
            'access_token_expires_at': time.time() + max(int(expires_in or 300) - 30, 30)
        })

    def clear_token(self):
        """Clear cached token"""
        self.ensure_one()
        self.write({
            'access_token': False,
            'access_token_expires_at': False
        })
