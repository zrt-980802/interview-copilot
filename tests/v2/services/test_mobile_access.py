import pytest

from interview_copilot.services.mobile_access import MobileAccess, PairingError


def test_pairing_is_single_use_and_new_code_replaces_previous_code():
    access = MobileAccess()
    old = access.issue("s1")["code"]
    code = access.issue("s1")["code"]
    if old != code:
        with pytest.raises(PairingError):
            access.exchange(old, "s1", "ip")
    token = access.exchange(code, "s1", "ip")
    assert access.authorize(token, "s1")
    assert not access.authorize(token, "s2")
    assert not access.authorize(token + "x", "s1")
    assert not access.authorize(None, "s1")
    with pytest.raises(PairingError):
        access.exchange(code, "s1", "ip")
    assert token not in repr(access.__dict__)


def test_expiry_revoke_and_session_boundary():
    now = [0]
    access = MobileAccess(clock=lambda: now[0], code_ttl=120, grant_ttl=300)
    code = access.issue("s1")["code"]
    now[0] = 120
    with pytest.raises(PairingError):
        access.exchange(code, "s1", "ip")
    token = access.exchange(access.issue("s1")["code"], "s1", "ip")
    now[0] = 420
    assert not access.authorize(token, "s1")
    token = access.exchange(access.issue("s1")["code"], "s1", "ip")
    access.revoke(token)
    assert not access.authorize(token, "s1")
    token = access.exchange(access.issue("s1")["code"], "s1", "ip")
    access.issue("s1")
    access.revoke_all()
    assert not access.authorize(token, "s1")
    assert access.challenge("s1")["code"] is None
    assert access.count("s1") == 0


def test_pairing_rate_limit_is_per_peer_and_global_and_cannot_be_reset_by_new_code():
    now = [0]
    access = MobileAccess(clock=lambda: now[0])
    code = access.issue("s1")["code"]
    for _ in range(5):
        with pytest.raises(PairingError) as error:
            access.exchange("invalid", "s1", "same-ip")
        assert error.value.status == 401
    code = access.issue("s1")["code"]
    with pytest.raises(PairingError) as error:
        access.exchange(code, "s1", "same-ip")
    assert error.value.status == 429
    for index in range(25):
        with pytest.raises(PairingError):
            access.exchange("invalid", "s1", f"peer-{index}")
    with pytest.raises(PairingError) as error:
        access.exchange(code, "s1", "new-ip")
    assert error.value.status == 429
    now[0] = 61
    assert access.authorize(access.exchange(code, "s1", "same-ip"), "s1")


def test_grants_are_bounded():
    access = MobileAccess()
    for index in range(8):
        access.exchange(access.issue("s")["code"], "s", str(index))
    with pytest.raises(PairingError) as error:
        access.exchange(access.issue("s")["code"], "s", "ninth")
    assert error.value.status == 409
    assert access.count("s") == 8
