from quiddy.core.security import sign_hmac, verify_hmac


def test_hmac_roundtrip():
    signed = sign_hmac("secret", "POST", "/v1/events", b"{}", timestamp=1_800_000_000, nonce="abc")
    assert verify_hmac("secret", signed, "POST", "/v1/events", b"{}", max_skew_seconds=10**9)
    assert not verify_hmac("wrong", signed, "POST", "/v1/events", b"{}", max_skew_seconds=10**9)
