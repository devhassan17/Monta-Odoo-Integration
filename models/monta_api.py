# -*- coding: utf-8 -*-
import logging
import json
import re
import requests
from requests.auth import HTTPBasicAuth

from odoo import models, fields

_logger = logging.getLogger(__name__)


class MontaAPIMixin(models.AbstractModel):
    _name = "monta.api.mixin"
    _description = "Monta API Helper Methods (payload build + HTTP calls)"

    # ---------------------------
    # Address utilities
    # ---------------------------
    def _split_street(self, street, street2=""):
        """Split street + house number (Dutch style)."""
        full = (street or "") + " " + (street2 or "")
        full = full.strip()
        m = re.match(r"^(?P<street>.*?)[\s,]+(?P<number>\d+)(?P<suffix>\s*\w*)$", full)
        if m:
            return (
                m.group("street").strip(),
                m.group("number").strip(),
                (m.group("suffix") or "").strip(),
            )
        return full, "", ""

    # ---------------------------
    # Payload
    # ---------------------------
    def _prepare_monta_order_payload(self):
        """Build Monta order payload from sale.order."""
        self.ensure_one()
        partner = self.partner_id
        street, house_number, house_suffix = self._split_street(
            partner.street or "", partner.street2 or ""
        )

        lines = []
        for l in self.order_line:
            sku = l.product_id.default_code or f"TESTSKU-{l.product_id.id}"
            lines.append(
                {
                    "Sku": sku,
                    "OrderedQuantity": int(l.product_uom_qty or 0),
                }
            )

        payload = {
            "WebshopOrderId": self.name,
            "Reference": self.client_order_ref or "",
            "Origin": "Moyee_Odoo",
            "ConsumerDetails": {
                "DeliveryAddress": {
                    "Company": partner.company_name or partner.name or "",
                    "FirstName": partner.name.split(" ")[0] if partner.name else "",
                    "LastName": " ".join(partner.name.split(" ")[1:])
                    if len((partner.name or "").split(" ")) > 1
                    else "",
                    "Street": street,
                    "HouseNumber": house_number or "1",
                    "HouseNumberAddition": house_suffix or "",
                    "PostalCode": partner.zip or "0000AA",
                    "City": partner.city or "TestCity",
                    "CountryCode": partner.country_id.code if partner.country_id else "NL",
                    "PhoneNumber": partner.phone or "0000000000",
                    "EmailAddress": partner.email or "test@example.com",
                },
                "InvoiceAddress": {
                    "Company": partner.company_name or partner.name or "",
                    "FirstName": partner.name.split(" ")[0] if partner.name else "",
                    "LastName": " ".join(partner.name.split(" ")[1:])
                    if len((partner.name or "").split(" ")) > 1
                    else "",
                    "Street": street,
                    "HouseNumber": house_number or "1",
                    "HouseNumberAddition": house_suffix or "",
                    "PostalCode": partner.zip or "0000AA",
                    "City": partner.city or "TestCity",
                    "CountryCode": partner.country_id.code if partner.country_id else "NL",
                    "PhoneNumber": partner.phone or "0000000000",
                    "EmailAddress": partner.email or "test@example.com",
                },
            },
            "Lines": lines,
            "Invoice": {
                "PaymentMethodDescription": "Odoo Test Order",
                "AmountInclTax": float(self.amount_total or 0.0),
                "TotalTax": float(sum(line.price_tax for line in self.order_line)),
                "WebshopFactuurID": int(re.sub(r"\D", "", self.name) or "9999"),
                "Currency": self.currency_id.name or "EUR",
            },
        }
        return payload

    # ---------------------------
    # HTTP helpers
    # ---------------------------
    def _monta_base_url(self):
        return "https://api-v6.monta.nl"

    def _monta_credentials(self):
        """
        Return (username, password).
        You said you'll keep credentials in code for now.
        """
        username = "testmoyeeMONTAODOOCONNECTOR"
        password = "91C4%@$=VL42"
        return username, password

    def _send_to_monta(self, payload):
        """POST order to Monta."""
        self.ensure_one()
        url = f"{self._monta_base_url()}/order"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        username, password = self._monta_credentials()

        try:
            _logger.info("=== Starting Monta API Request ===")
            _logger.info(f"Preparing request to: {url}")
            _logger.debug(f"Request payload: {json.dumps(payload, indent=2, default=str)}")
            _logger.info("Using username: [REDACTED]")
            _logger.info("Using password: [REDACTED]")

            start_time = fields.Datetime.now()
            resp = requests.post(
                url,
                headers=headers,
                json=payload,
                auth=HTTPBasicAuth(username, password),
                timeout=15,
            )
            elapsed = (fields.Datetime.now() - start_time).total_seconds()
            _logger.info(f"Response received in {elapsed:.2f}s (HTTP {resp.status_code})")
            _logger.debug(f"Response content (first 500): {resp.text[:500]}")

            if resp.status_code in (200, 201):
                _logger.info("Monta API request successful.")
                return resp.json() if resp.text else {"status": "ok"}
            return {"error": f"API Error {resp.status_code}", "details": resp.text}
        except requests.exceptions.RequestException as e:
            _logger.error(f"Request failed: {type(e).__name__} - {str(e)}")
            return {"error": str(e)}
        except Exception as e:
            _logger.error("Unexpected error in _send_to_monta")
            _logger.error(f"{type(e).__name__}: {str(e)}")
            return {"error": str(e)}
        finally:
            _logger.info("=== Monta API Request Completed ===")

    # ---------------------------
    # DELETE /order/{webshoporderid} with JSON-Patch note
    # ---------------------------
    def _delete_monta_order(self, webshoporderid, note="Cancelled"):
        """
        DELETE /order/{webshoporderid}
        Body: {"Note": "<reason>"} with content-type: application/json-patch+json
        Success: 204 No Content
        """
        self.ensure_one()
        username, password = self._monta_credentials()
        url = f"{self._monta_base_url().rstrip('/')}/order/{webshoporderid}"
        headers = {
            "Content-Type": "application/json-patch+json",
            "Accept": "application/json",
        }
        payload = {"Note": note or "Cancelled"}

        try:
            _logger.info(f"Deleting Monta order: {webshoporderid}")
            _logger.info("Using credentials: [REDACTED]/[REDACTED]")
            _logger.debug(f"DELETE payload: {payload}")

            resp = requests.delete(
                url,
                headers=headers,
                json=payload,  # Monta expects a JSON body in DELETE here
                auth=HTTPBasicAuth(username, password),
                timeout=15,
            )
            _logger.info(f"Monta DELETE HTTP {resp.status_code}")

            if resp.status_code == 204:
                return {"status": "deleted"}
            elif resp.status_code == 400:
                # error payload example contains 'OrderDeleteInvalidReasons'
                try:
                    return {"error": "Order delete invalid", "details": resp.json()}
                except Exception:
                    return {"error": "Order delete invalid", "details": resp.text}
            elif resp.status_code == 401:
                return {"error": "Unauthorized"}
            elif resp.status_code == 404:
                return {"error": "Order not found"}
            else:
                # unexpected
                try:
                    return {"error": f"API Error {resp.status_code}", "details": resp.json()}
                except Exception:
                    return {"error": f"API Error {resp.status_code}", "details": resp.text}
        except requests.exceptions.RequestException as e:
            return {"error": str(e)}
