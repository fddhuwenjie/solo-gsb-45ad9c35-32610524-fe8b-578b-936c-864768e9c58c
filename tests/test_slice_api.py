"""Regression tests for ``POST /slice`` (minimal operation sub-contract)."""

from __future__ import annotations

import copy
import hashlib
import json

from fastapi.testclient import TestClient

from app.main import app
from app.slicer import canonical_json, parse_selector

client = TestClient(app)


def make_doc(paths=None, **extra) -> dict:
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "Pet API", "version": "1.0"},
        "paths": paths or {},
    }
    doc.update(extra)
    return doc


def slice_(doc, selectors):
    return client.post("/slice", json={"document": doc, "selectors": selectors})


def assert_canonical(contract) -> None:
    """Every dict in *contract* must serialize with keys in sorted order."""
    if isinstance(contract, dict):
        assert list(contract) == sorted(contract), list(contract)
        for value in contract.values():
            assert_canonical(value)
    elif isinstance(contract, list):
        for value in contract:
            assert_canonical(value)


# --------------------------------------------------------------------------- #
# Basic skeleton semantics
# --------------------------------------------------------------------------- #

def test_slice_keeps_openapi_info_selected_operation_and_path_fields():
    doc = make_doc(
        servers=[{"url": "https://api.example.com"}],
        tags=[{"name": "pets"}],
        paths={
            "/pets": {
                "summary": "pets collection",
                "description": "shared",
                "servers": [{"url": "https://eu.example.com"}],
                "parameters": [
                    {"name": "x-tenant", "in": "header",
                     "schema": {"type": "string"}}
                ],
                "get": {"operationId": "list", "responses": {"200": {"description": "ok"}}},
                "post": {"operationId": "create", "responses": {"201": {"description": "c"}}},
            }
        },
    )
    resp = slice_(doc, ["GET /pets"])
    assert resp.status_code == 200, resp.text
    contract = resp.json()["contract"]

    assert set(contract) == {"openapi", "info", "paths"}
    assert contract["openapi"] == "3.0.3"
    assert contract["info"] == {"title": "Pet API", "version": "1.0"}

    item = contract["paths"]["/pets"]
    assert set(item) == {
        "get", "summary", "description", "servers", "parameters"
    }
    assert item["get"]["operationId"] == "list"
    # The unselected sibling method must never sneak in.
    assert "post" not in item
    assert "delete" not in item
    assert "servers" not in contract  # root-level non-OpenAPI-keep fields dropped
    assert "tags" not in contract
    assert_canonical(contract)


def test_slice_selecting_two_methods_keeps_both_but_not_third():
    doc = make_doc(paths={
        "/pets": {
            "get": {"responses": {"200": {"description": "ok"}}},
            "post": {"responses": {"201": {"description": "c"}}},
            "delete": {"responses": {"204": {"description": "gone"}}},
        }
    })
    resp = slice_(doc, ["GET /pets", "post /pets"])
    assert resp.status_code == 200
    item = resp.json()["contract"]["paths"]["/pets"]
    assert set(item) == {"get", "post"}


def test_slice_method_matching_is_case_insensitive():
    doc = make_doc(paths={"/pets": {"GeT": {"responses": {"200": {"description": "ok"}}}}})
    resp = slice_(doc, ["get /pets"])
    assert resp.status_code == 200
    # Normalized to lowercase in output.
    assert "get" in resp.json()["contract"]["paths"]["/pets"]


def test_slice_multiple_paths_are_independent():
    doc = make_doc(paths={
        "/pets": {"get": {"responses": {"200": {"description": "ok"}}}},
        "/toys": {
            "get": {"responses": {"200": {"description": "ok"}}},
            "post": {"responses": {"201": {"description": "c"}}},
        },
    })
    resp = slice_(doc, ["GET /pets", "POST /toys"])
    paths = resp.json()["contract"]["paths"]
    assert set(paths) == {"/pets", "/toys"}
    assert set(paths["/toys"]) == {"post"}


def test_ref_into_path_with_escaped_slash_does_not_smuggle_siblings():
    # Path keys containing "/" are escaped as "~1" inside JSON Pointers;
    # closure reconstruction must still refuse to rebuild an unselected
    # sibling method of a retained path (here: keep get, drop post).
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/a/b": {
                "get": {
                    "responses": {"200": {
                        "description": "ok",
                        "content": {"application/json": {"schema": {
                            "$ref": "#/x-weird/op"}}}}},
                },
                "post": {"responses": {"201": {"description": "c"}}},
            }
        },
        "x-weird": {"op": {"type": "string"}},
    }
    resp = slice_(doc, ["GET /a/b"])
    assert resp.status_code == 200
    item = resp.json()["contract"]["paths"]["/a/b"]
    assert set(item) == {"get"}
    assert resp.json()["contract"]["x-weird"]["op"] == {"type": "string"}


