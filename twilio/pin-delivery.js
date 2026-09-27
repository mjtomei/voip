// Protected Function. PINs belong only in this IVR, never logs or agent context.
const { voiceAccess } = require('./access');
exports.handler = async function (context, event, callback) {
  const crypto = require('node:crypto');
  const response = new Twilio.twiml.VoiceResponse();
  const end = () => { response.hangup(); return callback(null, response); };
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  try {
    if (!uuid.test(event.delivery || '') || !uuid.test(event.attempt || '') ||
        !/^CA[0-9a-fA-F]{32}$/.test(event.CallSid || '') || event.Direction !== 'outbound-api' ||
        !context.VOICE_ACCOUNT_SID || event.AccountSid !== context.VOICE_ACCOUNT_SID ||
        !context.TWILIO_NUMBER || event.From !== context.TWILIO_NUMBER ||
        !/^IS[0-9a-fA-F]{32}$/.test(context.PIN_SYNC_SERVICE_SID || '') ||
        !/^SK[0-9a-fA-F]{32}$/.test(context.PIN_SYNC_KEY_SID || '') || !context.PIN_SYNC_KEY_SECRET) return end();
    const url = `https://sync.twilio.com/v1/Services/${context.PIN_SYNC_SERVICE_SID}/Documents/pin-${event.delivery}`;
    const headers = {Authorization: 'Basic ' + Buffer.from(context.PIN_SYNC_KEY_SID + ':' + context.PIN_SYNC_KEY_SECRET).toString('base64')};
    async function request(fields, revision) {
      const result = await fetch(url, {method: fields ? 'POST' : 'GET', redirect:'error',
        headers: {...headers, ...(fields ? {'Content-Type':'application/x-www-form-urlencoded', 'If-Match':revision} : {})},
        body: fields ? new URLSearchParams({Data:JSON.stringify(fields)}).toString() : undefined,
        signal: AbortSignal.timeout(3000)});
      if (!result.ok) throw new Error('Receipt unavailable');
      return result.json();
    }
    let doc = await request();
    let d = doc.data;
    if (!d || d.state !== 'pending' || d.id !== event.delivery || d.attempt !== event.attempt ||
        d.from !== event.From || d.to !== event.To || (d.call_sid && d.call_sid !== event.CallSid)) return end();
    // Bind the first signed provider webhook to this unique attempt. A lost
    // create response can then be recovered without a duplicate dial.
    if (!d.call_sid) {
      doc = await request({...d, call_sid:event.CallSid}, doc.revision);
      d = doc.data;
    }
    async function settle(state) {
      // Replace, don't merge: remove encrypted prompt immediately in the cloud,
      // even if the local dispatcher is offline. CAS defeats stale callbacks.
      await request({id:d.id, recipient_id:d.recipient_id, from:d.from, to:d.to,
        auth_tag:d.auth_tag, attempt:d.attempt, call_sid:event.CallSid,
        state, settled_at:Math.floor(Date.now()/1000), settled_by:'recipient'}, doc.revision);
    }
    // Cancel before loading/decrypting the PIN; this is not confirmation.
    if (event.Digits === '9') {
      await settle('cancelled');
      response.say('This PIN delivery is cancelled. We will stop retrying it. Goodbye.');
      return end();
    }
    const access = voiceAccess(context, event.To);
    if (!access || access.id !== d.recipient_id || !access.pin_auth || access.pin_auth.tag !== d.auth_tag) return end();
    if (typeof event.Digits === 'string' && voiceAccess.verify(context, access, event.Digits)) {
      await settle('confirmed');
      response.say('Your access code is confirmed. Goodbye.');
      return end();
    }
    const round = event.round === undefined ? 0 : Number(event.round);
    if (!Number.isInteger(round) || round < 0 || round >= 3) return end();
    if (!/^[0-9a-f]{64}$/.test(context.PIN_MASTER_KEY || '') || !d.sealed ||
        !/^[0-9a-f]{24}$/.test(d.sealed.nonce || '') || !/^[0-9a-f]{44,56}$/.test(d.sealed.ciphertext || '')) return end();
    const key = crypto.createHmac('sha256', Buffer.from(context.PIN_MASTER_KEY, 'hex')).update('pin-delivery-v1').digest();
    const blob = Buffer.from(d.sealed.ciphertext, 'hex');
    const cipher = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(d.sealed.nonce, 'hex'));
    cipher.setAAD(Buffer.from(d.id)); cipher.setAuthTag(blob.subarray(-16));
    const pin = Buffer.concat([cipher.update(blob.subarray(0, -16)), cipher.final()]).toString();
    if (!voiceAccess.verify(context, access, pin)) return end();
    const gather = response.gather({input:'dtmf', finishOnKey:'#', numDigits:voiceAccess.pinLength(access),
      timeout:15, actionOnEmptyResult:true, method:'POST',
      action:`/pin-delivery?delivery=${d.id}&attempt=${d.attempt}&round=${round+1}`});
    gather.say('This is an access code delivery. If this call is not for you, or you want to stop these delivery calls, press 9, then pound.');
    if (round && event.Digits) gather.say('That entry did not match.');
    gather.say('Your new access code is: ' + [...pin].join(', ') + '.');
    gather.say(`Enter all ${pin.length} digits of that code to confirm delivery.`);
    return end();
  } catch (_) { return end(); } // Never echo events, provider errors, digits, or ciphertext.
};
