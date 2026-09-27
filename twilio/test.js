const test = require('node:test');
const assert = require('node:assert/strict');
global.Twilio = require('twilio');
const incoming = require('./incoming').handler;
const authorize = require('./authorize').handler;

const context = {
  TWILIO_NUMBER: '+12025550100', ALLOWED_CALLERS: '+12025550101', CALL_PIN: '12345678',
  SIP_DOMAIN: 'example.sip.twilio.com', SIP_USERNAME: 'operator',
};
const event = { From: context.ALLOWED_CALLERS, To: context.TWILIO_NUMBER, Digits: context.CALL_PIN };

function invoke(handler, settings = context, request = event) {
  let xml;
  handler(settings, request, (error, response) => {
    assert.equal(error, null);
    xml = response.toString();
  });
  assert.ok(xml);
  return xml;
}

test('known caller gets DTMF authentication before any SIP connection', () => {
  const xml = invoke(incoming);
  assert.match(xml, /<Gather[^>]*input="dtmf"/);
  assert.match(xml, /numDigits="8"/);
  assert.match(xml, /action="\/authorize"/);
  assert.match(xml, /<Hangup\/>/);
  assert.doesNotMatch(xml, /<Dial|12345678/);
});

test('only an authenticated caller reaches the configured TLS and SRTP SIP destination', () => {
  const xml = invoke(authorize);
  assert.match(xml, /<Dial[^>]*timeLimit="14400"[^>]*record="do-not-record"/);
  assert.match(xml, /<Sip>sip:operator@example\.sip\.twilio\.com;transport=tls;secure=true<\/Sip>/);
  assert.match(xml, /<\/Dial><Hangup\/>/);
  assert.doesNotMatch(xml, /12345678/);
});

for (const [description, request] of [
  ['unknown caller', { ...event, From: '+12025550102' }],
  ['wrong called number', { ...event, To: '+12025550103' }],
  ['missing caller', { ...event, From: undefined }],
]) {
  test(`${description} cannot enter or bypass the PIN flow`, () => {
    assert.match(invoke(incoming, context, request), /<Reject/);
    assert.doesNotMatch(invoke(authorize, context, request), /<Dial/);
  });
}

for (const pin of ['', '1234567', '123456789', '87654321', undefined]) {
  test(`incorrect PIN ${JSON.stringify(pin)} cannot connect`, () => {
    assert.doesNotMatch(invoke(authorize, context, { ...event, Digits: pin }), /<Dial/);
  });
}

test('invalid configuration fails closed', () => {
  for (const override of [{ CALL_PIN: '' }, { SIP_DOMAIN: 'attacker.example' },
    { SIP_DOMAIN: 'example.sip.us1.twilio.com' }, { SIP_DOMAIN: 'example.sip.sydney.twilio.com' },
    { SIP_USERNAME: 'user;header=injection' }, { ALLOWED_CALLERS: '' }, { TWILIO_NUMBER: '' }]) {
    assert.doesNotMatch(invoke(authorize, { ...context, ...override }), /<Dial/);
  }
});
