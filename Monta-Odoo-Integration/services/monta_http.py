#services/monta_http.py
# -*- coding: utf-8 -*-
import logging
import requests
from requests.auth import HTTPBasicAuth
from odoo import models

_logger = logging.getLogger(__name__)

class MontaHttp(models.AbstractModel):
    _name = "monta.http"
    _description = "HTTP client for Monta API (basic auth)"

    def _conf(self):
        ICP = self.env["ir.config_parameter"].sudo()
        base = (ICP.get_param("monta.base_url") or ICP.get_param("monta.api.base_url") or "").rstrip("/")
        user = (ICP.get_param("monta.username") or "").strip()
        pwd  = (ICP.get_param("monta.password") or "").strip()
        timeout = int(ICP.get_param("monta.timeout") or 5)
        return base, user, pwd, timeout

    def get_json(self, path: str, params=None):
        base, user, pwd, timeout = self._conf()
        if not base:
            _logger.warning("[Monta] Base URL not configured (monta.base_url or monta.api.base_url)")
            return {}
        url = f"{base}/{path.lstrip('/')}"
        try:
            auth = HTTPBasicAuth(user, pwd) if (user and pwd) else None
            resp = requests.get(url, params=params or {}, timeout=timeout, auth=auth, headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            })
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except Exception as e:
            _logger.error("[Monta] GET %s failed: %s", url, e)
            return {}