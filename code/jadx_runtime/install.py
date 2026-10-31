"""Idempotent deployment for the existing managed 1.5.6 / plugin 6.5.0 setup."""

import hashlib
import json
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path


def install(request):
    root = Path(request['root'])
    for name in ('minorguard-xvfb', 'minorguard-jadx', 'minorguard-mcp'):
        check = subprocess.run(['tmux', 'show-environment', '-t', '=' + name, 'MG_MANAGED_ROOT'],
                               capture_output=True, text=True)
        if check.returncode == 0 and check.stdout.strip() != 'MG_MANAGED_ROOT=' + str(root):
            raise RuntimeError('Unmanaged session; deployment refused')
    destination = root / 'tools/minorguard-runtime'
    destination.mkdir(mode=0o700, exist_ok=True)
    signature = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    stamp = destination / 'installed.json'
    if stamp.is_file():
        recorded = json.loads(stamp.read_text())
        expected = recorded.get('file_hashes', {})
        if (recorded.get('signature') == signature and expected and all(
                Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                for path, digest in expected.items())):
            return {'changed': False, 'signature': signature}
    backup = destination / ('backup-' + time.strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(mode=0o700)
    def update(path, content):
        if path.is_file():
            shutil.copy2(path, backup / (path.name + '.previous'))
        path.write_text(content, encoding='utf-8')
        path.chmod(0o600)
    env = root / 'scripts/env.sh'
    text, changes = re.subn(r'-Xmx\d+g\b', '-Xmx%dg' % request['heap_gb'], env.read_text())
    if changes != 1:
        raise RuntimeError('Unknown JAVA_OPTS layout; not overwritten')
    # Upgrade the underlying MCP -> plugin timeout too (otherwise it fails at 60s).
    mcp_config = root / 'tools/jadx-mcp-server/src/server/config.py'
    config_text, changes = re.subn(r'JADX_DEFAULT_TIMEOUT = [\d.]+', 'JADX_DEFAULT_TIMEOUT = 180.0', mcp_config.read_text())
    if changes != 1:
        raise RuntimeError('Unknown MCP config; not overwritten')
    # Manual start must use the same real readiness contract as the workflow.
    start = root / 'scripts/start_static.sh'
    start_text = start.read_text().replace('$(seq 1 90)', '$(seq 1 600)')
    start_text = start_text.replace(
        "curl --noproxy '*' -fsS --max-time 2 http://127.0.0.1:8650/health >/dev/null 2>&1",
        "python3 \"$MG_ROOT/tools/minorguard-runtime/probe_ready.py\" >/dev/null 2>&1")
    classes = destination / 'classes'
    classes.mkdir(mode=0o700, exist_ok=True)
    for name, content in request['files'].items():
        update(destination / name, content)
    plugin = root / 'tools/jadx-ai-mcp'
    original = plugin / 'jadx-ai-mcp-6.5.0.jar'
    patched = plugin / 'jadx-ai-mcp-6.5.0-localhost.jar'
    cp = str(original) + ':' + str(root / 'tools/jadx/lib/jadx-1.5.6-all.jar')
    def command(args):
        done = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if done.returncode:
            raise RuntimeError('Build/install failed: ' + (done.stderr or done.stdout)[-2500:])
    # Compile before changing any active deployment settings/jar.
    command(['java', '-m', 'jdk.compiler/com.sun.tools.javac.Main', '--release', '11',
             '-cp', cp, '-d', str(classes), str(destination / 'PluginServer.java'),
             str(destination / 'IndependentRead.java')])
    installed = root / 'runtime/config/jadx/plugins/installed/jadx-ai-mcp.jar'
    try:
        with zipfile.ZipFile(installed) as archive:
            old_class = archive.read('com/zin/jadxaimcp/server/PluginServer.class')
        class_changed = old_class != (classes / 'com/zin/jadxaimcp/server/PluginServer.class').read_bytes()
    except (OSError, KeyError, zipfile.BadZipFile):
        class_changed = True
    restart_required = class_changed or text != env.read_text() or config_text != mcp_config.read_text()
    candidate = destination / 'jadx-ai-mcp-6.5.0-localhost.jar'
    shutil.copy2(original, candidate)
    command(['java', '-m', 'jdk.jartool/sun.tools.jar.Main', '--update', '--file', str(candidate),
             '-C', str(classes), 'com/zin/jadxaimcp/server/PluginServer.class'])
    update(env, text)
    update(mcp_config, config_text)
    update(start, start_text)
    env.chmod(0o700); start.chmod(0o700)
    shutil.copy2(patched, backup / patched.name)
    shutil.copy2(candidate, patched)
    # Keep the old documented rebuild entry using the new source, not the old patch.
    update(plugin / 'patch-src/PluginServer.java', request['files']['PluginServer.java'])
    command(['bash', '-c', 'source "$1/scripts/env.sh"; jadx plugins --install-jar "$2"',
             'minorguard-install', str(root), str(patched)])
    watched = [env, mcp_config, start, patched, classes / 'IndependentRead.class',
               installed]
    watched += [destination / name for name in request['files']]
    # The installed plugin path can differ; watch known files only. Compilation
    # and plugin-manager installation still must have completed successfully.
    file_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in watched if path.is_file()}
    stamp.write_text(json.dumps({'signature': signature, 'heap_gb': request['heap_gb'], 'file_hashes': file_hashes}))
    return {'changed': True, 'signature': signature, 'backup': str(backup), 'restart_required': restart_required}
