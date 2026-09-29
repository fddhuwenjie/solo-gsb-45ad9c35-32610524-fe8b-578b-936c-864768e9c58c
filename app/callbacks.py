"""Asynchronous callback URL resolution for one OpenAPI operation.

Implements ``POST /callbacks/resolve``: given an OpenAPI 3.x document, a
``METHOD /path`` operation selector and one concrete request/response
context, resolve every callback declared by the selected operation into an
actual callback URL plus the Path Item Object that describes the callback
exchange.

Callback Object keys are runtime expressions as defined by the OpenAPI 3
specification: either one complete runtime expression (``$url``,
``$method``, ``$statusCode``, ``$request.<source>[...]`` or
``$response.<source>[...]``) or a URL template embedding one or more
expressions in ``{...}`` braces. Literal-only keys are fixed callback URLs.

Supported sources: request ``path`` / ``query`` / ``header`` / ``body`` and
response ``header`` / ``body``. Body fragments are JSON Pointers (RFC
6901); header names match case-insensitively. Every interpolated value
must be a scalar.

Local ``$ref`` resolution (the Callback Object reference itself and its
Path Item Object values, including OpenAPI 3.1 sibling fields) is
cycle-safe. Missing values, malformed expressions, non-scalar
interpolation and dangling / external / circular references are all
collected and rejected with HTTP 422, carrying the source JSON Pointers of
the callback and the expression.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from .refresolver import (
    MAX_REF_CHAIN,
    RefResolver,
    UnresolvableReferenceError,
    is_local_ref,
    parse_local_ref,
)
from .slicer import HTTP_METHODS, parse_selector

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class CallbackError(Exception):
    """Base class for callback resolution failures."""


class InvalidDocumentError(CallbackError):
    """The submitted document is not a recognizable OpenAPI 3.x doc (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class InvalidSelectorError(CallbackError):
    """The selector is malformed or unknown (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class InvalidCallbackRequestError(CallbackError):
    """The request/response context envelope is malformed (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class CallbackResolutionError(CallbackError):
    """One or more semantic callback resolution problems (HTTP 422)."""

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


def unescape_token(token: str) -> str:
    # Order matters: "~1" must be expanded before "~0".
    return token.replace("~1", "/").replace("~0", "~")


def pointer_join(base: str, token: str) -> str:
    return base + "/" + escape_token(token)


def _valid_pointer_syntax(pointer: str) -> bool:
    """True when *pointer* is ``""`` or ``/<token>...`` with valid escapes."""
    if pointer == "":
        return True
    if not pointer.startswith("/"):
        return False
    for raw_token in pointer.split("/")[1:]:
        index = 0
        while index < len(raw_token):
            if raw_token[index] == "~":
                if index + 1 >= len(raw_token) or raw_token[index + 1] not in "01":
                    return False
                index += 2
            else:
                index += 1
    return True


def _valid_array_index(token: str, length: int) -> bool:
    if token == "0":
        return length > 0
    if not token.isdigit() or token[0] == "0":
        return False
    return int(token) < length


# --------------------------------------------------------------------------- #
# Runtime expression grammar (OpenAPI 3 specification, Runtime Expressions)
# --------------------------------------------------------------------------- #

# RFC 7230 tchar (header field name characters).
_TCHAR = frozenset("!#$%&'*+-.^_`|~")

# Allowed punctuation for the runtime-expression ``name`` production used by
# path/query references (besides unreserved characters and pct-encoded).
_NAME_PUNCT = frozenset("/?!$&'()*+,;=:@")

_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)

# Descriptor shapes:
#   ("simple", "url" | "method" | "status_code")
#   ("message", side, "path" | "query", raw_name)
#   ("message", side, "header", token)
#   ("message", side, "body", json_pointer | None)
Descriptor = Tuple[Any, ...]


def _is_hex_digit(char: str) -> bool:
    return char in "0123456789abcdefABCDEF"


def _valid_name(text: str) -> bool:
    """Validate the greedy ``name`` production of a path/query reference."""
    index = 0
    while index < len(text):
        char = text[index]
        if char in _UNRESERVED or char in _NAME_PUNCT:
            index += 1
        elif (
            char == "%"
            and index + 2 < len(text)
            and _is_hex_digit(text[index + 1])
            and _is_hex_digit(text[index + 2])
        ):
            index += 3
        else:
            return False
    return True


def _parse_body_tail(tail: str) -> Optional[str]:
    """Validate ``body`` / ``body#<json-pointer>`` and return the pointer."""
    if tail == "":
        return None
    if not tail.startswith("#"):
        return None
    pointer = tail[1:]
    if not _valid_pointer_syntax(pointer):
        return None
    return pointer


def _parse_expression(text: str) -> Optional[Descriptor]:
    """Parse one complete runtime expression.

    Returns the descriptor, or ``None`` when *text* is not a syntactically
    valid complete runtime expression.
    """
    if text == "$url":
        return ("simple", "url")
    if text == "$method":
        return ("simple", "method")
    if text == "$statusCode":
        return ("simple", "status_code")

    for side in ("request", "response"):
        prefix = "$" + side + "."
        if not text.startswith(prefix):
            continue
        rest = text[len(prefix):]
        if not rest:
            return None

        if rest.startswith("header."):
            token = rest[len("header."):]
            if not token or not all(ch.isalnum() or ch in _TCHAR for ch in token):
                return None
            return ("message", side, "header", token)

        # Only requests expose path and query references.
        if side == "request" and rest.startswith("query."):
            name = rest[len("query."):]
            if not _valid_name(name):
                return None
            return ("message", side, "query", name)

        if side == "request" and rest.startswith("path."):
            name = rest[len("path."):]
            if not _valid_name(name):
                return None
            return ("message", side, "path", name)

        if rest == "body" or rest.startswith("body#"):
            pointer = _parse_body_tail(rest[len("body"):])
            if pointer is None and rest != "body":
                return None
            return ("message", side, "body", pointer if rest != "body" else None)

        return None

    return None


# A template segment is ``("lit", text)`` or ``("expr", descriptor, raw)``.
Segment = Tuple[Any, ...]


class _TemplateSyntaxError(Exception):
    """A ``{...}`` URL template is malformed."""


def _split_template(text: str) -> List[Segment]:
    """Split a callback key into literal / embedded-expression segments."""
    segments: List[Segment] = []
    length = len(text)
    index = 0
    literal_start = 0

    while index < length:
        char = text[index]
        if char == "}":
            raise _TemplateSyntaxError(
                "stray '}': a closing brace without a matching '{'"
            )
        if char != "{":
            index += 1
            continue

        close = text.find("}", index + 1)
        if close == -1:
            raise _TemplateSyntaxError(
                "unclosed '{': every embedded expression must end with '}'"
            )
        inner = text[index + 1:close]
        if "{" in inner:
            raise _TemplateSyntaxError(
                "nested braces are not allowed inside a '{...}' expression"
            )
        if inner == "":
            raise _TemplateSyntaxError(
                "empty embedded expression '{}'"
            )
        descriptor = _parse_expression(inner)
        if descriptor is None:
            raise _TemplateSyntaxError(
                f"{inner!r} is not a valid runtime expression"
            )
        if index > literal_start:
            segments.append(("lit", text[literal_start:index]))
        segments.append(("expr", descriptor, inner))
        index = close + 1
        literal_start = index

    if literal_start < length:
        segments.append(("lit", text[literal_start:]))
    return segments


# --------------------------------------------------------------------------- #
# Cycle-safe $ref chains
# --------------------------------------------------------------------------- #


def _reference_error(
    pointer: str, ref: str, message: str
) -> Dict[str, Any]:
    return {
        "code": "unresolvable_reference",
        "pointer": pointer,
        "ref": ref,
        "error": message,
    }


def _follow_chain(
    doc: Dict[str, Any], node: Any, node_pointer: str
) -> Tuple[Optional[List[Tuple[Dict[str, Any], str]]], Optional[Dict[str, Any]]]:
    """Follow the local ``$ref`` chain starting at *node*.

    Returns ``(layers, error)``; each layer is ``(node, pointer)`` from the
    referencing occurrence through to the terminal non-reference object.
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
            return None, _reference_error(
                current_pointer,
                ref,
                f"unsupported non-local reference '{ref}': only document-local "
                "references of the form '#/...' are supported",
            )
        target_pointer = parse_local_ref(ref)
        if target_pointer == "":
            return None, _reference_error(
                current_pointer,
                ref,
                f"reference '{ref}' targets the document root; callback "
                "references must point to a specific Callback Object or Path "
                "Item Object location",
            )
        if target_pointer in seen:
            loop_start = seen[target_pointer]
            loop = refs[loop_start:] + [ref]
            return None, _reference_error(
                current_pointer,
                ref,
                "circular reference detected: " + " -> ".join(loop),
            )
        if len(refs) >= MAX_REF_CHAIN:
            return None, _reference_error(
                current_pointer,
                ref,
                f"reference chain exceeds the maximum depth of "
                f"{MAX_REF_CHAIN} links (last reference: {ref})",
            )
        seen[target_pointer] = len(refs)
        refs.append(ref)
        try:
            target = resolver.pointer(target_pointer)
        except UnresolvableReferenceError as exc:
            return None, _reference_error(current_pointer, exc.ref, str(exc))
        if not isinstance(target, dict):
            return None, _reference_error(
                current_pointer,
                ref,
                f"reference '{ref}' does not target an object",
            )
        layers.append((current, current_pointer))
        current = target
        current_pointer = target_pointer

    if not isinstance(current, dict):
        return None, {
            "code": "invalid_callback",
            "pointer": current_pointer,
            "error": "callback node must be an object",
        }

    layers.append((current, current_pointer))
    return layers, None


def _build_effective(
    layers: List[Tuple[Dict[str, Any], str]], merge_siblings: bool
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Build the effective object from a resolved chain.

    Returns ``(object, key_provenance)``; *key_provenance* maps each member
    to the JSON Pointer of the layer it came from. OpenAPI 3.0 uses the
    terminal target verbatim (siblings of ``$ref`` ignored); OpenAPI 3.1
    overlays sibling fields from the terminal target back toward the
    occurrence, so nearer siblings win.
    """
    if not merge_siblings:
        terminal, terminal_pointer = layers[-1]
        return copy.deepcopy(terminal), {
            key: terminal_pointer for key in terminal
        }

    merged: Dict[str, Any] = {}
    provenance: Dict[str, str] = {}
    for layer_node, layer_pointer in reversed(layers):
        for key, value in layer_node.items():
            if key == "$ref":
                continue
            merged[key] = copy.deepcopy(value)
            provenance[key] = layer_pointer
    return merged, provenance


# --------------------------------------------------------------------------- #
# Context envelope
# --------------------------------------------------------------------------- #

_REQUEST_FIELDS = ("url", "method", "path", "query", "header", "body")
_RESPONSE_FIELDS = ("statusCode", "header", "body")
_SCALAR_HEADER_TYPES = (str, int, float, bool)


def _normalize_context(
    context: Any,
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Validate and normalize the request/response context envelope."""
    if not isinstance(context, dict):
        return None, ["context must be a JSON object"]

    messages: List[str] = []
    for section in sorted(context, key=str):
        if section not in ("request", "response"):
            messages.append(
                f"context.{section}: unknown context section; expected one of "
                "'request'/'response'"
            )

    normalized: Dict[str, Any] = {}

    request = context.get("request")
    if request is not None:
        if not isinstance(request, dict):
            messages.append("context.request must be a JSON object")
        else:
            for field in sorted(request):
                if field not in _REQUEST_FIELDS:
                    messages.append(
                        f"context.request.{field}: unknown request field; "
                        "expected one of url/method/path/query/header/body"
                    )
            norm_request: Dict[str, Any] = {}
            url = request.get("url")
            if url is not None:
                if isinstance(url, str):
                    norm_request["url"] = url
                else:
                    messages.append("context.request.url must be a string")
            method = request.get("method")
            if method is not None:
                if isinstance(method, str):
                    norm_request["method"] = method
                else:
                    messages.append("context.request.method must be a string")
            for group in ("path", "query"):
                value = request.get(group)
                if value is not None:
                    if isinstance(value, dict):
                        norm_request[group] = value
                    else:
                        messages.append(
                            f"context.request.{group} must be an object mapping "
                            "names to values"
                        )
            headers = request.get("header")
            if headers is not None:
                if isinstance(headers, dict):
                    bad_headers = [
                        key
                        for key in sorted(headers, key=str)
                        if headers[key] is not None
                        and not isinstance(
                            headers[key], _SCALAR_HEADER_TYPES
                        )
                    ]
                    for key in bad_headers:
                        messages.append(
                            f"context.request.header.{key}: HTTP header values "
                            "must be scalar strings"
                        )
                    if not bad_headers:
                        norm_request["header"] = headers
                else:
                    messages.append(
                        "context.request.header must be an object mapping "
                        "header names to values"
                    )
            if "body" in request:
                norm_request["body"] = request["body"]
            normalized["request"] = norm_request

    response = context.get("response")
    if response is not None:
        if not isinstance(response, dict):
            messages.append("context.response must be a JSON object")
        else:
            for field in sorted(response):
                if field not in _RESPONSE_FIELDS:
                    messages.append(
                        f"context.response.{field}: unknown response field; "
                        "expected one of statusCode/header/body"
                    )
            norm_response: Dict[str, Any] = {}
            status = response.get("statusCode")
            if status is not None:
                if isinstance(status, bool) or not isinstance(
                    status, (int, str)
                ):
                    messages.append(
                        "context.response.statusCode must be an integer or string"
                    )
                elif isinstance(status, str) and not status:
                    messages.append(
                        "context.response.statusCode must not be an empty string"
                    )
                else:
                    norm_response["statusCode"] = status
            headers = response.get("header")
            if headers is not None:
                if isinstance(headers, dict):
                    bad_headers = [
                        key
                        for key in sorted(headers, key=str)
                        if headers[key] is not None
                        and not isinstance(
                            headers[key], _SCALAR_HEADER_TYPES
                        )
                    ]
                    for key in bad_headers:
                        messages.append(
                            f"context.response.header.{key}: HTTP header values "
                            "must be scalar strings"
                        )
                    if not bad_headers:
                        norm_response["header"] = headers
                else:
                    messages.append(
                        "context.response.header must be an object mapping "
                        "header names to values"
                    )
            if "body" in response:
                norm_response["body"] = response["body"]
            normalized["response"] = norm_response

    return normalized, messages


# --------------------------------------------------------------------------- #
# Expression evaluation
# --------------------------------------------------------------------------- #


def _to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _navigate_body(body: Any, pointer: Optional[str]) -> Any:
    """Apply a JSON Pointer to a body value (iterative, recursion-safe)."""
    node = body
    if pointer:
        for raw_token in pointer.split("/")[1:]:
            token = unescape_token(raw_token)
            if isinstance(node, dict):
                if token not in node:
                    return MISSING
                node = node[token]
            elif isinstance(node, list):
                if not _valid_array_index(token, len(node)):
                    return MISSING
                node = node[int(token)]
            else:
                return MISSING
    return node


class _Missing:
    """Sentinel for a value that could not be located in the context."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return "<MISSING>"


MISSING = _Missing()


def _header_lookup(
    headers: Dict[str, Any], name: str
) -> Tuple[str, Optional[str], Any]:
    """Case-insensitive header lookup.

    Returns ``(status, matched_key, value)`` where status is one of
    ``"missing"`` / ``"ambiguous"`` / ``"found"``.
    """
    lowered = name.lower()
    matches = [
        key for key in headers if isinstance(key, str) and key.lower() == lowered
    ]
    if not matches:
        return "missing", None, None
    if len(matches) > 1:
        return "ambiguous", None, None
    return "found", matches[0], headers[matches[0]]


def _evaluate(
    descriptor: Descriptor, context: Dict[str, Any]
) -> Dict[str, Any]:
    """Evaluate a parsed descriptor against the context.

    Result kinds: ``value`` (scalar), ``missing``, ``non_scalar`` and
    ``ambiguous`` (a header name matching several case-variant keys).
    """
    kind = descriptor[0]
    if kind == "simple":
        name = descriptor[1]
        if name == "status_code":
            response = context.get("response")
            if not response or "statusCode" not in response:
                return {
                    "kind": "missing",
                    "context_pointer": "/context/response/statusCode",
                }
            return {"kind": "value", "value": response["statusCode"]}

        request = context.get("request")
        field = "url" if name == "url" else "method"
        if not request or field not in request:
            return {
                "kind": "missing",
                "context_pointer": f"/context/request/{field}",
            }
        return {"kind": "value", "value": request[field]}

    _, side, source, token = descriptor
    message = context.get(side)
    base_pointer = f"/context/{side}"

    if source in ("path", "query"):
        if not message or source not in message:
            return {
                "kind": "missing",
                "context_pointer": f"{base_pointer}/{source}",
            }
        name = unquote(token)
        values = message[source]
        if name not in values:
            return {
                "kind": "missing",
                "context_pointer": pointer_join(
                    f"{base_pointer}/{source}", name
                ),
            }
        value = values[name]
        if value is None:
            return {
                "kind": "missing",
                "context_pointer": pointer_join(
                    f"{base_pointer}/{source}", name
                ),
            }
        if isinstance(value, (dict, list)):
            return {
                "kind": "non_scalar",
                "context_pointer": pointer_join(
                    f"{base_pointer}/{source}", name
                ),
                "value_type": "object" if isinstance(value, dict) else "array",
            }
        return {"kind": "value", "value": value}

    if source == "header":
        if not message or "header" not in message:
            return {
                "kind": "missing",
                "context_pointer": f"{base_pointer}/header",
            }
        headers = message["header"]
        status, matched_key, value = _header_lookup(headers, token)
        header_pointer = pointer_join(f"{base_pointer}/header", token)
        if status == "missing":
            return {"kind": "missing", "context_pointer": header_pointer}
        if status == "ambiguous":
            return {"kind": "ambiguous", "context_pointer": header_pointer}
        if value is None:
            return {"kind": "missing", "context_pointer": header_pointer}
        if isinstance(value, (dict, list)):
            return {
                "kind": "non_scalar",
                "context_pointer": pointer_join(
                    f"{base_pointer}/header", matched_key
                ),
                "value_type": "object" if isinstance(value, dict) else "array",
            }
        return {"kind": "value", "value": value}

    # body
    pointer = token
    if not message or "body" not in message:
        return {"kind": "missing", "context_pointer": f"{base_pointer}/body"}
    value = _navigate_body(message["body"], pointer)
    context_pointer = f"{base_pointer}/body" + (pointer or "")
    if value is MISSING or value is None:
        return {"kind": "missing", "context_pointer": context_pointer}
    if isinstance(value, (dict, list)):
        return {
            "kind": "non_scalar",
            "context_pointer": context_pointer,
            "value_type": "object" if isinstance(value, dict) else "array",
        }
    return {"kind": "value", "value": value}


# --------------------------------------------------------------------------- #
# Document shape / operation lookup
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


def _resolve_path_item(
    doc: Dict[str, Any], raw_item: Any, item_pointer: str, is31: bool
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Resolve the effective operation path item, cycle-safe."""
    if not isinstance(raw_item, dict):
        return None, {
            "code": "invalid_callback",
            "pointer": item_pointer,
            "error": "path item must be an object",
        }
    if not isinstance(raw_item.get("$ref"), str):
        return raw_item, None
    layers, error = _follow_chain(doc, raw_item, item_pointer)
    if error is not None:
        return None, error
    effective, _provenance = _build_effective(layers, is31)
    return effective, None


# --------------------------------------------------------------------------- #
# Per-callback processing
# --------------------------------------------------------------------------- #


def _error(
    code: str,
    message: str,
    *,
    pointer: str,
    callback_pointer: Optional[str] = None,
    expression_pointer: Optional[str] = None,
    expression: Optional[str] = None,
    ref: Optional[str] = None,
    context_pointer: Optional[str] = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {"code": code, "pointer": pointer, "error": message}
    if callback_pointer is not None:
        item["callback_pointer"] = callback_pointer
    if expression_pointer is not None:
        item["expression_pointer"] = expression_pointer
    if expression is not None:
        item["expression"] = expression
    if ref is not None:
        item["ref"] = ref
    if context_pointer is not None:
        item["context_pointer"] = context_pointer
    return item


def _resolve_path_item_value(
    doc: Dict[str, Any],
    raw_value: Any,
    value_pointer: str,
    is31: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if not isinstance(raw_value, dict):
        return None, {
            "code": "invalid_callback",
            "pointer": value_pointer,
            "error": "callback expression value must be a Path Item Object",
        }
    if not isinstance(raw_value.get("$ref"), str):
        return raw_value, None
    layers, error = _follow_chain(doc, raw_value, value_pointer)
    if error is not None:
        return None, error
    effective, _provenance = _build_effective(layers, is31)
    if not isinstance(effective, dict):
        return None, {
            "code": "invalid_callback",
            "pointer": value_pointer,
            "error": "callback expression value must be a Path Item Object",
        }
    return effective, None


def _build_url(
    expression_key: str,
    expression_pointer: str,
    context: Dict[str, Any],
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Resolve one callback key into its concrete URL string."""
    errors: List[Dict[str, Any]] = []

    if expression_key == "":
        return None, [
            {
                "code": "invalid_expression",
                "pointer": expression_pointer,
                "expression": expression_key,
                "error": "callback expression key must not be an empty string",
            }
        ]

    standalone = _parse_expression(expression_key)
    if standalone is not None:
        segments: List[Segment] = [("expr", standalone, expression_key)]
    else:
        try:
            segments = _split_template(expression_key)
        except _TemplateSyntaxError as exc:
            return None, [
                {
                    "code": "invalid_expression",
                    "pointer": expression_pointer,
                    "expression": expression_key,
                    "error": str(exc),
                }
            ]

    parts: List[str] = []
    for segment in segments:
        if segment[0] == "lit":
            parts.append(segment[1])
            continue
        _, descriptor, raw_expression = segment
        outcome = _evaluate(descriptor, context)
        kind = outcome["kind"]
        if kind == "missing":
            errors.append(
                {
                    "code": "missing_value",
                    "pointer": expression_pointer,
                    "expression": raw_expression,
                    "context_pointer": outcome["context_pointer"],
                    "error": (
                        f"runtime expression {raw_expression!r} has no value in "
                        "the supplied request/response context"
                    ),
                }
            )
        elif kind == "non_scalar":
            errors.append(
                {
                    "code": "non_scalar_interpolation",
                    "pointer": expression_pointer,
                    "expression": raw_expression,
                    "context_pointer": outcome["context_pointer"],
                    "error": (
                        f"runtime expression {raw_expression!r} resolved to a "
                        f"{outcome['value_type']}; only scalar values can be "
                        "interpolated into a callback URL"
                    ),
                }
            )
        elif kind == "ambiguous":
            errors.append(
                {
                    "code": "ambiguous_header",
                    "pointer": expression_pointer,
                    "expression": raw_expression,
                    "context_pointer": outcome["context_pointer"],
                    "error": (
                        "header name matches several context headers that "
                        "differ only in case"
                    ),
                }
            )
        else:
            parts.append(_to_text(outcome["value"]))

    if errors:
        return None, errors
    return "".join(parts), []


def _process_callback(
    doc: Dict[str, Any],
    callback_name: str,
    callback_raw: Any,
    callback_pointer: str,
    is31: bool,
    context: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Resolve one operation callbacks map entry.

    Returns ``(results, errors)``.
    """
    if not isinstance(callback_raw, dict):
        return [], [
            _error(
                "invalid_callback",
                "callback value must be a Callback Object",
                pointer=callback_pointer,
                callback_pointer=callback_pointer,
            )
        ]

    if isinstance(callback_raw.get("$ref"), str):
        layers, error = _follow_chain(doc, callback_raw, callback_pointer)
        if error is not None:
            error["callback_pointer"] = callback_pointer
            return [], [error]
        callback_map, provenance = _build_effective(layers, is31)
    else:
        callback_map = callback_raw
        provenance = None

    if not isinstance(callback_map, dict):
        return [], [
            _error(
                "invalid_callback",
                "callback value must be a Callback Object",
                pointer=callback_pointer,
                callback_pointer=callback_pointer,
            )
        ]

    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for expression_key in callback_map:
        declaration_pointer = (
            provenance[expression_key] if provenance is not None
            else callback_pointer
        )
        expression_pointer = pointer_join(declaration_pointer, expression_key)

        path_item, item_error = _resolve_path_item_value(
            doc,
            callback_map[expression_key],
            expression_pointer,
            is31,
        )
        if item_error is not None:
            item_error["callback_pointer"] = callback_pointer
            item_error["expression_pointer"] = expression_pointer
            item_error["expression"] = expression_key
            errors.append(item_error)
            continue

        url, url_errors = _build_url(
            expression_key, expression_pointer, context
        )
        if url_errors:
            for item in url_errors:
                item["callback_pointer"] = callback_pointer
                item["expression_pointer"] = expression_pointer
            errors.extend(url_errors)
            continue

        results.append(
            {
                "name": callback_name,
                "callback_pointer": callback_pointer,
                "expression": expression_key,
                "expression_pointer": expression_pointer,
                "url": url,
                "path_item": copy.deepcopy(path_item),
            }
        )

    return results, errors


def _sorted_errors(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def key(item: Dict[str, Any]):
        return (
            str(item.get("code")),
            str(item.get("pointer") or ""),
            str(item.get("expression") or ""),
            str(item.get("ref") or ""),
            str(item.get("error") or ""),
        )

    return sorted(errors, key=key)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def resolve_callbacks(
    doc: Any, selector: Any, context: Any
) -> List[Dict[str, Any]]:
    """Resolve every callback URL declared by the selected operation.

    :raises InvalidDocumentError: *doc* is not OpenAPI 3.x.
    :raises InvalidSelectorError: the selector is malformed/unknown (400).
    :raises InvalidCallbackRequestError: *context* is malformed (400).
    :raises CallbackResolutionError: semantic problems (422), each carrying
        source JSON Pointers.
    """
    shape_errors = _validate_shape(doc)
    if shape_errors:
        raise InvalidDocumentError(shape_errors)

    parsed = parse_selector(selector)
    if parsed is None:
        raise InvalidSelectorError(
            [
                f"malformed selector {selector!r}: expected 'METHOD /path' "
                "(e.g. 'POST /subscribe')"
            ]
        )
    method, path = parsed
    if path not in doc["paths"]:
        raise InvalidSelectorError([f"{method.upper()} {path}: path is not declared"])

    normalized_context, context_errors = _normalize_context(context)
    if context_errors:
        raise InvalidCallbackRequestError(context_errors)

    is31 = str(doc.get("openapi", "")).startswith(("3.1", "3.2"))
    path_pointer = pointer_join("/paths", path)
    path_item, item_error = _resolve_path_item(
        doc, doc["paths"][path], path_pointer, is31
    )
    if item_error is not None:
        raise CallbackResolutionError(_sorted_errors([item_error]))

    method_keys = {
        key.lower(): key for key in path_item if key.lower() in HTTP_METHODS
    }
    if method not in method_keys:
        raise InvalidSelectorError(
            [f"{method.upper()} {path}: operation is not declared"]
        )
    operation_key = method_keys[method]
    operation = path_item[operation_key]
    operation_pointer = pointer_join(path_pointer, operation_key)
    if not isinstance(operation, dict):
        raise CallbackResolutionError(
            _sorted_errors(
                [
                    {
                        "code": "invalid_callback",
                        "pointer": operation_pointer,
                        "error": "operation must be an object",
                    }
                ]
            )
        )

    if "callbacks" not in operation:
        return []
    callbacks = operation["callbacks"]
    callbacks_pointer = pointer_join(operation_pointer, "callbacks")
    if not isinstance(callbacks, dict):
        raise CallbackResolutionError(
            _sorted_errors(
                [
                    {
                        "code": "invalid_callback",
                        "pointer": callbacks_pointer,
                        "error": "'callbacks' must be an object",
                    }
                ]
            )
        )

    all_results: List[Dict[str, Any]] = []
    all_errors: List[Dict[str, Any]] = []
    for callback_name, callback_raw in callbacks.items():
        callback_pointer = pointer_join(callbacks_pointer, callback_name)
        results, errors = _process_callback(
            doc,
            callback_name,
            callback_raw,
            callback_pointer,
            is31,
            normalized_context,
        )
        all_results.extend(results)
        all_errors.extend(errors)

    if all_errors:
        raise CallbackResolutionError(_sorted_errors(all_errors))

    all_results.sort(
        key=lambda item: (item["callback_pointer"], item["expression_pointer"])
    )
    return all_results
