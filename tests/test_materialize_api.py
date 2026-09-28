"""Regression tests for ``POST /requests/materialize``.

The serializers are checked against the example tables of the OpenAPI
3.0.3 specification; the API tests additionally cover $ref resolution,
path/operation parameter merging, error reporting with source JSON
Pointers and stability guarantees.
"""

from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def make_doc(paths=None, *, openapi="3.0.3", **extra) -> dict:
    doc = {
        "openapi": openapi,
        "info": {"title": "Pet API", "version": "1.0"},
        "paths": paths or {},
    }
    doc.update(extra)
    return doc


def param(name, location, **extra) -> dict:
    node = {"name": name, "in": location}
    node.update(extra)
    if "schema" not in node and "content" not in node:
        node["schema"] = {"type": "string"}
    return node


def materialize(doc, selector, values=None):
    payload = {"document": doc, "selector": selector}
    if values is not None:
        payload["values"] = values
    return client.post("/requests/materialize", json=payload)


# --------------------------------------------------------------------------- #
# Basic happy path
# --------------------------------------------------------------------------- #

def test_basic_path_query_header_cookie_materialization():
    doc = make_doc(
        paths={
            "/pets/{petId}": {
                "get": {
                    "parameters": [
                        param("petId", "path", required=True),
                        param("q", "query"),
                        param("X-Trace", "header"),
                        param("session", "cookie"),
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )
    resp = materialize(
        doc,
        "GET /pets/{petId}",
        {
            "path": {"petId": "abc"},
            "query": {"q": "x"},
            "header": {"X-Trace": "t"},
            "cookie": {"session": "s"},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_target"] == "/pets/abc?q=x"
    assert body["headers"] == {"X-Trace": "t"}
    assert body["cookie"] == "session=s"


def test_no_values_and_no_query_returns_plain_path():
    doc = make_doc(
        paths={
            "/pets": {
                "get": {
                    "parameters": [param("q", "query", required=False)],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )
    resp = materialize(doc, "get /pets")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_target"] == "/pets"
    assert body["headers"] == {}
    assert body["cookie"] == ""


def test_selector_method_is_case_insensitive():
    doc = make_doc(
        paths={"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}}
    )
    assert materialize(doc, "GeT /pets").status_code == 200


# --------------------------------------------------------------------------- #
# Path styles
# --------------------------------------------------------------------------- #

def test_path_simple_is_default_for_arrays_and_objects():
    doc = make_doc(
        paths={
            "/x/{id}": {
                "get": {
                    "parameters": [
                        param("id", "path", required=True,
                              style="simple", explode=False,
                              schema={"type": "array"})
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )
    resp = materialize(doc, "GET /x/{id}", {"path": {"id": ["a", "b"]}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["request_target"] == "/x/a,b"


def test_path_simple_object_exploded_and_non_exploded():
    exploded = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True, style="simple", explode=True,
                      schema={"type": "object"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        exploded, "GET /x/{id}",
        {"path": {"id": {"role": "admin", "firstName": "Alex"}}},
    )
    # Object keys sorted for stable output.
    assert resp.json()["request_target"] == "/x/firstName=Alex,role=admin"

    flat = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True, style="simple", explode=False,
                      schema={"type": "object"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        flat, "GET /x/{id}",
        {"path": {"id": {"role": "admin", "firstName": "Alex"}}},
    )
    assert resp.json()["request_target"] == "/x/firstName,Alex,role,admin"


def test_path_label_styles():
    cases = [
        (True, "id", "/.id"),
        (True, ["a", "b", "c"], "/.a.b.c"),
        (False, ["a", "b", "c"], "/.a,b,c"),
    ]
    for explode, value, suffix in cases:
        doc = make_doc(
            paths={
                "/x/{id}": {"get": {"parameters": [
                    param("id", "path", required=True, style="label",
                          explode=explode)
                ], "responses": {"200": {"description": "ok"}}}}
            }
        )
        resp = materialize(doc, "GET /x/{id}", {"path": {"id": value}})
        assert resp.json()["request_target"] == "/x" + suffix, suffix


def test_path_matrix_styles():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True, style="matrix")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    # matrix default explode is false
    resp = materialize(doc, "GET /x/{id}", {"path": {"id": "5"}})
    assert resp.json()["request_target"] == "/x/;id=5"

    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True, style="matrix", explode=True,
                      schema={"type": "array"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x/{id}", {"path": {"id": ["a", "b"]}})
    assert resp.json()["request_target"] == "/x/;id=a;id=b"

    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True, style="matrix", explode=False,
                      schema={"type": "array"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x/{id}", {"path": {"id": ["a", "b"]}})
    assert resp.json()["request_target"] == "/x/;id=a,b"


def test_path_default_style_and_explode_are_simple_false():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True, schema={"type": "array"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x/{id}", {"path": {"id": ["a", "b"]}})
    assert resp.json()["request_target"] == "/x/a,b"


def test_path_values_are_percent_encoded():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True)
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x/{id}", {"path": {"id": "a/b c?d"}})
    assert resp.json()["request_target"] == "/x/a%2Fb%20c%3Fd"


def test_path_style_must_be_one_of_simple_label_matrix():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True, style="form")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x/{id}", {"path": {"id": "a"}})
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unsupported_style"
    assert error["pointer"].endswith("/style")
    assert error["in"] == "path"


# --------------------------------------------------------------------------- #
# Query styles
# --------------------------------------------------------------------------- #

def test_query_form_defaults_to_exploded_repeated_keys():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("id", "query", schema={"type": "array"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"id": ["a", "b"]}})
    assert resp.json()["request_target"] == "/x?id=a&id=b"


def test_query_form_non_exploded_is_comma_joined():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("id", "query", explode=False, schema={"type": "array"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"id": ["a", "b", "c"]}})
    assert resp.json()["request_target"] == "/x?id=a,b,c"


def test_query_form_object_exploded_uses_property_keys():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("id", "query", schema={"type": "object"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x",
        {"query": {"id": {"b": "2", "a": "1"}}},
    )
    # exploded form: property keys become query keys; sorted for stability
    assert resp.json()["request_target"] == "/x?a=1&b=2"


def test_query_form_object_non_exploded_uses_param_name():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("id", "query", explode=False, schema={"type": "object"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x",
        {"query": {"id": {"b": "2", "a": "1"}}},
    )
    assert resp.json()["request_target"] == "/x?id=a,1,b,2"


def test_query_space_delimited_and_pipe_delimited():
    for style, delim in [("spaceDelimited", "%20"), ("pipeDelimited", "|")]:
        doc = make_doc(
            paths={
                "/x": {"get": {"parameters": [
                    param("id", "query", style=style, explode=False,
                          schema={"type": "array"})
                ], "responses": {"200": {"description": "ok"}}}}
            }
        )
        resp = materialize(doc, "GET /x", {"query": {"id": ["a", "b"]}})
        assert resp.json()["request_target"] == f"/x?id=a{delim}b"
        # exploded variant repeats the key
        doc2 = make_doc(
            paths={
                "/x": {"get": {"parameters": [
                    param("id", "query", style=style, explode=True,
                          schema={"type": "array"})
                ], "responses": {"200": {"description": "ok"}}}}
            }
        )
        resp2 = materialize(doc2, "GET /x", {"query": {"id": ["a", "b"]}})
        assert resp2.json()["request_target"] == "/x?id=a&id=b"


def test_query_deep_object_uses_bracket_keys():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("id", "query", style="deepObject",
                      schema={"type": "object"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x",
        {"query": {"id": {"role": "admin", "name": "Al x"}}},
    )
    assert (
        resp.json()["request_target"]
        == "/x?id[name]=Al%20x&id[role]=admin"
    )


def test_query_space_and_pipe_styles_require_arrays():
    for style in ("spaceDelimited", "pipeDelimited"):
        doc = make_doc(
            paths={
                "/x": {"get": {"parameters": [
                    param("id", "query", style=style, explode=False)
                ], "responses": {"200": {"description": "ok"}}}}
            }
        )
        resp = materialize(doc, "GET /x", {"query": {"id": {"a": 1}}})
        assert resp.status_code == 422
        assert resp.json()["errors"][0]["code"] == "invalid_value_shape"


def test_query_deep_object_requires_object():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("id", "query", style="deepObject")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"id": ["a"]}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "invalid_value_shape"


def test_allow_reserved_keeps_reserved_characters_raw():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("q", "query", allowReserved=True)
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"q": "a:b/c?d&e"}})
    assert resp.json()["request_target"] == "/x?q=a:b/c?d&e"

    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("q", "query")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"q": "a:b/c?d&e"}})
    assert resp.json()["request_target"] == "/x?q=a%3Ab%2Fc%3Fd%26e"


def test_query_reserved_structure_delimiters_are_not_encoded():
    # the '&' between repeated pairs and the '='/','/ '|' delimiters stay raw
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("id", "query", explode=False, schema={"type": "array"}),
                param("p", "query", style="pipeDelimited", explode=False,
                      schema={"type": "array"}),
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x",
        {"query": {"id": ["a", "b"], "p": ["1", "2"]}},
    )
    assert resp.json()["request_target"] == "/x?id=a,b&p=1|2"


# --------------------------------------------------------------------------- #
# Header / cookie
# --------------------------------------------------------------------------- #

def test_header_simple_arrays_and_objects():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("X-Colors", "header", explode=False,
                      schema={"type": "array"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"header": {"X-Colors": ["a", "b"]}})
    assert resp.json()["headers"] == {"X-Colors": "a,b"}

    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("X-Meta", "header", explode=True,
                      schema={"type": "object"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x", {"header": {"X-Meta": {"b": "2", "a": "1"}}}
    )
    assert resp.json()["headers"] == {"X-Meta": "a=1,b=2"}


def test_header_value_matching_is_case_insensitive():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("X-Trace-Id", "header")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"header": {"x-trace-id": "t"}})
    assert resp.status_code == 200, resp.text
    # emitted under the declared name
    assert resp.json()["headers"] == {"X-Trace-Id": "t"}


def test_header_only_supports_simple_style():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("X", "header", style="label")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"header": {"X": "1"}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "unsupported_style"


def test_header_cr_lf_is_rejected():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("X", "header")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"header": {"X": "a\nb"}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "invalid_value_shape"


def test_cookie_form_serialization_and_header_assembly():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("a", "cookie"),
                param("b", "cookie", schema={"type": "array"}),
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x", {"cookie": {"a": "1", "b": ["x", "y"]}}
    )
    assert resp.status_code == 200, resp.text
    # cookie form: explode ignored, arrays comma-joined, pairs joined by '; '
    assert resp.json()["cookie"] == "a=1; b=x,y"
    assert "Cookie" not in resp.json()["headers"]
    assert "cookie" not in resp.json()["headers"]


def test_cookie_only_supports_form_style():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("c", "cookie", style="simple")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"cookie": {"c": "1"}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "unsupported_style"


# --------------------------------------------------------------------------- #
# Parameter merging: path-level vs operation-level
# --------------------------------------------------------------------------- #

def test_operation_parameter_overrides_path_parameter_by_in_and_name():
    doc = make_doc(
        paths={
            "/x/{id}": {
                "parameters": [
                    param("id", "path", required=True, style="label"),
                    param("shared", "query",
                          description="path-level default"),
                ],
                "get": {
                    "parameters": [
                        param("shared", "query", style="pipeDelimited",
                              explode=False, schema={"type": "array"}),
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
            }
        }
    )
    resp = materialize(
        doc, "GET /x/{id}",
        {"path": {"id": "p"}, "query": {"shared": ["a", "b"]}},
    )
    assert resp.status_code == 200, resp.text
    # label prefix survives (path-level param); shared uses the
    # operation-level pipeDelimited style.
    assert resp.json()["request_target"] == "/x/.p?shared=a|b"


def test_path_level_parameters_apply_to_the_operation():
    doc = make_doc(
        paths={
            "/x": {
                "parameters": [param("tenant", "header")],
                "get": {"responses": {"200": {"description": "ok"}}},
            }
        }
    )
    resp = materialize(doc, "GET /x", {"header": {"tenant": "t"}})
    assert resp.json()["headers"] == {"tenant": "t"}


def test_duplicate_parameter_at_same_level_is_422_with_both_pointers():
    doc = make_doc(
        paths={
            "/x": {
                "get": {
                    "parameters": [
                        param("q", "query"),
                        param("q", "query"),
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"q": "1"}})
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "duplicate_parameter"
    assert error["pointer"] == "/paths/~1x/get/parameters/1"
    assert error["first_pointer"] == "/paths/~1x/get/parameters/0"


def test_duplicate_path_level_parameter_is_422():
    doc = make_doc(
        paths={
            "/x": {
                "parameters": [param("q", "query"), param("q", "query")],
                "get": {"responses": {"200": {"description": "ok"}},
            }}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"q": "1"}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "duplicate_parameter"
    assert resp.json()["errors"][0]["pointer"].startswith(
        "/paths/~1x/parameters/"
    )


def test_header_parameters_differing_only_in_case_are_duplicates():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("X-T", "header"),
                param("x-t", "header"),
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"header": {"X-T": "1"}})
    assert resp.status_code == 422
    codes = {e["code"] for e in resp.json()["errors"]}
    assert "duplicate_parameter" in codes
    duplicate = next(
        e for e in resp.json()["errors"]
        if e["code"] == "duplicate_parameter"
    )
    assert duplicate["pointer"] == "/paths/~1x/get/parameters/1"
    assert duplicate["first_pointer"] == "/paths/~1x/get/parameters/0"


def test_invalid_in_value_is_422_with_pointer():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                {"name": "q", "in": "body", "schema": {}}
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {})
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "invalid_parameter"
    assert error["pointer"] == "/paths/~1x/get/parameters/0"


def test_parameter_missing_name_is_422():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                {"in": "query", "schema": {}}
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "invalid_parameter"


# --------------------------------------------------------------------------- #
# $ref resolution
# --------------------------------------------------------------------------- #

def test_parameters_resolve_through_component_refs():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {
                "parameters": [
                    {"$ref": "#/components/parameters/Id"},
                    {"$ref": "#/components/parameters/Q"},
                ],
                "responses": {"200": {"description": "ok"}},
            }}
        },
        components={"parameters": {
            "Id": param("id", "path", required=True, style="label"),
            "Q": param("q", "query"),
        }},
    )
    resp = materialize(
        doc, "GET /x/{id}",
        {"path": {"id": "9"}, "query": {"q": "z"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["request_target"] == "/x/.9?q=z"


def test_dangling_parameter_ref_is_422_with_source_pointer():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                {"$ref": "#/components/parameters/Missing"}
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert error["pointer"] == "/paths/~1x/get/parameters/0"
    assert error["ref"] == "#/components/parameters/Missing"


def test_circular_parameter_ref_chain_is_422():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                {"$ref": "#/components/parameters/A"}
            ], "responses": {"200": {"description": "ok"}}}}
        },
        components={"parameters": {
            "A": {"$ref": "#/components/parameters/B"},
            "B": {"$ref": "#/components/parameters/A"},
        }},
    )
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "unresolvable_reference"


def test_external_parameter_ref_is_422():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                {"$ref": "https://example.com/params.json#/Q"}
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "unresolvable_reference"


def test_path_level_ref_is_resolved_cycle_safely():
    doc = make_doc(
        openapi="3.1.0",
        paths={
            "/x": {"$ref": "#/components/pathItems/X"}
        },
        components={"pathItems": {
            "X": {
                "parameters": [param("q", "query")],
                "get": {"responses": {"200": {"description": "ok"}}},
            }
        }},
    )
    resp = materialize(doc, "GET /x", {"query": {"q": "1"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["request_target"] == "/x?q=1"


def test_circular_path_item_ref_chain_is_422():
    doc = make_doc(
        openapi="3.1.0",
        paths={"/x": {"$ref": "#/components/pathItems/A"}},
        components={"pathItems": {
            "A": {"$ref": "#/components/pathItems/B"},
            "B": {"$ref": "#/components/pathItems/A"},
        }},
    )
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "unresolvable_reference"


def test_31_parameter_ref_siblings_override_target_fields():
    doc = make_doc(
        openapi="3.1.0",
        paths={
            "/x": {"get": {"parameters": [
                {"$ref": "#/components/parameters/Q", "style": "pipeDelimited",
                 "explode": False, "schema": {"type": "array"}}
            ], "responses": {"200": {"description": "ok"}}}}
        },
        components={"parameters": {
            "Q": param("q", "query"),
        }},
    )
    resp = materialize(doc, "GET /x", {"query": {"q": ["a", "b"]}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["request_target"] == "/x?q=a|b"


def test_30_parameter_ref_ignores_siblings():
    doc = make_doc(
        openapi="3.0.3",
        paths={
            "/x": {"get": {"parameters": [
                {"$ref": "#/components/parameters/Q", "style": "pipeDelimited",
                 "explode": False, "schema": {"type": "array"}}
            ], "responses": {"200": {"description": "ok"}}}}
        },
        components={"parameters": {
            "Q": param("q", "query"),
        }},
    )
    resp = materialize(doc, "GET /x", {"query": {"q": ["a", "b"]}})
    assert resp.status_code == 200, resp.text
    # siblings ignored -> plain form explode=true repeated keys
    assert resp.json()["request_target"] == "/x?q=a&q=b"


# --------------------------------------------------------------------------- #
# Required / optional / unknown values
# --------------------------------------------------------------------------- #

def test_missing_required_path_value_is_422_with_pointers():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True)
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x/{id}")
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "missing_required_value"
    # the document definition is the source pointer
    assert error["pointer"] == "/paths/~1x~1{id}/get/parameters/0"
    assert error["in"] == "path"
    assert error["name"] == "id"


def test_path_parameters_are_always_required_even_without_flag():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x/{id}")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "missing_required_value"


def test_missing_required_query_header_cookie_values_are_422():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("q", "query", required=True),
                param("h", "header", required=True),
                param("c", "cookie", required=True),
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    codes = sorted(
        (e["code"], e["in"], e["name"]) for e in resp.json()["errors"]
    )
    assert codes == [
        ("missing_required_value", "cookie", "c"),
        ("missing_required_value", "header", "h"),
        ("missing_required_value", "query", "q"),
    ]


def test_null_value_counts_as_absent():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("q", "query", required=True),
                param("o", "query"),
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    # null for required -> error
    resp = materialize(doc, "GET /x", {"query": {"q": None}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "missing_required_value"
    # null for optional -> omitted from the request
    resp = materialize(
        doc, "GET /x", {"query": {"q": "1", "o": None}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["request_target"] == "/x?q=1"


def test_optional_parameter_without_value_is_omitted():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("q", "query")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {}})
    assert resp.json()["request_target"] == "/x"


def test_unknown_value_is_422_with_value_pointer():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("q", "query")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x", {"query": {"q": "1", "bogus": "2"}}
    )
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unknown_value"
    assert error["pointer"] == "/values/query/bogus"
    assert error["name"] == "bogus"


def test_unknown_value_in_each_group_is_422():
    doc = make_doc(
        paths={"/x": {"get": {"responses": {"200": {"description": "ok"}}}}}
    )
    for group in ("path", "query", "header", "cookie"):
        resp = materialize(doc, "GET /x", {group: {"nope": "v"}})
        assert resp.status_code == 422
        assert resp.json()["errors"][0]["code"] == "unknown_value"


def test_case_insensitive_header_unknown_value():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("X-Known", "header")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"header": {"x-known": "1", "X-Other": "2"}})
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unknown_value"
    assert error["name"] == "X-Other"


def test_ambiguous_header_value_keys_differing_only_in_case():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("X-K", "header")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x", {"header": {"x-k": "1", "X-K": "2"}}
    )
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "ambiguous_value"


# --------------------------------------------------------------------------- #
# Path template consistency
# --------------------------------------------------------------------------- #

def test_template_variable_without_parameter_declaration_is_422():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x/{id}", {"path": {"id": "9"}})
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "invalid_path_template"
    assert error["name"] == "id"


def test_path_parameter_not_used_by_template_is_422():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("id", "path", required=True)
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"path": {"id": "9"}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "invalid_path_template"


# --------------------------------------------------------------------------- #
# Encoding & primitive rendering
# --------------------------------------------------------------------------- #

def test_boolean_integer_and_float_values_render_as_text():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True,
                      schema={"type": "boolean"}),
                param("n", "query", schema={"type": "number"}),
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x/{id}",
        {"path": {"id": True}, "query": {"n": 1.5}},
    )
    assert resp.json()["request_target"] == "/x/true?n=1.5"


def test_unicode_values_are_utf8_percent_encoded():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("q", "query")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"q": "€"}})
    assert resp.json()["request_target"] == "/x?q=%E2%82%AC"


def test_nested_object_values_are_rejected():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("q", "query", style="deepObject")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"q": {"a": {"b": 1}}}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "invalid_value_shape"


