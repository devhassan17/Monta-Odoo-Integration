# -*- coding: utf-8 -*-
import logging
import time
import requests
from requests.auth import HTTPBasicAuth

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20


class MontaClient:
    """Thin HTTP client for Monta with basic auth and structured logging."""
    def __init__(self, env, company=None):
        self.env = env
        self.company = company or env.company

    def _conf(self):
        cfg = self.env["monta.config"].sudo().get_for_company(self.company)
        if not cfg:
            return None

        base = (cfg.base_url or "https://api-v6.monta.nl").rstrip("/")
        user = (cfg.username or "").strip()
        pwd = (cfg.password or "").strip()
        to = int(cfg.timeout or DEFAULT_TIMEOUT)
        return base, user, pwd, to

    def request(self, order, method, path, payload=None, headers=None):
        conf = self._conf()
        if not conf:
            return 0, {"error": f"Monta not configured or company not allowed ({self.company.display_name})."}
        base, user, pwd, timeout = conf

        url = f"{base}/{path.lstrip('/')}"
        headers = headers or {"Content-Type": "application/json", "Accept": "application/json"}

        start = time.time()
        _logger.info("[Monta API] %s %s | User: %s", method.upper(), url, user)

        order._create_monta_log(
            {"request": {"method": method.upper(), "url": url, "headers": headers, "auth_user": user, "payload": payload or {}}},
            "info", tag="Monta API", console_summary=f"[Monta API] queued request log for {method.upper()} {url}"
        )

        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=payload,
                auth=HTTPBasicAuth(user, pwd) if (user or pwd) else None,
                timeout=timeout,
            )
            elapsed = time.time() - start
            try:
                body = resp.json()
            except Exception:
                body = {"raw": (resp.text or "")[:1000]}

            msg = "[Monta API] %s %s | Status: %s | Time: %.2fs" % (method.upper(), url, resp.status_code, elapsed)
            (_logger.info if resp.ok else _logger.error)(msg)

            order._create_monta_log(
                {"response": {"status": resp.status_code, "time_seconds": round(elapsed, 2), "body": body}},
                "info" if resp.ok else "error",
                tag="Monta API",
                console_summary=f"[Monta API] saved response log for {method.upper()} {url}",
            )
            return resp.status_code, body

        except requests.RequestException as e:
            elapsed = time.time() - start
            _logger.error("[Monta API] %s %s | Request failed after %.2fs | %s", method.upper(), url, elapsed, str(e))
            order._create_monta_log({"exception": str(e)}, "error", tag="Monta API", console_summary="[Monta API] saved exception log")
            return 0, {"error": str(e)}
