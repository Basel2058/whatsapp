import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest
from email.message import EmailMessage
from unittest import mock

import requests

import bridge
from bridge import BridgeError, Config


def make_cfg(**kw):
    base = dict(
        imap_host="imap.example.com", imap_user="wa@example.com", imap_password="x",
        allowed_senders=("@company.com",), wa_token="t", wa_phone_number_id="123",
        wa_doc_template="document_send",
    )
    base.update(kw)
    return Config(**base)


def make_mail(subject, attachments=(), body="hello"):
    msg = EmailMessage()
    msg["From"] = "Terminal <terminal@company.com>"
    msg["To"] = "wa@example.com"
    msg["Subject"] = subject
    msg.set_content(body)
    for name, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg


class PhoneTests(unittest.TestCase):
    def test_local_number_gets_country_code(self):
        self.assertEqual(bridge.normalize_phone("0501234567"), "972501234567")
        self.assertEqual(bridge.normalize_phone("050-123-4567"), "972501234567")

    def test_international_forms(self):
        self.assertEqual(bridge.normalize_phone("+972501234567"), "972501234567")
        self.assertEqual(bridge.normalize_phone("00972501234567"), "972501234567")
        self.assertEqual(bridge.normalize_phone("972501234567"), "972501234567")
        self.assertEqual(bridge.normalize_phone("+1 (212) 555-0100".replace(" ", "")), "12125550100")

    def test_other_default_country(self):
        self.assertEqual(bridge.normalize_phone("07911123456", "44"), "447911123456")

    def test_too_short(self):
        with self.assertRaises(BridgeError):
            bridge.normalize_phone("12345")


class SubjectTests(unittest.TestCase):
    def test_number_then_caption(self):
        self.assertEqual(bridge.parse_subject("0501234567 Invoice 1042"), ("972501234567", "Invoice 1042"))

    def test_separators(self):
        self.assertEqual(bridge.parse_subject("050-123-4567: Quote 7"), ("972501234567", "Quote 7"))
        self.assertEqual(bridge.parse_subject("+972501234567 - Quote 7"), ("972501234567", "Quote 7"))

    def test_number_only(self):
        self.assertEqual(bridge.parse_subject("0501234567"), ("972501234567", ""))

    def test_missing_number(self):
        with self.assertRaises(BridgeError):
            bridge.parse_subject("Invoice 1042")


class HelperTests(unittest.TestCase):
    def test_template_text_flattens_whitespace(self):
        self.assertEqual(bridge.template_text("a\n\tb     c"), "a b c")

    def test_sender_allowed(self):
        allowed = ("@company.com", "boss@gmail.com")
        self.assertTrue(bridge.sender_allowed("Terminal@Company.com", allowed))
        self.assertTrue(bridge.sender_allowed("boss@gmail.com", allowed))
        self.assertFalse(bridge.sender_allowed("x@evil.com", allowed))
        self.assertFalse(bridge.sender_allowed("x@notcompany.com", allowed))

    def test_extract(self):
        atts, body = bridge.extract(make_mail("s", [("a.pdf", b"%PDF-1")], body="hi there"))
        self.assertEqual([(a.filename, a.mime_type, a.data) for a in atts],
                         [("a.pdf", "application/pdf", b"%PDF-1")])
        self.assertEqual(body, "hi there")

    def test_template_message(self):
        m = bridge.build_template_message("9725", "doc", "he", ["Invoice\n1"], {"id": "M", "filename": "a.pdf"})
        self.assertEqual(m["template"]["components"], [
            {"type": "header", "parameters": [{"type": "document", "document": {"id": "M", "filename": "a.pdf"}}]},
            {"type": "body", "parameters": [{"type": "text", "text": "Invoice 1"}]},
        ])


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.ok, self.text = payload, status, status < 400, str(payload)

    def json(self):
        return self.payload


