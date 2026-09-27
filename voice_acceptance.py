"""Real Pulse TTS/capture plus independent, offline STT acceptance assertions.

Never uses native transcript text as a speech oracle. Expected words are supplied
only to the assertion, never to Whisper. Artifacts contain test audio/context;
callers must isolate the device or explicitly arrange a telephony test.
"""
import array
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import wave

from voip import ROOT, SetupError, atomic_write, lock

RATE = 16000


def rms(samples):
    values = array.array('h')
    values.frombytes(samples[:len(samples) // 2 * 2])
    if sys.byteorder != 'little':
        values.byteswap()
    return math.sqrt(sum(float(x) * x for x in values) / len(values)) if values else 0


def save_wav(path, samples, rate=RATE):
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(samples)
    os.chmod(path, 0o600)


def normalize(text):
    return ' '.join(re.findall(r"[a-z0-9]+", text.lower()))


def matches(text, required, forbidden=()):
    text = normalize(text)
    def found(expression):
        return re.search(r'\b(?:' + expression + r')\b', text) is not None
    return all(found(x) for x in required) and not any(found(x) for x in forbidden)


def stt_paths():
    tools = Path(os.environ.get('VOIP_TOOLS_DIR', str(ROOT / '.tools')))
    return tools / 'bin/whisper-cli', tools / 'share/whisper/ggml-small.en.bin'


def transcribe(recording, output=None, *, start_seconds=0):
    """Only the captured WAV is input; no prompt, vocabulary hints, or reference."""
    output = Path(output or Path(recording).with_suffix(''))
    binary, model = stt_paths()
    if not binary.is_file() or not model.is_file():
        raise SetupError('Install local STT with python3 scripts/install-stt.py')
    with wave.open(str(recording)) as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (1, 2, RATE):
            raise SetupError('STT requires mono 16-bit 16 kHz capture')
        samples = stream.readframes(stream.getnframes())
        first_byte = max(0, int(start_seconds * RATE) * 2)
        if rms(samples[first_byte:]) < 20:
            raise SetupError('No audible output; refusing STT on silence')
    # Exclude only leading/trailing quiet, with a full second of padding. Long
    # empty tails can make Whisper hallucinate "you" repeatedly. The original
    # recording remains intact; disclose the exact analyzed interval.
    width = RATE // 5  # 100 ms of int16 PCM
    active = [i for i in range(first_byte, len(samples), width) if rms(samples[i:i + width]) > 50]
    if not active:
        raise SetupError('No speech-level signal in recording')
    begin = max(first_byte, active[0] - RATE * 2)
    end = min(len(samples), active[-1] + width + RATE * 2)
    offset_ms = begin * 1000 // (RATE * 2)
    duration_ms = max(1, (end - begin) * 1000 // (RATE * 2))
    started = time.monotonic()
    # Concurrent voice cases should not compete with several CPU-heavy STT jobs.
    with lock(Path(os.environ.get('XDG_RUNTIME_DIR', str(ROOT / 'state'))) / 'voip-whisper-stt.lock', blocking=True):
        inference_started = time.monotonic()
        result = subprocess.run([str(binary), '-m', str(model), '-f', str(recording), '-l', 'en',
                                 '-t', '4', '-nt', '-ot', str(offset_ms), '-d', str(duration_ms),
                                 '-otxt', '-of', str(output)],
                                capture_output=True, text=True, timeout=180)
        inference_seconds = time.monotonic() - inference_started
    if result.returncode:
        raise SetupError('Local STT failed; exit ' + str(result.returncode))
    transcript = Path(str(output) + '.txt').read_text().strip()
    os.chmod(Path(str(output) + '.txt'), 0o600)
    return {'engine': 'whisper.cpp v1.8.3', 'model': 'small.en', 'text': transcript,
            'seconds': round(inference_seconds, 3), 'queue_seconds': round(inference_started - started, 3),
            'expected_text_supplied_to_stt': False,
            'recording_offset_ms': offset_ms, 'recording_duration_ms': duration_ms}


def synthesize(project, text, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path.with_suffix('.txt'), text + '\n')
    subprocess.run([project.executable('espeak-ng'), '-s', '155', '-w', str(path), text],
                   check=True, timeout=30, env=project.pulse_env(), capture_output=True)
    os.chmod(path, 0o600)
    with wave.open(str(path)) as stream:
        if stream.getsampwidth() != 2 or rms(stream.readframes(stream.getnframes())) < 50:
            raise SetupError('TTS produced no speech')
        return stream.getnframes() / stream.getframerate()


def play(project, path):
    with wave.open(str(path)) as stream:
        duration = stream.getnframes() / stream.getframerate()
    subprocess.run([project.executable('paplay'), '--device=' + project.audio['incoming_sink'], str(path)],
                   check=True, timeout=duration + 10, env=project.pulse_env(), capture_output=True)


class Capture:
    def __init__(self, project, directory, source=None):
        self.project, self.directory = project, Path(directory)
        self.source = source or project.audio['outgoing_sink'] + '.monitor'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.raw = self.directory / 'capture.pcm'
        self.recording = self.directory / 'response.wav'
        self.process = None
        self.offset = 0
        self.first_signal = self.last_signal = None
        self.started = None
        self.verification_offset = 0
        self.marks = {}

    def __enter__(self):
        self.guard = lock(self.project.state / 'audio-acceptance.lock')
        self.guard.__enter__()
        try:
            self.output = self.raw.open('wb')
            os.chmod(self.raw, 0o600)
            self.started = time.monotonic()
            self.process = subprocess.Popen([self.project.executable('parec'), '--raw', '--format=s16le',
                '--rate=' + str(RATE), '--channels=1', '--latency-msec=20', '--device=' + self.source],
                stdout=self.output, stderr=subprocess.DEVNULL, env=self.project.pulse_env())
            time.sleep(.3)
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def poll(self):
        if self.process.poll() is not None:
            raise SetupError('Audio recorder stopped unexpectedly')
        with self.raw.open('rb') as data:
            data.seek(self.offset)
            new = data.read()
        # Actual output PCM, in 100 ms windows. These are recording offsets,
        # not speech-save timestamps or promises of what a handset played.
        for i in range(0, len(new) - RATE // 5 + 1, RATE // 5):
            if rms(new[i:i + RATE // 5]) > 100:
                when = (self.offset + i) / (RATE * 2)
                if self.first_signal is None:
                    self.first_signal = when
                self.last_signal = when
        self.offset += len(new) // (RATE // 5) * (RATE // 5)
        return self.offset / (RATE * 2)

    def snapshot(self, target=None):
        samples = self.raw.read_bytes()
        samples = samples[:len(samples) // 2 * 2]
        save_wav(target or self.recording, samples)
        return rms(samples)

    def after(self, label):
        """Exclude output received before an observed call/phase transition."""
        self.verification_offset = self.raw.stat().st_size / (RATE * 2)
        self.marks[label] = {'recording_seconds': self.verification_offset, 'observed_at': time.time()}

    def verify(self, required, *, predicate=lambda: True, forbidden=(), timeout=120, tick=None):
        deadline = time.monotonic() + timeout
        next_stt = 0
        last_stt = None
        state_ok = False
        try:
            while time.monotonic() < deadline:
                if tick:
                    tick()
                elapsed = self.poll()
                state_ok = bool(predicate())
                if (state_ok and self.last_signal is not None and self.last_signal >= self.verification_offset
                        and elapsed - self.last_signal >= 3
                        and elapsed >= next_stt):
                    self.snapshot()
                    last_stt = transcribe(self.recording, start_seconds=self.verification_offset)
                    if matches(last_stt['text'], required, forbidden):
                        break
                    next_stt = elapsed + 10
                time.sleep(.25)
            else:
                raise SetupError('Audio acceptance timed out: required speech or state not demonstrated')
        except BaseException as error:
            level = self.snapshot()
            if level > 20:
                try:
                    last_stt = transcribe(self.recording, start_seconds=self.verification_offset)
                except SetupError:
                    # Preserve the original failure and PCM even when all speech
                    # preceded the connection, or the recognizer itself failed.
                    pass
            self.evidence(False, required, forbidden, state_ok, last_stt, str(error))
            raise
        return self.evidence(True, required, forbidden, state_ok, last_stt)

    def evidence(self, passed, required, forbidden, state_ok, stt, error=None):
        result = {'passed': passed, 'session': self.project.session_id, 'thread_id': self.project.thread_id(),
            'capture_source': self.source, 'response_audio': str(self.recording),
            'required_speech_patterns': list(required), 'forbidden_speech_patterns': list(forbidden),
            'state_assertion_passed': state_ok, 'spoken_words_verified': bool(passed and stt),
            'stt': stt, 'error': error, 'first_output_signal_seconds': self.first_signal,
            'last_output_signal_seconds': self.last_signal,
            'verification_after_seconds': self.verification_offset, 'phase_marks': self.marks,
            'timing_basis': 'PCM offsets at local output monitor; not transcript-save or handset playback times'}
        atomic_write(self.directory / 'result.json', json.dumps(result, indent=2) + '\n')
        return result

    def __exit__(self, *args):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if hasattr(self, 'output'):
            self.output.close()
        if self.raw.exists() and not self.recording.exists():
            self.snapshot()
        self.raw.unlink(missing_ok=True)
        self.guard.__exit__(*args)


def ask(project, directory, text, required, **kwargs):
    """The caller's complete request enters through TTS, never appendText."""
    directory = Path(directory)
    wav = directory / 'request.wav'
    synthesize(project, text, wav)
    with Capture(project, directory) as capture:
        play(project, wav)
        return capture.verify(required, **kwargs)
