"""Constants for the InfoMentor integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "infomentor"
# Neutral on purpose: Home Assistant writes the entry title to its log.
ENTRY_TITLE = "InfoMentor"
MANUFACTURER = "InfoMentor"

# Configuration keys.
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CHILDREN = "children"
# Every pupil linked to the account at the last login, selected or not, so a
# child that was deselected can be selected again.
CONF_LINKED_CHILDREN = "linked_children"
CONF_INCLUDE_CHILDCARE = "include_childcare"

# Defaults.
DEFAULT_INCLUDE_CHILDCARE = False
DEFAULT_SCAN_INTERVAL = timedelta(minutes=15)
NEWS_LIMIT = 10
# Home Assistant's recorder drops state attributes above 16 KB. Long texts are
# cut so the attributes stay well below that, with room for UTF-8.
MAX_NEWS_CONTENT_LENGTH = 6000
MAX_NOTIFICATION_BODY_LENGTH = 3000
# The notification list may use this many bytes. Over it, the oldest
# messages' bodies are shortened first, then left out, so the newest message
# keeps its text.
NOTIFICATIONS_BYTE_BUDGET = 14000
SHORT_NOTIFICATION_BODY_LENGTH = 300
CALENDAR_DAYS_AHEAD = 30
CHILDCARE_DAYS_AHEAD = 7

# InfoMentor Hub mobile app grant (verified configuration, see docs/mobile-auth.md
# in the infomentor-mcp project). client_secret "NONE" is a public protocol
# constant of the inspected app, not a private secret.
AUTH_ORIGIN = "https://im.infomentor.se"
API_ORIGIN = "https://api-im.infomentor.se"
HUB_ORIGIN = "https://hub.infomentor.se"
TOKEN_PATH = "/Authentication/OAuth2/Token"
SSO_PATH = "/NA1/Authentication/sso"
CLIENT_ID = "notificationapp"
CLIENT_SECRET = "NONE"
SCOPE = "IM2-API-NOTIFICATION"
REQUEST_TIMEOUT = 20
USER_AGENT = "ha-infomentor/0.1.0"
MAX_SSO_HOPS = 12
MAX_PUPIL_SWITCH_HOPS = 6

# Hub read endpoints (read-only; the integration never writes school data).
PATH_NEWS = "/Communication/News/GetNewsList"
PATH_NOTIFICATIONS = "/NotificationApp/NotificationApp/GetNotifications"
PATH_CALENDAR = "/calendarv2/calendarv2/getentries"
PATH_CHILDCARE = "/TimeRegistration/TimeRegistration/GetTimeRegistrations/"
PATH_SWITCH_PUPIL = "/Account/PupilSwitcher/SwitchPupil/"
# Message detail; id comes from a notification's "#/message/show/<id>" reference.
# Opening a message marks it read at InfoMentor, so the unread count drops.
PATH_MESSAGE = "/Communication/Message/GetMessage/"
# How many unread messages get their body pulled per poll (bounded API load).
MAX_MESSAGE_BODIES = 3

TIME_ZONE = "Europe/Stockholm"

PLATFORMS = ["calendar", "sensor"]
