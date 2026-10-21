"""Mail -> WhatsApp bridge.

Watches a mailbox over IMAP. Every new email whose subject starts with a phone
number is forwarded to that number on WhatsApp through the official WhatsApp
Cloud API: each attachment is sent as a document, using an approved message
template so it reaches customers who haven't written to you in the last 24h.

    Subject: 0501234567 Invoice 1042      ->  +972 50-123-4567 gets the PDF,
                                              "Invoice 1042" fills {{1}}

Optionally it also listens for WhatsApp webhooks, so customer replies (with
their files) and failed deliveries are emailed to you instead of being lost on
a number that no phone app shows.

All settings come from environment variables (see .env.example).
"""

import email
import email.policy
import hashlib
import hmac
import imaplib
import json
import logging
import mimetypes
import os
import re
import smtplib
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import parseaddr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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

    forward_to: str = ""
    wa_verify_token: str = ""
    wa_app_secret: str = ""
    webhook_port: int = 8080

    @property
    def webhook_enabled(self):
        return bool(self.wa_verify_token)

    @classmethod
    def from_env(cls, mail=True):
        """mail=False skips the mailbox settings, for the `send` test command."""
        env = os.environ

        def need(name):
            value = env.get(name, "").strip()
            if not value:
                sys.exit(f"Missing required setting: {name}")
            return value

        def need_mail(name):
            return need(name) if mail else env.get(name, "").strip()

        senders = tuple(
            s.strip().lower() for s in env.get("ALLOWED_SENDERS", "").split(",") if s.strip()
        )
        if mail and not senders:
            sys.exit("ALLOWED_SENDERS must list at least one address (or domain like @company.com)")

        if mail and env.get("WA_VERIFY_TOKEN", "").strip():
            need("WA_APP_SECRET")
            need("FORWARD_TO")
            need("SMTP_HOST")

        return cls(
            imap_host=need_mail("IMAP_HOST"),
            imap_user=need_mail("IMAP_USER"),
            imap_password=need_mail("IMAP_PASSWORD"),
            imap_port=int(env.get("IMAP_PORT", 993)),
            imap_folder=env.get("IMAP_FOLDER", "INBOX"),
            poll_seconds=int(env.get("POLL_SECONDS", 30)),
            allowed_senders=senders,
            default_country_code=env.get("DEFAULT_COUNTRY_CODE", "972").lstrip("+"),
            wa_token=need("WA_TOKEN"),
            wa_phone_number_id=need("WA_PHONE_NUMBER_ID"),
            wa_api_version=env.get("WA_API_VERSION", "v23.0"),
            # Empty: plain documents, which only reach people who wrote in the last 24h.
            wa_doc_template=env.get("WA_DOC_TEMPLATE", "").strip(),
            wa_text_template=env.get("WA_TEXT_TEMPLATE", ""),
            wa_template_lang=env.get("WA_TEMPLATE_LANG", "he"),
            wa_template_params=int(env.get("WA_TEMPLATE_PARAMS", 1)),
            smtp_host=env.get("SMTP_HOST", ""),
            smtp_port=int(env.get("SMTP_PORT", 587)),
            smtp_user=env.get("SMTP_USER") or env.get("IMAP_USER", ""),
            smtp_password=env.get("SMTP_PASSWORD") or env.get("IMAP_PASSWORD", ""),
            notify_sender=env.get("NOTIFY_SENDER", "errors").lower(),
            forward_to=env.get("FORWARD_TO", "").strip(),
            wa_verify_token=env.get("WA_VERIFY_TOKEN", "").strip(),
            wa_app_secret=env.get("WA_APP_SECRET", "").strip(),
            webhook_port=int(env.get("WEBHOOK_PORT", 8080)),
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
        self.graph = f"https://graph.facebook.com/{cfg.wa_api_version}"
        self.base = f"{self.graph}/{cfg.wa_phone_number_id}"

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

    def download(self, media_id, filename=None):
        """Fetch a file a customer sent us."""
        info = self._check(self.http.get(f"{self.graph}/{media_id}", timeout=30))
        resp = self.http.get(info["url"], timeout=120)
        if not resp.ok:
            raise BridgeError(f"Could not download media {media_id}: HTTP {resp.status_code}")
        mime_type = info.get("mime_type", "application/octet-stream").split(";")[0]
        if not filename:
            filename = media_id + (mimetypes.guess_extension(mime_type) or "")
        return Attachment(filename, mime_type, resp.content)

    def _send(self, message):
        resp = self.http.post(f"{self.base}/messages", json=message, timeout=60)
        return self._check(resp)["messages"][0]["id"]

    def send_document(self, to, att, caption):
        media_id = self.upload(att)
        if not self.cfg.wa_doc_template:
            return self._send({
                "messaging_product": "whatsapp", "to": to, "type": "document",
                "document": {"id": media_id, "filename": att.filename, "caption": caption or att.filename},
            })
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

# Recently sent message ids -> the email they came from, so a delivery failure
# that WhatsApp reports later by webhook can be emailed back to its sender.
_sent = OrderedDict()
_sent_lock = threading.Lock()


def remember(wamid, msg):
    with _sent_lock:
        _sent[wamid] = (parseaddr(str(msg.get("From", "")))[1], str(msg.get("Subject", "")))
        while len(_sent) > 1000:
            _sent.popitem(last=False)


def handle(msg, cfg, wa):
    """Forward one email. Returns a short summary; raises BridgeError on bad input."""
    to, caption = parse_subject(str(msg.get("Subject", "")), cfg.default_country_code)
    attachments, body = extract(msg)

    if attachments:
        for att in attachments:
            remember(wa.send_document(to, att, caption), msg)
            log.info("sent %s to +%s", att.filename, to)
        names = ", ".join(a.filename for a in attachments)
        return f"Sent {len(attachments)} file(s) to +{to}: {names}"

    if cfg.wa_text_template:
        text = caption or body
        if not text:
            raise BridgeError("The email has no attachment and no text to send")
        remember(wa.send_text(to, text), msg)
        log.info("sent text to +%s", to)
        return f"Sent a text message to +{to}"

    raise BridgeError("The email has no attachment to send")


def send_email(cfg, to, subject, body, attachments=(), in_reply_to=None):
    mail = EmailMessage()
    mail["From"] = cfg.smtp_user
    mail["To"] = to
    mail["Subject"] = subject
    if in_reply_to:
        mail["In-Reply-To"] = in_reply_to
    mail.set_content(body)
    for att in attachments:
        maintype, _, subtype = att.mime_type.partition("/")
        mail.add_attachment(att.data, maintype=maintype, subtype=subtype or "octet-stream",
                            filename=att.filename)
    try:
        if cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
            server.starttls()
        with server:
            server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(mail)
    except Exception:
        log.exception("could not email %s", to)


def notify(cfg, msg, ok, summary):
    if not cfg.smtp_host or cfg.notify_sender == "none":
        return
    if ok and cfg.notify_sender != "all":
        return
    sender = parseaddr(str(msg.get("From", "")))[1]
    subject = ("WhatsApp sent: " if ok else "WhatsApp NOT sent: ") + str(msg.get("Subject", ""))
    send_email(cfg, sender, subject, summary, in_reply_to=msg.get("Message-ID"))


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


# --------------------------------------------------------------------------- incoming

MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")


def verify_signature(body, header, secret):
    """Meta signs every webhook with the app secret; reject anything else."""
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), (header or "").encode())


