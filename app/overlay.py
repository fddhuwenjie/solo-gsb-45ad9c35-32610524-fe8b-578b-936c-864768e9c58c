"""Ordered structural overlay actions on an OpenAPI document
(``POST /overlay/apply``).

The request carries one OpenAPI 3.x document and 1..50 ordered actions.
Each action addresses one or more nodes with a *restricted JSONPath*
``target`` and either merges a value into them (``update``) or deletes
them (``remove``):

* Target syntax is deliberately small: ``$`` addresses the root,
  ``.name`` a member (one or more of ``[A-Za-z0-9_]``; anything else,
  e.g. ``/pets`` or ``x-custom``, needs the quoted form),
  ``['name']`` / ``["name"]`` an arbitrary member (backslash escapes
  supported), ``[0]`` an array index (no negatives, no leading zeros),
  and ``.*`` / ``[*]`` a wildcard over every member of an object or
  element of an array. Filters (``?``) and recursive descent (``..``)
  are rejected as syntax errors.
* ``update`` recursively merges two objects member by member; any other
  combination replaces the target value wholesale. Arrays are never
  merged element-wise -- they are replaced as a whole.
* ``remove`` may not delete the root node (``$``).
* Within one action every match is frozen (evaluated against the state
  left by the previous actions) before anything is applied; array
  element removals run in descending index order so earlier deletions
  cannot shift the addresses of later ones. Subsequent actions match
  against the result of the previous ones.
* The batch is atomic: a malformed action, a target syntax error, or a
  target that matches zero nodes rejects the whole request with HTTP 422
  (the error body carries the action index) and no partial result is
  returned. The request document is never modified.
"""

from __future__ import annotations

import copy
import hashlib
import json
import string
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .slicer import canonical_json, validate_openapi_shape

MIN_ACTIONS = 1
MAX_ACTIONS = 50


class OverlayError(Exception):
    """Base class for overlay request failures."""


