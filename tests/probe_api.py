"""Direct API probe: reports auth status without printing the key."""

import json
import os
import sys
import urllib.error
import urllib.request

key = os.environ.get("MODEL_API_KEY", "")
if not key:
    # Absence is a skip, not a failure: a caller cannot distinguish
    # skip from pass by exit code alone.
    print("PROBE: MODEL_API_KEY not visible in this environment")
    sys.exit(0)

print(f"PROBE: key visible, length={len(key)} prefix={key[:4]}...")
body = json.dumps(
    {
        "model": "muse-spark-1.2-contributor",
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 5,
    }
).encode()
req = urllib.request.Request(
    "https://api.meta.ai/v1/chat/completions",
    data=body,
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
        print("PROBE: HTTP", resp.status)
        print("PROBE: reply =", data["choices"][0]["message"]["content"])
except urllib.error.HTTPError as exc:
    print(f"PROBE: HTTP {exc.code}")
    print("PROBE: body:", exc.read().decode(errors="replace")[:400])
# Other failures (timeouts, connection errors, bad JSON) surface as an
# uncaught traceback.
