"""Local UI: streamlit run hrv_review/app.py --server.address 127.0.0.1."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from hrv_review.core import (
    DEFAULT_ROOT, DEFAULT_STORE, POLICY, VERSION, analyze, comparison_rows,
    draft_for, latest_review, load_recording, rejection_reason, save_review,
)

LABELS = {"before": "До", "practice": "Практика", "after": "После", "session": "Сеанс"}
COLORS = ["#3182ce", "#38a169", "#d69e2e", "#805ad5"]


def metric_table(items):
    return pd.DataFrame([{
        "Этап": LABELS.get(item["phase"], item["phase"]),
        "Минут": round(item["duration_sec"] / 60, 2),
        "RMSSD, мс": item["rmssd_ms"], "lnRMSSD": item["ln_rmssd"],
        "SDNN, мс": item["sdnn_ms"], "Пульс, уд/мин": item["mean_hr_bpm"],
        "Принято RR": item["accepted_beats"], "Исключено": item["excluded_beats"],
        "Покрытие, %": round(item["coverage"] * 100, 1),
        "Проверка": "; ".join(item["issues"]) or "Автопроверки пройдены",
        **({"Для сравнения": item["comparison_ok"]} if "comparison_ok" in item else {}),
    } for item in items])


def show_metrics(items):
    frame = metric_table(items)
    order = ["Этап", "Минут", "RMSSD, мс"]
    if "Для сравнения" in frame:
        order.append("Для сравнения")
    order += ["SDNN, мс", "Пульс, уд/мин", "Проверка", "lnRMSSD", "Принято RR", "Исключено", "Покрытие, %"]
    st.dataframe(frame, hide_index=True, width="stretch", column_order=order, column_config={
        name: st.column_config.NumberColumn(format="%.2f")
        for name in ("Минут", "RMSSD, мс", "lnRMSSD", "SDNN, мс", "Пульс, уд/мин", "Покрытие, %")
    })


def rr_figure(recording, draft):
    figure = go.Figure()
    x, y, rejected_x, rejected_y, reasons = [], [], [], [], []
    previous = None
    exclusions = set(draft["excluded_indices"])
    for row in recording.rows:
        if previous is not None and (
            row["segment"] != previous["segment"] or row["index"] != previous["index"] + 1
            or row["time"] - previous["time"] > POLICY["receipt_gap_sec"]):
            x.append(None)
            y.append(None)
        reason = rejection_reason(row, exclusions)
        x.append(row["time"] / 60)
        y.append(None if reason else row["raw"])
        if reason and row["raw"] is not None:
            rejected_x.append(row["time"] / 60)
            rejected_y.append(row["raw"])
            reasons.append(f"#{row['index']} · {reason}")
        if previous is not None and row["segment"] != previous["segment"]:
            figure.add_vline(x=row["time"] / 60, line_dash="dot", line_color="#d97706")
        previous = row
    figure.add_trace(go.Scatter(x=x, y=y, name="Исходные RR", mode="lines",
                                line={"color": "#2563eb", "width": 1.5}, connectgaps=False))
    figure.add_trace(go.Scatter(x=rejected_x, y=rejected_y, name="Исключённые RR", mode="markers",
                                marker={"color": "#dc2626", "size": 7}, text=reasons,
                                hovertemplate="%{text}<br>%{y:.2f} мс<extra></extra>"))
    for index, phase in enumerate(draft["phases"]):
        figure.add_vrect(x0=phase["start_sec"] / 60, x1=phase["end_sec"] / 60,
                         fillcolor=COLORS[index % len(COLORS)], opacity=0.09, line_width=0,
                         annotation_text=LABELS.get(phase["name"], phase["name"]))
    figure.update_layout(height=360, margin={"l": 20, "r": 20, "t": 25, "b": 20},
                         xaxis_title="Минуты от начала записи", yaxis_title="RR, мс",
                         legend={"orientation": "h", "y": 1.15}, template="plotly_white")
    return figure


def run():
    st.set_page_config(page_title="Анализ HRV", page_icon="◉", layout="wide")
    st.title("Анализ HRV")
    st.caption("Сохранённые интервалы → разметка этапов → воспроизводимые расчёты NeuroKit2")
    with st.sidebar:
        st.header("Записи")
        root = Path(st.text_input("Папка с сеансами", str(DEFAULT_ROOT))).expanduser()
        store = Path(st.text_input("Папка разметок", str(DEFAULT_STORE))).expanduser()
        if st.button("Обновить записи"):
            st.session_state.pop("review_key", None)
        st.caption("Работает локально. Исходные записи открываются только для чтения.")
    recordings, errors = [], []
    for path in sorted(root.glob("**/meditation.json"), reverse=True):
        try:
            recordings.append(load_recording(path.parent))
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f"{path.parent.name}: {error}")
    if errors:
        with st.expander(f"Не открыты записи: {len(errors)}"):
            for error in errors:
                st.write(error)
    if not recordings:
        st.info("В этой папке нет завершённых записей с rr_intervals.csv и meditation.json.")
        return
    choice = st.sidebar.selectbox("Сеанс", range(len(recordings)), format_func=lambda i: (
        recordings[i].metadata.get("started_at", "")[:19].replace("T", " ") + " · "
        + str((recordings[i].metadata.get("diary") or {}).get("technique") or recordings[i].directory.name)))
    recording = recordings[choice]
    key = (recording.key, recording.raw_hash, recording.metadata_hash, str(store))
    if st.session_state.get("review_key") != key:
        try:
            saved = latest_review(recording, store, allow_stale=True)
        except (OSError, ValueError) as error:
            st.error(str(error))
            return
        st.session_state.review_key = key
        stale = bool(saved and saved["source"] != recording.source)
        st.session_state.source_changed = stale
        st.session_state.draft = copy.deepcopy(saved["draft"] if saved and not stale else draft_for(recording))
        st.session_state.parent = saved["revision"] if saved else None
        st.session_state.edit_generation = st.session_state.get("edit_generation", 0) + 1
    draft = st.session_state.draft
    if st.session_state.get("source_changed"):
        st.warning("Исходная запись изменилась. Старая разметка сохранена в истории версий и не применена. "
                   "Проверьте текущие границы и сохраните новую версию.")
    try:
        result = analyze(recording, draft)
    except ValueError as error:
        st.error(str(error))
        return
    review_tab, comparison_tab, method_tab = st.tabs(["Сеанс и этапы", "Сравнение сеансов", "Методика"])
    with review_tab:
        st.subheader("Показатели за каждый этап")
        st.caption("Рассчитаны по всей выбранной длительности этапа. Короткие участки тоже показаны; "
                   "они не становятся пятиминутными измерениями.")
        show_metrics(result["phases"])
        st.plotly_chart(rr_figure(recording, draft), width="stretch")
        st.caption("Красные точки — исключения. Пунктир — отметки регистратора, которые сами по себе "
                   "не доказывают обрыв Bluetooth. Приблизьте график для просмотра участка.")
        with st.expander("Редактировать этапы и условия", expanded=True):
            with st.form(f"phases-{recording.key}-{st.session_state.edit_generation}"):
                edited = []
                for i, phase in enumerate(draft["phases"]):
                    columns = st.columns([2, 1, 1])
                    name = columns[0].text_input(f"Имя этапа {i + 1}", phase["name"])
                    start = columns[1].number_input(f"Начало {i + 1}, мин", min_value=0.0,
                                                     value=phase["start_sec"] / 60, format="%.4f")
                    end = columns[2].number_input(f"Конец {i + 1}, мин", min_value=0.0,
                                                   value=phase["end_sec"] / 60, format="%.4f")
                    edited.append({"name": name.strip(), "start_sec": start * 60, "end_sec": end * 60})
                columns = st.columns(3)
                conditions = {
                    "practice": columns[0].text_input("Название практики", draft["conditions"]["practice"]),
                    "posture": columns[1].text_input("Поза", draft["conditions"]["posture"]),
                    "breathing": columns[2].text_input("Дыхание / протокол", draft["conditions"]["breathing"]),
                }
                window = st.number_input("Окно для сравнения, секунд", min_value=30, max_value=3600,
                                         value=int(draft["window_seconds"]), step=30)
                excluded_text = st.text_input("Исключить отсчёты по номерам через запятую",
                                              ", ".join(map(str, draft["excluded_indices"])))
                checked = st.checkbox("Я просмотрел отметки регистратора; они не означают потерю данных",
                                      draft["reviewed_recorder_marks"])
                notes = st.text_area("Заметки и уточнения времени", draft["notes"])
                submitted = st.form_submit_button("Применить и пересчитать")
            if submitted:
                try:
                    updated = {"phases": edited, "conditions": conditions, "window_seconds": window,
                               "excluded_indices": sorted({int(v.strip()) for v in excluded_text.split(",") if v.strip()}),
                               "reviewed_recorder_marks": checked, "notes": notes}
                    analyze(recording, updated)
                    st.session_state.draft = updated
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
        st.subheader("Окна для сопоставления")
        st.caption("Только полные окна выбранной длины с пройденными проверками входят в сравнение. "
                   "Непригодный After не скрывает пригодные Before и практику.")
        show_metrics(result["windows"])
        st.caption("Кнопка ниже сохраняет разметку после «Применить и пересчитать».")
        if st.button("Сохранить версию разметки", type="primary"):
            try:
                path = save_review(recording, draft, store, st.session_state.parent)
                st.session_state.parent = path.stem
                st.session_state.source_changed = False
                st.success(f"Версия сохранена: {path.name}")
            except (OSError, ValueError) as error:
                st.error(str(error))
        st.download_button("Скачать расчёты CSV", metric_table(result["phases"]).to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"{recording.directory.name}-phases.csv", mime="text/csv")
        st.download_button("Скачать полный анализ JSON", json.dumps({"draft": draft, **result}, ensure_ascii=False,
                                                                   indent=2, allow_nan=False),
                           file_name=f"{recording.directory.name}-review.json", mime="application/json")
        with st.expander("Исходные интервалы и причины исключения"):
            st.dataframe(pd.DataFrame([{"№": row["index"], "Время, с": row["time"], "RR, мс": row["raw"],
                                        "Отметка": row["correction"],
                                        "Исключение": rejection_reason(row, set(draft["excluded_indices"]))}
                                       for row in recording.rows]), hide_index=True, width="stretch")
    with comparison_tab:
        st.subheader("Сравнение одинаковых условий")
        st.caption("Используется текущая разметка открытого сеанса и последние сохранённые версии остальных. "
                   "Группы разделены по пользователю, практике, позе, дыханию, этапу, длине окна и времени суток.")
        phase = st.selectbox("Сравниваемый этап", [p["name"] for p in draft["phases"]],
                             format_func=lambda value: LABELS.get(value, value))
        records = []
        for other in recordings:
            if other.key == recording.key:
                records.append((recording, draft, result))
                continue
            try:
                saved = latest_review(other, store)
                other_draft = saved["draft"] if saved else draft_for(other)
                records.append((other, other_draft, analyze(other, other_draft)))
            except (OSError, ValueError) as error:
                st.warning(f"{other.directory.name}: {error}")
        rows = comparison_rows(records, phase)
        groups = sorted({row["group"] for row in rows})
        if not groups:
            st.info("Нет полных пригодных окон этого этапа с заполненными условиями. "
                    "Показатели коротких этапов доступны на первой вкладке.")
        else:
            group = st.selectbox("Условия", groups, format_func=lambda g: " / ".join(map(str, g)))
            selected = pd.DataFrame([row for row in rows if row["group"] == group]).drop(columns="group")
            selected["started_at"] = pd.to_datetime(selected["started_at"], utc=True)
            selected = selected.sort_values("started_at")
            figure = go.Figure(go.Scatter(x=selected["started_at"], y=selected["rmssd_ms"], mode="markers+lines"))
            figure.update_layout(xaxis_title="Дата (UTC)", yaxis_title="RMSSD, мс", height=300)
            st.plotly_chart(figure, width="stretch")
            st.dataframe(selected.rename(columns={"session": "Сеанс", "started_at": "Дата UTC", "rmssd_ms": "RMSSD, мс",
                                                 "windows": "Полных окон"}), hide_index=True, width="stretch")
            if len(selected) < 2:
                st.info("Пока одна сопоставимая запись. Точка показана, но тренда ещё нет.")
    with method_tab:
        st.markdown("""
