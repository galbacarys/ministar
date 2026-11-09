"""The Linux camera driver, against a fake libgphoto2.

The real body is not needed to prove the things that will silently break on it:
settings are snapped to the body's own choice list and read back, capture uses the
file path the runner expects, and focus pulses alternate with ``None`` (libgphoto2
#767). Those are all decisions in :mod:`ministar.camera.gphoto2`, so a fake widget
tree is enough.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ministar.camera import backend_name
from ministar.camera.base import CameraSettings
from ministar.camera.gphoto2 import (
    Gphoto2Camera,
    Gphoto2Unavailable,
    parse_aperture,
    parse_iso,
    parse_shutter,
)
from ministar.focus.driver import Direction

#: Widget types are opaque integers to the driver; only equality with the module
#: constants matters. The fake module supplies the same values.
SECTION = 1
TOGGLE = 2
RADIO = 5
MENU = 6


class FakeWidget:
    def __init__(
        self,
        name: str,
        *,
        value: Any = "",
        choices: list[str] | None = None,
        children: list[FakeWidget] | None = None,
        kind: int = RADIO,
        readonly: bool = False,
        frozen: bool = False,
    ) -> None:
        self.name = name
        self.value = value
        self.choices = list(choices or [])
        self.children = list(children or [])
        self.kind = kind
        self.readonly = readonly
        self.frozen = frozen

    def get_name(self) -> str:
        return self.name

    def get_label(self) -> str:
        return self.name

    def get_type(self) -> int:
        return self.kind

    def get_readonly(self) -> bool:
        return self.readonly

    def count_children(self) -> int:
        return len(self.children)

    def get_children(self) -> list[FakeWidget]:
        return self.children

    def count_choices(self) -> int:
        return len(self.choices)

    def get_choice(self, index: int) -> str:
        return self.choices[index]

    def get_value(self) -> Any:
        return self.value

    def set_value(self, value: Any) -> None:
        if self.frozen:
            return
        if self.choices and self.kind in (RADIO, MENU) and value not in self.choices:
            raise ValueError(f"{value!r} is not one of {self.choices}")
        self.value = value


class FakeFile:
    def __init__(self, payload: bytes = b"CR2DATA") -> None:
        self.payload = payload

    def get_data_and_size(self) -> bytes:
        return self.payload

    def save(self, target: str) -> None:
        Path(target).write_bytes(self.payload)


class FakeCamera:
    def __init__(self, root: FakeWidget, *, init_error: Exception | None = None) -> None:
        self.root = root
        self.init_error = init_error
        self.init_calls = 0
        self.exit_calls = 0
        self.set_config_calls = 0
        self.focus_sequence: list[str] = []
        self.capture_calls = 0
        self.preview_calls = 0
        self._focus = find_widget(root, "manualfocusdrive")

    def init(self) -> None:
        self.init_calls += 1
        if self.init_error is not None:
            raise self.init_error

    def exit(self) -> None:
        self.exit_calls += 1

    def get_config(self) -> FakeWidget:
        return self.root

    def set_config(self, config: FakeWidget) -> None:
        self.set_config_calls += 1
        if self._focus is not None:
            self.focus_sequence.append(str(self._focus.get_value()))

    def get_summary(self) -> str:
        return "Manufacturer: Canon\nModel: Canon EOS 60D\nSerial Number: 123\n"

    def capture(self, kind: int) -> SimpleNamespace:
        self.capture_calls += 1
        assert kind == 0x1234  # GP_CAPTURE_IMAGE in the fake module
        return SimpleNamespace(folder="/store_00010001/DCIM/100CANON", name="IMG_0001.CR2")

    def file_get(self, folder: str, name: str, kind: int) -> FakeFile:
        assert folder and name
        assert kind == 0x5678  # GP_FILE_TYPE_NORMAL
        return FakeFile()

    def capture_preview(self) -> FakeFile:
        self.preview_calls += 1
        return FakeFile(b"\xff\xd8fake-jpeg")


def find_widget(root: FakeWidget, name: str) -> FakeWidget | None:
    if root.get_name() == name:
        return root
    for child in root.get_children():
        found = find_widget(child, name)
        if found is not None:
            return found
    return None


def build_tree(*, focus: bool = True) -> tuple[FakeWidget, FakeWidget]:
    iso = FakeWidget("iso", value="800", choices=["100", "200", "400", "800", "1600"])
    shutter = FakeWidget(
        "shutterspeed", value="1/60", choices=["1/60", "1/30", "0.5", "1", "2", "4"]
    )
    # The 60D's real widget name; f-number is the alias some other bodies use.
    aperture = FakeWidget("aperture", value="2.8", choices=["2.8", "4", "5.6"])
    viewfinder = FakeWidget("viewfinder", value="1", choices=["0", "1"])
    capturetarget = FakeWidget(
        "capturetarget", value="Memory card", choices=["Memory card", "Internal RAM"]
    )
    actions = [viewfinder]
    if focus:
        actions.append(
            FakeWidget(
                "manualfocusdrive",
                value="None",
                choices=["Near 3", "Near 2", "Near 1", "None", "Far 1", "Far 2", "Far 3"],
            )
        )
    root = FakeWidget(
        "",
        kind=SECTION,
        children=[
            FakeWidget(
                "main",
                kind=SECTION,
                children=[
                    FakeWidget(
                        "status",
                        kind=SECTION,
                        children=[FakeWidget("iso", value="800", choices=["800"], readonly=True)],
                    ),
                    FakeWidget("imgsettings", kind=SECTION, children=[iso]),
                    FakeWidget("capturesettings", kind=SECTION, children=[shutter, aperture]),
                    FakeWidget(
                        "settings",
                        kind=SECTION,
                        children=[capturetarget],
                    ),
                    FakeWidget("actions", kind=SECTION, children=actions),
                ],
            )
        ],
    )
    return root, iso


def fake_gp(camera: FakeCamera) -> SimpleNamespace:
    return SimpleNamespace(
        Camera=lambda: camera,
        GP_CAPTURE_IMAGE=0x1234,
        GP_FILE_TYPE_NORMAL=0x5678,
        GP_WIDGET_SECTION=SECTION,
        GP_WIDGET_TOGGLE=TOGGLE,
        GP_WIDGET_RADIO=RADIO,
        GP_WIDGET_MENU=MENU,
    )


def connected(*, focus: bool = True) -> tuple[Gphoto2Camera, FakeCamera, FakeWidget, FakeWidget]:
    root, iso = build_tree(focus=focus)
    fake = FakeCamera(root)
    camera = Gphoto2Camera(gp=fake_gp(fake), sleep=lambda _: None, settle_s=0.0)
    camera.connect()
    return camera, fake, root, iso


def test_connect_reports_the_model_and_prefers_the_writable_iso() -> None:
    camera, _fake, _root, _iso = connected()
    try:
        assert camera.connected
        assert camera.name == "Canon EOS 60D"
        # The status tree repeats iso; the writable imgsettings one must win.
        assert camera.capabilities["iso"] == "/main/imgsettings/iso"
        assert camera.capabilities["aperture"] == "/main/capturesettings/aperture"
        assert camera.capabilities["manualfocusdrive"] == "/main/actions/manualfocusdrive"
    finally:
        camera.disconnect()


def test_settings_are_read_from_the_body() -> None:
    camera, _fake, _root, _iso = connected()
    try:
        settings = camera.get_settings()
        assert settings.iso == 800
        assert settings.shutter_seconds == pytest.approx(1 / 60)
        assert settings.aperture == pytest.approx(2.8)
        assert camera.iso_options() == (100, 200, 400, 800, 1600)
    finally:
        camera.disconnect()


def test_f_number_is_accepted_as_an_aperture_alias() -> None:
    camera, _fake, root, _iso = connected()
    try:
        widget = find_widget(root, "aperture")
        assert widget is not None
        widget.name = "f-number"
        assert camera.get_settings().aperture == pytest.approx(2.8)
    finally:
        camera.disconnect()


def test_apply_snaps_to_the_bodys_choices_and_reads_back() -> None:
    camera, _fake, _root, _iso = connected()
    try:
        camera.apply(CameraSettings(iso=1000, shutter_seconds=0.45, aperture=4.0))
        settings = camera.get_settings()
        assert settings.iso == 800  # nearest available; 800/1600 tie, first wins
        assert settings.shutter_seconds == pytest.approx(0.5)
        assert settings.aperture == pytest.approx(4.0)
    finally:
        camera.disconnect()


def test_apply_rejects_a_body_that_clamps_iso() -> None:
    camera, _fake, _root, iso = connected()
    iso.frozen = True
    try:
        with pytest.raises(RuntimeError, match="did not accept ISO"):
            camera.apply(CameraSettings(iso=1600, shutter_seconds=1.0))
    finally:
        camera.disconnect()


def test_capture_uses_internal_ram_and_restores_the_card_on_exit(tmp_path: Path) -> None:
    camera, _fake, root, _iso = connected()
    target = next(w for w in root.get_children()[0].get_children() if w.get_name() == "settings")
    capturetarget = next(w for w in target.get_children() if w.get_name() == "capturetarget")
    try:
        assert capturetarget.get_value() == "Internal RAM"
        written = camera.capture_to(tmp_path / "nested" / "frame.cr2")
        assert written.read_bytes() == b"CR2DATA"
    finally:
        camera.disconnect()
    assert capturetarget.get_value() == "Memory card"


def test_pulse_alternates_with_none_and_counts_position() -> None:
    camera, fake, _root, _iso = connected()
    try:
        start = len(fake.focus_sequence)
        result = camera.pulse(Direction.NEAR_3, count=2)
        assert result.ok and result.moved and result.pulses_sent == 2
        assert fake.focus_sequence[start:] == ["Near 3", "None", "Near 3", "None"]
        assert camera.position == 6
        camera.reset_position()
        assert camera.position == 0
        camera.pulse(Direction.FAR_2, count=1)
        assert camera.position == -2
    finally:
        camera.disconnect()


def test_pulse_without_the_widget_is_a_result_not_a_crash() -> None:
    camera, _fake, _root, _iso = connected(focus=False)
    try:
        result = camera.pulse(Direction.NEAR_1, count=1)
        assert not result.ok
        assert result.pulses_sent == 0
        assert "manualfocusdrive" in result.detail
    finally:
        camera.disconnect()


def test_live_view_and_stop_turn_the_viewfinder_off() -> None:
    camera, fake, root, _iso = connected()
    actions = next(w for w in root.get_children()[0].get_children() if w.get_name() == "actions")
    viewfinder = next(w for w in actions.get_children() if w.get_name() == "viewfinder")
    try:
        assert camera.grab_live_jpeg() == b"\xff\xd8fake-jpeg"
        assert fake.preview_calls == 1
        camera.stop_live_view()
        assert viewfinder.get_value() == "0"
    finally:
        camera.disconnect()


def test_connect_retries_then_reports_the_body_missing() -> None:
    root, _iso = build_tree()
    attempts: list[FakeCamera] = []

    class Failing:
        def __init__(self) -> None:
            self.camera = FakeCamera(root, init_error=RuntimeError("busy"))
            attempts.append(self.camera)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.camera, name)

    camera = Gphoto2Camera(
        gp=SimpleNamespace(Camera=Failing),
        sleep=lambda _: None,
        connect_retries=2,
        connect_retry_s=0.0,
    )
    with pytest.raises(Gphoto2Unavailable, match="no camera visible"):
        camera.connect()
    assert len(attempts) == 2
    assert all(attempt.exit_calls == 1 for attempt in attempts)


def test_parse_helpers_accept_gphoto2_labels() -> None:
    assert parse_iso("Auto") == 0
    assert parse_iso("ISO 1600") == 1600
    assert parse_iso(800) == 800
    assert parse_shutter("1/60") == pytest.approx(1 / 60)
    assert parse_shutter('2"') == 2.0
    assert parse_shutter("bulb") == 0.0
    assert parse_aperture("f/2.8") == pytest.approx(2.8)
    assert parse_aperture("--") is None


def test_backend_name_defaults_by_platform_and_honours_the_override(monkeypatch) -> None:
    monkeypatch.delenv("MINISTAR_CAMERA", raising=False)
    expected = "edsdk" if sys.platform == "win32" else "gphoto2"
    assert backend_name() == expected

    monkeypatch.setenv("MINISTAR_CAMERA", "gphoto2")
    assert backend_name() == "gphoto2"

    monkeypatch.setenv("MINISTAR_CAMERA", "nonsense")
    with pytest.raises(ValueError, match="not a backend"):
        backend_name()
