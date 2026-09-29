"""Regression tests for ``POST /callbacks/resolve``.

Covers runtime-expression evaluation (literal keys, $url/$method/
$statusCode, request path/query/header/body and response header/body,
JSON Pointer body fragments, case-insensitive headers, scalar-only
interpolation), cycle-safe Callback Object / Path Item $ref resolution,
422 error reporting with source JSON Pointers, stable sorting and the
non-mutation guarantee.
"""

from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def make_doc(callbacks=None, *, openapi="3.0.3", path="/subscribe",
             method="post", components=None, path_item_extra=None,
             operation_extra=None):
    operation = {"responses": {"202": {"description": "accepted"}}}
    if operation_extra:
        operation.update(operation_extra)
    if callbacks is not None:
        operation["callbacks"] = callbacks
    item = {method: operation}
    if path_item_extra:
        item.update(path_item_extra)
    doc = {
        "openapi": openapi,
        "info": {"title": "Hook API", "version": "1.0"},
        "paths": {path: item},
    }
    if components is not None:
        doc["components"] = components
    return doc


def default_context(**overrides):
    context = {
        "request": {
            "url": "https://api.example.com/subscribe?x=1",
            "method": "POST",
            "path": {"txId": "abc-123", "n": 7, "flag": True},
            "query": {"limit": "50", "tags": ["a", "b"], "x/y": "z"},
            "header": {"X-Trace": "t-9", "Content-Type": "application/json"},
            "body": {
                "callbackUrl": "https://my.host/hook",
                "nested": {"deep": {"url": "https://deep.example.com/x"}},
                "array": [{"href": "https://arr.example.com/0"}],
                "obj": {"a": 1},
                "count": 3,
            },
        },
        "response": {
            "statusCode": 202,
            "header": {"X-Request-Id": "r1", "Location": "/things/1"},
            "body": {"links": {"done": "https://done.example.com"}},
        },
    }
    context.update(overrides)
    return context


def resolve(doc, selector="POST /subscribe", context=None):
    payload = {"document": doc, "selector": selector}
    if context is not None:
        payload["context"] = context
    return client.post("/callbacks/resolve", json=payload)


def by_expression(items):
    return {item["expression"]: item for item in items}


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_literal_key_is_a_fixed_callback_url():
    doc = make_doc({"onEvent": {"https://fixed.example.com/hook": {
        "post": {"responses": {"2XX": {"description": "ack"}}}}}})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["url"] == "https://fixed.example.com/hook"
    assert item["name"] == "onEvent"
    assert item["path_item"] == {
        "post": {"responses": {"2XX": {"description": "ack"}}}
    }
    assert item["callback_pointer"] == "/paths/~1subscribe/post/callbacks/onEvent"
    assert item["expression_pointer"] == (
        "/paths/~1subscribe/post/callbacks/onEvent/https:~1~1fixed.example.com~1hook"
    )


def test_standalone_simple_expressions():
    doc = make_doc({
        "c": {
            "$url": {"post": {"responses": {"2XX": {"description": "a"}}}},
            "$method": {"post": {"responses": {"2XX": {"description": "b"}}}},
            "$statusCode": {"post": {"responses": {"2XX": {"description": "c"}}}},
        }
    })
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    values = by_expression(resp.json())
    assert values["$url"]["url"] == "https://api.example.com/subscribe?x=1"
    assert values["$method"]["url"] == "POST"
    assert values["$statusCode"]["url"] == "202"


