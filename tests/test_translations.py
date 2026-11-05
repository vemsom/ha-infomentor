"""Translations must exist for every language the UI can ask for.

Home Assistant looks up `translations/<language>.json` for custom components.
A missing file does not fall back to `strings.json`; the config flow renders
with empty labels and empty menus, which is invisible in tests that only look
at step ids.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "infomentor"
STRINGS = COMPONENT / "strings.json"
TRANSLATIONS = COMPONENT / "translations"


def _flatten(node: object, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys |= _flatten(value, f"{prefix}.{key}" if prefix else key)
    else:
        keys.add(prefix)
    return keys


@pytest.mark.parametrize("language", ["en", "sv"])
def test_language_file_covers_every_string(language: str) -> None:
    """Every language ships every key that strings.json defines."""
    source = _flatten(json.loads(STRINGS.read_text(encoding="utf-8")))
    path = TRANSLATIONS / f"{language}.json"
    assert path.is_file(), f"missing translations/{language}.json"

    translated = json.loads(path.read_text(encoding="utf-8"))
    assert _flatten(translated) == source, (
        f"translations/{language}.json does not mirror strings.json"
    )
    assert translated["config"]["step"]["user"]["menu_options"], (
        "the menu options need labels or the dialog shows empty rows"
    )


@pytest.mark.parametrize(
    "path",
    [STRINGS, TRANSLATIONS / "en.json", TRANSLATIONS / "sv.json"],
    ids=["strings", "en", "sv"],
)
def test_qr_page_links_open_in_their_own_tab(path: Path) -> None:
    """The QR page is linked through the target=_blank placeholders.

    A markdown link to {qr_url} is routed inside the frontend and shows the
    dashboard. An HTML tag in the text itself breaks the frontend's message
    formatter, so the tag comes in as placeholder values.
    """
    text = path.read_text(encoding="utf-8")
    assert "]({qr_url})" not in text
    assert "<a " not in text
    starts = text.count("{qr_link_start}")
    assert starts >= 1
    assert starts == text.count("{qr_link_end}")
