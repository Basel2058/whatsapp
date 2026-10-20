# Mail → WhatsApp bridge on Google Apps Script

The same bridge as [`../mail-bridge`](../mail-bridge/README.md), but it runs **free on Google's servers**:
no server, no Docker, no credit card. The bridge mailbox is a Gmail account, and the script checks it every minute.

```
Work terminal ──email──▶ bridge Gmail ──Apps Script──▶ WhatsApp Cloud API ──▶ customer
```

Usage is identical: email the file to the bridge Gmail with the customer's number at the start of the subject,
e.g. `0501234567 Invoice 1042`. Problems are emailed back to the sender.

## Setup

1. **Gmail:** create a Gmail account just for the bridge (e.g. `tbtech.invoices@gmail.com`).
   The terminal sends to this address.
2. **Script:** signed in as that account, open [script.google.com](https://script.google.com) → **New project**.
   Replace the contents of `Code.gs` with [`Code.gs`](Code.gs) from this folder and save.
3. **Settings:** ⚙️ **Project Settings → Script properties → Add script property**:

   | Property | Value |
   |---|---|
   | `WA_TOKEN` | access token (temporary from API Setup for testing; permanent system-user token for real use) |
   | `WA_PHONE_NUMBER_ID` | Phone number ID from WhatsApp → API Setup |
   | `WA_DOC_TEMPLATE` | `document_send` |
   | `WA_TEMPLATE_LANG` | `ar`, `he`, `en_US`… the template's language |
   | `ALLOWED_SENDERS` | the terminal's address, or `@company.com` for a whole domain |
   | `TEST_PHONE` | your own number, for `testSend` |

   Optional: `WA_TEMPLATE_PARAMS` (default `1`, set `0` if the template body has no `{{1}}`),
   `WA_TEXT_TEMPLATE`, `DEFAULT_COUNTRY_CODE` (default `972`), `NOTIFY_SENDER` (`errors` / `all` / `none`),
   `WA_API_VERSION` (default `v23.0`).
4. **Test:** in the editor pick **`testSend`** and press **Run**. The first time, Google asks for permission:
   **Review permissions → your account → Advanced → Go to project (unsafe) → Allow**. It's your own script.
   A test PDF should arrive on WhatsApp at `TEST_PHONE`.
5. **Start:** pick **`setup`** and press **Run**. From now on new emails are sent within about a minute.
   You can see each run under **Executions** (left sidebar).

## Customer replies (optional)
Emails customer replies (with their files) and failed deliveries to you, like the server version.

1. Add script properties `FORWARD_TO` (where replies go) and `WEBHOOK_TOKEN` (any random text).
2. **Deploy → New deployment → Web app**, *Execute as:* Me, *Who has access:* Anyone → **Deploy**. Copy the URL.
3. Meta app dashboard → **WhatsApp → Configuration → Webhook**:
   Callback URL = the web app URL + `?token=` + `WEBHOOK_TOKEN`, Verify token = `WEBHOOK_TOKEN`.
   **Verify and save**, then subscribe to the **messages** field.
4. The app must be **Live** for real messages (App settings → Basic → Privacy Policy URL, then publish).

Apps Script can't read request headers, so Meta's signature isn't checked; the secret token in the URL is what
keeps strangers out. Apps Script answers web requests through a redirect, which Meta may not accept; if
**Verify and save** fails, use the server version for replies.

## Limits
Free Gmail accounts can send about 100 emails a day (error reports and forwarded replies) and run triggers
for 90 minutes a day in total. Ten to twenty invoices a day is far below both.

## Tests
`node apps-script/test.js` runs `Code.gs` against fake Gmail and WhatsApp services.
