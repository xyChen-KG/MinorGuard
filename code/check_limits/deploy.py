"""Deploy versioned source, not ad-hoc edits to generated output or credentials."""
import re
import hashlib
from pathlib import Path, PurePosixPath

from collect_images.common import CollectorError
from .tools import SSHConnection


def ensure_runtime(config):
    root = config.get('managed_root', '')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', root) or '..' in PurePosixPath(root).parts or len(PurePosixPath(root).parts) < 3:
        raise CollectorError('部署需要明确的本项目 managed_root')
    sources = Path(__file__).resolve().parent.parent / 'jadx_runtime'
    request = {'root': root, 'runtime_version': 2, 'heap_gb': int(config.get('heap_gb', 32)),
               'files': {name: (sources / name).read_text(encoding='utf-8') for name in
                         ('PluginServer.java', 'IndependentRead.java', 'read_source.py', 'probe_ready.py')}}
    script = (sources / 'install.py').read_text(encoding='utf-8')
    request['installer_sha256'] = hashlib.sha256(script.encode('utf-8')).hexdigest()
    script += '\ntry:\n print(json.dumps(install(%r)))\nexcept Exception as error:\n print(json.dumps({"error":str(error)}))\n' % request
    status = SSHConnection(config).remote(script, timeout=180)
    if status.get('error'):
        raise CollectorError('JADX 运行源码部署失败：' + status['error'])
    return status