def describe(message):
    """A one-line text version of any incoming WhatsApp message."""
    kind = message.get("type", "")
    part = message.get(kind) or {}
    if kind == "text":
        return part.get("body", "")
    if kind in MEDIA_TYPES:
        return part.get("caption") or f"[{kind}]"
    if kind == "button":
        return part.get("text", "")
    if kind == "interactive":
        return (part.get("button_reply") or part.get("list_reply") or {}).get("title", "")
    if kind == "reaction":
        return f"[reaction {part.get('emoji', '')}]"
    if kind == "location":
        return f"[location {part.get('latitude')},{part.get('longitude')}] {part.get('name', '')}".strip()
    return f"[{kind} message]"


def forward_incoming(message, name, cfg, wa):
    sender = message.get("from", "")
    kind = message.get("type", "")
    text = describe(message)
    attachments = []
    if kind in MEDIA_TYPES:
        part = message[kind]
        try:
            attachments.append(wa.download(part["id"], part.get("filename")))
        except Exception as e:
            text += f"\n\n(The {kind} could not be downloaded: {e})"

    who = f"+{sender}" + (f" ({name})" if name else "")
    subject = f"WhatsApp from {who}: {template_text(text, 60)}"
    body = (f"{who} wrote on WhatsApp:\n\n{text}\n\n"
            f"To send them a file, email it to the bridge with the subject starting with +{sender}")
    send_email(cfg, cfg.forward_to, subject, body, attachments)
    log.info("forwarded a %s message from +%s", kind, sender)


