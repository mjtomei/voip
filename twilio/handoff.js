// Protected transfer endpoint. A short-lived ticket binds an already-authenticated phone call.
const { voiceAccess } = require('./access');
const { callRouting } = require('./call-routing');
exports.handler=async function(context,event,callback) {
  const response=new Twilio.twiml.VoiceResponse();
  const end=()=>{response.hangup();return callback(null,response);};
  const ticket=callRouting.verify(context,event.ticket);
  // Only validated, bounded identifiers and internal reason codes enter logs.
  const log=(reason,detail={})=>console.info(JSON.stringify({event:'voip_handoff',reason,detail,
    transfer_id:/^[0-9a-f-]{36}$/.test(ticket?.id || '')?ticket.id:null,
    stage:event.stage===undefined?'initial':event.stage==='result'?'result':'invalid'}));
  if(!ticket || ticket.purpose!=='handoff' || !callRouting.account(context,event) ||
     ticket.call_sid!==event.CallSid || event.From!==ticket.from || event.To!==ticket.to ||
     event.Direction!==ticket.direction || !/^[0-9a-f-]{36}$/.test(ticket.id || '') ||
     !/^[a-z0-9-]+\.sip\.twilio\.com$/.test(context.SIP_DOMAIN || '')){log('invalid_ticket_or_call');return end();}
  const number=event.Direction==='outbound-api'?event.To:event.From;
  const base=voiceAccess(context,number);
  if(!base || base.id!==ticket.recipient_id){log('recipient_denied');return end();}
  const source=async()=>{
    const saved=ticket.source_route,policy=voiceAccess.directoryPolicy(context,base);
    if(saved && /^dir_[0-9a-f]{24}$/.test(ticket.source_session || '') &&
       saved.session===ticket.source_session && policy && saved.policy_id===policy.id &&
       /^[A-Za-z0-9_.+-]{1,32}$/.test(saved.username || ''))
      return {...base,session:saved.session,username:saved.username};
    return callRouting.target(context,base,ticket.source_session);
  };
  let returning=false,access;
  if(event.stage==='result') {
    if(!['busy','failed','no-answer','canceled'].includes(event.DialCallStatus)){log('dial_finished');return end();}
    returning=true;
    log('target_dial_failed');
  }else if(event.stage!==undefined){log('invalid_stage');return end();}
  else {
    const diagnostic={};
    access=await callRouting.target(context,base,ticket.target_session,diagnostic);
    log(access?'target_resolved':'target_resolution_failed',diagnostic);
    returning=!access;
  }
  if(returning){
    access=await source();
    if(!access){
      log('source_unavailable');
      response.say('The transfer could not be completed, and the previous session is unavailable. Please call again.');
      return end();
    }
    log('returning_to_source');
    response.say('That session could not be reached. Returning you to the previous session.');
  }
  const options={timeout:20,timeLimit:14400,record:'do-not-record',
    callerId:returning?`voip-in-${base.id}-${event.CallSid}`:`voip-${ticket.id}`};
  if(!returning)options.action='/handoff?stage=result&ticket='+encodeURIComponent(event.ticket);
  response.dial(options).sip(`sip:${access.username}@${context.SIP_DOMAIN};transport=tls;secure=true`);
  return end();
};
