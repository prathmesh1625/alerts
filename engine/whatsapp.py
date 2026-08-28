"""
whatsapp.py — send an alert over the Meta WhatsApp Cloud API.

A deliberately small port of the mechanism the NSE bot uses (shares/bot/
whatsapp.py). It does NOT import from that project and never touches its
database: the two products share only a phone number, and that is a decision
recorded in config, not a dependency in code.

Two ways to reach a number, and which one applies is not our choice:

  * INSIDE WhatsApp's 24-hour customer-service window — free-form text. The
    window opens when the person messages the number and closes 24h after
    their last message.
  * OUTSIDE it — an APPROVED template only. Meta signals this by rejecting
    the free-form send with error 131047, so the fallback is driven by the
    API's own answer rather than by us trying to track the window.

Everything here raises on failure. A notifier that believes a message was
delivered when it was not is worse than one that reports the error, because
the dedup table would then record it as sent and never retry.
"""
import re

import requests

import config

API_VERSION = "v19.0"

# Meta's code for "the 24-hour window is closed, use a template".
REENGAGEMENT_ERROR_CODE = 131047
# "recipient is not a verified test number" — the usual first-run error while
# the Meta app is still in development mode.
NOT_A_TEST_NUMBER_CODE = 131026

# Below WhatsApp's ~1024-char template body limit, leaving room for the
# template's own fixed text around the variables.
TEMPLATE_PARAM_MAX_LEN = 900


class WhatsAppError(Exception):
    """A non-2xx response from the Meta Cloud API."""

    def __init__(self, status_code, error_code, message, response_text=""):
        self.status_code = status_code
        self.error_code = error_code
        self.response_text = response_text
        super().__init__("WhatsApp API {} (code={}): {}".format(
            status_code, error_code, message))

    @property
    def is_reengagement(self) -> bool:
        """True when the send failed only because the 24h window is closed."""
        return self.error_code == REENGAGEMENT_ERROR_CODE


def _base_url() -> str:
    # Read at call time, not import time, so tests can point it elsewhere.
    return "https://graph.facebook.com/{}/{}".format(
        API_VERSION, config.WHATSAPP_PHONE_NUMBER_ID)


def _headers() -> dict:
    return {"Authorization": "Bearer {}".format(config.WHATSAPP_TOKEN),
            "Content-Type": "application/json"}


def sanitize_template_param(text) -> str:
    """
    Flatten a value so Meta accepts it as a template variable.

    Newlines, tabs and runs of 4+ spaces are rejected inside a {{n}}, and our
    headlines are built for a dashboard rather than for this.
    """
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(flat) > TEMPLATE_PARAM_MAX_LEN:
        flat = flat[:TEMPLATE_PARAM_MAX_LEN - 1].rstrip() + "…"
    return flat or "Stock alert"


def _raise_for_response(response):
    code, msg = None, response.text
    try:
        err = (response.json() or {}).get("error", {}) or {}
        code = err.get("code")
        msg = err.get("message", msg)
    except Exception:
        pass

    if code == NOT_A_TEST_NUMBER_CODE:
        print("[whatsapp] {} is not a verified test number. Add it under Meta "
              "App Dashboard > WhatsApp > API Setup, or set the app Live."
              .format(config.WHATSAPP_PHONE_NUMBER_ID), flush=True)
    raise WhatsAppError(response.status_code, code, msg, response.text)


def _post(endpoint: str, payload: dict):
    resp = requests.post(_base_url() + endpoint, headers=_headers(),
                         json=payload, timeout=30)
    if resp.status_code not in (200, 201):
        _raise_for_response(resp)
    return resp


def _wamid(resp) -> str:
    return (resp.json().get("messages") or [{}])[0].get("id", "")


def send_text(to: str, message: str) -> str:
    """Free-form text. Valid only inside the 24-hour window."""
    return _wamid(_post("/messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message[:4096], "preview_url": True},
    }))


def send_template(to: str, body_params, template_name: str = None) -> str:
    """An approved text-only template. Valid outside the 24-hour window."""
    params = [{"type": "text", "text": sanitize_template_param(p)}
              for p in (body_params or [])]
    components = [{"type": "body", "parameters": params}] if params else []
    return _wamid(_post("/messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name or config.WHATSAPP_TEMPLATE_NAME,
            "language": {"code": config.WHATSAPP_TEMPLATE_LANG},
            "components": components,
        },
    }))


def send(to: str, text: str, template_params=None) -> tuple:
    """
    Deliver `text`, falling back to the template if the window has closed.

    Returns (channel, wamid) where channel is "text" or "template".

    The fallback is triggered by Meta's own 131047 rather than by tracking the
    window ourselves: any window state we kept would be a guess, and a wrong
    guess here means either a message that never arrives or a needless
    template send.
    """
    try:
        return "text", send_text(to, text)
    except WhatsAppError as e:
        if not e.is_reengagement:
            raise
        if not config.WHATSAPP_TEMPLATE_NAME:
            # Nothing to fall back TO. Say so plainly — silently dropping the
            # alert would look identical to there being no alert.
            raise
        return "template", send_template(to, template_params or [text])
