#!/usr/bin/env python3
"""Build pinned local whisper.cpp and download its checksum-verified English model.

No root privileges, OpenAI key, or audio upload. Requires cmake, C++, curl, tar.
Upstream: https://github.com/ggml-org/whisper.cpp/tree/v1.8.3
"""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]
class SetupError(Exception): pass
def atomic_write(path, text):
    path.write_text(text)

COMMIT = '2eeeba56e9edd762b4b38467bab96c2517163158'
SOURCE_SHA256 = '089b898aa83b24a8321e0fd554eeb0967fb03dd687e27f6374c72d3363b5b429'
MODEL_SHA1 = 'db8a495a91d927739e50b3fc1cc4c6b8f6c2d022'

def download(url, path, algorithm, digest):
    if path.exists() and hashlib.new(algorithm, path.read_bytes()).hexdigest() == digest:
        return
    partial = path.with_suffix(path.suffix + '.partial')
    subprocess.run(['curl', '-fL', '--retry', '2', url, '-o', str(partial)], check=True)
    if hashlib.new(algorithm, partial.read_bytes()).hexdigest() != digest:
        raise SetupError('Download checksum mismatch: ' + path.name)
    partial.replace(path)

def main():
    build = ROOT / '.build/whisper.cpp'
    build.mkdir(parents=True, exist_ok=True)
    archive = ROOT / '.build/whisper-source.tar.gz'
    download(f'https://github.com/ggml-org/whisper.cpp/archive/{COMMIT}.tar.gz', archive, 'sha256', SOURCE_SHA256)
    subprocess.run(['tar', '-xzf', str(archive), '--strip-components=1', '-C', str(build)], check=True)
    subprocess.run(['cmake', '-S', str(build), '-B', str(build / 'build'), '-DCMAKE_BUILD_TYPE=Release',
                    '-DGGML_NATIVE=OFF', '-DBUILD_SHARED_LIBS=OFF', '-DWHISPER_BUILD_TESTS=OFF', '-DWHISPER_BUILD_SERVER=OFF'], check=True)
    subprocess.run(['cmake', '--build', str(build / 'build'), '--target', 'whisper-cli', '-j', '8'], check=True)
    target = ROOT / '.tools/bin/whisper-cli'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(build / 'build/bin/whisper-cli', target)
    model = ROOT / '.tools/share/whisper/ggml-small.en.bin'
    model.parent.mkdir(parents=True, exist_ok=True)
    download('https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin', model, 'sha1', MODEL_SHA1)
    atomic_write(model.parent / 'VERSION', f'whisper.cpp {COMMIT}\nsmall.en sha1 {MODEL_SHA1}\n')
    print('Local STT ready: ' + str(target))

if __name__ == '__main__':
    main()
