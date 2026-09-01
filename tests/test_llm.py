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


def TOPIC(title, unit="", hours=1):
    return {"title": title, "unit": unit, "hours": hours}


def NOTE(date, text):
    return {"date": date, "text": text}


def test_resolve_coverage_returns_one_state_per_topic(fake_gemini):
    fake_gemini["reply"] = {
        "topics": [{"n": 1, "state": "done"}, {"n": 2, "state": "partial"},
                   {"n": 3, "state": "pending"}],
        "summary": "Mid-way through combinational logic.",
        "unmatched": [],
    }
    out = llm.resolve_coverage(
        "DEMO 208", "Example course subject 35",
        [TOPIC("Binary", hours=2), TOPIC("Combinational"), TOPIC("Sequential")],
        [NOTE("2054-08-20", "finished binary, started K-maps")])

    assert out["states"] == ["done", "partial", "pending"]
    assert out["summary"].startswith("Mid-way")
    assert "DEMO 208" in fake_gemini["prompt"]
    assert "finished binary" in fake_gemini["prompt"]     # the log is sent
    assert "2 lecture-hours" in fake_gemini["prompt"]     # so are the weights


def test_resolve_coverage_lets_a_later_topic_be_done_before_an_earlier_one(
        fake_gemini):
    """Instructors reorder and classes run late, so out-of-order coverage is a
    legitimate answer and must not be normalised away."""
    fake_gemini["reply"] = {"topics": [{"n": 1, "state": "pending"},
                                       {"n": 2, "state": "done"}]}
    out = llm.resolve_coverage("X", "Y", [TOPIC("a"), TOPIC("b")],
                               [NOTE("2054-08-20", "did b, skipped a")])
    assert out["states"] == ["pending", "done"]


@pytest.mark.parametrize("topics, why", [
    ([{"n": 1, "state": "done"}], "one state short"),
    ([{"n": 1, "state": "done"}, {"n": 3, "state": "done"}], "keyed wrong"),
    ([{"n": 1, "state": "done"}, {"n": 2, "state": "maybe"}], "invalid state"),
    ([], "nothing at all"),
])
def test_resolve_coverage_rejects_an_answer_that_is_not_one_state_per_topic(
        fake_gemini, topics, why):
    """A short or mis-keyed answer would silently mark the wrong half of a
    syllabus covered, which is worse than no answer."""
    fake_gemini["reply"] = {"topics": topics}
    with pytest.raises(llm.LLMError):
        llm.resolve_coverage("X", "Y", [TOPIC("a"), TOPIC("b")],
                             [NOTE("2054-08-20", "did a")])


def test_resolve_coverage_refuses_without_a_log_or_a_syllabus(monkeypatch):
    monkeypatch.setattr(llm, "GeminiBackend",
                        lambda: pytest.fail("should not be called"))
    with pytest.raises(llm.LLMError, match="no syllabus"):
        llm.resolve_coverage("X", "Y", [], [NOTE("2054-08-20", "did a")])
    with pytest.raises(llm.LLMError, match="log"):
        llm.resolve_coverage("X", "Y", [TOPIC("a")], [])


def test_estimate_hours_accepts_weights_that_add_up(fake_gemini):
    fake_gemini["reply"] = {"hours": [{"n": 1, "hours": 4},
                                      {"n": 2, "hours": 5},
                                      {"n": 3, "hours": 3}]}
    assert llm.estimate_hours("DEMO 208", "Example course subject 35", ["a", "b", "c"],
                              12) == [4, 5, 3]


@pytest.mark.parametrize("hours, why", [
    ([{"n": 1, "hours": 4}], "too few"),
    ([{"n": 1, "hours": 4}, {"n": 3, "hours": 5}], "keyed wrong"),
    ([{"n": 1, "hours": 4}, {"n": 2, "hours": 4}], "do not add up"),
    ([{"n": 1, "hours": 0}, {"n": 2, "hours": 9}], "a topic worth nothing"),
])
def test_estimate_hours_rejects_a_weighting_that_does_not_hold(
        fake_gemini, hours, why):
    fake_gemini["reply"] = {"hours": hours}
    with pytest.raises(llm.LLMError):
        llm.estimate_hours("X", "Y", ["a", "b"], 9)


def test_estimate_hours_refuses_fewer_hours_than_topics(fake_gemini):
    fake_gemini["reply"] = {"hours": []}
    with pytest.raises(llm.LLMError, match="cannot cover"):
        llm.estimate_hours("X", "Y", ["a", "b", "c"], 2)


def test_estimate_hours_with_no_topics_does_not_call_the_model(monkeypatch):
    monkeypatch.setattr(llm, "GeminiBackend",
                        lambda: pytest.fail("should not be called"))
    assert llm.estimate_hours("X", "Y", [], 10) == []
