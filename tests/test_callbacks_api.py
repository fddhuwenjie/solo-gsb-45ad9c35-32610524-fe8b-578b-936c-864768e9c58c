"""Regression tests for ``POST /callbacks/resolve``.

Covers Callback Object local ``$ref`` resolution (dangling / external /
circular, 3.0 verbatim vs 3.1 sibling merging), runtime-expression
evaluation ($url/$method/$statusCode, request/response
path/query/header/body, JSON Pointer body fragments, case-insensitive
headers, non-scalar rejection), literal-plus-expression key
interpolation, source JSON Pointer reporting, stable ordering and input
immutability.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def make_doc(callbacks=None, *, openapi="3.0.3", path="/pets", method="post",
             extra_paths=None, components=None, operation_extra=None):
    operation = {"responses": {"201": {"description": "created"}}}
    if operation_extra is not None:
        operation = operation_extra
    if callbacks is not None:
        operation["callbacks"] = callbacks
    paths = {path: {method.lower(): operation}}
    if extra_paths:
        paths.update(extra_paths)
    doc = {
        "openapi": openapi,
        "info": {"title": "Pet API", "version": "1.0"},
        "paths": paths,
    }
    if components is not None:
        doc["components"] = components
    return doc


def callback_path_item(**operations):
    item = {}
    item.update(operations)
    return item


def resolve(doc, selector="POST /pets", context=None):
    if context is None:
        context = {
            "request": {
                "url": "https://api.example.com/pets",
                "method": "POST",
                "path": {"petId": "p42"},
                "query": {"code": "ab"},
                "header": {"X-Token": "secret"},
                "body": {"id": 7, "owner": {"name": "sam"},
                         "tags": ["a", "b"], "slash": {"a/b": 1, "c~d": 2}},
            },
            "response": {
                "statusCode": 201,
                "header": {"Location": "/created/7"},
                "body": {"href": "https://cb.example.com/x", "ok": True},
            },
        }
    payload = {"document": doc, "selector": selector, "context": context}
    return client.post("/callbacks/resolve", json=payload)


def single_callback(key, path_item=None, *, name="onCreated", **extra):
    if path_item is None:
        path_item = {"post": {"responses": {"200": {"description": "ok"}}}}
    return make_doc({name: {key: path_item, **extra}})


# --------------------------------------------------------------------------- #
# Happy path: expression support matrix
# --------------------------------------------------------------------------- #

def test_literal_text_plus_embedded_expressions():
    doc = single_callback(
        "https://cb.example.com/hooks/{$request.path.petId}?s={$statusCode}"
    )
    resp = resolve(doc)
    assert resp.status_code == 200, resp.text
    entries = resp.json()["callbacks"]
    assert len(entries) == 1
    assert entries[0]["url"] == "https://cb.example.com/hooks/p42?s=201"


def test_bare_runtime_expression_key():
    doc = single_callback("$response.body#/href")
    resp = resolve(doc)
    assert resp.status_code == 200
    assert resp.json()["callbacks"][0]["url"] == "https://cb.example.com/x"


def test_pure_literal_key_has_no_expressions():
    doc = single_callback("https://cb.example.com/static-hook")
    resp = resolve(doc)
    assert resp.status_code == 200
    assert resp.json()["callbacks"][0]["url"] == "https://cb.example.com/static-hook"


def test_url_method_and_status_code_expressions():
    doc = make_doc({
        "a": {"$url": {"get": {"responses": {}}}},
        "b": {"{$method}": {"get": {"responses": {}}}},
        "c": {"{$statusCode}": {"get": {"responses": {}}}},
    })
    resp = resolve(doc)
    assert resp.status_code == 200, resp.text
    urls = {c["name"]: c["url"] for c in resp.json()["callbacks"]}
    assert urls == {
        "a": "https://api.example.com/pets",
        "b": "POST",
        "c": "201",
    }


def test_request_path_query_header_and_body_expressions():
    doc = make_doc({
        "cb": {
            "p/{$request.path.petId}": {"get": {"responses": {}}},
            "q/{$request.query.code}": {"get": {"responses": {}}},
            "h/{$request.header.x-token}": {"get": {"responses": {}}},
            "b/{$request.body#/owner/name}": {"get": {"responses": {}}},
            "rh/{$response.header.location}": {"get": {"responses": {}}},
            "rb/{$response.body#/ok}": {"get": {"responses": {}}},
        }
    })
    resp = resolve(doc)
    assert resp.status_code == 200, resp.text
    urls = {c["expression"]: c["url"] for c in resp.json()["callbacks"]}
    assert urls == {
        "p/{$request.path.petId}": "p/p42",
        "q/{$request.query.code}": "q/ab",
        "h/{$request.header.x-token}": "h/secret",
        "b/{$request.body#/owner/name}": "b/sam",
        "rh/{$response.header.location}": "rh//created/7",
        "rb/{$response.body#/ok}": "rb/true",
    }


def test_body_pointer_into_arrays_and_escaped_tokens():
    doc = make_doc({
        "cb": {
            "{$request.body#/tags/1}": {"get": {"responses": {}}},
            "{$request.body#/slash/a~1b}": {"get": {"responses": {}}},
            "{$request.body#/slash/c~0d}": {"get": {"responses": {}}},
        }
    })
    resp = resolve(doc)
    assert resp.status_code == 200, resp.text
    urls = {c["expression"]: c["url"] for c in resp.json()["callbacks"]}
    assert urls == {
        "{$request.body#/tags/1}": "b",
        "{$request.body#/slash/a~1b}": "1",
        "{$request.body#/slash/c~0d}": "2",
    }

def test_whole_body_scalar_is_allowed():
    doc = single_callback("{$request.body}")
    context = {
        "request": {"method": "POST", "body": "raw-body-id"},
        "response": {"statusCode": 200},
    }
    resp = resolve(doc, context=context)
    assert resp.status_code == 200, resp.text
    assert resp.json()["callbacks"][0]["url"] == "raw-body-id"


def test_scalars_render_like_json_primitives():
    doc = make_doc({
        "cb": {
            "{$request.path.petId}": {"get": {"responses": {}}},
            "{$request.body#/id}": {"get": {"responses": {}}},
        }
    })
    context = {
        "request": {"method": "POST", "path": {"petId": 99},
                    "body": {"id": True}},
        "response": {"statusCode": 200},
    }
    resp = resolve(doc, context=context)
    assert resp.status_code == 200, resp.text
    urls = {c["expression"]: c["url"] for c in resp.json()["callbacks"]}
    assert urls == {"{$request.path.petId}": "99", "{$request.body#/id}": "true"}


def test_headers_match_case_insensitively():
    doc = single_callback("{$request.header.x-token}")
    context = {
        "request": {"method": "POST", "header": {"X-TOKEN": "ok"}},
        "response": {"statusCode": 200},
    }
    resp = resolve(doc, context=context)
    assert resp.status_code == 200
    assert resp.json()["callbacks"][0]["url"] == "ok"


def test_response_headers_match_case_insensitively():
    doc = single_callback("{$response.header.loCATion}")
    context = {
        "request": {"method": "POST"},
        "response": {"statusCode": 201, "header": {"LOCATION": "/z"}},
    }
    resp = resolve(doc, context=context)
    assert resp.status_code == 200
    assert resp.json()["callbacks"][0]["url"] == "/z"


def test_multiple_embeddings_in_one_key_all_resolve():
    doc = single_callback(
        "{$method}/{$statusCode}/{$request.path.petId}/{$request.query.code}"
    )
    resp = resolve(doc)
    assert resp.status_code == 200
    assert resp.json()["callbacks"][0]["url"] == "POST/201/p42/ab"


def test_path_item_is_retained_with_all_fields():
    path_item = {
        "summary": "hook",
        "description": "receiver",
        "parameters": [{"name": "x", "in": "header"}],
        "servers": [{"url": "https://cb.example.com"}],
        "post": {"responses": {"200": {"description": "ok"}}},
    }
    doc = single_callback("{$request.path.petId}", path_item)
    resp = resolve(doc)
    assert resp.status_code == 200
    assert resp.json()["callbacks"][0]["path_item"] == path_item


def test_operation_without_callbacks_returns_empty_list():
    doc = make_doc()
    doc["paths"]["/pets"]["post"].pop("callbacks", None)
    resp = resolve(doc)
    assert resp.status_code == 200
    assert resp.json() == {"callbacks": []}


def test_selector_method_is_case_insensitive():
    resp = resolve(single_callback("{$request.path.petId}"), "pOsT /pets")
    assert resp.status_code == 200


def test_selector_works_when_path_item_is_a_31_ref():
    doc = make_doc(
        {"cb": {"{$request.path.petId}": {"get": {"responses": {}}}}},
        openapi="3.1.0",
        path="/things",
        components=None,
    )
    doc["paths"]["/things"] = {
        "$ref": "#/components/pathItems/Things",
        "post": {"callbacks": {"cb": {
            "{$request.path.petId}": {"get": {"responses": {}}}
        }}, "responses": {}},
    }
    doc["components"] = {"pathItems": {"Things": {"parameters": []}}}
    resp = resolve(doc, "POST /things")
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# Local $ref resolution of Callback / Path Item Objects
# --------------------------------------------------------------------------- #

def test_callback_object_via_local_ref():
    doc = make_doc(
        {"onCreated": {"$ref": "#/components/callbacks/OnCreated"}},
        components={"callbacks": {"OnCreated": {
            "{$request.path.petId}": {"post": {"responses": {}}},
        }}},
    )
    resp = resolve(doc)
    assert resp.status_code == 200, resp.text
    entry = resp.json()["callbacks"][0]
    assert entry["url"] == "p42"
    # Expression lives physically in the component; the map entry is at
    # the operation callbacks location.
    assert entry["callback_pointer"] == "/paths/~1pets/post/callbacks/onCreated"
    assert entry["expression_pointer"] == (
        "/components/callbacks/OnCreated/{$request.path.petId}"
    )


def test_path_item_via_local_ref_is_inlined():
    doc = make_doc({
        "cb": {"{$request.path.petId}": {
            "$ref": "#/components/pathItems/HookItem"}}},
        components={"pathItems": {"HookItem": {
            "post": {"responses": {"200": {"description": "ok"}}}}}},
    )
    resp = resolve(doc)
    assert resp.status_code == 200, resp.text
    entry = resp.json()["callbacks"][0]
    assert entry["url"] == "p42"
    assert entry["path_item"] == {
        "post": {"responses": {"200": {"description": "ok"}}}
    }
    assert "$ref" not in entry["path_item"]


def test_dangling_callback_ref_is_422():
    doc = make_doc(
        {"cb": {"$ref": "#/components/callbacks/Missing"}}
    )
    resp = resolve(doc)
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert len(errors) == 1
    assert errors[0]["code"] == "unresolvable_reference"
    assert errors[0]["ref"] == "#/components/callbacks/Missing"
    assert errors[0]["callback_pointer"] == "/paths/~1pets/post/callbacks/cb"
    assert errors[0]["ref_pointer"] == "/paths/~1pets/post/callbacks/cb"


def test_dangling_path_item_ref_is_422():
    doc = make_doc({
        "cb": {"{$request.path.petId}": {"$ref": "#/components/pathItems/Gone"}}
    })
    resp = resolve(doc)
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert error["ref"] == "#/components/pathItems/Gone"
    assert error["callback_pointer"] == "/paths/~1pets/post/callbacks/cb"
    assert error["expression"] == "{$request.path.petId}"
    assert error["ref_pointer"] == (
        "/paths/~1pets/post/callbacks/cb/{$request.path.petId}"
    )


def test_external_callback_ref_is_422():
    for ref in ("https://example.com/cb.json#/x", "callbacks.json#/x", "//host/x"):
        doc = make_doc({"cb": {"$ref": ref}})
        resp = resolve(doc)
        assert resp.status_code == 422, ref
        error = resp.json()["errors"][0]
        assert error["code"] == "unresolvable_reference"
        assert error["ref"] == ref


def test_circular_callback_ref_chain_is_422():
    doc = make_doc(
        {"a": {"$ref": "#/components/callbacks/A"}},
        components={"callbacks": {
            "A": {"$ref": "#/components/callbacks/B"},
            "B": {"$ref": "#/components/callbacks/A"},
        }},
    )
    resp = resolve(doc)
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert "circular reference detected" in error["error"]
    # Points at the $ref that closes the loop (inside components).
    assert error["ref_pointer"] == "/components/callbacks/B"


def test_circular_path_item_ref_chain_is_422():
    doc = make_doc({
        "cb": {"{$request.path.petId}": {"$ref": "#/components/pathItems/A"}}},
        components={"pathItems": {
            "A": {"$ref": "#/components/pathItems/B"},
            "B": {"$ref": "#/components/pathItems/A"},
        }},
    )
    resp = resolve(doc)
    assert resp.status_code == 422
    assert "circular reference detected" in resp.json()["errors"][0]["error"]


def test_ref_targeting_non_object_is_422():
    doc = make_doc(
        {"cb": {"$ref": "#/x"}},
        components=None,
    )
    doc["x"] = "not-an-object"
    resp = resolve(doc)
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "unresolvable_reference"


def test_openapi_31_sibling_keys_override_referenced_callback():
    doc = make_doc(
        {"a": {
            "$ref": "#/components/callbacks/A",
            "{$request.path.petId}/override": {"get": {"responses": {}}},
        }},
        openapi="3.1.0",
        components={"callbacks": {"A": {
            "base": {"get": {"responses": {}}},
        }}},
    )
    resp = resolve(doc)
    assert resp.status_code == 200, resp.text
    urls = {c["url"]: c for c in resp.json()["callbacks"]}
    assert set(urls) == {"base", "p42/override"}
    # The sibling key physically exists at the referencing operation node.
    assert urls["p42/override"]["expression_pointer"] == (
        "/paths/~1pets/post/callbacks/a/{$request.path.petId}~1override"
    )
    assert urls["base"]["expression_pointer"] == (
        "/components/callbacks/A/base"
    )


def test_openapi_30_ignores_ref_siblings():
    doc = make_doc(
        {"a": {
            "$ref": "#/components/callbacks/A",
            "{$request.path.petId}/override": {"get": {"responses": {}}},
        }},
        openapi="3.0.3",
        components={"callbacks": {"A": {
            "base": {"get": {"responses": {}}},
        }}},
    )
    resp = resolve(doc)
    assert resp.status_code == 200, resp.text
    assert [c["url"] for c in resp.json()["callbacks"]] == ["base"]


# --------------------------------------------------------------------------- #
# Missing values
# --------------------------------------------------------------------------- #

def test_missing_url_method_status_code_are_422():
    doc = make_doc({
        "u": {"{$url}": {"get": {"responses": {}}}},
        "m": {"{$method}": {"get": {"responses": {}}}},
        "s": {"{$statusCode}": {"get": {"responses": {}}}},
    })
    resp = resolve(doc, context={})
    assert resp.status_code == 422
    errors = {e["expression"]: e for e in resp.json()["errors"]}
    assert set(errors) == {"$url", "$method", "$statusCode"}
    for error in errors.values():
        assert error["code"] == "missing_value"
        assert error["callback_pointer"]
        assert error["expression_pointer"]


def test_missing_request_side_is_422():
    doc = make_doc({"cb": {"{$request.path.petId}": {"get": {"responses": {}}}}})
    resp = resolve(doc, context={"response": {"statusCode": 201}})
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "missing_value"
    assert "absent from the supplied context" in error["error"]


def test_missing_path_query_header_name_is_422():
    doc = make_doc({
        "p": {"{$request.path.nope}": {"get": {"responses": {}}}},
        "q": {"{$request.query.nope}": {"get": {"responses": {}}}},
        "h": {"{$request.header.nope}": {"get": {"responses": {}}}},
    })
    context = {
        "request": {"method": "POST", "path": {}, "query": {}, "header": {}},
        "response": {"statusCode": 201},
    }
    resp = resolve(doc, context=context)
    assert resp.status_code == 422
    assert len(resp.json()["errors"]) == 3
    assert all(e["code"] == "missing_value" for e in resp.json()["errors"])


def test_missing_mapping_envelope_is_422():
    doc = single_callback("{$request.query.code}")
    resp = resolve(doc, context={
        "request": {"method": "POST"}, "response": {"statusCode": 201},
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "missing_value"


def test_missing_body_and_body_pointer_are_422():
    doc = make_doc({
        "whole": {"{$request.body}": {"get": {"responses": {}}}},
        "frag": {"{$request.body#/owner/missing}": {"get": {"responses": {}}}},
    })
    context = {"request": {"method": "POST", "body": {"owner": {"name": "s"}}},
               "response": {"statusCode": 201}}
    resp = resolve(doc, context=context)
    assert resp.status_code == 422
    codes = {e["expression"]: e["code"] for e in resp.json()["errors"]}
    assert codes["$request.body"] == "non_scalar_value"
    assert codes["$request.body#/owner/missing"] == "missing_value"

    resp2 = resolve(single_callback("{$request.body#/id}"), context={
        "request": {"method": "POST"}, "response": {"statusCode": 201},
    })
    assert resp2.status_code == 422
    assert "absent from the supplied context" in (
        resp2.json()["errors"][0]["error"]
    )


def test_body_array_index_out_of_range_is_422():
    doc = single_callback("{$request.body#/tags/9}")
    resp = resolve(doc)
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "missing_value"


# --------------------------------------------------------------------------- #
# Illegal expressions
# --------------------------------------------------------------------------- #

def test_embedding_without_dollar_is_422():
    doc = single_callback("hook/{notAnExpression}")
    resp = resolve(doc)
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "invalid_expression"
    assert error["expression"] == "notAnExpression"


def test_empty_embedding_and_unbalanced_braces_are_422():
    for key in ("hook/{}", "hook/{", "hook/}", "hook/{$request.path.id",
                "hook/{$request.path.id}}"):
        doc = single_callback(key)
        resp = resolve(doc)
        assert resp.status_code == 422, key
        assert resp.json()["errors"][0]["code"] == "invalid_expression"


def test_unknown_expression_source_is_422():
    for key in ("$bogus", "{$bogus}", "{$request.unknown.petId}",
                "$response.statusCode", "{$request.path}", "{$request.bodyx}",
                "{$request.query.a:b}"):
        doc = single_callback(key)
        resp = resolve(doc)
        assert resp.status_code == 422, key
        assert resp.json()["errors"][0]["code"] == "invalid_expression", key


def test_unreserved_path_and_query_names_include_dash_dot_tilde():
    doc = make_doc({
        "p": {"{$request.path.a-b.c~d}": {"get": {"responses": {}}}},
        "q": {"{$request.query.x_y}": {"get": {"responses": {}}}},
    })
    context = {
        "request": {"method": "POST",
                    "path": {"a-b.c~d": "P"}, "query": {"x_y": "Q"}},
        "response": {"statusCode": 201},
    }
    resp = resolve(doc, context=context)
    assert resp.status_code == 200, resp.text
    urls = {c["expression"]: c["url"] for c in resp.json()["callbacks"]}
    assert urls == {"{$request.path.a-b.c~d}": "P", "{$request.query.x_y}": "Q"}


def test_invalid_header_token_and_query_name_are_422():
    doc = make_doc({
        "h": {"{$request.header.bad name}": {"get": {"responses": {}}}},
        "q1": {"{$request.query.a/b}": {"get": {"responses": {}}}},
        "q2": {"{$request.query.a%2}": {"get": {"responses": {}}}},
    })
    resp = resolve(doc)
    assert resp.status_code == 422
    assert all(e["code"] == "invalid_expression" for e in resp.json()["errors"])


def test_invalid_body_pointer_fragment_is_422():
    for key in ("{$request.body#missing}", "{$request.body#/%zz}"):
        doc = single_callback(key)
        resp = resolve(doc)
        assert resp.status_code == 422, key
        assert resp.json()["errors"][0]["code"] == "invalid_expression"


# --------------------------------------------------------------------------- #
# Non-scalar interpolation
# --------------------------------------------------------------------------- #

def test_object_array_and_null_interpolations_are_422():
    doc = make_doc({
        "o": {"{$request.body#/owner}": {"get": {"responses": {}}}},
        "a": {"{$request.body#/tags}": {"get": {"responses": {}}}},
        "n": {"{$request.query.maybe}": {"get": {"responses": {}}}},
    })
    context = {
        "request": {
            "method": "POST",
            "query": {"maybe": None},
            "body": {"owner": {"name": "s"}, "tags": []},
        },
        "response": {"statusCode": 201},
    }
    resp = resolve(doc, context=context)
    assert resp.status_code == 422
    errors = {e["expression"]: e for e in resp.json()["errors"]}
    assert set(errors) == {
        "$request.body#/owner", "$request.body#/tags", "$request.query.maybe"
    }
    assert "object" in errors["$request.body#/owner"]["error"]
    assert "array" in errors["$request.body#/tags"]["error"]
    assert "null" in errors["$request.query.maybe"]["error"]
    for error in errors.values():
        assert error["code"] == "non_scalar_value"
        assert error["context_pointer"]


# --------------------------------------------------------------------------- #
# Malformed Callback Objects
# --------------------------------------------------------------------------- #

def test_non_object_callback_entry_is_422():
    doc = make_doc({"bad": "nope"})
    resp = resolve(doc)
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "invalid_callback"
    assert error["callback_pointer"] == "/paths/~1pets/post/callbacks/bad"


def test_callback_object_without_expression_keys_is_422():
    doc = make_doc({"bad": {"summary": "no keys"}})
    resp = resolve(doc)
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "invalid_callback"


def test_non_object_path_item_is_422():
    doc = single_callback("{$request.path.petId}", "not-a-path-item")
    resp = resolve(doc)
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "invalid_callback"
    assert error["expression"] == "{$request.path.petId}"
    assert error["expression_pointer"]


def test_callbacks_mapping_not_an_object_is_422():
    doc = make_doc()
    doc["paths"]["/pets"]["post"]["callbacks"] = []
    resp = resolve(doc)
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "invalid_callback"


# --------------------------------------------------------------------------- #
# Ordering, pointers and immutability
# --------------------------------------------------------------------------- #

def test_results_sort_stably_by_source_pointer_independent_of_key_order():
    key = "{$request.path.petId}"
    item = {"get": {"responses": {}}}

    def build(order):
        callbacks = {name: {key: copy.deepcopy(item)} for name in order}
        return make_doc(callbacks)

    ascending = ["a1", "a2", "a3"]
    resp1 = resolve(build(ascending))
    resp2 = resolve(build(list(reversed(ascending))))
    assert resp1.status_code == resp2.status_code == 200
    names1 = [c["name"] for c in resp1.json()["callbacks"]]
    names2 = [c["name"] for c in resp2.json()["callbacks"]]
    assert names1 == names2 == ascending
    # Pointers are sorted component-wise too.
    pointers = [c["callback_pointer"] for c in resp1.json()["callbacks"]]
    assert pointers == sorted(pointers)


def test_every_result_and_error_carries_source_pointers():
    doc = make_doc({
        "ok": {"{$request.path.petId}": {"get": {"responses": {}}}},
        "bad": {"{$request.path.missing}": {"get": {"responses": {}}}},
    })
    resp = resolve(doc, context={
        "request": {"method": "POST", "path": {"petId": "x"}},
        "response": {"statusCode": 201},
    })
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["callback_pointer"] == "/paths/~1pets/post/callbacks/bad"
    assert error["expression_pointer"] == (
        "/paths/~1pets/post/callbacks/bad/{$request.path.missing}"
    )


def test_input_document_and_context_are_not_modified():
    doc = make_doc(
        {"onCreated": {"$ref": "#/components/callbacks/OnCreated"}},
        components={"callbacks": {"OnCreated": {
            "{$request.path.petId}": {
                "$ref": "#/components/pathItems/Hook"}},
        }, "pathItems": {"Hook": {"post": {"responses": {}}}}},
    )
    context = {
        "request": {"method": "POST", "path": {"petId": "x"}},
        "response": {"statusCode": 201},
    }
    doc_snapshot = copy.deepcopy(doc)
    ctx_snapshot = copy.deepcopy(context)
    resp = resolve(doc, context=context)
    assert resp.status_code == 200, resp.text
    assert doc == doc_snapshot
    assert context == ctx_snapshot
    # The returned path item is a copy, not the input object.
    returned = resp.json()["callbacks"][0]["path_item"]
    returned["post"]["injected"] = True
    assert "injected" not in doc["components"]["pathItems"]["Hook"]["post"]


# --------------------------------------------------------------------------- #
# 400 request-shape errors
# --------------------------------------------------------------------------- #

def test_non_openapi_document_is_400():
    payload = {
        "document": {"swagger": "2.0", "paths": {}},
        "selector": "POST /pets",
        "context": {},
    }
    resp = client.post("/callbacks/resolve", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_openapi_document"


def test_unknown_path_and_method_are_400():
    doc = make_doc()
    assert resolve(doc, "POST /missing").status_code == 400
    assert resolve(doc, "DELETE /pets").status_code == 400


def test_malformed_selector_is_400():
    doc = make_doc()
    assert resolve(doc, "garbage").status_code == 400
    assert resolve(doc, "POST").status_code == 400


def test_bad_context_envelope_is_400():
    doc = make_doc()
    base = {"document": doc, "selector": "POST /pets", "context": {}}

    def post(context):
        payload = dict(base, context=context)
        return client.post("/callbacks/resolve", json=payload)

    assert post("not-an-object").status_code == 400
    resp = post({"unknown-side": {}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_context"
    assert post({"request": []}).status_code == 400
    assert post({"request": {"unexpected": 1}}).status_code == 400
    assert post({"response": {"statusCode": "201"}}).status_code == 400
    assert post({"request": {"url": 7}}).status_code == 400
    assert post({"request": {"header": []}}).status_code == 400


def test_missing_required_request_fields_use_default_pydantic_422():
    resp = client.post("/callbacks/resolve", json={"document": {}})
    assert resp.status_code == 422
