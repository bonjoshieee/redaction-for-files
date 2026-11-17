import io

import openpyxl
import pytest

from nas_s3_sync import llm_pii
from nas_s3_sync.llm_pii import (
    LlmConfig, LlmUnavailable, extra_needles, find_pii_llm, judge_money_columns,
    plausible_amount, use_column_judge,
)
from nas_s3_sync.office_redact import redact_xlsx
from nas_s3_sync.redact import redact
from nas_s3_sync.twin import redact_folder

CFG = LlmConfig()
A = "[AMOUNT_REDACTED]"


def fake(amounts):
    return lambda cfg, text: {"amounts": amounts}


def test_only_amounts_really_in_the_text_are_kept():
    text = "Payment Due 91.200.( for notarization, OR No. 0147"
    pairs = find_pii_llm(text, CFG, ask=fake(["91.200.(", "PHP 5,000.00", "1", "only"]))
    assert pairs == [("91.200.(", A)]  # made-up and too-short answers are dropped


@pytest.mark.parametrize("s,ok", [
    ("300", True), ("1,200", True), ("One thousand two hundred pesos only", True),
    ("1", False), ("only", False), ("a", False),
])
def test_plausible_amount(s, ok):
    assert plausible_amount(s) is ok


def test_model_amounts_are_redacted_alongside_regex_and_ids_stay():
    with extra_needles([("1,200", A)]):
        out, n = redact("Maria Santos (maria@example.com) paid 1,200 and PHP 300.00, ref 11,200")
    assert out == f"Maria Santos (maria@example.com) paid {A} and {A}, ref 11,200"
    assert n == 2
    assert redact("paid 1,200")[1] == 0  # outside the block: regex only


def test_long_text_is_sent_in_chunks(monkeypatch):
    monkeypatch.setattr(llm_pii, "CHUNK_CHARS", 50)
    seen = []

    def ask(cfg, text):
        seen.append(text)
        return {"amounts": []}

    find_pii_llm("line of text here\n" * 20, CFG, ask=ask)
    assert len(seen) > 1 and all(len(c) <= 50 for c in seen)


def test_unreachable_ollama_raises():
    with pytest.raises(LlmUnavailable):
        find_pii_llm("hello there", LlmConfig(url="http://127.0.0.1:9", timeout=2))


def test_twin_uses_model_for_amounts(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_pii, "_ask", fake(["1,200"]))
    root = tmp_path / "AP"
    root.mkdir()
    (root / "memo.txt").write_text("To: Maria Santos\nAmount: 1,200\nOR No. 0147")
    redact_folder(root, log=lambda *_: None, llm=CFG)
    assert (tmp_path / "AP-redacted/memo.txt").read_text() == f"To: Maria Santos\nAmount: {A}\nOR No. 0147"


def test_run_stops_when_ollama_is_down_and_deletes_nothing(tmp_path):
    root = tmp_path / "HR"
    root.mkdir()
    (root / "a.txt").write_text("hello world")
    twin = tmp_path / "HR-redacted"
    twin.mkdir()
    (twin / "keep.txt").write_text("existing copy")
    with pytest.raises(LlmUnavailable):
        redact_folder(root, log=lambda *_: None, llm=LlmConfig(url="http://127.0.0.1:9", timeout=2))
    assert (twin / "keep.txt").exists()


# --- Excel columns ------------------------------------------------------------------------


def test_judge_keeps_only_known_column_ids():
    cols = [{"id": "S!B", "header": "Amount", "samples": [1]}, {"id": "S!C", "header": "Qty", "samples": [2]}]
    chat = lambda cfg, prompt, schema: {"money_columns": ["S!B", "S!Z"]}
    assert judge_money_columns(CFG, cols, chat=chat) == {"S!B"}


def test_plain_number_columns_judged_money_are_redacted_with_their_totals():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Billing"
    ws.append(["Client", "Qty", "Amount"])
    ws.append(["A", 3, 12500])
    ws.append(["B", 5, 300])
    ws["C4"] = "=SUM(C2:C3)"
    ws["B4"] = "=SUM(B2:B3)"
    buf = io.BytesIO()
    wb.save(buf)

    asked = []

    def judge(columns):
        asked.extend(columns)
        return {c["id"] for c in columns if c["header"] == "Amount"}

    with use_column_judge(judge):
        out, n = redact_xlsx(buf.getvalue())
    ws = openpyxl.load_workbook(io.BytesIO(out))["Billing"]
    assert [ws[c].value for c in ("C2", "C3", "C4")] == [A, A, A]
    assert [ws[c].value for c in ("B2", "B3", "B4")] == [3, 5, "=SUM(B2:B3)"]
    assert {c["id"]: c["header"] for c in asked} == {"Billing!B": "Qty", "Billing!C": "Amount"}


def test_cut_off_answer_is_retried_on_smaller_pieces():
    calls = []

    def ask(cfg, text):
        calls.append(len(text))
        if len(text) > 400:
            raise llm_pii.LlmBadAnswer("cut off")
        return {"amounts": [a for a in ("PHP 1,000.00", "PHP 2,000.00") if a in text]}

    text = "fee PHP 1,000.00\n" + "filler line\n" * 60 + "fee PHP 2,000.00\n"
    pairs = find_pii_llm(text, CFG, ask=ask)
    assert {s for s, _ in pairs} == {"PHP 1,000.00", "PHP 2,000.00"}
    assert len(calls) > 1


def test_xlsx_model_text_has_no_numbers():
    from nas_s3_sync.extract_office import xlsx_text_cells

    wb = openpyxl.Workbook()
    wb.active.append(["Client", "Amount", 12500, "paid PHP 300.00"])
    buf = io.BytesIO()
    wb.save(buf)
    assert xlsx_text_cells(buf.getvalue()).splitlines() == ["Client", "Amount", "paid PHP 300.00"]


def test_percent_formulas_and_item_numbers_stay():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Item", "Wt %", "Unit cost"])
    ws.append([1.01, None, 6953.57])
    ws.append([1.02, None, 500.25])
    ws["B2"] = "=C2/SUM(C2:C3)"
    ws["B2"].number_format = "0.00%"
    buf = io.BytesIO()
    wb.save(buf)
    with use_column_judge(lambda cols: {c["id"] for c in cols if c["header"] == "Unit cost"}):
        out, _ = redact_xlsx(buf.getvalue())
    ws = openpyxl.load_workbook(io.BytesIO(out)).active
    assert ws["C2"].value == ws["C3"].value == A
    assert ws["A2"].value == 1.01 and ws["B2"].value == "=C2/SUM(C2:C3)"
