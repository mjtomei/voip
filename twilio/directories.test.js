const test = require('node:test');
const assert = require('node:assert/strict');
global.Twilio = require('twilio');
const incoming = require('./incoming').handler;
const authorize = require('./authorize').handler;
const outgoing = require('./outgoing').handler;
const home='dir_'+'a'.repeat(24), work='dir_'+'b'.repeat(24);
const policy={id:'a'.repeat(64),home,recipients:['alice']};
const configuration={sessions:{default:'operator',team:'operator-team'},extensions:{100:'default'},directories:policy,
  recipients:[{id:'alice',number:'+12025550101',pin:'123456',sessions:['default','team'],incoming_session:'default'},
  {id:'bob',number:'+12025550102',pin:'654321',sessions:['default'],incoming_session:'default'}]};
const context={TWILIO_NUMBER:'+12025550100',SIP_DOMAIN:'example.sip.twilio.com',VOICE_ACCESS_JSON:JSON.stringify(configuration),
  PIN_SYNC_SERVICE_SID:'IS'+'a'.repeat(32),PIN_SYNC_KEY_SID:'SK'+'b'.repeat(32),PIN_SYNC_KEY_SECRET:'synthetic'};
const event={From:'+12025550101',To:context.TWILIO_NUMBER};
const fresh=()=>({v:1,policy_id:policy.id,home,generation:'c'.repeat(64),expires_at:Math.floor(Date.now()/1000)+90,
  routes:{1:work},targets:{[home]:{session:home,username:'operator-home'},[work]:{session:work,username:'operator-work'}}});
async function invoke(fn, e, data=fresh(), ctx=context) {
  const saved=global.fetch;
  global.fetch=async()=>({ok:true,json:async()=>({data})});
  try {let xml;await fn(ctx,e,(error,result)=>{assert.equal(error,null);xml=result.toString();});return xml;}
  finally {global.fetch=saved;}
}
function next(xml, digits){
  const action=xml.match(/action="([^"]+)"/)[1].replaceAll('&amp;','&');
  return {...event,...Object.fromEntries(new URL(action,'https://example.test').searchParams),Digits:digits};
}
test('plain PIN uses a dedicated home operator for an opted-in caller',async()=>{
  const entry=await invoke(incoming,event);
  const connected=await invoke(authorize,next(entry,'123456'));
  assert.match(connected,/sip:operator-home@/);
});
test('generated extension selects directory before PIN and no PIN enters its callback URL',async()=>{
  const selected=await invoke(authorize,{...event,Digits:'1'});
  assert.match(selected,/directory_id=dir_/);assert.doesNotMatch(selected,/<Dial|123456/);
  assert.match(await invoke(authorize,next(selected,'123456')),/sip:operator-work@/);
  assert.doesNotMatch(await invoke(authorize,next(selected,'000000')),/<Dial/);
});
test('renumbering during PIN entry cannot silently connect another directory',async()=>{
  const selected=await invoke(authorize,{...event,Digits:'1'});
  const map=fresh();map.routes[1]=home;map.generation='d'.repeat(64);
  assert.doesNotMatch(await invoke(authorize,next(selected,'123456'),map),/<Dial/);
});
test('expired, mismatched, absent and unready maps fail closed',async()=>{
  const selected=await invoke(authorize,{...event,Digits:'1'});
  for(const change of [{expires_at:0},{policy_id:'e'.repeat(64)},{targets:{}},{generation:'d'.repeat(64)}])
    assert.doesNotMatch(await invoke(authorize,next(selected,'123456'),{...fresh(),...change}),/<Dial/);
});
test('manual extensions keep precedence and work without a live directory map',async()=>{
  const selected=await invoke(authorize,{...event,Digits:'100'},null);
  assert.match(await invoke(authorize,next(selected,'123456'),null),/sip:operator@/);
  const changed=structuredClone(configuration);changed.extensions[100]='team';
  assert.doesNotMatch(await invoke(authorize,next(selected,'123456'),null,{...context,VOICE_ACCESS_JSON:JSON.stringify(changed)}),/<Dial/);
});
test('other recipients retain existing defaults and cannot enter directory sessions',async()=>{
  const other={...event,From:'+12025550102'};
  assert.match(await invoke(authorize,{...other,Digits:'654321'}),/sip:operator@/);
  assert.doesNotMatch(await invoke(authorize,{...other,Digits:'1'}),/<Gather|<Dial/);
  const selected=await invoke(authorize,{...event,Digits:'1'});
  assert.doesNotMatch(await invoke(authorize,{...next(selected,'654321'),...other}),/<Dial/);
});
test('signed outgoing requests can reach a generated directory after the ordinary PIN gate',async()=>{
  const ctx={...context,VOICE_ACCOUNT_SID:'AC'+'a'.repeat(32),CALL_REQUEST_SECRET:'ab'.repeat(32)};
  const now=Math.floor(Date.now()/1000);
  const request={v:1,id:'11111111-1111-1111-1111-111111111111',session:work,recipient:'alice',
    from:context.TWILIO_NUMBER,to:event.From,issued:now,expires:now+600};
  const body=Buffer.from(JSON.stringify(request)).toString('base64url');
  const signature=require('node:crypto').createHmac('sha256',Buffer.from(ctx.CALL_REQUEST_SECRET,'hex')).update(body).digest('hex');
  const call={AccountSid:ctx.VOICE_ACCOUNT_SID,CallSid:'CA'+'b'.repeat(32),Direction:'outbound-api',
    From:request.from,To:request.to,ticket:body+'.'+signature};
  assert.match(await invoke(outgoing,call,fresh(),ctx),/<Gather/);
  assert.match(await invoke(outgoing,{...call,stage:'authorize',Digits:'123456'},fresh(),ctx),/sip:operator-work@/);
  assert.doesNotMatch(await invoke(outgoing,{...call,stage:'authorize',Digits:'654321'},fresh(),ctx),/<Dial/);
  assert.doesNotMatch(await invoke(outgoing,{...call,stage:'authorize',Digits:'123456'},null,ctx),/<Dial/);
});
test('an unauthorized manual extension cannot fall through to a stale generated route',async()=>{
  const changed=structuredClone(configuration);
  changed.sessions.private='operator-private';changed.extensions[1]='private';
  const ctx={...context,VOICE_ACCESS_JSON:JSON.stringify(changed)};
  assert.doesNotMatch(await invoke(authorize,{...event,Digits:'1'},fresh(),ctx),/<Gather|<Dial/);
  const selected=await invoke(authorize,{...event,Digits:'1'});
  assert.doesNotMatch(await invoke(authorize,next(selected,'123456'),fresh(),ctx),/<Dial/);
});
