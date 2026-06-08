from db.base import _now, _row_to_dict, _rows_to_dicts


def test_now_is_utc_isoformat():
    ts = _now()
    assert "T" in ts
    assert ts.endswith("+00:00")


def test_row_to_dict_none_and_json_fields():
    assert _row_to_dict(None) is None

    row = {"id": "1", "meta": '{"k":1}', "bad": "{not-json}", "plain": "x"}
    d = _row_to_dict(row, json_fields={"meta": {}, "bad": {"fallback": True}})
    assert d["id"] == "1"
    assert d["meta"] == {"k": 1}
    assert d["bad"] == {"fallback": True}
    assert d["plain"] == "x"


def test_rows_to_dicts_delegates_row_to_dict():
    rows = [
        {"id": "1", "meta": '{"a":1}'},
        {"id": "2", "meta": '{"b":2}'},
    ]
    out = _rows_to_dicts(rows, json_fields={"meta": {}})
    assert out == [{"id": "1", "meta": {"a": 1}}, {"id": "2", "meta": {"b": 2}}]
