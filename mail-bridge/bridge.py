"""Mail -> WhatsApp bridge.

Watches a mailbox over IMAP. Every new email whose subject starts with a phone
number is forwarded to that number on WhatsApp through the official WhatsApp
Cloud API: each attachment is sent as a document, using an approved message
template so it reaches customers who haven't written to you in the last 24h.

    Subject: 0501234567 Invoice 1042      ->  +972 50-123-4567 gets the PDF,
                                              "Invoice 1042" fills {{1}}

All settings come from environment variables (see .env.example).
"""

import email
import email.policy
import imaplib
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr

import requests

log = logging.getLogger("mail-bridge")


# --------------------------------------------------------------------------- config

@dataclass
class Config:
    imap_host: str
    imap_user: str
    imap_password: str
    imap_port: int = 993
    imap_folder: str = "INBOX"
    poll_seconds: int = 30

    allowed_senders: tuple = ()
    default_country_code: str = "972"

    wa_token: str = ""
    wa_phone_number_id: str = ""
    wa_api_version: str = "v23.0"
    wa_doc_template: str = ""
    wa_text_template: str = ""
    wa_template_lang: str = "he"
    wa_template_params: int = 1

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notify_sender: str = "errors"  # errors | all | none

    @classmethod
    def from_env(cls):
        env = os.environ

        def need(name):
            value = env.get(name, "").strip()
            if not value:
                sys.exit(f"Missing required setting: {name}")
            return value

        senders = tuple(
            s.strip().lower() for s in env.get("ALLOWED_SENDERS", "").split(",") if s.strip()
        )
        if not senders:
            sys.exit("ALLOWED_SENDERS must list at least one address (or domain like @company.com)")

        return cls(
            imap_host=need("IMAP_HOST"),
            imap_user=need("IMAP_USER"),
            imap_password=need("IMAP_PASSWORD"),
            imap_port=int(env.get("IMAP_PORT", 993)),
            imap_folder=env.get("IMAP_FOLDER", "INBOX"),
            poll_seconds=int(env.get("POLL_SECONDS", 30)),
            allowed_senders=senders,
            default_country_code=env.get("DEFAULT_COUNTRY_CODE", "972").lstrip("+"),
            wa_token=need("WA_TOKEN"),
            wa_phone_number_id=need("WA_PHONE_NUMBER_ID"),
            wa_api_version=env.get("WA_API_VERSION", "v23.0"),
            wa_doc_template=need("WA_DOC_TEMPLATE"),
            wa_text_template=env.get("WA_TEXT_TEMPLATE", ""),
            wa_template_lang=env.get("WA_TEMPLATE_LANG", "he"),
            wa_template_params=int(env.get("WA_TEMPLATE_PARAMS", 1)),
            smtp_host=env.get("SMTP_HOST", ""),
            smtp_port=int(env.get("SMTP_PORT", 587)),
            smtp_user=env.get("SMTP_USER") or env["IMAP_USER"],
            smtp_password=env.get("SMTP_PASSWORD") or env["IMAP_PASSWORD"],
            notify_sender=env.get("NOTIFY_SENDER", "errors").lower(),
        )


# --------------------------------------------------------------------------- parsing

class BridgeError(Exception):
    """A problem with one email that should be reported back to its sender."""


PHONE_RE = re.compile(r"^\s*(\+?[\d][\d\-().]{6,}\d)\s*[:\-,;]?\s*(.*)$", re.S)


def normalize_phone(raw, default_country_code="972"):
    """Turn 050-123-4567 / +972501234567 / 00972501234567 into 972501234567."""
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = default_country_code + digits[1:]
    if not 8 <= len(digits) <= 15:
        raise BridgeError(f"'{raw}' doesn't look like a phone number")
    return digits


def parse_subject(subject, default_country_code="972"):
    """'0501234567 Invoice 1042' -> ('972501234567', 'Invoice 1042')."""
    match = PHONE_RE.match(subject or "")
    if not match:
        raise BridgeError(
            "The subject must start with the phone number, e.g. '0501234567 Invoice 1042'"
        )
    return normalize_phone(match.group(1), default_country_code), match.group(2).strip()


def template_text(text, limit=1000):
    """Template parameters can't hold newlines, tabs or long runs of spaces."""
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def sender_allowed(address, allowed):
    address = address.lower()
    return any(address == a or (a.startswith("@") and address.endswith(a)) for a in allowed)


@dataclass
class Attachment:
    filename: str
    mime_type: str
    data: bytes


def extract(msg):
    """Return (attachments, plain-text body) of a parsed email."""
    attachments = []
    for part in msg.iter_attachments():
        filename = part.get_filename()
        data = part.get_payload(decode=True)
        if filename and data:
            attachments.append(Attachment(filename, part.get_content_type(), data))

    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body is not None and body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    return attachments, text.strip()


