# Mail → WhatsApp bridge

Send WhatsApp messages from any program that can send email.

```
Work terminal ──email──▶ whatsapp@company.com ──bridge──▶ WhatsApp Cloud API ──▶ customer
```

1. In the terminal, send the document by mail to the bridge mailbox.
2. Put the customer's phone number at the **start of the subject**:

   | Subject | Goes to | Message text (`{{1}}`) |
   |---|---|---|
   | `0501234567 Invoice 1042` | +972 50-123-4567 | Invoice 1042 |
   | `050-123-4567: Quote 7` | +972 50-123-4567 | Quote 7 |
   | `+447911123456 Receipt` | +44 7911 123456 | Receipt |

3. Every attachment (PDF, image, …) is sent to that number as a WhatsApp document.
   If something goes wrong (bad number, no attachment, API error), the sender gets an email back saying why.

Only emails from `ALLOWED_SENDERS` are forwarded; everything else is ignored, so a stranger
can't use your mailbox to message people.

## One-time setup

### 1. A mailbox for the bridge
Create a dedicated address (e.g. `whatsapp@company.com`) with IMAP enabled.
For Gmail / Google Workspace or Microsoft 365, create an **app password** and use it as `IMAP_PASSWORD`.
The bridge reads only unread mail and marks each one read as it handles it.

### 2. WhatsApp Cloud API
1. At [business.facebook.com](https://business.facebook.com) create (or use) your Meta Business account.
2. At [developers.facebook.com](https://developers.facebook.com/apps) create an app of type **Business** and add the **WhatsApp** product.
3. Connect your business phone number and copy its **Phone number ID** → `WA_PHONE_NUMBER_ID`. Either:
   - **Keep using it in the WhatsApp Business app on the phone ("coexistence"):** connect it through Meta's embedded signup with the "WhatsApp Business App" option and scan the QR code from the app. Messages the bridge sends then also show up in the app. Open the app at least every ~2 weeks or the link drops.
   - **Dedicated API number:** add it under **WhatsApp → API Setup** and verify by SMS. It can't be used in any WhatsApp app after that.
4. In Business Settings → **System users**, create a system user, give it the app and the WhatsApp account, and generate a **permanent token** with `whatsapp_business_messaging` and `whatsapp_business_management` → `WA_TOKEN`.
5. Add a payment method to the WhatsApp account (template messages are billed per message by Meta).

### 3. Message template
WhatsApp only lets a business start a conversation with an approved template.
In **WhatsApp Manager → Message templates** create one:

- **Category:** Utility
- **Name:** `document_send` (→ `WA_DOC_TEMPLATE`)
- **Language:** Hebrew (→ `WA_TEMPLATE_LANG=he`; English is `en` or `en_US`, whichever you picked)
- **Header:** *Document*
- **Body:** e.g. `שלום, מצורף {{1}}. תודה!` / `Hello, here is your {{1}}. Thank you!`

`{{1}}` is filled with the subject text after the phone number (or the file name if there isn't any).
If your body has no variable, set `WA_TEMPLATE_PARAMS=0`.

Optional: to also send emails *without* an attachment, create a second template with a text body
`{{1}}` and set `WA_TEXT_TEMPLATE` — the subject text (or the email body) is sent.

Wait for the templates to show **Active** before going live.

### 4. Run it on the server
```bash
cp .env.example .env      # fill in the values
docker build -t mail-bridge .
docker run -d --name mail-bridge --restart unless-stopped --env-file .env mail-bridge
docker logs -f mail-bridge
```

Without Docker:
```bash
pip install -r requirements.txt
set -a; . ./.env; set +a
python bridge.py
```

## Notes
- "Sent" means WhatsApp accepted the message. If the number has no WhatsApp, Meta reports that
  later via webhooks, which this bridge doesn't listen to; check **WhatsApp Manager → Insights** if a customer says they got nothing.
- Attachments up to 100 MB are accepted by WhatsApp.
- Tests: `python -m unittest`
