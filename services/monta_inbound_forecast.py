# -*- coding: utf-8 -*-
import json, pytz, requests
from datetime import datetime, timedelta
from requests.auth import HTTPBasicAuth
import logging

from odoo import models, fields

_logger = logging.getLogger(__name__)

class MontaInboundForecastService(models.AbstractModel):
    _name = 'monta.inbound.forecast.service'
    _description = 'Create/Update Inbound Forecast in Monta'

    # ----- Config helpers -----
    def _conf(self):
        ICP = self.env['ir.config_parameter'].sudo()
        base = (ICP.get_param('monta.base_url') or 'https://api-v6.monta.nl').rstrip('/')
        user = ICP.get_param('monta.username') or ''
        pwd  = ICP.get_param('monta.password') or ''
        tz   = ICP.get_param('monta.warehouse_tz') or 'Europe/Amsterdam'
        wh_display = (ICP.get_param('monta.inbound_warehouse_display_name') or '').strip()
        return base, user, pwd, tz, wh_display

    def _supplier_code_for(self, partner):
        ICP = self.env['ir.config_parameter'].sudo()
        override = (ICP.get_param('monta.supplier_code_override') or '').strip()
        if override:
            return override
        x = (getattr(partner, 'x_monta_supplier_code', '') or '').strip()
        if x:
            return x
        # JSON map in ICP: {"FAIR CH":"FAIR-CH","SupplierRef":"CODE123"}
        try:
            mp = { (k or '').strip().upper(): (v or '').strip()
                   for k, v in json.loads(ICP.get_param('monta.supplier_code_map') or "{}").items() }
        except Exception:
            mp = {}
        name_u = (partner.display_name or partner.name or '').strip().upper()
        ref_u  = (partner.ref or '').strip().upper()
        if name_u in mp and mp[name_u]:
            return mp[name_u]
        if ref_u and ref_u in mp and mp[ref_u]:
            return mp[ref_u]
        # fallback to partner.ref or VAT, else global default
        for attr in ('ref', 'vat'):
            v = (getattr(partner, attr, '') or '').strip()
            if v:
                return v
        return (ICP.get_param('monta.default_supplier_code') or '').strip()

    def _warehouse_display_name_for(self, po):
        wh_name = (getattr(po.picking_type_id.warehouse_id, 'x_monta_inbound_warehouse_name', '') or '').strip()
        if wh_name:
            return wh_name
        _base, _u, _p, _tz, wh_icp = self._conf()
        return wh_icp  # may be empty; some tenants accept missing

    def _iso_with_tz(self, dt, tzname):
        tz = pytz.timezone(tzname)
        if not dt:
            dt = fields.Datetime.now()
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        else:
            dt = dt.astimezone(tz)
        s = dt.strftime('%Y-%m-%dT%H:%M:%S%z')
        return s[:-2] + ':' + s[-2:]

    # ----- HTTP -----
    def _http(self, po, method, url, payload=None, auth=None, headers=None, timeout=30):
        headers = headers or {"Accept": "application/json", "Content-Type": "application/json"}
        # DB log + server log
        try:
            po._create_monta_log(
                {'inbound_forecast': {'step': 'HTTP '+method, 'url': url, 'payload': payload or {}}},
                level='info', tag='Monta IF', console_summary=f"[Monta IF] {method} {url}"
            )
        except Exception:
            pass

        _logger.info("[Monta IF] %s %s", method, url)
        if payload:
            _logger.info("[Monta IF] payload: %s", json.dumps(payload)[:1500])

        r = requests.request(method=method, url=url, json=payload, auth=auth, headers=headers, timeout=timeout)
        try:
            body = r.json()
        except Exception:
            body = {'raw': (r.text or '')[:2000]}

        (_logger.info if r.ok else _logger.error)("[Monta IF] ← %s %s (%s)", method, url, r.status_code)

        try:
            po._create_monta_log(
                {'inbound_forecast': {'step': 'HTTP RESP', 'status': r.status_code, 'body': body}},
                level='info' if r.ok else 'error', tag='Monta IF', console_summary=f"[Monta IF] {r.status_code}"
            )
        except Exception:
            pass

        return r.status_code, body

    # ----- Builders -----
    def _collect_lines(self, po, line_dt_iso):
        rows = []
        for l in po.order_line:
            sku = (l.product_id.monta_sku or l.product_id.default_code or '').strip()
            if not sku:
                raise ValueError(f"Line '{l.display_name}' has no SKU (monta_sku/default_code).")
            q = int(l.product_qty or 0)
            if q > 0:
                rows.append({"Sku": sku, "Quantity": q, "DeliveryDate": line_dt_iso})
        if not rows:
            raise ValueError("PO has no positive-quantity lines.")
        return rows

    def _group_payload(self, po, tz):
        planned = po.date_planned or fields.Datetime.now()
        # Some tenants require future EDD
        if planned < fields.Datetime.now() - timedelta(minutes=1):
            planned = fields.Datetime.now() + timedelta(days=1, hours=1)
        edd   = self._iso_with_tz(planned, tz)
        wh_dn = self._warehouse_display_name_for(po)
        payload = {
            "Reference": po.name,
            "SupplierCode": self._supplier_code_for(po.partner_id),
            "ExpectedDeliveryDate": edd,
            "AllocateStockOnDelivery": True,
            "WarehouseDisplayName": wh_dn or None,
            "InboundForecasts": self._collect_lines(po, edd),
            "Comment": (po.origin or "")[:200],
        }
        return payload

    # ----- Public main -----
    def send_for_po(self, po):
        """
        Create (POST) inbound forecast group with lines.
        If duplicate reference exists, PUT header to sync it.
        """
        base, user, pwd, tz, _wh = self._conf()
        auth = HTTPBasicAuth(user, pwd)

        if po.state not in ('purchase', 'done'):
            _logger.info("[Monta IF] Skip PO %s (state=%s)", po.name, po.state)
            return False

        header = self._group_payload(po, tz)
        if not header.get('SupplierCode'):
            raise ValueError("SupplierCode is required. Set partner.x_monta_supplier_code or ICP maps.")

        # POST group with lines
        url_post = f"{base}/inboundforecast/group"
        st, body = self._http(po, "POST", url_post, header, auth=auth)

        # Success
        if st in (200, 201):
            uid = body.get("UniqueId")
            if uid:
                try:
                    po.write({'x_monta_inboundforecast_uid': uid})
                except Exception:
                    pass
            _logger.info("[Monta IF] ✅ Created group for %s", po.name)
            return True

        # Duplicate / already exists → try syncing header (without lines in payload)
        body_txt = json.dumps(body).lower()
        if any(k in body_txt for k in ("already", "exist")) or st == 409:
            hdr = header.copy()
            hdr.pop("InboundForecasts", None)
            url_put = f"{base}/inboundforecast/group/{po.name}"
            st2, body2 = self._http(po, "PUT", url_put, hdr, auth=auth)
            if st2 in (200, 201):
                _logger.info("[Monta IF] ✅ Header synced for %s (group existed)", po.name)
                return True

        # Error path
        raise RuntimeError(f"Create inbound forecast failed for PO {po.name}: HTTP {st} {body}")
