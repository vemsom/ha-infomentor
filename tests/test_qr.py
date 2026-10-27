"""Tests for the short-lived BankID QR view."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from custom_components.infomentor.qr import BASE_PATH, async_get_store, encode_matrix

QR_PAYLOAD = "bankid.8b8f3c.1759212345678.6a3d0e9b1c2f4a7d"


@pytest.fixture
def store(hass):
    """Return the per-instance QR store, with its view registered."""
    return async_get_store(hass)


def test_store_is_shared_per_instance(hass, store) -> None:
    """The view is registered once, not once per login attempt."""
    assert async_get_store(hass) is store
    assert hass.data["infomentor_bankid_qr"] is store


def test_matrix_is_a_valid_qr_shape() -> None:
    """The encoder produces a full square QR symbol, not a partial frame."""
    matrix = encode_matrix(QR_PAYLOAD)
    size = len(matrix)
    assert (size - 17) % 4 == 0 and size >= 21
    assert all(len(row) == size for row in matrix)
    # The three finder patterns sit in the corners the standard puts them in.
    for left, top in ((0, 0), (size - 7, 0), (0, size - 7)):
        assert matrix[top][left] and matrix[top][left + 6]
        assert not matrix[top + 1][left + 1]
        assert matrix[top + 3][left + 3]


async def test_page_and_frame_need_no_auth(hass, hass_client_no_auth, store) -> None:
    """The QR page is a one-time ticket: no login required, nothing else served."""
    session = store.open()
    client = await hass_client_no_auth()
    path = urlsplit(session.url).path
    assert path.startswith(BASE_PATH + "/")
    token = path.rsplit("/", 1)[-1]
    assert len(token) >= 22  # token_urlsafe(32) -> 43 chars, >= 128 bits

    page = await client.get(path)
    assert page.status == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    body = await page.text()
    assert "<canvas" in body
    assert "refresh" not in body.lower() and "cookie" not in body.lower()

    frame = await client.get(path + "/frame")
    assert frame.status == 200
    assert frame.headers["Cache-Control"] == "no-store"
    assert await frame.json() == {
        "matrix": None,
        "status": "starting",
        "launch_url": None,
    }


async def test_update_publishes_the_rotating_qr(
    hass, hass_client_no_auth, store
) -> None:
    """Every poll can republish a new payload and status."""
    session = store.open()
    client = await hass_client_no_auth()
    path = urlsplit(session.url).path

    session.update(QR_PAYLOAD, "waiting")
    payload = await (await client.get(path + "/frame")).json()
    assert payload["status"] == "waiting"
    assert payload["matrix"] == encode_matrix(QR_PAYLOAD)

    session.update(None, "verifying")
    payload = await (await client.get(path + "/frame")).json()
    assert payload["matrix"] is None
    assert payload["status"] == "verifying"


async def test_closed_session_is_gone(hass, hass_client_no_auth, store) -> None:
    """After closing, both URLs are 404 and the token is forgotten."""
    session = store.open()
    client = await hass_client_no_auth()
    path = urlsplit(session.url).path
    assert (await client.get(path)).status == 200

    session.close()

    assert (await client.get(path)).status == 404
    assert (await client.get(path + "/frame")).status == 404
    assert path.rsplit("/", 1)[-1] not in store.entries


async def test_unknown_token_is_not_served(hass, hass_client_no_auth, store) -> None:
    """A guessed or stale path never leaks a page."""
    client = await hass_client_no_auth()
    assert (await client.get(f"{BASE_PATH}/not-a-real-token")).status == 404
    assert (await client.get(f"{BASE_PATH}/not-a-real-token/frame")).status == 404


async def test_each_attempt_gets_its_own_path(hass, store) -> None:
    """Two flows cannot read each other's QR."""
    first = store.open()
    second = store.open()
    assert first.url != second.url
    first.update(QR_PAYLOAD, "waiting")

    assert first.entry.matrix is not None
    assert second.entry.matrix is None
