# odoo/addons/Monta-Odoo-Integration/models/monta_http.py
from odoo import models
import requests

class MontaHttp(models.AbstractModel):
    _name = "monta.http"
    _description = "Monta HTTP client"

    def get_json(self, path):
        base = self.env["ir.config_parameter"].sudo().get_param("monta.base_url") or "https://api.montaportal.nl"
        token = self.env["ir.config_parameter"].sudo().get_param("monta.token")
        try:
            resp = requests.get(f"{base.rstrip('/')}{path}", timeout=20, headers={"Authorization": f"Bearer {token}"} if token else {})
            resp.raise_for_status()
            return resp.json() or {}
        except Exception:
            return {}