# --------------------------------------------------------------------------- #
# Selector / document validation (400)
# --------------------------------------------------------------------------- #

def test_duplicate_selector_is_400():
    doc = make_doc(paths={"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}})
    resp = slice_(doc, ["GET /pets", "get /pets"])
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_selector"
    assert any("duplicate selector" in m for m in body["messages"])


def test_unknown_operation_is_400():
    doc = make_doc(paths={"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}})
    resp = slice_(doc, ["DELETE /pets"])
    assert resp.status_code == 400
    assert "operation is not declared" in resp.json()["messages"][0]


def test_unknown_path_is_400():
    doc = make_doc(paths={"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}})
    resp = slice_(doc, ["GET /nope"])
    assert resp.status_code == 400
    assert "path is not declared" in resp.json()["messages"][0]


def test_malformed_selectors_are_400():
    doc = make_doc(paths={"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}})
    for bad in ["GET/pets", "garbage", "GET", ""]:
        resp = slice_(doc, [bad])
        assert resp.status_code == 400, (bad, resp.text)
        assert resp.json()["error"] == "invalid_selector"
    # Empty list is also a 400.
    resp = slice_(doc, [])
    assert resp.status_code == 400


def test_non_openapi_document_is_400():
    resp = client.post(
        "/slice",
        json={"document": {"swagger": "2.0", "paths": {}}, "selectors": ["GET /x"]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_openapi_document"


def test_selector_errors_are_reported_deterministically():
    doc = make_doc(paths={"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}})
    a = slice_(doc, ["DELETE /pets", "GET /missing"]).json()["messages"]
    b = slice_(doc, ["GET /missing", "DELETE /pets"]).json()["messages"]
    assert a == b


# --------------------------------------------------------------------------- #
# $ref closure
# --------------------------------------------------------------------------- #

PETSTORE = {
    "openapi": "3.0.3",
    "info": {"title": "Pet API", "version": "1.0"},
    "paths": {
        "/pets": {
            "parameters": [
                {"name": "tenant", "in": "header",
                 "schema": {"$ref": "#/components/schemas/Tenant"}}
            ],
            "get": {
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Pets"}
                            }
                        },
                    }
                },
            },
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/NewPet"}
                        }
                    }
                },
                "responses": {"201": {"description": "created"}},
            },
        },
        "/toys": {
            "get": {
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/UnusedToy"}
                            }
                        },
                    }
                }
            }
        },
    },
    "components": {
        "schemas": {
            "Tenant": {"type": "string"},
            "Pets": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/Pet"},
            },
            "Pet": {
                "type": "object",
                "properties": {
                    "id": {"$ref": "#/components/schemas/Id"},
                    "parent": {"$ref": "#/components/schemas/Pet"},
                },
            },
            "Id": {"type": "integer"},
            "NewPet": {"type": "object"},
            "UnusedToy": {"type": "string"},
        },
        "parameters": {"Trace": {"name": "x-trace", "in": "header"}},
    },
}


def test_closure_copies_transitive_dependencies_at_original_pointers():
    resp = slice_(PETSTORE, ["GET /pets"])
    assert resp.status_code == 200, resp.text
    schemas = resp.json()["contract"]["components"]["schemas"]
    # Directly referenced (path-level parameter + response).
    assert schemas["Tenant"] == {"type": "string"}
    assert schemas["Pets"]["type"] == "array"
    # Transitive: Pets -> Pet -> Id.
    assert schemas["Pet"]["properties"]["id"] == {"$ref": "#/components/schemas/Id"}
    assert schemas["Id"] == {"type": "integer"}
    # Unselected branches are not part of the closure.
    assert "NewPet" not in schemas
    assert "UnusedToy" not in schemas
    # components/parameters was not needed at all.
    assert "parameters" not in resp.json()["contract"]["components"]


def test_closure_cycle_target_is_copied_once():
    resp = slice_(PETSTORE, ["GET /pets"])
    schemas = resp.json()["contract"]["components"]["schemas"]
    # Pet references itself via "parent"; the node exists once and its
    # internal reference still points at the same rebuilt location.
    assert schemas["Pet"]["properties"]["parent"] == {
        "$ref": "#/components/schemas/Pet"
    }
    assert list(schemas).count("Pet") == 1


def test_closure_union_across_selected_operations():
    resp = slice_(PETSTORE, ["GET /pets", "POST /pets"])
    schemas = resp.json()["contract"]["components"]["schemas"]
    assert {"Tenant", "Pets", "Pet", "Id", "NewPet"} <= set(schemas)
    assert "UnusedToy" not in schemas


