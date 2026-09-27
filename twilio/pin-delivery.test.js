const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {fixture,pin} = require('./pin-delivery-fixture');
test('dedicated encrypted IVR speaks digits, offers opt-out first and requires full code',async()=>{
  const f=fixture(), xml=await f.invoke();
  assert.match(xml,/numDigits="8"/);assert.match(xml,/finishOnKey="#"/);
  assert.match(xml,/9, 0, 7, 6, 5, 4, 3, 2/);assert.ok(xml.indexOf('press 9')<xml.indexOf('9, 0, 7'));
  assert.match(xml,/Enter all 8 digits/);assert.doesNotMatch(xml,/<Dial/);
  assert.equal(f.doc.data.call_sid,f.event.CallSid);
});
test('entering the delivered code confirms and deletes encrypted prompt',async()=>{
  const f=fixture();await f.invoke();const xml=await f.invoke({Digits:pin,round:'1'});
  assert.match(xml,/confirmed/);assert.equal(f.doc.data.state,'confirmed');assert.equal(f.doc.data.sealed,undefined);
  assert.doesNotMatch(await f.invoke(),/<Say|<Gather/);
});
test('9 pound cancels without confirmation and old callbacks cannot resume delivery',async()=>{
  const f=fixture(),xml=await f.invoke({Digits:'9',round:'1'});
  assert.match(xml,/cancelled/);assert.equal(f.doc.data.state,'cancelled');assert.equal(f.doc.data.sealed,undefined);
  assert.doesNotMatch(xml,/9, 0, 7/);
  assert.doesNotMatch(await f.invoke({Digits:pin,round:'2'}),/<Say|<Gather/);assert.equal(f.doc.data.state,'cancelled');
});
test('wrong digits and pressing 1 never confirm; empty input repeats only within bounded call',async()=>{
  const f=fixture();
  for(const Digits of ['1','90765433','']) {
    assert.match(await f.invoke({Digits,round:'1'}),/<Gather/);assert.equal(f.doc.data.state,'pending');
  }
  assert.doesNotMatch(await f.invoke({Digits:'',round:'3'}),/<Say|<Gather/);
});
test('a PIN starting with 9 is distinguished from the single-digit stop command',async()=>{
  const f=fixture();await f.invoke({Digits:pin});assert.equal(f.doc.data.state,'confirmed');
});
test('provider identity, number, attempt, and bound call are enforced',async()=>{
  for(const change of [{AccountSid:'AC'+'f'.repeat(32)},{From:'+12025550999'},{To:'+12025550999'},
    {Direction:'inbound'},{attempt:crypto.randomUUID()},{delivery:'../another'}]) {
    const f=fixture();assert.doesNotMatch(await f.invoke(change),/<Say|<Gather/);assert.equal(f.doc.data.state,'pending');
  }
  const f=fixture();await f.invoke();assert.doesNotMatch(await f.invoke({CallSid:'CA'+'e'.repeat(32),Digits:pin}),/<Say|<Gather/);
});
test('cloud conflict/unavailability fails closed without falsely confirming or leaking errors',async()=>{
  for(const flag of ['conflict','offline']) {
    const f=fixture();await f.invoke();f[flag]=true;
    const xml=await f.invoke({Digits:pin});assert.doesNotMatch(xml,/<Say|<Gather|confirmed|failure/);assert.equal(f.doc.data.state,'pending');
  }
});
test('tampered encrypted PIN fails closed, while opt-out still works without decrypting',async()=>{
  const f=fixture();f.doc.data.sealed.ciphertext='00'+f.doc.data.sealed.ciphertext.slice(2);
  assert.doesNotMatch(await f.invoke(),/<Say|<Gather/);
  assert.match(await f.invoke({Digits:'9',round:'1'}),/cancelled/);
});
test('rotated PIN prevents the old delivery being read',async()=>{
  const f=fixture();f.doc.data.auth_tag='00'.repeat(32);
  assert.doesNotMatch(await f.invoke(),/<Say|<Gather/);
});
