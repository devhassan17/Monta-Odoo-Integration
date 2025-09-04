# -*- coding: utf-8 -*-
import json
import logging
import pytz
import requests
from datetime import datetime
from requests.auth import HTTPBasicAuth

from odoo import models, fields

_logger = logging.getLogger(__name__)


class MontaInboundForecastService(models.AbstractModel):
    _name = 'monta.inbound.forecast.service'
    _description = 'Build & send Monta Inbound Forecasts (group + lines)'

    # --- config helpers ------------------------------------------------------
    def _conf(self):
        ICP = self.env['ir.config_parameter'].sudo()
        base = (ICP.get_param('monta.base_url') or 'https://api-v6.monta.nl').rstrip('/')
        user = ICP.get_param('monta.username') or ''
        pwd  = ICP.get_param('monta.password') or ''
        tz   = ICP.get_param('monta.warehouse_tz') or 'Europe/Amsterdam'
        dry  = (ICP.get_param('monta.dry_run_inbound') or '').strip() in ('1','true','True')
        return base, user, pwd, tz, dry

    def _iso_with_tz(self, dt: datetime, tzname: str) -> str:
        tz = pytz.timezone(tzname)
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        else:
            dt = dt.astimezone(tz)
        s = dt.strftime('%Y-%m-%dT%H:%M:%S%z')
        return s[:-2] + ':' + s[-2:]

    # --- payloads ------------------------------------------------------------
    def _collect_lines(self, po, line_delivery_dt_iso: str):
        rows = []
        for l in po.order_line:
            sku = (l.product_id.monta_sku or l.product_id.default_code or '').strip()
            if not sku:
                raise ValueError(f"Line '{l.display_name}' missing SKU (monta_sku/default_code).")
            qty = int(l.product_qty or 0)
            if qty > 0:
                rows.append({
                    "Sku": sku,
                    "Quantity": qty,
                    "DeliveryDate": line_delivery_dt_iso,  # tenant requires per-line DeliveryDate
                })
        if not rows:
            raise ValueError("No positive-quantity lines to send.")
        return rows

    def build_payload(self, po, supplier_code: str, warehouse_display_name: str, planned_dt: datetime):
        base, _user, _pwd, tz, _dry = self._conf()
        group_edd = self._iso_with_tz(planned_dt, tz)
        line_dd   = self._iso_with_tz(planned_dt, tz)

        payload = {
            "Reference": po.name,
            "SupplierCode": supplier_code,
            "ExpectedDeliveryDate": group_edd,
            "AllocateStockOnDelivery": True,
            "WarehouseDisplayName": warehouse_display_name,   # exact Monta display name
            "InboundForecasts": self._collect_lines(po, line_dd),
            "Comment": (po.origin or "")[:200],
        }
        url = f"{base}/inboundforecast/group"
        return payload, url

    # --- http ---------------------------------------------------------------
    def _http_json(self, method, url, payload=None, auth=None, headers=None, dry=False, timeout=30):
        headers = headers or {"Accept": "application/json", "Content-Type": "application/json"}
        if dry:
            _logger.info("[Monta Inbound] DRY-RUN %s %s payload=%s", method, url, (json.dumps(payload, default=str)[:800] if payload else ''))
            return 299, {"dry_run": True}

        try:
            resp = requests.request(method=method, url=url, json=payload, auth=auth, headers=headers, timeout=timeout)
            try:
                body = resp.json()
            except Exception:
                body = {"raw": (resp.text or "")[:2000]}
            (_logger.info if resp.ok else _logger.error)(
                "[Monta Inbound] %s %s -> %s | body_excerpt=%s",
                method, url, resp.status_code, str(body)[:800],
            )
            return resp.status_code, body
        except requests.RequestException as e:
            _logger.error("[Monta Inbound] HTTP error: %s %s | %s", method, url, e, exc_info=True)
            return 0, {"error": str(e)}

    # --- send ----------------------------------------------------------------
    def post_group_with_lines(self, po, payload, url=None):
        base, user, pwd, _tz, dry = self._conf()
        auth = HTTPBasicAuth(user, pwd)
        url = url or f"{base}/inboundforecast/group"

        # POST (create with lines)
        s, b = self._http_json("POST", url, payload, auth=auth, dry=dry)
        if s in (200, 201, 299):
            return s, b

        # If duplicate reference: PUT header (without lines)
        txt = json.dumps(b).lower()
        if "already" in txt or "exists" in txt:
            hdr = payload.copy()
            hdr.pop("InboundForecasts", None)
            put_url = f"{base}/inboundforecast/group/{po.name}"
            s2, b2 = self._http_json("PUT", put_url, hdr, auth=auth, dry=dry)
            return s2, b2

        return s, b