def test_embedded_request_and_response_expressions():
    doc = make_doc({"onEvent": {
        "https://h/{$request.path.txId}/{$request.query.limit}": {"post": {"responses": {"2XX": {"description": "p"}}}},
        "trace:{$request.header.x-trace}": {"post": {"responses": {"2XX": {"description": "h"}}}},
        "{$request.body#/callbackUrl}": {"post": {"responses": {"2XX": {"description": "b"}}}},
        "{$request.body#/nested/deep/url}": {"post": {"responses": {"2XX": {"description": "d"}}}},
        "{$request.body#/array/0/href}": {"post": {"responses": {"2XX": {"description": "a"}}}},
        "resp:{$response.header.x-request-id}": {"post": {"responses": {"2XX": {"description": "rh"}}}},
        "{$response.body#/links/done}": {"post": {"responses": {"2XX": {"description": "rb"}}}},
    }})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    values = by_expression(resp.json())
    assert values["https://h/{$request.path.txId}/{$request.query.limit}"]["url"] == "https://h/abc-123/50"
    assert values["trace:{$request.header.x-trace}"]["url"] == "trace:t-9"
    assert values["{$request.body#/callbackUrl}"]["url"] == "https://my.host/hook"
    assert values["{$request.body#/nested/deep/url}"]["url"] == "https://deep.example.com/x"
    assert values["{$request.body#/array/0/href}"]["url"] == "https://arr.example.com/0"
    assert values["resp:{$response.header.x-request-id}"]["url"] == "resp:r1"
    assert values["{$response.body#/links/done}"]["url"] == "https://done.example.com"


