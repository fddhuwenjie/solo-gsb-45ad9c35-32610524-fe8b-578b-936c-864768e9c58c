"""End-to-end tests for the audit API and the reference resolver."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.refresolver import (
    CircularReferenceError,
    RefResolver,
    UnresolvableReferenceError,
)

client = TestClient(app)


def make_doc(paths=None, components=None) -> dict:
    return {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": paths or {},
        **({"components": components} if components else {}),
    }


OP = {
    "get": {
        "summary": "list",
        "responses": {"200": {"description": "ok"}},
    }
}


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #

def test_resolver_follows_local_ref():
    doc = {"components": {"schemas": {"Pet": {"type": "object"}}}}
    node = {"$ref": "#/components/schemas/Pet"}
    target, pointer = RefResolver(doc).resolve(node)
    assert target == {"type": "object"}
    assert pointer == "/components/schemas/Pet"


def test_resolver_root_ref():
    doc = {"openapi": "3.0.0", "paths": {}}
    target, pointer = RefResolver(doc).resolve({"$ref": "#"})
    assert target is doc
    assert pointer == ""


def test_resolver_missing_pointer():
    doc = {"components": {}}
    with pytest.raises(UnresolvableReferenceError) as exc:
        RefResolver(doc).resolve({"$ref": "#/components/schemas/Missing"})
    assert "no property 'schemas'" in str(exc.value)


def test_resolver_array_index_out_of_range():
    doc = {"a": [1]}
    with pytest.raises(UnresolvableReferenceError):
        RefResolver(doc).resolve({"$ref": "#/a/5"})


def test_resolver_rejects_remote_ref():
    with pytest.raises(UnresolvableReferenceError) as exc:
        RefResolver({}).resolve(
            {"$ref": "https://example.com/schemas/Pet.json"}
        )
    assert "non-local reference" in str(exc.value)


def test_resolver_escaped_pointer_tokens():
    doc = {"a~b": {"c/d": 7}}
    target, _ = RefResolver(doc).resolve({"$ref": "#/a~0b/c~1d"})
    assert target == 7


def test_resolver_self_cycle_raises():
    doc = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {"self": {"$ref": "#/components/schemas/Node"}},
                }
            }
        }
    }
    # A direct $ref -> $ref cycle (rare; recursive property is the normal
    # shape) is detected explicitly.
    cyclic_doc = {
        "components": {
            "schemas": {
                "A": {"$ref": "#/components/schemas/B"},
                "B": {"$ref": "#/components/schemas/A"},
            }
        }
    }
    with pytest.raises(CircularReferenceError):
        RefResolver(cyclic_doc).resolve(cyclic_doc["components"]["schemas"]["A"])
    # The recursive-property shape resolves one level without problems.
    target, pointer = RefResolver(doc).resolve(
        doc["components"]["schemas"]["Node"]["properties"]["self"]
    )
    assert pointer == "/components/schemas/Node"
    assert target["type"] == "object"


# --------------------------------------------------------------------------- #
# API: general
# --------------------------------------------------------------------------- #

def test_health_and_root():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").json()["audit"] == "POST /audit"


def test_rejects_non_openapi():
    resp = client.post(
        "/audit", json={"baseline": {"swagger": "2.0"}, "candidate": make_doc()}
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_openapi_document"


def test_unresolvable_reference_returns_422():
    baseline = make_doc(
        components={"schemas": {"X": {"$ref": "#/components/schemas/Gone"}}}
    )
    candidate = make_doc()
    resp = client.post(
        "/audit", json={"baseline": baseline, "candidate": candidate}
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unresolvable_reference"
    assert body["references"][0]["document"] == "baseline"
    assert body["references"][0]["ref"] == "#/components/schemas/Gone"


def test_remote_reference_returns_422():
    baseline = make_doc(
        components={"schemas": {"X": {"$ref": "http://x/y.json"}}}
    )
    resp = client.post(
        "/audit", json={"baseline": baseline, "candidate": make_doc()}
    )
    assert resp.status_code == 422
    assert "non-local" in resp.json()["references"][0]["error"]


# --------------------------------------------------------------------------- #
# API: operation/parameter changes
# --------------------------------------------------------------------------- #

def categories(payload):
    resp = client.post("/audit", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {f["category"]: f for f in body["findings"]}, body


def test_operation_removed_is_breaking():
    baseline = make_doc(paths={"/pets": dict(OP)})
    candidate = make_doc(paths={"/pets": {}})
    findings, body = categories(
        {"baseline": baseline, "candidate": candidate}
    )
    assert findings["operation-removed"]["severity"] == "breaking"
    assert findings["operation-removed"]["pointer"] == "/paths/~1pets/get"
    assert body["compatible"] is False


def test_operation_added_is_info():
    baseline = make_doc(paths={"/pets": {}})
    candidate = make_doc(paths={"/pets": dict(OP)})
    findings, body = categories(
        {"baseline": baseline, "candidate": candidate}
    )
    assert findings["operation-added"]["severity"] == "info"
    assert body["compatible"] is True


def test_new_required_query_parameter_is_breaking():
    def doc(required):
        return make_doc(
            paths={
                "/pets": {
                    "get": {
                        "parameters": [
                            {
                                "name": "limit",
                                "in": "query",
                                "required": required,
                                "schema": {"type": "integer"},
                            }
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )

    findings, body = categories(
        {"baseline": doc(False), "candidate": doc(True)}
    )
    assert findings["parameter-required-added"]["severity"] == "breaking"
    assert body["compatible"] is False


def test_new_optional_parameter_is_compatible():
    baseline = make_doc(paths={"/pets": dict(OP)})
    candidate = make_doc(
        paths={
            "/pets": {
                "get": {
                    "parameters": [
                        {"name": "q", "in": "query", "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )
    findings, body = categories(
        {"baseline": baseline, "candidate": candidate}
    )
    assert findings["parameter-added"]["severity"] == "info"
    assert body["compatible"] is True


# --------------------------------------------------------------------------- #
# API: schema changes
# --------------------------------------------------------------------------- #

def pet_schema_doc(properties, required=None):
    return make_doc(
        paths={
            "/pets": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Pet"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "created"}},
                }
            }
        },
        components={
            "schemas": {
                "Pet": {
                    "type": "object",
                    "properties": properties,
                    **({"required": required} if required else {}),
                }
            }
        },
    )


def test_request_required_property_added_is_breaking():
    baseline = pet_schema_doc(
        {"name": {"type": "string"}}, required=["name"]
    )
    candidate = pet_schema_doc(
        {"name": {"type": "string"}, "tag": {"type": "string"}},
        required=["name", "tag"],
    )
    findings, body = categories(
        {"baseline": baseline, "candidate": candidate}
    )
    assert findings["required-added"]["severity"] == "breaking"
    # Pointer follows the $ref to the component definition.
    assert findings["required-added"]["pointer"] == "/components/schemas/Pet"
    assert body["compatible"] is False


def test_response_property_removed_is_breaking():
    def doc(fields):
        return make_doc(
            paths={
                "/pets": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": fields,
                                        }
                                    }
                                },
                            }
                        }
                    }
                }
            }
        )

    baseline = doc({"id": {"type": "integer"}, "name": {"type": "string"}})
    candidate = doc({"id": {"type": "integer"}})
    findings, body = categories(
        {"baseline": baseline, "candidate": candidate}
    )
    assert findings["property-removed"]["severity"] == "breaking"
    assert "name" in findings["property-removed"]["reason"]


def test_request_type_change_is_breaking():
    baseline = pet_schema_doc({"age": {"type": "integer"}}, required=["age"])
    candidate = pet_schema_doc({"age": {"type": "string"}}, required=["age"])
    findings, _ = categories({"baseline": baseline, "candidate": candidate})
    assert findings["type-changed"]["severity"] == "breaking"
    assert "integer" in findings["type-changed"]["reason"]


def test_request_enum_narrowing_is_breaking_widening_is_info():
    baseline = pet_schema_doc(
        {"status": {"type": "string", "enum": ["a", "b", "c"]}},
        required=["status"],
    )
    candidate = pet_schema_doc(
        {"status": {"type": "string", "enum": ["a"]}},
        required=["status"],
    )
    findings, body = categories(
        {"baseline": baseline, "candidate": candidate}
    )
    breaking = [f for f in body["findings"] if f["category"] == "enum-changed" and f["severity"] == "breaking"]
    info = [f for f in body["findings"] if f["category"] == "enum-changed" and f["severity"] == "info"]
    assert breaking and "b" in breaking[0]["reason"]
    assert not info  # pure narrowing


def test_response_enum_widening_is_breaking():
    def doc(values):
        return make_doc(
            paths={
                "/pets": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "kind": {
                                                    "type": "string",
                                                    "enum": values,
                                                }
                                            },
                                        }
                                    }
                                },
                            }
                        }
                    }
                }
            }
        )

    findings, body = categories(
        {"baseline": doc(["x"]), "candidate": doc(["x", "y"])}
    )
    breaking = [f for f in body["findings"] if f["category"] == "enum-changed" and f["severity"] == "breaking"]
    assert breaking and "y" in breaking[0]["reason"]


def test_response_required_relaxed_is_breaking():
    def doc(required):
        return make_doc(
            paths={
                "/pets": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {"id": {"type": "integer"}},
                                            "required": required,
                                        }
                                    }
                                },
                            }
                        }
                    }
                }
            }
        )

    findings, _ = categories(
        {"baseline": doc(["id"]), "candidate": doc([])}
    )
    assert findings["required-removed"]["severity"] == "breaking"


# --------------------------------------------------------------------------- #
# Response / request body / media types
# --------------------------------------------------------------------------- #

def test_response_status_removed_is_breaking():
    baseline = make_doc(
        paths={
            "/pets": {
                "get": {
                    "responses": {
                        "200": {"description": "ok"},
                        "404": {"description": "missing"},
                    }
                }
            }
        }
    )
    candidate = make_doc(
        paths={"/pets": {"get": {"responses": {"200": {"description": "ok"}}}}}
    )
    findings, _ = categories(
        {"baseline": baseline, "candidate": candidate}
    )
    assert findings["response-removed"]["severity"] == "breaking"
    assert "404" in findings["response-removed"]["reason"]


def test_request_body_added_required_is_breaking():
    baseline = make_doc(paths={"/pets": {"post": {"responses": {"201": {"description": "ok"}}}}})
    candidate = make_doc(
        paths={
            "/pets": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"type": "object"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "ok"}},
                }
            }
        }
    )
    findings, _ = categories(
        {"baseline": baseline, "candidate": candidate}
    )
    assert findings["request-body-added"]["severity"] == "breaking"


def test_response_media_type_removed_is_breaking():
    def doc(types):
        content = {t: {"schema": {"type": "object"}} for t in types}
        return make_doc(
            paths={
                "/pets": {
                    "get": {
                        "responses": {
                            "200": {"description": "ok", "content": content}
                        }
                    }
                }
            }
        )

    findings, _ = categories(
        {
            "baseline": doc(["application/json", "application/xml"]),
            "candidate": doc(["application/json"]),
        }
    )
    assert findings["media-type-removed"]["severity"] == "breaking"
    assert "application/xml" in findings["media-type-removed"]["reason"]


# --------------------------------------------------------------------------- #
# Cycles, refs and ordering
# --------------------------------------------------------------------------- #

def test_recursive_schema_does_not_run_away():
    recursive = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/nodes": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Node"}
                                }
                            },
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "children": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Node"},
                        },
                    },
                }
            }
        },
    }
    # Same document on both sides: cycle-safe traversal, compatible result.
    resp = client.post("/audit", json={"baseline": recursive, "candidate": recursive})
    assert resp.status_code == 200
    assert resp.json()["compatible"] is True
    assert resp.json()["breaking_count"] == 0


def test_findings_stably_sorted_by_pointer():
    def doc(status_type):
        return make_doc(
            paths={
                "/a": {"get": {"responses": {"200": {"description": "ok"}}}},
                "/b": {
                    "get": {
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {"f": {"type": status_type}},
                                        }
                                    }
                                },
                            }
                        }
                    }
                },
            }
        )

    resp = client.post(
        "/audit", json={"baseline": doc("integer"), "candidate": doc("string")}
    )
    pointers = [f["pointer"] for f in resp.json()["findings"]]
    assert pointers == sorted(pointers)


def test_request_property_removed_is_compatible_info():
    baseline = pet_schema_doc(
        {"name": {"type": "string"}, "legacy": {"type": "string"}},
        required=["name"],
    )
    candidate = pet_schema_doc(
        {"name": {"type": "string"}}, required=["name"]
    )
    findings, body = categories(
        {"baseline": baseline, "candidate": candidate}
    )
    removed = findings["property-removed"]
    assert removed["severity"] == "info"
    assert "legacy" in removed["reason"]
    assert body["compatible"] is True


def test_path_level_parameters_considered():
    def doc(with_param):
        params = (
            [{"name": "x-tenant", "in": "header", "required": True,
              "schema": {"type": "string"}}]
            if with_param
            else []
        )
        return make_doc(
            paths={
                "/pets": {
                    "parameters": params,
                    "get": {"responses": {"200": {"description": "ok"}}},
                }
            }
        )

    findings, body = categories(
        {"baseline": doc(False), "candidate": doc(True)}
    )
    assert findings["parameter-added"]["severity"] == "breaking"
    assert body["compatible"] is False


# --------------------------------------------------------------------------- #
# Schema value-set semantics: request constraints must not tighten, response
# value sets must not widen. Every rule below is tested in both directions.
# --------------------------------------------------------------------------- #

def schema_doc(side, schema, openapi_version="3.0.3", property_name="f"):
    """A document carrying one schema either in a request body or a response."""
    wrapped_schema = {"type": "object", "properties": {property_name: schema}}
    if side == "request":
        path = {
            "/items": {
                "post": {
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": wrapped_schema}
                        },
                    },
                    "responses": {"201": {"description": "created"}},
                }
            }
        }
    else:
        path = {
            "/items": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {"schema": wrapped_schema}
                            },
                        }
                    }
                }
            }
        }
    doc = make_doc(paths=path)
    doc["openapi"] = openapi_version
    return doc


def audit_schema_change(side, base_schema, cand_schema, version="3.0.3"):
    payload = {
        "baseline": schema_doc(side, base_schema, version),
        "candidate": schema_doc(side, cand_schema, version),
    }
    return categories(payload)


def severities_for(findings_dict, category):
    return sorted(
        f["severity"]
        for f in findings_dict.values()
        if f["category"] == category
    )


# --- integer / number containment ---------------------------------------- #

def test_request_integer_to_number_widening_is_info():
    # Baseline integer, candidate number: every previously sent integer is
    # still accepted, so the constraint was loosened (compatible).
    findings, body = audit_schema_change(
        "request", {"type": "integer"}, {"type": "number"}
    )
    assert severities_for(findings, "type-changed") == ["info"]
    assert body["compatible"] is True


def test_request_number_to_integer_narrowing_is_breaking():
    findings, body = audit_schema_change(
        "request", {"type": "number"}, {"type": "integer"}
    )
    assert severities_for(findings, "type-changed") == ["breaking"]
    assert body["compatible"] is False


def test_response_integer_to_number_widening_is_breaking():
    # Response clients were promised integers; a fractional number may now
    # arrive and break consumers that cannot handle it.
    findings, body = audit_schema_change(
        "response", {"type": "integer"}, {"type": "number"}
    )
    assert severities_for(findings, "type-changed") == ["breaking"]
    assert body["compatible"] is False


def test_response_number_to_integer_narrowing_is_info():
    findings, body = audit_schema_change(
        "response", {"type": "number"}, {"type": "integer"}
    )
    assert severities_for(findings, "type-changed") == ["info"]
    assert body["compatible"] is True


# --- enum introduced / removed (absent enum == unbounded, not empty) ------ #

def test_request_enum_introduced_is_breaking():
    findings, body = audit_schema_change(
        "request", {"type": "string"}, {"type": "string", "enum": ["a"]}
    )
    assert severities_for(findings, "enum-changed") == ["breaking"]
    assert "introduced" in findings["enum-changed"]["reason"]
    assert body["compatible"] is False


def test_request_enum_removed_is_info():
    findings, body = audit_schema_change(
        "request", {"type": "string", "enum": ["a"]}, {"type": "string"}
    )
    assert severities_for(findings, "enum-changed") == ["info"]
    assert body["compatible"] is True


def test_response_enum_introduced_is_info():
    # A safe narrowing of the produced value set.
    findings, body = audit_schema_change(
        "response", {"type": "string"}, {"type": "string", "enum": ["a"]}
    )
    assert severities_for(findings, "enum-changed") == ["info"]
    assert body["compatible"] is True


def test_response_enum_removed_is_breaking():
    # Unconstrained responses may now carry previously unadvertised values.
    findings, body = audit_schema_change(
        "response", {"type": "string", "enum": ["a"]}, {"type": "string"}
    )
    assert severities_for(findings, "enum-changed") == ["breaking"]
    assert "removed" in findings["enum-changed"]["reason"]
    assert body["compatible"] is False


# --- enum value-level widening / narrowing (pairwise) --------------------- #

def test_request_enum_widened_is_info_and_narrowed_is_breaking():
    findings_wide, body_wide = audit_schema_change(
        "request", {"type": "string", "enum": ["a"]},
        {"type": "string", "enum": ["a", "b"]},
    )
    assert severities_for(findings_wide, "enum-changed") == ["info"]
    assert body_wide["compatible"] is True

    findings_narrow, body_narrow = audit_schema_change(
        "request", {"type": "string", "enum": ["a", "b"]},
        {"type": "string", "enum": ["a"]},
    )
    assert severities_for(findings_narrow, "enum-changed") == ["breaking"]
    assert body_narrow["compatible"] is False


def test_response_enum_widened_is_breaking_and_narrowed_is_info():
    findings_wide, body_wide = audit_schema_change(
        "response", {"type": "string", "enum": ["a"]},
        {"type": "string", "enum": ["a", "b"]},
    )
    assert severities_for(findings_wide, "enum-changed") == ["breaking"]
    assert body_wide["compatible"] is False

    findings_narrow, body_narrow = audit_schema_change(
        "response", {"type": "string", "enum": ["a", "b"]},
        {"type": "string", "enum": ["a"]},
    )
    assert severities_for(findings_narrow, "enum-changed") == ["info"]
    assert body_narrow["compatible"] is True


# --- unspecified type (absent type == universal domain) ------------------- #

def test_request_type_added_is_breaking_and_removed_is_info():
    findings_added, body_added = audit_schema_change(
        "request", {}, {"type": "integer"}
    )
    assert severities_for(findings_added, "type-changed") == ["breaking"]
    assert body_added["compatible"] is False

    findings_removed, body_removed = audit_schema_change(
        "request", {"type": "integer"}, {}
    )
    assert severities_for(findings_removed, "type-changed") == ["info"]
    assert body_removed["compatible"] is True


def test_response_type_added_is_info_and_removed_is_breaking():
    findings_added, body_added = audit_schema_change(
        "response", {}, {"type": "integer"}
    )
    assert severities_for(findings_added, "type-changed") == ["info"]
    assert body_added["compatible"] is True

    findings_removed, body_removed = audit_schema_change(
        "response", {"type": "integer"}, {}
    )
    assert severities_for(findings_removed, "type-changed") == ["breaking"]
    assert body_removed["compatible"] is False


# --- OpenAPI 3.0 nullable ------------------------------------------------- #

def test_request_nullable_removed_is_breaking_and_added_is_info():
    findings_removed, body_removed = audit_schema_change(
        "request", {"type": "string", "nullable": True}, {"type": "string"}
    )
    assert severities_for(findings_removed, "type-changed") == ["breaking"]
    assert body_removed["compatible"] is False

    findings_added, body_added = audit_schema_change(
        "request", {"type": "string"}, {"type": "string", "nullable": True}
    )
    assert severities_for(findings_added, "type-changed") == ["info"]
    assert body_added["compatible"] is True


def test_response_nullable_removed_is_info_and_added_is_breaking():
    findings_removed, body_removed = audit_schema_change(
        "response", {"type": "string", "nullable": True}, {"type": "string"}
    )
    assert severities_for(findings_removed, "type-changed") == ["info"]
    assert body_removed["compatible"] is True

    findings_added, body_added = audit_schema_change(
        "response", {"type": "string"}, {"type": "string", "nullable": True}
    )
    assert severities_for(findings_added, "type-changed") == ["breaking"]
    assert body_added["compatible"] is False


def test_nullable_ineffective_against_enum_without_null():
    # An enum without null bounds the values even when nullable is set, so
    # dropping "nullable" changes neither the request nor response verdict.
    for side, expected in (("request", True), ("response", True)):
        _, body = audit_schema_change(
            side,
            {"type": "string", "nullable": True, "enum": ["a"]},
            {"type": "string", "enum": ["a"]},
        )
        assert body["compatible"] is expected


# --- OpenAPI 3.1 type unions ---------------------------------------------- #

def test_31_null_union_request_directions():
    findings_removed, body_removed = audit_schema_change(
        "request", {"type": ["string", "null"]}, {"type": "string"},
        version="3.1.0",
    )
    assert severities_for(findings_removed, "type-changed") == ["breaking"]
    assert body_removed["compatible"] is False

    findings_added, body_added = audit_schema_change(
        "request", {"type": "string"}, {"type": ["string", "null"]},
        version="3.1.0",
    )
    assert severities_for(findings_added, "type-changed") == ["info"]
    assert body_added["compatible"] is True


def test_31_null_union_response_directions():
    findings_removed, body_removed = audit_schema_change(
        "response", {"type": ["string", "null"]}, {"type": "string"},
        version="3.1.0",
    )
    assert severities_for(findings_removed, "type-changed") == ["info"]
    assert body_removed["compatible"] is True

    findings_added, body_added = audit_schema_change(
        "response", {"type": "string"}, {"type": ["string", "null"]},
        version="3.1.0",
    )
    assert severities_for(findings_added, "type-changed") == ["breaking"]
    assert body_added["compatible"] is False


def test_31_nullable_keyword_is_ignored():
    # "nullable" is not a JSON Schema 2020-12 keyword; nothing changes.
    for side in ("request", "response"):
        _, body = audit_schema_change(
            side,
            {"type": "string"},
            {"type": "string", "nullable": True},
            version="3.1.0",
        )
        assert body["compatible"] is True


def test_31_integer_null_union_to_number_null_union_directions():
    findings_req, body_req = audit_schema_change(
        "request", {"type": ["integer", "null"]},
        {"type": ["number", "null"]}, version="3.1.0",
    )
    assert severities_for(findings_req, "type-changed") == ["info"]
    assert body_req["compatible"] is True

    findings_resp, body_resp = audit_schema_change(
        "response", {"type": ["integer", "null"]},
        {"type": ["number", "null"]}, version="3.1.0",
    )
    assert severities_for(findings_resp, "type-changed") == ["breaking"]
    assert body_resp["compatible"] is False


def test_30_nullable_equivalent_to_31_null_union():
    # Migrating the same null allowance between dialect keywords is a no-op.
    for side in ("request", "response"):
        payload = {
            "baseline": schema_doc(
                side, {"type": "string", "nullable": True}, "3.0.3"
            ),
            "candidate": schema_doc(
                side, {"type": ["string", "null"]}, "3.1.0"
            ),
        }
        findings, body = categories(payload)
        assert "type-changed" not in findings
        assert body["compatible"] is True


def test_integer_number_rule_also_applies_to_query_parameter():
    def doc(type_value):
        return make_doc(
            paths={
                "/pets": {
                    "get": {
                        "parameters": [
                            {"name": "limit", "in": "query",
                             "schema": {"type": type_value}}
                        ],
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        )

    findings, body = categories(
        {"baseline": doc("integer"), "candidate": doc("number")}
    )
    assert severities_for(findings, "type-changed") == ["info"]
    assert body["compatible"] is True

