"""
login.py — OAuth 2.1 PKCE flow against Swiggy MCP.

What this does:
  1. Discovers Swiggy's OAuth endpoints from the well-known metadata URL.
  2. Registers this script as an OAuth client via Dynamic Client Registration.
  3. Generates PKCE verifier + challenge.
  4. Opens browser to Swiggy's login page.
  5. Runs a tiny local HTTP server to catch the OAuth redirect.
  6. Exchanges the authorization code for an access token.
  7. Saves the token + client_id to token.json.

Run this ONCE per ~5 days (token lifetime), then use hello.py.
"""

import base64
import hashlib
import json
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# --- Config ----------------------------------------------------------------

SWIGGY_BASE = "https://mcp.swiggy.com"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
CLIENT_NAME = "Swiggy MCP POC (local dev)"
SCOPE = "mcp:tools mcp:resources mcp:prompts"
TOKEN_FILE = "token.json"


# --- Step 1: Discover OAuth metadata ---------------------------------------

def discover_oauth_endpoints():
    """Hit the well-known endpoint to get the real authorize/token/register URLs."""
    print("→ Discovering Swiggy OAuth endpoints...")
    url = f"{SWIGGY_BASE}/.well-known/oauth-authorization-server"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    meta = r.json()

    endpoints = {
        "authorization_endpoint": meta["authorization_endpoint"],
        "token_endpoint": meta["token_endpoint"],
        # DCR endpoint may or may not be in metadata; fallback to documented path
        "registration_endpoint": meta.get(
            "registration_endpoint", f"{SWIGGY_BASE}/auth/register"
        ),
    }
    print(f"  authorize:    {endpoints['authorization_endpoint']}")
    print(f"  token:        {endpoints['token_endpoint']}")
    print(f"  register:     {endpoints['registration_endpoint']}")
    return endpoints


# --- Step 2: Dynamic Client Registration -----------------------------------

def register_client(registration_endpoint):
    """Register this app as an OAuth client. Returns the client_id."""
    print("→ Registering OAuth client (DCR)...")
    payload = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",  # public client, PKCE only
        "scope": SCOPE,
    }
    r = requests.post(registration_endpoint, json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    client_id = data["client_id"]
    print(f"  client_id:    {client_id}")
    return client_id


# --- Step 3: PKCE codes ----------------------------------------------------

def generate_pkce():
    """Generate verifier + S256 challenge per OAuth 2.1 / RFC 7636."""
    verifier = (
        base64.urlsafe_b64encode(secrets.token_bytes(32))
        .decode("ascii")
        .rstrip("=")
    )
    challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    return verifier, challenge


# --- Step 4 + 5: Authorize in browser, capture callback --------------------

class CallbackHandler(BaseHTTPRequestHandler):
    """Tiny HTTP handler that captures ?code=...&state=... from Swiggy's redirect."""

    captured = {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        CallbackHandler.captured["code"] = params.get("code", [None])[0]
        CallbackHandler.captured["state"] = params.get("state", [None])[0]
        CallbackHandler.captured["error"] = params.get("error", [None])[0]
        CallbackHandler.captured["error_description"] = params.get(
            "error_description", [None]
        )[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if CallbackHandler.captured.get("error"):
            msg = (
                f"<h1>OAuth error</h1>"
                f"<p>{CallbackHandler.captured['error']}: "
                f"{CallbackHandler.captured.get('error_description', '')}</p>"
            )
        else:
            msg = (
                "<h1>Logged in successfully.</h1>"
                "<p>You can close this tab and return to your terminal.</p>"
            )
        self.wfile.write(msg.encode("utf-8"))

    def log_message(self, *args, **kwargs):
        pass  # silence default access-log spam


def run_authorization_flow(authorize_endpoint, client_id):
    """Open browser to /authorize, run local server to catch the redirect."""
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": SCOPE,
    }
    auth_url = f"{authorize_endpoint}?{urllib.parse.urlencode(params)}"

    print("→ Starting local callback server on", REDIRECT_URI)
    httpd = HTTPServer(("localhost", REDIRECT_PORT), CallbackHandler)

    print("→ Opening browser for Swiggy login...")
    print("  (if it doesn't open, paste this URL manually:)")
    print(f"  {auth_url}")
    webbrowser.open(auth_url)

    print("→ Waiting for OAuth callback...")
    while "code" not in CallbackHandler.captured and "error" not in CallbackHandler.captured:
        httpd.handle_request()

    captured = CallbackHandler.captured
    if captured.get("error"):
        raise RuntimeError(
            f"OAuth error: {captured['error']}: {captured.get('error_description')}"
        )
    if captured.get("state") != state:
        raise RuntimeError("CSRF state mismatch — possible attack, aborting.")

    print("  got authorization code.")
    return captured["code"], verifier


# --- Step 6: Exchange code for token --------------------------------------

def exchange_code_for_token(token_endpoint, code, verifier, client_id):
    print("→ Exchanging code for access token...")
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
    }
    r = requests.post(token_endpoint, json=payload, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({r.status_code}): {r.text}")
    return r.json()


# --- Step 7: Save token ----------------------------------------------------

def save_token(token_data, client_id):
    out = {
        "access_token": token_data["access_token"],
        "token_type": token_data.get("token_type", "Bearer"),
        "expires_in": token_data.get("expires_in"),
        "scope": token_data.get("scope"),
        "client_id": client_id,
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(out, f, indent=2)
    print(f"→ Token saved to {TOKEN_FILE}")
    print(f"  expires in: {out['expires_in']} seconds "
          f"(~{int(out['expires_in']) // 86400} days)" if out['expires_in'] else "")


# --- Main ------------------------------------------------------------------

def main():
    try:
        endpoints = discover_oauth_endpoints()
        client_id = register_client(endpoints["registration_endpoint"])
        code, verifier = run_authorization_flow(
            endpoints["authorization_endpoint"], client_id
        )
        token_data = exchange_code_for_token(
            endpoints["token_endpoint"], code, verifier, client_id
        )
        save_token(token_data, client_id)
        print("\n✓ Login complete. Run `python hello.py` next.")
    except requests.HTTPError as e:
        print(f"\n✗ HTTP error: {e}")
        print(f"  Response body: {e.response.text if e.response else '(none)'}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
