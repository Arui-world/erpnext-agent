import base64
import hashlib

from erpnext_agent.auth.oauth_client import create_pkce_request


def test_pkce_challenge_is_s256_of_verifier() -> None:
    request = create_pkce_request()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(request.verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert request.challenge == expected
    assert request.state
    assert request.nonce