# --------------------------------------------------------------------------- #
# Request-level validation (400)
# --------------------------------------------------------------------------- #

def test_document_without_openapi_or_paths_is_400():
    resp = client.post(
        "/requests/materialize",
        json={"document": {}, "selector": "GET /x", "values": {}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_openapi_document"


def test_malformed_selector_is_400():
    doc = make_doc(paths={"/x": {"get": {"responses": {"200": {"description": "ok"}}}}})
    for bad in ["garbage", "GET", "/x", "GETT /x"]:
        resp = materialize(doc, bad)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"] == "invalid_selector"


def test_unknown_path_or_method_selector_is_400():
    doc = make_doc(paths={"/x": {"get": {"responses": {"200": {"description": "ok"}}}}})
    assert materialize(doc, "GET /missing").status_code == 400
    assert materialize(doc, "POST /x").status_code == 400


def test_unknown_values_group_is_400():
    doc = make_doc(paths={"/x": {"get": {"responses": {"200": {"description": "ok"}}}}})
    resp = materialize(doc, "GET /x", {"body": {}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_materialize_request"


def test_non_object_group_is_400():
    doc = make_doc(paths={"/x": {"get": {"responses": {"200": {"description": "ok"}}}}})
    resp = materialize(doc, "GET /x", {"query": ["a"]})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_materialize_request"


def test_missing_values_key_is_treated_as_empty():
    doc = make_doc(
        paths={"/x": {"get": {"responses": {"200": {"description": "ok"}}}}}
    )
    payload = {"document": doc, "selector": "GET /x"}
    resp = client.post("/requests/materialize", json=payload)
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# Stability & immutability
# --------------------------------------------------------------------------- #

def test_output_is_stable_across_input_object_key_order():
    def build():
        return make_doc(
            paths={
                "/x": {"get": {"parameters": [
                    param("f", "query", style="deepObject",
                          schema={"type": "object"}),
                ], "responses": {"200": {"description": "ok"}}}}
            }
        )

    value_a = {"z": "1", "a": "2", "m": "3"}
    value_b = {"m": "3", "z": "1", "a": "2"}
    resp_a = materialize(build(), "GET /x", {"query": {"f": value_a}})
    resp_b = materialize(build(), "GET /x", {"query": {"f": value_b}})
    assert resp_a.json()["request_target"] == resp_b.json()["request_target"]


def test_request_document_and_values_are_not_mutated():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                {"$ref": "#/components/parameters/Id"},
                param("q", "query"),
            ], "responses": {"200": {"description": "ok"}}}}
        },
        components={"parameters": {"Id": param("id", "path", required=True)}},
    )
    values = {"path": {"id": "a"}, "query": {"q": ["x", "y"]}}
    doc_snapshot = copy.deepcopy(doc)
    values_snapshot = copy.deepcopy(values)
    resp = materialize(doc, "GET /x/{id}", values)
    assert resp.status_code == 200, resp.text
    assert doc == doc_snapshot
    assert values == values_snapshot


