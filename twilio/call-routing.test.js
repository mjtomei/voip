const test=require('node:test'),assert=require('node:assert/strict');
global.Twilio=require('twilio');
const incoming=require('./incoming').handler,authorize=require('./authorize').handler,status=require('./call-status').handler;
const handoff=require('./handoff').handler,{callRouting}=require('./call-routing');
const mapping={sessions:{default:'operator',project:'operator-project',private:'operator-private'},extensions:{'17':'project','10':'default'},recipients:[
 {id:'alice',number:'+12025550101',pin:'123456',sessions:['default','project'],incoming_session:'default'},
 {id:'bob',number:'+12025550102',pin:'654321',sessions:['default'],incoming_session:'default'}]};
const context={TWILIO_NUMBER:'+12025550100',SIP_DOMAIN:'test.sip.twilio.com',VOICE_ACCOUNT_SID:'AC'+'a'.repeat(32),
 CALL_REQUEST_SECRET:'ab'.repeat(32),VOICE_ACCESS_JSON:JSON.stringify(mapping),CALL_ROUTING_ENABLED:'1',
 PIN_SYNC_SERVICE_SID:'IS'+'a'.repeat(32),PIN_SYNC_KEY_SID:'SK'+'b'.repeat(32),PIN_SYNC_KEY_SECRET:'synthetic'};
