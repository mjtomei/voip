const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const vm = require('node:vm');
const Twilio = require('twilio');
const pin = '90765432'; // Synthetic fixture, never a real recipient credential.
const master = Buffer.from('ab'.repeat(32), 'hex');
const derive = s => crypto.createHmac('sha256',master).update(s).digest();
const auth = {v:1,length:8,salt:'cd'.repeat(16),
  hash:crypto.scryptSync(crypto.createHmac('sha256',derive('pin-auth-v1')).update(pin).digest(),Buffer.from('cd'.repeat(16),'hex'),32,{N:16384,r:8,p:5}).toString('hex'),
  tag:crypto.createHmac('sha256',derive('pin-unique-v1')).update(pin).digest('hex')};
function fixture() {
  const id=crypto.randomUUID(), attempt=crypto.randomUUID();
  const nonce=crypto.randomBytes(12), cipher=crypto.createCipheriv('aes-256-gcm',derive('pin-delivery-v1'),nonce);
  cipher.setAAD(Buffer.from(id));
  const blob=Buffer.concat([cipher.update(pin),cipher.final(),cipher.getAuthTag()]);
  const context={VOICE_ACCOUNT_SID:'AC'+'a'.repeat(32),TWILIO_NUMBER:'+12025550100',PIN_SYNC_SERVICE_SID:'IS'+'b'.repeat(32),
    PIN_SYNC_KEY_SID:'SK'+'c'.repeat(32),PIN_SYNC_KEY_SECRET:'synthetic-key',PIN_MASTER_KEY:master.toString('hex'),
    VOICE_ACCESS_JSON:JSON.stringify({sessions:{default:'operator'},recipients:[{id:'alice',number:'+12025550101',
      sessions:['default'],incoming_session:'default',pin_auth:auth}]})};
  const event={AccountSid:context.VOICE_ACCOUNT_SID,From:context.TWILIO_NUMBER,To:'+12025550101',
    CallSid:'CA'+'d'.repeat(32),Direction:'outbound-api',delivery:id,attempt};
  let doc={revision:'1',data:{id,attempt,state:'pending',recipient_id:'alice',from:event.From,to:event.To,
    auth_tag:auth.tag,sealed:{nonce:nonce.toString('hex'),ciphertext:blob.toString('hex')}}};
  let conflict=false, offline=false;
  const fetch=async (url,options)=>{
    assert.match(url,/^https:\/\/sync.twilio.com\/v1\/Services\/IS[0-9a-f]{32}\/Documents\/pin-/);
    if(offline) throw new Error('test provider failure must not leak');
    if(options.method==='POST') {
      if(conflict || options.headers['If-Match']!==doc.revision) return {ok:false};
      doc={revision:String(Number(doc.revision)+1),data:JSON.parse(new URLSearchParams(options.body).get('Data'))};
    }
    return {ok:true,json:async()=>structuredClone(doc)};
  };
  const source=fs.readFileSync(__dirname+'/access.js','utf8')+'\n'+fs.readFileSync(__dirname+'/pin-delivery.js','utf8').replace("const { voiceAccess } = require('./access');",'');
  const sandbox={exports:{},Twilio,Buffer,require,fetch,URLSearchParams,AbortSignal};vm.runInNewContext(source,sandbox);
  return {context,event,get doc(){return doc;},set conflict(v){conflict=v;},set offline(v){offline=v;},
    invoke:(changes={},ctx={})=>new Promise((resolve,reject)=>sandbox.exports.handler({...context,...ctx},{...event,...changes},(err,response)=>err?reject(err):resolve(response.toString())))};
}

module.exports = {fixture, pin};
