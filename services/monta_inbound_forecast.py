# -*- coding: utf-8 -*-
import json, pytz, requests, logging
from datetime import datetime, timedelta
from requests.auth import HTTPBasicAuth
from odoo import models, fields

_logger = logging.getLogger(__name__)

class MontaInboundForecastService(models.AbstractModel):
    _name = 'monta.inbound.forecast.service'
    _description = 'Create/Update/Delete Inbound Forecast in Monta (idempotent + line upserts)'

    # ---------- config ----------
    def _conf(self):
        ICP = self.env['ir.config_parameter'].sudo()
        base = (ICP.get_param('monta.base_url') or 'https://api-v6.monta.nl').rstrip('/')
        user = ICP.get_param('monta.username') or ''
        pwd  = ICP.get_param('monta.password') or ''
        tz   = ICP.get_param('monta.warehouse_tz') or 'Europe/Amsterdam'
        wh_display = (ICP.get_param('monta.inbound_warehouse_display_name') or '').strip()
        allowed = (ICP.get_param('monta.allowed_instance_url') or '').strip().rstrip('/') + '/' if ICP.get_param('monta.allowed_instance_url') else ''  # optional guard
        return base, user, pwd, tz, wh_display, allowed

    def _is_allowed_instance(self):
        """Guard to avoid pushing from staging by mistake.

        Configure System Parameter:
          - monta.allowed_instance_url = https://YOUR-PROD-URL/
        (Include trailing slash, or we’ll add it.)
        If unset -> allow all.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        allowed = (ICP.get_param('monta.allowed_instance_url') or '').strip().rstrip('/') + '/'
        if not allowed:
            return True
        web_url = (ICP.get_param('web.base.url') or '').strip().rstrip('/') + '/'
        ok = (web_url.lower() == allowed.lower())
        if not ok:
            _logger.warning("[Monta IF Guard] Blocked push. web.base.url=%s expected=%s", web_url, allowed)
        return ok

    def _supplier_code_for(self, partner):
        ICP = self.env['ir.config_parameter'].sudo()
        override = (ICP.get_param('monta.supplier_code_override') or '').strip()
        if override:
            return override
        x = (getattr(partner, 'x_monta_supplier_code', '') or '').strip()
        if x:
            return x
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
        for attr in ('ref', 'vat'):
            v = (getattr(partner, attr, '') or '').strip()
            if v:
                return v
        return (ICP.get_param('monta.default_supplier_code') or '').strip()

    def _warehouse_display_name_for(self, po):
        wh_name = (getattr(po.picking_type_id.warehouse_id, 'x_monta_inbound_warehouse_name', '') or '').strip()
        if wh_name:
            return wh_name
        _base, _u, _p, _tz, wh_icp, _allow = self._conf()
        return wh_icp or None

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

    # ---------- HTTP + logging ----------
    def _http(self, po, method, url, payload=None, auth=None, headers=None, timeout=30):
        headers = headers or {"Accept": "application/json", "Content-Type": "application/json"}
        try:
            po._create_monta_log({'IF': {'step': f'HTTP {method}', 'url': url, 'payload': payload or {}}},
                                 'info', 'Monta IF', console_summary=f"[Monta IF] {method} {url}")
        except Exception:
            pass

        _logger.info("[Monta IF] %s %s", method, url)
        if payload is not None:
            try:
                _logger.info("[Monta IF] payload: %s", json.dumps(payload)[:1500])
            except Exception:
                pass

        r = requests.request(method=method, url=url, json=payload, auth=auth, headers=headers, timeout=timeout)
        try:
            body = r.json()
        except Exception:
            body = {'raw': (r.text or '')[:2000]}

        (_logger.info if r.ok else _logger.error)("[Monta IF] ← %s %s (%s)", method, url, r.status_code)
        try:
            po._create_monta_log({'IF': {'step': 'HTTP RESP', 'status': r.status_code, 'body': body}},
                                 'info' if r.ok else 'error', 'Monta IF', console_summary=f"[Monta IF] {r.status_code}")
        except Exception:
            pass
        return r.status_code, body

    # ---------- builders ----------
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
        if planned < fields.Datetime.now() - timedelta(minutes=1):
            planned = fields.Datetime.now() + timedelta(days=1, hours=1)
        edd   = self._iso_with_tz(planned, tz)
        wh_dn = self._warehouse_display_name_for(po)
        payload = {
            "Reference": po.name,
            "SupplierCode": self._supplier_code_for(po.partner_id),
            "ExpectedDeliveryDate": edd,
            "AllocateStockOnDelivery": True,
            "WarehouseDisplayName": wh_dn,
            "Comment": (po.origin or "")[:200],
        }
        return payload, edd

    # ---------- helpers for idempotency ----------
    def _get_group(self, base, auth, po):
        url = f"{base}/inboundforecast/group/{po.name}"
        st, body = self._http(po, "GET", url, None, auth=auth)
        return st, body

    def _create_group_with_lines(self, base, auth, po, header, lines):
        payload = header.copy()
        payload["InboundForecasts"] = lines
        url_post = f"{base}/inboundforecast/group"
        st, body = self._http(po, "POST", url_post, payload, auth=auth)
        return st, body

    def _put_header(self, base, auth, po, header):
        url_put = f"{base}/inboundforecast/group/{po.name}"
        st, body = self._http(po, "PUT", url_put, header, auth=auth)
        return st, body

    def _delete_group(self, base, auth, po, note="Cancelled from Odoo"):
        """Best-effort delete. Treat 200/204/404 as success."""
        url = f"{base}/inboundforecast/group/{po.name}"
        headers = {"Accept": "application/json", "Content-Type": "application/json-patch+json"}
        st, body = self._http(po, "DELETE", url, {"Note": note}, auth=auth, headers=headers)
        if st in (200, 204, 404):
            _logger.info("[Monta IF] Deleted group (or already gone) for %s", po.name)
            return True
        _logger.error("[Monta IF] Delete failed for %s: %s %s", po.name, st, body)
        return False

    def _get_existing_skus(self, group_body):
        existing = set()
        try:
            for f in (group_body.get("InboundForecasts") or []):
                s = (f.get("Sku") or "").strip()
                if s:
                    existing.add(s)
                    if s.startswith('[') and s.endswith(']'):
                        existing.add(s[1:-1])
        except Exception:
            pass
        return existing

    def _upsert_lines(self, base, auth, po, edd, existing_skus):
        url_group = f"{base}/inboundforecast/group/{po.name}"
        for l in po.order_line:
            sku = (l.product_id.monta_sku or l.product_id.default_code or '').strip()
            if not sku:
                raise ValueError(f"Line '{l.display_name}' has no SKU.")
            qty = int(l.product_qty or 0)
            if qty <= 0:
                continue
            line_payload = {
                "Sku": sku,
                "Quantity": qty,
                "DeliveryDate": edd,
                "Reference": po.name,
                "Approved": False,
                "Comment": "",
            }
            if sku in existing_skus:
                self._http(po, "PUT", f"{url_group}/{sku}", line_payload, auth=auth)
            else:
                st, body = self._http(po, "POST", url_group, line_payload, auth=auth)
                if not (200 <= (st or 0) < 300):
                    # fallback if server says “already in group”
                    txt = json.dumps(body).lower()
                    if "already" in txt or "exist" in txt:
                        self._http(po, "PUT", f"{url_group}/{sku}", line_payload, auth=auth)

    # ---------- public: create/update ----------
    def send_for_po(self, po):
        """Create or update (idempotent). Call on confirm or on update."""
        if not self._is_allowed_instance():
            return False
        if po.state not in ('purchase', 'done'):
            _logger.info("[Monta IF] Skip PO %s (state=%s)", po.name, po.state)
            return False

        base, user, pwd, tz, _wh, _allow = self._conf()
        if not user or not pwd:
            raise RuntimeError("Monta credentials missing in System Parameters.")
        auth = HTTPBasicAuth(user, pwd)
        header, edd = self._group_payload(po, tz)

        sc = (header.get('SupplierCode') or '').strip()
        if not sc or len(sc) < 3:
            raise ValueError("SupplierCode is missing/invalid.")

        # 1) check
        st, body = self._get_group(base, auth, po)
        if st == 404:
            # 2a) create with lines
            st2, body2 = self._create_group_with_lines(base, auth, po, header, self._collect_lines(po, edd))
            if 200 <= (st2 or 0) < 300:
                uid = body2.get("UniqueId")
                if uid:
                    try:
                        po.write({'x_monta_inboundforecast_uid': uid})
                    except Exception:
                        pass
                _logger.info("[Monta IF] ✅ Created group for %s", po.name)
                return True
            # if POST failed but conflict-like, fall through to PUT + upsert
            body = body2
        elif 200 <= (st or 0) < 300:
            pass
        else:
            raise RuntimeError(f"GET group failed for {po.name}: HTTP {st} {body}")

        # 2b) update header
        st3, body3 = self._put_header(base, auth, po, header)
        if not (200 <= (st3 or 0) < 300):
            raise RuntimeError(f"PUT header failed for {po.name}: HTTP {st3} {body3}")

        # 3) lines upsert
        existing = self._get_existing_skus(body if st == 200 else body3 if isinstance(body3, dict) else {})
        self._upsert_lines(base, auth, po, edd, existing)

        _logger.info("[Monta IF] ✅ Header synced and lines upserted for %s", po.name)
        return True

    # ---------- public: delete ----------
    def delete_for_po(self, po, note="Cancelled/Deleted from Odoo"):
        """Delete inbound forecast group when PO is cancelled or deleted."""
        if not self._is_allowed_instance():
            return False
        base, user, pwd, _tz, _wh, _allow = self._conf()
        if not user or not pwd:
            _logger.error("[Monta IF] Missing credentials; cannot delete group for %s", po.name)
            return False
        auth = HTTPBasicAuth(user, pwd)
        ok = self._delete_group(base, auth, po, note=note)
        return ok
