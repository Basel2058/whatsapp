import unittest
from email.message import EmailMessage
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
