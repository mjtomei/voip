const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const vm = require('node:vm');
global.Twilio = require('twilio');
const incoming = require('./incoming').handler;
const authorize = require('./authorize').handler;
const outgoing = require('./outgoing').handler;
const mapping = {sessions:{default:'operator',team:'operator-team'},recipients:[
  {id:'alice',number:'+12025550101',pin:'123456',sessions:['default','team'],incoming_session:'default'},
  {id:'bob',number:'+12025550102',pin:'65432100',sessions:['team'],incoming_session:'team'}]};
const context={TWILIO_NUMBER:'+12025550100',SIP_DOMAIN:'example.sip.twilio.com',
  VOICE_ACCOUNT_SID:'AC'+'a'.repeat(32),CALL_REQUEST_SECRET:'ab'.repeat(32),VOICE_ACCESS_JSON:JSON.stringify(mapping)};
function invoke(fn,event,config=context) {
  let xml; fn(config,event,(err,result)=>{assert.equal(err,null);xml=result.toString();});return xml;
}
function inbound(index,pin) { const p=mapping.recipients[index]; return {From:p.number,To:context.TWILIO_NUMBER,Digits:pin||p.pin}; }
function ticket(recipient,session) {
  const now=Math.floor(Date.now()/1000);
  const body=Buffer.from(JSON.stringify({v:1,id:crypto.randomUUID(),from:context.TWILIO_NUMBER,
    to:recipient.number,recipient:recipient.id,session,issued:now,expires:now+600})).toString('base64url');
  return body+'.'+crypto.createHmac('sha256',Buffer.from(context.CALL_REQUEST_SECRET,'hex')).update(body).digest('hex');
}
test('each number uses its own PIN length, PIN and incoming session',()=>{
  assert.match(invoke(incoming,inbound(0)),/numDigits="6"/);
  assert.match(invoke(incoming,inbound(1)),/numDigits="8"/);
  assert.match(invoke(authorize,inbound(0)),/callerId="voip-in-alice"/);
  assert.match(invoke(authorize,inbound(1)),/sip:operator-team@/);
  assert.doesNotMatch(invoke(authorize,inbound(1,'123456')),/<Dial/);
  assert.doesNotMatch(invoke(authorize,inbound(0,'65432100')),/<Dial/);
});
test('incoming query parameters cannot choose a different session',()=>{
  assert.match(invoke(authorize,{...inbound(1),session:'default',recipient:'alice'}),/sip:operator-team@/);
});
test('outgoing destination, signed session, and recipient must all agree',()=>{
  const p=mapping.recipients[1]; const event={AccountSid:context.VOICE_ACCOUNT_SID,CallSid:'CA'+'b'.repeat(32),
    From:context.TWILIO_NUMBER,To:p.number,Direction:'outbound-api',stage:'authorize',Digits:p.pin};
  assert.match(invoke(outgoing,{...event,ticket:ticket(p,'team')}),/sip:operator-team@/);
  assert.doesNotMatch(invoke(outgoing,{...event,ticket:ticket(p,'default')}),/<Dial/);
  assert.doesNotMatch(invoke(outgoing,{...event,ticket:ticket({...p,id:'alice'},'team')}),/<Dial/);
  assert.doesNotMatch(invoke(outgoing,{...event,ticket:ticket(p,'team'),Digits:'123456'}),/<Dial/);
});
test('invalid private configuration fails closed without falling back to legacy PIN',()=>{
  const config={...context,VOICE_ACCESS_JSON:'bad json',CALL_PIN:'123456',ALLOWED_CALLERS:inbound(0).From,SIP_USERNAME:'operator'};
  assert.doesNotMatch(invoke(authorize,inbound(0),config),/<Dial/);
});
test('bundled handlers load private Asset with no local module dependencies',()=>{
  for(const name of ['incoming','authorize','outgoing']) {
    const source=fs.readFileSync(__dirname+'/access.js','utf8')+'\n'+fs.readFileSync(__dirname+'/'+name+'.js','utf8').replace("const { voiceAccess } = require('./access');",'');
    const sandbox={exports:{},Twilio:global.Twilio,Buffer,require,Runtime:{getAssets:()=>({'/voice-access.json':{open:()=>JSON.stringify(mapping)}})}};
    vm.runInNewContext(source,sandbox);
    assert.equal(typeof sandbox.exports.handler,'function');
    if(name==='authorize') assert.match(invoke(sandbox.exports.handler,inbound(1),{...context,VOICE_ACCESS_ASSET:'/voice-access.json'}),/sip:operator-team@/);
  }
});
