// Private Sync journal. No PINs, transcripts, agendas, or phone numbers are stored here.
const { voiceAccess } = require('./access');
const routingCrypto = require('node:crypto');
function routingEnabled(context) { return context.CALL_ROUTING_ENABLED === '1'; }
function routingName(id) {
  if (!/^[a-z][a-z0-9_-]{0,31}$/.test(id || '')) throw Error('Invalid recipient');
  return 'call-history-' + id;
}
async function routingRequest(context, recipient, fields, revision, create=false) {
  if (!/^IS[0-9a-fA-F]{32}$/.test(context.PIN_SYNC_SERVICE_SID || '') ||
      !/^SK[0-9a-fA-F]{32}$/.test(context.PIN_SYNC_KEY_SID || '') || !context.PIN_SYNC_KEY_SECRET) throw Error('Missing journal service');
  const base=`https://sync.twilio.com/v1/Services/${context.PIN_SYNC_SERVICE_SID}/Documents`;
  const headers={Authorization:'Basic '+Buffer.from(context.PIN_SYNC_KEY_SID+':'+context.PIN_SYNC_KEY_SECRET).toString('base64')};
  if(revision) headers['If-Match']=revision;
  if(fields) headers['Content-Type']='application/x-www-form-urlencoded';
  const response=await fetch(create?base:base+'/'+routingName(recipient),{redirect:'error',signal:AbortSignal.timeout(3000),
    method:fields?'POST':'GET',headers,...(fields?{body:new URLSearchParams(fields).toString()}:{})});
  if(!response.ok) {const error=Error('Journal unavailable');error.status=response.status;throw error;}
  return response.json();
}
async function routingRead(context, recipient) {
  try{return await routingRequest(context,recipient);}catch(e){if(e.status===404)return {data:{v:1,calls:{}},revision:null};throw e;}
}
async function routingWrite(context, recipient, sid, patch) {
  if(!/^CA[0-9a-fA-F]{32}$/.test(sid || ''))throw Error('Invalid call');
  for(let attempt=0;attempt<4;attempt++) {
    const doc=await routingRead(context,recipient),data=doc.data;
    if(data.v!==1 || !data.calls || typeof data.calls!=='object')throw Error('Invalid journal');
    const old=data.calls[sid] || {},now=Math.floor(Date.now()/1000);
    const next={...old,...patch,call_sid:sid,recipient_id:recipient,updated_at:now,
      authenticated:!!(old.authenticated || patch.authenticated),connected:!!(old.connected || patch.connected)};
    // A late started/answered callback cannot erase a terminal result.
    if(old.ended_at) {next.ended_at=old.ended_at;next.status=old.status;}
    if(old.callback_consumed_at) {next.callback_consumed_at=old.callback_consumed_at;next.callback_call_sid=old.callback_call_sid;}
    data.calls[sid]=next;
    const entries=Object.entries(data.calls).filter(([,c])=>c.created_at>=now-7*86400)
      .sort((a,b)=>b[1].created_at-a[1].created_at || b[0].localeCompare(a[0])).slice(0,100);
    data.calls=Object.fromEntries(entries);
    try{return await routingRequest(context,recipient,{Data:JSON.stringify(data),Ttl:'691200',
      ...(!doc.revision?{UniqueName:routingName(recipient)}:{})},doc.revision,!doc.revision);}
    catch(e){if(![409,412].includes(e.status))throw e;}
  }
  throw Error('Concurrent journal updates');
}
async function routingTarget(context, base, session, diagnostic={}) {
  diagnostic.reason='access_denied';
  if(!base)return null;
  // Recheck configured access; opaque generated identities never grant access by themselves.
  let config;
  try{config=context.VOICE_ACCESS_ASSET?JSON.parse(Runtime.getAssets()[context.VOICE_ACCESS_ASSET].open()):JSON.parse(context.VOICE_ACCESS_JSON || '{}');}catch(_){return null;}
  const person=(config.recipients || []).find(r=>r.id===base.id);
  if(!person)return null;
  const configured=voiceAccess(context,person.number,session);
  if(configured){diagnostic.reason='configured_route';return configured;}
  if(!/^dir_[0-9a-f]{24}$/.test(session || ''))return null;
  if(!voiceAccess.directoryPolicy(context,base)){diagnostic.reason='directory_forbidden';return null;}
  const map=await voiceAccess.directoryMap(context,base);
  if(!map){diagnostic.reason='directory_unavailable';return null;}
  diagnostic.generation=map.generation;
  const id=Object.keys(map.targets).find(k=>map.targets[k]?.session===session);
  const target=voiceAccess.directoryTarget(context,base,map,id);
  diagnostic.route_present=!!target;
  diagnostic.reason=target?'directory_route':'target_not_published';
  return target;
}
async function routingRecent(context, access) {
  if(!routingEnabled(context))return null;
  try {
    const doc=await routingRead(context,access.id),now=Math.floor(Date.now()/1000);
    const calls=Object.values(doc.data.calls || {}).filter(c=>c.direction==='outgoing' && c.accepted &&
      Number.isInteger(c.created_at) && c.created_at<=now && now-c.created_at<10800)
      .sort((a,b)=>(b.placed_at || b.created_at)-(a.placed_at || a.created_at) || b.call_sid.localeCompare(a.call_sid));
    const last=calls[0]; if(!last || last.callback_consumed_at)return null;
    const target=await routingTarget(context,access,last.session_id);
    return target?{...target,call_sid:last.call_sid}:null;
  }catch(_){return null;}
}
async function routingConsumeCallback(context, recipient, sid) {
  const doc=await routingRead(context,recipient),call=doc.data.calls[sid];
  if(!call || call.direction!=='incoming' || !call.authenticated || !call.connected ||
     !/^CA[0-9a-fA-F]{32}$/.test(call.callback_of || ''))return;
  const original=doc.data.calls[call.callback_of];
  if(!original || original.direction!=='outgoing' || !original.accepted || original.recipient_id!==recipient ||
     original.session_id!==call.session_id || original.callback_consumed_at)return;
  await routingWrite(context,recipient,call.callback_of,{callback_consumed_at:Math.floor(Date.now()/1000),callback_call_sid:sid});
}
function routingAccount(context,event) {
  return context.VOICE_ACCOUNT_SID && event.AccountSid===context.VOICE_ACCOUNT_SID && /^CA[0-9a-fA-F]{32}$/.test(event.CallSid || '');
}
function routingSign(context, data) {
  if(!/^[0-9a-f]{64}$/.test(context.CALL_REQUEST_SECRET || ''))throw Error('Missing signing key');
  const body=Buffer.from(JSON.stringify(data)).toString('base64url');
  return body+'.'+routingCrypto.createHmac('sha256',Buffer.from(context.CALL_REQUEST_SECRET,'hex')).update(body).digest('hex');
}
function routingVerify(context, ticket) {
  try {
    if(typeof ticket!=='string' || ticket.length>4096)return null;
    const [body,mac,...rest]=ticket.split('.');if(rest.length || !/^[0-9a-f]{64}$/.test(mac || ''))return null;
    const expected=routingSign(context,JSON.parse(Buffer.from(body,'base64url').toString()));
    if(ticket.length!==expected.length || !routingCrypto.timingSafeEqual(Buffer.from(ticket),Buffer.from(expected)))return null;
    const value=JSON.parse(Buffer.from(body,'base64url').toString()),now=Math.floor(Date.now()/1000);
    return Number.isInteger(value.expires) && value.expires>now && value.expires<=now+14460?value:null;
  }catch(_){return null;}
}
exports.callRouting={enabled:routingEnabled,read:routingRead,write:routingWrite,target:routingTarget,recent:routingRecent,consumeCallback:routingConsumeCallback,account:routingAccount,sign:routingSign,verify:routingVerify};
