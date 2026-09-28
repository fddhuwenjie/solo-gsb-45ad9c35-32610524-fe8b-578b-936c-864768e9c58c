"""Backward-compatibility audit between two OpenAPI 3.x documents.

Compatibility policy
--------------------
* **Requests** describe what clients send. Their accepted value set must
  not be *tightened*: removing an operation, adding a required
  parameter/property, changing a type, narrowing an enum, or adding a
  request body all break clients that sent previously valid requests.
* **Responses** describe what clients receive. Their produced value set
  must not be *loosened*: removing a response status code/media
  type/property, changing a type, or expanding an enum break clients
  that rely on the previously guaranteed response shape.

Schema constraints are compared as value sets, not by literal keyword
equality. An absent ``type`` or ``enum`` means the universal (unbounded)
domain; OpenAPI 3.0 ``nullable: true`` and the OpenAPI 3.1
``"null"`` type-union member both add the null value; and ``integer``
is contained in ``number``. Hence ``integer`` -> ``number`` is a
compatible request widening but a breaking response widening, while
introducing an enum breaks the request side and removing one breaks the
response side.

Changes that are safe in the respective direction (adding a new operation
or an optional request parameter, removing a request enum value, dropping a
value from a response enum, ...) are reported with severity ``"info"`` so
the report stays informative; only ``"breaking"`` findings violate the
contract policy.

JSON Pointer convention
-----------------------
* Deleted constructs are located in the *baseline* document.
* New or modified constructs are located in the *candidate* document.
* When a construct is a ``$ref``, the pointer follows the reference to the
  resolved component definition (e.g. ``#/components/schemas/Pet``), which
  is the stable location to fix the incompatibility at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .refresolver import (
    CircularReferenceError,
    RefResolver,
    ReferenceError,
    UnresolvableReferenceError,
)

HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)

# Defensive guard for pathological schema nesting that is not a pure
# $ref cycle (deep inline recursion can otherwise exhaust the stack).
MAX_SCHEMA_DEPTH = 300


@dataclass(frozen=True)
class Finding:
    """One audit result row."""

    pointer: str
    category: str
    severity: str  # "breaking" | "info"
    reason: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "pointer": self.pointer,
            "category": self.category,
            "severity": self.severity,
            "reason": self.reason,
        }


def escape_token(token: str) -> str:
    """Escape a JSON object key as a JSON Pointer reference token."""
    return token.replace("~", "~0").replace("/", "~1")


def ptr(*parts: Any) -> str:
    """Build a JSON Pointer from path parts (``ptr('a','b') -> '/a/b'``)."""
    return "/" + "/".join(escape_token(str(p)) for p in parts) if parts else ""


@dataclass
class AuditContext:
    base_doc: Dict[str, Any]
    cand_doc: Dict[str, Any]
    base: RefResolver
    cand: RefResolver
    findings: List[Finding] = field(default_factory=list)
    _seen: Set[Tuple[int, int, str]] = field(default_factory=set)
    _dedup: Set[Tuple[str, str, str]] = field(default_factory=set)

    def emit(
        self, pointer: str, category: str, severity: str, reason: str
    ) -> None:
        key = (pointer, category, reason)
        if key in self._dedup:
            return
        self._dedup.add(key)
        self.findings.append(Finding(pointer, category, severity, reason))


# --------------------------------------------------------------------------- #
# Schema handling
# --------------------------------------------------------------------------- #

def _merge_allof(resolver: RefResolver, node: Any, depth: int) -> Dict[str, Any]:
    """Expand ``allOf`` into a single merged schema.

    ``allOf`` means every subschema applies, so object ``properties`` and
    ``required`` lists are unioned. Scalar annotations present on the
    hosting node win (they describe the node itself); subschemas fill in
    what the host leaves unset.
    """
    node, _ = resolver.resolve(node)
    if not isinstance(node, dict) or "allOf" not in node:
        return node if isinstance(node, dict) else {}
    if depth > MAX_SCHEMA_DEPTH:
        return node if isinstance(node, dict) else {}

    merged: Dict[str, Any] = {}
    properties: Dict[str, Any] = {}
    required: Set[str] = set()

    subschemas = list(node.get("allOf") or [])
    for sub in list(subschemas):
        sub = _merge_allof(resolver, sub, depth + 1)
        if not isinstance(sub, dict):
            continue
        for key, value in sub.items():
            if key == "properties" and isinstance(value, dict):
                properties.update(value)
            elif key == "required" and isinstance(value, list):
                required.update(str(r) for r in value)
            elif key not in ("allOf",) and key not in merged:
                merged[key] = value

    # Host keys take precedence over subschema scalar keys.
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            properties.update(value)
        elif key == "required" and isinstance(value, list):
            required.update(str(r) for r in value)
        elif key != "allOf":
            merged[key] = value

    if properties:
        merged["properties"] = properties
    if required:
        merged["required"] = sorted(required)
    return merged


def _effective(
    resolver: RefResolver, node: Any
) -> Tuple[Dict[str, Any], str]:
    """Resolve ``$ref`` and expand ``allOf`` for effective comparison."""
    target, target_pointer = resolver.resolve(node)
    merged = _merge_allof(resolver, target, 0)
    return merged, target_pointer


def _is_openapi_31(doc: Any) -> bool:
    """True for an OpenAPI 3.1+ document (JSON Schema 2020-12 dialect)."""
    return isinstance(doc, dict) and str(doc.get("openapi", "")).startswith(
        ("3.1", "3.2")
    )


def _type_domain(schema: Any, doc: Any) -> Optional[frozenset]:
    """Normalize the type/null constraints of a schema into a value domain.

    The domain is the set of primitive JSON value kinds the schema accepts:

    * a bare ``"type": "string"`` becomes ``{"string"}``;
    * an OpenAPI 3.1 type union (``"type": ["string", "null"]``) becomes a
      set with every listed member;
    * OpenAPI 3.0 ``nullable: true`` contributes the ``"null"`` kind, just as
      the explicit ``"null"`` type does in 3.1 (where ``nullable`` is not a
      recognized keyword and is ignored);
    * a schema without ``type`` places no constraint and maps to ``None``,
      the universal domain that contains every value.

    ``"integer"`` is kept distinct from ``"number"`` here; the containment
    relation (every integer is a number) is applied by
    :func:`_domain_contains`.
    """
    if not isinstance(schema, dict):
        return frozenset()
    raw = schema.get("type")
    if raw is None:
        return None
    if isinstance(raw, str):
        tags = {raw}
    elif isinstance(raw, list):
        tags = {str(t) for t in raw}
    else:
        tags = {str(raw)}
    if not _is_openapi_31(doc) and schema.get("nullable") is True:
        tags.add("null")
    domain: Optional[frozenset] = frozenset(tags)
    # "enum" constrains alongside "type": when an enum is present and does
    # not list null explicitly, null is rejected even if 3.0 "nullable" or
    # a 3.1 "null" union member advertised it.
    enum = schema.get("enum")
    if isinstance(enum, list) and None not in enum:
        domain = domain - {"null"}
    return domain


def _format_domain(domain: Optional[frozenset]) -> str:
    if domain is None:
        return "unspecified"
    return "|".join(sorted(domain))


def _domain_contains(outer: Optional[frozenset], inner: Optional[frozenset]) -> bool:
    """True if every value accepted by *inner* is also accepted by *outer*.

    ``None`` denotes the universal (unconstrained) domain; ``integer`` is
    contained in ``number`` per the JSON Schema / OpenAPI type hierarchy;
    all other primitive kinds are pairwise disjoint.
    """
    if outer is None:
        return True
    if inner is None:
        return False
    for kind in inner:
        if kind in outer:
            continue
        if kind == "integer" and "number" in outer:
            continue
        return False
    return True


def _norm_enum(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [repr(v) for v in value]


def _set_contains(
    outer: Optional[Set[str]], inner: Optional[Set[str]]
) -> bool:
    """Set containment with ``None`` denoting the unbounded value set."""
    if outer is None:
        return True
    if inner is None:
        return False
    return inner <= outer


# --------------------------------------------------------------------------- #
# Schema comparison
# --------------------------------------------------------------------------- #

def compare_schemas(
    ctx: AuditContext,
    base_node: Any,
    cand_node: Any,
    base_pointer: str,
    cand_pointer: str,
    side: str,
    depth: int = 0,
) -> None:
    """Compare two parameter/body/response property schemas.

    *side* is ``"request"`` or ``"response"`` and decides in which
    direction a change is breaking.
    """
    if depth > MAX_SCHEMA_DEPTH:
        ctx.emit(
            cand_pointer,
            "schema-too-deep",
            "info",
            f"schema nesting exceeds {MAX_SCHEMA_DEPTH} levels; deeper "
            "constraints were not compared",
        )
        return

    try:
        b_schema, b_target = _effective(ctx.base, base_node)
        c_schema, c_target = _effective(ctx.cand, cand_node)
    except ReferenceError:
        # Reported up-front by scan_references; stop walking this branch.
        return

    # Memoization key: stable component pointers when the node is a $ref
    # (identity differs per document, pointer does not), Python object
    # identity for inline nodes. Cycles are cut here: revisiting the same
    # pair on the same side returns immediately.
    b_key = ("ptr", b_target) if b_target else ("id", id(b_schema))
    c_key = ("ptr", c_target) if c_target else ("id", id(c_schema))
    memo_key = (hash(b_key), hash(c_key), side)
    if memo_key in ctx._seen:
        return
    ctx._seen.add(memo_key)

    b_pointer = b_target if b_target else base_pointer
    c_pointer = c_target if c_target else cand_pointer

    if not isinstance(b_schema, dict) or not isinstance(c_schema, dict):
        if b_schema != c_schema:
            ctx.emit(
                c_pointer,
                "type-changed",
                "breaking",
                f"schema changed from {_describe_schema(b_schema)!r} to "
                f"{_describe_schema(c_schema)!r}",
            )
        return

    # --- type / nullability ----------------------------------------------
    # The accepted primitive value sets must be compared by containment,
    # not by literal equality: requests may only widen (candidate must
    # accept everything the baseline accepted), responses may only narrow
    # (candidate must return only values the baseline already promised).
    # Absence of "type" means the universal domain, 3.0 "nullable" and the
    # 3.1 "null" union member both add the null value, and integer is a
    # subset of number.
    b_domain = _type_domain(b_schema, ctx.base_doc)
    c_domain = _type_domain(c_schema, ctx.cand_doc)
    if b_domain != c_domain:
        if side == "request":
            tightened = not _domain_contains(c_domain, b_domain)
            change = "tightened" if tightened else "widened"
        else:
            tightened = not _domain_contains(b_domain, c_domain)
            change = "loosened" if tightened else "narrowed"
        ctx.emit(
            c_pointer,
            "type-changed",
            "breaking" if tightened else "info",
            f"{side} type constraint was {change} from "
            f"{_format_domain(b_domain)} to {_format_domain(c_domain)}",
        )

    # --- enum -------------------------------------------------------------
    # A missing "enum" means *no* constraint (every value of the type is
    # allowed), not the empty set. The direction is therefore set
    # containment: the candidate request set must contain the baseline
    # set; the candidate response set must be contained in the baseline
    # set. A change touching the unbounded side (enum introduced/removed)
    # cannot enumerate its affected values and is described explicitly.
    b_enum = b_schema.get("enum")
    c_enum = c_schema.get("enum")
    if isinstance(b_enum, list) or isinstance(c_enum, list):
        # Null is part of the value set only when the enum itself lists it
        # (3.1 style; 3.0 tooling also writes null into enums). A bare 3.0
        # "nullable: true" is ineffective once an enum without null bounds
        # the values, so it is not injected here -- _type_domain applies
        # the same rule to the type domain.
        b_set = set(_norm_enum(b_enum)) if isinstance(b_enum, list) else None
        c_set = set(_norm_enum(c_enum)) if isinstance(c_enum, list) else None

        if side == "request":
            # Tightening: the candidate rejects values the baseline
            # accepted.
            if c_set is not None and not _set_contains(c_set, b_set):
                if b_set is None:
                    ctx.emit(
                        c_pointer,
                        "enum-changed",
                        "breaking",
                        f"{side} enum constraint was introduced; only "
                        f"values: {', '.join(sorted(c_set))} are accepted",
                    )
                else:
                    ctx.emit(
                        c_pointer,
                        "enum-changed",
                        "breaking",
                        f"{side} enum constraint was narrowed; removed "
                        f"values: {', '.join(sorted(b_set - c_set))}",
                    )
            # Widening: the candidate accepts additional values.
            if b_set is not None and not _set_contains(b_set, c_set):
                if c_set is None:
                    ctx.emit(
                        c_pointer,
                        "enum-changed",
                        "info",
                        f"{side} enum constraint was removed; every value of "
                        "the type is now accepted (compatible)",
                    )
                else:
                    ctx.emit(
                        c_pointer,
                        "enum-changed",
                        "info",
                        f"{side} enum gained values: "
                        f"{', '.join(sorted(c_set - b_set))} (compatible)",
                    )
        else:
            # Loosening: the candidate may produce previously unadvertised
            # values.
            if b_set is not None and not _set_contains(b_set, c_set):
                if c_set is None:
                    ctx.emit(
                        c_pointer,
                        "enum-changed",
                        "breaking",
                        f"{side} enum constraint was removed; previously "
                        "unadvertised values may now be returned",
                    )
                else:
                    ctx.emit(
                        c_pointer,
                        "enum-changed",
                        "breaking",
                        f"{side} enum constraint was widened; added "
                        f"values: {', '.join(sorted(c_set - b_set))}",
                    )
            # Narrowing: the candidate promises fewer output values.
            if c_set is not None and not _set_contains(c_set, b_set):
                if b_set is None:
                    ctx.emit(
                        c_pointer,
                        "enum-changed",
                        "info",
                        f"{side} enum constraint was introduced; only "
                        f"values: {', '.join(sorted(c_set))} may now be "
                        "returned (compatible)",
                    )
                else:
                    ctx.emit(
                        c_pointer,
                        "enum-changed",
                        "info",
                        f"{side} enum no longer advertises values: "
                        f"{', '.join(sorted(b_set - c_set))} (compatible)",
                    )

    # --- required ---------------------------------------------------------
    b_required = {str(r) for r in (b_schema.get("required") or [])}
    c_required = {str(r) for r in (c_schema.get("required") or [])}
    b_props = b_schema.get("properties") if isinstance(
        b_schema.get("properties"), dict
    ) else {}
    c_props = c_schema.get("properties") if isinstance(
        c_schema.get("properties"), dict
    ) else {}
    newly_required = sorted(c_required - b_required)
    no_longer_required = sorted(b_required - c_required)
    if newly_required:
        breaking = side == "request"
        for name in newly_required:
            location = c_pointer
            reason = (
                f"property '{name}' became required in the {side}"
                if breaking
                else f"property '{name}' is newly required in the {side} "
                "(response: stricter output guarantee is compatible)"
            )
            ctx.emit(
                location,
                "required-added",
                "breaking" if breaking else "info",
                reason,
            )
    if no_longer_required and side == "response":
        for name in no_longer_required:
            ctx.emit(
                b_pointer,
                "required-removed",
                "breaking",
                f"response property '{name}' is no longer guaranteed to be "
                "required",
            )

    # --- properties -------------------------------------------------------
    names = sorted(set(b_props) | set(c_props))
    for name in names:
        in_b = name in b_props
        in_c = name in c_props
        if in_b and not in_c:
            breaking = side == "response"
            ctx.emit(
                b_pointer + ptr("properties", name),
                "property-removed",
                "breaking" if breaking else "info",
                (
                    f"response property '{name}' was removed; clients "
                    "relying on it will break"
                    if breaking
                    else f"request property '{name}' was removed; accepting "
                    "fewer sent fields is compatible"
                ),
            )
        elif in_c and not in_b:
            breaking = side == "request" and name in c_required
            ctx.emit(
                c_pointer + ptr("properties", name),
                "property-added",
                "breaking" if breaking else "info",
                (
                    f"new required request property '{name}'"
                    if breaking
                    else f"new {'required' if name in c_required else 'optional'} "
                    f"{side} property '{name}'"
                ),
            )
        else:
            compare_schemas(
                ctx,
                b_props[name],
                c_props[name],
                b_pointer + ptr("properties", name),
                c_pointer + ptr("properties", name),
                side,
                depth + 1,
            )

    # --- array items ------------------------------------------------------
    if "items" in b_schema or "items" in c_schema:
        if "items" not in b_schema or "items" not in c_schema:
            ctx.emit(
                c_pointer,
                "type-changed",
                "breaking",
                f"{side} schema changed between array and non-array",
            )
        else:
            compare_schemas(
                ctx,
                b_schema["items"],
                c_schema["items"],
                b_pointer + ptr("items"),
                c_pointer + ptr("items"),
                side,
                depth + 1,
            )

    # --- oneOf / anyOf ----------------------------------------------------
    for combiner in ("oneOf", "anyOf"):
        b_variants = b_schema.get(combiner)
        c_variants = c_schema.get(combiner)
        if isinstance(b_variants, list) and isinstance(c_variants, list):
            if len(b_variants) != len(c_variants):
                ctx.emit(
                    c_pointer + ptr(combiner),
                    "schema-variants-changed",
                    "breaking",
                    f"{side} '{combiner}' variant count changed from "
                    f"{len(b_variants)} to {len(c_variants)}",
                )
            for i, (bv, cv) in enumerate(zip(b_variants, c_variants)):
                compare_schemas(
                    ctx,
                    bv,
                    cv,
                    b_pointer + ptr(combiner, i),
                    c_pointer + ptr(combiner, i),
                    side,
                    depth + 1,
                )
        elif b_variants is not None or c_variants is not None:
            ctx.emit(
                c_pointer,
                "type-changed",
                "breaking",
                f"{side} '{combiner}' construct was "
                f"{'added' if b_variants is None else 'removed'}",
            )


def _describe_schema(node: Any) -> str:
    if isinstance(node, dict):
        parts = []
        if "type" in node:
            parts.append(f"type={node['type']}")
        if isinstance(node.get("enum"), list):
            parts.append(f"enum={node['enum']}")
        return ",".join(parts) or "object"
    return repr(node)


# --------------------------------------------------------------------------- #
# Parameters, request bodies, responses
# --------------------------------------------------------------------------- #

def _resolve_param(
    resolver: RefResolver, param: Any
) -> Dict[str, Any]:
    node, _ = resolver.resolve(param)
    return node if isinstance(node, dict) else {}


def _collect_parameters(
    resolver: RefResolver,
    path_item: Dict[str, Any],
    operation: Dict[str, Any],
) -> Dict[Tuple[str, str], Tuple[Dict[str, Any], str]]:
    """Return effective parameters keyed by (name, location).

    Operation-level parameters override path-level parameters sharing the
    same name and ``in`` (OpenAPI 3 specification).
    """
    result: Dict[Tuple[str, str], Tuple[Dict[str, Any], str]] = {}

    for p in path_item.get("parameters") or []:
        node = _resolve_param(resolver, p)
        if node.get("name") and node.get("in"):
            result[(node["name"], node["in"])] = (node, "")
    for p in operation.get("parameters") or []:
        node = _resolve_param(resolver, p)
        if node.get("name") and node.get("in"):
            result[(node["name"], node["in"])] = (node, "")
    return result


def compare_parameters(
    ctx: AuditContext,
    b_params: Dict[Tuple[str, str], Tuple[Dict[str, Any], str]],
    c_params: Dict[Tuple[str, str], Tuple[Dict[str, Any], str]],
    base_op_pointer: str,
    cand_op_pointer: str,
) -> None:
    for key in sorted(set(b_params) | set(c_params)):
        name, location = key
        in_b = key in b_params
        in_c = key in c_params
        if in_b and not in_c:
            ctx.emit(
                base_op_pointer + ptr("parameters"),
                "parameter-removed",
                "info",
                f"{location} parameter '{name}' was removed; the operation "
                "no longer requires clients to send it (compatible)",
            )
            continue
        if in_c and not in_b:
            c_param = c_params[key][0]
            # path parameters are always required by the OpenAPI spec;
            # a new query/header/cookie parameter only tightens requests
            # when explicitly marked required.
            required = location == "path" or bool(c_param.get("required"))
            ctx.emit(
                cand_op_pointer + ptr("parameters"),
                "parameter-added",
                "breaking" if required else "info",
                (
                    f"new required {location} parameter '{name}' on the request"
                    if required
                    else f"new optional {location} parameter '{name}' on the "
                    "request"
                ),
            )
            continue

        b_param = b_params[key][0]
        c_param = c_params[key][0]
        b_required = bool(b_param.get("required")) or location == "path"
        c_required = bool(c_param.get("required")) or location == "path"
        if c_required and not b_required:
            ctx.emit(
                cand_op_pointer + ptr("parameters"),
                "parameter-required-added",
                "breaking",
                f"{location} parameter '{name}' became required; previously "
                "requests could omit it",
            )
        elif b_required and not c_required:
            ctx.emit(
                cand_op_pointer + ptr("parameters"),
                "parameter-required-removed",
                "info",
                f"{location} parameter '{name}' is no longer required "
                "(compatible)",
            )

        b_schema = b_param.get("schema")
        c_schema = c_param.get("schema")
        if b_schema is not None and c_schema is not None:
            compare_schemas(
                ctx,
                b_schema,
                c_schema,
                base_op_pointer + ptr("parameters"),
                cand_op_pointer + ptr("parameters"),
                "request",
            )


def _json_schema(
    resolver: RefResolver, content: Any
) -> Tuple[Optional[str], Optional[Any]]:
    """Pick the schema carried by the JSON-ish media type of a content map."""
    if not isinstance(content, dict):
        return None, None
    for media in ("application/json",):
        if media in content and isinstance(content[media], dict):
            return media, content[media].get("schema")
    # Fall back to any "+json" structured media type.
    for media, item in sorted(content.items()):
        if isinstance(media, str) and media.endswith("+json") and isinstance(
            item, dict
        ):
            return media, item.get("schema")
    return None, None


def compare_request_body(
    ctx: AuditContext,
    b_body: Any,
    c_body: Any,
    base_pointer: str,
    cand_pointer: str,
) -> None:
    try:
        b_node, _ = ctx.base.resolve(b_body)
        c_node, _ = ctx.cand.resolve(c_body)
    except ReferenceError:
        return
    b_node = b_node if isinstance(b_node, dict) else {}
    c_node = c_node if isinstance(c_node, dict) else {}

    if b_node.get("required") and not c_node.get("required"):
        ctx.emit(
            cand_pointer,
            "request-body-required-removed",
            "info",
            "request body is no longer required (compatible)",
        )
    elif c_node.get("required") and not b_node.get("required"):
        ctx.emit(
            cand_pointer,
            "request-body-required-added",
            "breaking",
            "request body became required; clients that sent no body will "
            "now be rejected",
        )

    b_content = b_node.get("content")
    c_content = c_node.get("content")
    if b_content is not None and c_content is not None:
        compare_content(ctx, b_content, c_content, base_pointer, cand_pointer, "request")
    elif c_content is not None and b_content is None:
        ctx.emit(
            cand_pointer,
            "request-body-added",
            "breaking" if c_node.get("required") else "info",
            "a request body schema was introduced",
        )


def compare_content(
    ctx: AuditContext,
    b_content: Dict[str, Any],
    c_content: Dict[str, Any],
    base_pointer: str,
    cand_pointer: str,
    side: str,
) -> None:
    for media in sorted(set(b_content) | set(c_content)):
        in_b = media in b_content
        in_c = media in c_content
        if in_b and not in_c:
            ctx.emit(
                base_pointer + ptr("content", media),
                "media-type-removed",
                "breaking",
                f"{side} media type '{media}' was removed"
                + ("; clients expecting it cannot read the response"
                   if side == "response"
                   else "; clients sending it will be rejected"),
            )
            continue
        if in_c and not in_b:
            ctx.emit(
                cand_pointer + ptr("content", media),
                "media-type-added",
                "info",
                f"{side} media type '{media}' was added (compatible)",
            )
            continue

        b_mt = b_content.get(media) if isinstance(b_content.get(media), dict) else {}
        c_mt = c_content.get(media) if isinstance(c_content.get(media), dict) else {}
        b_schema = b_mt.get("schema")
        c_schema = c_mt.get("schema")
        if b_schema is None or c_schema is None:
            continue
        compare_schemas(
            ctx,
            b_schema,
            c_schema,
            base_pointer + ptr("content", media, "schema"),
            cand_pointer + ptr("content", media, "schema"),
            side,
        )


def compare_responses(
    ctx: AuditContext,
    b_responses: Any,
    c_responses: Any,
    base_op_pointer: str,
    cand_op_pointer: str,
) -> None:
    if not isinstance(b_responses, dict) or not isinstance(c_responses, dict):
        return

    codes = sorted(set(b_responses) | set(c_responses))
    for code in codes:
        in_b = code in b_responses
        in_c = code in c_responses
        # A "2X" range may cover an exact code; treat explicit/range codes
        # independently except that a missing exact code covered by a
        # matching range on the same side is not considered deleted/added.
        if in_b and not in_c and not _range_covers(c_responses, code):
            ctx.emit(
                base_op_pointer + ptr("responses", code),
                "response-removed",
                "breaking",
                f"response {code} was removed; clients handling it lose the "
                "guaranteed response",
            )
            continue
        if in_c and not in_b and not _range_covers(b_responses, code):
            ctx.emit(
                cand_op_pointer + ptr("responses", code),
                "response-added",
                "info",
                f"new response {code} documented (compatible)",
            )
            continue
        if not (in_b and in_c):
            continue

        try:
            b_node, _ = ctx.base.resolve(b_responses[code])
            c_node, _ = ctx.cand.resolve(c_responses[code])
        except ReferenceError:
            continue
        if not isinstance(b_node, dict) or not isinstance(c_node, dict):
            continue

        b_content = b_node.get("content")
        c_content = c_node.get("content")
        if b_content is not None and c_content is not None:
            compare_content(
                ctx,
                b_content,
                c_content,
                base_op_pointer + ptr("responses", code),
                cand_op_pointer + ptr("responses", code),
                "response",
            )
        elif b_content is not None and c_content is None:
            ctx.emit(
                base_op_pointer + ptr("responses", code, "content"),
                "response-body-removed",
                "breaking",
                f"response {code} no longer defines a response body schema",
            )


def _range_covers(responses: Any, code: str) -> bool:
    """True if a status code range key (e.g. ``2XX``) covers *code*."""
    if not isinstance(responses, dict) or not (len(code) == 3 and code.isdigit()):
        return False
    for key in responses:
        if (
            isinstance(key, str)
            and len(key) == 3
            and key[0].isdigit()
            and key[1:].upper() == "XX"
            and key[0] == code[0]
        ):
            return True
    return False


# --------------------------------------------------------------------------- #
# Document-level audit
# --------------------------------------------------------------------------- #

def _resolve_path_item(resolver: RefResolver, item: Any) -> Dict[str, Any]:
    node, _ = resolver.resolve(item)
    return node if isinstance(node, dict) else {}


def compare_documents(
    baseline: Dict[str, Any], candidate: Dict[str, Any]
) -> List[Finding]:
    ctx = AuditContext(
        base_doc=baseline,
        cand_doc=candidate,
        base=RefResolver(baseline, label="baseline"),
        cand=RefResolver(candidate, label="candidate"),
    )

    b_paths = baseline.get("paths") if isinstance(baseline.get("paths"), dict) else {}
    c_paths = candidate.get("paths") if isinstance(candidate.get("paths"), dict) else {}

    for url_path in sorted(set(b_paths) | set(c_paths)):
        b_item_raw = b_paths.get(url_path)
        c_item_raw = c_paths.get(url_path)

        if b_item_raw is not None and c_item_raw is None:
            ctx.emit(
                ptr("paths", url_path),
                "path-removed",
                "breaking",
                f"path '{url_path}' and all of its operations were removed",
            )
            continue
        if c_item_raw is not None and b_item_raw is None:
            ctx.emit(
                ptr("paths", url_path),
                "path-added",
                "info",
                f"new path '{url_path}' was added (compatible)",
            )
            continue

        try:
            b_item = _resolve_path_item(ctx.base, b_item_raw)
            c_item = _resolve_path_item(ctx.cand, c_item_raw)
        except ReferenceError:
            continue

        b_methods = {m for m in b_item if m.lower() in HTTP_METHODS}
        c_methods = {m for m in c_item if m.lower() in HTTP_METHODS}

        for method in sorted(b_methods - c_methods, key=str.lower):
            ctx.emit(
                ptr("paths", url_path, method.lower()),
                "operation-removed",
                "breaking",
                f"operation {method.upper()} {url_path} was removed",
            )
        for method in sorted(c_methods - b_methods, key=str.lower):
            ctx.emit(
                ptr("paths", url_path, method.lower()),
                "operation-added",
                "info",
                f"new operation {method.upper()} {url_path} (compatible)",
            )

        for method in sorted(b_methods & c_methods, key=str.lower):
            b_op = b_item[method] if isinstance(b_item[method], dict) else {}
            c_op = c_item[method] if isinstance(c_item[method], dict) else {}
            base_op_pointer = ptr("paths", url_path, method.lower())
            cand_op_pointer = ptr("paths", url_path, method.lower())

            try:
                b_params = _collect_parameters(ctx.base, b_item, b_op)
                c_params = _collect_parameters(ctx.cand, c_item, c_op)
            except ReferenceError:
                continue
            compare_parameters(
                ctx, b_params, c_params, base_op_pointer, cand_op_pointer
            )

            b_body = b_op.get("requestBody")
            c_body = c_op.get("requestBody")
            if b_body is not None and c_body is not None:
                compare_request_body(
                    ctx, b_body, c_body, base_op_pointer, cand_op_pointer
                )
            elif c_body is not None and b_body is None:
                try:
                    c_node, _ = ctx.cand.resolve(c_body)
                except ReferenceError:
                    c_node = {}
                ctx.emit(
                    cand_op_pointer + ptr("requestBody"),
                    "request-body-added",
                    "breaking"
                    if isinstance(c_node, dict) and c_node.get("required")
                    else "info",
                    "a request body was introduced on the operation",
                )

            compare_responses(
                ctx,
                b_op.get("responses"),
                c_op.get("responses"),
                base_op_pointer,
                cand_op_pointer,
            )

    return _sort_findings(ctx.findings)


def _sort_findings(findings: List[Finding]) -> List[Finding]:
    """Stable ordering: JSON Pointer, then category, then reason."""
    return sorted(findings, key=lambda f: (f.pointer, f.category, f.reason))


# --------------------------------------------------------------------------- #
# Whole-document reference validation
# --------------------------------------------------------------------------- #

def _walk_refs(
    doc: Any,
    resolver: RefResolver,
    label: str,
    expanded: Optional[Set[str]] = None,
    chain: Tuple[str, ...] = (),
) -> None:
    """Validate every ``$ref`` reachable from *doc*.

    Recursive schemas (``Pet`` -> ``parent`` -> ``#/components/schemas/Pet``)
    are a legitimate OpenAPI pattern: when a reference repeats inside the
    current chain the branch simply stops expanding instead of erroring.
    Fully validated subtrees are skipped through *expanded*. Only a pointer
    that cannot actually be followed (or a chain exceeding
    :data:`MAX_REF_CHAIN`) raises.
    """
    if expanded is None:
        expanded = set()

    if isinstance(doc, dict) and isinstance(doc.get("$ref"), str):
        ref = doc["$ref"]
        if "://" in ref or ref.startswith("//") or not ref.startswith("#"):
            raise UnresolvableReferenceError(
                ref,
                f"[{label}] unsupported non-local reference '{ref}': only "
                "document-local '#/...' references are supported",
            )
        if ref in chain:
            # Legitimate recursion (e.g. Pet.parent -> Pet): stop here.
            return
        if ref in expanded:
            return
        # resolve() raises on dangling pointers or runaway chains.
        target, target_pointer = resolver.resolve(doc, chain)
        expanded.add(ref)
        if target_pointer:
            expanded.add("#" + target_pointer)
        _walk_refs(target, resolver, label, expanded, chain + (ref,))
        return

    if isinstance(doc, dict):
        for value in doc.values():
            _walk_refs(value, resolver, label, expanded, chain)
    elif isinstance(doc, list):
        for value in doc:
            _walk_refs(value, resolver, label, expanded, chain)


def validate_references(doc: Dict[str, Any], label: str) -> List[Dict[str, str]]:
    """Return a list of unresolvable-reference error descriptions.

    Circular references by themselves are accepted (they are common in
    recursive schemas and the audit traversal is cycle-safe); only pointers
    that cannot be resolved produce errors.
    """
    errors: List[Dict[str, str]] = []
    resolver = RefResolver(doc, label=label)
    try:
        _walk_refs(doc, resolver, label)
    except UnresolvableReferenceError as exc:
        errors.append({"document": label, "ref": exc.ref, "error": str(exc)})
    except CircularReferenceError as exc:
        # Reached only for a runaway chain longer than MAX_REF_CHAIN.
        errors.append({"document": label, "ref": exc.ref, "error": str(exc)})
    return errors
