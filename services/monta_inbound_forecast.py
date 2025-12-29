# -*- coding: utf-8 -*-
import json
import pytz
import requests
import logging
from datetime import timedelta
from requests.auth import HTTPBasicAuth
from odoo import models, fields

_logger = logging.getLogger(__name__)


class MontaInboundForecastService(models.AbstractModel):
    _name = "monta.inbound.forecast.service"
    _description = "Create/Update Inbound Forecast in Monta (idempotent + line upserts)"

    def _conf(self, company=None):
        company = company or self.env.company
        cfg = self.env["monta.config"].sudo().get_for_company(company)
        if not cfg:
            return None
        base = (cfg.base_url or "https://api-v6.monta.nl").rstrip("/")
        user = (cfg.username or "").strip()
        pwd = (cfg.password or "").strip()
        tz = (cfg.warehouse_tz or "Europe/Amsterdam").strip()
        wh_display = (cfg.inbound_warehouse_display_name or "").strip()
        return cfg, base, user, pwd, tz, wh_display

    def _supplier_code_for(self, cfg, partner):
        override = (cfg.supplier_code_override or "").strip()
        if override:
            return override

        x = (getattr(partner, "x_monta_supplier_code", "") or "").strip()
        if x:
            return x

        try:
            mp = {(k or "").strip().upper(): (v or "").strip()
                  for k, v in json.loads(cfg.supplier_code_map or "{}").items()}
        except Exception:
            mp = {}

        name_u = (partner.display_name or partner.name or "").strip().upper()
        ref_u = (partner.ref or "").strip().upper()
        if name_u in mp and mp[name_u]:
            return mp[name_u]
        if ref_u and ref_u in mp and mp[ref_u]:
            return mp[ref_u]

        for attr in ("ref", "vat"):
            v = (getattr(partner, attr, "") or "").strip()
            if v:
                return v

        return (cfg.default_supplier_code or "").strip()

    def _warehouse_display_name_for(self, po, cfg):
        wh_name = (getattr(po.picking_type_id.warehouse_id, "x_monta_inbound_warehouse_name", "") or "").strip()
        if wh_name:
            return wh_name
        return (cfg.inbound_warehouse_display_name or "").strip() or None

    def _iso_with_tz(self, dt, tzname):
        tz = pytz.timezone(tzname)
        if not dt:
            dt = fields.Datetime.now()
        if dt.tzinfo is None:
            dt = tz.localize(dt)
        else:
            dt = dt.astimezone(tz)
        s = dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        return s[:-2] + ":" + s[-2:]

    def _http(self, po, method, url, payload=None, auth=None, headers=None, timeout=30):
        headers = headers or {"Accept": "application/json", "Content-Type": "application/json"}
        r = requests.request(method=method, url=url, json=payload, auth=auth, headers=headers, timeout=timeout)
        try:
            body = r.json()
        except Exception:
            body = {"raw": (r.text or "")[:2000]}
        return r.status_code, body

    def _collect_lines(self, po, line_dt_iso):
        from collections import defaultdict
        from ..utils.pack import expand_to_leaf_components, is_pack_like

        env = self.env
        company_id = getattr(po.company_id, "id", getattr(env.company, "id", False))
        rows_map = defaultdict(float)

        for l in po.order_line:
            product = l.product_id
            qty = float(l.product_qty or 0.0)
            if qty <= 0:
                continue

            sku = (getattr(product, "monta_sku", False) or getattr(product, "default_code", "") or "").strip()
            try_expand = is_pack_like(env, product, company_id) or (not sku)

            if try_expand:
                leaves = expand_to_leaf_components(env, company_id, product, qty) or []
                leaves = [(c, float(q or 0.0)) for (c, q) in leaves if q and float(q) > 0]
                if leaves:
                    for comp, q in leaves:
                        csku = (getattr(comp, "monta_sku", False) or getattr(comp, "default_code", "") or "").strip()
                        if not csku:
                            raise ValueError(f"Component of pack '{l.display_name}' has no SKU.")
                        rows_map[csku] += float(q or 0.0)
                    continue

            if not sku:
                raise ValueError(f"Line '{l.display_name}' has no SKU.")
            rows_map[sku] += qty

        rows = [{"Sku": sku, "Quantity": int(round(q)), "DeliveryDate": line_dt_iso}
                for sku, q in rows_map.items() if q > 0]
        if not rows:
            raise ValueError("PO has no positive-quantity component lines after pack expansion.")
        return rows

    def _group_payload(self, po, cfg, tz):
        planned = po.date_planned or fields.Datetime.now()
        if planned < fields.Datetime.now() - timedelta(minutes=1):
            planned = fields.Datetime.now() + timedelta(days=1, hours=1)
        edd = self._iso_with_tz(planned, tz)
        wh_dn = self._warehouse_display_name_for(po, cfg)
        payload = {
            "Reference": po.name,
            "SupplierCode": self._supplier_code_for(cfg, po.partner_id),
            "ExpectedDeliveryDate": edd,
            "AllocateStockOnDelivery": True,
            "WarehouseDisplayName": wh_dn,
            "Comment": (po.origin or "")[:200],
        }
        return payload, edd

    def send_for_po(self, po):
        conf = self._conf(company=po.company_id)
        if not conf:
            _logger.info("[Monta IF] Config missing or company not allowed — skipping PO %s", po.name)
            return False

        cfg, base, user, pwd, tz, _wh = conf
        if not cfg.inbound_enable:
            _logger.info("[Monta IF] Disabled in Monta Configuration — skipping PO %s", po.name)
            return False

        if po.state not in ("purchase", "done"):
            return False

        auth = HTTPBasicAuth(user, pwd)
        header, edd = self._group_payload(po, cfg, tz)

        url_get = f"{base}/inboundforecast/group/{po.name}"
        st, body = self._http(po, "GET", url_get, None, auth=auth)

        if st == 404:
            payload = header.copy()
            payload["InboundForecasts"] = self._collect_lines(po, edd)
            st2, body2 = self._http(po, "POST", f"{base}/inboundforecast/group", payload, auth=auth)
            return bool(200 <= (st2 or 0) < 300)

        if not (200 <= (st or 0) < 300):
            raise RuntimeError(f"GET group failed for {po.name}: HTTP {st} {body}")

        st3, body3 = self._http(po, "PUT", f"{base}/inboundforecast/group/{po.name}", header, auth=auth)
        if not (200 <= (st3 or 0) < 300):
            raise RuntimeError(f"PUT header failed for {po.name}: HTTP {st3} {body3}")

        return True

    def delete_for_po(self, po, note="Cancelled/Deleted from Odoo"):
        conf = self._conf(company=po.company_id)
        if not conf:
            return False
        cfg, base, user, pwd, _tz, _wh = conf
        if not cfg.inbound_enable:
            return False

        auth = HTTPBasicAuth(user, pwd)
        url = f"{base}/inboundforecast/group/{po.name}"
        headers = {"Content-Type": "application/json-patch+json", "Accept": "application/json"}
        payload = {"Note": note}
        st, _body = self._http(po, "DELETE", url, payload, auth=auth, headers=headers)
        return st in (200, 204)