def test_multiple_errors_are_all_reported_and_sorted():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True),
                param("q", "query", required=True, style="spaceDelimited",
                      explode=False, schema={"type": "array"}),
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x/{id}",
        {"query": {"q": "not-an-array", "extra": "1"}},
    )
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    codes = [e["code"] for e in errors]
    # missing path value, invalid array shape, unknown query value
    assert "missing_required_value" in codes
    assert "invalid_value_shape" in codes
    assert "unknown_value" in codes
    # stable order
    assert errors == sorted(
        errors,
        key=lambda e: (e["code"], e.get("in") or "", e.get("name") or "",
                       e.get("pointer") or "", e.get("error") or ""),
    )


def test_content_parameter_is_unsupported_422():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                {"name": "q", "in": "query",
                 "content": {"application/json": {}}}
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"q": {"a": 1}}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "unsupported_parameter"


def test_ref_to_parameter_with_ref_pointer_is_the_component_location():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                {"$ref": "#/components/parameters/Q"}
            ], "responses": {"200": {"description": "ok"}}}}
        },
        components={"parameters": {
            "Q": param("q", "query", required=True),
        }},
    )
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    # the missing-value error points at the component definition; the
    # dangling-reference error would point at the $ref occurrence instead
    assert error["pointer"] == "/components/parameters/Q"


