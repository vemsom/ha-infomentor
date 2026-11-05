"""Tests for the BankID QR login and the InfoMentor app grant.

The whole chain runs against the local fake of the Swedish hosts in
``conftest.py``: SSO redirect and SAML form chain, BankID order and polling,
Hub session, discovery, app authorization and the token exchange. Nothing
touches the real service.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.infomentor.bankid import (
    MAX_RENEWALS,
    BankIdLogin,
    BankIdProtocol,
    BankIdProtocolError,
    BankIdTokenError,
)


async def test_protocol_starts_an_order_and_rotates_the_qr(fake_hosts) -> None:
    """The SSO chain, the order and the rotating QR follow the app protocol."""
    fake, client = fake_hosts
    protocol = BankIdProtocol(client.session)
    await protocol.async_begin("qr")

    assert fake.initialize_calls == ["qr"]
    assert protocol.qr_data == "bankid.QR1"
    assert await protocol.async_poll() == "USER_SIGN"
    # The payload rotates on every poll; the page must redraw it.
    assert protocol.qr_data == "bankid.QR2"
    assert await protocol.async_poll() == "OK"


async def test_protocol_completes_session_discovery_and_grant(fake_hosts) -> None:
    """A full login yields the verified token pair and its exact requests."""
    fake, client = fake_hosts
    protocol = BankIdProtocol(client.session)
    await protocol.async_begin("qr")
    await protocol.async_poll()
    await protocol.async_poll()
    await protocol.async_finish()

    assert await protocol.async_verify_session() == ["12345"]
    assert await protocol.async_discover() == "GUID"
    code = await protocol.async_authorize(device_name="Home Assistant")
    assert code == "CODE1"
    assert await protocol.async_exchange(code) == ("ACCESS", "REFRESH")

    assert fake.authorize_params is not None
    assert fake.authorize_params["authGuid"] == "GUID"
    assert fake.authorize_params["DeviceFriendlyName"] == "Home Assistant"
    assert fake.authorize_params["DevicePlatform"] == "Android"
    assert fake.authorize_params["scope"] == "IM2-API-NOTIFICATION"
    assert fake.authorize_params["redirect_uri"] == (
        "InfomentorNotification://oauth2Callback"
    )
    assert fake.authorize_params["client_id"] == "notificationapp"
    assert fake.authorize_params["response_type"] == "code"
    assert fake.authorize_params["isSandBox"] == "false"
    # A fresh device identifier per login: never a copied app UUID.
    assert len(fake.authorize_params["DeviceIdentifier"]) == 36
    assert fake.token_requests == [
        {
            "code": "CODE1",
            "client_id": "notificationapp",
            "client_secret": "NONE",
            "redirect_uri": "InfomentorNotification://oauth2Callback",
            "grant_type": "authorization_code",
        }
    ]


async def test_same_device_order_returns_an_autostart_link(fake_hosts) -> None:
    """The single-phone variant starts the BankID app instead of a QR scan."""
    fake, client = fake_hosts
    protocol = BankIdProtocol(client.session)
    await protocol.async_begin("same_device")

    assert fake.initialize_calls == ["bankid"]
    assert protocol.qr_data is None
    assert protocol.launch_url is not None
    parsed = urlsplit(protocol.launch_url)
    assert parsed.scheme == "https" and parsed.netloc == "app.bankid.com"
    assert parse_qs(parsed.query) == {
        "autostarttoken": ["AUTOSTART"],
        "redirect": ["null"],
    }


async def test_login_renews_an_expired_order(fake_hosts) -> None:
    """An order the bank dropped is replaced, not reported as a rejection.

    Stockholm ends an unscanned order after about thirty seconds; the user may
    still be picking up a second screen, so the attempt gets a new order.
    """
    fake, client = fake_hosts
    fake.states = ["ERROR", "USER_SIGN", "OK"]
    login = BankIdLogin(client.session, interval=0.01, reload_delay=0)
    await login.async_start()
    await login.async_wait()

    assert fake.orders == 2
    assert login.status == "complete"
    assert login.error is None
    assert login.refresh_token == "REFRESH"


async def test_login_gives_up_after_the_renewal_cap(fake_hosts) -> None:
    """A bank that keeps dropping orders ends as a rejection, not a loop."""
    fake, client = fake_hosts
    fake.states = ["ERROR"] * (MAX_RENEWALS + 2)
    login = BankIdLogin(client.session, interval=0.01, reload_delay=0)
    await login.async_start()
    await login.async_wait()

    assert fake.orders == MAX_RENEWALS + 1
    assert login.status == "cancelled"
    assert login.error == "cancelled"
    assert login.refresh_token is None


async def test_login_saves_only_the_refresh_token(fake_hosts) -> None:
    """The orchestrator polls, grants and exposes the refresh token."""
    _fake, client = fake_hosts
    updates: list[tuple[str | None, str, str | None]] = []
    login = BankIdLogin(
        client.session,
        interval=0.01,
        reload_delay=0,
        on_update=lambda qr, status, launch: updates.append((qr, status, launch)),
    )
    await login.async_start()
    assert login.status == "waiting"
    await login.async_wait()

    assert login.status == "complete"
    assert login.error is None
    assert login.refresh_token == "REFRESH"
    assert login.qr_data is None
    assert ("bankid.QR1", "waiting", None) in updates
    assert updates[-1][1] == "complete"


async def test_login_never_polls_the_order_after_approval(fake_hosts) -> None:
    """The first request after OK is the page reload, exactly like the browser.

    Stockholm builds the SAML handover for the first request that follows a
    completed order. The page's own script stops polling on OK and reloads
    three seconds later (``case "OK": location.reload()``); a ``verifyorder``
    poll after approval is a request the browser never makes, spends the
    handover, and leaves the reload on the BankID page with no SAML form — the
    reported "chooser page, then Hub answers 302" failure.
    """
    fake, client = fake_hosts
    login = BankIdLogin(client.session, interval=0.01, reload_delay=0)
    await login.async_start()
    await login.async_wait()

    assert fake.verify_after_ok == 0
    assert fake.handover_spent is False
    assert login.status == "complete"
    assert login.error is None
    assert login.refresh_token == "REFRESH"


async def test_login_reports_cancellation(fake_hosts) -> None:
    """A rejected order ends the attempt without any token."""
    fake, client = fake_hosts
    fake.states = ["CANCELLED"]
    login = BankIdLogin(client.session, interval=0.01, reload_delay=0)
    await login.async_start()
    await login.async_wait()

    assert login.status == "cancelled"
    assert login.error == "cancelled"
    assert login.refresh_token is None


async def test_login_times_out_without_approval(fake_hosts) -> None:
    """An unscanned order ends as a timeout, not as a hang."""
    fake, client = fake_hosts
    fake.states = ["PENDING"]
    login = BankIdLogin(client.session, interval=0.01, timeout=0.05, reload_delay=0)
    await login.async_start()
    await login.async_wait()

    assert login.status == "timeout"
    assert login.error == "timeout"
    assert login.refresh_token is None


async def test_login_maps_a_rejected_code_to_auth_error(fake_hosts) -> None:
    """A refused authorization code is an auth error, not a crash."""
    fake, client = fake_hosts
    fake.token_status = 400
    login = BankIdLogin(client.session, interval=0.01, reload_delay=0)
    await login.async_start()
    await login.async_wait()

    assert login.status == "failed"
    assert login.error == "invalid_auth"
    assert login.refresh_token is None


async def test_discovery_rejects_a_foreign_authorization_origin(fake_hosts) -> None:
    """Discovery output is validated before it is trusted."""
    fake, client = fake_hosts
    fake.discovery_authentication_url = (
        "https://attacker.example/Authentication/Authentication/LoginOAuth2"
        "?authGuid=GUID"
    )
    protocol = BankIdProtocol(client.session)
    await protocol.async_begin("qr")
    await protocol.async_poll()
    await protocol.async_poll()
    await protocol.async_finish()

    with pytest.raises(BankIdProtocolError):
        await protocol.async_discover()
    assert protocol.auth_guid is None


async def test_rejected_code_error_never_contains_the_code(fake_hosts) -> None:
    """Error messages must not become a way to read the capability."""
    fake, client = fake_hosts
    fake.token_status = 401
    protocol = BankIdProtocol(client.session)

    with pytest.raises(BankIdTokenError) as error:
        await protocol.async_exchange("SECRETCODE")

    assert "SECRETCODE" not in str(error.value)
    assert "ACCESS" not in str(error.value)
