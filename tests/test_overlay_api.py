"""Regression tests for ``POST /overlay/apply`` (ordered structural overlays)."""

from __future__ import annotations

import copy
import hashlib
import json

from fastapi.testclient import TestClient

from app.main import app
from app.overlay import (
    MAX_ACTIONS,
    evaluate_target,
    parse_target,
)
from app.slicer import canonical_json

client = TestClient(app)


def make_doc(paths=None, **extra) -> dict:
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "Pet API", "version": "1.0"},
        "paths": paths or {},
    }
    doc.update(extra)
    return doc


def apply(doc, actions):
    return client.post(
        "/overlay/apply", json={"document": doc, "actions": actions}
    )


def assert_canonical(node) -> None:
    """Every object in *node* must serialize with keys in sorted order."""
    if isinstance(node, dict):
        assert list(node) == sorted(node), list(node)
        for value in node.values():
            assert_canonical(value)
    elif isinstance(node, list):
        for value in node:
            assert_canonical(value)


def assert_digest_ok(body) -> None:
    encoded = canonical_json(body["contract"])
    assert hashlib.sha256(encoded).hexdigest() == body["sha256"]


def body_entry(resp, index):
    """Return the error entry for *index* from a 422 response body."""
    entries = [a for a in resp.json()["actions"] if a["action_index"] == index]
    assert entries, resp.text
    return entries[0]


# --------------------------------------------------------------------------- #
# Basic success behavior
# --------------------------------------------------------------------------- #