def test_header_names_match_case_insensitively_but_declared_name_is_used():
    doc = make_doc({"onEvent": {"{$request.header.X-TRACE}": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["url"] == "t-9"


def test_numbers_and_booleans_render_like_json_primitives():
    doc = make_doc({"onEvent": {
        "n={$request.path.n}&f={$request.path.flag}&c={$request.body#/count}": {
            "post": {"responses": {"2XX": {"description": "a"}}}}}})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["url"] == "n=7&f=true&c=3"


def test_status_code_string_context_renders_verbatim():
    context = default_context()
    context["response"]["statusCode"] = "202"
    doc = make_doc({"onEvent": {"{$statusCode}": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    resp = resolve(doc, context=context)
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["url"] == "202"


def test_percent_encoded_query_and_path_names_are_decoded_for_lookup():
    doc = make_doc({"onEvent": {"{$request.query.x%2Fy}": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["url"] == "z"


def test_results_include_the_path_item_with_nested_refs_kept_verbatim():
    doc = make_doc(
        {"onEvent": {"{$request.body#/callbackUrl}": {
            "post": {
                "requestBody": {"$ref": "#/components/requestBodies/Event"},
                "responses": {"2XX": {"description": "a"}},
            }}}},
        components={"requestBodies": {"Event": {"content": {}}}},
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["path_item"] == {
        "post": {
            "requestBody": {"$ref": "#/components/requestBodies/Event"},
            "responses": {"2XX": {"description": "a"}},
        }
    }


def test_operation_without_callbacks_returns_empty_list():
    doc = make_doc(callbacks=None)
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_empty_context_with_only_literal_key_succeeds():
    doc = make_doc({"onEvent": {"https://fixed.example.com": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    resp = resolve(doc, context={})
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["url"] == "https://fixed.example.com"


def test_method_selector_is_case_insensitive():
    doc = make_doc({"onEvent": {"https://fixed.example.com": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    resp = resolve(doc, selector="post /subscribe", context={})
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# Stable sorting
# --------------------------------------------------------------------------- #

def test_results_sorted_stably_by_source_pointer_regardless_of_key_order():
    doc = make_doc({
        "zzz": {
            "b-{$request.path.txId}": {"post": {"responses": {"2XX": {"description": "a"}}}},
            "a-{$request.path.txId}": {"post": {"responses": {"2XX": {"description": "a"}}}},
        },
        "aaa": {
            "c-{$request.path.txId}": {"post": {"responses": {"2XX": {"description": "a"}}}},
        },
    })
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    names_and_expressions = [(i["name"], i["expression"]) for i in resp.json()]
    # callback "aaa" sorts before "zzz"; within "zzz", "a-..." before "b-..."
    assert names_and_expressions == [
        ("aaa", "c-{$request.path.txId}"),
        ("zzz", "a-{$request.path.txId}"),
        ("zzz", "b-{$request.path.txId}"),
    ]


# --------------------------------------------------------------------------- #
# Missing values (422)
# --------------------------------------------------------------------------- #

def test_missing_url_method_statuscode_return_422_with_pointers():
    doc = make_doc({"onEvent": {
        "$url": {"post": {"responses": {"2XX": {"description": "a"}}}},
        "$method": {"post": {"responses": {"2XX": {"description": "b"}}}},
        "$statusCode": {"post": {"responses": {"2XX": {"description": "c"}}}},
    }})
    resp = resolve(doc, context={"request": {}})
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    by_expr = {e["expression"]: e for e in errors}
    assert by_expr["$url"]["code"] == "missing_value"
    assert by_expr["$url"]["context_pointer"] == "/context/request/url"
    assert by_expr["$method"]["context_pointer"] == "/context/request/method"
    assert by_expr["$statusCode"]["context_pointer"] == "/context/response/statusCode"
    for error in errors:
        assert error["callback_pointer"] == "/paths/~1subscribe/post/callbacks/onEvent"
        assert error["expression_pointer"].startswith(
            "/paths/~1subscribe/post/callbacks/onEvent/"
        )


def test_missing_path_query_header_body_values_return_422():
    doc = make_doc({"onEvent": {
        "{$request.path.missing}": {"post": {"responses": {"2XX": {"description": "a"}}}},
        "{$request.query.missing}": {"post": {"responses": {"2XX": {"description": "b"}}}},
        "{$request.header.missing}": {"post": {"responses": {"2XX": {"description": "c"}}}},
        "{$request.body#/missing}": {"post": {"responses": {"2XX": {"description": "d"}}}},
        "{$response.header.missing}": {"post": {"responses": {"2XX": {"description": "e"}}}},
        "{$response.body#/missing}": {"post": {"responses": {"2XX": {"description": "f"}}}},
    }})
    resp = resolve(doc, context={"request": {"path": {}, "query": {}, "header": {}, "body": {}},
                                 "response": {"header": {}, "body": {}}})
    assert resp.status_code == 422
    assert [e["code"] for e in resp.json()["errors"]] == ["missing_value"] * 6
    pointers = {e["context_pointer"] for e in resp.json()["errors"]}
    assert "/context/request/path/missing" in pointers
    assert "/context/request/body/missing" in pointers
    assert "/context/response/body/missing" in pointers


def test_json_pointer_into_scalar_or_bad_array_index_is_missing():
    doc = make_doc({"onEvent": {
        "{$request.body#/count/x}": {"post": {"responses": {"2XX": {"description": "a"}}}},
        "{$request.body#/array/9/href}": {"post": {"responses": {"2XX": {"description": "b"}}}},
        "{$request.body#/array/01}": {"post": {"responses": {"2XX": {"description": "c"}}}},
        "{$request.body#/array/-}": {"post": {"responses": {"2XX": {"description": "d"}}}},
    }})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    assert [e["code"] for e in resp.json()["errors"]] == ["missing_value"] * 4


def test_null_context_values_are_treated_as_missing():
    doc = make_doc({"onEvent": {
        "{$request.query.q}": {"post": {"responses": {"2XX": {"description": "a"}}}},
        "{$request.body#/maybe}": {"post": {"responses": {"2XX": {"description": "b"}}}},
    }})
    context = {"request": {"query": {"q": None}, "body": {"maybe": None}}}
    resp = resolve(doc, context=context)
    assert resp.status_code == 422
    assert [e["code"] for e in resp.json()["errors"]] == ["missing_value"] * 2


def test_missing_request_section_for_body_expression_is_422():
    doc = make_doc({"onEvent": {"{$request.body#/id}": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    resp = resolve(doc, context={})
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "missing_value"
    assert error["context_pointer"] == "/context/request/body"


# --------------------------------------------------------------------------- #
# Non-scalar interpolation (422)
# --------------------------------------------------------------------------- #

def test_non_scalar_interpolation_returns_422():
    doc = make_doc({"onEvent": {
        "{$request.query.tags}": {"post": {"responses": {"2XX": {"description": "a"}}}},
        "{$request.body#/obj}": {"post": {"responses": {"2XX": {"description": "b"}}}},
        "{$request.body}": {"post": {"responses": {"2XX": {"description": "c"}}}},
    }})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert [e["code"] for e in errors] == ["non_scalar_interpolation"] * 3
    by_expr = {e["expression"]: e for e in errors}
    assert by_expr["$request.query.tags"]["context_pointer"] == "/context/request/query/tags"
    assert by_expr["$request.body#/obj"]["context_pointer"] == "/context/request/body/obj"
    assert by_expr["$request.body"]["context_pointer"] == "/context/request/body"


def test_scalar_whole_body_standalone_expression_succeeds():
    doc = make_doc({"onEvent": {"{$request.body}": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    context = {"request": {"body": "https://direct.example.com"}}
    resp = resolve(doc, context=context)
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["url"] == "https://direct.example.com"


def test_ambiguous_case_variant_header_is_422():
    doc = make_doc({"onEvent": {"{$request.header.x-trace}": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    context = {"request": {"header": {"X-Trace": "a", "x-trace": "b"}}}
    resp = resolve(doc, context=context)
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "ambiguous_header"
    assert error["context_pointer"] == "/context/request/header/x-trace"


# --------------------------------------------------------------------------- #
# Invalid expressions (422)
# --------------------------------------------------------------------------- #

def test_invalid_expressions_return_422():
    invalid = [
        "{txId}",                       # bare template variable
        "{$foo}",                       # unknown root
        "{$request.unknown.x}",         # unknown source
        "{$response.path.x}",           # response has no path/query
        "{$request.query}",             # missing name
        "{$request.header.}",           # empty header token
        "{$request.body/foo}",          # body fragment needs '#'
        "{$request.body#/a~}",          # bad JSON Pointer escape
        "{$request.body#2}",            # pointer not starting with '/'
        "{$request.query.x%zz}",        # bad percent triplet
        "{$request.query.x ",           # trailing whitespace
        "{}",                           # empty braces
        "{$request.path.x}}",           # stray closing brace
        "{$request.path.x",             # unclosed brace in a template
        "{{$request.path.x}}",          # nested braces
        "{$request.body#/a~2}",         # '~' not followed by 0/1
        "{$statusCode.extra}",          # simple expression with a tail
        "",                             # empty key
    ]
    doc = make_doc({"onEvent": {
        key: {"post": {"responses": {"2XX": {"description": "x"}}}}
        for key in invalid
    }})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert len(errors) == len(invalid)
    assert {e["code"] for e in errors} == {"invalid_expression"}
    expressions = {e["expression"] for e in errors}
    assert expressions == set(invalid)
    for error in errors:
        assert error["expression_pointer"].startswith(
            "/paths/~1subscribe/post/callbacks/onEvent/"
        )


def test_embedded_expression_preserves_literal_surroundings_in_error_key():
    doc = make_doc({"onEvent": {"pre-{$nope}-post": {
        "post": {"responses": {"2XX": {"description": "x"}}}}}})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "invalid_expression"
    assert error["expression"] == "pre-{$nope}-post"


def test_complete_expression_with_trailing_text_is_not_standalone():
    # '$url/x' is not a valid standalone expression; it contains no braces,
    # so it is simply a fixed literal callback URL.
    doc = make_doc({"onEvent": {"$url/x": {
        "post": {"responses": {"2XX": {"description": "x"}}}}}})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["url"] == "$url/x"


# --------------------------------------------------------------------------- #
# $ref resolution
# --------------------------------------------------------------------------- #

CALLBACK_COMPONENTS = {
    "callbacks": {
        "OnEvent": {
            "{$request.body#/callbackUrl}": {
                "post": {"responses": {"2XX": {"description": "from-component"}}}
            },
            "$url": {"post": {"responses": {"2XX": {"description": "url"}}}},
        },
        "LoopA": {"$ref": "#/components/callbacks/LoopB"},
        "LoopB": {"$ref": "#/components/callbacks/LoopA"},
        "ViaPathItemRef": {
            "{$request.body#/callbackUrl}": {"$ref": "#/pathItems/Hook"}
        },
        "External": {"{$request.body#/callbackUrl}": {
            "$ref": "https://example.com/openapi.yaml#/paths/~1hook/post"}},
        "Dangling": {"{$request.body#/callbackUrl}": {
            "$ref": "#/pathItems/Missing"}},
        "NotAnObject": {"{$request.body#/callbackUrl}": {"$ref": "#/components/schemas/S"}},
    },
    "schemas": {"S": "not-an-object"},
}

PATH_ITEM_COMPONENTS = {
    "pathItems": {
        "Hook": {
            "post": {"responses": {"2XX": {"description": "from-path-item-component"}}}
        }
    }
}


def test_callback_object_local_ref_resolves_and_points_at_component():
    components = copy.deepcopy(CALLBACK_COMPONENTS)
    doc = make_doc(
        {"onEvent": {"$ref": "#/components/callbacks/OnEvent"}},
        components=components,
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    values = by_expression(resp.json())
    # occurrence pointer is the $ref site
    assert values["$url"]["callback_pointer"] == (
        "/paths/~1subscribe/post/callbacks/onEvent"
    )
    # expression pointer follows the chain into the component
    assert values["$url"]["expression_pointer"] == (
        "/components/callbacks/OnEvent/$url"
    )
    assert values["{$request.body#/callbackUrl}"]["expression_pointer"] == (
        "/components/callbacks/OnEvent/{$request.body#~1callbackUrl}"
    )
    assert values["{$request.body#/callbackUrl}"]["url"] == "https://my.host/hook"


def test_path_item_value_local_ref_resolves():
    components = copy.deepcopy(CALLBACK_COMPONENTS)
    doc = make_doc(
        {"onEvent": {"$ref": "#/components/callbacks/ViaPathItemRef"}},
        components=components,
    )
    doc["pathItems"] = copy.deepcopy(PATH_ITEM_COMPONENTS["pathItems"])
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    item = resp.json()[0]
    assert item["path_item"] == {
        "post": {"responses": {"2XX": {"description": "from-path-item-component"}}}
    }


def test_dangling_callback_ref_is_422_at_occurrence():
    doc = make_doc(
        {"onEvent": {"$ref": "#/components/callbacks/Missing"}},
        components={"callbacks": {}},
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert error["pointer"] == "/paths/~1subscribe/post/callbacks/onEvent"
    assert error["ref"] == "#/components/callbacks/Missing"
    assert error["callback_pointer"] == "/paths/~1subscribe/post/callbacks/onEvent"


def test_dangling_path_item_ref_is_422_at_expression():
    components = copy.deepcopy(CALLBACK_COMPONENTS)
    components["pathItems"] = {}
    doc = make_doc(
        {"onEvent": {"$ref": "#/components/callbacks/Dangling"}},
        components=components,
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert error["ref"] == "#/pathItems/Missing"
    assert error["expression"] == "{$request.body#/callbackUrl}"
    assert error["expression_pointer"] == (
        "/components/callbacks/Dangling/{$request.body#~1callbackUrl}"
    )


def test_external_path_item_ref_is_422():
    doc = make_doc(
        {"onEvent": {"$ref": "#/components/callbacks/External"}},
        components=copy.deepcopy(CALLBACK_COMPONENTS),
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert error["ref"].startswith("https://")


def test_circular_callback_ref_chain_is_422():
    doc = make_doc(
        {"onEvent": {"$ref": "#/components/callbacks/LoopA"}},
        components=copy.deepcopy(CALLBACK_COMPONENTS),
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert "circular reference detected" in error["error"]
    # the error points at the $ref occurrence whose target closes the loop
    assert error["pointer"] == "/components/callbacks/LoopB"
    assert error["callback_pointer"] == "/paths/~1subscribe/post/callbacks/onEvent"


def test_reference_target_that_is_not_an_object_is_422():
    doc = make_doc(
        {"onEvent": {"$ref": "#/components/callbacks/NotAnObject"}},
        components=copy.deepcopy(CALLBACK_COMPONENTS),
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert error["expression_pointer"] == (
        "/components/callbacks/NotAnObject/{$request.body#~1callbackUrl}"
    )


def test_openapi_31_callback_ref_siblings_override_and_extend_target():
    doc = make_doc(
        openapi="3.1.0",
        callbacks={
            "onEvent": {
                "$ref": "#/components/callbacks/OnEvent",
                # sibling overrides the component's same-named key
                "$url": {"post": {"responses": {"2XX": {"description": "sibling"}}}},
            }
        },
        components=copy.deepcopy(CALLBACK_COMPONENTS),
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    values = by_expression(resp.json())
    # overridden key keeps the occurrence location of the sibling
    assert values["$url"]["expression_pointer"] == (
        "/paths/~1subscribe/post/callbacks/onEvent/$url"
    )
    # non-overridden key comes from the component
    assert values["{$request.body#/callbackUrl}"]["expression_pointer"] == (
        "/components/callbacks/OnEvent/{$request.body#~1callbackUrl}"
    )
    assert values["$url"]["path_item"] == {
        "post": {"responses": {"2XX": {"description": "sibling"}}}
    }


def test_openapi_30_callback_ref_ignores_siblings():
    doc = make_doc(
        openapi="3.0.3",
        callbacks={
            "onEvent": {
                "$ref": "#/components/callbacks/OnEvent",
                "{$request.body#/sibling}": {
                    "post": {"responses": {"2XX": {"description": "ignored"}}}
                },
            }
        },
        components=copy.deepcopy(CALLBACK_COMPONENTS),
    )
    context = default_context()
    context["request"]["body"]["sibling"] = "https://sibling.example.com"
    resp = resolve(doc, context=context)
    assert resp.status_code == 200, resp.text
    expressions = {item["expression"] for item in resp.json()}
    assert "{$request.body#/sibling}" not in expressions
    assert "{$request.body#/callbackUrl}" in expressions


def test_31_path_level_ref_provides_the_selected_operation():
    doc = make_doc(
        openapi="3.1.0",
        path_item_extra={"$ref": "#/components/pathItems/Subscribe"},
        callbacks={"onEvent": {"https://fixed.example.com": {
            "post": {"responses": {"2XX": {"description": "a"}}}}}},
        components={
            "pathItems": {
                "Subscribe": {
                    "post": {"responses": {"202": {"description": "accepted"}}}
                }
            }
        },
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["url"] == "https://fixed.example.com"


def test_callback_ref_chain_follows_multiple_hops():
    components = {
        "callbacks": {
            "Indirect": {"$ref": "#/components/callbacks/OnEvent"},
            "OnEvent": {
                "$url": {"post": {"responses": {"2XX": {"description": "x"}}}}
            },
        }
    }
    doc = make_doc(
        {"onEvent": {"$ref": "#/components/callbacks/Indirect"}},
        components=components,
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["expression_pointer"] == "/components/callbacks/OnEvent/$url"


# --------------------------------------------------------------------------- #
# Invalid document structures (422 / 400)
# --------------------------------------------------------------------------- #

def test_callback_value_that_is_not_an_object_is_422():
    doc = make_doc(callbacks={"onEvent": "not-an-object"})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "invalid_callback"
    assert error["pointer"] == "/paths/~1subscribe/post/callbacks/onEvent"


def test_expression_value_that_is_not_a_path_item_is_422():
    doc = make_doc({"onEvent": {"https://fixed.example.com": 42}})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "invalid_callback"
    assert error["pointer"].endswith("/https:~1~1fixed.example.com")


def test_callbacks_member_that_is_not_an_object_is_422():
    doc = make_doc()
    doc["paths"]["/subscribe"]["post"]["callbacks"] = ["not", "an", "object"]
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "invalid_callback"
    assert error["pointer"] == "/paths/~1subscribe/post/callbacks"


def test_path_item_that_is_not_an_object_is_422():
    doc = make_doc()
    doc["paths"]["/subscribe"] = "scalar"
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["pointer"] == "/paths/~1subscribe"


def test_bad_document_shape_is_400():
    for bad_doc, message_fragment in [
        ({"openapi": "2.0", "paths": {}}, "3."),
        ({"info": {"title": "x"}}, "openapi"),
    ]:
        resp = resolve(bad_doc, context=default_context())
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid_openapi_document"
        assert message_fragment in str(resp.json()["messages"])


def test_bad_selector_is_400():
    doc = make_doc()
    assert resolve(doc, selector="nonsense", context={}).status_code == 400
    missing_path = resolve(doc, selector="POST /nope", context={})
    assert missing_path.status_code == 400
    assert missing_path.json()["error"] == "invalid_selector"
    missing_method = resolve(doc, selector="DELETE /subscribe", context={})
    assert missing_method.status_code == 400


# --------------------------------------------------------------------------- #
# Malformed context envelope (400)
# --------------------------------------------------------------------------- #

def test_malformed_context_envelope_is_400():
    doc = make_doc({"onEvent": {"https://fixed.example.com": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})

    def post(context):
        return client.post(
            "/callbacks/resolve",
            json={"document": doc, "selector": "POST /subscribe", "context": context},
        )

    assert post("not-object").status_code == 422  # pydantic type validation

    resp = post({"weird": {}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_callback_request"
    assert "context.weird" in resp.json()["messages"][0]

    resp = post({"request": []})
    assert resp.status_code == 400
    assert "context.request" in resp.json()["messages"][0]

    resp = post({"request": {"bogus": 1}})
    assert resp.status_code == 400
    assert "context.request.bogus" in resp.json()["messages"][0]

    resp = post({"response": {"bogus": 1}})
    assert resp.status_code == 400
    assert "context.response.bogus" in resp.json()["messages"][0]

    resp = post({"request": {"url": 5}})
    assert resp.status_code == 400
    assert "context.request.url" in resp.json()["messages"][0]

    resp = post({"request": {"method": 9}})
    assert resp.status_code == 400

    resp = post({"request": {"path": []}})
    assert resp.status_code == 400
    assert "context.request.path" in resp.json()["messages"][0]

    resp = post({"response": {"statusCode": True}})
    assert resp.status_code == 400
    assert "context.response.statusCode" in resp.json()["messages"][0]

    resp = post({"response": {"statusCode": []}})
    assert resp.status_code == 400

    resp = post({"request": {"header": {"X": {"nested": True}}}})
    assert resp.status_code == 400
    assert "context.request.header.X" in resp.json()["messages"][0]

    resp = post({"response": {"header": {"X": ["list"]}}})
    assert resp.status_code == 400
    assert "context.response.header.X" in resp.json()["messages"][0]

    # null sections / fields are simply absent
    resp = post({"request": None, "response": {"header": {"X": None}}})
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# Multi-error, non-mutation and source stability
# --------------------------------------------------------------------------- #

def test_multiple_errors_are_all_reported_and_stably_sorted():
    doc = make_doc({"onEvent": {
        "{$request.path.missing}": {"post": {"responses": {"2XX": {"description": "a"}}}},
        "{$request.query.tags}": {"post": {"responses": {"2XX": {"description": "b"}}}},
        "{$nope}": {"post": {"responses": {"2XX": {"description": "c"}}}},
    }})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert {e["code"] for e in errors} == {
        "missing_value",
        "non_scalar_interpolation",
        "invalid_expression",
    }
    assert errors == sorted(
        errors,
        key=lambda e: (e["code"], e["pointer"], e.get("expression") or "",
                       e.get("ref") or "", e["error"]),
    )


def test_errors_from_different_callbacks_are_all_reported():
    doc = make_doc({
        "first": {"{$request.path.missing}": {
            "post": {"responses": {"2XX": {"description": "a"}}}}},
        "second": {"{$nope}": {
            "post": {"responses": {"2XX": {"description": "b"}}}}},
    })
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    pointers = {e["callback_pointer"] for e in resp.json()["errors"]}
    assert "/paths/~1subscribe/post/callbacks/first" in pointers
    assert "/paths/~1subscribe/post/callbacks/second" in pointers


def test_input_document_and_context_are_not_mutated():
    doc = make_doc(
        {"onEvent": {"$ref": "#/components/callbacks/OnEvent"}},
        components=copy.deepcopy(CALLBACK_COMPONENTS),
    )
    context = default_context()
    doc_snapshot = copy.deepcopy(doc)
    context_snapshot = copy.deepcopy(context)
    resp = resolve(doc, context=context)
    assert resp.status_code == 200, resp.text
    assert doc == doc_snapshot
    assert context == context_snapshot


def test_expression_keys_with_slashes_use_rfc6901_escaped_pointers():
    key = "{$request.body#/callbackUrl}"
    doc = make_doc({"onEvent": {key: {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["expression_pointer"] == (
        "/paths/~1subscribe/post/callbacks/onEvent/{$request.body#~1callbackUrl}"
    )


def test_missing_context_field_defaults_to_empty_context():
    doc = make_doc({"onEvent": {"https://fixed.example.com": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    resp = client.post(
        "/callbacks/resolve",
        json={"document": doc, "selector": "POST /subscribe"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["url"] == "https://fixed.example.com"


def test_whole_response_body_pointer_with_array_index_navigation():
    doc = make_doc({"onEvent": {"{$response.body#/items/0/href}": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    context = {"response": {"body": {"items": [{"href": "https://x.example.com"}]}}}
    resp = resolve(doc, context=context)
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["url"] == "https://x.example.com"


def test_bare_complete_expression_key_without_braces_is_standalone():
    doc = make_doc({"onEvent": {"$request.body#/callbackUrl": {
        "post": {"responses": {"2XX": {"description": "a"}}}}}})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["url"] == "https://my.host/hook"


def test_template_with_one_missing_of_several_expressions_is_422():
    doc = make_doc({"onEvent": {
        "https://h/{$request.path.txId}/{$request.path.missing}": {
            "post": {"responses": {"2XX": {"description": "a"}}}}}})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "missing_value"
    assert error["expression"] == "$request.path.missing"
    assert error["context_pointer"] == "/context/request/path/missing"


def test_external_operation_path_item_ref_is_422():
    doc = make_doc(
        openapi="3.1.0",
        path_item_extra={"$ref": "https://example.com/api.yaml#/paths/~1subscribe"},
        callbacks={"onEvent": {"https://fixed.example.com": {
            "post": {"responses": {"2XX": {"description": "a"}}}}}},
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert error["pointer"] == "/paths/~1subscribe"
    assert error["ref"].startswith("https://")


def test_circular_operation_path_item_ref_is_422():
    doc = make_doc(
        openapi="3.1.0",
        path_item_extra={"$ref": "#/components/pathItems/LoopA"},
        callbacks={"onEvent": {"https://fixed.example.com": {
            "post": {"responses": {"2XX": {"description": "a"}}}}}},
        components={"pathItems": {
            "LoopA": {"$ref": "#/components/pathItems/LoopB"},
            "LoopB": {"$ref": "#/components/pathItems/LoopA"},
        }},
    )
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert "circular reference detected" in error["error"]


def test_root_reference_hash_is_422_not_treated_as_path_item():
    doc = make_doc({"onEvent": {"https://fixed.example.com": {"$ref": "#"}}})
    resp = resolve(doc, context=default_context())
    assert resp.status_code == 422
    error = resp.json()["errors"][0]
    assert error["code"] == "unresolvable_reference"
    assert error["ref"] == "#"
    assert "document root" in error["error"]

