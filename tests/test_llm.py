import pytest

import llm


@pytest.fixture()
def fake_gemini(monkeypatch):
    """Swap the network call for a canned response."""
    box = {}

    class Fake:
        def complete(self, prompt):
            box["prompt"] = prompt
            return box["reply"]

    monkeypatch.setattr(llm, "GeminiBackend", lambda: Fake())
    return box


def test_parse_json_object_survives_fences_and_prose():
    assert llm.parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json_object('Sure!\n{"a": 2}\nHope that helps') == {"a": 2}
    with pytest.raises(llm.LLMError):
        llm.parse_json_object("no json at all")
    with pytest.raises(llm.LLMError):
        llm.parse_json_object("[1, 2, 3]")          # array, not object


def test_load_keys_splits_and_trims(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEYS", " a , b ,, c ")
    assert llm.load_keys() == ["a", "b", "c"]
    monkeypatch.setenv("GEMINI_API_KEYS", "")
    assert llm.load_keys() == [] and llm.configured() is False


def test_estimate_accepts_a_contiguous_in_order_cover(fake_gemini):
    fake_gemini["reply"] = {"ranges": [
        {"n": 1, "from": 1, "to": 4},
        {"n": 2, "from": 5, "to": 9},
        {"n": 3, "from": 10, "to": 12},
    ]}
    out = llm.estimate_lecture_ranges("DEMO 208", "Example course subject 35", 12,
                                      ["Binary", "Combinational", "Sequential"])
    assert out == [(1, 4), (5, 9), (10, 12)]
    assert "DEMO 208" in fake_gemini["prompt"]


@pytest.mark.parametrize("ranges, why", [
    ([{"n": 1, "from": 1, "to": 4}], "too few ranges"),
    ([{"n": 1, "from": 1, "to": 4}, {"n": 2, "from": 4, "to": 9}], "overlaps"),
    ([{"n": 1, "from": 1, "to": 4}, {"n": 2, "from": 6, "to": 9}], "leaves a gap"),
    ([{"n": 1, "from": 2, "to": 4}, {"n": 2, "from": 5, "to": 9}], "does not start at 1"),
    ([{"n": 1, "from": 1, "to": 4}, {"n": 2, "from": 5, "to": 7}], "stops short of the total"),
    ([{"n": 1, "from": 1, "to": 4}, {"n": 2, "from": 5, "to": 99}], "runs past the total"),
    ([{"n": 1, "from": 5, "to": 1}, {"n": 2, "from": 6, "to": 9}], "is backwards"),
])
def test_estimate_rejects_anything_that_is_not_a_clean_cover(fake_gemini, ranges, why):
    """A mis-mapped range silently marks the wrong topics done, which is worse
    than having no estimate at all."""
    fake_gemini["reply"] = {"ranges": ranges}
    with pytest.raises(llm.LLMError):
        llm.estimate_lecture_ranges("X", "Y", 9, ["a", "b"])


def test_estimate_refuses_when_there_are_more_topics_than_lectures(fake_gemini):
    fake_gemini["reply"] = {"ranges": []}
    with pytest.raises(llm.LLMError, match="cannot cover"):
        llm.estimate_lecture_ranges("X", "Y", 2, ["a", "b", "c"])


def test_estimate_with_no_topics_does_not_call_the_model(monkeypatch):
    monkeypatch.setattr(llm, "GeminiBackend",
                        lambda: pytest.fail("should not be called"))
    assert llm.estimate_lecture_ranges("X", "Y", 10, []) == []