def test_reachable_dangling_reference_is_422():
    doc = make_doc(paths={
        "/a": {"get": {"responses": {"200": {
            "description": "ok",
            "content": {"application/json": {"schema": {
                "$ref": "#/components/schemas/Gone"}}}}}}},
    })
    resp = slice_(doc, ["GET /a"])
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unresolvable_reference"
    assert body["references"][0]["ref"].endswith("schemas/Gone")


def test_reachable_non_local_reference_is_422():
    doc = make_doc(paths={
        "/a": {"get": {"responses": {"200": {
            "description": "ok",
            "content": {"application/json": {"schema": {
                "$ref": "https://example.com/schemas/Pet.json"}}}}}}},
    })
    resp = slice_(doc, ["GET /a"])
    assert resp.status_code == 422
    assert "non-local" in resp.json()["references"][0]["error"]


def test_bad_reference_in_unselected_operation_does_not_block():
    doc = make_doc(paths={
        "/pets": {
            "get": {"responses": {"200": {"description": "ok"}}},
            "post": {"responses": {"201": {
                "description": "c",
                "content": {"application/json": {"schema": {
                    "$ref": "#/components/schemas/Gone"}}}}}},
        },
        "/broken": {"get": {"responses": {"200": {
            "description": "ok",
            "content": {"application/json": {"schema": {
                "$ref": "http://example.com/x.json"}}}}}}},
    })
    resp = slice_(doc, ["GET /pets"])
    assert resp.status_code == 200
    assert "components" not in resp.json()["contract"]


def test_bad_reference_in_unselected_schema_does_not_block():
    # The kept Pet references a healthy Id; a sibling unused schema points
    # at a dangling target and must not be walked.
    doc = copy.deepcopy(PETSTORE)
    doc["components"]["schemas"]["UnusedToy"] = {
        "$ref": "#/components/schemas/DoesNotExist"
    }
    resp = slice_(doc, ["GET /pets"])
    assert resp.status_code == 200
    schemas = resp.json()["contract"]["components"]["schemas"]
    assert "UnusedToy" not in schemas


def test_pure_ref_chain_cycle_is_copied_once_and_resolves():
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"get": {"responses": {"200": {
            "description": "ok",
            "content": {"application/json": {"schema": {
                "$ref": "#/components/schemas/A"}}}}}}}},
        "components": {"schemas": {
            "A": {"$ref": "#/components/schemas/B"},
            "B": {"$ref": "#/components/schemas/A"},
        }},
    }
    resp = slice_(doc, ["GET /a"])
    assert resp.status_code == 200
    schemas = resp.json()["contract"]["components"]["schemas"]
    assert schemas == {
        "A": {"$ref": "#/components/schemas/B"},
        "B": {"$ref": "#/components/schemas/A"},
    }


def test_openapi_31_path_item_ref_is_inlined():
    doc = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "$ref": "#/components/pathItems/Item",
                "parameters": [{"name": "tid", "in": "header"}],
            }
        },
        "components": {"pathItems": {
            "Item": {
                "summary": "from ref",
                "get": {"responses": {"200": {"description": "ok"}}},
                "post": {"responses": {"201": {"description": "c"}}},
            }
        }},
    }
    resp = slice_(doc, ["GET /x"])
    assert resp.status_code == 200
    item = resp.json()["contract"]["paths"]["/x"]
    assert "get" in item
    assert "post" not in item
    assert item["summary"] == "from ref"
    assert item["parameters"][0]["name"] == "tid"
    assert "$ref" not in item


def test_dangling_path_item_ref_is_422_not_unknown_operation():
    doc = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/x": {"$ref": "#/components/pathItems/Missing"}},
    }
    resp = slice_(doc, ["GET /x"])
    assert resp.status_code == 422
    assert resp.json()["references"][0]["ref"].endswith("pathItems/Missing")


def test_ref_in_sibling_of_31_ref_is_kept():
    # OpenAPI 3.1 allows sibling annotations next to $ref inside schemas.
    doc = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {"/a": {"get": {"responses": {"200": {
            "description": "ok",
            "content": {"application/json": {"schema": {
                "$ref": "#/components/schemas/A",
                "description": "wrapper",
            }}}}}}}},
        "components": {"schemas": {
            "A": {"type": "object", "properties": {"b": {
                "$ref": "#/components/schemas/B", "title": "b"}}},
            "B": {"type": "string"},
        }},
    }
    resp = slice_(doc, ["GET /a"])
    assert resp.status_code == 200
    schemas = resp.json()["contract"]["components"]["schemas"]
    assert schemas["B"] == {"type": "string"}
    assert schemas["A"]["properties"]["b"]["title"] == "b"


# --------------------------------------------------------------------------- #
# Security inheritance + securitySchemes filtering
# --------------------------------------------------------------------------- #

