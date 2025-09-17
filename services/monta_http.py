import logging
import json
import requests

from odoo import models

_logger = logging.getLogger(__name__)

class MontaHttp(models.AbstractModel):
    _name = "monta.http"
    _description = "HTTP client for Monta API"

    def _base_url(self):
        ICP = self.env["ir.config_parameter"].sudo()
        return (ICP.get_param("monta.api.base_url") or "").rstrip("/")

    def _headers(self):
        ICP = self.env["ir.config_parameter"].sudo()
        token = ICP.get_param("monta.api.token") or ""
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def get_json(self, path: str):
        base = self._base_url()
        if not base:
            _logger.warning("[Monta] Base URL not configured (monta.api.base_url)")
            return {}
        url = f"{base}/{path.lstrip('/')}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=20)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except Exception as e:
            _logger.error("[Monta] GET %s failed: %s", url, e)
            return {}
