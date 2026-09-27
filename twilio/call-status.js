// Protected callback. Call SID and number must match an existing journal entry.
const { voiceAccess } = require('./access');
const { callRouting } = require('./call-routing');
exports.handler=async function(context,event,callback) {
  const response=new Twilio.twiml.VoiceResponse();
  try {
    if(!callRouting.enabled(context) || !callRouting.account(context,event))return callback(null,response);
    const number=event.Direction==='outbound-api'?event.To:event.From;
    const access=voiceAccess(context,number);
    if(!access || (event.Direction==='outbound-api'?event.From:event.To)!==context.TWILIO_NUMBER)return callback(null,response);
    const doc=await callRouting.read(context,access.id),old=doc.data.calls[event.CallSid];
    if(!old)return callback(null,response);
    const status=event.phase==='dial'?event.DialCallStatus:event.CallStatus;
    const terminal=['completed','busy','failed','no-answer','canceled'].includes(status);
    await callRouting.write(context,access.id,event.CallSid,{status,
      ...(event.phase==='dial'?{connected:status==='completed' && Number(event.DialCallDuration)>0}:{}),
      ...(terminal?{ended_at:Math.floor(Date.now()/1000)}:{})});
    if(old.callback_of)await callRouting.consumeCallback(context,access.id,event.CallSid);
    if(event.phase==='dial')response.hangup();
    return callback(null,response);
  }catch(_){return callback(Error('Call history update unavailable'));}
};
