/**
 * Mail -> WhatsApp bridge for Google Apps Script: free, no server.
 *
 * Runs every minute on Google's servers inside the Gmail account the work
 * terminal emails. Every new email whose subject starts with a phone number is
 * sent to that number on WhatsApp through the official Cloud API: each
 * attachment as a document, using an approved message template.
 *
 *     Subject: 0501234567 Invoice 1042   ->  +972 50-123-4567 gets the PDF,
 *                                            "Invoice 1042" fills {{1}}
 *
 * Optionally, deployed as a web app, it receives WhatsApp webhooks and emails
 * customer replies and failed deliveries to FORWARD_TO.
 *
 * Same behaviour as ../mail-bridge/bridge.py. Settings live in
 * Project Settings -> Script properties (see README.md).
 */

class BridgeError extends Error {}

function config_() {
  const p = PropertiesService.getScriptProperties().getProperties();
  const need = (key) => {
    const value = (p[key] || '').trim();
    if (!value) throw new Error('Missing script property: ' + key);
    return value;
  };
  return {
    token: need('WA_TOKEN'),
    phoneNumberId: need('WA_PHONE_NUMBER_ID'),
    docTemplate: need('WA_DOC_TEMPLATE'),
    apiVersion: (p.WA_API_VERSION || 'v23.0').trim(),
    textTemplate: (p.WA_TEXT_TEMPLATE || '').trim(),
    lang: (p.WA_TEMPLATE_LANG || 'he').trim(),
    templateParams: Number(p.WA_TEMPLATE_PARAMS || 1),
    countryCode: (p.DEFAULT_COUNTRY_CODE || '972').trim().replace(/^\+/, ''),
    allowedSenders: (p.ALLOWED_SENDERS || '').split(',').map((s) => s.trim().toLowerCase()).filter(Boolean),
    notifySender: (p.NOTIFY_SENDER || 'errors').trim().toLowerCase(),
    forwardTo: (p.FORWARD_TO || '').trim(),
    webhookToken: (p.WEBHOOK_TOKEN || '').trim(),
    testPhone: (p.TEST_PHONE || '').trim(),
  };
}

// ------------------------------------------------------------------ parsing

/** 050-123-4567 / +972501234567 / 00972501234567 -> 972501234567 */
function normalizePhone_(raw, countryCode) {
  raw = String(raw).trim();
  let digits = raw.replace(/\D/g, '');
  if (raw.startsWith('+')) {
    // already international
  } else if (digits.startsWith('00')) {
    digits = digits.slice(2);
  } else if (digits.startsWith('0')) {
    digits = countryCode + digits.slice(1);
  }
  if (digits.length < 8 || digits.length > 15) {
    throw new BridgeError(`'${raw}' doesn't look like a phone number`);
  }
  return digits;
}

/** '0501234567 Invoice 1042' -> ['972501234567', 'Invoice 1042'] */
function parseSubject_(subject, countryCode) {
  const match = /^\s*(\+?\d[\d\-().]{6,}\d)\s*[:\-,;]?\s*([\s\S]*)$/.exec(subject || '');
  if (!match) {
    throw new BridgeError("The subject must start with the phone number, e.g. '0501234567 Invoice 1042'");
  }
  return [normalizePhone_(match[1], countryCode), match[2].trim()];
}

/** Template parameters can't hold newlines, tabs or long runs of spaces. */
function templateText_(text, limit) {
  return String(text || '').replace(/\s+/g, ' ').trim().slice(0, limit || 1000);
}

function addressOf_(from) {
  const match = /<([^>]+)>/.exec(from || '');
  return (match ? match[1] : from || '').trim().toLowerCase();
}

function senderAllowed_(address, allowed) {
  return allowed.some((a) => address === a || (a.startsWith('@') && address.endsWith(a)));
}

function buildTemplateMessage_(to, template, lang, params, document) {
  const components = [];
  if (document) {
    components.push({ type: 'header', parameters: [{ type: 'document', document }] });
  }
  if (params.length) {
    components.push({ type: 'body', parameters: params.map((p) => ({ type: 'text', text: templateText_(p) })) });
  }
  return {
    messaging_product: 'whatsapp',
    to,
    type: 'template',
    template: { name: template, language: { code: lang }, components },
  };
}

// ------------------------------------------------------------------ whatsapp

function graph_(c, path) {
  return `https://graph.facebook.com/${c.apiVersion}/${path}`;
}

function call_(c, url, options) {
  const resp = UrlFetchApp.fetch(url, Object.assign(
    { muteHttpExceptions: true, headers: { Authorization: 'Bearer ' + c.token } }, options));
  const code = resp.getResponseCode();
  let body = {};
  try { body = JSON.parse(resp.getContentText()); } catch (e) { /* not JSON */ }
  if (code >= 400) {
    const err = body.error || {};
    const detail = err.error_user_msg || err.message || resp.getContentText().slice(0, 300);
    throw new BridgeError(`WhatsApp API error ${code}: ${detail}`);
  }
  return body;
}