def _security_doc():
    return {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "security": [{"oauth": ["pets:read"]}, {"apikey": []}],
        "paths": {
            "/pets": {
                "get": {"operationId": "inherit",
                        "responses": {"200": {"description": "ok"}}},
                "post": {
                    "operationId": "override",
                    "security": [{"basic": []}],
                    "responses": {"201": {"description": "c"}},
                },
                "delete": {
                    "operationId": "disabled",
                    "security": [],
                    "responses": {"204": {"description": "gone"}},
                },
            }
        },
        "components": {"securitySchemes": {
            "oauth": {"type": "oauth2", "flows": {}},
            "apikey": {"type": "apiKey", "name": "k", "in": "header"},
            "basic": {"type": "http", "scheme": "basic"},
            "unused": {"type": "http", "scheme": "bearer"},
        }},
    }


def test_security_inheritance_and_scheme_filtering():
    resp = slice_(_security_doc(), ["GET /pets", "POST /pets", "DELETE /pets"])
    assert resp.status_code == 200
    contract = resp.json()["contract"]
    item = contract["paths"]["/pets"]

    assert item["get"]["security"] == [
        {"oauth": ["pets:read"]}, {"apikey": []}
    ]
    assert item["post"]["security"] == [{"basic": []}]
    assert item["delete"]["security"] == []

    schemes = contract["components"]["securitySchemes"]
    assert set(schemes) == {"oauth", "apikey", "basic"}
    assert "unused" not in schemes
    # Root security is not carried as a document-wide override.
    assert "security" not in contract


def test_security_schemes_section_omitted_when_unused():
    doc = _security_doc()
    resp = slice_(doc, ["DELETE /pets"])
    assert resp.status_code == 200
    contract = resp.json()["contract"]
    assert "components" not in contract


def test_operation_without_root_security_has_no_security_key():
    doc = make_doc(paths={
        "/pets": {"get": {"responses": {"200": {"description": "ok"}}}}
    })
    resp = slice_(doc, ["GET /pets"])
    assert "security" not in resp.json()["contract"]["paths"]["/pets"]["get"]


# --------------------------------------------------------------------------- #
# SHA-256 + deterministic ordering
# --------------------------------------------------------------------------- #

def test_sha256_matches_canonical_encoding():
    resp = slice_(PETSTORE, ["GET /pets"])
    body = resp.json()
    expected = hashlib.sha256(canonical_json(body["contract"])).hexdigest()
    assert body["sha256"] == expected
    assert len(body["sha256"]) == 64


def test_input_key_order_does_not_change_contract_or_hash():
    def reorder(obj):
        if isinstance(obj, dict):
            return {k: reorder(obj[k]) for k in reversed(list(obj))}
        if isinstance(obj, list):
            return [reorder(v) for v in obj]
        return obj

    resp1 = slice_(PETSTORE, ["GET /pets", "POST /pets"])
    resp2 = slice_(reorder(copy.deepcopy(PETSTORE)), ["post /pets", "GET /pets"])
    assert resp1.status_code == resp2.status_code == 200
    assert resp1.json()["sha256"] == resp2.json()["sha256"]
    assert canonical_json(resp1.json()["contract"]) == canonical_json(
        resp2.json()["contract"]
    )
    assert_canonical(resp2.json()["contract"])


def test_selectors_affect_content_but_hash_is_stable_per_selection_set():
    a = slice_(PETSTORE, ["GET /pets"]).json()
    b = slice_(PETSTORE, ["GET /pets"]).json()
    c = slice_(PETSTORE, ["POST /pets"]).json()
    assert a["sha256"] == b["sha256"]
    assert a["sha256"] != c["sha256"]


def test_response_envelope_key_order():
    resp = slice_(PETSTORE, ["GET /pets"])
    # Response body itself is serialized with sorted keys too.
    assert list(resp.json()) == ["contract", "sha256"]


# --------------------------------------------------------------------------- #
# Selector parsing unit coverage
# --------------------------------------------------------------------------- #

def test_parse_selector_variants():
    assert parse_selector("GET /pets/{id}") == ("get", "/pets/{id}")
    assert parse_selector("  post   /pets  ") == ("post", "/pets")
    assert parse_selector("GET") is None
    assert parse_selector("pets /get") is None  # path must start with "/"
    assert parse_selector("") is None
    assert parse_selector(7) is None


# --------------------------------------------------------------------------- #
# Existing interface stays intact
# --------------------------------------------------------------------------- #

def test_audit_and_root_endpoints_still_work():
    assert client.get("/health").json() == {"status": "ok"}
    root = client.get("/").json()
    assert root["audit"] == "POST /audit"
    assert root["slice"] == "POST /slice"
    resp = client.post(
        "/audit",
        json={
            "baseline": make_doc(paths={
                "/pets": {"get": {"responses": {"200": {"description": "ok"}}}}}),
            "candidate": make_doc(paths={"/pets": {}}),
        },
    )
    assert resp.status_code == 200
    assert resp.json()["compatible"] is False
