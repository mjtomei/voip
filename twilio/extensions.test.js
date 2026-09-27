const test = require('node:test');
const assert = require('node:assert/strict');
global.Twilio = require('twilio');
const incoming = require('./incoming').handler;
const authorize = require('./authorize').handler;
const mapping = {sessions:{default:'operator',project:'operator-project',review:'operator-review'},
  extensions:{'100':'default','200':'project','201':'review','12345':'review'}, recipients:[
  {id:'alice',number:'+12025550101',pin:'123456',sessions:['default','project','review'],incoming_session:'default'},
  {id:'bob',number:'+12025550102',pin:'87654321',sessions:['project','review'],incoming_session:'project'}]};
const context = {TWILIO_NUMBER:'+12025550100',SIP_DOMAIN:'example.sip.twilio.com',
  VOICE_ACCOUNT_SID:'AC'+'a'.repeat(32),CALL_REQUEST_SECRET:'ab'.repeat(32),VOICE_ACCESS_JSON:JSON.stringify(mapping)};
function initial(index=0) {const p=mapping.recipients[index];return {From:p.number,To:context.TWILIO_NUMBER,
  AccountSid:context.VOICE_ACCOUNT_SID,CallSid:'CA'+String(index+1).repeat(32),Direction:'inbound',Digits:p.pin};}
function invoke(event,config=context,fn=authorize) {
  let xml;fn(config,event,(err,r)=>{assert.equal(err,null);xml=r.toString();});return xml;
}
function selected(index=0,extension='200') {
  const input={...initial(index),Digits:extension};const xml=invoke(input);
  assert.match(xml,/<Gather/);assert.doesNotMatch(xml,/<Dial/);
  const action=xml.match(/action="([^"]+)"/)[1].replaceAll('&amp;','&');
  return {...initial(index),...Object.fromEntries(new URL(action,'https://example.twil.io').searchParams)};
}
test('plain number plus PIN connects directly without extension choice or waiting',()=>{
  for(const [index,destination] of [[0,'operator'],[1,'operator-project']]) {
    const entry=invoke(initial(index),context,incoming);
    assert.match(entry,new RegExp('numDigits="'+mapping.recipients[index].pin.length+'"'));
    assert.match(entry,/finishOnKey="#"/);
    const authorized=invoke(initial(index));
    assert.match(authorized,new RegExp('sip:'+destination+'@'));
    assert.doesNotMatch(authorized,/<Gather|<Pause|project extension|usual session/);
  }
});
test('saved contact extension digits before PIN select the intended persistent session',()=>{
  assert.match(invoke(selected(0,'200')),/sip:operator-project@/);
  assert.match(invoke(selected(0,'201')),/sip:operator-review@/);
  assert.match(invoke(selected(0,'12345')),/sip:operator-review@/);
  const bob=invoke(selected(1,'201'));
  assert.match(bob,/callerId="voip-in-bob"/);assert.match(bob,/sip:operator-review@/);
  assert.doesNotMatch(bob,/<Gather/);
});
test('extension selection grants no access and its callback contains no PIN or auth grant',()=>{
  const xml=invoke({...initial(),Digits:'200'});
  assert.match(xml,/stage=pin&amp;extension=200/);
  assert.match(xml,/numDigits="6"/);
  assert.doesNotMatch(xml,/<Dial|123456|route=|ticket=/);
  for(const digits of ['',undefined,'000000','200'])
    assert.doesNotMatch(invoke({...selected(),Digits:digits}),/<Dial/);
  assert.doesNotMatch(invoke({...initial(1),stage:'pin',extension:'200',Digits:'123456'}),/<Dial/);
});
test('unknown and unauthorized extensions cannot prompt for access or fall back',()=>{
  for(const digits of ['100','999','../default','default','200#','0200','*200']) {
    assert.doesNotMatch(invoke({...initial(1),Digits:digits}),/<Gather|<Dial/);
    assert.doesNotMatch(invoke({...initial(1),stage:'pin',extension:digits}),/<Dial/);
  }
});
test('every extension PIN callback rechecks the number, permissions and destination',()=>{
  const event=selected();
  for(const mutation of [{From:mapping.recipients[1].number},{To:'+12025550999'},
    {From:undefined},{From:'+12025550999'},{extension:undefined},{extension:'../default'},
    {stage:'extension',route:'old-auth-ticket'},{stage:'other'}])
    assert.doesNotMatch(invoke({...event,...mutation}),/<Dial/);
  const changed=structuredClone(mapping);changed.recipients[0].sessions=['default'];
  assert.doesNotMatch(invoke(event,{...context,VOICE_ACCESS_JSON:JSON.stringify(changed)}),/<Dial/);
});
test('wrong, empty and missing PIN never start a session or open a post-PIN menu',()=>{
  for(const digits of ['000000','',undefined,'1234567',{},'12345x'])
    assert.doesNotMatch(invoke({...initial(),Digits:digits}),/<Gather|<Dial/);
  assert.match(invoke({...initial(),session:'review',extension:'201',recipient:'bob'}),/sip:operator@/);
});
test('six-digit extensions are excluded to keep every extension shorter than a PIN',()=>{
  const changed=structuredClone(mapping);changed.extensions['654321']='review';
  assert.doesNotMatch(invoke({...initial(),Digits:'654321'},{...context,VOICE_ACCESS_JSON:JSON.stringify(changed)}),/<Gather|<Dial/);
});
test('unconfigured extensions retain ordinary PIN routing',()=>{
  const changed=structuredClone(mapping);delete changed.extensions;
  assert.match(invoke(initial(),{...context,VOICE_ACCESS_JSON:JSON.stringify(changed)}),/sip:operator@/);
  assert.doesNotMatch(invoke({...initial(),Digits:'200'},{...context,VOICE_ACCESS_JSON:JSON.stringify(changed)}),/<Gather|<Dial/);
});