### Что именно считается
RMSSD и SDNN рассчитываются через **NeuroKit2**. Для разорванной серии RMSSD
объединяется по числу допустимых соседних пар внутри непрерывных участков.
Ни одна пара не пересекает исключённый отсчёт, отметку сегмента или разрыв поступления.
SDNN и средний пульс используют все принятые интервалы выбранного этапа.
Пульс здесь — 60000 / средний RR.

Время на графике — момент получения данных компьютером, а не измеренное время R-пика.
Автопроверки — технические эвристики. Их прохождение не подтверждает отсутствие
движений, эктопических ударов или других артефактов. Мы не исправляем их интерполяцией автоматически.

Числа короткого этапа доступны для просмотра. Сравнение использует одинаковые полные
окна и показывает медиану окон каждого сеанса. Выбор длины окна не доказывает её
научную пригодность для любой задачи. Разные дыхательные протоколы следует разделять.
HRV не является оценкой уровня медитации.

Разметки сохраняются отдельными версиями. Исходные CSV, дневник и расчёты регистратора
не перезаписываются. Экспорт содержит версии алгоритма, NeuroKit2, правила и хеши входных файлов.
""")
        st.json({"algorithm": VERSION, "neurokit": result["neurokit_version"], "policy": POLICY})


if __name__ == "__main__":
    run()
