from pathlib import Path

from streamlit.testing.v1 import AppTest

import hrv_review.core as core
from test_hrv_review import recording


def by_label(widgets, label):
    return next(widget for widget in widgets if widget.label == label)


def test_review_edit_save_reload_and_source_immutability(tmp_path, monkeypatch):
    rec = recording(tmp_path)
    originals = {p.name: p.read_bytes() for p in rec.directory.iterdir()}
    monkeypatch.setattr(core, "DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(core, "DEFAULT_STORE", tmp_path / "reviews")
    app_path = Path(__file__).parents[1] / "hrv_review/app.py"
    at = AppTest.from_file(app_path, default_timeout=20).run()
    assert not at.exception
    assert at.dataframe[0].value.iloc[0]["Минут"] == 14
    by_label(at.number_input, "Конец 1, мин").set_value(4)
    by_label(at.button, "Применить и пересчитать").click().run()
    assert not at.exception
    assert at.dataframe[0].value.iloc[0]["Минут"] == 4
    assert at.dataframe[0].value.iloc[0]["RMSSD, мс"] == 20
    assert not at.dataframe[1].value.iloc[0]["Для сравнения"]
    by_label(at.button, "Сохранить версию разметки").click().run()
    assert not at.exception and at.success
    assert core.latest_review(rec, tmp_path / "reviews")["draft"]["phases"][0]["end_sec"] == 240
    assert {p.name: p.read_bytes() for p in rec.directory.iterdir()} == originals
    reopened = AppTest.from_file(app_path, default_timeout=20).run()
    assert not reopened.exception
    assert reopened.dataframe[0].value.iloc[0]["Минут"] == 4
