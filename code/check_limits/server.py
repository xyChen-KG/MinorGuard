"""Explicit pipeline-only preparation of the project's managed JADX instance."""

import json
import re
import subprocess
import time
from pathlib import PurePosixPath

from collect_images.common import CollectorError
from .tools import MCPClient, SSHConnection, manifest_identity
from .deploy import ensure_runtime


def _check_manifest(config, metadata):
    with SSHConnection(config) as connection:
        connection.wait_ready()
        mcp = MCPClient(connection.url, timeout=config.get("timeout", 180))
        try:
            mcp.initialize()
            found = manifest_identity(mcp.call("get_android_manifest", {}))
            return all(str(found.get(k)) == str(metadata.get(k)) for k in ("package", "version_name", "version_code"))
        finally:
            mcp.close()


def ensure_server(apk, metadata, config):
    """Reuse the correct project; otherwise upload by hash and switch owned sessions."""
    root = config.get("managed_root", "")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", root) or ".." in PurePosixPath(root).parts or len(PurePosixPath(root).parts) < 3:
        raise CollectorError("自动准备服务器需要明确、非根目录的 jadx.managed_root")
    if config["remote_apk_state_file"] != root + "/runtime/run/current_apk.txt":
        raise CollectorError("自动准备仅支持本项目标准托管目录，不修改未知部署")
    helper = SSHConnection(config)
    target = root + "/apks/" + metadata["sha256"] + ".apk"
    data = repr({"root": root, "target": target})
    status = helper.remote("""import hashlib,json,subprocess
from pathlib import Path
d=%s
r=Path(d['root'])
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 return h.hexdigest()
state=r/'runtime/run/current_apk.txt'
current=Path(state.read_text().strip()) if state.is_file() else None
sessions={}
for name in ('minorguard-xvfb','minorguard-jadx','minorguard-mcp'):
 p=subprocess.run(['tmux','show-environment','-t','='+name,'MG_MANAGED_ROOT'],capture_output=True,text=True)
 if p.returncode==0:sessions[name]=p.stdout.strip()
print(json.dumps({'current_sha256':digest(current) if current and current.is_file() else None,
 'target_sha256':digest(Path(d['target'])) if Path(d['target']).is_file() else None,'sessions':sessions}))
""" % data)
    expected_marker = "MG_MANAGED_ROOT=" + root
    if any(value != expected_marker for value in status["sessions"].values()):
        raise CollectorError("发现非本项目管理的同名服务；没有切换或停止它")
    runtime = ensure_runtime(config)
    if not runtime.get("restart_required", runtime["changed"]) and status.get("current_sha256") == metadata["sha256"] and len(status["sessions"]) == 3:
        try:
            if _check_manifest(config, metadata):
                return {"status": "reused", "sha256": metadata["sha256"], "note": "服务器已有相同 APK，未上传或重启"}
        except CollectorError:
            pass  # Readiness/identity failed: the explicit pipeline may reload its owned project.
    if status.get("target_sha256") not in (None, metadata["sha256"]):
        raise CollectorError("服务器的同哈希命名文件内容异常；未覆盖该文件")
    if status.get("target_sha256") is None:
        helper.remote("import json\nfrom pathlib import Path\nPath(%r).mkdir(parents=True,exist_ok=True,mode=0o700)\nprint(json.dumps({'ready':True}))\n" % (root + "/apks"))
        print("[服务器] 上传对应 APK（按哈希命名，不覆盖旧版本）…", flush=True)
        try:
            upload = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                     str(apk), config["ssh_host"] + ":" + target], capture_output=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired):
            raise CollectorError("APK 上传失败或超时，未继续静态核对") from None
        if upload.returncode:
            raise CollectorError("APK 上传失败，未继续静态核对")
    print("[服务器] 加载本项目的对应 JADX 工程…", flush=True)
    payload = repr({"root": root, "target": target, "sha256": metadata["sha256"],
                    "ready_timeout": config.get("ready_timeout", 600)})
    script = """import fcntl,hashlib,json,os,shlex,signal,socket,subprocess,time,urllib.request
from pathlib import Path
d=%s
r=Path(d['root']); apk=Path(d['target'])
def command(args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=d['ready_timeout']+120)
 if p.returncode:raise RuntimeError('Managed command failed: '+args[0])
 return p.stdout.strip()
try:
 lock=(r/'runtime/run/setup.lock').open('a')
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 h=hashlib.sha256()
 with apk.open('rb') as f:
  for b in iter(lambda:f.read(1048576),b''):h.update(b)
 if h.hexdigest()!=d['sha256']:raise RuntimeError('Uploaded APK hash mismatch')
 names=('minorguard-xvfb','minorguard-jadx','minorguard-mcp')
 active=[]
 for name in names:
  p=subprocess.run(['tmux','show-environment','-t','='+name,'MG_MANAGED_ROOT'],capture_output=True,text=True)
  if p.returncode==0:
   if p.stdout.strip()!='MG_MANAGED_ROOT='+str(r):raise RuntimeError('Unmanaged session; not stopped')
   active.append(name)
 if len(active)==3:
  for name in ('minorguard-mcp','minorguard-jadx'):
   pid=int(command(['tmux','display-message','-p','-t','='+name+':0.0','#{pane_pid}']))
   if pid<=1:raise RuntimeError('Invalid managed PID')
   os.kill(pid,signal.SIGTERM)
   stopped=False
   for _ in range(150):
    p=subprocess.run(['ps','-p',str(pid),'-o','stat='],capture_output=True,text=True)
    if not p.stdout.strip() or p.stdout.lstrip().startswith('Z'):stopped=True;break
    time.sleep(.2)
   if not stopped:raise RuntimeError('Managed component did not exit; not force-killed')
   subprocess.run(['tmux','kill-session','-t','='+name],capture_output=True)
  for name,args in (('minorguard-jadx',['jadx',str(apk)]),('minorguard-mcp',['mcp'])):
   command(['tmux','new-session','-d','-s',name,'exec '+shlex.join(['bash',str(r/'scripts/run_component.sh')]+args)])
   command(['tmux','set-environment','-t','='+name,'MG_MANAGED_ROOT',str(r)])
 else:
  command(['bash',str(r/'scripts/stop_static.sh')])
  ready=False
  for _ in range(80):
   sockets=[]
   try:
    for port in (8650,8651):
     s=socket.socket();sockets.append(s);s.bind(('127.0.0.1',port))
    ready=True;break
   except OSError:time.sleep(1)
   finally:
    for s in sockets:s.close()
  if not ready:raise RuntimeError('Ports occupied; no unrelated process stopped')
  command(['bash',str(r/'scripts/start_static.sh'),str(apk)])
 ready=False
 deadline=time.monotonic()+d['ready_timeout']
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 while time.monotonic()<deadline:
  try:
   with opener.open('http://127.0.0.1:8650/minorguard-ready',timeout=2) as response:probe=json.load(response)
   ready=(probe.get('protocol')=='minorguard-jadx-ready-v1' and probe.get('ready') is True and probe.get('load_returned') is True and probe.get('passes_initialized') is True)
   if ready:break
  except OSError:pass
  time.sleep(1)
 if not ready:raise RuntimeError('JADX did not become ready')
 (r/'runtime/run/current_apk.txt').write_text(str(apk)+'\\n')
 print(json.dumps({'status':'loaded','path':str(apk),'sha256':d['sha256']}))
except Exception as e:
 print(json.dumps({'error':type(e).__name__+': '+str(e)[:300]}))
""" % payload
    result = helper.remote(script, timeout=config.get("ready_timeout", 600) + 240)
    if result.get("error"):
        raise CollectorError("服务器自动准备失败：" + result["error"])
    for _ in range(5):
        try:
            if _check_manifest(config, metadata):
                return result
        except CollectorError:
            pass
        time.sleep(1)
    raise CollectorError("服务器 Manifest 尚未与 APK 对应；未用旧工程继续分析")
