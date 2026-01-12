# -*- coding: utf-8 -*-
import logging

import requests
from requests.auth import HTTPBasicAuth

from odoo import models

_logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


class MontaHttp(models.AbstractModel):
    _name = "monta.http"
    _description = "HTTP client for Monta API (basic auth)"

    def _conf(self, company=None):
        company = company or self.env.company
        cfg = self.env["monta.config"].sudo().get_for_company(company)
        if not cfg:
            return None

        base = (cfg.base_url or "").rstrip("/")
        user = (cfg.username or "").strip()
        pwd = (cfg.password or "").strip()
        timeout = int(cfg.timeout or 5)
        return base, user, pwd, timeout

    def get_json(self, path, params=None, company=None):
        conf = self._conf(company=company)
        if not conf:
            _logger.warning("[Monta] Config missing or company not allowed.")
            return {}

        base, user, pwd, timeout = conf
        if not base:
            _logger.warning("[Monta] Base URL not configured.")
            return {}

        url = f"{base}/{(path or '').lstrip('/')}"
        try:
            auth = HTTPBasicAuth(user, pwd) if (user and pwd) else None
            resp = requests.get(
                url,
                params=params or {},
                timeout=timeout,
                auth=auth,
                headers=_DEFAULT_HEADERS,
            )
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except Exception as e:
            _logger.error("[Monta] GET %s failed: %s", url, e)
            return {}
