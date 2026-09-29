"""Asynchronous callback URL resolution (``POST /callbacks/resolve``).

Given an OpenAPI 3.x document, an operation selector and one concrete
request/response context, resolve every Callback Object declared by the
operation:

* the Callback Object behind each ``callbacks`` map entry is reached
  cycle-safely through local ``$ref`` chains (dangling / external /
  circular references are rejected with HTTP 422);
* each Callback Object key is either a runtime expression on its own
  (``$...``) or a mix of literal text and ``{expression}`` Runtime
  Expression embeddings; the literal fragments and the expression values
  are concatenated into the actual callback URL;
* the resolved Path Item Object paired with every key is returned
  alongside the URL (3.1 path-item ``$ref`` chains are followed with
  sibling merging; 3.0 uses the referenced object verbatim).

Supported runtime expressions (OpenAPI 3.0.3 section 4.7.10 / 3.1
section 4.8.24.1): ``$url``, ``$method``, ``$statusCode``,
``$request.path.name`` / ``$request.query.name`` /
``$request.header.name`` / ``$request.body`` /
``$request.body#/json/pointer`` and the ``$response.*`` counterparts.
Body fragments are read by JSON Pointer (RFC 6901) and header names are
matched case-insensitively. Missing values, illegal expressions and
non-scalar interpolations are all rejected with HTTP 422, each error
carrying the source JSON Pointer of both the callback map entry and the
expression key. Results sort stably by their source pointers and the
input document and context are never mutated.
"""

from __future__ import annotations

import copy
import re
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

# RFC 7230 token characters for runtime-expression header tokens.
_HEADER_TOKEN_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
# OpenAPI runtime-expression path/query 'name' tokens: RFC 3986
# 'unreserved' characters (ALPHA / DIGIT / "-" / "." / "_" / "~") plus
# 'pct-encoded' triplets, per the Runtime Expression ABNF.
_UNESCAPED_NAME_CHARS = frozenset(
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz-._~"
)
_BRACE_RE = re.compile(r"\{([^{}]*)\}")
_PERCENT_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_SOURCE_RE = re.compile(r"^\$(request|response)\.(.*)$", re.DOTALL)


class CallbackResolveError(Exception):
    """Base class for callback resolution failures."""