function upload_(c, blob) {
  // A Blob in the payload makes UrlFetchApp send multipart/form-data.
  return call_(c, graph_(c, c.phoneNumberId + '/media'), {
    method: 'post',
    payload: { messaging_product: 'whatsapp', type: blob.getContentType(), file: blob },
  }).id;
}

function sendMessage_(c, message) {
  return call_(c, graph_(c, c.phoneNumberId + '/messages'), {
    method: 'post', contentType: 'application/json', payload: JSON.stringify(message),
  }).messages[0].id;
}

function sendDocument_(c, to, blob, caption) {
  const mediaId = upload_(c, blob);
  const params = [caption || blob.getName()].slice(0, c.templateParams);
  return sendMessage_(c, buildTemplateMessage_(to, c.docTemplate, c.lang, params,
    { id: mediaId, filename: blob.getName() }));
}

function sendText_(c, to, text) {
  return sendMessage_(c, buildTemplateMessage_(to, c.textTemplate, c.lang, [text].slice(0, c.templateParams)));
}

const EXTENSIONS_ = {
  'application/pdf': '.pdf', 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
  'video/mp4': '.mp4', 'audio/ogg': '.ogg', 'audio/mpeg': '.mp3', 'audio/mp4': '.m4a',
};

/** Fetch a file a customer sent us. */
function download_(c, mediaId, filename) {
  const info = call_(c, graph_(c, mediaId), { method: 'get' });
  const resp = UrlFetchApp.fetch(info.url, {
    muteHttpExceptions: true, headers: { Authorization: 'Bearer ' + c.token },
  });
  if (resp.getResponseCode() >= 400) {
    throw new BridgeError(`Could not download media ${mediaId}: HTTP ${resp.getResponseCode()}`);
  }
  const mime = (info.mime_type || 'application/octet-stream').split(';')[0];
  return resp.getBlob().setContentType(mime).setName(filename || mediaId + (EXTENSIONS_[mime] || ''));
}

// ------------------------------------------------------------------ mailbox

/** Message ids we sent -> the email they came from, so later failures reach its sender. */
function remember_(wamid, origin) {
  CacheService.getScriptCache().put('wamid:' + wamid, JSON.stringify(origin), 21600);
}

/** Forward one email. Returns a short summary; throws BridgeError on bad input. */
function handleEmail_(msg, c) {
  const [to, caption] = parseSubject_(msg.getSubject(), c.countryCode);
  const attachments = msg.getAttachments({ includeInlineImages: false });
  const origin = { from: addressOf_(msg.getFrom()), subject: msg.getSubject() };

  if (attachments.length) {
    for (const att of attachments) {
      remember_(sendDocument_(c, to, att.copyBlob(), caption), origin);
    }
    return `Sent ${attachments.length} file(s) to +${to}: ${attachments.map((a) => a.getName()).join(', ')}`;
  }
  if (c.textTemplate) {
    const text = caption || msg.getPlainBody().trim();
    if (!text) throw new BridgeError('The email has no attachment and no text to send');
    remember_(sendText_(c, to, text), origin);
    return `Sent a text message to +${to}`;
  }
  throw new BridgeError('The email has no attachment to send');
}

function notify_(c, msg, ok, summary) {
  if (c.notifySender === 'none' || (ok && c.notifySender !== 'all')) return;
  const subject = (ok ? 'WhatsApp sent: ' : 'WhatsApp NOT sent: ') + msg.getSubject();
  GmailApp.sendEmail(addressOf_(msg.getFrom()), subject, summary);
}

/** Runs every minute (see setup). */
function processMailbox() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(1000)) return; // the previous run is still busy
  try {
    const c = config_();
    if (!c.allowedSenders.length) {
      throw new Error('Set ALLOWED_SENDERS to at least one address (or a domain like @company.com)');
    }
    for (const thread of GmailApp.search('in:inbox is:unread', 0, 20)) {
      for (const msg of thread.getMessages()) {
        if (!msg.isUnread()) continue;
        // Mark as read first so a failure can never send the same email twice.
        msg.markRead();
        const sender = addressOf_(msg.getFrom());
        if (!senderAllowed_(sender, c.allowedSenders)) {
          console.warn('ignored email from unlisted sender ' + sender);
          continue;
        }
        let ok = true;
        let summary;
        try {
          summary = handleEmail_(msg, c);
        } catch (e) {
          ok = false;
          summary = e instanceof BridgeError ? e.message : 'Unexpected error: ' + e.message;
          console.error(`not sent (${msg.getSubject()}): ${e.message}`);
        }
        notify_(c, msg, ok, summary);
      }
    }
  } finally {
    lock.releaseLock();
  }
}

// ------------------------------------------------------------------ incoming (web app)