def test_single_update_merges_objects_and_returns_report_and_digest():
    doc = make_doc(paths={"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}})
    resp = apply(doc, [
        {"target": "$.info", "update": {"contact": {"name": "team"}, "title": "New Title"}}
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contract"]["info"] == {
        "title": "New Title",
        "version": "1.0",
        "contact": {"name": "team"},
    }
    assert body["actions"] == [
        {"action_index": 0, "op": "update", "target": "$.info", "matches": ["/info"]}
    ]
    assert_digest_ok(body)
    assert_canonical(body["contract"])


def test_bracket_member_accepts_single_and_double_quotes():
    doc = make_doc(paths={"/pets": {"get": {}}, "/toys": {"post": {}}})
    resp = apply(doc, [
        {"target": '$["paths"][\'/pets\']', "remove": True},
    ])
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["contract"]["paths"]) == {"/toys"}
    assert resp.json()["actions"][0]["matches"] == ["/paths/~1pets"]


def test_array_index_targets_one_element():
    doc = make_doc(paths={}, servers=[{"url": "a"}, {"url": "b"}, {"url": "c"}])
    resp = apply(doc, [{"target": "$.servers[1]", "update": {"url": "b2"}}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["contract"]["servers"] == [
        {"url": "a"}, {"url": "b2"}, {"url": "c"}
    ]
    assert resp.json()["actions"][0]["matches"] == ["/servers/1"]


def test_root_update_merges_into_document():
    doc = make_doc(paths={})
    resp = apply(doc, [{"target": "$", "update": {"x-custom": {"enabled": True}}}])
    assert resp.status_code == 200, resp.text
    contract = resp.json()["contract"]
    assert contract["x-custom"] == {"enabled": True}
    assert contract["openapi"] == "3.0.3"
    assert resp.json()["actions"][0]["matches"] == [""]


def test_minimum_and_maximum_action_counts_are_accepted():
    one = apply(make_doc(), [{"target": "$.info", "update": {"title": "x"}}])
    assert one.status_code == 200, one.text
    fifty = apply(
        make_doc(),
        [{"target": "$.info", "update": {f"k{i}": i}} for i in range(MAX_ACTIONS)],
    )
    assert fifty.status_code == 200, fifty.text


# --------------------------------------------------------------------------- #
# update semantics
# --------------------------------------------------------------------------- #

def test_update_recursively_merges_nested_objects():
    doc = make_doc(paths={}, schema={
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    })
    resp = apply(doc, [{"target": "$.schema", "update": {
        "properties": {"b": {"format": "int32"}, "c": {"type": "boolean"}},
        "required": ["a"],
    }}])
    assert resp.status_code == 200, resp.text
    schema = resp.json()["contract"]["schema"]
    assert schema == {
        "type": "object",
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer", "format": "int32"},
            "c": {"type": "boolean"},
        },
        "required": ["a"],
    }


def test_update_replaces_non_object_target_wholesale():
    doc = make_doc(paths={}, tags=["a", "b", "c"])
    resp = apply(doc, [{"target": "$.tags", "update": ["only"]}])
    assert resp.status_code == 200
    assert resp.json()["contract"]["tags"] == ["only"]


def test_update_array_value_is_replaced_not_merged_elementwise():
    doc = make_doc(paths={}, tags=[{"name": "a"}, {"name": "b"}])
    resp = apply(doc, [{"target": "$.tags", "update": [{"name": "c"}]}])
    assert resp.status_code == 200
    assert resp.json()["contract"]["tags"] == [{"name": "c"}]


def test_update_object_value_replaces_scalar_target():
    doc = make_doc(paths={}, count=3)
    resp = apply(doc, [{"target": "$.count", "update": {"n": 3}}])
    assert resp.status_code == 200
    assert resp.json()["contract"]["count"] == {"n": 3}


def test_update_scalar_value_replaces_object_target():
    doc = make_doc(paths={}, meta={"a": 1})
    resp = apply(doc, [{"target": "$.meta", "update": None}])
    assert resp.status_code == 200
    assert resp.json()["contract"]["meta"] is None


def test_update_wildcard_applies_to_every_match():
    doc = make_doc(paths={
        "/pets": {"get": {"responses": {"200": {"description": "a"}}}},
        "/toys": {"get": {"responses": {"200": {"description": "b"}}}},
    })
    resp = apply(doc, [
        {"target": "$.paths.*.get.responses.200",
         "update": {"content": {"application/json": {}}}}
    ])
    assert resp.status_code == 200, resp.text
    contract = resp.json()["contract"]
    for path, description in (("/pets", "a"), ("/toys", "b")):
        node = contract["paths"][path]["get"]["responses"]["200"]
        assert node == {
            "description": description,
            "content": {"application/json": {}},
        }
    matches = resp.json()["actions"][0]["matches"]
    assert matches == [
        "/paths/~1pets/get/responses/200",
        "/paths/~1toys/get/responses/200",
    ]


def test_update_value_is_copied_independently_per_match():
    doc = {"openapi": "3.0.3", "info": {"title": "x", "version": "1"},
           "paths": {}, "arr": [{}, {}]}
    resp = apply(doc, [
        {"target": "$.arr[*]", "update": {"nested": {"v": 1}}},
        {"target": "$.arr[0].nested", "update": {"extra": 2}},
    ])
    assert resp.status_code == 200, resp.text
    arr = resp.json()["contract"]["arr"]
    assert arr[0] == {"nested": {"v": 1, "extra": 2}}
    # A shared reference would have leaked 'extra' into the second match.
    assert arr[1] == {"nested": {"v": 1}}


# --------------------------------------------------------------------------- #
# remove semantics
# --------------------------------------------------------------------------- #

def test_remove_object_member():
    doc = make_doc(paths={"/pets": {"get": {}, "post": {}}})
    resp = apply(doc, [{"target": "$.paths['/pets'].post", "remove": True}])
    assert resp.status_code == 200
    assert set(resp.json()["contract"]["paths"]["/pets"]) == {"get"}


def test_remove_array_element_shifts_remaining_elements():
    doc = make_doc(paths={}, servers=[{"url": "a"}, {"url": "b"}, {"url": "c"}])
    resp = apply(doc, [{"target": "$.servers[1]", "remove": True}])
    assert resp.status_code == 200
    assert resp.json()["contract"]["servers"] == [{"url": "a"}, {"url": "c"}]


def test_remove_wildcard_over_array_freezes_all_and_empties_it():
    doc = make_doc(paths={}, servers=[{"url": str(i)} for i in range(5)])
    # Naive ascending deletion would hit IndexError and leave stragglers;
    # frozen matches + descending-index deletion must remove every element.
    resp = apply(doc, [{"target": "$.servers[*]", "remove": True}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["contract"]["servers"] == []
    assert resp.json()["actions"][0]["matches"] == [
        f"/servers/{i}" for i in range(5)
    ]


def test_remove_object_wildcard_deletes_every_member():
    doc = make_doc(paths={"/pets": {"get": {}, "post": {}, "delete": {}}})
    resp = apply(doc, [{"target": "$.paths['/pets'].*", "remove": True}])
    assert resp.status_code == 200
    assert resp.json()["contract"]["paths"]["/pets"] == {}


def test_remove_wildcard_across_mixed_arrays_uses_descending_indices_per_parent():
    doc = {"openapi": "3.0.3", "info": {"title": "x", "version": "1"}, "paths": {},
           "groups": {"a": [1, 2, 3], "b": [4, 5]}}
    resp = apply(doc, [{"target": "$.groups.*[*]", "remove": True}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["contract"]["groups"] == {"a": [], "b": []}
    assert resp.json()["actions"][0]["matches"] == [
        "/groups/a/0", "/groups/a/1", "/groups/a/2",
        "/groups/b/0", "/groups/b/1",
    ]


def test_remove_dotted_path_and_special_keys_need_bracket_notation():
    doc = make_doc(paths={}, **{"x-custom": {"a/b": 1, "~tilde": 2}})
    resp = apply(doc, [{"target": "$['x-custom']['a/b']", "remove": True}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["contract"]["x-custom"] == {"~tilde": 2}
    assert resp.json()["actions"][0]["matches"] == [
        "/x-custom/a~1b"
    ]


def test_quoted_member_escapes_are_decoded():
    doc = {"openapi": "3.0.3", "info": {"title": "x", "version": "1"},
           "paths": {}, "a'b": 1, "a\"b": 2, "tab\tname": 3, "uni\\code": 4,
           "uA": 5}
    cases = [
        ("$['a\\'b']", "a'b"),
        ('$["a\\"b"]', 'a"b'),
        ("$['tab\\tname']", "tab\tname"),
        ("$['uni\\\\code']", "uni\\code"),
        ("$['u\\u0041']", "uA"),
    ]
    actions = [{"target": target, "remove": True} for target, _ in cases]
    resp = apply(doc, actions)
    assert resp.status_code == 200, resp.text
    contract = resp.json()["contract"]
    for _, name in cases:
        assert name not in contract
    assert resp.json()["actions"][0]["matches"] == ["/a'b"]


def test_empty_quoted_member_name_is_valid():
    doc = {"openapi": "3.0.3", "info": {"title": "x", "version": "1"},
           "paths": {}, "": {"keep": 1}}
    resp = apply(doc, [{"target": "$[''].keep", "remove": True}])
    assert resp.status_code == 200, resp.text
    assert resp.json()["contract"][""] == {}


# --------------------------------------------------------------------------- #
# Sequential actions and freezing
# --------------------------------------------------------------------------- #

def test_later_action_matches_result_of_previous_update():
    doc = make_doc(paths={})
    resp = apply(doc, [
        {"target": "$.paths", "update": {"/pets": {"get": {}}}},
        {"target": "$.paths['/pets']", "update": {
            "get": {"responses": {"200": {"description": "ok"}}}}},
    ])
    assert resp.status_code == 200, resp.text
    op = resp.json()["contract"]["paths"]["/pets"]["get"]
    assert op["responses"]["200"]["description"] == "ok"
    assert [a["action_index"] for a in resp.json()["actions"]] == [0, 1]


def test_later_action_matches_result_of_previous_remove():
    doc = make_doc(paths={}, servers=[{"url": "a"}, {"url": "b"}, {"url": "c"}])
    # After removing index 1, the old index 2 is now at index 1.
    resp = apply(doc, [
        {"target": "$.servers[1]", "remove": True},
        {"target": "$.servers[1]", "update": {"url": "was-c"}},
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["contract"]["servers"] == [
        {"url": "a"}, {"url": "was-c"}
    ]


def test_reports_record_actual_matches_per_action():
    doc = make_doc(paths={
        "/pets": {"get": {}, "post": {}},
        "/toys": {"get": {}},
    })
    resp = apply(doc, [
        {"target": "$.paths.*.get", "update": {"deprecated": True}},
        {"target": "$.paths.*.post", "remove": True},
    ])
    assert resp.status_code == 200, resp.text
    reports = resp.json()["actions"]
    assert reports[0]["matches"] == [
        "/paths/~1pets/get", "/paths/~1toys/get"
    ]
    assert reports[1]["matches"] == ["/paths/~1pets/post"]


# --------------------------------------------------------------------------- #
# Request object immutability
# --------------------------------------------------------------------------- #

def test_request_document_and_actions_are_not_mutated():
    doc = make_doc(paths={"/pets": {"get": {}}}, servers=[{"url": "a"}])
    snapshot = copy.deepcopy(doc)
    action_value = {"merged": {"n": 1}}
    actions = [
        {"target": "$.info", "update": action_value},
        {"target": "$.servers[*]", "remove": True},
    ]
    actions_snapshot = copy.deepcopy(actions)
    resp = apply(doc, actions)
    assert resp.status_code == 200, resp.text
    assert doc == snapshot
    assert actions == actions_snapshot
    assert action_value == {"merged": {"n": 1}}


# --------------------------------------------------------------------------- #
# 422: malformed actions and target syntax
# --------------------------------------------------------------------------- #

def test_action_that_is_not_an_object_is_422_with_index():
    resp = apply(make_doc(), ["not-an-object"])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "invalid_action"
    assert body["actions"] == [
        {"action_index": 0, "error": "action must be a JSON object, got str"}
    ]


def test_missing_target_is_422_with_index():
    resp = apply(make_doc(), [{"update": 1}])
    assert resp.status_code == 422
    assert body_entry(resp, 0)["error"].startswith("action.target must be")


def test_both_update_and_remove_is_422():
    resp = apply(make_doc(), [{"target": "$.info", "update": 1, "remove": True}])
    assert resp.status_code == 422
    assert body_entry(resp, 0)["error"].startswith("action must contain exactly one")


def test_neither_update_nor_remove_is_422():
    resp = apply(make_doc(), [{"target": "$.info"}])
    assert resp.status_code == 422
    assert body_entry(resp, 0)["error"].startswith("action must contain exactly one")


def test_unknown_action_field_is_422():
    resp = apply(make_doc(), [{"target": "$.info", "update": 1, "bogus": 2}])
    assert resp.status_code == 422
    assert "unknown action field(s): bogus" in body_entry(resp, 0)["error"]


def test_remove_false_is_422():
    resp = apply(make_doc(), [{"target": "$.info", "remove": False}])
    assert resp.status_code == 422
    assert body_entry(resp, 0)["error"] == "action.remove must be true"


def test_remove_non_boolean_is_422():
    resp = apply(make_doc(), [{"target": "$.info", "remove": 1}])
    assert resp.status_code == 422
    assert body_entry(resp, 0)["error"] == "action.remove must be true"


def test_remove_root_is_422():
    resp = apply(make_doc(), [{"target": "$", "remove": True}])
    assert resp.status_code == 422
    assert body_entry(resp, 0)["error"] == "the root node '$' cannot be removed"


def test_target_not_starting_with_dollar_is_422():
    resp = apply(make_doc(), [{"target": "info", "remove": True}])
    assert resp.status_code == 422
    assert body_entry(resp, 0)["target"] == "info"
    assert "must start with '$'" in body_entry(resp, 0)["error"]


def test_recursive_descent_is_422():
    resp = apply(make_doc(), [{"target": "$..info", "remove": True}])
    assert resp.status_code == 422
    assert "position 2" in body_entry(resp, 0)["error"]


def test_filter_expression_is_422():
    resp = apply(make_doc(), [{"target": "$[?(@.x==1)]", "remove": True}])
    assert resp.status_code == 422
    assert body_entry(resp, 0)["target"] == "$[?(@.x==1)]"


def test_negative_array_index_is_422():
    resp = apply(make_doc(), [{"target": "$.servers[-1]", "remove": True}])
    assert resp.status_code == 422


def test_leading_zero_array_index_is_422():
    resp = apply(make_doc(), [{"target": "$.servers[01]", "remove": True}])
    assert resp.status_code == 422
    assert "leading zeros" in body_entry(resp, 0)["error"]


def test_trailing_dot_is_422():
    resp = apply(make_doc(), [{"target": "$.info.", "remove": True}])
    assert resp.status_code == 422


def test_unterminated_bracket_and_quote_are_422():
    for target in ("$.servers[0", "$['info", "$['unclosed\\", "$[]"):
        resp = apply(make_doc(), [{"target": target, "remove": True}])
        assert resp.status_code == 422, target
        assert body_entry(resp, 0)["target"] == target


def test_dotted_special_characters_are_422():
    for target in ("$.a/b", "$.x-custom", "$.$", "$ .info"):
        resp = apply(make_doc(), [{"target": target, "remove": True}])
        assert resp.status_code == 422, target


def test_empty_target_is_422():
    resp = apply(make_doc(), [{"target": "", "remove": True}])
    assert resp.status_code == 422


def test_non_string_target_is_422():
    resp = apply(make_doc(), [{"target": 3, "remove": True}])
    assert resp.status_code == 422


def test_all_invalid_actions_are_reported_together_with_indices():
    resp = apply(make_doc(), [
        {"target": "$.ok", "update": 1},                       # 0 valid
        {"target": "$.bad[", "remove": True},                  # 1 syntax
        {"target": "$.info", "update": 1, "remove": True},     # 2 shape
        "scalar",                                              # 3 shape
        {"target": "$", "remove": True},                       # 4 remove root
    ])
    assert resp.status_code == 422
    indices = [a["action_index"] for a in resp.json()["actions"]]
    assert indices == [1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# 422: zero matches and atomicity
# --------------------------------------------------------------------------- #

def test_zero_match_is_422_with_action_index():
    resp = apply(make_doc(), [{"target": "$.missing", "remove": True}])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "no_match"
    assert body["actions"] == [
        {"action_index": 0, "target": "$.missing",
         "error": "target '$.missing' matched 0 nodes"}
    ]


def test_index_out_of_range_is_422_not_crash():
    doc = make_doc(paths={}, servers=[{"url": "a"}])
    resp = apply(doc, [{"target": "$.servers[5]", "remove": True}])
    assert resp.status_code == 422
    assert resp.json()["actions"][0]["action_index"] == 0


def test_member_on_scalar_is_422():
    doc = make_doc(paths={}, count=3)
    resp = apply(doc, [{"target": "$.count.nope", "remove": True}])
    assert resp.status_code == 422


def test_wildcard_over_scalar_matches_nothing():
    doc = make_doc(paths={}, count=3)
    resp = apply(doc, [{"target": "$.count.*", "remove": True}])
    assert resp.status_code == 422


def test_batch_is_atomic_no_partial_result_or_side_effects():
    doc = make_doc(paths={"/pets": {"get": {}}}, servers=[{"url": "a"}])
    snapshot = copy.deepcopy(doc)
    resp = apply(doc, [
        {"target": "$.servers[*]", "remove": True},          # would succeed
        {"target": "$.paths['/missing']", "remove": True},   # zero match
    ])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "no_match"
    assert body["actions"][0]["action_index"] == 1
    assert "contract" not in body and "actions_report" not in body
    # Nothing was applied: the request object is untouched.
    assert doc == snapshot


def test_syntax_error_in_later_action_still_rejects_whole_batch():
    resp = apply(make_doc(), [
        {"target": "$.info", "update": {"title": "changed"}},
        {"target": "$.paths..x", "remove": True},
    ])
    assert resp.status_code == 422
    assert resp.json()["actions"][0]["action_index"] == 1


# --------------------------------------------------------------------------- #
# 400: document shape and action count
# --------------------------------------------------------------------------- #

def test_non_openapi_document_is_400():
    resp = client.post("/overlay/apply", json={
        "document": {"swagger": "2.0", "paths": {}},
        "actions": [{"target": "$", "update": {"x": 1}}],
    })
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_openapi_document"


def test_empty_actions_is_400():
    resp = apply(make_doc(), [])
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_overlay_request"
    assert "1..50" in resp.json()["messages"][0]


def test_too_many_actions_is_400():
    resp = apply(
        make_doc(),
        [{"target": "$.info", "update": {}} for _ in range(MAX_ACTIONS + 1)],
    )
    assert resp.status_code == 400
    assert "1..50" in resp.json()["messages"][0]


# --------------------------------------------------------------------------- #
# Parser / evaluator unit tests
# --------------------------------------------------------------------------- #

def test_parser_accepts_supported_forms():
    assert parse_target("$") == []
    assert parse_target("$.a")[0].kind == "member"
    assert parse_target("$['a/b']")[0].value == "a/b"
    assert parse_target('$["a"]')[0].value == "a"
    assert parse_target("$[12]")[0]._asdict() == {"kind": "index", "value": 12}
    assert parse_target("$.*")[0].kind == "wildcard"
    assert parse_target("$[*]")[0].kind == "wildcard"
    segments = parse_target("$.a[2]['b c'].*")
    assert [s.kind for s in segments] == ["member", "index", "member", "wildcard"]


def test_evaluator_wildcard_works_on_both_objects_and_arrays():
    doc = {"o": {"b": 1, "a": 2}, "l": [9, 8]}
    assert evaluate_target(doc, parse_target("$.o.*")) == ["/o/a", "/o/b"]
    assert evaluate_target(doc, parse_target("$.l[*]")) == ["/l/0", "/l/1"]
    assert evaluate_target(doc, parse_target("$.o[*]")) == ["/o/a", "/o/b"]
    assert evaluate_target(doc, parse_target("$.l.*")) == ["/l/0", "/l/1"]
    assert evaluate_target(doc, parse_target("$.missing.*")) == []


# --------------------------------------------------------------------------- #
# End-to-end OpenAPI-style scenario
# --------------------------------------------------------------------------- #

def test_openapi_flavored_end_to_end_overlay():
    doc = make_doc(
        servers=[{"url": "https://old.example.com"}],
        paths={
            "/pets": {
                "get": {"operationId": "listPets",
                        "responses": {"200": {"description": "ok"}}},
                "post": {"operationId": "addPet",
                         "responses": {"201": {"description": "created"}}},
            },
            "/internal": {"delete": {"responses": {"204": {"description": "x"}}}},
        },
    )
    resp = apply(doc, [
        {"target": "$.servers[0]", "update": {"url": "https://api.example.com"}},
        {"target": "$.paths['/internal']", "remove": True},
        {"target": "$.paths.*.*", "update": {"deprecated": True}},
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert_digest_ok(body)
    contract = body["contract"]
    assert contract["servers"][0]["url"] == "https://api.example.com"
    assert "/internal" not in contract["paths"]
    for operation in contract["paths"]["/pets"].values():
        assert operation["deprecated"] is True
    assert body["actions"][2]["matches"] == [
        "/paths/~1pets/get", "/paths/~1pets/post"
    ]
    # The digest is stable for the same inputs.
    again = apply(copy.deepcopy(doc), copy.deepcopy([
        {"target": "$.servers[0]", "update": {"url": "https://api.example.com"}},
        {"target": "$.paths['/internal']", "remove": True},
        {"target": "$.paths.*.*", "update": {"deprecated": True}},
    ]))
    assert again.json()["sha256"] == body["sha256"]


def test_root_is_listed_in_service_index():
    root = client.get("/").json()
    assert root["overlay_apply"] == "POST /overlay/apply"
