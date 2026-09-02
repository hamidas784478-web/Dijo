"""
Dijo AI Proxy
=============
A tiny standalone server whose only job is to hold your real Groq API key
privately and forward chat requests to Groq on behalf of every copy of the
Dijo app you hand out. This is the piece that makes "AI always on, no key
entry" possible WITHOUT baking a real key into an app you distribute.

Why this exists:
  Dijo's main app.py is plain, readable Python. If you put your real Groq
  key inside it and give that app to other people, every one of them can
  open the file and read the key out, then use it on your bill. There is
  no way to prevent that if the key travels inside the distributed app.
  The only real fix is to keep the key on a server only YOU control, and
  have the distributed app call that server instead of Groq directly.

How it works:
  - You deploy this file (one small Flask app) somewhere you control
    (a $5/mo VPS, Render, Railway, Fly.io, PythonAnywhere, etc. — anywhere
    that can run Python and stay reachable over HTTPS).
  - You set two environment variables on THAT server only:
        GROQ_API_KEY   = your real Groq key
        PROXY_TOKEN    = a random string you make up (this is NOT the Groq
                          key — it just stops random strangers from using
                          your proxy if they find its URL)
  - In the Dijo app you distribute, you set (in its own environment):
        DIJO_AI_ENDPOINT = https://your-proxy-domain.com/v1/chat/completions
        DIJO_AI_API_KEY  = the same PROXY_TOKEN you picked above
    Dijo already reads both of these (see app.py's _ai_config()), so no
    code changes are needed in the main app — just point it at the proxy.
  - Real Groq key now lives ONLY on your proxy server. Every Dijo install
    talks to your proxy, never to Groq directly, and never sees the key.

Run locally to test:
    pip install flask requests
    GROQ_API_KEY=gsk_xxx PROXY_TOKEN=pick-a-long-random-string python3 ai_proxy.py

Note: this is a basic shared-token gate, not full auth. If you're shipping
this to the public at scale, also add per-install rate limiting so one
leaked PROXY_TOKEN can't run up your whole Groq bill.
"""
import os
import json
import urllib.request
import urllib.error
from flask import Flask, request, jsonify

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
PROXY_TOKEN = os.environ.get("PROXY_TOKEN", "")

app = Flask(__name__)


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    if not GROQ_API_KEY:
        return jsonify({"error": {"message": "GROQ_API_KEY not set on proxy server"}}), 500
    if not PROXY_TOKEN:
        return jsonify({"error": {"message": "PROXY_TOKEN not set on proxy server"}}), 500

    auth = request.headers.get("Authorization", "")
    sent_token = auth.replace("Bearer ", "").strip()
    if sent_token != PROXY_TOKEN:
        return jsonify({"error": {"message": "Invalid proxy token"}}), 401

    payload = request.get_json(force=True, silent=True) or {}
    req = urllib.request.Request(
        GROQ_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + GROQ_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read()
            return app.response_class(body, status=r.status, mimetype="application/json")
    except urllib.error.HTTPError as e:
        body = e.read()
        return app.response_class(body, status=e.code, mimetype="application/json")
    except urllib.error.URLError as e:
        return jsonify({"error": {"message": f"Network error reaching Groq: {e.reason}"}}), 502


@app.route("/health")
def health():
    return jsonify({"ok": True, "groq_key_configured": bool(GROQ_API_KEY), "token_configured": bool(PROXY_TOKEN)})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8100)))
