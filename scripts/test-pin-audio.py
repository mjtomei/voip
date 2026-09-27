#!/usr/bin/env python3
"""Synthetic PIN IVR -> local TTS -> isolated Pulse device -> independent STT.

No provider calls, real credentials, or operator turns. Twilio's actual handset
voice and DTMF transport still require a separately arranged telephone test.
"""
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voip import Project, ROOT, atomic_write
from voice_acceptance import Capture, synthesize, transcribe, matches

project = Project()
directory = ROOT/'state/e2e'/('pin-ivr-'+time.strftime('%Y%m%d-%H%M%S'))
directory.mkdir(parents=True, mode=0o700)
script = """const {fixture,pin}=require('./twilio/pin-delivery-fixture');
(async()=>{const a=fixture(),b=fixture();
console.log(JSON.stringify({prompt:await a.invoke(),confirmation:await a.invoke({Digits:pin,round:'1'}),
cancellation:await b.invoke({Digits:'9',round:'1'})}));})();"""
response = subprocess.run(['node','-e',script],cwd=ROOT,capture_output=True,text=True,check=True)
phrases = json.loads(response.stdout)
sink = 'pin_test_' + uuid.uuid4().hex[:12]
module = project.pactl('load-module','module-null-sink','sink_name='+sink).strip()
results=[]
try:
    for name, xml in phrases.items():
        words=' '.join(node.text or '' for node in ET.fromstring(xml).iter('Say'))
        folder=directory/name
        synthesize(project,words,folder/'input.wav')
        with Capture(project,folder,source=sink+'.monitor') as capture:
            time.sleep(.3)
            subprocess.run([project.executable('paplay'),'--device='+sink,str(folder/'input.wav')],check=True,capture_output=True,timeout=90,env=project.pulse_env())
            time.sleep(.5)
        transcript=transcribe(capture.recording)
        required={'prompt':[r'press (?:9|nine) then pound',r'enter all (?:8|eight) digits'],
                  'confirmation':[r'access code is confirmed'],
                  'cancellation':[r'delivery is cancel(?:led|ed)',r'stop retrying']}[name]
        passed=matches(transcript['text'],required)
        if name=='prompt':
            # Verify the spoken synthetic digits independently of the TwiML text.
            parts=re.search(r'code is\s*(.*?)\s*enter all',transcript['text'],re.I|re.S)
            digit_words=dict(zip('zero one two three four five six seven eight nine'.split(),'0123456789'))
            tokens=re.findall(r'[a-z]+|[0-9]',parts[1].lower()) if parts else []
            heard=''.join(digit_words.get(t,t if t.isdigit() else '') for t in tokens)
            passed=passed and heard=='90765432'
        results.append({'case':name,'passed':passed,'stt':transcript,'recording':str(capture.recording)})
        print(name+': '+('PASS' if passed else 'FAIL'),flush=True)
finally:
    project.pactl('unload-module',module)
atomic_write(directory/'results.json',json.dumps({'boundary':'synthetic hosted handler; real local TTS/Pulse capture/STT; no telephony',
    'results':results},indent=2)+'\n')
print('Evidence: '+str(directory/'results.json'))
sys.exit(0 if all(r['passed'] for r in results) else 1)
