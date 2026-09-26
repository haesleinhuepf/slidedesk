from pathlib import Path
from types import SimpleNamespace

from slidedesk import convert


def test_conversion_and_repair_close_presentations_but_leave_powerpoint_open(
    tmp_path, monkeypatch
):
    applications = []

    class FakePresentation:
        def __init__(self):
            self.close_calls = 0

        def SaveAs(self, path, file_format):
            path = Path(path)
            path.write_bytes(str(file_format).encode())

        def Close(self):
            self.close_calls += 1

    class FakeApplication:
        def __init__(self):
            self.quit_calls = 0
            self.presentations = []
            self.Presentations = SimpleNamespace(Open=self.open)

        def open(self, *args, **kwargs):
            presentation = FakePresentation()
            self.presentations.append(presentation)
            return presentation

        def Quit(self):
            self.quit_calls += 1

    def dispatch_ex(_name):
        app = FakeApplication()
        applications.append(app)
        return app

    pythoncom = SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None)
    win32com_client = SimpleNamespace(DispatchEx=dispatch_ex)
    monkeypatch.setattr(convert.platform, "system", lambda: "Windows")
    monkeypatch.setattr(convert._local, "app", None, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "pythoncom", pythoncom)
    monkeypatch.setitem(
        __import__("sys").modules,
        "win32com",
        SimpleNamespace(client=win32com_client),
    )
    monkeypatch.setitem(__import__("sys").modules, "win32com.client", win32com_client)

    source = tmp_path / "source.pptx"
    source.write_bytes(b"source")
    pdf = tmp_path / "source.pdf"
    repaired = tmp_path / "repaired.pptx"

    convert.convert_pptx_to_pdf(source, pdf)
    convert.shutdown()
    convert.repair_pptx(source, repaired)

    assert pdf.is_file()
    assert repaired.is_file()
    assert convert._local.app is applications[0]
    assert len(convert._apps) >= 2
    assert all(app.quit_calls == 0 for app in applications)
    assert all(
        presentation.close_calls == 1
        for app in applications
        for presentation in app.presentations
    )