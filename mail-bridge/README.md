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
   - **Dedicated API number (simplest, free):** if the number is active in a WhatsApp app, delete the account there first
     (WhatsApp Business → Settings → Account → Delete my account) and wait a few minutes. Then add it under
     **WhatsApp → API Setup → Add phone number** and verify by SMS. It can't be used in any WhatsApp app after that;
     turn on [customer replies](#customer-replies-optional) so answers reach you by email.
   - **Keep using it in the WhatsApp Business app on the phone ("coexistence"):** the developer dashboard's "Add phone number" only offers SMS, so connect it with Meta's Embedded Signup instead, either through a partner (BSP, monthly fee) or for free from your own app with [`signup.html`](../signup.html) (see below). Messages the bridge sends then also show up in the app. Open the app at least every ~2 weeks or the link drops.
4. In Business Settings → **System users**, create a system user, give it the app and the WhatsApp account, and generate a **permanent token** with `whatsapp_business_messaging` and `whatsapp_business_management` → `WA_TOKEN`.
5. Add a payment method to the WhatsApp account (template messages are billed per message by Meta).

#### Coexistence without a partner (`signup.html`)
Meta only lets an app run Embedded Signup after it qualifies as a Tech Provider (business verification and
app review), even for your own number. Until then the flow stops at "can't onboard customers right now".

1. In the app dashboard, under **Join partners**, join as a **Tech Provider**.
2. Add the **Facebook Login for Business** product → **Configurations** → create one with the
   **WhatsApp Embedded Signup** login variation. Copy its **Configuration ID**.
3. **Facebook Login for Business → Settings:** turn on *Login with the JavaScript SDK* and add the page's
   site (e.g. `https://basel2058.github.io/`) to *Allowed domains for the JavaScript SDK*.
4. Open `signup.html` on that site, enter the **App ID** and **Configuration ID**, press the button, choose
   to connect your existing WhatsApp Business app, and scan the QR code from the app on the phone.
5. The page shows the `WA_PHONE_NUMBER_ID`. Give your system user access to the new WhatsApp account
   (Business Settings → System users → Assign assets) so its token can send from that number.

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

### Quick test (WhatsApp only, no email)
Before setting up the mailbox, check the WhatsApp side from any computer with Python 3:
```bash
pip install requests
cp .env.example .env      # fill in at least WA_TOKEN, WA_PHONE_NUMBER_ID, WA_DOC_TEMPLATE, WA_TEMPLATE_LANG
python bridge.py send 0501234567 invoice.pdf Invoice 1042
```
With Meta's **test number** (WhatsApp → API Setup), use its Phone number ID and temporary access token, add the
phone you send to under *To* first, and create the template in the test WhatsApp account.

### 4. Run it on the server
```bash
cp .env.example .env      # fill in the values
docker compose up -d --build          # bridge + HTTPS for customer replies
# or, without customer replies:  docker compose up -d --build bridge
docker compose logs -f bridge
```

Without Docker:
```bash
pip install -r requirements.txt
set -a; . ./.env; set +a
python bridge.py
```

## Customer replies (optional)
A number used only through the API isn't in any phone app, so customer replies would go unseen.
With this turned on, the bridge emails you:
- every message a customer sends to the number, with any file or photo attached;
- every message WhatsApp could not deliver (e.g. the number has no WhatsApp), sent back to whoever emailed it.

Setup:
1. In `.env` set `FORWARD_TO` (where replies go), `SMTP_HOST`, a random `WA_VERIFY_TOKEN`,
   `WA_APP_SECRET` (App dashboard → App settings → Basic) and `DOMAIN`. Without a domain, use the server's
   IP with dashes plus `.sslip.io`, e.g. `203-0-113-5.sslip.io` for `203.0.113.5`.
2. Open ports 80 and 443 on the server and start with `docker compose up -d --build`.
   `https://DOMAIN/health` should show `ok`.
3. App dashboard → **WhatsApp → Configuration → Webhook**: Callback URL `https://DOMAIN/webhook`,
   Verify token = `WA_VERIFY_TOKEN`, then **Verify and save**. Under *Webhook fields*, subscribe to **messages**.
4. Meta only sends real messages to apps in **Live** mode: App settings → Basic, set the Privacy Policy URL to
   `https://basel2058.github.io/whatsapp/privacy.html`, then switch the app to **Live** (Publish).

Replying: email a file to the bridge with the customer's number in the subject, as usual.

## Notes
- "Sent" means WhatsApp accepted the message. Without customer replies turned on, a failed delivery
  (e.g. the number has no WhatsApp) isn't reported; check **WhatsApp Manager → Insights** if a customer says they got nothing.
- Attachments up to 100 MB are accepted by WhatsApp.
- Tests: `python -m unittest`
