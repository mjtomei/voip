// Deploy as a Protected Twilio Function at /authorize. Do not log event.Digits.
const { voiceAccess } = require('./access');
const { callRouting } = require('./call-routing');
exports.handler = async function (context, event, callback) {
  const response = new Twilio.twiml.VoiceResponse();
  let access = voiceAccess(context, event.From);
  let callbackOf = null;
  // Registered endpoints are dialed by their general AOR, without an edge suffix.
  const domainOK = /^[a-z0-9-]+\.sip\.twilio\.com$/.test(context.SIP_DOMAIN || '');
  const userOK = access && /^[A-Za-z0-9_.+-]+$/.test(access.username);
  if (!context.TWILIO_NUMBER || event.To !== context.TWILIO_NUMBER ||
      !event.From || !access || !domainOK || !userOK ||
      (event.stage !== undefined && event.stage !== 'pin')) {
    response.hangup();
    return callback(null, response);
  }
  const extensions = voiceAccess.extensions(context, event.From);
  const reserved = voiceAccess.manualExtensions(context);
  if (event.stage === 'pin') {
    if (event.directory_id !== undefined) {
      const map = await voiceAccess.directoryMap(context, access);
      access = map && map.generation === event.generation &&
        typeof event.extension === 'string' && /^[1-9][0-9]{0,4}$/.test(event.extension) &&
        map.routes[event.extension] === event.directory_id && !Object.hasOwn(reserved, event.extension) &&
        voiceAccess.directoryTarget(context, access, map, event.directory_id);
    } else {
      const session = typeof event.extension === 'string' &&
        /^[1-9][0-9]{0,4}$/.test(event.extension) && extensions[event.extension];
      access = session && (!event.selected_session || event.selected_session === session) && voiceAccess(context, event.From, session);
    }
  } else if (typeof event.Digits === 'string' && /^[1-9][0-9]{0,4}$/.test(event.Digits)) {
    // A saved contact sends extension# after answer, before the human enters
    // their PIN. Extensions are shorter than all PINs, so ordinary PIN entry
    // still submits immediately at its configured length with no extra menu.
    const session = extensions[event.Digits];
    let selected = session && voiceAccess(context, event.From, session);
    let binding = selected ? '&selected_session='+encodeURIComponent(session) : '';
    if (!selected && !Object.hasOwn(reserved, event.Digits) && voiceAccess.directoryPolicy(context, access)) {
      const map = await voiceAccess.directoryMap(context, access);
      const id = map && map.routes[event.Digits];
      selected = voiceAccess.directoryTarget(context, access, map, id);
      if (selected) binding = '&directory_id='+id+'&generation='+map.generation;
    }
    if (!selected) {
      response.say('That extension is unavailable.');
      response.hangup();
      return callback(null, response);
    }
    // Only the public, configured extension enters the callback. It grants no
    // authentication: this next request must supply the caller's personal PIN.
    const gather = response.gather({input:'dtmf', numDigits:voiceAccess.pinLength(selected),
      finishOnKey:'#', timeout:10, method:'POST',
      action:'/authorize?stage=pin&extension=' + encodeURIComponent(event.Digits) + binding});
    gather.say('Enter your access code.');
    response.hangup();
    return callback(null, response);
  } else if (callRouting.enabled(context) && event.recent_call !== undefined) {
    const recent = await callRouting.recent(context,access);
    access = recent && recent.call_sid === event.recent_call && recent.session === event.recent_session ? recent : null;
    if(access)callbackOf=recent.call_sid;
  } else if (voiceAccess.directoryPolicy(context, access)) {
    const map = await voiceAccess.directoryMap(context, access);
    access = map && map.home === event.directory_default && map.generation === event.generation &&
      voiceAccess.directoryTarget(context, access, map, map.home);
  }
  if (!voiceAccess.verify(context, access, event.Digits)) {
    response.hangup();
    return callback(null, response);
  }
  if (callRouting.enabled(context)) {
    if(!callRouting.account(context,event)) {response.hangup();return callback(null,response);}
    try { await callRouting.write(context,access.id,event.CallSid,{created_at:Math.floor(Date.now()/1000),
      session_id:access.session,direction:'incoming',status:'dialing_session',authenticated:true,callback_of:callbackOf}); } catch (_) {}
  }
  // Carry the PIN-verified callback binding directly to the local SIP observer.
  // Its connection notice must not race the periodic Sync history poll. Only
  // opaque call IDs cross this boundary; the prepared agenda stays local.
  const callerId = `voip-in-${access.id}` + (callRouting.enabled(context)?`-${event.CallSid}`:'') +
    (callbackOf?`-cb-${callbackOf}`:'');
  const dial = response.dial({ ...(callRouting.enabled(context)?{action:'/call-status?phase=dial',method:'POST'}:{}), timeout: 20, timeLimit: 14400, record: 'do-not-record', callerId });
  // Twilio's outbound SIP leg needs secure=true to negotiate SRTP as well as TLS.
  dial.sip(`sip:${access.username}@${context.SIP_DOMAIN};transport=tls;secure=true`);
  // Ends the caller leg if the SIP endpoint disconnects first.
  response.hangup();
  return callback(null, response);
};
