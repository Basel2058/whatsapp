// Runs Code.gs against fake Apps Script services: node apps-script/test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const CODE = fs.readFileSync(path.join(__dirname, 'Code.gs'), 'utf8');

function blob(name, type, data = 'x') {
  return {
    name, type, data,
    getName() { return this.name; },
    getContentType() { return this.type; },
    setName(n) { this.name = n; return this; },
    setContentType(t) { this.type = t; return this; },
    copyBlob() { return blob(this.name, this.type, this.data); },
  };
}

function mail(subject, { from = 'Terminal <terminal@company.com>', attachments = [], body = 'hi' } = {}) {
  return {
    unread: true,
    getSubject: () => subject,
    getFrom: () => from,
    getPlainBody: () => body,
    getAttachments: () => attachments,
    isUnread() { return this.unread; },
    markRead() { this.unread = false; },
  };
}

function response(code, json) {
  return {
    getResponseCode: () => code,
    getContentText: () => JSON.stringify(json),
    getBlob: () => blob('download', 'application/octet-stream'),
  };
}

function load({ props = {}, mails = [], fetch } = {}) {
  const fetched = [];
  const sent = [];
  const cache = new Map();
  const ctx = {
    console: { log() {}, warn() {}, error() {} },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperties: () => Object.assign({
          WA_TOKEN: 't', WA_PHONE_NUMBER_ID: '123', WA_DOC_TEMPLATE: 'document_send',
          ALLOWED_SENDERS: '@company.com',
        }, props),
      }),
    },
    UrlFetchApp: {
      fetch(url, options) {
        fetched.push({ url, options });
        if (fetch) return fetch(url, options);
        return url.endsWith('/media') ? response(200, { id: 'MEDIA1' })
          : response(200, { messages: [{ id: 'wamid.' + fetched.length }] });
      },
    },
    GmailApp: {
      search: () => [{ getMessages: () => mails }],
      sendEmail: (to, subject, body, options) => sent.push({ to, subject, body, options }),
    },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock() {} }) },
    CacheService: {
      getScriptCache: () => ({ put: (k, v) => cache.set(k, v), get: (k) => cache.get(k) || null }),
    },
    ContentService: { createTextOutput: (text) => ({ text }) },
  };
  vm.createContext(ctx);
  vm.runInContext(CODE, ctx);
  return { ctx, fetched, sent };
}