const sid=n=>'CA'+String(n).repeat(32),now=()=>Math.floor(Date.now()/1000);
const event=()=>({From:mapping.recipients[0].number,To:context.TWILIO_NUMBER,AccountSid:context.VOICE_ACCOUNT_SID,CallSid:sid(9),Direction:'inbound'});
const invoke=(handler,e,c=context)=>new Promise((resolve,reject)=>handler(c,e,(err,r)=>err?reject(err):resolve(r.toString())));
function store(){
 const docs=new Map();let rev=0;const original=global.fetch;
 global.fetch=async(url,options={})=>{
  const fields=new URLSearchParams(options.body),name=fields.get('UniqueName')||url.split('/').at(-1),doc=docs.get(name);
  if(!options.body)return {ok:!!doc,status:doc?200:404,json:async()=>structuredClone(doc)};
  if(fields.get('UniqueName') && doc || options.headers['If-Match'] && options.headers['If-Match']!==doc?.revision)return {ok:false,status:412};
  const value={data:JSON.parse(fields.get('Data')),revision:String(++rev)};docs.set(name,value);
  return {ok:true,json:async()=>structuredClone(value)};
 };
 return {docs,restore:()=>{global.fetch=original;}};
}
async function placed(id,recipient='alice',session='project',created=now()){
 await callRouting.write(context,recipient,id,{created_at:created,direction:'outgoing',accepted:true,session_id:session,status:'no-answer',ended_at:created+30});
}
function params(xml){return Object.fromEntries(new URL(xml.match(/action="([^"]+)"/)[1].replaceAll('&amp;','&'),'https://test.twil.io').searchParams);}
test('recent call selects its session after personal PIN; explicit extension overrides it',async()=>{
 const s=store();try{
  await placed(sid(1));const start=await invoke(incoming,event());assert.match(start,/recent_call=/);assert.doesNotMatch(start,/<Dial/);
  const e={...event(),...params(start),Digits:'123456'};
  assert.match(await invoke(authorize,e),/sip:operator-project@/);
  assert.doesNotMatch(await invoke(authorize,{...e,Digits:'000000'}),/<Dial/);
  const selected=await invoke(authorize,{...e,Digits:'10'});
  assert.match(await invoke(authorize,{...event(),...params(selected),Digits:'123456'}),/sip:operator@/);
 }finally{s.restore();}
});
test('each retry refreshes the window; a status update does not; exactly three hours expires',async()=>{
 const s=store();try{
  await placed(sid(1),'alice','project',now()-10800);
  assert.doesNotMatch(await invoke(incoming,event()),/recent_call=/);
  await callRouting.write(context,'alice',sid(1),{status:'completed'});
  assert.doesNotMatch(await invoke(incoming,event()),/recent_call=/);
  await placed(sid(2));assert.match(await invoke(incoming,event()),new RegExp('recent_call='+sid(2)));
 }finally{s.restore();}
});
test('successful callback consumes latest route without reviving older calls; a retry starts a new window',async()=>{
 const s=store();try{
  await placed(sid(1),'alice','default',now()-100);await placed(sid(2),'alice','project',now()-20);
  const e={...event(),...params(await invoke(incoming,event())),Digits:'123456'};
  await invoke(authorize,e);
  assert.equal(s.docs.get('call-history-alice').data.calls[e.CallSid].callback_of,sid(2));
  assert.match(await invoke(incoming,{...event(),CallSid:sid(8)}),/recent_call=/);
  await invoke(status,{...event(),phase:'dial',DialCallStatus:'completed',DialCallDuration:'4'});
  assert.ok(s.docs.get('call-history-alice').data.calls[sid(2)].callback_consumed_at);
  assert.doesNotMatch(await invoke(incoming,{...event(),CallSid:sid(7)}),/recent_call=/);
  await callRouting.write(context,'alice',sid(2),{status:'completed',callback_consumed_at:null});
  assert.doesNotMatch(await invoke(incoming,{...event(),CallSid:sid(7)}),/recent_call=/);
  await placed(sid(3));assert.match(await invoke(incoming,{...event(),CallSid:sid(6)}),new RegExp('recent_call='+sid(3)));
 }finally{s.restore();}
});
test('failed callback and explicit extension leave the callback preference unconsumed',async()=>{
 const s=store();try{
  await placed(sid(1));const start=params(await invoke(incoming,event()));
  await invoke(authorize,{...event(),...start,Digits:'000000'});
  assert.equal(s.docs.get('call-history-alice').data.calls[sid(9)].callback_of,undefined);
  await invoke(authorize,{...event(),...start,Digits:'123456'});
  await invoke(status,{...event(),phase:'dial',DialCallStatus:'busy',DialCallDuration:'0'});
  assert.equal(s.docs.get('call-history-alice').data.calls[sid(1)].callback_consumed_at,undefined);
  const next={...event(),CallSid:sid(8)},begin=params(await invoke(incoming,next));
  const chosen=params(await invoke(authorize,{...next,...begin,Digits:'17'}));
  await invoke(authorize,{...next,...chosen,Digits:'123456'});
  await invoke(status,{...next,phase:'dial',DialCallStatus:'completed',DialCallDuration:'4'});
  assert.equal(s.docs.get('call-history-alice').data.calls[sid(8)].callback_of,null);
  assert.equal(s.docs.get('call-history-alice').data.calls[sid(1)].callback_consumed_at,undefined);
 }finally{s.restore();}
});
test('an older callback completing cannot consume a newer outgoing attempt',async()=>{
 const s=store();try{
  await placed(sid(1),'alice','project',now()-20);
  await invoke(authorize,{...event(),...params(await invoke(incoming,event())),Digits:'123456'});
  await placed(sid(2),'alice','default');
  await invoke(status,{...event(),phase:'dial',DialCallStatus:'completed',DialCallDuration:'4'});
  assert.equal(s.docs.get('call-history-alice').data.calls[sid(2)].callback_consumed_at,undefined);
  assert.match(await invoke(incoming,{...event(),CallSid:sid(8)}),new RegExp('recent_call='+sid(2)));
 }finally{s.restore();}
});
test('latest call wins; other recipients and unauthorized targets never grant access',async()=>{
 const s=store();try{
  await placed(sid(1),'alice','project',now()-100);await placed(sid(2),'alice','default',now()-10);
  assert.match(await invoke(authorize,{...event(),...params(await invoke(incoming,event())),Digits:'123456'}),/sip:operator@/);
  await placed(sid(3),'bob','project');
  const bob={...event(),From:mapping.recipients[1].number,CallSid:sid(8)};
  assert.doesNotMatch(await invoke(incoming,bob),/recent_call=/);
  await placed(sid(4),'alice','private');assert.doesNotMatch(await invoke(incoming,event()),/recent_call=/);
 }finally{s.restore();}
});
test('changing last caller during PIN entry cannot silently select another session',async()=>{
 const s=store();try{
  await placed(sid(1),'alice','project',now()-10);const first=params(await invoke(incoming,event()));
  await placed(sid(2),'alice','default');
  assert.doesNotMatch(await invoke(authorize,{...event(),...first,Digits:'123456'}),/<Dial/);
 }finally{s.restore();}
});
test('missed attempts remain unauthenticated; connected status is monotonic despite delayed callbacks',async()=>{
 const s=store();try{
  await invoke(incoming,event());await invoke(status,{...event(),CallStatus:'completed'});
  let row=s.docs.get('call-history-alice').data.calls[sid(9)];assert.equal(row.authenticated,false);assert.equal(row.connected,false);assert.ok(row.ended_at);
  await invoke(status,{...event(),phase:'dial',DialCallStatus:'completed',DialCallDuration:'5'});
  await invoke(status,{...event(),CallStatus:'in-progress'});
  row=s.docs.get('call-history-alice').data.calls[sid(9)];assert.equal(row.connected,true);assert.equal(row.status,'completed');
  await invoke(status,{...event(),From:mapping.recipients[1].number,CallStatus:'failed'});
  assert.equal(s.docs.get('call-history-alice').data.calls[sid(9)].status,'completed');
 }finally{s.restore();}
});
test('transfer ticket binds caller and phone leg; failed target returns to source without PIN',async()=>{
 const s=store();try{
  const e=event(),body={purpose:'handoff',id:'11111111-1111-4111-8111-111111111111',call_sid:e.CallSid,recipient_id:'alice',
   source_session:'default',target_session:'project',from:e.From,to:e.To,direction:e.Direction,expires:now()+120};
  const ticket=callRouting.sign(context,body);
  const xml=await invoke(handoff,{...e,ticket});assert.match(xml,/sip:operator-project@/);assert.doesNotMatch(xml,/<Gather/);
  const returned=await invoke(handoff,{...e,ticket,stage:'result',DialCallStatus:'busy'});
  assert.match(returned,/sip:operator@/);assert.match(returned,/Returning/);
  for(const change of [{CallSid:sid(8)},{From:mapping.recipients[1].number},{ticket:ticket.slice(0,-1)+'x'},{Direction:'outbound-api'}])
   assert.doesNotMatch(await invoke(handoff,{...e,ticket,...change}),/<Dial/);
  assert.doesNotMatch(await invoke(handoff,{...e,ticket:callRouting.sign(context,{...body,target_session:'private'})}),/sip:operator-private@/);
 }finally{s.restore();}
});

test('two calls placed in one second choose the later attempt, not SID order',async()=>{
 const s=store();try{
  const stamp=now()-1;await placed(sid(8),'alice','default',stamp);await placed(sid(1),'alice','project',stamp);
  await callRouting.write(context,'alice',sid(8),{placed_at:stamp+0.1});
  await callRouting.write(context,'alice',sid(1),{placed_at:stamp+0.9});
  assert.match(await invoke(incoming,event()),new RegExp('recent_call='+sid(1)));
 }finally{s.restore();}
});

const directoryHome='dir_'+'c'.repeat(24),directoryWork='dir_'+'d'.repeat(24);
function directoryContext(){
 const config=structuredClone(mapping);
 config.directories={id:'e'.repeat(64),home:directoryHome,recipients:['alice']};
 return {...context,VOICE_ACCESS_JSON:JSON.stringify(config)};
}
function transferBody(change={}){
 const e=event();return {purpose:'handoff',id:'11111111-1111-4111-8111-111111111111',call_sid:e.CallSid,
  recipient_id:'alice',source_session:'default',target_session:directoryWork,
  from:e.From,to:e.To,direction:e.Direction,expires:now()+120,...change};
}
function directoryMap(targets={}){
 return {data:{v:1,policy_id:'e'.repeat(64),home:directoryHome,generation:'f'.repeat(64),
  expires_at:now()+90,routes:{1:directoryWork},targets}};
}
test('route withdrawn during warmup returns to the source with an explanation, no PIN or retry loop',async()=>{
 const s=store(),ctx=directoryContext();try{
  s.docs.set('directory-routes',directoryMap());
  const ticket=callRouting.sign(ctx,transferBody());
  const xml=await invoke(handoff,{...event(),ticket},ctx);
  assert.match(xml,/Returning you to the previous session/);
  assert.match(xml,/sip:operator@/);assert.doesNotMatch(xml,/<Gather|action=|sip:.*dir_/);
  assert.equal((xml.match(/<Dial /g)||[]).length,1);
  assert.match(xml,new RegExp('callerId="voip-in-alice-'+sid(9)+'"'));
 }finally{s.restore();}
});
test('generated source return survives directory outage using only its signed, policy-bound route',async()=>{
 const ctx=directoryContext(),original=global.fetch;
 const body=transferBody({source_session:directoryHome,source_route:{session:directoryHome,
  username:'dir-'+directoryHome,policy_id:'e'.repeat(64)}});
 let requests=0;
 global.fetch=async()=>{requests++;throw Error('simulated network failure');};
 try{
  const ticket=callRouting.sign(ctx,body);
  const xml=await invoke(handoff,{...event(),ticket},ctx);
  assert.match(xml,new RegExp('sip:dir-'+directoryHome+'@'));
  assert.match(xml,/Returning/);assert.doesNotMatch(xml,/<Gather|action=/);
  assert.equal(requests,1); // Do not depend on another cloud fetch to recover.
  const returned=await invoke(handoff,{...event(),ticket,stage:'result',DialCallStatus:'no-answer'},ctx);
  assert.match(returned,new RegExp('sip:dir-'+directoryHome+'@'));
  assert.equal(requests,1);
  for(const change of [{CallSid:sid(8)},{From:mapping.recipients[1].number},
      {ticket:ticket.slice(0,-1)+'x'},
      {ticket:callRouting.sign(ctx,{...body,expires:now()-1})}])
   assert.doesNotMatch(await invoke(handoff,{...event(),ticket,...change},ctx),/<Dial/);
 }finally{global.fetch=original;}
});
test('source snapshot never grants target access and revoked policy cannot recover a generated source',async()=>{
 const s=store(),ctx=directoryContext();try{
  const body=transferBody({source_session:directoryHome,source_route:{session:directoryHome,
   username:'dir-'+directoryHome,policy_id:'e'.repeat(64)}});
  for(const change of [{policy_id:'a'.repeat(64)},{session:directoryWork},{username:'bad@external.test'}]){
   const ticket=callRouting.sign(ctx,{...body,source_route:{...body.source_route,...change}});
   const xml=await invoke(handoff,{...event(),ticket},ctx);
   assert.doesNotMatch(xml,/<Dial/);assert.match(xml,/previous session is unavailable/);
  }
  const revoked=JSON.parse(ctx.VOICE_ACCESS_JSON);revoked.directories.recipients=[];
  assert.doesNotMatch(await invoke(handoff,{...event(),ticket:callRouting.sign(ctx,body)},
   {...ctx,VOICE_ACCESS_JSON:JSON.stringify(revoked)}),/<Dial/);
  const ticket=callRouting.sign(ctx,{...body,target_session:'private'});
  const xml=await invoke(handoff,{...event(),ticket},ctx);
  assert.doesNotMatch(xml,/sip:operator-private@/);
  assert.match(xml,new RegExp('sip:dir-'+directoryHome+'@'));
 }finally{s.restore();}
});
test('legacy tickets can return to a generated source that remains published',async()=>{
 const s=store(),ctx=directoryContext();try{
  s.docs.set('directory-routes',directoryMap({[directoryHome]:{session:directoryHome,username:'dir-'+directoryHome}}));
  const ticket=callRouting.sign(ctx,transferBody({source_session:directoryHome}));
  assert.match(await invoke(handoff,{...event(),ticket},ctx),new RegExp('sip:dir-'+directoryHome+'@'));
 }finally{s.restore();}
});
test('chained transfers retain the parent and resolve the new generated target after an old result callback',async()=>{
 const s=store(),ctx=directoryContext();try{
  const first=callRouting.sign(ctx,transferBody({target_session:'project'}));
  assert.match(await invoke(handoff,{...event(),ticket:first},ctx),/sip:operator-project@/);
  const finished=await invoke(handoff,{...event(),ticket:first,stage:'result',DialCallStatus:'completed'},ctx);
  assert.doesNotMatch(finished,/<Dial|Returning/);
  s.docs.set('directory-routes',directoryMap({[directoryWork]:{session:directoryWork,username:'dir-'+directoryWork}}));
  const next=callRouting.sign(ctx,transferBody({id:'22222222-2222-4222-8222-222222222222',source_session:'project'}));
  const xml=await invoke(handoff,{...event(),ticket:next},ctx);
  assert.match(xml,new RegExp('sip:dir-'+directoryWork+'@'));
  assert.doesNotMatch(xml,/<Gather|Returning/);
 }finally{s.restore();}
});
test('handoff diagnostics identify missing routes without logging tickets, numbers, PINs or supplied text',async()=>{
 const s=store(),ctx=directoryContext(),original=console.info,logs=[];
 console.info=x=>logs.push(JSON.parse(x));
 try{
  s.docs.set('directory-routes',directoryMap({[directoryWork]:null}));
  const ticket=callRouting.sign(ctx,transferBody());
  await invoke(handoff,{...event(),ticket,Digits:'123456',note:'private message'},ctx);
  const failure=logs.find(x=>x.reason==='target_resolution_failed');
  assert.equal(failure.detail.reason,'target_not_published');
  assert.equal(failure.detail.generation,'f'.repeat(64));
  assert.equal(failure.detail.route_present,false);
  assert.ok(logs.some(x=>x.reason==='returning_to_source'));
  const text=JSON.stringify(logs);
  for(const value of [ticket,event().From,event().To,'123456','private message',ctx.CALL_REQUEST_SECRET,ctx.PIN_SYNC_KEY_SECRET])
   assert.ok(!text.includes(value));
 }finally{console.info=original;s.restore();}
});
