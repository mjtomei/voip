const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
global.Twilio = require('twilio');
const outgoing = require('./outgoing').handler;
const secret = 'ab'.repeat(32);
const context = { VOICE_ACCOUNT_SID: 'AC' + 'a'.repeat(32), TWILIO_NUMBER: '+12025550100',
  OUTBOUND_ALLOWED_RECIPIENTS: '+12025550101', CALL_PIN: '123456', CALL_REQUEST_SECRET: secret,
  SIP_DOMAIN: 'example.sip.twilio.com', SIP_USERNAME: 'operator' };
const now = Math.floor(Date.now() / 1000);
const payload = { v: 1, id: crypto.randomUUID(), from: context.TWILIO_NUMBER,
  to: context.OUTBOUND_ALLOWED_RECIPIENTS, issued: now, expires: now + 600 };
function sign(value) {
  const body = Buffer.from(JSON.stringify(value)).toString('base64url');
  return body + '.' + crypto.createHmac('sha256', Buffer.from(secret, 'hex')).update(body).digest('hex');
}
const event = { AccountSid: context.VOICE_ACCOUNT_SID, CallSid: 'CA' + 'b'.repeat(32),
  From: payload.from, To: payload.to, Direction: 'outbound-api', ticket: sign(payload) };
function invoke(overrides = {}, config = {}) {
  let xml;
  outgoing({ ...context, ...config }, { ...event, ...overrides }, (error, response) => {
    assert.equal(error, null); xml = response.toString();
  });
  assert.ok(xml);
  return xml;
}
test('outgoing answer prompts for PIN before connecting; no project information or PIN spoken', () => {
  const xml = invoke();
  assert.match(xml, /<Gather[^>]*numDigits="6"/);
  assert.match(xml, /stage=authorize/);
  assert.match(xml, /Enter your access code/);
  assert.doesNotMatch(xml, /<Dial|123456/);
});
test('correct PIN connects the same registered endpoint with TLS and SRTP', () => {
  const xml = invoke({ stage: 'authorize', Digits: context.CALL_PIN });
  assert.match(xml, /sip:operator@example.sip.twilio.com;transport=tls;secure=true/);
  assert.match(xml, /record="do-not-record"/);
  assert.ok(xml.includes(`callerId="voip-${payload.id}"`));
  assert.match(xml, /<\/Dial><Hangup\/>/);
});
test('SIP agenda identity comes only from the signed request', () => {
  const xml = invoke({ stage: 'authorize', Digits: context.CALL_PIN, callerId: 'attacker', requestId: 'attacker' });
  assert.ok(xml.includes(`callerId="voip-${payload.id}"`));
  assert.doesNotMatch(xml, /attacker/);
});
for (const [name, overrides] of [
  ['inbound direction', { Direction: 'inbound' }],
  ['different account', { AccountSid: 'AC' + 'c'.repeat(32) }],
  ['different recipient', { To: '+12025550102' }],
  ['different origin', { From: '+12025550102' }],
  ['missing call identity', { CallSid: undefined }],
  ['missing ticket', { ticket: undefined }],
  ['bad signature', { ticket: sign(payload).slice(0, -1) + 'x' }],
  ['expired ticket', { ticket: sign({ ...payload, issued: now - 700, expires: now - 1 }) }],
  ['excessive ticket lifetime', { ticket: sign({ ...payload, expires: now + 700 }) }],
  ['future ticket', { ticket: sign({ ...payload, issued: now + 100, expires: now + 600 }) }],
  ['wrong ticket recipient', { ticket: sign({ ...payload, to: '+12025550102' }) }],
  ['malformed signed payload', { ticket: sign(null) }],
  ['unknown stage', { stage: 'skip-pin' }],
]) {
  test(name + ' fails closed even with correct PIN', () => {
    assert.equal(invoke({ stage: 'authorize', Digits: context.CALL_PIN, ...overrides }), '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>');
  });
}
for (const Digits of [undefined, '', '12345', '1234567', '654321']) {
  test('invalid or absent PIN never bridges: ' + String(Digits), () => {
    assert.doesNotMatch(invoke({ stage: 'authorize', Digits }), /<Dial/);
  });
}
test('missing or invalid hosted configuration fails closed', () => {
  for (const config of [{ CALL_REQUEST_SECRET: '' }, { OUTBOUND_ALLOWED_RECIPIENTS: '' },
    { VOICE_ACCOUNT_SID: '' }, { TWILIO_NUMBER: '' }, { SIP_DOMAIN: 'wrong.example' },
    { SIP_USERNAME: 'bad;param=1' }, { CALL_PIN: '' }]) {
    assert.doesNotMatch(invoke({ stage: 'authorize', Digits: context.CALL_PIN }, config), /<Dial/);
  }
});