class InvalidDocumentError(CallbackResolveError):
    """The submitted document is not a recognizable OpenAPI 3.x doc (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class InvalidSelectorError(CallbackResolveError):
    """The selector is malformed or unknown (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class InvalidContextError(CallbackResolveError):
    """The request/response context envelope is malformed (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class CallbackResolutionError(CallbackResolveError):
    """One or more callback resolution problems (HTTP 422)."""

    def __init__(self, errors: List[Dict[str, Any]]) -> None:
        self.errors = errors
        super().__init__(
            "; ".join(
                f"{e['code']} @ {e.get('callback_pointer', '')}"
                for e in errors
            )
        )


# --------------------------------------------------------------------------- #
# JSON Pointer helpers (RFC 6901)
# --------------------------------------------------------------------------- #

def escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def pointer_join(base: str, token: str) -> str:
    return base + "/" + escape_token(token)


def _decode_pointer_token(raw: str) -> str:
    # JSON Pointer tokens are percent-decoded too: the runtime-expression
    # grammar reuses RFC 6901, whose tokens in a URL fragment are
    # percent-encoded as well as ~-escaped.
    return unquote(raw.replace("~1", "/").replace("~0", "~"))


def _json_pointer_value(
    node: Any, pointer: str, *, expression: str
) -> Tuple[Any, Optional[str]]:
    """Follow *pointer* (including the empty pointer) from *node*.

    Returns ``(value, None)`` or ``(None, reason)`` when navigation fails;
    malformed pointers raise :class:`ValueError`.
    """
    if pointer == "":
        return node, None
    if not pointer.startswith("/"):
        raise ValueError(
            f"invalid JSON Pointer fragment in {expression!r}: a pointer "
            "must start with '/' or be empty ('#')"
        )
    current = node
    for raw_token in pointer.split("/")[1:]:
        token = _decode_pointer_token(raw_token)
        if isinstance(current, dict):
            if token not in current:
                return None, (
                    f"body JSON Pointer '/{pointer.lstrip('/')}' has no "
                    f"property '{token}'"
                )
            current = current[token]
        elif isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                return None, (
                    f"body JSON Pointer '/{pointer.lstrip('/')}' token "
                    f"'{token}' is not a valid array index"
                )
            index = int(token)
            if index >= len(current):
                return None, (
                    f"body JSON Pointer '/{pointer.lstrip('/')}' array "
                    f"index {index} out of range (length {len(current)})"
                )
            current = current[index]
        else:
            return None, (
                f"cannot descend into body JSON Pointer token '{token}' "
                "from a scalar value"
            )
    return current, None


def _validate_pointer_syntax(pointer: str) -> None:
    """Reject malformed percent-escapes inside a JSON Pointer fragment."""
    for index, char in enumerate(pointer):
        if char == "%" and not _PERCENT_RE.match(pointer[index : index + 3]):
            raise ValueError(
                f"malformed percent-encoding in JSON Pointer fragment "
                f"{pointer!r}: '%' must be followed by two hexadecimal digits"
            )


# --------------------------------------------------------------------------- #
# Cycle-safe local $ref resolution (with OpenAPI 3.1 sibling merging)
# --------------------------------------------------------------------------- #

def _resolve_ref_chain(
    doc: Dict[str, Any],
    node: Any,
    node_pointer: str,
    *,
    merge_siblings: bool,
) -> Tuple[
    Optional[Dict[str, Any]],
    Optional[str],
    Optional[Dict[str, str]],
    Optional[Dict[str, Any]],
]:
    """Follow a local ``$ref`` chain starting at *node*.

    Returns ``(merged_node, terminal_pointer, origins, error)`` with
    *error* set instead of the first three on failure. With
    *merge_siblings* (OpenAPI 3.1) every layer's siblings are overlaid
    from the terminal target back to the referencing node; otherwise the
    terminal target is returned verbatim (OpenAPI 3.0). ``origins`` maps
    each surviving key to the JSON Pointer of the layer that supplied it
    (the nearest layer defining a key wins), so callers can point at the
    physical definition of an expression key.
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
            return None, None, None, {
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
            return None, None, None, {
                "code": "unresolvable_reference",
                "pointer": current_pointer,
                "ref": ref,
                "error": "circular reference detected: " + " -> ".join(loop),
            }
        if len(refs) >= MAX_REF_CHAIN:
            return None, None, None, {
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
            return None, None, None, {
                "code": "unresolvable_reference",
                "pointer": current_pointer,
                "ref": exc.ref,
                "error": str(exc),
            }
        if not isinstance(target, dict):
            return None, None, None, {
                "code": "unresolvable_reference",
                "pointer": current_pointer,
                "ref": ref,
                "error": f"reference '{ref}' does not target an object",
            }
        layers.append((current, current_pointer))
        current = target
        current_pointer = target_pointer

    if not isinstance(current, dict):
        return None, None, None, {
            "code": "invalid_callback",
            "pointer": current_pointer,
            "ref": refs[-1] if refs else None,
            "error": "reference target must be an object",
        }

    layers.append((current, current_pointer))
    origins: Dict[str, str] = {}
    if merge_siblings:
        merged: Dict[str, Any] = {}
        for layer_node, layer_pointer in reversed(layers):
            for key, value in layer_node.items():
                if key != "$ref":
                    merged[key] = copy.deepcopy(value)
                    origins[key] = pointer_join(layer_pointer, key)
        result = merged
    else:
        result = copy.deepcopy(current)
        for key in result:
            origins[key] = pointer_join(current_pointer, key)
    return result, current_pointer, origins, None


# --------------------------------------------------------------------------- #
# Context envelope validation
# --------------------------------------------------------------------------- #

def _validate_context(context: Any) -> List[str]:
    messages: List[str] = []
    if not isinstance(context, dict):
        return ["context must be a JSON object"]
    for key in context:
        if key not in ("request", "response"):
            messages.append(
                f"context.{key}: unknown member; only 'request' and "
                "'response' are allowed"
            )
    for side, fields in (
        ("request", ("url", "method", "path", "query", "header", "body")),
        ("response", ("statusCode", "header", "body")),
    ):
        value = context.get(side)
        if value is None:
            continue
        if not isinstance(value, dict):
            messages.append(f"context.{side} must be an object")
            continue
        for key in value:
            if key not in fields:
                messages.append(
                    f"context.{side}.{key}: unknown member for a {side} "
                    "context"
                )
        if side == "request":
            if not isinstance(value.get("url", ""), str):
                messages.append("context.request.url must be a string")
            if not isinstance(value.get("method", ""), str):
                messages.append("context.request.method must be a string")
            for mapping in ("path", "query", "header"):
                mapping_value = value.get(mapping)
                if mapping_value is not None and not isinstance(
                    mapping_value, dict
                ):
                    messages.append(
                        f"context.request.{mapping} must be an object"
                    )
        else:
            status = value.get("statusCode")
            if status is not None and (
                not isinstance(status, int) or isinstance(status, bool)
            ):
                messages.append(
                    "context.response.statusCode must be an integer"
                )
        header_value = value.get("header")
        if header_value is not None and not isinstance(header_value, dict):
            messages.append(f"context.{side}.header must be an object")
    return messages


# --------------------------------------------------------------------------- #
# Runtime expression evaluation
# --------------------------------------------------------------------------- #

def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _header_lookup(
    headers: Dict[str, Any], name: str, side: str
) -> Tuple[bool, Any, Optional[str]]:
    """Case-insensitive header lookup; ``(found, value, pointer)``."""
    lowered = name.lower()
    matches = sorted(
        key for key in headers if isinstance(key, str)
        and key.lower() == lowered
    )
    if not matches:
        return False, None, None
    return True, headers[matches[0]], f"/context/{side}/header/{matches[0]}"


def _valid_name(name: str) -> bool:
    """Validate a runtime-expression path/query name against the spec ABNF.

    A name is a run of unreserved (RFC 3986) / ``pct-encoded`` tokens;
    every other byte (sub-delimiters such as ``!`` / ``$``, ``:``,
    ``@``, whitespace ...) makes the expression illegal.
    """
    index = 0
    while index < len(name):
        char = name[index]
        if char in _UNESCAPED_NAME_CHARS:
            index += 1
        elif char == "%" and _PERCENT_RE.fullmatch(
            name[index : index + 3]
        ):
            index += 3
        else:
            return False
    return True


def evaluate_expression(
    expression: str,
    context: Dict[str, Any],
    *,
    expression_pointer: str,
    callback_pointer: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Evaluate one runtime expression against the request/response context.

    Returns ``(text, None)`` or ``(None, error)``; malformed expression
    syntax raises :class:`ValueError`.
    """

    def error(code: str, message: str, **extra: Any) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "code": code,
            "callback_pointer": callback_pointer,
            "expression_pointer": expression_pointer,
            "expression": expression,
            "error": message,
        }
        entry.update(extra)
        return entry

    # -- The three source-less identifiers ------------------------------ #
    if expression == "$url":
        present, value, source = _request_scalar(context, "url")
        if not present:
            return None, error(
                "missing_value",
                "runtime expression '$url' has no value in the supplied "
                "context",
                context_pointer=source,
            )
        return _scalar_or_error(expression, value, error, source)
    if expression == "$method":
        present, value, source = _request_scalar(context, "method")
        if not present:
            return None, error(
                "missing_value",
                "runtime expression '$method' has no value in the supplied "
                "context",
                context_pointer=source,
            )
        return _scalar_or_error(expression, value, error, source)
    if expression == "$statusCode":
        present, value, source = _status_code(context)
        if not present:
            return None, error(
                "missing_value",
                "runtime expression '$statusCode' has no value in the "
                "supplied context",
                context_pointer=source,
            )
        return _scalar_or_error(expression, value, error, source)

    # -- $request.* / $response.* --------------------------------------- #
    source_match = _SOURCE_RE.fullmatch(expression)
    if source_match is None:
        raise ValueError(
            f"unsupported runtime expression {expression!r}: expected one "
            "of '$url', '$method', '$statusCode' or a '$request.*'/"
            "'$response.*' expression"
        )
    side = source_match.group(1)
    remainder = source_match.group(2)

    kind: str
    name: Optional[str] = None
    body_pointer: Optional[str] = None
    if remainder == "body" or remainder.startswith("body#"):
        kind = "body"
        body_pointer = "" if remainder == "body" else remainder[len("body#") :]
        if body_pointer and not body_pointer.startswith("/"):
            raise ValueError(
                f"invalid JSON Pointer fragment in {expression!r}: the "
                "'#' fragment must be empty or start with '/'"
            )
        _validate_pointer_syntax(body_pointer)
    elif remainder.startswith(("path.", "query.", "header.")):
        kind = remainder.split(".", 1)[0]
        name = remainder[len(kind) + 1 :]
        if not name:
            raise ValueError(
                f"runtime expression {expression!r} is missing a {kind} name"
            )
        if kind == "header":
            if not _HEADER_TOKEN_RE.fullmatch(name):
                raise ValueError(
                    f"invalid header token {name!r} in runtime expression "
                    f"{expression!r}"
                )
        else:
            if not _valid_name(name):
                raise ValueError(
                    f"invalid {kind} name {name!r} in runtime expression "
                    f"{expression!r}"
                )
    else:
        raise ValueError(
            f"unsupported runtime expression {expression!r}: expected "
            "'path.<name>', 'query.<name>', 'header.<name>', 'body' or "
            "'body#/json/pointer'"
        )

    side_context = context.get(side)
    if not isinstance(side_context, dict):
        return None, error(
            "missing_value",
            f"runtime expression {expression!r} references the {side}, "
            "which is absent from the supplied context",
            context_pointer=f"/context/{side}",
        )

    if kind == "body":
        assert body_pointer is not None
        if "body" not in side_context:
            return None, error(
                "missing_value",
                f"runtime expression {expression!r} references the {side} "
                "body, which is absent from the supplied context",
                context_pointer=f"/context/{side}/body",
            )
        body = side_context["body"]
        try:
            value, reason = _json_pointer_value(
                body, body_pointer, expression=expression
            )
        except ValueError as exc:
            raise ValueError(str(exc))
        if reason is not None:
            return None, error(
                "missing_value",
                f"runtime expression {expression!r}: {reason}",
                context_pointer=f"/context/{side}/body",
            )
        source = f"/context/{side}/body" + body_pointer
        return _scalar_or_error(expression, value, error, source)

    assert name is not None
    mapping = side_context.get(kind)
    if not isinstance(mapping, dict):
        return None, error(
            "missing_value",
            f"runtime expression {expression!r} references {side}.{kind}, "
            "which is absent from the supplied context",
            context_pointer=f"/context/{side}/{kind}",
        )
    if kind == "header":
        found, value, source = _header_lookup(mapping, name, side)
        if not found:
            return None, error(
                "missing_value",
                f"runtime expression {expression!r}: header {name!r} is "
                "absent from the supplied context (names are "
                "case-insensitive)",
                context_pointer=f"/context/{side}/header",
            )
    else:
        decoded_name = unquote(name)
        if decoded_name not in mapping:
            return None, error(
                "missing_value",
                f"runtime expression {expression!r}: {kind} parameter "
                f"{decoded_name!r} is absent from the supplied context",
                context_pointer=f"/context/{side}/{kind}",
            )
        value = mapping[decoded_name]
        source = pointer_join(f"/context/{side}/{kind}", decoded_name)
    return _scalar_or_error(expression, value, error, source)


def _request_scalar(
    context: Dict[str, Any], key: str
) -> Tuple[bool, Any, str]:
    request = context.get("request")
    if not isinstance(request, dict) or key not in request:
        return False, None, f"/context/request/{key}"
    return True, request[key], f"/context/request/{key}"


def _status_code(context: Dict[str, Any]) -> Tuple[bool, Any, str]:
    response = context.get("response")
    if not isinstance(response, dict) or "statusCode" not in response:
        return False, None, "/context/response/statusCode"
    return True, response["statusCode"], "/context/response/statusCode"


def _scalar_or_error(
    expression: str,
    value: Any,
    error_factory: Any,
    source: str,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if value is None or isinstance(value, (dict, list)):
        kind = "null" if value is None else (
            "object" if isinstance(value, dict) else "array"
        )
        return None, error_factory(
            "non_scalar_value",
            f"runtime expression {expression!r} resolved to a {kind} but a "
            "scalar value (string/number/boolean) is required for URL "
            "interpolation",
            context_pointer=source,
        )
    return _render_scalar(value), None


# --------------------------------------------------------------------------- #
# Callback key interpolation
# --------------------------------------------------------------------------- #

def build_url(
    key: str,
    context: Dict[str, Any],
    *,
    callback_pointer: str,
    expression_pointer: str,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Resolve a Callback Object key into a concrete URL.

    Returns ``(url, errors)``; a key may carry several ``{expression}``
    embeddings and every embedding is validated, so several errors can be
    reported at once. *expression_pointer* is the physical JSON Pointer
    of the key (it may live inside a referenced component rather than
    directly under *callback_pointer*).
    """
    errors: List[Dict[str, Any]] = []

    def embed_error(
        code: str, expression: str, message: str, **extra: Any
    ) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "code": code,
            "callback_pointer": callback_pointer,
            "expression_pointer": expression_pointer,
            "expression": expression,
            "error": message,
        }
        entry.update(extra)
        return entry

    # A bare runtime expression is a callback key on its own; everything
    # else must use '{...}' embeddings inside literal text.
    if key.startswith("$"):
        try:
            text, error = evaluate_expression(
                key,
                context,
                expression_pointer=expression_pointer,
                callback_pointer=callback_pointer,
            )
        except ValueError as exc:
            return None, [embed_error("invalid_expression", key, str(exc))]
        if error is not None:
            return None, [error]
        return text, errors

    fragments: List[str] = []
    cursor = 0
    for match in _BRACE_RE.finditer(key):
        fragments.append(key[cursor : match.start()])
        cursor = match.end()
        expression = match.group(1)
        if not expression:
            errors.append(
                embed_error(
                    "invalid_expression",
                    "{}",
                    "empty runtime expression: a callback embedding must "
                    "contain an expression like '{$request.path.id}'",
                )
            )
            continue
        if not expression.startswith("$"):
            errors.append(
                embed_error(
                    "invalid_expression",
                    expression,
                    f"invalid runtime expression {expression!r}: expressions "
                    "must start with '$'",
                )
            )
            continue
        try:
            text, error = evaluate_expression(
                expression,
                context,
                expression_pointer=expression_pointer,
                callback_pointer=callback_pointer,
            )
        except ValueError as exc:
            errors.append(
                embed_error("invalid_expression", expression, str(exc))
            )
            continue
        if error is not None:
            errors.append(error)
            continue
        fragments.append(text)

    leftover = key[cursor:]
    # Stray braces are illegal: well-formed embeddings have all been
    # consumed by the regex, so any remaining '{' / '}' is unbalanced.
    if "{" in leftover or "}" in leftover:
        errors.append(
            embed_error(
                "invalid_expression",
                key,
                f"malformed callback key {key!r}: runtime expression "
                "embeddings must be balanced non-empty '{...}' tokens",
            )
        )
    if errors:
        return None, errors
    fragments.append(leftover)
    return "".join(fragments), errors


# --------------------------------------------------------------------------- #
# End-to-end resolution
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


def resolve_callbacks(
    doc: Any, selector: Any, context: Any
) -> List[Dict[str, Any]]:
    """Resolve every callback URL declared by the selected operation.

    :raises InvalidDocumentError: *doc* is not OpenAPI 3.x (400).
    :raises InvalidSelectorError: the selector is malformed/unknown (400).
    :raises InvalidContextError: *context* has a bad envelope (400).
    :raises CallbackResolutionError: semantic problems (422), each with
        the source JSON Pointers of its callback and expression.
    """
    shape_errors = _validate_shape(doc)
    if shape_errors:
        raise InvalidDocumentError(shape_errors)

    parsed = parse_selector(selector)
    if parsed is None:
        raise InvalidSelectorError(
            [
                f"malformed selector {selector!r}: expected 'METHOD /path' "
                "(e.g. 'POST /pets')"
            ]
        )
    method, path = parsed
    if path not in doc["paths"]:
        raise InvalidSelectorError([f"{method.upper()} {path}: path is not declared"])

    context_errors = _validate_context(context)
    if context_errors:
        raise InvalidContextError(context_errors)
    assert isinstance(context, dict)

    is31 = str(doc.get("openapi", "")).startswith(("3.1", "3.2"))
    path_pointer = pointer_join("/paths", path)
    raw_item = doc["paths"][path]
    path_item, _terminal, _path_origins, item_error = _resolve_ref_chain(
        doc, raw_item, path_pointer, merge_siblings=True
    )
    if item_error is not None:
        raise CallbackResolutionError(_sorted([item_error]))

    method_keys = {k.lower(): k for k in path_item if k.lower() in HTTP_METHODS}
    if method not in method_keys:
        raise InvalidSelectorError(
            [f"{method.upper()} {path}: operation is not declared"]
        )
    actual_method_key = method_keys[method]
    operation = path_item[actual_method_key]
    op_pointer = pointer_join(path_pointer, actual_method_key)
    if not isinstance(operation, dict):
        raise CallbackResolutionError(
            _sorted(
                [
                    {
                        "code": "invalid_callback",
                        "pointer": op_pointer,
                        "error": "operation must be an object",
                    }
                ]
            )
        )

    callbacks = operation.get("callbacks")
    callbacks_pointer = pointer_join(op_pointer, "callbacks")
    if callbacks is None:
        return []
    if not isinstance(callbacks, dict):
        raise CallbackResolutionError(
            _sorted(
                [
                    {
                        "code": "invalid_callback",
                        "pointer": callbacks_pointer,
                        "error": "'callbacks' must be an object mapping names "
                        "to Callback Objects",
                    }
                ]
            )
        )

    errors: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []

    # Iterate in sorted key order so both error and success ordering are
    # independent of the document's key order.
    for callback_name in sorted(callbacks):
        raw_callback = callbacks[callback_name]
        callback_pointer = pointer_join(callbacks_pointer, callback_name)
        if not isinstance(raw_callback, dict):
            errors.append(
                {
                    "code": "invalid_callback",
                    "callback_pointer": callback_pointer,
                    "error": "callback entry must be an object or a local "
                    "$ref to a Callback Object",
                }
            )
            continue

        resolved, _terminal_pointer, origins, ref_error = _resolve_ref_chain(
            doc,
            raw_callback,
            callback_pointer,
            merge_siblings=is31,
        )
        if ref_error is not None:
            entry = dict(ref_error)
            entry["callback_pointer"] = callback_pointer
            if entry.get("pointer"):
                entry["ref_pointer"] = entry.pop("pointer")
            errors.append(entry)
            continue

        expression_keys = sorted(
            key for key in resolved if key != "$ref" and isinstance(key, str)
        )
        if not expression_keys:
            errors.append(
                {
                    "code": "invalid_callback",
                    "callback_pointer": callback_pointer,
                    "error": "Callback Object must contain at least one "
                    "expression key identifying the callback URL",
                }
            )
            continue

        for key in expression_keys:
            raw_path_item = resolved[key]
            # The physical key location: the referenced component for a
            # plain 3.0 $ref (and the terminal layer on a 3.1 chain), or
            # the nearer layer when 3.1 siblings override the key.
            expression_pointer = origins.get(
                key, pointer_join(callback_pointer, key)
            )
            if not isinstance(raw_path_item, dict):
                errors.append(
                    {
                        "code": "invalid_callback",
                        "callback_pointer": callback_pointer,
                        "expression_pointer": expression_pointer,
                        "expression": key,
                        "error": "callback expression must map to a Path Item "
                        "Object",
                    }
                )
                continue
            effective_item, _item_terminal, _item_origins, item_ref_error = (
                _resolve_ref_chain(
                    doc,
                    raw_path_item,
                    expression_pointer,
                    merge_siblings=is31,
                )
            )
            if item_ref_error is not None:
                entry = dict(item_ref_error)
                entry["callback_pointer"] = callback_pointer
                entry["expression_pointer"] = expression_pointer
                entry["expression"] = key
                if entry.get("pointer"):
                    entry["ref_pointer"] = entry.pop("pointer")
                errors.append(entry)
                continue

            url, key_errors = build_url(
                key,
                context,
                callback_pointer=callback_pointer,
                expression_pointer=expression_pointer,
            )
            if key_errors:
                errors.extend(key_errors)
                continue
            results.append(
                {
                    "name": callback_name,
                    "callback_pointer": callback_pointer,
                    "expression_pointer": expression_pointer,
                    "expression": key,
                    "url": url,
                    "path_item": effective_item,
                }
            )

    if errors:
        raise CallbackResolutionError(_sorted(errors))

    results.sort(
        key=lambda item: (
            item["callback_pointer"],
            item["expression_pointer"],
        )
    )
    return results


def _sorted(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stable, order-independent error ordering."""

    def key(error: Dict[str, Any]):
        return (
            str(error.get("code")),
            str(error.get("callback_pointer") or ""),
            str(error.get("expression_pointer") or ""),
            str(error.get("expression") or ""),
            str(error.get("ref") or ""),
            str(error.get("error") or ""),
        )

    return sorted(errors, key=key)