test('subject parsing', () => {
  const { ctx } = load();
  assert.deepEqual([...ctx.parseSubject_('0501234567 Invoice 1042', '972')], ['972501234567', 'Invoice 1042']);
  assert.deepEqual([...ctx.parseSubject_('050-123-4567: Quote 7', '972')], ['972501234567', 'Quote 7']);
  assert.deepEqual([...ctx.parseSubject_('+447911123456 x', '972')], ['447911123456', 'x']);
  assert.deepEqual([...ctx.parseSubject_('00972501234567', '972')], ['972501234567', '']);
  assert.throws(() => ctx.parseSubject_('Invoice 1042', '972'), /must start with the phone number/);
  assert.throws(() => ctx.normalizePhone_('12345', '972'), /doesn't look like a phone number/);
});

test('each attachment is uploaded and sent with the template', () => {
  const m = mail('0501234567 Invoice 1042', {
    attachments: [blob('a.pdf', 'application/pdf'), blob('b.pdf', 'application/pdf')],
  });
  const { ctx, fetched, sent } = load({ mails: [m] });
  ctx.processMailbox();

  const base = 'https://graph.facebook.com/v23.0/123';
  assert.deepEqual(fetched.map((f) => f.url), [`${base}/media`, `${base}/messages`, `${base}/media`, `${base}/messages`]);
  assert.equal(fetched[0].options.payload.file.getName(), 'a.pdf');
  assert.equal(fetched[0].options.headers.Authorization, 'Bearer t');
  const message = JSON.parse(fetched[1].options.payload);
  assert.equal(message.to, '972501234567');
  assert.equal(message.template.name, 'document_send');
  assert.deepEqual(message.template.components[0].parameters[0].document, { id: 'MEDIA1', filename: 'a.pdf' });
  assert.equal(message.template.components[1].parameters[0].text, 'Invoice 1042');
  assert.equal(m.unread, false);
  assert.equal(sent.length, 0); // success isn't reported unless NOTIFY_SENDER=all
});

test('without a template, files go as plain documents', () => {
  const m = mail('0501234567 Invoice 9', { attachments: [blob('a.pdf', 'application/pdf')] });
  const { ctx, fetched } = load({ mails: [m], props: { WA_DOC_TEMPLATE: '' } });
  ctx.processMailbox();
  const message = JSON.parse(fetched[1].options.payload);
  assert.equal(message.type, 'document');
  assert.deepEqual(message.document, { id: 'MEDIA1', filename: 'a.pdf', caption: 'Invoice 9' });
});

test('problems are emailed back and unlisted senders ignored', () => {
  const noFile = mail('0501234567 Invoice 1');
  const badSubject = mail('Invoice 2', { attachments: [blob('a.pdf', 'application/pdf')] });
  const stranger = mail('0501234567 spam', { from: 'x@evil.com', attachments: [blob('a.pdf', 'application/pdf')] });
  const { ctx, fetched, sent } = load({ mails: [noFile, badSubject, stranger] });
  ctx.processMailbox();

  assert.equal(fetched.length, 0);
  assert.deepEqual(sent.map((s) => [s.to, s.subject]), [
    ['terminal@company.com', 'WhatsApp NOT sent: 0501234567 Invoice 1'],
    ['terminal@company.com', 'WhatsApp NOT sent: Invoice 2'],
  ]);
  assert.match(sent[1].body, /must start with the phone number/);
  assert.ok([noFile, badSubject, stranger].every((m) => !m.unread));
});

test('API errors are reported', () => {
  const m = mail('0501234567', { attachments: [blob('a.pdf', 'application/pdf')] });
  const { ctx, sent } = load({
    mails: [m],
    fetch: () => response(400, { error: { message: 'Template name does not exist' } }),
  });
  ctx.processMailbox();
  assert.match(sent[0].body, /WhatsApp API error 400: Template name does not exist/);
});

test('webhook verification and token check', () => {
  const { ctx, sent } = load({ props: { WEBHOOK_TOKEN: 'secret', FORWARD_TO: 'office@company.com' } });
  const ok = ctx.doGet({ parameter: { 'hub.mode': 'subscribe', 'hub.verify_token': 'secret', 'hub.challenge': '42' } });
  assert.equal(ok.text, '42');
  const bad = ctx.doGet({ parameter: { 'hub.mode': 'subscribe', 'hub.verify_token': 'nope', 'hub.challenge': '42' } });
  assert.equal(bad.text, 'ok');

  const body = JSON.stringify({ entry: [{ changes: [{ value: {
    contacts: [{ wa_id: '972501234567', profile: { name: 'Dana' } }],
    messages: [{ from: '972501234567', type: 'text', text: { body: 'Thanks!\nGot it' } }],
  } }] }] });
  assert.equal(ctx.doPost({ parameter: {}, postData: { contents: body } }).text, 'forbidden');
  assert.equal(sent.length, 0);
  assert.equal(ctx.doPost({ parameter: { token: 'secret' }, postData: { contents: body } }).text, 'ok');
  assert.equal(sent[0].to, 'office@company.com');
  assert.equal(sent[0].subject, 'WhatsApp from +972501234567 (Dana): Thanks! Got it');
});

test('incoming document is downloaded and attached', () => {
  const { ctx, fetched, sent } = load({
    props: { WEBHOOK_TOKEN: 'secret', FORWARD_TO: 'office@company.com' },
    fetch: (url) => response(200, { url: 'https://lookaside.example/m', mime_type: 'application/pdf' }),
  });
  ctx.handleWebhook_({ entry: [{ changes: [{ value: { messages: [{
    from: '972501234567', type: 'document', document: { id: 'MEDIA9', filename: 'signed.pdf' },
  }] } }] }] }, ctx.config_());
  assert.deepEqual(fetched.map((f) => f.url), ['https://graph.facebook.com/v23.0/MEDIA9', 'https://lookaside.example/m']);
  const att = sent[0].options.attachments[0];
  assert.deepEqual([att.getName(), att.getContentType()], ['signed.pdf', 'application/pdf']);
});

test('failed delivery goes back to whoever emailed it', () => {
  const m = mail('0501234567 Invoice 7', { attachments: [blob('a.pdf', 'application/pdf')] });
  const { ctx, sent } = load({ mails: [m], props: { FORWARD_TO: 'office@company.com' } });
  ctx.processMailbox();
  ctx.handleWebhook_({ entry: [{ changes: [{ value: { statuses: [{
    id: 'wamid.2', status: 'failed', recipient_id: '972501234567',
    errors: [{ code: 131026, title: 'Message undeliverable' }],
  }] } }] }] }, ctx.config_());
  assert.equal(sent[0].to, 'terminal@company.com');
  assert.equal(sent[0].subject, 'WhatsApp NOT delivered: 0501234567 Invoice 7');
  assert.match(sent[0].body, /no WhatsApp/);
});
