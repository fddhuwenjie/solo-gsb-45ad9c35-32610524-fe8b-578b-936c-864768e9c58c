"""Regression tests for ``POST /compose`` (gateway contract composition)."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app.composer import compose_gateway, normalize_path_prefix
from app.main import app
from app.slicer import HTTP_METHODS

client = TestClient(app)

INFO = {"title": "Gateway", "version": "1.0"}

OK_RESPONSES = {"200": {"description": "ok"}}


def make_doc(paths=None, components=None, **extra) -> dict:
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "svc", "version": "1.0"},
        "paths": paths if paths is not None else {},
    }
    if components:
        doc["components"] = components
    doc.update(extra)
    return doc


def service(service_id, path_prefix, document):
    return {
        "service_id": service_id,
        "path_prefix": path_prefix,
        "document": document,
    }


def compose_(services, info=None):
    return client.post(
        "/compose",
        json={"info": INFO if info is None else info, "services": services},
    )


def pets_doc() -> dict:
    return make_doc(
        paths={
            "/animals": {
                "parameters": [
                    {"name": "x-tenant", "in": "header",
                     "schema": {"type": "string"}}
                ],
                "get": {
                    "operationId": "listAnimals",
                    "tags": ["animals"],
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/Animal"
                                    }
                                }
                            },
                        }
                    },
                },
                "post": {
                    "operationId": "createAnimal",
                    "security": [{"oauth": ["animals:write"]}],
                    "responses": {"201": {"description": "created"}},
                },
                "delete": {
                    "operationId": "deleteAnimals",
                    "security": [],
                    "responses": {"204": {"description": "gone"}},
                },
            }
        },
        components={
            "schemas": {
                "Animal": {
                    "type": "object",
                    "properties": {
                        "friend": {"$ref": "#/components/schemas/Animal"}
                    },
                }
            },
            "securitySchemes": {"oauth": {"type": "oauth2", "flows": {}}},
        },
        security=[{"oauth": ["animals:read"]}],
        tags=[{"name": "animals", "description": "Animal operations"}],
    )


def store_doc() -> dict:
    return make_doc(
        paths={
            "/items": {
                "get": {"operationId": "listItems", "responses": OK_RESPONSES}
            }
        },
        components={"schemas": {"Item": {"type": "object"}}},
    )


# --------------------------------------------------------------------------- #
# Happy path: merge, prefix normalization, namespacing, provenance
# --------------------------------------------------------------------------- #

def test_compose_merges_services_and_normalizes_prefixes():
    resp = compose_([
        service("pets", "pets", pets_doc()),        # missing leading slash
        service("store", "/store/", store_doc()),   # trailing slash
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    contract = body["contract"]

    # The composed info comes from the request, not from any source.
    assert contract["info"] == INFO
    assert set(contract["paths"]) == {"/pets/animals", "/store/items"}

    item = contract["paths"]["/pets/animals"]
    # Path-level fields survive; all methods are mounted.
    assert [p["name"] for p in item["parameters"]] == ["x-tenant"]
    assert set(m for m in item if m in HTTP_METHODS) == {
        "get", "post", "delete"
    }

    # operationId and tags are namespaced with the service id.
    assert item["get"]["operationId"] == "pets_listAnimals"
    assert item["get"]["tags"] == ["pets_animals"]
    store_get = contract["paths"]["/store/items"]["get"]
    assert store_get["operationId"] == "store_listItems"

    # Components are namespaced; refs into components are rewritten.
    schemas = contract["components"]["schemas"]
    assert set(schemas) == {"pets_Animal", "store_Item"}
    assert (
        schemas["pets_Animal"]["properties"]["friend"]["$ref"]
        == "#/components/schemas/pets_Animal"
    )
    assert (
        item["get"]["responses"]["200"]["content"]["application/json"]
        ["schema"]["$ref"]
        == "#/components/schemas/pets_Animal"
    )

    # Root tags are merged with prefixed names; no root security remains.
    assert contract["tags"] == [
        {"name": "pets_animals", "description": "Animal operations"}
    ]
    assert "security" not in contract
    assert "servers" not in contract


def test_compose_materializes_root_security_per_source():
    resp = compose_([
        service("pets", "/pets", pets_doc()),
        service("store", "/store", store_doc()),
    ])
    assert resp.status_code == 200, resp.text
    paths = resp.json()["contract"]["paths"]

    # Operation without "security" inherits the source root requirement,
    # with the scheme key namespaced.
    assert paths["/pets/animals"]["get"]["security"] == [
        {"pets_oauth": ["animals:read"]}
    ]
    # An explicitly declared requirement is kept (and namespaced).
    assert paths["/pets/animals"]["post"]["security"] == [
        {"pets_oauth": ["animals:write"]}
    ]
    # An explicit empty requirement means "no auth" and stays empty.
    assert paths["/pets/animals"]["delete"]["security"] == []
    # A source without root security leaves operations untouched.
    assert "security" not in paths["/store/items"]["get"]

    # The scheme definition is renamed consistently with the keys.
    assert set(contract_schemes(resp)) == {"pets_oauth"}


def contract_schemes(resp):
    return resp.json()["contract"]["components"]["securitySchemes"]


def test_compose_provenance_covers_paths_operations_and_components():
    resp = compose_([
        service("pets", "/pets", pets_doc()),
        service("store", "/store", store_doc()),
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    contract, provenance = body["contract"], body["provenance"]

    for path, item in contract["paths"].items():
        sources = provenance["paths"][path]
        assert {s["service_id"] for s in sources} <= {"pets", "store"}
        for key in item:
            if key in HTTP_METHODS:
                op_source = provenance["operations"][path][key]
                assert op_source["service_id"] in {"pets", "store"}
                assert op_source["source_pointer"].startswith("/paths/")

    for section, entries in contract["components"].items():
        for name in entries:
            source = provenance["components"][section][name]
            assert source["service_id"] in {"pets", "store"}
            assert source["source_pointer"].startswith(
                f"/components/{section}/"
            )

    # Spot-check exact pointers (RFC 6901 escaping of the source path).
    assert provenance["paths"]["/pets/animals"] == [{
        "service_id": "pets",
        "source_path": "/animals",
        "source_pointer": "/paths/~1animals",
    }]
    assert (
        provenance["operations"]["/pets/animals"]["get"]["source_pointer"]
        == "/paths/~1animals/get"
    )
    assert (
        provenance["components"]["schemas"]["pets_Animal"]["source_pointer"]
        == "/components/schemas/Animal"
    )


def test_compose_root_mount_prefixes():
    resp = compose_([
        service("a", "", make_doc(paths={"/x": {"get": {"responses": OK_RESPONSES}}})),
        service("b", "/", make_doc(paths={"/y": {"get": {"responses": OK_RESPONSES}}})),
    ])
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["contract"]["paths"]) == {"/x", "/y"}


def test_compose_openapi_version_is_the_max_of_sources():
    doc_31 = make_doc(paths={"/b": {"get": {"responses": OK_RESPONSES}}})
    doc_31["openapi"] = "3.1.0"
    resp = compose_([
        service("a", "/a", make_doc(paths={"/a": {"get": {"responses": OK_RESPONSES}}})),
        service("b", "/b", doc_31),
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["contract"]["openapi"] == "3.1.0"


def test_compose_rewrites_refs_to_every_component_section():
    doc = make_doc(
        paths={
            "/x": {
                "get": {
                    "parameters": [{"$ref": "#/components/parameters/Limit"}],
                    "requestBody": {
                        "$ref": "#/components/requestBodies/Payload"
                    },
                    "responses": {"200": {"$ref": "#/components/responses/Ok"}},
                }
            }
        },
        components={
            "parameters": {
                "Limit": {"name": "limit", "in": "query",
                          "schema": {"type": "integer"}}
            },
            "requestBodies": {"Payload": {"content": {}}},
            "responses": {"Ok": {"description": "ok"}},
        },
    )
    resp = compose_([
        service("a", "/a", doc),
        service("b", "/b", make_doc()),
    ])
    assert resp.status_code == 200, resp.text
    contract = resp.json()["contract"]
    operation = contract["paths"]["/a/x"]["get"]
    assert operation["parameters"][0]["$ref"] == (
        "#/components/parameters/a_Limit"
    )
    assert operation["requestBody"]["$ref"] == (
        "#/components/requestBodies/a_Payload"
    )
    assert operation["responses"]["200"]["$ref"] == (
        "#/components/responses/a_Ok"
    )
    components = contract["components"]
    assert set(components["parameters"]) == {"a_Limit"}
    assert set(components["requestBodies"]) == {"a_Payload"}
    assert set(components["responses"]) == {"a_Ok"}


def test_compose_rewrites_deep_ref_into_component():
    doc = make_doc(
        paths={
            "/x": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/Animal"
                                        "/properties/name"
                                    }
                                }
                            },
                        }
                    }
                }
            }
        },
        components={
            "schemas": {
                "Animal": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                }
            }
        },
    )
    resp = compose_([
        service("a", "/a", doc),
        service("b", "/b", make_doc()),
    ])
    assert resp.status_code == 200, resp.text
    schema = resp.json()["contract"]["paths"]["/a/x"]["get"]["responses"]
    assert schema["200"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/a_Animal/properties/name"
    )


def test_compose_merges_path_item_across_services():
    doc_a = make_doc(paths={
        "/things": {
            "summary": "from-a",
            "parameters": [{"name": "p1", "in": "query",
                            "schema": {"type": "string"}}],
            "get": {"responses": OK_RESPONSES},
        }
    })
    doc_b = make_doc(paths={
        "/things": {
            "summary": "from-b",
            "parameters": [
                {"name": "p1", "in": "query", "schema": {"type": "string"}},
                {"name": "p2", "in": "query", "schema": {"type": "string"}},
            ],
            "post": {"responses": OK_RESPONSES},
        }
    })
    resp = compose_([
        service("a", "/api", doc_a),
        service("b", "/api", doc_b),
    ])
    assert resp.status_code == 200, resp.text
    item = resp.json()["contract"]["paths"]["/api/things"]
    # Different methods on one composed path merge into a single item.
    assert set(m for m in item if m in HTTP_METHODS) == {"get", "post"}
    # Path-level fields are first-wins; parameters are unioned.
    assert item["summary"] == "from-a"
    assert [p["name"] for p in item["parameters"]] == ["p1", "p2"]
    # The path provenance lists both contributing services.
    sources = resp.json()["provenance"]["paths"]["/api/things"]
    assert [s["service_id"] for s in sources] == ["a", "b"]


def test_compose_openapi31_path_item_ref_is_kept_and_target_transformed():
    doc = make_doc(
        paths={"/things": {"$ref": "#/components/pathItems/Things"}},
        components={
            "pathItems": {
                "Things": {
                    "get": {"operationId": "listThings",
                            "responses": OK_RESPONSES}
                }
            },
            "securitySchemes": {"oauth": {"type": "oauth2", "flows": {}}},
        },
        security=[{"oauth": []}],
    )
    doc["openapi"] = "3.1.0"
    resp = compose_([
        service("t", "/t", doc),
        service("u", "/u", make_doc()),
    ])
    assert resp.status_code == 200, resp.text
    contract = resp.json()["contract"]
    # The path keeps referencing the renamed pathItems component...
    assert contract["paths"]["/t/things"]["$ref"] == (
        "#/components/pathItems/t_Things"
    )
    # ...whose operations are namespaced and inherit root security.
    operation = contract["components"]["pathItems"]["t_Things"]["get"]
    assert operation["operationId"] == "t_listThings"
    assert operation["security"] == [{"t_oauth": []}]
    provenance = resp.json()["provenance"]
    assert provenance["components"]["pathItems"]["t_Things"] == {
        "service_id": "t",
        "source_pointer": "/components/pathItems/Things",
    }


def test_compose_drops_non_retained_root_fields():
    doc = make_doc(
        paths={"/x": {"get": {"responses": OK_RESPONSES}}},
        servers=[{"url": "https://a.example.com"}],
        externalDocs={"url": "https://docs.example.com"},
        webhooks={
            "ping": {"post": {"requestBody": {"content": {
                "application/json": {
                    "schema": {"$ref": "https://evil.example.com/x.json"}
                }
            }}}}}
        ,
        **{"x-internal": {"note": "dropped"}},
    )
    resp = compose_([
        service("a", "/a", doc),
        service("b", "/b", make_doc()),
    ])
    # A bad reference inside a dropped root field (webhooks) never blocks.
    assert resp.status_code == 200, resp.text
    contract = resp.json()["contract"]
    assert set(contract) == {"openapi", "info", "paths"}


def test_compose_does_not_mutate_source_documents():
    doc_a = pets_doc()
    doc_b = store_doc()
    snapshot = copy.deepcopy([doc_a, doc_b])
    contract, provenance = compose_gateway(
        INFO,
        [
            service("pets", "/pets", doc_a),
            service("store", "/store", doc_b),
        ],
    )
    assert contract["paths"]
    assert [doc_a, doc_b] == snapshot


def test_normalize_path_prefix():
    assert normalize_path_prefix("") == ("", None)
    assert normalize_path_prefix("/") == ("", None)
    assert normalize_path_prefix("pets") == ("/pets", None)
    assert normalize_path_prefix("/pets/") == ("/pets", None)
    assert normalize_path_prefix("/a/b") == ("/a/b", None)
    prefix, error = normalize_path_prefix("bad prefix")
    assert prefix is None and error is not None
    prefix, error = normalize_path_prefix("/a?b")
    assert prefix is None and error is not None


# --------------------------------------------------------------------------- #
# 409 operation conflicts
# --------------------------------------------------------------------------- #

def test_compose_conflict_returns_409_with_both_sources():
    resp = compose_([
        service("alpha", "/pets", make_doc(paths={
            "/animals": {"get": {"responses": OK_RESPONSES},
                         "post": {"responses": OK_RESPONSES}}
        })),
        service("beta", "pets", make_doc(paths={
            "/animals": {"get": {"responses": OK_RESPONSES}}
        })),
    ])
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "operation_conflict"
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["path"] == "/pets/animals"
    assert conflict["method"] == "get"
    assert [s["service_id"] for s in conflict["sources"]] == ["alpha", "beta"]
    assert conflict["sources"][0]["source_pointer"] == "/paths/~1animals/get"
    assert conflict["sources"][1]["source_pointer"] == "/paths/~1animals/get"
    assert conflict["sources"][1]["source_path"] == "/animals"


def test_compose_conflict_detection_is_case_insensitive():
    doc = make_doc(paths={"/x": {"GET": {"responses": OK_RESPONSES}}})
    resp = compose_([
        service("a", "/a", make_doc(paths={
            "/x": {"get": {"responses": OK_RESPONSES}}
        })),
        service("b", "/a", doc),
    ])
    assert resp.status_code == 409, resp.text
    assert resp.json()["conflicts"][0]["method"] == "get"


# --------------------------------------------------------------------------- #
# 409 conflicts through OpenAPI 3.1 path-level $ref chains
# --------------------------------------------------------------------------- #

def path_items_doc(path="/animals", method_key="get"):
    """A 3.1 document whose path item is a local $ref to a pathItems
    component that provides *method_key*."""
    doc = make_doc(
        paths={path: {"$ref": "#/components/pathItems/Item"}},
        components={
            "pathItems": {"Item": {method_key: {"responses": OK_RESPONSES}}}
        },
    )
    doc["openapi"] = "3.1.0"
    return doc


def test_compose_conflict_when_method_comes_from_path_item_ref():
    resp = compose_([
        service("alpha", "/pets", path_items_doc()),
        service("beta", "/pets", make_doc(paths={
            "/animals": {"get": {"responses": OK_RESPONSES}}
        })),
    ])
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error"] == "operation_conflict"
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["path"] == "/pets/animals"
    assert conflict["method"] == "get"
    # Each side points at the operation inside its own source document:
    # the referenced component operation for alpha, the direct one for beta.
    assert conflict["sources"] == [
        {
            "service_id": "alpha",
            "source_path": "/animals",
            "source_pointer": "/components/pathItems/Item/get",
        },
        {
            "service_id": "beta",
            "source_path": "/animals",
            "source_pointer": "/paths/~1animals/get",
        },
    ]


def test_compose_conflict_through_multilevel_path_item_ref_chain():
    doc = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/A"}},
        components={
            "pathItems": {
                "A": {"$ref": "#/components/pathItems/B"},
                "B": {"$ref": "#/components/pathItems/C"},
                "C": {"get": {"responses": OK_RESPONSES}},
            }
        },
    )
    doc["openapi"] = "3.1.0"
    resp = compose_([
        service("alpha", "/pets", doc),
        service("beta", "/pets", make_doc(paths={
            "/animals": {"get": {"responses": OK_RESPONSES}}
        })),
    ])
    assert resp.status_code == 409, resp.text
    conflict = resp.json()["conflicts"][0]
    assert conflict["method"] == "get"
    # The pointer follows the chain to the operation's real location.
    assert (
        conflict["sources"][0]["source_pointer"]
        == "/components/pathItems/C/get"
    )


def test_compose_conflict_between_two_path_item_refs():
    resp = compose_([
        service("alpha", "/pets", path_items_doc()),
        service("beta", "/pets", path_items_doc()),
    ])
    assert resp.status_code == 409, resp.text
    conflict = resp.json()["conflicts"][0]
    assert [s["service_id"] for s in conflict["sources"]] == ["alpha", "beta"]
    assert all(
        s["source_pointer"] == "/components/pathItems/Item/get"
        for s in conflict["sources"]
    )


def test_compose_conflict_via_ref_is_case_insensitive():
    resp = compose_([
        service("alpha", "/pets", path_items_doc(method_key="GET")),
        service("beta", "/pets", make_doc(paths={
            "/animals": {"get": {"responses": OK_RESPONSES}}
        })),
    ])
    assert resp.status_code == 409, resp.text
    assert resp.json()["conflicts"][0]["method"] == "get"


def test_compose_path_item_sibling_method_overrides_referenced_one():
    # The path item references a component that provides "get" but also
    # declares a sibling "get": the sibling wins, so the conflict source
    # is the sibling operation on the path item itself.
    doc = make_doc(
        paths={
            "/animals": {
                "$ref": "#/components/pathItems/Item",
                "get": {"operationId": "own", "responses": OK_RESPONSES},
            }
        },
        components={
            "pathItems": {
                "Item": {
                    "get": {"operationId": "referenced",
                            "responses": OK_RESPONSES},
                    "delete": {"responses": OK_RESPONSES},
                }
            }
        },
    )
    doc["openapi"] = "3.1.0"
    resp = compose_([
        service("alpha", "/pets", doc),
        service("beta", "/pets", make_doc(paths={
            "/animals": {"get": {"responses": OK_RESPONSES}}
        })),
    ])
    assert resp.status_code == 409, resp.text
    conflicts = resp.json()["conflicts"]
    assert [c["method"] for c in conflicts] == ["get"]
    assert (
        conflicts[0]["sources"][0]["source_pointer"] == "/paths/~1animals/get"
    )


def test_compose_ref_chain_collects_methods_from_intermediate_siblings():
    # A contributes "delete" as a sibling on an intermediate chain node,
    # B (the terminal node) contributes "get"; both collide with beta.
    doc = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/A"}},
        components={
            "pathItems": {
                "A": {
                    "$ref": "#/components/pathItems/B",
                    "delete": {"responses": OK_RESPONSES},
                },
                "B": {"get": {"responses": OK_RESPONSES}},
            }
        },
    )
    doc["openapi"] = "3.1.0"
    resp = compose_([
        service("alpha", "/pets", doc),
        service("beta", "/pets", make_doc(paths={
            "/animals": {
                "get": {"responses": OK_RESPONSES},
                "delete": {"responses": OK_RESPONSES},
            }
        })),
    ])
    assert resp.status_code == 409, resp.text
    conflicts = {
        c["method"]: c for c in resp.json()["conflicts"]
    }
    assert set(conflicts) == {"get", "delete"}
    assert (
        conflicts["get"]["sources"][0]["source_pointer"]
        == "/components/pathItems/B/get"
    )
    assert (
        conflicts["delete"]["sources"][0]["source_pointer"]
        == "/components/pathItems/A/delete"
    )


def test_compose_non_conflicting_path_item_ref_keeps_ref_shape():
    # Different methods via ref and directly: no conflict, the composed
    # path item keeps its $ref form, and provenance maps both methods.
    resp = compose_([
        service("alpha", "/pets", path_items_doc()),
        service("beta", "/pets", make_doc(paths={
            "/animals": {"post": {"responses": OK_RESPONSES}}
        })),
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    item = body["contract"]["paths"]["/pets/animals"]
    assert item["$ref"] == "#/components/pathItems/alpha_Item"
    assert set(m for m in item if m in HTTP_METHODS) == {"post"}
    operations = body["provenance"]["operations"]["/pets/animals"]
    assert operations["get"] == {
        "service_id": "alpha",
        "source_path": "/animals",
        "source_pointer": "/components/pathItems/Item/get",
    }
    assert operations["post"]["source_pointer"] == "/paths/~1animals/post"


# --------------------------------------------------------------------------- #
# Merging non-conflicting path-level $ref chains on one composed path
# --------------------------------------------------------------------------- #

def composed_methods(contract, path):
    """Effective methods of a composed path item, following its local
    ``$ref`` chain inside the composed contract (siblings included)."""
    item = contract["paths"][path]
    methods = set()
    seen_refs = set()
    while isinstance(item, dict):
        methods.update(k.lower() for k in item if k.lower() in HTTP_METHODS)
        ref = item.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/"):
            break
        if ref in seen_refs:
            break
        seen_refs.add(ref)
        node = contract
        for token in ref[2:].split("/"):
            node = node[token.replace("~1", "/").replace("~0", "~")]
        item = node
    return methods


def test_compose_merges_path_item_refs_from_two_services():
    # Two services mount the same path under the same prefix, each
    # providing a different method through a path-level $ref: both
    # methods must stay resolvable from the composed path item.
    alpha = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/Item"}},
        components={
            "pathItems": {
                "Item": {
                    "get": {
                        "operationId": "listAnimals",
                        "responses": {
                            "200": {
                                "description": "ok",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "$ref": "#/components/schemas/Animal"
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
            "schemas": {"Animal": {"type": "object"}},
            "securitySchemes": {"oauth": {"type": "oauth2", "flows": {}}},
        },
        security=[{"oauth": ["animals:read"]}],
    )
    alpha["openapi"] = "3.1.0"
    beta = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/Item"}},
        components={
            "pathItems": {
                "Item": {
                    "post": {
                        "operationId": "createAnimal",
                        "responses": {"201": {"description": "created"}},
                    }
                }
            }
        },
    )
    beta["openapi"] = "3.1.0"
    resp = compose_([
        service("alpha", "/pets", alpha),
        service("beta", "/pets", beta),
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    contract = body["contract"]
    item = contract["paths"]["/pets/animals"]

    # The first reference keeps its shape; beta's method is inlined as a
    # sibling operation, so both methods resolve from the composed path.
    assert item["$ref"] == "#/components/pathItems/alpha_Item"
    assert composed_methods(contract, "/pets/animals") == {"get", "post"}

    # The inlined operation carries beta's namespacing; the referenced
    # get operation is transformed inside alpha's pathItems component.
    assert item["post"]["operationId"] == "beta_createAnimal"
    target_get = contract["components"]["pathItems"]["alpha_Item"]["get"]
    assert target_get["operationId"] == "alpha_listAnimals"
    assert target_get["security"] == [{"alpha_oauth": ["animals:read"]}]
    assert (
        target_get["responses"]["200"]["content"]["application/json"]
        ["schema"]["$ref"] == "#/components/schemas/alpha_Animal"
    )

    # Provenance points at the real source operations on both sides.
    operations = body["provenance"]["operations"]["/pets/animals"]
    assert operations["get"] == {
        "service_id": "alpha",
        "source_path": "/animals",
        "source_pointer": "/components/pathItems/Item/get",
    }
    assert operations["post"] == {
        "service_id": "beta",
        "source_path": "/animals",
        "source_pointer": "/components/pathItems/Item/post",
    }


def test_compose_merges_multilevel_ref_chains_from_two_services():
    # Both sides use multi-level chains; alpha's chain also contributes a
    # method from an intermediate node's sibling field.
    alpha = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/A"}},
        components={
            "pathItems": {
                "A": {"$ref": "#/components/pathItems/B"},
                "B": {
                    "$ref": "#/components/pathItems/C",
                    "delete": {"operationId": "del",
                               "responses": {"204": {"description": "gone"}}},
                },
                "C": {"get": {"operationId": "list",
                              "responses": OK_RESPONSES}},
            }
        },
    )
    alpha["openapi"] = "3.1.0"
    beta = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/X"}},
        components={
            "pathItems": {
                "X": {"$ref": "#/components/pathItems/Y"},
                "Y": {"post": {"operationId": "create",
                               "responses": OK_RESPONSES}},
            }
        },
    )
    beta["openapi"] = "3.1.0"
    resp = compose_([
        service("alpha", "/pets", alpha),
        service("beta", "/pets", beta),
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    contract = body["contract"]
    item = contract["paths"]["/pets/animals"]

    # get/delete resolve through alpha's kept chain; beta's post (from
    # the terminal node of its own chain) is inlined.
    assert item["$ref"] == "#/components/pathItems/alpha_A"
    assert composed_methods(contract, "/pets/animals") == {
        "get", "delete", "post"
    }
    assert item["post"]["operationId"] == "beta_create"

    operations = body["provenance"]["operations"]["/pets/animals"]
    assert (
        operations["get"]["source_pointer"] == "/components/pathItems/C/get"
    )
    assert (
        operations["delete"]["source_pointer"]
        == "/components/pathItems/B/delete"
    )
    assert operations["post"] == {
        "service_id": "beta",
        "source_path": "/animals",
        "source_pointer": "/components/pathItems/Y/post",
    }


def test_compose_inlined_operation_is_the_effective_one():
    # beta's chain has an intermediate sibling "post" that overrides the
    # terminal node's "post": the inlined operation must be the sibling's.
    beta = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/Mid"}},
        components={
            "pathItems": {
                "Mid": {
                    "$ref": "#/components/pathItems/Base",
                    "post": {"operationId": "siblingPost",
                             "responses": OK_RESPONSES},
                },
                "Base": {
                    "post": {"operationId": "basePost",
                             "responses": OK_RESPONSES},
                    "put": {"operationId": "basePut",
                            "responses": OK_RESPONSES},
                },
            }
        },
    )
    beta["openapi"] = "3.1.0"
    resp = compose_([
        service("alpha", "/pets", path_items_doc(method_key="get")),
        service("beta", "/pets", beta),
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    contract = body["contract"]
    item = contract["paths"]["/pets/animals"]

    assert composed_methods(contract, "/pets/animals") == {
        "get", "post", "put"
    }
    # The sibling (nearer) post wins over the referenced one; the
    # terminal node's put is inlined with its own identity.
    assert item["post"]["operationId"] == "beta_siblingPost"
    assert item["put"]["operationId"] == "beta_basePut"

    operations = body["provenance"]["operations"]["/pets/animals"]
    assert (
        operations["post"]["source_pointer"]
        == "/components/pathItems/Mid/post"
    )
    assert (
        operations["put"]["source_pointer"]
        == "/components/pathItems/Base/put"
    )


def test_compose_ref_chain_methods_merge_alongside_direct_methods():
    # alpha provides "get" via $ref; beta declares "put" directly and
    # provides "post" through its own $ref (dropped by first-wins).
    beta = make_doc(
        paths={
            "/animals": {
                "$ref": "#/components/pathItems/Item",
                "put": {"operationId": "replace", "responses": OK_RESPONSES},
            }
        },
        components={
            "pathItems": {
                "Item": {
                    "post": {"operationId": "create",
                             "responses": OK_RESPONSES}
                }
            }
        },
    )
    beta["openapi"] = "3.1.0"
    resp = compose_([
        service("alpha", "/pets", path_items_doc(method_key="get")),
        service("beta", "/pets", beta),
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    contract = body["contract"]
    item = contract["paths"]["/pets/animals"]

    assert item["$ref"] == "#/components/pathItems/alpha_Item"
    assert composed_methods(contract, "/pets/animals") == {
        "get", "post", "put"
    }
    assert item["put"]["operationId"] == "beta_replace"
    assert item["post"]["operationId"] == "beta_create"

    operations = body["provenance"]["operations"]["/pets/animals"]
    assert operations["put"]["source_pointer"] == "/paths/~1animals/put"
    assert (
        operations["post"]["source_pointer"]
        == "/components/pathItems/Item/post"
    )


def test_compose_three_ref_chains_on_one_path_all_resolve():
    resp = compose_([
        service("alpha", "/pets", path_items_doc(method_key="get")),
        service("beta", "/pets", path_items_doc(method_key="post")),
        service("gamma", "/pets", path_items_doc(method_key="delete")),
    ])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    contract = body["contract"]
    item = contract["paths"]["/pets/animals"]
    assert item["$ref"] == "#/components/pathItems/alpha_Item"
    assert composed_methods(contract, "/pets/animals") == {
        "get", "post", "delete"
    }
    operations = body["provenance"]["operations"]["/pets/animals"]
    assert operations["get"]["service_id"] == "alpha"
    assert operations["post"]["service_id"] == "beta"
    assert operations["delete"]["service_id"] == "gamma"


# --------------------------------------------------------------------------- #
# 422 reference errors from path-level $ref chains
# --------------------------------------------------------------------------- #

def test_compose_circular_path_item_ref_chain_returns_422():
    doc = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/A"}},
        components={
            "pathItems": {
                "A": {"$ref": "#/components/pathItems/B"},
                "B": {"$ref": "#/components/pathItems/A"},
            }
        },
    )
    doc["openapi"] = "3.1.0"
    resp = compose_([
        service("cyc", "/pets", doc),
        service("other", "/other", make_doc()),
    ])
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "unsupported_reference"
    assert len(body["references"]) == 1
    reference = body["references"][0]
    assert reference["service_id"] == "cyc"
    assert reference["pointer"] == "/components/pathItems/B"
    assert reference["ref"] == "#/components/pathItems/A"
    assert "circular reference detected" in reference["error"]


def test_compose_self_referencing_path_item_component_returns_422():
    doc = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/A"}},
        components={
            "pathItems": {"A": {"$ref": "#/components/pathItems/A"}}
        },
    )
    doc["openapi"] = "3.1.0"
    resp = compose_([
        service("cyc", "/pets", doc),
        service("other", "/other", make_doc()),
    ])
    assert resp.status_code == 422, resp.text
    reference = resp.json()["references"][0]
    assert "circular reference detected" in reference["error"]


def test_compose_dangling_ref_inside_path_item_chain_returns_422():
    doc = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/A"}},
        components={
            "pathItems": {
                "A": {"$ref": "#/components/pathItems/Missing"}
            }
        },
    )
    doc["openapi"] = "3.1.0"
    resp = compose_([
        service("a", "/pets", doc),
        service("b", "/other", make_doc()),
    ])
    assert resp.status_code == 422, resp.text
    reference = resp.json()["references"][0]
    assert reference["ref"] == "#/components/pathItems/Missing"
    assert "unresolvable reference" in reference["error"]


def test_compose_external_path_item_ref_returns_422():
    doc = make_doc(
        paths={"/animals": {"$ref": "https://example.com/items.json#/Item"}}
    )
    doc["openapi"] = "3.1.0"
    resp = compose_([
        service("a", "/pets", doc),
        service("b", "/other", make_doc()),
    ])
    assert resp.status_code == 422, resp.text
    reference = resp.json()["references"][0]
    assert reference["pointer"] == "/paths/~1animals"
    assert "non-local" in reference["error"]


def test_compose_path_item_ref_to_non_object_returns_422():
    doc = make_doc(
        paths={"/animals": {"$ref": "#/components/schemas/Name"}},
        components={"schemas": {"Name": "just-a-string"}},
    )
    doc["openapi"] = "3.1.0"
    resp = compose_([
        service("a", "/pets", doc),
        service("b", "/other", make_doc()),
    ])
    assert resp.status_code == 422, resp.text
    reference = resp.json()["references"][0]
    assert reference["ref"] == "#/components/schemas/Name"
    assert "does not target an object" in reference["error"]


def test_compose_excessively_deep_path_item_chain_returns_422():
    # 150 chained pathItems components exceed the reference-chain ceiling;
    # the request must fail fast with a 422, not hang or 5xx.
    items = {}
    for index in range(150):
        if index < 149:
            items[f"P{index}"] = {
                "$ref": f"#/components/pathItems/P{index + 1}"
            }
        else:
            items[f"P{index}"] = {"get": {"responses": OK_RESPONSES}}
    doc = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/P0"}},
        components={"pathItems": items},
    )
    doc["openapi"] = "3.1.0"
    resp = compose_([
        service("deep", "/pets", doc),
        service("other", "/other", make_doc()),
    ])
    assert resp.status_code == 422, resp.text
    reference = resp.json()["references"][0]
    assert "maximum depth" in reference["error"]


def test_compose_circular_chain_takes_precedence_over_conflict():
    # A circular chain (422) and a real method conflict (409) both exist;
    # reference errors are reported first.
    cyclic = make_doc(
        paths={"/animals": {"$ref": "#/components/pathItems/A"}},
        components={
            "pathItems": {"A": {"$ref": "#/components/pathItems/A"}}
        },
    )
    cyclic["openapi"] = "3.1.0"
    resp = compose_([
        service("alpha", "/pets", cyclic),
        service("beta", "/pets", make_doc(paths={
            "/animals": {"get": {"responses": OK_RESPONSES}}
        })),
    ])
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# 400 request errors
# --------------------------------------------------------------------------- #

def test_compose_duplicate_service_id_returns_400():
    resp = compose_([
        service("pets", "/a", make_doc()),
        service("pets", "/b", make_doc()),
    ])
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"] == "invalid_compose_request"
    assert any("duplicate service_id 'pets'" in m for m in body["messages"])


def test_compose_requires_two_to_twenty_services():
    resp = compose_([service("only", "/x", make_doc())])
    assert resp.status_code == 400, resp.text
    assert "2" in resp.json()["messages"][0]

    resp = compose_([
        service(f"s{i}", f"/p{i}", make_doc()) for i in range(21)
    ])
    assert resp.status_code == 400, resp.text

    resp = compose_([
        service(f"s{i}", f"/p{i}", make_doc()) for i in range(20)
    ])
    assert resp.status_code == 200, resp.text


def test_compose_invalid_document_returns_400():
    resp = compose_([
        service("bad", "/x", {"openapi": "2.0", "paths": {}}),
        service("ok", "/y", make_doc()),
    ])
    assert resp.status_code == 400, resp.text
    assert any(
        "services[0].document" in m for m in resp.json()["messages"]
    )


def test_compose_path_key_must_start_with_slash():
    resp = compose_([
        service("a", "/a", make_doc(paths={"noslash": {}})),
        service("b", "/b", make_doc()),
    ])
    assert resp.status_code == 400, resp.text
    assert any("must start with '/'" in m for m in resp.json()["messages"])


def test_compose_invalid_service_id_returns_400():
    resp = compose_([
        service("bad id!", "/a", make_doc()),
        service("ok", "/b", make_doc()),
    ])
    assert resp.status_code == 400, resp.text
    assert any("service_id" in m for m in resp.json()["messages"])

    resp = compose_([
        service("", "/a", make_doc()),
        service("ok", "/b", make_doc()),
    ])
    assert resp.status_code == 400, resp.text


def test_compose_invalid_path_prefix_returns_400():
    resp = compose_([
        service("a", "/ok", make_doc()),
        service("b", "bad prefix?", make_doc()),
    ])
    assert resp.status_code == 400, resp.text
    assert any("path_prefix" in m for m in resp.json()["messages"])


# --------------------------------------------------------------------------- #
# 422 reference errors
# --------------------------------------------------------------------------- #

def doc_with_schema_ref(ref: str) -> dict:
    return make_doc(paths={
        "/x": {
            "get": {
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {"schema": {"$ref": ref}}
                        },
                    }
                }
            }
        }
    })


SCHEMA_POINTER = "/paths/~1x/get/responses/200/content/application~1json/schema"


def test_compose_external_reference_returns_422_with_source_pointer():
    resp = compose_([
        service("a", "/a", doc_with_schema_ref(
            "https://example.com/schemas.json#/Animal"
        )),
        service("b", "/b", make_doc()),
    ])
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["error"] == "unsupported_reference"
    assert len(body["references"]) == 1
    reference = body["references"][0]
    assert reference["service_id"] == "a"
    assert reference["ref"] == "https://example.com/schemas.json#/Animal"
    assert reference["pointer"] == SCHEMA_POINTER
    assert "non-local" in reference["error"]


@pytest.mark.parametrize(
    "bad_ref",
    ["#", "#/info", "#/paths/~1x", "#/components", "#/components/schemas"],
)
def test_compose_reference_outside_components_returns_422(bad_ref):
    doc = doc_with_schema_ref(bad_ref)
    if bad_ref == "#/paths/~1x":
        # Make the target resolvable: location, not resolvability, decides.
        doc["components"] = {"schemas": {"X": {"type": "object"}}}
    resp = compose_([
        service("a", "/a", doc),
        service("b", "/b", make_doc()),
    ])
    assert resp.status_code == 422, resp.text
    reference = resp.json()["references"][0]
    assert reference["ref"] == bad_ref
    assert reference["pointer"] == SCHEMA_POINTER
    assert "components" in reference["error"]


def test_compose_dangling_reference_returns_422():
    resp = compose_([
        service("a", "/a", doc_with_schema_ref("#/components/schemas/Missing")),
        service("b", "/b", make_doc()),
    ])
    assert resp.status_code == 422, resp.text
    reference = resp.json()["references"][0]
    assert reference["ref"] == "#/components/schemas/Missing"
    assert "unresolvable reference" in reference["error"]


def test_compose_reports_all_reference_errors_sorted():
    doc_a = doc_with_schema_ref("http://example.com/a.json#/X")
    doc_b = doc_with_schema_ref("#/components/schemas/Missing")
    resp = compose_([
        service("a", "/a", doc_a),
        service("b", "/b", doc_b),
    ])
    assert resp.status_code == 422, resp.text
    references = resp.json()["references"]
    assert [r["service_id"] for r in references] == ["a", "b"]


def test_compose_reference_errors_take_precedence_over_conflicts():
    doc_a = make_doc(paths={"/x": {"get": {"responses": OK_RESPONSES}}})
    doc_b = make_doc(paths={
        "/x": {
            "get": {
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "http://example.com/x"}
                            }
                        },
                    }
                }
            }
        }
    })
    resp = compose_([
        service("a", "/api", doc_a),
        service("b", "/api", doc_b),
    ])
    # Both a conflict and a bad reference exist; the reference error wins.
    assert resp.status_code == 422, resp.text


def test_compose_ref_like_string_inside_example_is_validated():
    doc = make_doc(paths={
        "/x": {
            "get": {
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "example": {"$ref": "http://example.com/x"}
                            }
                        },
                    }
                }
            }
        }
    })
    resp = compose_([
        service("a", "/a", doc),
        service("b", "/b", make_doc()),
    ])
    # Consistent with /audit and /slice: any "$ref" string in retained
    # content is treated as a reference.
    assert resp.status_code == 422, resp.text


# --------------------------------------------------------------------------- #
# Existing endpoints are unaffected
# --------------------------------------------------------------------------- #

def test_audit_slice_health_and_root_still_work():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").json()["compose"] == "POST /compose"

    doc = make_doc(paths={"/x": {"get": {"responses": OK_RESPONSES}}})
    audit = client.post("/audit", json={"baseline": doc, "candidate": doc})
    assert audit.status_code == 200, audit.text
    assert audit.json()["compatible"] is True

    slice_resp = client.post(
        "/slice", json={"document": doc, "selectors": ["GET /x"]}
    )
    assert slice_resp.status_code == 200, slice_resp.text
    assert "sha256" in slice_resp.json()
