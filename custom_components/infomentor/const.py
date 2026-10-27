"""Constants for the InfoMentor integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "infomentor"
MANUFACTURER = "InfoMentor"

# Configuration keys.
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CHILDREN = "children"
CONF_INCLUDE_CHILDCARE = "include_childcare"

# Defaults.
DEFAULT_INCLUDE_CHILDCARE = False
DEFAULT_SCAN_INTERVAL = timedelta(minutes=15)
NEWS_LIMIT = 10
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

TIME_ZONE = "Europe/Stockholm"

PLATFORMS = ["calendar", "sensor"]