class HandleTests(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock()
        self.session.headers = {}
        self.session.post.side_effect = lambda url, **kw: FakeResponse(
            {"id": "MEDIA1"} if url.endswith("/media") else {"messages": [{"id": "wamid.1"}]}
        )

    def test_each_attachment_is_uploaded_and_sent(self):
        cfg = make_cfg()
        wa = bridge.WhatsApp(cfg, self.session)
        mail = make_mail("0501234567 Invoice 1042", [("a.pdf", b"1"), ("b.pdf", b"2")])
        summary = bridge.handle(mail, cfg, wa)

        self.assertIn("2 file(s) to +972501234567", summary)
        urls = [c.args[0] for c in self.session.post.call_args_list]
        base = "https://graph.facebook.com/v23.0/123"
        self.assertEqual(urls, [f"{base}/media", f"{base}/messages"] * 2)
        sent = self.session.post.call_args_list[1].kwargs["json"]
        self.assertEqual(sent["to"], "972501234567")
        self.assertEqual(sent["template"]["name"], "document_send")
        self.assertEqual(sent["template"]["components"][1]["parameters"][0]["text"], "Invoice 1042")
        self.assertEqual(self.session.headers["Authorization"], "Bearer t")

    def test_template_without_params(self):
        cfg = make_cfg(wa_template_params=0)
        bridge.handle(make_mail("0501234567 x", [("a.pdf", b"1")]), cfg, bridge.WhatsApp(cfg, self.session))
        sent = self.session.post.call_args_list[1].kwargs["json"]
        self.assertEqual([c["type"] for c in sent["template"]["components"]], ["header"])

    def test_plain_document_without_template(self):
        cfg = make_cfg(wa_doc_template="")
        bridge.handle(make_mail("0501234567 Invoice 9", [("a.pdf", b"1")]), cfg, bridge.WhatsApp(cfg, self.session))
        sent = self.session.post.call_args.kwargs["json"]
        self.assertEqual(sent["type"], "document")
        self.assertEqual(sent["document"], {"id": "MEDIA1", "filename": "a.pdf", "caption": "Invoice 9"})

    def test_no_attachment_without_text_template(self):
        cfg = make_cfg()
        with self.assertRaises(BridgeError):
            bridge.handle(make_mail("0501234567 hi"), cfg, bridge.WhatsApp(cfg, self.session))

    def test_no_attachment_with_text_template(self):
        cfg = make_cfg(wa_text_template="text_send")
        bridge.handle(make_mail("0501234567", body="Your order\nis ready"), cfg, bridge.WhatsApp(cfg, self.session))
        sent = self.session.post.call_args.kwargs["json"]
        self.assertEqual(sent["template"]["name"], "text_send")
        self.assertEqual(sent["template"]["components"][0]["parameters"][0]["text"], "Your order is ready")

    def test_api_error_is_reported(self):
        self.session.post.side_effect = lambda url, **kw: FakeResponse(
            {"error": {"message": "Template name does not exist"}}, 400
        )
        cfg = make_cfg()
        with self.assertRaisesRegex(BridgeError, "Template name does not exist"):
            bridge.handle(make_mail("0501234567", [("a.pdf", b"1")]), cfg, bridge.WhatsApp(cfg, self.session))


class FakeImap:
    def __init__(self, mails):
        self.mails = {str(i).encode(): m.as_bytes() for i, m in enumerate(mails, 1)}
        self.seen = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        pass

    def select(self, folder):
        pass

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b" ".join(self.mails)]
        if command == "fetch":
            return "OK", [(b"1 (UID 1 BODY[] {99}", self.mails[args[0]]), b")"]
        if command == "store":
            self.seen.append(args[0])
            return "OK", [b""]


class MailboxTests(unittest.TestCase):
    def test_only_allowed_senders_are_forwarded(self):
        good = make_mail("0501234567 Invoice", [("a.pdf", b"1")])
        bad = make_mail("0501234567 spam", [("x.pdf", b"1")])
        bad.replace_header("From", "someone@evil.com")
        imap = FakeImap([good, bad])
        wa = mock.Mock()

        with mock.patch("imaplib.IMAP4_SSL", return_value=imap):
            bridge.process_mailbox(make_cfg(), wa)

        self.assertEqual(imap.seen, [b"1", b"2"])
        wa.send_document.assert_called_once()
        to, att, caption = wa.send_document.call_args.args
        self.assertEqual((to, att.filename, caption), ("972501234567", "a.pdf", "Invoice"))


def webhook_payload(messages=(), statuses=(), contacts=()):
    value = {"messaging_product": "whatsapp", "contacts": list(contacts),
             "messages": list(messages), "statuses": list(statuses)}
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}]}


class IncomingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(forward_to="office@company.com", wa_verify_token="vt",
                            wa_app_secret="secret", smtp_host="smtp.example.com")
        patcher = mock.patch("bridge.send_email")
        self.send_email = patcher.start()
        self.addCleanup(patcher.stop)

    def test_text_reply_is_emailed(self):
        payload = webhook_payload(
            messages=[{"from": "972501234567", "id": "wamid.in", "type": "text", "text": {"body": "Thanks!\nGot it"}}],
            contacts=[{"wa_id": "972501234567", "profile": {"name": "Dana"}}],
        )
        bridge.handle_webhook(payload, self.cfg, mock.Mock())
        to, subject, body, attachments = self.send_email.call_args.args[1:]
        self.assertEqual(to, "office@company.com")
        self.assertEqual(subject, "WhatsApp from +972501234567 (Dana): Thanks! Got it")
        self.assertIn("Thanks!\nGot it", body)
        self.assertEqual(attachments, [])

    def test_document_reply_is_downloaded_and_attached(self):
        wa = mock.Mock()
        wa.download.return_value = bridge.Attachment("signed.pdf", "application/pdf", b"%PDF")
        payload = webhook_payload(messages=[{
            "from": "972501234567", "id": "wamid.in", "type": "document",
            "document": {"id": "MEDIA9", "filename": "signed.pdf", "mime_type": "application/pdf"},
        }])
        bridge.handle_webhook(payload, self.cfg, wa)
        wa.download.assert_called_once_with("MEDIA9", "signed.pdf")
        attachments = self.send_email.call_args.args[4]
        self.assertEqual([a.filename for a in attachments], ["signed.pdf"])

    def test_failed_delivery_goes_back_to_original_sender(self):
        bridge.remember("wamid.out", make_mail("0501234567 Invoice 7"))
        payload = webhook_payload(statuses=[{
            "id": "wamid.out", "status": "failed", "recipient_id": "972501234567",
            "errors": [{"code": 131026, "title": "Message undeliverable"}],
        }])
        bridge.handle_webhook(payload, self.cfg, mock.Mock())
        to, subject, body = self.send_email.call_args.args[1:4]
        self.assertEqual(to, "terminal@company.com")
        self.assertEqual(subject, "WhatsApp NOT delivered: 0501234567 Invoice 7")
        self.assertIn("no WhatsApp", body)

    def test_delivered_status_is_ignored(self):
        payload = webhook_payload(statuses=[{"id": "x", "status": "delivered", "recipient_id": "1"}])
        bridge.handle_webhook(payload, self.cfg, mock.Mock())
        self.send_email.assert_not_called()

    def test_download(self):
        session = mock.Mock()
        session.headers = {}
        session.get.side_effect = [
            FakeResponse({"url": "https://lookaside.example/m", "mime_type": "image/jpeg"}),
            mock.Mock(ok=True, content=b"JPEG"),
        ]
        att = bridge.WhatsApp(self.cfg, session).download("MEDIA1")
        self.assertEqual((att.filename, att.mime_type, att.data), ("MEDIA1.jpg", "image/jpeg", b"JPEG"))
        self.assertEqual(session.get.call_args_list[0].args[0], "https://graph.facebook.com/v23.0/MEDIA1")


class WebhookServerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg(forward_to="office@company.com", wa_verify_token="vt",
                            wa_app_secret="secret", smtp_host="smtp.example.com", webhook_port=0)
        self.server = bridge.start_webhook_server(self.cfg, mock.Mock())
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/webhook"

    def test_verification_handshake(self):
        ok = requests.get(self.url, params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
        self.assertEqual((ok.status_code, ok.text), (200, "42"))
        bad = requests.get(self.url, params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"})
        self.assertEqual(bad.status_code, 403)

    def test_signed_post_is_processed_and_unsigned_rejected(self):
        body = json.dumps(webhook_payload(
            messages=[{"from": "972501234567", "id": "w", "type": "text", "text": {"body": "hi"}}])).encode()
        done = threading.Event()
        with mock.patch("bridge.send_email", side_effect=lambda *a, **k: done.set()) as send:
            unsigned = requests.post(self.url, data=body, headers={"X-Hub-Signature-256": "sha256=bad"})
            self.assertEqual(unsigned.status_code, 403)

            sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
            signed = requests.post(self.url, data=body, headers={"X-Hub-Signature-256": sig})
            self.assertEqual(signed.status_code, 200)
            self.assertTrue(done.wait(5))
            self.assertEqual(send.call_count, 1)


class SendCommandTests(unittest.TestCase):
    def test_send_needs_only_whatsapp_settings(self):
        env = {"WA_TOKEN": "t", "WA_PHONE_NUMBER_ID": "123", "WA_DOC_TEMPLATE": "document_send"}
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(bridge.WhatsApp, "send_document", return_value="wamid.T") as send, \
                mock.patch("builtins.print") as out:
            path = os.path.join(d, "invoice.pdf")
            with open(path, "wb") as f:
                f.write(b"%PDF")
            bridge.send_test(["050-123-4567", path, "Invoice", "1042"])

        to, att, caption = send.call_args.args
        self.assertEqual((to, att.filename, att.mime_type, att.data, caption),
                         ("972501234567", "invoice.pdf", "application/pdf", b"%PDF", "Invoice 1042"))
        self.assertIn("wamid.T", out.call_args.args[0])

    def test_env_file(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KEEP": "env"}, clear=True):
            path = os.path.join(d, ".env")
            with open(path, "w") as f:
                f.write("# comment\nWA_TOKEN='abc'\nKEEP=file\nEMPTY=\n")
            bridge.load_env_file(path)
            self.assertEqual((os.environ["WA_TOKEN"], os.environ["KEEP"], os.environ["EMPTY"]), ("abc", "env", ""))


if __name__ == "__main__":
    unittest.main()
