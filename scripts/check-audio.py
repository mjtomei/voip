#!/usr/bin/env python3
"""Send synthetic audio through both virtual paths and check for cross-routing."""
import array
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voip import Project, audio_up


def main():
    project = Project()
    audio_up(project)
    rate = project.audio["rate"]
    tone = array.array("h", (int(6000 * math.sin(2 * math.pi * 700 * i / rate)) for i in range(rate)))
    if sys.byteorder != "little":
        tone.byteswap()
    sources = [project.audio["microphone"], project.audio["outgoing_sink"] + ".monitor"]
    sinks = [project.audio["incoming_sink"], project.audio["outgoing_sink"]]
    options = ["--raw", "--format=s16le", f"--rate={rate}", "--channels=1", "--latency-msec=20"]
    for expected, sink in enumerate(sinks):
        with tempfile.TemporaryFile() as first, tempfile.TemporaryFile() as second:
            streams = [first, second]
            recorders = []
            try:
                for source, stream in zip(sources, streams):
                    recorders.append(subprocess.Popen([project.executable("parec"), *options, "--device=" + source],
                        stdout=stream, stderr=subprocess.PIPE, env=project.pulse_env()))
                time.sleep(0.3)
                subprocess.run([project.executable("pacat"), *options, "--device=" + sink],
                    input=tone.tobytes(), env=project.pulse_env(), check=True, timeout=10)
                time.sleep(0.2)
                if any(p.poll() is not None for p in recorders):
                    raise RuntimeError("A virtual recording stream exited early")
            finally:
                for recorder in recorders:
                    recorder.terminate()
                    recorder.communicate(timeout=5)
            levels = []
            for stream in streams:
                stream.seek(0)
                samples = array.array("h")
                samples.frombytes(stream.read())
                if sys.byteorder != "little":
                    samples.byteswap()
                if len(samples) < rate // 2:
                    raise RuntimeError("Too few recorded samples")
                levels.append(math.sqrt(sum(float(x) * x for x in samples) / len(samples)))
            if levels[expected] < 500 or levels[1 - expected] > 5:
                raise RuntimeError(f"Routing failed for {sink}: RMS levels {levels}")
            print(f"PASS {sink} -> {sources[expected]}: signal {levels[expected]:.0f}, other path {levels[1 - expected]:.0f}")
    print("Both virtual paths carry audio independently. This does not test a phone call or Codex voice.")


if __name__ == "__main__":
    main()
