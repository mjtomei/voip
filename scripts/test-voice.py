#!/usr/bin/env python3
"""Inject offline TTS into the phone audio path and verify a Codex shell result.

prepare: synthesize the probe only.
check-input: verify synthesized speech reaches the virtual microphone.
run: send the probe to an already connected voice operator and record its reply.

The complete request and fresh challenge are spoken through the virtual audio
device. No prompt is submitted through tmux, app-server, or a request file.
"""
import argparse
import array
import json
import math
from pathlib import Path
import secrets
import subprocess
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voip import ROOT, Project, SetupError, atomic_write, audio_up, lock
from voice_acceptance import ask


def rms(samples):
    values = array.array("h")
    values.frombytes(samples)
    if sys.byteorder != "little":
        values.byteswap()
    return math.sqrt(sum(float(x) * x for x in values) / len(values)) if values else 0


def save_wav(path, samples, rate):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check-input", "run"))
    parser.add_argument("--response-seconds", type=int, default=120)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--session", default="default")
    parser.add_argument("--marker-file", type=Path, help="Test-owned result path relative to the project root")
    args = parser.parse_args()
    if not 5 <= args.response_seconds <= 300:
        parser.error("response-seconds must be between 5 and 300")
    project = Project(args.root, args.session)
    directory = project.state / "voice" / "probe"
    directory.mkdir(parents=True, exist_ok=True)
    with lock(directory / "probe.lock"):
        result = project.root / args.marker_file if args.marker_file else directory / "result.txt"
        if not result.resolve().is_relative_to(project.root):
            raise SetupError("The test marker must be inside the selected project")
        old_result = result.read_text().strip() if result.exists() else ""
        nonce = old_result
        while nonce == old_result:
            nonce = str(100000 + secrets.randbelow(900000))
        phrase = "Voice input and shell access verified."
        digit_words = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
        spoken_digits = ", ".join(digit_words[int(digit)] for digit in nonce)
        # Put the challenge and location before the action in one utterance.
        # A sentence break after "run a test" can trigger realtime delegation
        # before the model has heard the nonce or path.
        spoken_path = str(result.relative_to(project.root)).replace('/', ' slash ').replace('.txt', ' dot T X T')
        prompt = ("With these six digits " + spoken_digits + " please use your shell to write them "
                  "without spaces into " + spoken_path + " relative to this project's root directory "
                  "then verify the file and say voice input and shell access verified.")
        if args.action == "run":
            from call_events import CallEvents
            if any(c["connected"] for c in CallEvents(project).load()["calls"].values()):
                raise SetupError("A phone call is active; use the arranged telephony test instead")
            audio_up(project)
            from voice_lifecycle import warm, release
            key = "explicit-tts-probe"
            warm(project, key, seconds=args.response_seconds+100, kind="test")
            try:
                evidence = ask(project, directory, prompt, ["voice input and shell access verified"],
                    predicate=lambda: result.exists() and result.read_text().strip() == nonce,
                    timeout=args.response_seconds)
            except Exception:
                saved = directory / 'result.json'
                evidence = json.loads(saved.read_text()) if saved.exists() else {'passed': False}
                evidence.update(passed=False, spoken_words_verified=False,
                    request_transport="tts-through-virtual-microphone",
                    shell_marker_matches=result.exists() and result.read_text().strip() == nonce)
                atomic_write(directory / "last-result.json", json.dumps(evidence, indent=2) + "\n")
                raise
            finally:
                release(project, key)
            evidence.update(request_transport="tts-through-virtual-microphone", shell_marker_matches=True)
            atomic_write(directory / "last-result.json", json.dumps(evidence, indent=2) + "\n")
            print(json.dumps(evidence, indent=2))
            print("PASS: fresh shell marker and independently transcribed spoken confirmation")
            return 0
        atomic_write(directory / "prompt.txt", prompt + "\n")
        prompt_wav = directory / "prompt.wav"
        subprocess.run([project.executable("espeak-ng"), "-s", "155", "-w", str(prompt_wav), prompt],
                       check=True, timeout=15, env=project.pulse_env())
        with wave.open(str(prompt_wav)) as stream:
            duration = stream.getnframes() / stream.getframerate()
            if stream.getsampwidth() != 2 or not rms(stream.readframes(stream.getnframes())) > 50:
                raise SetupError("TTS did not produce audible 16-bit audio")
        print(f"Prepared {duration:.1f}s of offline TTS: {prompt_wav}", flush=True)
        if args.action == "prepare":
            return 0
        audio_up(project)
        if args.action == "run":
            source = project.audio["outgoing_sink"] + ".monitor"
            recording = directory / "response.wav"
        else:
            source = project.audio["microphone"]
            recording = directory / "input-captured.wav"
        rate = project.audio["rate"]
        raw = directory / "capture.pcm"
        with raw.open("wb") as output:
            recorder = subprocess.Popen([project.executable("parec"), "--raw", "--format=s16le",
                f"--rate={rate}", "--channels=1", "--latency-msec=20", "--device=" + source],
                stdout=output, stderr=subprocess.PIPE, env=project.pulse_env())
            try:
                time.sleep(0.3)
                subprocess.run([project.executable("paplay"), "--device=" + project.audio["incoming_sink"], str(prompt_wav)],
                    env=project.pulse_env(), check=True, timeout=duration + 10)
                print("Speech injected through " + project.audio["incoming_sink"], flush=True)
                time.sleep(args.response_seconds if args.action == "run" else 0.3)
                if recorder.poll() is not None:
                    raise SetupError("Audio recording stream exited early")
            finally:
                recorder.terminate()
                recorder.communicate(timeout=5)
        samples = raw.read_bytes()
        save_wav(recording, samples, rate)
        raw.unlink()
        level = rms(samples)
        if args.action == "check-input":
            if level < 50:
                raise SetupError("Synthesized speech did not reach the virtual microphone")
            print(f"PASS: TTS reached {source}; recorded RMS {level:.0f}. This checks audio input only, not Codex.")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SetupError, OSError, subprocess.SubprocessError) as error:
        print("Error: " + str(error), file=sys.stderr)
        sys.exit(1)