const MEDIA_TYPES_ = ['image', 'video', 'audio', 'document', 'sticker'];

/** A one-line text version of any incoming WhatsApp message. */
function describe_(message) {
  const kind = message.type || '';
  const part = message[kind] || {};
  if (kind === 'text') return part.body || '';
  if (MEDIA_TYPES_.includes(kind)) return part.caption || `[${kind}]`;
  if (kind === 'button') return part.text || '';
  if (kind === 'interactive') return (part.button_reply || part.list_reply || {}).title || '';
  if (kind === 'reaction') return `[reaction ${part.emoji || ''}]`;
  if (kind === 'location') return `[location ${part.latitude},${part.longitude}] ${part.name || ''}`.trim();
  return `[${kind} message]`;
}

function forwardIncoming_(message, name, c) {
  const sender = message.from || '';
  const kind = message.type || '';
  let text = describe_(message);
  const attachments = [];
  if (MEDIA_TYPES_.includes(kind)) {
    try {
      attachments.push(download_(c, message[kind].id, message[kind].filename));
    } catch (e) {
      text += `\n\n(The ${kind} could not be downloaded: ${e.message})`;
    }
  }
  const who = '+' + sender + (name ? ` (${name})` : '');
  GmailApp.sendEmail(c.forwardTo, `WhatsApp from ${who}: ${templateText_(text, 60)}`,
    `${who} wrote on WhatsApp:\n\n${text}\n\n` +
    `To send them a file, email it to this address with the subject starting with +${sender}`,
    { attachments });
}

function reportFailure_(status, c) {
  const cached = CacheService.getScriptCache().get('wamid:' + status.id);
  const origin = cached ? JSON.parse(cached) : null;
  const err = (status.errors || [{}])[0];
  const reason = (err.error_data || {}).details || err.message || err.title || 'unknown';
  const recipient = status.recipient_id || '';
  let body = `WhatsApp could not deliver the message to +${recipient}.\nReason: ${reason} (code ${err.code})`;
  if (err.code === 131026) body += '\n\nThis usually means the number has no WhatsApp.';
  const to = origin ? origin.from : c.forwardTo;
  if (to) GmailApp.sendEmail(to, 'WhatsApp NOT delivered: ' + (origin ? origin.subject : '+' + recipient), body);
}

function handleWebhook_(payload, c) {
  for (const entry of payload.entry || []) {
    for (const change of entry.changes || []) {
      const value = change.value || {};
      const names = {};
      for (const contact of value.contacts || []) names[contact.wa_id] = (contact.profile || {}).name || '';
      for (const message of value.messages || []) {
        if (c.forwardTo) forwardIncoming_(message, names[message.from] || '', c);
      }
      for (const status of value.statuses || []) {
        if (status.status === 'failed') reportFailure_(status, c);
      }
    }
  }
}

/** Meta's webhook check: answer the challenge when the verify token matches. */
function doGet(e) {
  const p = (e && e.parameter) || {};
  const c = config_();
  if (p['hub.mode'] === 'subscribe' && c.webhookToken && p['hub.verify_token'] === c.webhookToken) {
    return ContentService.createTextOutput(p['hub.challenge']);
  }
  return ContentService.createTextOutput('ok');
}

/**
 * Apps Script can't read request headers, so Meta's signature can't be checked;
 * the callback URL carries ?token=WEBHOOK_TOKEN instead and anything else is dropped.
 */
function doPost(e) {
  const c = config_();
  if (!c.webhookToken || ((e && e.parameter) || {}).token !== c.webhookToken) {
    return ContentService.createTextOutput('forbidden');
  }
  try {
    handleWebhook_(JSON.parse(e.postData.contents), c);
  } catch (err) {
    console.error('could not handle webhook: ' + err.message);
  }
  return ContentService.createTextOutput('ok');
}

// ------------------------------------------------------------------ run these by hand

/** Run once: checks the settings and starts the every-minute mailbox check. */
function setup() {
  config_();
  for (const trigger of ScriptApp.getProjectTriggers()) {
    if (trigger.getHandlerFunction() === 'processMailbox') ScriptApp.deleteTrigger(trigger);
  }
  ScriptApp.newTrigger('processMailbox').timeBased().everyMinutes(1).create();
  console.log('Done. This Gmail inbox is now checked every minute.');
}

/** Sends a small test PDF to TEST_PHONE, without any email. */
function testSend() {
  const c = config_();
  if (!c.testPhone) throw new Error('Add the TEST_PHONE script property with your own number first');
  const to = normalizePhone_(c.testPhone, c.countryCode);
  const pdf = Utilities.newBlob('<h1>Test invoice</h1><p>Sent by the mail bridge.</p>', 'text/html', 'test.html')
    .getAs('application/pdf').setName('test.pdf');
  const id = sendDocument_(c, to, pdf, 'Test 1');
  console.log(`Sent test.pdf to +${to} (message id ${id})`);
}