def test_dangling_ref_chain_pointer_points_at_occurrence_not_component():
    doc = make_doc(
        components={"parameters": {"A": {"$ref": "#/components/parameters/B"}}},
        paths={
            "/x": {"get": {"parameters": [
                {"$ref": "#/components/parameters/A"}
            ], "responses": {"200": {"description": "ok"}}}}
        },
    )
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert error["pointer"] == "/components/parameters/A"


def test_query_preserves_declared_parameter_order():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("z", "query"),
                param("a", "query"),
                param("m", "query"),
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x",
        {"query": {"z": "1", "a": "2", "m": "3"}},
    )
    assert resp.json()["request_target"] == "/x?z=1&a=2&m=3"


def test_matrix_object_exploded_uses_property_names_without_param_name():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True, style="matrix", explode=True,
                      schema={"type": "object"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x/{id}", {"path": {"id": {"b": "2", "a": "1"}}}
    )
    assert resp.json()["request_target"] == "/x/;a=1;b=2"


def test_simple_style_is_not_valid_for_query():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("q", "query", style="simple")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"q": "1"}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "unsupported_style"


def test_form_style_not_valid_for_path():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True, style="form")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x/{id}", {"path": {"id": "1"}})
    assert resp.status_code == 422


def test_empty_array_and_empty_object_render_emptied_fragments():
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {
                "parameters": [
                    param("id", "path", required=True, style="simple",
                          schema={"type": "array"}),
                    param("q", "query"),
                ],
                "responses": {"200": {"description": "ok"}},
            }}
        }
    )
    resp = materialize(
        doc, "GET /x/{id}",
        {"path": {"id": []}, "query": {"q": []}},
    )
    assert resp.status_code == 200, resp.text
    # empty simple array -> empty substitution; exploded empty array
    # contributes no pairs (no dangling '?')
    assert resp.json()["request_target"] == "/x/"


