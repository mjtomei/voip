// Deploy as a Protected Twilio Function at /incoming.
// Twilio validates signed requests before running Protected Functions.
const { voiceAccess } = require('./access');
const { callRouting } = require('./call-routing');
exports.handler = async function (context, event, callback) {
  const response = new Twilio.twiml.VoiceResponse();
  const access = voiceAccess(context, event.From);
  if (!context.TWILIO_NUMBER || event.To !== context.TWILIO_NUMBER ||
      !event.From || !access || (callRouting.enabled(context) && !callRouting.account(context,event))) {
    response.reject({ reason: 'rejected' });
    return callback(null, response);
  }
  let action = '/authorize', selected = access;
  const recent = callRouting.enabled(context) ? await callRouting.recent(context, access) : null;
  if (recent) {
    selected = recent;
    action += '?recent_call='+recent.call_sid+'&recent_session='+encodeURIComponent(recent.session);
  } else if (voiceAccess.directoryPolicy(context, access)) {
    const map = await voiceAccess.directoryMap(context, access);
    selected = voiceAccess.directoryTarget(context, access, map, map && map.home) || access;
    action += map ? '?directory_default='+map.home+'&generation='+map.generation : '?directory_default=unavailable';
  }
  if (callRouting.enabled(context) && callRouting.account(context,event)) {
    try { await callRouting.write(context,access.id,event.CallSid,{created_at:Math.floor(Date.now()/1000),
      session_id:selected.session,direction:'incoming',status:'awaiting_pin',authenticated:false}); } catch (_) {}
  }
  const gather = response.gather({ input: 'dtmf', numDigits: voiceAccess.pinLength(access),
    timeout: 10, finishOnKey: '#', action, method: 'POST' });
  gather.say('Enter your access code.');
  response.hangup();
  return callback(null, response);
};
