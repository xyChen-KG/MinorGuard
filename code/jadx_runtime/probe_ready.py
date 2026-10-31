"""Shell start-script readiness probe: HTTP 200 by itself never passes."""
import json
import sys
import urllib.request

try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8650/minorguard-ready', timeout=2) as response:
        status = json.load(response)
    success = (status.get('protocol') == 'minorguard-jadx-ready-v1' and status.get('ready') is True
               and status.get('load_returned') is True and status.get('passes_initialized') is True)
except Exception:
    success = False
if __name__ == '__main__':
    sys.exit(0 if success else 1)
