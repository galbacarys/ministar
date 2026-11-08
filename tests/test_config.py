"""Runtime paths, naming, and the one folder name a session gets."""

from __future__ import annotations

from datetime import datetime

from ministar.config import default_session_name, safe_folder_name, session_token


def test_the_default_session_name_is_date_time_and_target_code() -> None:
    when = datetime(2026, 11, 7, 22, 15)
    assert default_session_name("M31", when) == "2026_11_07-22-15-M31"


def test_a_long_label_becomes_one_safe_folder_component() -> None:
    """The display label carries common names; the folder gets the sanitised form."""
    when = datetime(2026, 11, 7, 22, 15)
    assert (
        default_session_name("M31  NGC0224  Andromeda Galaxy", when)
        == "2026_11_07-22-15-M31_NGC0224_Andromeda_Galaxy"
    )


def test_no_target_still_produces_a_name() -> None:
    when = datetime(2026, 11, 7, 22, 15)
    assert default_session_name(None, when) == "2026_11_07-22-15"


def test_windows_path_characters_are_removed() -> None:
    assert session_token('M31/Andromeda:*?"<>|') == "M31_Andromeda"


def test_a_typed_folder_name_keeps_spaces_but_cannot_escape_the_root() -> None:
    assert safe_folder_name("M31 first try") == "M31 first try"
    escaped = safe_folder_name("../../etc/passwd")
    assert "/" not in escaped
    assert escaped != ".."