def test_non_exploded_empty_query_array_keeps_empty_assignment():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("q", "query", explode=False, schema={"type": "array"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"q": []}})
    assert resp.json()["request_target"] == "/x?q="


def test_deep_object_encodes_brackets_and_keys():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("f", "query", style="deepObject")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(
        doc, "GET /x", {"query": {"f": {"k/y": "a b", "z": "1"}}}
    )
    target = resp.json()["request_target"]
    # brackets are structural and stay raw; '/' inside key is encoded
    assert target == "/x?f[k%2Fy]=a%20b&f[z]=1"


def test_cookie_percent_encodes_delimiters_in_values():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("sid", "cookie")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"cookie": {"sid": "a=b; c"}})
    assert resp.json()["cookie"] == "sid=a%3Db%3B%20c"


def test_31_path_item_sibling_get_overrides_referenced_operation():
    doc = make_doc(
        openapi="3.1.0",
        paths={
            "/x": {
                "$ref": "#/components/pathItems/X",
                "get": {"responses": {"200": {"description": "sibling"}}},
            }
        },
        components={"pathItems": {"X": {
            "get": {"parameters": [param("q", "query")],
                    "responses": {"200": {"description": "ref"}}},
        }}},
    )
    # the sibling operation carries no 'q' parameter -> unknown value
    resp = materialize(doc, "GET /x", {"query": {"q": "1"}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "unknown_value"


def test_operation_level_override_pointer_is_component_of_operation_ref():
    doc = make_doc(
        paths={
            "/x": {
                "parameters": [{"$ref": "#/components/parameters/Q"}],
                "get": {
                    "parameters": [
                        {"$ref": "#/components/parameters/Q2"},
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
            }
        },
        components={
            "parameters": {
                "Q": param("q", "query", required=True),
                "Q2": param("q", "query"),
            }
        },
    )
    # Q (path-level, required) is overridden by Q2 (operation-level,
    # optional); omitting the value is therefore fine.
    resp = materialize(doc, "GET /x", {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["request_target"] == "/x"


def test_boolean_false_is_a_present_value_not_an_omission():
    doc = make_doc(
        paths={
            "/x/{on}": {"get": {"parameters": [
                param("on", "path", required=True, schema={"type": "boolean"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x/{on}", {"path": {"on": False}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["request_target"] == "/x/false"


def test_non_string_style_and_non_boolean_explode_are_422():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                {"name": "q", "in": "query", "style": 5, "schema": {}}
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"q": "1"}})
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "invalid_parameter"
    assert error["pointer"].endswith("/style")

    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                {"name": "q", "in": "query", "explode": "yes", "schema": {}}
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x", {"query": {"q": "1"}})
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "invalid_parameter"
    assert error["pointer"].endswith("/explode")


def test_reference_target_that_is_not_an_object_is_422_reference_error():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                {"$ref": "#/x-scalar"}
            ], "responses": {"200": {"description": "ok"}}}}
        },
        **{"x-scalar": "not-an-object"},
    )
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert error["pointer"] == "/paths/~1x/get/parameters/0"

    # a dict target that is not a parameter is reported as invalid_parameter
    doc2 = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                {"$ref": "#/components/schemas/X"}
            ], "responses": {"200": {"description": "ok"}}}}
        },
        components={"schemas": {"X": {"type": "string"}}},
    )
    resp2 = materialize(doc2, "GET /x")
    assert resp2.status_code == 422
    assert resp2.json()["errors"][0]["code"] == "invalid_parameter"


