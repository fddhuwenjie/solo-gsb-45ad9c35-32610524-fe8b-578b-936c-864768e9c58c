"""End-to-end tests for ``POST /examples/validate``."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def make_doc(paths=None, components=None, openapi="3.0.3") -> dict:
    doc = {
        "openapi": openapi,
        "info": {"title": "t", "version": "1"},
        "paths": paths or {},
    }
    if components:
        doc["components"] = components
    return doc


def op_with_request(media_type_object, **op_extra):
    return {
        "post": {
            "requestBody": {"content": {"application/json": media_type_object}},
            "responses": {"200": {"description": "ok"}},
            **op_extra,
        }
    }


def op_with_response(media_type_object):
    return {
        "get": {
            "responses": {
                "200": {
                    "description": "ok",
                    "content": {"application/json": media_type_object},
                }
            }
        }
    }


def validate(doc):
    return client.post("/examples/validate", json={"document": doc})


PET_SCHEMA = {
    "type": "object",
    "required": ["id", "created"],
    "properties": {
        "id": {"type": "integer"},
        "name": {"type": "string"},
        "created": {"type": "string", "readOnly": True},
        "password": {"type": "string", "writeOnly": True},
    },
}


# --------------------------------------------------------------------------- #
# Happy path and basic schema violations
# --------------------------------------------------------------------------- #

def test_valid_document_returns_valid_true():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": PET_SCHEMA,
        "example": {"id": 1, "name": "Rex", "password": "s3cret"},
        "examples": {"also_good": {"value": {"id": 2}}},
    })})
    resp = validate(doc)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"valid": True, "error_count": 0, "errors": []}


def test_document_without_examples_is_valid():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": PET_SCHEMA,
    })})
    resp = validate(doc)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


def test_type_mismatch_returns_200_with_valid_false():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"type": "object", "properties": {"id": {"type": "integer"}}},
        "example": {"id": "abc"},
    })})
    resp = validate(doc)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert body["error_count"] == 1
    (error,) = body["errors"]
    assert error["side"] == "request"
    assert error["example_pointer"] == (
        "/paths/~1pets/post/requestBody/content/application~1json/example"
    )
    assert error["instance_pointer"] == "/id"
    assert error["schema_pointer"].endswith("/schema/properties/id")
    assert error["keyword"] == "type"
    assert "integer" in error["reason"]


def test_example_without_schema_is_not_validated():
    doc = make_doc(paths={"/pets": op_with_request({
        "example": {"anything": ["goes", 1, None]},
    })})
    resp = validate(doc)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


def test_non_json_media_types_are_ignored():
    doc = make_doc(paths={"/pets": {
        "post": {
            "requestBody": {
                "content": {
                    "text/plain": {
                        "schema": {"type": "string"},
                        "example": 42,  # would violate; not a JSON media type
                    },
                    "application/octet-stream": {"example": {"not": "checked"}},
                }
            },
            "responses": {"200": {"description": "ok"}},
        }
    }})
    resp = validate(doc)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


def test_vendor_json_media_type_is_validated():
    doc = make_doc(paths={"/pets": {
        "get": {
            "responses": {
                "200": {
                    "description": "ok",
                    "content": {
                        "application/problem+json": {
                            "schema": {"type": "object",
                                       "required": ["title"]},
                            "example": {},
                        }
                    },
                }
            }
        }
    }})
    resp = validate(doc)
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "required"
    assert "application~1problem+json" in body["errors"][0]["example_pointer"]


# --------------------------------------------------------------------------- #
# readOnly / writeOnly and direction-aware required
# --------------------------------------------------------------------------- #

def test_readonly_property_in_request_example_is_an_error():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": PET_SCHEMA,
        "example": {"id": 1, "created": "2024-01-01"},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "readOnly"
    assert error["side"] == "request"
    assert error["instance_pointer"] == "/created"
    assert "readOnly" in error["reason"]


def test_readonly_required_is_not_enforced_on_requests():
    # "created" is readOnly + required: required applies to responses only.
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": PET_SCHEMA,
        "example": {"id": 1},
    })})
    assert validate(doc).json()["valid"] is True


def test_readonly_required_is_enforced_on_responses():
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": PET_SCHEMA,
        "example": {"id": 1},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "required"
    assert error["side"] == "response"
    assert "'created'" in error["reason"]


def test_writeonly_property_in_response_example_is_an_error():
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": PET_SCHEMA,
        "example": {"id": 1, "created": "2024-01-01", "password": "x"},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "writeOnly"
    assert error["side"] == "response"
    assert error["instance_pointer"] == "/password"


def test_writeonly_required_is_not_enforced_on_responses():
    schema = {
        "type": "object",
        "required": ["password"],
        "properties": {"password": {"type": "string", "writeOnly": True}},
    }
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": schema,
        "example": {},
    })})
    assert validate(doc).json()["valid"] is True


def test_writeonly_required_is_enforced_on_requests():
    schema = {
        "type": "object",
        "required": ["password"],
        "properties": {"password": {"type": "string", "writeOnly": True}},
    }
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema,
        "example": {},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "required"


def test_required_enforced_in_both_directions_for_normal_properties():
    schema = {
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"type": "integer"}},
    }
    for op in (op_with_request, op_with_response):
        doc = make_doc(paths={"/pets": op({"schema": schema, "example": {}})})
        body = validate(doc).json()
        assert body["valid"] is False
        (error,) = body["errors"]
        assert error["keyword"] == "required"
        assert error["instance_pointer"] == ""


# --------------------------------------------------------------------------- #
# 3.0 nullable vs 3.1 type unions
# --------------------------------------------------------------------------- #

def test_30_nullable_true_admits_null():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"type": "string", "nullable": True},
        "example": None,
    })})
    assert validate(doc).json()["valid"] is True


def test_30_null_rejected_without_nullable():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"type": "string"},
        "example": None,
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "type"
    assert error["instance_pointer"] == ""
    assert "null" in error["reason"]


def test_31_type_union_admits_null():
    doc = make_doc(openapi="3.1.0", paths={"/pets": op_with_request({
        "schema": {"type": ["string", "null"]},
        "example": None,
    })})
    assert validate(doc).json()["valid"] is True


def test_31_nullable_keyword_is_ignored():
    # "nullable" is not a JSON Schema 2020-12 keyword; in 3.1 it must not
    # admit null.
    doc = make_doc(openapi="3.1.0", paths={"/pets": op_with_request({
        "schema": {"type": "string", "nullable": True},
        "example": None,
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "type"


def test_31_union_member_mismatch_reports_expected_union():
    doc = make_doc(openapi="3.1.0", paths={"/pets": op_with_request({
        "schema": {"type": ["string", "null"]},
        "example": 7,
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "type"
    assert "null|string" in error["reason"]


# --------------------------------------------------------------------------- #
# Scalar / collection keywords
# --------------------------------------------------------------------------- #

def test_enum_violation():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"type": "string", "enum": ["a", "b"]},
        "example": "c",
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "enum"


def test_enum_uses_json_equality_not_python_equality():
    # true is not the enum value 1.
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"enum": [1]},
        "example": True,
    })})
    assert validate(doc).json()["valid"] is False
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"enum": [1]},
        "example": 1.0,  # 1.0 == 1 in JSON semantics
    })})
    assert validate(doc).json()["valid"] is True


def test_integer_number_relationship():
    # integer is accepted where number is required.
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"type": "number"}, "example": 5,
    })})
    assert validate(doc).json()["valid"] is True
    # a float without fractional part also satisfies integer
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"type": "integer"}, "example": 5.0,
    })})
    assert validate(doc).json()["valid"] is True
    # ... but a real fraction does not
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"type": "integer"}, "example": 5.5,
    })})
    assert validate(doc).json()["valid"] is False
    # booleans are neither integer nor number
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"type": "integer"}, "example": True,
    })})
    assert validate(doc).json()["valid"] is False


def test_string_and_number_and_array_keywords():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 3, "maxLength": 5,
                         "pattern": "^[a-z]+$"},
                "age": {"type": "integer", "minimum": 0, "maximum": 130},
                "tags": {"type": "array", "minItems": 2},
                "dup": {"type": "array", "uniqueItems": True},
            },
        },
        "example": {"name": "X", "age": 200, "tags": ["a"], "dup": ["x", "x"]},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    keywords = {(e["instance_pointer"], e["keyword"]) for e in body["errors"]}
    assert ("/name", "minLength") in keywords
    assert ("/name", "pattern") in keywords
    assert ("/age", "maximum") in keywords
    assert ("/tags", "minItems") in keywords
    assert ("/dup/1", "uniqueItems") in keywords


def test_exclusive_bounds_30_boolean_and_31_numeric_forms():
    # 3.0: exclusiveMinimum is a boolean modifier of minimum.
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"type": "number", "minimum": 5, "exclusiveMinimum": True},
        "example": 5,
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "exclusiveMinimum"
    # 3.1: exclusiveMinimum carries the bound itself.
    doc = make_doc(openapi="3.1.0", paths={"/pets": op_with_request({
        "schema": {"type": "number", "exclusiveMinimum": 5},
        "example": 5,
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "exclusiveMinimum"


def test_multiple_of():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"type": "number", "multipleOf": 0.5},
        "example": 1.3,
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "multipleOf"


def test_additional_properties_false():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "additionalProperties": False,
        },
        "example": {"id": 1, "extra": "nope"},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "additionalProperties"
    assert error["instance_pointer"] == "/extra"


def test_nested_instance_pointers():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        },
        "example": {"tags": ["ok", 3]},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["instance_pointer"] == "/tags/1"
    assert error["schema_pointer"].endswith("/properties/tags/items")


# --------------------------------------------------------------------------- #
# allOf / anyOf / oneOf
# --------------------------------------------------------------------------- #

def test_allof_branches_are_all_enforced():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {
            "allOf": [
                {"type": "object", "required": ["id"]},
                {"type": "object", "properties": {"id": {"type": "integer"}}},
            ]
        },
        "example": {"id": "abc"},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    keywords = {e["keyword"] for e in body["errors"]}
    assert keywords == {"type"}
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"allOf": [{"type": "object", "required": ["id"]}]},
        "example": {},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "required"


def test_anyof_and_oneof():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        "example": True,
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "anyOf"

    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"oneOf": [{"type": "integer"}, {"type": "number"}]},
        "example": 5,  # matches both branches
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "oneOf"

    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        "example": 5,
    })})
    assert validate(doc).json()["valid"] is True


# --------------------------------------------------------------------------- #
# Direction-aware required across allOf branches
# --------------------------------------------------------------------------- #

def test_allof_readonly_on_sibling_branch_exempts_request_required():
    # One branch requires "id"; another marks it readOnly. A request example
    # omitting "id" must be accepted (readOnly required is response-only).
    schema = {"allOf": [
        {"type": "object", "required": ["id"]},
        {"type": "object",
         "properties": {"id": {"type": "integer", "readOnly": True}}},
    ]}
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema, "example": {"name": "Rex"},
    })})
    body = validate(doc).json()
    assert body["valid"] is True
    assert body["errors"] == []


def test_allof_readonly_on_sibling_branch_still_required_on_response():
    schema = {"allOf": [
        {"type": "object", "required": ["id"]},
        {"type": "object",
         "properties": {"id": {"type": "integer", "readOnly": True}}},
    ]}
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": schema, "example": {"name": "Rex"},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "required"
    assert error["side"] == "response"
    assert error["instance_pointer"] == ""
    assert "'id'" in error["reason"]
    # The physical schema pointer stays on the branch declaring required.
    assert error["schema_pointer"].endswith("/schema/allOf/0")


def test_allof_writeonly_on_sibling_branch_exempts_response_required():
    schema = {"allOf": [
        {"type": "object", "required": ["password"]},
        {"type": "object",
         "properties": {"password": {"type": "string", "writeOnly": True}}},
    ]}
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": schema, "example": {},
    })})
    assert validate(doc).json()["valid"] is True


def test_allof_writeonly_on_sibling_branch_still_required_on_request():
    schema = {"allOf": [
        {"type": "object", "required": ["password"]},
        {"type": "object",
         "properties": {"password": {"type": "string", "writeOnly": True}}},
    ]}
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema, "example": {},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "required"
    assert error["side"] == "request"


def test_allof_marker_on_property_ref_exempts_request_required():
    # The direction marker lives behind a property-level local $ref.
    schema = {"allOf": [
        {"type": "object", "required": ["id"]},
        {"type": "object",
         "properties": {"id": {"$ref": "#/components/schemas/Id"}}},
    ]}
    components = {"schemas": {"Id": {"type": "integer", "readOnly": True}}}
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema, "example": {"name": "Rex"},
    })}, components=components)
    assert validate(doc).json()["valid"] is True


def test_allof_required_on_branch_ref_and_marker_on_sibling():
    # The required branch itself is reached through a local $ref; the
    # readOnly marker sits on a sibling branch.
    schema = {"allOf": [
        {"$ref": "#/components/schemas/RequiresId"},
        {"type": "object",
         "properties": {"id": {"type": "integer", "readOnly": True}}},
    ]}
    components = {"schemas": {
        "RequiresId": {"type": "object", "required": ["id"]},
    }}
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema, "example": {"name": "Rex"},
    })}, components=components)
    assert validate(doc).json()["valid"] is True

    # On a response the missing id is still an error pointing at the
    # physical component branch.
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": schema, "example": {"name": "Rex"},
    })}, components=components)
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "required"
    assert error["schema_pointer"] == "/components/schemas/RequiresId"


def test_allof_both_branches_behind_refs_request_and_response():
    schema = {"allOf": [
        {"$ref": "#/components/schemas/RequiresId"},
        {"$ref": "#/components/schemas/IdProperty"},
    ]}
    components = {"schemas": {
        "RequiresId": {"type": "object", "required": ["id"]},
        "IdProperty": {"type": "object",
                       "properties": {"id": {"$ref": "#/components/schemas/Id"}}},
        "Id": {"type": "integer", "readOnly": True},
    }}
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema, "example": {},
    })}, components=components)
    assert validate(doc).json()["valid"] is True
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": schema, "example": {},
    })}, components=components)
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "required"


def test_allof_marker_inside_nested_branch_exempts_required():
    schema = {"allOf": [
        {"allOf": [
            {"type": "object",
             "properties": {"id": {"type": "integer", "readOnly": True}}},
        ]},
        {"type": "object", "required": ["id"]},
    ]}
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema, "example": {},
    })})
    assert validate(doc).json()["valid"] is True


def test_allof_other_branch_constraints_remain_enforced():
    # Exempting the cross-branch readOnly required must not mask any other
    # constraint declared on either branch.
    schema = {"allOf": [
        {"type": "object", "required": ["id"],
         "properties": {"id": {"type": "integer", "readOnly": True}}},
        {"type": "object", "minProperties": 2,
         "properties": {"name": {"type": "string"}}},
    ]}
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema, "example": {"name": 42},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    keywords = {e["keyword"] for e in body["errors"]}
    assert keywords == {"minProperties", "type"}
    # No required error leaks for the readOnly id.


def test_allof_direction_marker_does_not_exempt_normal_required():
    schema = {"allOf": [
        {"type": "object", "required": ["id", "name"]},
        {"type": "object", "properties": {
            "id": {"type": "integer", "readOnly": True},
            "name": {"type": "string"},
        }},
    ]}
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema, "example": {},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "required"
    assert "'name'" in error["reason"]


def test_anyof_branch_direction_marker_does_not_leak_across_variants():
    # Alternatives are independent: a readOnly marker on an allOf branch
    # must not exempt required declared *inside* a oneOf alternative, since
    # those do not describe the same allOf-combined object.
    schema = {"allOf": [
        {"type": "object",
         "properties": {"id": {"type": "integer", "readOnly": True}}},
        {"oneOf": [
            {"type": "object", "required": ["id"],
             "properties": {"id": {"type": "integer"}}},
            {"type": "object", "required": ["id"],
             "properties": {"id": {"type": "string"}}},
        ]},
    ]}
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema, "example": {},
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    # Both oneOf alternatives fail required independently; the surfaced
    # error is the oneOf mismatch, not a cross-branch exemption.
    assert body["errors"][0]["keyword"] == "oneOf"


def test_circular_allof_collects_direction_markers_without_looping():
    # A -> allOf [B], B -> allOf [A]: the aggregation walk must terminate
    # and still observe the readOnly marker contributed by A.
    schema = {"$ref": "#/components/schemas/A"}
    components = {"schemas": {
        "A": {"allOf": [{"$ref": "#/components/schemas/B"}],
              "properties": {"id": {"readOnly": True}}},
        "B": {"allOf": [{"$ref": "#/components/schemas/A"}],
              "required": ["id"],
              "properties": {"name": {"type": "string"}}},
    }}
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema, "example": {"name": "x"},
    })}, components=components)
    assert validate(doc).json()["valid"] is True


def test_circular_allof_still_enforces_required_on_response():
    schema = {"$ref": "#/components/schemas/A"}
    components = {"schemas": {
        "A": {"allOf": [{"$ref": "#/components/schemas/B"}],
              "properties": {"id": {"readOnly": True}}},
        "B": {"allOf": [{"$ref": "#/components/schemas/A"}],
              "required": ["id"],
              "properties": {"name": {"type": "string"}}},
    }}
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": schema, "example": {"name": "x"},
    })}, components=components)
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["errors"][0]["keyword"] == "required"


# --------------------------------------------------------------------------- #
# Local $ref handling
# --------------------------------------------------------------------------- #

def test_schema_ref_is_resolved_and_pointed_at_component():
    doc = make_doc(
        paths={"/pets": op_with_request({
            "schema": {"$ref": "#/components/schemas/Pet"},
            "example": {"id": "abc"},
        })},
        components={"schemas": {"Pet": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
        }}},
    )
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["schema_pointer"] == "/components/schemas/Pet/properties/id"


def test_example_object_ref_is_followed_to_components():
    doc = make_doc(
        paths={"/pets": op_with_request({
            "schema": {"type": "object",
                       "properties": {"id": {"type": "integer"}}},
            "examples": {"shared": {"$ref": "#/components/examples/Shared"}},
        })},
        components={"examples": {"Shared": {"value": {"id": "abc"}}}},
    )
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    # The example pointer follows the $ref to the physical component.
    assert error["example_pointer"] == "/components/examples/Shared/value"
    assert error["instance_pointer"] == "/id"


def test_shared_example_used_twice_is_reported_once():
    op = op_with_request({
        "schema": {"$ref": "#/components/schemas/Id"},
        "examples": {"shared": {"$ref": "#/components/examples/Shared"}},
    })
    doc = make_doc(
        paths={"/a": op, "/b": op},
        components={
            "schemas": {"Id": {"type": "object",
                               "properties": {"id": {"type": "integer"}}}},
            "examples": {"Shared": {"value": {"id": "abc"}}},
        },
    )
    body = validate(doc).json()
    assert body["valid"] is False
    assert body["error_count"] == 1


def test_requestbody_and_response_component_refs_are_followed():
    doc = make_doc(
        paths={"/pets": {
            "post": {
                "requestBody": {"$ref": "#/components/requestBodies/PetBody"},
                "responses": {"200": {"$ref": "#/components/responses/Pet"}},
            }
        }},
        components={
            "requestBodies": {"PetBody": {"content": {"application/json": {
                "schema": {"type": "object",
                           "properties": {"id": {"type": "integer"}}},
                "example": {"id": "abc"},
            }}}},
            "responses": {"Pet": {"description": "ok", "content": {
                "application/json": {
                    "schema": {"type": "object", "required": ["name"]},
                    "example": {},
                }
            }}},
        },
    )
    body = validate(doc).json()
    assert body["valid"] is False
    pointers = {e["example_pointer"] for e in body["errors"]}
    assert pointers == {
        "/components/requestBodies/PetBody/content/application~1json/example",
        "/components/responses/Pet/content/application~1json/example",
    }
    sides = {e["side"] for e in body["errors"]}
    assert sides == {"request", "response"}


def test_path_level_ref_31_supplies_the_operation():
    doc = make_doc(openapi="3.1.0")
    doc["paths"] = {"/pets": {"$ref": "#/components/pathItems/Pets"}}
    doc["components"] = {"pathItems": {"Pets": {
        "get": {"responses": {"200": {
            "description": "ok",
            "content": {"application/json": {
                "schema": {"type": "string"}, "example": 3,
            }},
        }}},
    }}}
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["example_pointer"] == (
        "/components/pathItems/Pets/get/responses/200"
        "/content/application~1json/example"
    )


def test_external_value_inside_referenced_component_is_422():
    doc = make_doc(
        paths={"/pets": op_with_response({
            "schema": {},
            "examples": {"x": {"$ref": "#/components/examples/X"}},
        })},
        components={"examples": {"X": {
            "externalValue": "http://e.com/x.json",
        }}},
    )
    resp = validate(doc)
    assert resp.status_code == 422
    (ref,) = resp.json()["references"]
    assert ref["pointer"] == "/components/examples/X/externalValue"


def test_circular_example_ref_chain_is_422():
    doc = make_doc(
        paths={"/pets": op_with_response({
            "schema": {},
            "examples": {"a": {"$ref": "#/components/examples/A"}},
        })},
        components={"examples": {
            "A": {"$ref": "#/components/examples/B"},
            "B": {"$ref": "#/components/examples/A"},
        }},
    )
    resp = validate(doc)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unresolvable_reference"
    assert "circular reference detected" in body["references"][0]["error"]


def test_readonly_direction_works_through_property_ref():
    schema = {
        "type": "object",
        "required": ["when"],
        "properties": {"when": {"$ref": "#/components/schemas/Stamp"}},
    }
    components = {"schemas": {"Stamp": {"type": "string", "readOnly": True}}}

    doc = make_doc(paths={"/pets": op_with_request({
        "schema": schema, "example": {"when": "2024"},
    })}, components=components)
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    assert error["keyword"] == "readOnly"
    assert error["schema_pointer"] == "/components/schemas/Stamp"
    # required on the readOnly member is response-only: no extra error.

    doc = make_doc(paths={"/pets": op_with_response({
        "schema": schema, "example": {"when": "2024"},
    })}, components=components)
    assert validate(doc).json()["valid"] is True


def test_recursive_schema_terminates():
    doc = make_doc(
        paths={"/nodes": op_with_request({
            "schema": {"$ref": "#/components/schemas/Node"},
            "example": {
                "value": 1,
                "child": {"value": "bad", "child": {}},
            },
        })},
        components={"schemas": {"Node": {
            "type": "object",
            "properties": {
                "value": {"type": "integer"},
                "child": {"$ref": "#/components/schemas/Node"},
            },
        }}},
    )
    body = validate(doc).json()
    assert body["valid"] is False
    (error,) = body["errors"]
    # The leaf error is reached through the recursive $ref and its
    # instance/schema pointers keep accumulating.
    assert error["instance_pointer"] == "/child/value"
    assert error["schema_pointer"] == (
        "/components/schemas/Node/properties/value"
    )


# --------------------------------------------------------------------------- #
# 422 reference problems
# --------------------------------------------------------------------------- #

def test_external_value_is_422_with_source_pointer():
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": {"type": "object"},
        "examples": {"remote": {"externalValue": "https://example.com/x.json"}},
    })})
    resp = validate(doc)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unsupported_reference"
    (ref,) = body["references"]
    assert ref["pointer"] == (
        "/paths/~1pets/get/responses/200/content/application~1json"
        "/examples/remote/externalValue"
    )
    assert ref["ref"] == "https://example.com/x.json"


def test_external_example_ref_is_422():
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": {"type": "object"},
        "examples": {"remote": {"$ref": "https://example.com/e.json#/Pet"}},
    })})
    resp = validate(doc)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unsupported_reference"
    (ref,) = body["references"]
    assert ref["ref"] == "https://example.com/e.json#/Pet"
    assert ref["pointer"].endswith("/examples/remote")


def test_dangling_example_ref_is_422_with_source_pointer():
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": {"type": "object"},
        "examples": {"bad": {"$ref": "#/components/examples/Missing"}},
    })})
    resp = validate(doc)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unresolvable_reference"
    (ref,) = body["references"]
    assert ref["ref"] == "#/components/examples/Missing"
    assert ref["pointer"] == (
        "/paths/~1pets/get/responses/200/content/application~1json"
        "/examples/bad"
    )


def test_dangling_schema_ref_is_422_with_source_pointer():
    doc = make_doc(paths={"/pets": op_with_response({
        "schema": {"$ref": "#/components/schemas/Missing"},
        "example": {},
    })})
    resp = validate(doc)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unresolvable_reference"
    (ref,) = body["references"]
    assert ref["ref"] == "#/components/schemas/Missing"
    assert ref["pointer"] == (
        "/paths/~1pets/get/responses/200/content/application~1json/schema"
    )


def test_dangling_requestbody_ref_is_422():
    doc = make_doc(paths={"/pets": {
        "post": {
            "requestBody": {"$ref": "#/components/requestBodies/Missing"},
            "responses": {"200": {"description": "ok"}},
        }
    }})
    resp = validate(doc)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "unresolvable_reference"
    assert body["references"][0]["pointer"] == (
        "/paths/~1pets/post/requestBody"
    )


def test_example_ref_to_non_object_is_422():
    doc = make_doc(
        paths={"/pets": op_with_response({
            "schema": {"type": "object"},
            "examples": {"weird": {"$ref": "#/components/examples/Scalar"}},
        })},
        components={"examples": {"Scalar": 42}},
    )
    resp = validate(doc)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_example"


def test_unrelated_dangling_refs_do_not_block_validation():
    # A dangling ref in a component that no example reaches is irrelevant.
    doc = make_doc(
        paths={"/pets": op_with_response({
            "schema": {"type": "object"},
            "example": {},
        })},
        components={"schemas": {"Broken": {"$ref": "#/nowhere"}}},
    )
    resp = validate(doc)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


# --------------------------------------------------------------------------- #
# Sorting, shape errors, service metadata
# --------------------------------------------------------------------------- #

def test_errors_are_stably_sorted_by_example_and_instance_pointer():
    doc = make_doc(paths={"/pets": op_with_request({
        "schema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
        },
        "example": {"a": "x", "b": "y"},
        "examples": {
            "z_first": {"value": {"a": 1}},
            "a_first": {"value": {"a": "bad"}},
        },
    })})
    body = validate(doc).json()
    assert body["valid"] is False
    keys = [(e["example_pointer"], e["instance_pointer"]) for e in body["errors"]]
    assert keys == sorted(keys)
    # The inline "example" sorts before "examples" entries; within one
    # example, /a sorts before /b.
    inline = "/paths/~1pets/post/requestBody/content/application~1json/example"
    assert keys[0] == (inline, "/a")
    assert keys[1] == (inline, "/b")
    assert keys[2][0].endswith("/examples/a_first/value")


def test_invalid_document_shape_is_400():
    resp = validate({"openapi": "2.0", "paths": {}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_openapi_document"
    resp = validate({"openapi": "3.0.3"})
    assert resp.status_code == 400


def test_root_lists_examples_validate():
    assert client.get("/").json()["examples_validate"] == "POST /examples/validate"


def test_openapi_schema_contains_the_new_route():
    spec = client.get("/openapi.json").json()
    assert "/examples/validate" in spec["paths"]