class InvalidOverlayDocumentError(OverlayError):
    """The submitted document is not a recognizable OpenAPI 3.x doc (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class InvalidOverlayRequestError(OverlayError):
    """The overlay request envelope is malformed (HTTP 400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class InvalidActionError(OverlayError):
    """One or more actions are malformed or carry a bad target (HTTP 422)."""

    def __init__(self, actions: List[Dict[str, Any]]) -> None:
        self.actions = actions
        super().__init__(
            "; ".join(
                f"action {a['action_index']}: {a['error']}" for a in actions
            )
        )


class NoMatchError(OverlayError):
    """An action target matched zero nodes (HTTP 422)."""

    def __init__(self, action_index: int, target: str) -> None:
        self.action_index = action_index
        self.target = target
        self.actions = [
            {
                "action_index": action_index,
                "target": target,
                "error": f"target '{target}' matched 0 nodes",
            }
        ]
        super().__init__(self.actions[0]["error"])


# --------------------------------------------------------------------------- #
# Restricted JSONPath target parsing
# --------------------------------------------------------------------------- #

# Dot-notation member names: one or more of these characters. Anything
# else (``/``, ``-``, spaces, unicode, ...) needs the quoted bracket form.
_DOT_NAME_CHARS = frozenset(string.ascii_letters + string.digits + "_")

# Backslash escapes recognized inside quoted member names.
_ESCAPES = {
    "\\": "\\",
    "'": "'",
    '"': '"',
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_HEX_DIGITS = frozenset(string.hexdigits)


class Segment(NamedTuple):
    """One target path segment: member access, array index, or wildcard."""

    kind: str  # "member" | "index" | "wildcard"
    value: Any = None


class TargetSyntaxError(Exception):
    """A target string is not valid restricted JSONPath."""

    def __init__(self, target: str, position: int, reason: str) -> None:
        self.target = target
        self.position = position
        self.reason = reason
        super().__init__(f"invalid target {target!r}: {reason} (position {position})")


def _parse_quoted_name(target: str, start: int) -> Tuple[str, int]:
    """Parse a quoted member name starting at *start* (the quote character).

    Returns ``(name, next_position)`` where *next_position* is the index
    just past the closing quote.
    """
    quote = target[start]
    i = start + 1
    n = len(target)
    chars: List[str] = []
    while i < n:
        ch = target[i]
        if ch == "\\":
            if i + 1 >= n:
                raise TargetSyntaxError(target, i, "unterminated escape sequence")
            nxt = target[i + 1]
            if nxt == "u":
                digits = target[i + 2 : i + 6]
                if len(digits) < 4 or any(c not in _HEX_DIGITS for c in digits):
                    raise TargetSyntaxError(
                        target, i, "invalid \\u escape: expected 4 hex digits"
                    )
                chars.append(chr(int(digits, 16)))
                i += 6
                continue
            if nxt not in _ESCAPES:
                raise TargetSyntaxError(
                    target, i, f"unsupported escape sequence '\\{nxt}'"
                )
            chars.append(_ESCAPES[nxt])
            i += 2
            continue
        if ch == quote:
            return "".join(chars), i + 1
        chars.append(ch)
        i += 1
    raise TargetSyntaxError(target, start, "unterminated quoted member name")


def parse_target(target: str) -> List[Segment]:
    """Parse a restricted JSONPath *target* into segments.

    Grammar (no whitespace allowed anywhere)::

        target  := '$' segment*
        segment := '.' name | '.*'
                 | '[' index ']' | '[' quoted ']' | '[*]'

    :raises TargetSyntaxError: the target does not match the grammar
        (including filters ``?`` and recursive descent ``..``).
    """
    if not isinstance(target, str) or not target:
        raise TargetSyntaxError(
            str(target), 0, "target must be a non-empty string starting with '$'"
        )
    if not target.startswith("$"):
        raise TargetSyntaxError(target, 0, "target must start with '$'")

    segments: List[Segment] = []
    i = 1
    n = len(target)
    while i < n:
        ch = target[i]
        if ch == ".":
            i += 1
            if i < n and target[i] == "*":
                segments.append(Segment("wildcard"))
                i += 1
                continue
            if i >= n or target[i] not in _DOT_NAME_CHARS:
                raise TargetSyntaxError(
                    target, i, "expected a member name or '*' after '.'"
                )
            start = i + 1
            while start < n and target[start] in _DOT_NAME_CHARS:
                start += 1
            segments.append(Segment("member", target[i:start]))
            i = start
        elif ch == "[":
            i += 1
            if i >= n:
                raise TargetSyntaxError(target, i, "unterminated '[' selector")
            inner = target[i]
            if inner == "*":
                i += 1
                if i >= n or target[i] != "]":
                    raise TargetSyntaxError(
                        target, i, "expected ']' after '[*'"
                    )
                segments.append(Segment("wildcard"))
                i += 1
            elif inner in "'\"":
                name, i = _parse_quoted_name(target, i)
                if i >= n or target[i] != "]":
                    raise TargetSyntaxError(
                        target, i, "expected ']' after quoted member name"
                    )
                segments.append(Segment("member", name))
                i += 1
            elif inner.isdigit():
                start = i
                while i < n and target[i].isdigit():
                    i += 1
                text = target[start:i]
                if len(text) > 1 and text.startswith("0"):
                    raise TargetSyntaxError(
                        target, start, "array indices must not have leading zeros"
                    )
                if i >= n or target[i] != "]":
                    raise TargetSyntaxError(
                        target, i, "expected ']' after array index"
                    )
                segments.append(Segment("index", int(text)))
                i += 1
            else:
                raise TargetSyntaxError(
                    target,
                    i,
                    "expected a quoted member name, array index or '*' after '['",
                )
        else:
            raise TargetSyntaxError(target, i, "expected '.' or '['")
    return segments


# --------------------------------------------------------------------------- #
# Target evaluation
# --------------------------------------------------------------------------- #

def _escape_token(token: str) -> str:
    """Escape one JSON Pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _decode_token(token: str) -> str:
    """Decode one JSON Pointer reference token (RFC 6901)."""
    return token.replace("~1", "/").replace("~0", "~")


def evaluate_target(doc: Any, segments: List[Segment]) -> List[str]:
    """Return the JSON Pointers of every node in *doc* matched by *segments*.

    The result may be empty (a member that does not exist, an index out of
    range, a wildcard over a scalar, ...); the caller turns that into a
    422. Matches are reported in deterministic evaluation order (object
    members are visited in sorted key order).
    """
    current: List[Tuple[Any, str]] = [(doc, "")]
    for segment in segments:
        following: List[Tuple[Any, str]] = []
        for node, pointer in current:
            if segment.kind == "member":
                if isinstance(node, dict) and segment.value in node:
                    following.append(
                        (node[segment.value], pointer + "/" + _escape_token(segment.value))
                    )
            elif segment.kind == "index":
                if isinstance(node, list) and 0 <= segment.value < len(node):
                    following.append(
                        (node[segment.value], pointer + "/" + str(segment.value))
                    )
            else:  # wildcard
                if isinstance(node, dict):
                    for key in sorted(node):
                        following.append(
                            (node[key], pointer + "/" + _escape_token(key))
                        )
                elif isinstance(node, list):
                    for index in range(len(node)):
                        following.append(
                            (node[index], pointer + "/" + str(index))
                        )
        current = following
    return [pointer for _, pointer in current]


# --------------------------------------------------------------------------- #
# Action application
# --------------------------------------------------------------------------- #

def _split_pointer(pointer: str) -> Tuple[str, str]:
    """Split a JSON Pointer into ``(parent_pointer, decoded_last_token)``."""
    parent, _, raw = pointer.rpartition("/")
    return parent, _decode_token(raw)


def _resolve_pointer(doc: Any, pointer: str) -> Any:
    """Follow a JSON Pointer built by :func:`evaluate_target`."""
    node = doc
    if pointer == "":
        return node
    for raw in pointer.split("/")[1:]:
        token = _decode_token(raw)
        node = node[token] if isinstance(node, dict) else node[int(token)]
    return node


def _merge_value(existing: Any, update: Any) -> Any:
    """Merge *update* into *existing*.

    Two objects merge member by member, recursively; every other
    combination (including arrays on either side) replaces the existing
    value wholesale. Update content is always deep-copied so the result
    never aliases the request object.
    """
    if isinstance(existing, dict) and isinstance(update, dict):
        for key, value in update.items():
            if key in existing:
                existing[key] = _merge_value(existing[key], value)
            else:
                existing[key] = copy.deepcopy(value)
        return existing
    return copy.deepcopy(update)


def _apply_update(doc: Any, pointers: List[str], update: Any) -> Any:
    """Apply one ``update`` action to every frozen match.

    Returns the document (it is only ever a different object when the
    root itself is replaced by a non-object update value).
    """
    for pointer in pointers:
        if pointer == "":
            doc = _merge_value(doc, update)
            continue
        parent_pointer, token = _split_pointer(pointer)
        parent_node = _resolve_pointer(doc, parent_pointer)
        if isinstance(parent_node, list):
            index = int(token)
            parent_node[index] = _merge_value(parent_node[index], update)
        else:
            parent_node[token] = _merge_value(parent_node[token], update)
    return doc


def _apply_remove(doc: Any, pointers: List[str]) -> None:
    """Apply one ``remove`` action to every frozen match.

    Matches are grouped by their parent node; within one array the
    elements are deleted in descending index order so a deletion cannot
    shift the address of a sibling that is still pending.
    """
    parents: Dict[str, List[str]] = {}
    for pointer in pointers:
        parent_pointer, token = _split_pointer(pointer)
        parents.setdefault(parent_pointer, []).append(token)
    for parent_pointer in sorted(parents):
        parent_node = _resolve_pointer(doc, parent_pointer)
        tokens = parents[parent_pointer]
        if isinstance(parent_node, list):
            for token in sorted((int(t) for t in tokens), reverse=True):
                del parent_node[token]
        else:
            for token in sorted(tokens):
                del parent_node[token]


# --------------------------------------------------------------------------- #
# Action validation and batch driver
# --------------------------------------------------------------------------- #

_ACTION_KEYS = frozenset({"target", "update", "remove"})


def _validate_action(
    index: int, action: Any
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Validate one action's shape and parse its target.

    Returns ``(prepared, error)`` where exactly one member is set; the
    error dict always carries ``action_index``.
    """
    if not isinstance(action, dict):
        return None, {
            "action_index": index,
            "error": f"action must be a JSON object, got {type(action).__name__}",
        }
    unknown = sorted(set(action) - _ACTION_KEYS)
    if unknown:
        return None, {
            "action_index": index,
            "error": f"unknown action field(s): {', '.join(unknown)}",
        }
    target = action.get("target")
    if not isinstance(target, str) or not target:
        return None, {
            "action_index": index,
            "error": "action.target must be a non-empty string",
        }
    has_update = "update" in action
    has_remove = "remove" in action
    if has_update == has_remove:
        return None, {
            "action_index": index,
            "target": target,
            "error": "action must contain exactly one of 'update' or 'remove'",
        }
    if has_remove and action["remove"] is not True:
        return None, {
            "action_index": index,
            "target": target,
            "error": "action.remove must be true",
        }
    try:
        segments = parse_target(target)
    except TargetSyntaxError as exc:
        return None, {
            "action_index": index,
            "target": target,
            "error": str(exc),
        }
    if has_remove and not segments:
        return None, {
            "action_index": index,
            "target": target,
            "error": "the root node '$' cannot be removed",
        }
    prepared = {
        "target": target,
        "segments": segments,
        "op": "update" if has_update else "remove",
        "value": action.get("update"),
    }
    return prepared, None


def apply_overlay(
    doc: Any, raw_actions: Any
) -> Tuple[Any, List[Dict[str, Any]], str]:
    """Apply the ordered overlay actions to *doc*.

    Returns ``(contract, action_reports, sha256_hex)`` where *contract*
    is the transformed document in canonical (sorted-key) form and each
    report entry carries ``action_index``, ``op``, ``target`` and the
    sorted list of matched JSON Pointers.

    :raises InvalidOverlayDocumentError: the document is not an OpenAPI
        3.x object (HTTP 400).
    :raises InvalidOverlayRequestError: the action count is outside
        1..50 (HTTP 400).
    :raises InvalidActionError: an action is malformed or its target has
        a syntax error (HTTP 422).
    :raises NoMatchError: an action target matched zero nodes (HTTP 422).
    """
    shape_errors = validate_openapi_shape(doc)
    if shape_errors:
        raise InvalidOverlayDocumentError(shape_errors)
    if not isinstance(raw_actions, list) or not (
        MIN_ACTIONS <= len(raw_actions) <= MAX_ACTIONS
    ):
        count = len(raw_actions) if isinstance(raw_actions, list) else 0
        raise InvalidOverlayRequestError(
            [
                f"actions must be an array of {MIN_ACTIONS}..{MAX_ACTIONS} "
                f"actions, got {count}"
            ]
        )

    prepared: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for index, action in enumerate(raw_actions):
        prep, error = _validate_action(index, action)
        if error is not None:
            errors.append(error)
        else:
            prepared.append(prep)
    if errors:
        raise InvalidActionError(errors)

    # Never mutate the request object: work on a private deep copy.
    working = copy.deepcopy(doc)
    reports: List[Dict[str, Any]] = []
    for index, prep in enumerate(prepared):
        # Freeze every match of this action against the state left by the
        # previous actions before anything is applied.
        matches = sorted(evaluate_target(working, prep["segments"]))
        if not matches:
            raise NoMatchError(index, prep["target"])
        if prep["op"] == "remove":
            _apply_remove(working, matches)
        else:
            working = _apply_update(working, matches, prep["value"])
        reports.append(
            {
                "action_index": index,
                "op": prep["op"],
                "target": prep["target"],
                "matches": matches,
            }
        )

    encoded = canonical_json(working)
    # Round-trip so the delivered contract itself carries stable key order.
    contract = json.loads(encoded.decode("utf-8"))
    return contract, reports, hashlib.sha256(encoded).hexdigest()