def test_malformed_path_item_value_is_422():
    doc = make_doc(paths={"/x": "not-an-object"})
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["pointer"] == "/paths/~1x"


def test_non_object_parameters_array_member_is_422():
    doc = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                "just-a-string", 42,
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    pointers = {e["pointer"] for e in resp.json()["errors"]}
    assert "/paths/~1x/get/parameters/0" in pointers
    assert "/paths/~1x/get/parameters/1" in pointers


def test_parameters_member_that_is_not_an_array_is_422():
    doc = make_doc(
        paths={
            "/x": {"get": {
                "parameters": {"name": "q", "in": "query"},
                "responses": {"200": {"description": "ok"}},
            }}
        }
    )
    resp = materialize(doc, "GET /x")
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["pointer"] == "/paths/~1x/get/parameters"


def test_unknown_group_values_400_and_selector_400_do_not_reveal_500s():
    doc = make_doc(paths={"/x": {"get": {"responses": {"200": {"description": "ok"}}}}})
    resp = materialize(doc, "GET /x", {"query": {}, "path": {}, "weird": {}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_materialize_request"
    assert "weird" in resp.json()["messages"][0]


def test_all_four_optional_groups_serialize_together():
    doc = make_doc(
        paths={
            "/x/{id}": {
                "parameters": [
                    param("id", "path", required=True),
                    param("h", "header"),
                    param("c", "cookie"),
                ],
                "get": {
                    "parameters": [param("q", "query")],
                    "responses": {"200": {"description": "ok"}},
                },
            }
        }
    )
    resp = materialize(
        doc,
        "GET /x/{id}",
        {
            "path": {"id": "7"},
            "query": {"q": "z"},
            "header": {"h": "v"},
            "cookie": {"c": "1"},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_target"] == "/x/7?q=z"
    assert body["headers"] == {"h": "v"}
    assert body["cookie"] == "c=1"




def test_structure_or_null_given_for_primitive_style_is_422():
    # A primitive style rejects arrays containing null or nested values.
    doc = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True, style="simple",
                      schema={"type": "array"})
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp = materialize(doc, "GET /x/{id}", {"path": {"id": ["a", None]}})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "invalid_value_shape"

    doc2 = make_doc(
        paths={
            "/x/{id}": {"get": {"parameters": [
                param("id", "path", required=True, style="simple")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp2 = materialize(doc2, "GET /x/{id}", {"path": {"id": None}})
    assert resp2.status_code == 422
    assert resp2.json()["errors"][0]["code"] == "missing_required_value"

    doc3 = make_doc(
        paths={
            "/x": {"get": {"parameters": [
                param("q", "query")
            ], "responses": {"200": {"description": "ok"}}}}
        }
    )
    resp3 = materialize(doc3, "GET /x", {"query": {"q": [["nested"]]}})
    assert resp3.status_code == 422
    assert resp3.json()["errors"][0]["code"] == "invalid_value_shape"
