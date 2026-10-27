#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A user pool's signing key and JWKS endpoint, served locally, for tests that need tokens our
verifier accepts: signed RS256 with the pool's key, found by `kid` through the real PyJWKClient."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

CLIENT = "web-client"
KID = "pool-key-1"
POOL_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Jwks:
    """The user pool's /.well-known/jwks.json, on localhost: one RSA key, by kid."""

    def __init__(self):
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(POOL_KEY.public_key()))
        body = json.dumps({"keys": [{**jwk, "kid": KID, "alg": "RS256", "use": "sig"}]}).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                found = self.path.endswith("/.well-known/jwks.json")
                self.send_response(200 if found else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body if found else b"{}")

            def log_message(self, format, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self.server.server_address[1]}/us-east-1_pool"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def configure(self, monkeypatch) -> None:
        """The stack's settings for a verifier (the interceptor's, the runtime's), pointing here."""
        monkeypatch.setenv("IDENTITY_PROVIDER", "cognito")
        monkeypatch.setenv("OIDC_ISSUER", self.issuer)
        monkeypatch.setenv("ALLOWED_CLIENT_IDS", CLIENT)

    def close(self) -> None:
        self.server.shutdown()


def claims(issuer: str, **overrides) -> dict:
    """A current access token's claims for the web client; None drops a claim."""
    now = int(time.time())
    out = {"sub": "sub-alice", "iss": issuer, "exp": now + 300, "iat": now,
           "token_use": "access", "client_id": CLIENT, "scope": "gateway/invoke", **overrides}
    return {k: v for k, v in out.items() if v is not None}


def signed(token_claims: dict, key=POOL_KEY, kid: str = KID) -> str:
    return jwt.encode(token_claims, key, algorithm="RS256", headers={"kid": kid})


