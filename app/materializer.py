"""HTTP request materialization for one OpenAPI operation (``POST /requests/materialize``).

Given an OpenAPI 3.x document, a ``METHOD /path`` operation selector and a
set of grouped parameter values, build the concrete HTTP request pieces:

* ``request_target`` -- the origin-form target (path template substituted
  with the path parameters, plus the serialized query string);
* ``headers`` -- one entry per materialized ``in: header`` parameter;
* ``cookie`` -- the serialized ``Cookie`` header value assembled from the
  ``in: cookie`` parameters.

Serialization follows the OpenAPI 3 parameter serialization rules:
default ``style`` / ``explode`` per location, path ``simple`` / ``label`` /
``matrix``, query ``form`` / ``spaceDelimited`` / ``pipeDelimited`` /
``deepObject``, header ``simple`` and cookie ``form``; arrays, objects,
``allowReserved``, repeated query keys and percent-encoding are all
handled. Path-level parameters are merged with operation-level ones; the
operation-level parameter sharing ``(in, name)`` overrides the path-level
one.

Local ``$ref`` resolution (parameters and 3.1 path items) is cycle-safe.
Every semantic failure is collected and rejected together with HTTP 422
and a source JSON Pointer.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

from .refresolver import (
    MAX_REF_CHAIN,
    RefResolver,
    UnresolvableReferenceError,
    is_local_ref,
    parse_local_ref,
)
from .slicer import HTTP_METHODS, parse_selector

GROUPS = ("path", "query", "header", "cookie")

DEFAULT_STYLE = {
    "path": "simple",
    "query": "form",
    "header": "simple",
    "cookie": "form",
}

ALLOWED_STYLES = {
    "path": frozenset({"simple", "label", "matrix"}),
    "query": frozenset(
        {"form", "spaceDelimited", "pipeDelimited", "deepObject"}
    ),
    "header": frozenset({"simple"}),
    "cookie": frozenset({"form"}),
}

# RFC 3986 unreserved characters; everything else is percent-encoded
# unless the style delimiter itself is emitted structurally or query
# ``allowReserved`` admits the reserved set raw.
UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
RESERVED = frozenset(":/?#[]@!$&'()*+,;=")

# RFC 7230 token separators (a header name must be a token).
_HEADER_SEPARATORS = frozenset('()<>@,;:\\"/[]?={} \t')
_TEMPLATE_RE = re.compile(r"\{([^{}]+)\}")


def _is_http_token(name: str) -> bool:
    """True when *name* is an RFC 7230 token usable as a header field name."""
    return bool(name) and all(
        ord(ch) >= 33 and ord(ch) != 127 and ch not in _HEADER_SEPARATORS
        for ch in name
    )


def _is_cookie_name(name: str) -> bool:
    """True when *name* is safe as a cookie name (no '=', whitespace, CTL)."""
    return bool(name) and all(
        c != "=" and c not in " \t\r\n" and 33 <= ord(c) != 127 for c in name
    )


class MaterializeError(Exception):
    """Base class for materialization request failures."""


class InvalidDocumentError(MaterializeError):
    """The submitted document is not a recognizable OpenAPI 3.x doc (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class InvalidSelectorError(MaterializeError):
    """The selector is malformed or unknown (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class InvalidRequestError(MaterializeError):
    """The request envelope (``values``) is malformed (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class MaterializationError(MaterializeError):
    """One or more semantic materialization problems (HTTP 422)."""

    def __init__(self, errors: List[Dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(
            "; ".join(f"{e['code']} @ {e.get('pointer', '')}" for e in errors)
        )


# --------------------------------------------------------------------------- #
# JSON Pointer helpers (RFC 6901)
# --------------------------------------------------------------------------- #

def escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def pointer_join(base: str, token: str) -> str:
    return base + "/" + escape_token(token)


# --------------------------------------------------------------------------- #
# Cycle-safe $ref resolution with OpenAPI 3.1 sibling merging
# --------------------------------------------------------------------------- #

def _resolve_chain(
    doc: Dict[str, Any],
    node: Any,
    node_pointer: str,
    *,
    merge_siblings: bool,
) -> Tuple[Dict[str, Any], str, Optional[Dict[str, Any]]]:
    """Follow a local ``$ref`` chain starting at *node*.

    Returns ``(merged_node, terminal_pointer, error)`` with exactly one of
    *merged_node* / *error* unset. When *merge_siblings* is set (OpenAPI
    3.1 documents), sibling fields on every layer are overlaid from the
    terminal target back to the referencing node, so nearer siblings win;
    otherwise the terminal target is used verbatim (OpenAPI 3.0 ignores
    siblings of a ``$ref``).
    """
    resolver = RefResolver(doc)
    layers: List[Tuple[Dict[str, Any], str]] = []
    current = node
    current_pointer = node_pointer
    seen: Dict[str, int] = {}
    refs: List[str] = []

    while isinstance(current, dict) and isinstance(current.get("$ref"), str):
        ref = current["$ref"]
        if not is_local_ref(ref):
            return None, None, {
                "code": "unresolvable_reference",
                "pointer": current_pointer,
                "ref": ref,
                "error": (
                    f"unsupported non-local reference '{ref}': only document-"
                    "local references of the form '#/...' are supported"
                ),
            }
        target_pointer = parse_local_ref(ref)
        if target_pointer in seen:
            loop_start = seen[target_pointer]
            loop = refs[loop_start:] + [ref]
            return None, None, {
                "code": "unresolvable_reference",
                "pointer": current_pointer,
                "ref": ref,
                "error": "circular reference detected: " + " -> ".join(loop),
            }
        if len(refs) >= MAX_REF_CHAIN:
            return None, None, {
                "code": "unresolvable_reference",
                "pointer": current_pointer,
                "ref": ref,
                "error": (
                    f"reference chain exceeds the maximum depth of "
                    f"{MAX_REF_CHAIN} links (last reference: {ref})"
                ),
            }
        seen[target_pointer] = len(refs)
        refs.append(ref)
        try:
            target = resolver.pointer(target_pointer)
        except UnresolvableReferenceError as exc:
            return None, None, {
                "code": "unresolvable_reference",
                "pointer": current_pointer,
                "ref": exc.ref,
                "error": str(exc),
            }
        if not isinstance(target, dict):
            return None, None, {
                "code": "unresolvable_reference",
                "pointer": current_pointer,
                "ref": ref,
                "error": f"reference '{ref}' does not target an object",
            }
        layers.append((current, current_pointer))
        current = target
        current_pointer = target_pointer

    if not isinstance(current, dict):
        return None, None, {
            "code": "unresolvable_reference",
            "pointer": current_pointer,
            "ref": refs[-1] if refs else None,
            "error": "reference target must be an object",
        }

    layers.append((current, current_pointer))
    if merge_siblings:
        merged: Dict[str, Any] = {}
        for layer_node, _ in reversed(layers):
            for key, value in layer_node.items():
                if key != "$ref":
                    merged[key] = copy.deepcopy(value)
        result = merged
    else:
        result = copy.deepcopy(current)
    return result, current_pointer, None


def _resolve_path_item(
    doc: Dict[str, Any], raw_item: Any, item_pointer: str
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Resolve the effective path item, cycle-safely merging 3.1 layers.

    Path-level ``$ref`` chains merge sibling fields at every layer
    (terminal target first, nearer referencing nodes win). Returns
    ``(path_item, error)`` with exactly one member set.
    """
    if not isinstance(raw_item, dict):
        return None, {
            "code": "invalid_parameter",
            "pointer": item_pointer,
            "error": "path item must be an object",
        }
    if not isinstance(raw_item.get("$ref"), str):
        return raw_item, None
    merged, _terminal, error = _resolve_chain(
        doc, raw_item, item_pointer, merge_siblings=True
    )
    if error is not None:
        return None, error
    return merged, None


# --------------------------------------------------------------------------- #
# Value typing and percent-encoding
# --------------------------------------------------------------------------- #

def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def to_text(value: Any) -> str:
    """Render a JSON primitive the way OpenAPI serializes it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    # str before int is unnecessary in Python; keep the order explicit.
    return str(value)


def pct_encode(text: str, extra_safe: str = "") -> str:
    """Percent-encode *text* (UTF-8), keeping unreserved chars raw."""
    safe = set(UNRESERVED) | set(extra_safe)
    out: List[str] = []
    for byte in text.encode("utf-8"):
        char = chr(byte)
        if char in safe:
            out.append(char)
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Parameter model
# --------------------------------------------------------------------------- #

class Parameter:
    """One effective parameter after path/operation merging."""

    __slots__ = ("location", "name", "node", "pointer")

    def __init__(
        self,
        location: str,
        name: str,
        node: Dict[str, Any],
        pointer: str,
    ) -> None:
        self.location = location
        self.name = name
        self.node = node
        # JSON Pointer of the effective parameter definition in the
        # document (a $ref is followed to the component location, matching
        # the convention used by /audit).
        self.pointer = pointer

    def style_explode(self) -> Tuple[str, bool]:
        style = self.node.get("style")
        if not isinstance(style, str) or not style:
            style = DEFAULT_STYLE[self.location]
        explode = self.node.get("explode")
        if explode is None:
            # 'form' and 'deepObject' default to explode=true; every other
            # style defaults to false (deepObject is only defined in its
            # exploded form).
            explode = style in ("form", "deepObject")
        return style, bool(explode)

    @property
    def required(self) -> bool:
        # Path parameters are always required by the OpenAPI specification.
        return self.location == "path" or bool(self.node.get("required"))


# --------------------------------------------------------------------------- #
# Document-level collection
# --------------------------------------------------------------------------- #

def _validate_shape(doc: Any) -> List[str]:
    if not isinstance(doc, dict):
        return ["document must be a JSON object"]
    messages: List[str] = []
    version = doc.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        messages.append(
            "document.openapi must be a version string starting with '3.' "
            f"(OpenAPI 3.x), got {version!r}"
        )
    if not isinstance(doc.get("paths"), dict):
        messages.append("document.paths must be an object")
    return messages


def _resolve_occurrences(
    doc: Dict[str, Any],
    raw_parameters: Any,
    base_pointer: str,
    is31: bool,
    errors: List[Dict[str, Any]],
) -> List[Tuple[Dict[str, Any], str]]:
    """Resolve each parameter array element through its ``$ref`` chain."""
    resolved: List[Tuple[Dict[str, Any], str]] = []
    if not raw_parameters:
        return resolved
    if not isinstance(raw_parameters, list):
        errors.append(
            {
                "code": "invalid_parameter",
                "pointer": base_pointer,
                "error": "'parameters' must be an array",
            }
        )
        return resolved
    for index, raw_param in enumerate(raw_parameters):
        origin_pointer = pointer_join(base_pointer, str(index))
        if not isinstance(raw_param, dict):
            errors.append(
                {
                    "code": "invalid_parameter",
                    "pointer": origin_pointer,
                    "error": "parameter must be an object",
                }
            )
            continue
        node, terminal_pointer, error = _resolve_chain(
            doc,
            raw_param,
            origin_pointer,
            merge_siblings=is31,
        )
        if error is not None:
            errors.append(error)
            continue
        if not isinstance(node, dict):
            errors.append(
                {
                    "code": "invalid_parameter",
                    "pointer": origin_pointer,
                    "error": "parameter must be an object",
                }
            )
            continue
        resolved.append((node, terminal_pointer))
    return resolved


def _build_parameters(
    doc: Dict[str, Any],
    path_item: Dict[str, Any],
    operation: Dict[str, Any],
    path_pointer: str,
    op_pointer: str,
    is31: bool,
) -> Tuple[List[Parameter], List[Dict[str, Any]]]:
    """Merge path-level and operation-level parameters.

    Operation-level parameters override path-level ones sharing
    ``(in, name)``. Duplicate keys *within the same level* are rejected.
    The effective order is path-level parameters (document order) followed
    by operation-only parameters (document order).
    """
    errors: List[Dict[str, Any]] = []
    path_occurrences = _resolve_occurrences(
        doc,
        path_item.get("parameters"),
        pointer_join(path_pointer, "parameters"),
        is31,
        errors,
    )
    op_occurrences = _resolve_occurrences(
        doc,
        operation.get("parameters"),
        pointer_join(op_pointer, "parameters"),
        is31,
        errors,
    )

    def index_level(
        occurrences: List[Tuple[Dict[str, Any], str]],
        level: str,
    ) -> Dict[Tuple[str, str], Tuple[Dict[str, Any], str]]:
        indexed: Dict[Tuple[str, str], Tuple[Dict[str, Any], str]] = {}
        for node, pointer in occurrences:
            name = node.get("name")
            location = node.get("in")
            if not isinstance(name, str) or not name:
                errors.append(
                    {
                        "code": "invalid_parameter",
                        "pointer": pointer,
                        "error": "parameter is missing a string 'name'",
                    }
                )
                continue
            if location not in GROUPS:
                errors.append(
                    {
                        "code": "invalid_parameter",
                        "pointer": pointer,
                        "in": location if isinstance(location, str) else None,
                        "name": name,
                        "error": (
                            "parameter 'in' must be one of path/query/header/"
                            f"cookie, got {location!r}"
                        ),
                    }
                )
                continue
            key = (location, name)
            if key in indexed:
                errors.append(
                    {
                        "code": "duplicate_parameter",
                        "pointer": pointer,
                        "first_pointer": indexed[key][1],
                        "in": location,
                        "name": name,
                        "error": (
                            f"duplicate {level}-level {location} parameter "
                            f"'{name}'; a parameter list must not declare the "
                            "same (in, name) twice"
                        ),
                    }
                )
                continue
            indexed[key] = (node, pointer)
        return indexed

    path_index = index_level(path_occurrences, "path")
    op_index = index_level(op_occurrences, "operation")

    effective: List[Parameter] = []
    for (location, name), (node, pointer) in path_index.items():
        if (location, name) in op_index:
            # Operation-level parameter overrides the path-level one.
            op_node, op_pointer_def = op_index[(location, name)]
            effective.append(
                Parameter(location, name, op_node, op_pointer_def)
            )
        else:
            effective.append(
                Parameter(location, name, node, pointer)
            )
    for (location, name), (node, pointer) in op_index.items():
        if (location, name) not in path_index:
            effective.append(
                Parameter(location, name, node, pointer)
            )
    return effective, errors


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #

def _object_pairs(value: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """Sorted (key, primitive-value) pairs; nested values are rejected."""
    pairs: List[Tuple[str, Any]] = []
    for key in sorted(value):
        item = value[key]
        if isinstance(item, (dict, list)) or item is None:
            raise _MaterializeValueError(
                f"object property '{key}' must be a primitive value; nested "
                "arrays, objects and null are not serializable in parameter "
                "styles"
            )
        pairs.append((key, item))
    return pairs


class _MaterializeValueError(Exception):
    """A supplied value cannot be serialized with its declared style."""


def _primitive(value: Any, where: str) -> str:
    if isinstance(value, (dict, list)) or value is None:
        raise _MaterializeValueError(
            f"{where} expects a primitive value (string/number/boolean), "
            f"got {_json_type(value)}"
        )
    return to_text(value)


def _array_texts(value: List[Any], where: str) -> List[str]:
    """Validate that an array holds only primitive values, render each."""
    return [
        _primitive(item, where) for item in value
    ]


def _encode_path(text: str) -> str:
    return pct_encode(text)


def serialize_path_fragment(
    name: str, style: str, explode: bool, value: Any
) -> str:
    """Serialize an ``in: path`` value into the template substitution."""
    vtype = _json_type(value)
    if style == "simple":
        if vtype == "array":
            texts = [
                _encode_path(t)
                for t in _array_texts(value, "path style 'simple' array item")
            ]
            return ",".join(texts)
        if vtype == "object":
            pairs = _object_pairs(value)
            if explode:
                return ",".join(
                    f"{_encode_path(k)}={_encode_path(to_text(v))}"
                    for k, v in pairs
                )
            return ",".join(
                _encode_path(part)
                for k, v in pairs
                for part in (k, to_text(v))
            )
        return _encode_path(_primitive(value, "path style 'simple'"))

    if style == "label":
        prefix = "."
        if vtype == "array":
            texts = [
                _encode_path(t)
                for t in _array_texts(value, "path style 'label' array item")
            ]
            return prefix + (".".join(texts) if explode else ",".join(texts))
        if vtype == "object":
            pairs = _object_pairs(value)
            if explode:
                return prefix + ".".join(
                    f"{_encode_path(k)}={_encode_path(to_text(v))}"
                    for k, v in pairs
                )
            return prefix + ",".join(
                _encode_path(part)
                for k, v in pairs
                for part in (k, to_text(v))
            )
        return prefix + _encode_path(_primitive(value, "path style 'label'"))

    # matrix
    if vtype == "array":
        texts = [
            _encode_path(t)
            for t in _array_texts(value, "path style 'matrix' array item")
        ]
        if explode:
            return "".join(
                f";{_encode_path(name)}={text}" for text in texts
            )
        return f";{_encode_path(name)}=" + ",".join(texts)
    if vtype == "object":
        pairs = _object_pairs(value)
        if explode:
            return "".join(
                f";{_encode_path(k)}={_encode_path(to_text(v))}"
                for k, v in pairs
            )
        return (
            f";{_encode_path(name)}="
            + ",".join(
                _encode_path(part)
                for k, v in pairs
                for part in (k, to_text(v))
            )
        )
    return (
        f";{_encode_path(name)}="
        + _encode_path(_primitive(value, "path style 'matrix'"))
    )


def serialize_query_pairs(
    name: str,
    style: str,
    explode: bool,
    allow_reserved: bool,
    value: Any,
) -> List[str]:
    """Serialize an ``in: query`` value into ``key=value`` pair strings."""
    extra = "".join(RESERVED) if allow_reserved else ""

    def enc(text: str) -> str:
        return pct_encode(text, extra)

    vtype = _json_type(value)

    if style == "deepObject":
        if not explode:
            raise _MaterializeValueError(
                "style 'deepObject' always uses exploded form; "
                "'explode: false' is not defined for it"
            )
        if vtype != "object":
            raise _MaterializeValueError(
                "style 'deepObject' requires an object value, got "
                f"{vtype}"
            )
        pairs = _object_pairs(value)
        return [
            f"{enc(name)}[{enc(k)}]={enc(to_text(v))}" for k, v in pairs
        ]

    if style in ("spaceDelimited", "pipeDelimited"):
        if vtype != "array":
            raise _MaterializeValueError(
                f"style '{style}' requires an array value, got {vtype}"
            )
        texts = [
            enc(t)
            for t in _array_texts(value, f"query style '{style}' array item")
        ]
        if explode:
            return [f"{enc(name)}={text}" for text in texts]
        delimiter = "%20" if style == "spaceDelimited" else "|"
        return [f"{enc(name)}={delimiter.join(texts)}"]

    # form
    if vtype == "array":
        texts = [
            enc(t) for t in _array_texts(value, "query style form array item")
        ]
        if explode:
            return [f"{enc(name)}={text}" for text in texts]
        return [f"{enc(name)}=" + ",".join(texts)]
    if vtype == "object":
        pairs = _object_pairs(value)
        if explode:
            return [
                f"{enc(k)}={enc(to_text(v))}" for k, v in pairs
            ]
        return [
            f"{enc(name)}="
            + ",".join(
                enc(part)
                for k, v in pairs
                for part in (k, to_text(v))
            )
        ]
    return [f"{enc(name)}={enc(_primitive(value, 'query style form'))}"]


def serialize_header(name: str, explode: bool, value: Any) -> str:
    """Serialize an ``in: header`` value (simple style, no encoding)."""
    vtype = _json_type(value)
    if vtype == "array":
        texts = _array_texts(value, "header array item")
        text = ",".join(texts)
    elif vtype == "object":
        pairs = _object_pairs(value)
        if explode:
            text = ",".join(f"{k}={to_text(v)}" for k, v in pairs)
        else:
            text = ",".join(
                part for k, v in pairs for part in (k, to_text(v))
            )
    else:
        text = _primitive(value, "header parameter")
    for bad in ("\r", "\n", "\x00"):
        if bad in text:
            raise _MaterializeValueError(
                "header values must not contain CR, LF or NUL characters"
            )
    return text


def serialize_cookie_pairs(name: str, value: Any) -> str:
    """Serialize one ``in: cookie`` parameter into a cookie ``name=value``.

    Cookies only support the form style; the explode flag is ignored by
    the OpenAPI cookie table, so arrays render ``a,b`` and objects
    ``k1,v1,k2,v2``. Tokens are percent-encoded with the unreserved set
    so the assembled Cookie header stays unambiguous.
    """
    vtype = _json_type(value)

    def enc(text: str) -> str:
        return pct_encode(text)

    if vtype == "array":
        texts = [
            enc(t) for t in _array_texts(value, "cookie array item")
        ]
        body = ",".join(texts)
    elif vtype == "object":
        pairs = _object_pairs(value)
        body = ",".join(
            enc(part) for k, v in pairs for part in (k, to_text(v))
        )
    else:
        body = enc(_primitive(value, "cookie parameter"))
    return f"{enc(name)}={body}"


# --------------------------------------------------------------------------- #
# End-to-end materialization
# --------------------------------------------------------------------------- #

def _value_pointer(location: str, name: str) -> str:
    return pointer_join(pointer_join("/values", location), name)


def _header_lookup(
    values: Dict[str, Any], name: str
) -> Tuple[bool, Any, Optional[str]]:
    """Case-insensitive header value lookup.

    Returns ``(found, value, matched_key)``. Two request keys matching the
    same header name are reported by the caller.
    """
    lowered = name.lower()
    matches = [key for key in values if isinstance(key, str) and key.lower() == lowered]
    if not matches:
        return False, None, None
    return True, values[matches[0]], matches[0]


def materialize_request(
    doc: Any, selector: Any, values: Optional[Dict[str, Any]]
) -> Tuple[str, Dict[str, str], str]:
    """Materialize one operation.

    :returns ``(request_target, headers, cookie)``.
    :raises InvalidDocumentError: *doc* is not OpenAPI 3.x.
    :raises InvalidSelectorError: the selector is malformed/unknown (400).
    :raises InvalidRequestError: *values* has a malformed envelope (400).
    :raises MaterializationError: semantic problems (422), each with a
        source JSON Pointer.
    """
    shape_errors = _validate_shape(doc)
    if shape_errors:
        raise InvalidDocumentError(shape_errors)

    parsed = parse_selector(selector)
    if parsed is None:
        raise InvalidSelectorError(
            [
                f"malformed selector {selector!r}: expected 'METHOD /path' "
                "(e.g. 'GET /pets/{petId}')"
            ]
        )
    method, path = parsed

    selector_errors: List[str] = []
    if path not in doc["paths"]:
        selector_errors.append(
            f"{method.upper()} {path}: path is not declared"
        )
    if values is None:
        values = {}
    if not isinstance(values, dict):
        raise InvalidRequestError(["values must be a JSON object"])
    bad_groups = [
        key for key in values if not isinstance(key, str) or key not in GROUPS
    ]
    if bad_groups:
        raise InvalidRequestError(
            [
                f"values.{key}: unknown parameter group; expected one of "
                "path/query/header/cookie"
                for key in sorted(bad_groups, key=str)
            ]
        )
    group_errors: List[str] = []
    for group in GROUPS:
        group_value = values.get(group, {})
        if group_value is None:
            values[group] = {}
            continue
        if not isinstance(group_value, dict):
            group_errors.append(
                f"values.{group} must be an object mapping parameter names "
                "to values"
            )
    if group_errors:
        raise InvalidRequestError(group_errors)
    if selector_errors:
        raise InvalidSelectorError(selector_errors)

    is31 = str(doc.get("openapi", "")).startswith(("3.1", "3.2"))
    path_pointer = pointer_join("/paths", path)
    raw_item = doc["paths"][path]
    path_item, item_error = _resolve_path_item(doc, raw_item, path_pointer)
    errors: List[Dict[str, Any]] = []
    if item_error is not None:
        errors.append(item_error)
        raise MaterializationError(_sorted(errors))

    method_keys = {k.lower(): k for k in path_item if k.lower() in HTTP_METHODS}
    if method not in method_keys:
        raise InvalidSelectorError(
            [f"{method.upper()} {path}: operation is not declared"]
        )
    actual_method_key = method_keys[method]
    operation = path_item[actual_method_key]
    op_pointer = pointer_join(path_pointer, actual_method_key)
    if not isinstance(operation, dict):
        raise MaterializationError(
            _sorted(
                [
                    {
                        "code": "invalid_parameter",
                        "pointer": op_pointer,
                        "error": "operation must be an object",
                    }
                ]
            )
        )

    parameters, param_errors = _build_parameters(
        doc, path_item, operation, path_pointer, op_pointer, is31
    )
    errors.extend(param_errors)

    # --- Static parameter validation (style matrix, names, content) ---- #
    for param in parameters:
        if "content" in param.node:
            errors.append(
                {
                    "code": "unsupported_parameter",
                    "pointer": param.pointer,
                    "in": param.location,
                    "name": param.name,
                    "error": (
                        "parameters using 'content' media type objects are "
                        "not supported; declare 'schema' with a style"
                    ),
                }
            )
            continue
        raw_style = param.node.get("style", DEFAULT_STYLE[param.location])
        if not isinstance(raw_style, str) or not raw_style:
            errors.append(
                {
                    "code": "invalid_parameter",
                    "pointer": pointer_join(param.pointer, "style"),
                    "in": param.location,
                    "name": param.name,
                    "error": "'style' must be a non-empty string",
                }
            )
        raw_explode = param.node.get("explode")
        if raw_explode is not None and not isinstance(raw_explode, bool):
            errors.append(
                {
                    "code": "invalid_parameter",
                    "pointer": pointer_join(param.pointer, "explode"),
                    "in": param.location,
                    "name": param.name,
                    "error": "'explode' must be a boolean",
                }
            )
        style, _explode = param.style_explode()
        if isinstance(raw_style, str) and raw_style and style not in ALLOWED_STYLES[param.location]:
            errors.append(
                {
                    "code": "unsupported_style",
                    "pointer": pointer_join(param.pointer, "style"),
                    "in": param.location,
                    "name": param.name,
                    "style": style,
                    "error": (
                        f"style '{style}' is not valid for {param.location} "
                        "parameters"
                    ),
                }
            )
        if param.location == "header" and not _is_http_token(param.name):
            errors.append(
                {
                    "code": "invalid_parameter",
                    "pointer": pointer_join(param.pointer, "name"),
                    "in": param.location,
                    "name": param.name,
                    "error": "header parameter name is not a valid HTTP token",
                }
            )
        if param.location == "cookie" and not _is_cookie_name(param.name):
            errors.append(
                {
                    "code": "invalid_parameter",
                    "pointer": pointer_join(param.pointer, "name"),
                    "in": param.location,
                    "name": param.name,
                    "error": "cookie parameter name contains illegal characters",
                }
            )

    # --- Value bookkeeping: required, unknown, ambiguous --------------- #
    declared = {(p.location, p.name): p for p in parameters}
    header_declared = {p.name.lower(): p for p in parameters if p.location == "header"}

    for location in GROUPS:
        supplied = values.get(location) or {}
        if location == "header":
            # Detect request keys that differ only in case.
            seen_lower: Dict[str, str] = {}
            for key in supplied:
                if not isinstance(key, str):
                    continue
                lowered = key.lower()
                if lowered in seen_lower and seen_lower[lowered] != key:
                    errors.append(
                        {
                            "code": "ambiguous_value",
                            "pointer": _value_pointer(location, key),
                            "in": location,
                            "name": key,
                            "error": (
                                f"header values '{seen_lower[lowered]}' and "
                                f"'{key}' differ only in case; supply one key"
                            ),
                        }
                    )
                seen_lower[lowered] = key
            for key in supplied:
                if not isinstance(key, str):
                    continue
                if key.lower() not in header_declared:
                    errors.append(
                        {
                            "code": "unknown_value",
                            "pointer": _value_pointer(location, key),
                            "in": location,
                            "name": key,
                            "error": (
                                f"the operation declares no header parameter "
                                f"named '{key}'"
                            ),
                        }
                    )
        else:
            for key in supplied:
                if not isinstance(key, str):
                    continue
                if (location, key) not in declared:
                    errors.append(
                        {
                            "code": "unknown_value",
                            "pointer": _value_pointer(location, key),
                            "in": location,
                            "name": key,
                            "error": (
                                f"the operation declares no {location} "
                                f"parameter named '{key}'"
                            ),
                        }
                    )

    # Header-name clashes differing only in case inside the contract.
    seen_header_names: Dict[str, Parameter] = {}
    for param in parameters:
        if param.location != "header":
            continue
        lowered = param.name.lower()
        if lowered in seen_header_names:
            errors.append(
                {
                    "code": "duplicate_parameter",
                    "pointer": param.pointer,
                    "first_pointer": seen_header_names[lowered].pointer,
                    "in": "header",
                    "name": param.name,
                    "error": (
                        f"header parameters '{seen_header_names[lowered].name}' "
                        f"and '{param.name}' differ only in case and would map "
                        "to the same HTTP header"
                    ),
                }
            )
        else:
            seen_header_names[lowered] = param

    def lookup(param: Parameter) -> Tuple[bool, Any]:
        supplied = values.get(param.location) or {}
        if param.location == "header":
            found, value, _ = _header_lookup(supplied, param.name)
            return found, value
        return param.name in supplied, supplied.get(param.name)

    for param in parameters:
        found, value = lookup(param)
        if not found or value is None:
            # An explicit JSON null means "no value": required parameters
            # then miss their mandatory value, optional ones are skipped.
            if param.required:
                errors.append(
                    {
                        "code": "missing_required_value",
                        "pointer": param.pointer,
                        "in": param.location,
                        "name": param.name,
                        "error": (
                            f"missing value for required {param.location} "
                            f"parameter '{param.name}'"
                        ),
                    }
                )
            continue
        style, explode = param.style_explode()
        if style not in ALLOWED_STYLES[param.location]:
            continue  # unsupported_style already reported above
        try:
            if param.location == "path":
                serialize_path_fragment(param.name, style, explode, value)
            elif param.location == "query":
                serialize_query_pairs(
                    param.name,
                    style,
                    explode,
                    bool(param.node.get("allowReserved")),
                    value,
                )
            elif param.location == "header":
                serialize_header(param.name, explode, value)
            else:
                serialize_cookie_pairs(param.name, value)
        except _MaterializeValueError as exc:
            errors.append(
                {
                    "code": "invalid_value_shape",
                    "pointer": _value_pointer(param.location, param.name),
                    "in": param.location,
                    "name": param.name,
                    "error": str(exc),
                }
            )

    # --- Path template / declared path parameter consistency ----------- #
    template_names = _TEMPLATE_RE.findall(path)
    # A stray '{' / '}' or an empty '{}' expression is malformed: remove
    # every well-formed '{name}' token and see whether braces remain.
    leftover = _TEMPLATE_RE.sub("", path)
    template_malformed = ("{" in leftover or "}" in leftover) or ("{}" in path)
    if template_malformed:
        errors.append(
            {
                "code": "invalid_path_template",
                "pointer": path_pointer,
                "error": (
                    f"path template '{path}' is malformed: expressions must "
                    "be non-empty '{name}' tokens"
                ),
            }
        )
    path_param_names = {p.name for p in parameters if p.location == "path"}
    for template_name in template_names:
        if template_name not in path_param_names:
            errors.append(
                {
                    "code": "invalid_path_template",
                    "pointer": path_pointer,
                    "in": "path",
                    "name": template_name,
                    "error": (
                        f"path template variable '{{{template_name}}}' has no "
                        "corresponding path parameter declaration"
                    ),
                }
            )
    for param in parameters:
        if (
            param.location == "path"
            and param.name not in template_names
        ):
            errors.append(
                {
                    "code": "invalid_path_template",
                    "pointer": param.pointer,
                    "in": "path",
                    "name": param.name,
                    "error": (
                        f"path parameter '{param.name}' is not used by the "
                        f"path template '{path}'"
                    ),
                }
            )

    if errors:
        raise MaterializationError(_sorted(errors))

    # ------------------------------------------------------------------ #
    # Serialization (everything above validated successfully)
    # ------------------------------------------------------------------ #
    fragments: Dict[str, str] = {}
    query_pairs: List[str] = []
    headers: Dict[str, str] = {}
    cookie_pairs: List[str] = []

    for param in parameters:
        found, value = lookup(param)
        if not found or value is None:
            continue  # optional parameter omitted from the request
        style, explode = param.style_explode()
        if param.location == "path":
            fragments[param.name] = serialize_path_fragment(
                param.name, style, explode, value
            )
        elif param.location == "query":
            query_pairs.extend(
                serialize_query_pairs(
                    param.name,
                    style,
                    explode,
                    bool(param.node.get("allowReserved")),
                    value,
                )
            )
        elif param.location == "header":
            headers[param.name] = serialize_header(param.name, explode, value)
        else:
            cookie_pairs.append(serialize_cookie_pairs(param.name, value))

    def substitute(match: "re.Match[str]") -> str:
        return fragments[match.group(1)]

    target_path = _TEMPLATE_RE.sub(substitute, path)
    request_target = target_path
    if query_pairs:
        request_target += "?" + "&".join(query_pairs)
    cookie = "; ".join(cookie_pairs)
    return request_target, headers, cookie


def _sorted(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stable, order-independent error ordering."""

    def key(error: Dict[str, Any]):
        return (
            str(error.get("code")),
            str(error.get("in") or ""),
            str(error.get("name") or ""),
            str(error.get("pointer") or ""),
            str(error.get("error") or ""),
        )

    return sorted(errors, key=key)