def report_failure(status, cfg):
    with _sent_lock:
        origin = _sent.get(status.get("id"))
    err = (status.get("errors") or [{}])[0]
    reason = (err.get("error_data") or {}).get("details") or err.get("message") or err.get("title") or "unknown"
    recipient = status.get("recipient_id", "")
    body = f"WhatsApp could not deliver the message to +{recipient}.\nReason: {reason} (code {err.get('code')})"
    if err.get("code") == 131026:
        body += "\n\nThis usually means the number has no WhatsApp."
    to, subject = (origin[0], origin[1]) if origin else (cfg.forward_to, f"+{recipient}")
    send_email(cfg, to, "WhatsApp NOT delivered: " + subject, body)
    log.warning("delivery to +%s failed: %s", recipient, reason)


def handle_webhook(payload, cfg, wa):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                     for c in value.get("contacts", [])}
            for message in value.get("messages", []):
                forward_incoming(message, names.get(message.get("from"), ""), cfg, wa)
            for status in value.get("statuses", []):
                if status.get("status") == "failed":
                    report_failure(status, cfg)


def process_webhook(payload, cfg, wa):
    try:
        handle_webhook(payload, cfg, wa)
    except Exception:
        log.exception("could not handle webhook")


def make_handler(cfg, wa):
    class Webhook(BaseHTTPRequestHandler):
        def _reply(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/health":
                return self._reply(200, b"ok")
            q = parse_qs(url.query)
            token = q.get("hub.verify_token", [""])[0]
            if (url.path == "/webhook" and q.get("hub.mode") == ["subscribe"]
                    and hmac.compare_digest(token.encode(), cfg.wa_verify_token.encode())):
                return self._reply(200, q.get("hub.challenge", [""])[0].encode())
            self._reply(403)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if urlparse(self.path).path != "/webhook" or length > 1_000_000:
                return self._reply(404)
            body = self.rfile.read(length)
            if not verify_signature(body, self.headers.get("X-Hub-Signature-256"), cfg.wa_app_secret):
                log.warning("rejected a webhook with a bad signature")
                return self._reply(403)
            # Answer Meta right away; downloading media and emailing can take a while.
            self._reply(200)
            try:
                payload = json.loads(body)
            except ValueError:
                return
            threading.Thread(target=process_webhook, args=(payload, cfg, wa), daemon=True).start()

        def log_message(self, fmt, *args):
            pass

    return Webhook


def start_webhook_server(cfg, wa):
    server = ThreadingHTTPServer(("0.0.0.0", cfg.webhook_port), make_handler(cfg, wa))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("webhook listening on port %s, forwarding to %s", cfg.webhook_port, cfg.forward_to)
    return server


# --------------------------------------------------------------------------- entry points

def load_env_file(path=".env"):
    """Read KEY=VALUE lines from .env so running by hand needs no `export`s."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def send_test(args):
    """python bridge.py send 0501234567 invoice.pdf Invoice 1042  ->  WhatsApp only, no email."""
    if len(args) < 2:
        sys.exit("Usage: python bridge.py send PHONE FILE [TEXT FOR {{1}}]")
    cfg = Config.from_env(mail=False)
    path = args[1]
    try:
        to = normalize_phone(args[0], cfg.default_country_code)
        with open(path, "rb") as f:
            att = Attachment(os.path.basename(path), mimetypes.guess_type(path)[0] or "application/octet-stream", f.read())
        wamid = WhatsApp(cfg).send_document(to, att, " ".join(args[2:]))
    except (BridgeError, OSError) as e:
        sys.exit(f"Not sent: {e}")
    print(f"Sent {att.filename} to +{to} (message id {wamid})")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env_file()
    if sys.argv[1:2] == ["send"]:
        return send_test(sys.argv[2:])
    cfg = Config.from_env()
    wa = WhatsApp(cfg)
    if cfg.webhook_enabled:
        start_webhook_server(cfg, WhatsApp(cfg))
    log.info("watching %s@%s/%s every %ss", cfg.imap_user, cfg.imap_host, cfg.imap_folder, cfg.poll_seconds)
    while True:
        try:
            process_mailbox(cfg, wa)
        except Exception:
            log.exception("mailbox check failed, retrying")
        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
