"""Shared test fixtures for the InfoMentor integration tests."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from homeassistant.util import dt as dt_util

pytest_plugins = "pytest_homeassistant_custom_component"

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components in every test."""
    yield


@pytest.fixture(autouse=True)
def mock_info_mentor_api(monkeypatch):
    """Replace the real API class with an in-memory fake in every module."""

    class FakeApi:
        """Stands in for InfoMentorApi; no network access at all."""

        instances: ClassVar[list[FakeApi]] = []
        children: ClassVar[list[dict[str, str]]] = [
            {"id": "pupil-1", "name": "Ella"},
            {"id": "pupil-2", "name": "Olle"},
        ]
        fetch_error: ClassVar[Exception | None] = None
        validate_error: ClassVar[Exception | None] = None

        def __init__(self, session, refresh_token, *, on_token_refresh=None):
            self.session = session
            self._refresh_token = refresh_token
            self.on_token_refresh = on_token_refresh
            self.validated = False
            self.fetched: list[list[str]] = []
            FakeApi.instances.append(self)

        @property
        def refresh_token(self):
            return self._refresh_token

        async def async_validate(self):
            from custom_components.infomentor.api import Child

            if FakeApi.validate_error is not None:
                raise FakeApi.validate_error
            self.validated = True
            return [Child(id=item["id"], name=item["name"]) for item in self.children]

        async def async_fetch(self, child_ids, **kwargs):
            from custom_components.infomentor.api import (
                Child,
                ChildData,
                NewsItem,
                Notification,
                SchoolEvent,
            )

            self.fetched.append(list(child_ids))
            if FakeApi.fetch_error is not None:
                raise FakeApi.fetch_error
            tomorrow = dt_util.now() + timedelta(days=1)
            event_start = tomorrow.replace(hour=8, minute=15, second=0, microsecond=0)
            event_end = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
            result = []
            for child_id in child_ids:
                name = next(
                    item["name"] for item in self.children if item["id"] == child_id
                )
                result.append(
                    ChildData(
                        child=Child(id=child_id, name=name),
                        notifications=(
                            Notification(
                                id="n1",
                                title="Ny läxa i matematik",
                                state="New",
                                sent_at="2026-10-27T07:00:00",
                                notification_type="Homework",
                            ),
                            Notification(
                                id="n2",
                                title="Redan läst",
                                state="Seen",
                                sent_at="2026-10-26T07:00:00",
                                notification_type="Info",
                            ),
                        ),
                        news=(
                            NewsItem(
                                id="news-1",
                                title="Klassresan till Skansen",
                                content="Vi åker den 12 november.",
                                published_at="2026-10-26T12:00:00",
                            ),
                        ),
                        events=(
                            SchoolEvent(
                                id="event-1",
                                title="Föräldramöte",
                                description="I klassrummet",
                                start=event_start,
                                end=event_end,
                                all_day=False,
                            ),
                        ),
                    )
                )
            return result

        async def async_close(self):
            return None

    import custom_components.infomentor as integration
    import custom_components.infomentor.config_flow as config_flow

    monkeypatch.setattr(integration, "InfoMentorApi", FakeApi)
    monkeypatch.setattr(config_flow, "InfoMentorApi", FakeApi)
    return FakeApi
