// Protected Function at /outgoing. Never log the event, ticket, or PIN.
// The same endpoint handles entry and the subsequent keypad submission.
const { voiceAccess } = require('./access');
const { callRouting } = require('./call-routing');
exports.handler = async function (context, event, callback) {
  const crypto = require('node:crypto');
  const response = new Twilio.twiml.VoiceResponse();
  const end = () => { response.hangup(); return callback(null, response); };
  const secret = context.CALL_REQUEST_SECRET || '';
  let request;
  try {
    if (!/^[0-9a-f]{64}$/.test(secret) || typeof event.ticket !== 'string' || event.ticket.length > 2048) return end();
    const parts = event.ticket.split('.');
    if (parts.length !== 2 || !/^[0-9a-f]{64}$/.test(parts[1]) || !/^[A-Za-z0-9_-]+$/.test(parts[0])) return end();
    const digest = crypto.createHmac('sha256', Buffer.from(secret, 'hex')).update(parts[0]).digest();
    if (!crypto.timingSafeEqual(digest, Buffer.from(parts[1], 'hex'))) return end();
    request = JSON.parse(Buffer.from(parts[0], 'base64url').toString());
    if (!request || typeof request !== 'object' || Array.isArray(request)) return end();
  } catch (_) { return end(); }
  let access = voiceAccess(context, event.To, request.session || 'default', true);
  if (!access && /^dir_[0-9a-f]{24}$/.test(request.session || '')) {
    const base = voiceAccess(context, event.To);
    const map = await voiceAccess.directoryMap(context, base);
    const id = map && Object.keys(map.targets).find(id => map.targets[id].session === request.session);
    access = voiceAccess.directoryTarget(context, base, map, id);
  }
  const now = Math.floor(Date.now() / 1000);
  if (request.v !== 1 || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(request.id || '') ||
      !Number.isInteger(request.issued) || !Number.isInteger(request.expires) ||
      request.issued > now + 30 || request.expires <= now || request.expires > request.issued + 600 ||
      !context.VOICE_ACCOUNT_SID || event.AccountSid !== context.VOICE_ACCOUNT_SID ||
      !/^CA[0-9a-fA-F]{32}$/.test(event.CallSid || '') || event.Direction !== 'outbound-api' ||
      !context.TWILIO_NUMBER || event.From !== context.TWILIO_NUMBER || request.from !== event.From ||
      !event.To || request.to !== event.To || !access ||
      (request.recipient && request.recipient !== access.id) ||
      !/^[a-z0-9-]+\.sip\.twilio\.com$/.test(context.SIP_DOMAIN || '') ||
      !/^[A-Za-z0-9_.+-]+$/.test(access.username)) return end();

  if (callRouting.enabled(context)) {
    try { await callRouting.write(context,access.id,event.CallSid,{created_at:request.issued,placed_at:typeof request.placed_at==='number'?request.placed_at:request.issued,session_id:access.session,
      direction:'outgoing',accepted:true,status:'awaiting_pin'}); } catch (_) {}
  }
  if (event.stage === undefined) {
    const gather = response.gather({ input: 'dtmf', numDigits: voiceAccess.pinLength(access),
      timeout: 10, action: `/outgoing?stage=authorize&ticket=${encodeURIComponent(event.ticket)}`, method: 'POST' });
    gather.say('This is your voice operator. Enter your access code to connect.');
    return end();
  }
  if (event.stage !== 'authorize') return end();
  if (!voiceAccess.verify(context, access, event.Digits)) return end();
  if (callRouting.enabled(context)) {
    try { await callRouting.write(context,access.id,event.CallSid,{authenticated:true,status:'dialing_session'}); } catch (_) {}
  }
  // The SIP-only caller identity binds the local connection to its prepared
  // agenda. It changes neither the handset caller ID nor the destination.
  response.dial({ ...(callRouting.enabled(context)?{action:'/call-status?phase=dial',method:'POST'}:{}), callerId: `voip-${request.id}`, timeout: 20, timeLimit: 14400, record: 'do-not-record' })
    .sip(`sip:${access.username}@${context.SIP_DOMAIN};transport=tls;secure=true`);
  return end();
};
