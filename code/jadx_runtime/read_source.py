"""Server-side independent reader; request paths/settings are from the trusted client."""

import fcntl
import hashlib
import json
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            h.update(chunk)
    return h.hexdigest()


def read_source(request):
    root = Path(request['root'])
    state = root / 'runtime/run/current_apk.txt'
    apk = Path(state.read_text().strip())
    expected = request['sha256']
    if digest(apk) != expected:
        raise RuntimeError('APK identity changed before independent read')
    timeout = request['timeout']
    started = time.monotonic()
    attempts = []
    raw_name = request['class_name']
    # Resolve the GUI alias back to the original DEX name, never guess an alias.
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = 'http://127.0.0.1:8650/minorguard-class-info?' + urllib.parse.urlencode({'class_name': raw_name})
        with opener.open(url, timeout=min(10, timeout)) as response:
            raw_name = json.load(response)['raw_name']
    except Exception as error:
        attempts.append({'step': 'resolve_raw_name', 'error': type(error).__name__})
    scratch = Path(tempfile.mkdtemp(prefix='independent-read-', dir=root / 'runtime/tmp'))
    # No concurrent extra 32 GB JVMs from multiple queries/clients.
    with (root / 'runtime/run/independent-read.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise RuntimeError('Independent read time budget exhausted')
        command = ['java', '-Xmx%dg' % request['heap_gb'], '-XX:ActiveProcessorCount=4',
                   '-cp', str(root / 'tools/minorguard-runtime/classes') + ':' +
                   str(root / 'tools/jadx/lib/jadx-1.5.6-all.jar'), 'IndependentRead', str(apk), raw_name, str(scratch)]
        with (scratch / 'process.log').open('wb') as log:
            try:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=remaining)
                attempts.append({'step': 'independent_full_apk', 'exit_code': process.returncode})
            except subprocess.TimeoutExpired:
                attempts.append({'step': 'independent_full_apk', 'error': 'timeout; child JVM terminated'})
    if digest(apk) != expected or Path(state.read_text().strip()) != apk:
        raise RuntimeError('APK identity changed during independent read')
    result = {'backend': 'independent_full_apk', 'apk_sha256': expected, 'raw_name': raw_name,
              'attempts': attempts, 'remote_evidence_dir': str(scratch), 'scope': 'whole_class',
              'limitations': ['独立解析完整 APK 的 DEX；不读取资源表；关闭内联，类名/行号可能与 GUI 不同。',
                              '方法读取恢复返回整个类，需按实际方法声明核对重载；这不是运行时验证。']}
    for key, file in (('code', 'class.java'), ('smali', 'class.smali')):
        path = scratch / file
        if path.is_file():
            result[key] = path.read_text(encoding='utf-8')
    path = scratch / 'metadata.json'
    if path.is_file():
        result['metadata'] = json.loads(path.read_text())
    return result
