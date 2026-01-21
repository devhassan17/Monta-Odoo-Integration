# Monta NL ⇄ Odoo Integration

Seamless integration between **Odoo** and **Monta NL (Fulfilment API)**.  
This module synchronizes orders, statuses, and tracking data between both systems and can be extended to support shipments and inventory.

---

## 🚀 Features

- Fetch orders from Monta into Odoo.
- Avoid duplicate orders via reference matching.
- Mirror Monta status, status code, and tracking info in Odoo.
- Configurable API credentials and timeout from Odoo Settings.
- Secure error logging (no sensitive data exposed).
- Easy to extend with new endpoints.

---

## ⚙️ Configuration

You can configure credentials from:

**Settings → General Settings → Monta Integration**

or manually via **Settings → Technical → System Parameters**.

| System Parameter Key | Description | Default / Example |
|----------------------|--------------|------------------|
| `monta.username` | Monta API username | e.g. `moyeeMONTAUSER` |
| `monta.password` | Monta API password (sensitive) | `••••••••` |
| `monta.base_url` | Base Monta API endpoint | `https://api-v6.monta.nl/` |
| `monta.timeout` | HTTP timeout in seconds | `30` |

---

## 🔐 Security Notes

- The Monta password is stored in **plain text** inside Odoo’s `ir.config_parameter` table.  
  Restrict backend and database access to trusted administrators only.
- Always use **HTTPS** for the base URL to secure credentials and payloads.
- API errors are logged in `ir.logging` but exclude sensitive information.
- Avoid echoing API responses containing credentials in logs or messages.

---

## 🧠 Developer Notes

The integration client is located in:
`monta_integration/models/monta_client.py`