# --------------------------------------------------------------------------- whatsapp

class WhatsApp:
    def __init__(self, cfg, session=None):
        self.cfg = cfg
        self.http = session or requests.Session()
        self.http.headers["Authorization"] = f"Bearer {cfg.wa_token}"
        self.base = f"https://graph.facebook.com/{cfg.wa_api_version}/{cfg.wa_phone_number_id}"

    def _check(self, resp):
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not resp.ok:
            err = payload.get("error", {})
            detail = err.get("error_user_msg") or err.get("message") or resp.text[:300]
            raise BridgeError(f"WhatsApp API error {resp.status_code}: {detail}")
        return payload

    def upload(self, att):
        resp = self.http.post(
            f"{self.base}/media",
            data={"messaging_product": "whatsapp", "type": att.mime_type},
            files={"file": (att.filename, att.data, att.mime_type)},
            timeout=120,
        )
        return self._check(resp)["id"]

    def _send(self, message):
        resp = self.http.post(f"{self.base}/messages", json=message, timeout=60)
        return self._check(resp)["messages"][0]["id"]

    def send_document(self, to, att, caption):
        media_id = self.upload(att)
        return self._send(build_template_message(
            to, self.cfg.wa_doc_template, self.cfg.wa_template_lang,
            params=[caption or att.filename][: self.cfg.wa_template_params],
            document={"id": media_id, "filename": att.filename},
        ))

    def send_text(self, to, text):
        return self._send(build_template_message(
            to, self.cfg.wa_text_template, self.cfg.wa_template_lang,
            params=[text][: self.cfg.wa_template_params],
        ))


def build_template_message(to, template, lang, params=(), document=None):
    components = []
    if document:
        components.append({
            "type": "header",
            "parameters": [{"type": "document", "document": document}],
        })
    if params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": template_text(p)} for p in params],
        })
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {"name": template, "language": {"code": lang}, "components": components},
    }


# --------------------------------------------------------------------------- bridge

def handle(msg, cfg, wa):
    """Forward one email. Returns a short summary; raises BridgeError on bad input."""
    to, caption = parse_subject(str(msg.get("Subject", "")), cfg.default_country_code)
    attachments, body = extract(msg)

    if attachments:
        for att in attachments:
            wa.send_document(to, att, caption)
            log.info("sent %s to +%s", att.filename, to)
        names = ", ".join(a.filename for a in attachments)
        return f"Sent {len(attachments)} file(s) to +{to}: {names}"

    if cfg.wa_text_template:
        text = caption or body
        if not text:
            raise BridgeError("The email has no attachment and no text to send")
        wa.send_text(to, text)
        log.info("sent text to +%s", to)
        return f"Sent a text message to +{to}"

    raise BridgeError("The email has no attachment to send")


def notify(cfg, msg, ok, summary):
    if not cfg.smtp_host or cfg.notify_sender == "none":
        return
    if ok and cfg.notify_sender != "all":
        return
    sender = parseaddr(str(msg.get("From", "")))[1]
    reply = EmailMessage()
    reply["From"] = cfg.smtp_user
    reply["To"] = sender
    reply["Subject"] = ("WhatsApp sent: " if ok else "WhatsApp NOT sent: ") + str(msg.get("Subject", ""))
    if msg.get("Message-ID"):
        reply["In-Reply-To"] = msg["Message-ID"]
    reply.set_content(summary)
    try:
        if cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
            server.starttls()
        with server:
            server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(reply)
    except Exception:
        log.exception("could not email the result back to %s", sender)


def process_mailbox(cfg, wa):
    with imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port) as imap:
        imap.login(cfg.imap_user, cfg.imap_password)
        imap.select(cfg.imap_folder)
        _, data = imap.uid("search", None, "UNSEEN")
        for uid in data[0].split():
            _, parts = imap.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            # Mark as read first so a crash can never send the same email twice.
            imap.uid("store", uid, "+FLAGS", "(\\Seen)")
            if raw is None:
                continue
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            sender = parseaddr(str(msg.get("From", "")))[1]

            if not sender_allowed(sender, cfg.allowed_senders):
                log.warning("ignored email from unlisted sender %s", sender)
                continue

            try:
                summary = handle(msg, cfg, wa)
                ok = True
            except BridgeError as e:
                summary, ok = str(e), False
                log.warning("not sent (%s): %s", msg.get("Subject"), e)
            except Exception as e:
                summary, ok = f"Unexpected error: {e}", False
                log.exception("failed on %s", msg.get("Subject"))
            notify(cfg, msg, ok, summary)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    wa = WhatsApp(cfg)
    log.info("watching %s@%s/%s every %ss", cfg.imap_user, cfg.imap_host, cfg.imap_folder, cfg.poll_seconds)
    while True:
        try:
            process_mailbox(cfg, wa)
        except Exception:
            log.exception("mailbox check failed, retrying")
        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
