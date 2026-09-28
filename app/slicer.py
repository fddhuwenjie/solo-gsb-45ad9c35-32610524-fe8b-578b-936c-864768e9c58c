"""Minimal operation sub-contract extraction (``POST /slice``).

Given an OpenAPI 3.x document and a list of ``METHOD /path`` selectors,
build the smallest contract that still describes exactly the selected
operations:

* the root ``openapi`` and ``info`` members are retained verbatim;
* only the selected paths appear under ``paths``, and on each path only
  path-level fields plus the selected operations (siblings such as the
  ``parameters`` shared by every method, ``servers``, ``summary`` and
  ``x-`` extensions survive; other HTTP methods are dropped);
* an operation that does not declare ``security`` inherits the root-level
  ``security`` requirement, which is materialized onto the operation so
  the sub-contract stays self-contained;
* ``components/securitySchemes`` is reduced to the schemes actually
  referenced by the surviving security requirements;
* every local ``$ref`` transitively reachable from a retained node is
  copied once into the sub-contract at its original JSON Pointer location,
  so every kept reference keeps resolving (a reference cycle only copies
  its targets once and never unfolds infinitely).

Only references reachable from retained nodes are validated: a dangling
or non-local reference hidden inside an unselected operation is irrelevant
to the slice and never blocks it.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, List, Optional, Set, Tuple

from .refresolver import (
    UnresolvableReferenceError,
    RefResolver,
    is_local_ref,
    parse_local_ref,
)

HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


class SliceError(Exception):
    """Base class for slice request failures."""


class InvalidSelectorError(SliceError):
    """The selector list is malformed (HTTP 400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class InvalidDocumentError(SliceError):
    """The submitted document is not a recognizable OpenAPI 3.x doc (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class SliceReferenceError(SliceError):
    """A retained node carries an unresolvable reference (HTTP 422)."""

    def __init__(self, references: List[Dict[str, str]]) -> None:
        self.references = references
        super().__init__(
            "; ".join(f"{r['ref']}: {r['error']}" for r in references)
        )


# --------------------------------------------------------------------------- #
# Selector parsing
# --------------------------------------------------------------------------- #

def parse_selector(raw: Any) -> Optional[Tuple[str, str]]:
    """Parse one ``"METHOD /path"`` selector.

    Returns ``(method, path)`` with the method lower-cased, or ``None``
    when the selector is not a string in the required shape (empty method
    or missing path are rejected as malformed).
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    parts = text.split(None, 1)
    if len(parts) != 2:
        return None
    method, path = parts[0].lower(), parts[1]
    if method not in HTTP_METHODS or not path or not path.startswith("/"):
        return None
    return method, path


# --------------------------------------------------------------------------- #
# Retained-node skeleton
# --------------------------------------------------------------------------- #

def validate_openapi_shape(doc: Any) -> List[str]:
    """Lightweight OpenAPI 3.x structural validation (mirrors /audit)."""
    if not isinstance(doc, dict):
        return ["document must be a JSON object"]
    messages: List[str] = []
    version = doc.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        messages.append(
            f"document.openapi must be a version string starting with '3.' "
            f"(OpenAPI 3.x), got {version!r}"
        )
    if not isinstance(doc.get("paths"), dict):
        messages.append("document.paths must be an object")
    return messages


def _resolve_path_item(
    resolver: RefResolver, raw_item: Any
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    """Return the effective path item, following a path-level ``$ref``.

    OpenAPI 3.1 allows ``$ref`` with sibling fields on path items; the
    referenced object supplies the methods and defaults, sibling fields on
    the referencing node win. Returns ``(path_item, error)`` where exactly
    one member is set.
    """
    if isinstance(raw_item, dict) and isinstance(raw_item.get("$ref"), str):
        ref = raw_item["$ref"]
        if not is_local_ref(ref):
            return None, {
                "ref": ref,
                "error": (
                    f"unsupported non-local reference '{ref}': only document-"
                    "local references of the form '#/...' are supported"
                ),
            }
        try:
            target = resolver.resolve({"$ref": ref})[0]
        except UnresolvableReferenceError as exc:
            return None, {"ref": exc.ref, "error": str(exc)}
        if not isinstance(target, dict):
            return None, {
                "ref": ref,
                "error": f"path item reference '{ref}' does not target an object",
            }
        merged = dict(target)
        # Sibling fields on the referencing node override referenced ones,
        # except "$ref" itself which must not leak into the inline copy.
        for key, value in raw_item.items():
            if key != "$ref":
                merged[key] = value
        return merged, None
    if not isinstance(raw_item, dict):
        return {}, None
    return raw_item, None


def _path_level_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    """Extract path-level (non-operation, non-reference) fields."""
    return {
        key: value
        for key, value in item.items()
        if key.lower() not in HTTP_METHODS
    }


def build_skeleton(
    doc: Dict[str, Any], selections: List[Tuple[str, str]]
) -> Tuple[Dict[str, Any], RefResolver, List[Dict[str, str]]]:
    """Construct the retained-node skeleton before ref-closure expansion.

    Returns ``(skeleton, resolver, reference_errors)``. Selector problems
    and unknown operations raise :class:`InvalidSelectorError` first, so
    the caller can answer 400 without touching reference resolution.
    """
    shape_errors = validate_openapi_shape(doc)
    if shape_errors:
        raise InvalidDocumentError(shape_errors)

    # Selector validation: duplicates and unknown methods/paths are 400.
    seen: Set[Tuple[str, str]] = set()
    duplicates: List[str] = []
    unknown: List[str] = []
    for method, path in selections:
        if (method, path) in seen:
            duplicates.append(f"{method.upper()} {path}")
            continue
        seen.add((method, path))
        if path not in doc["paths"]:
            unknown.append(f"{method.upper()} {path}: path is not declared")

    resolver = RefResolver(doc, label="document")

    # A method must exist on the *effective* path item (a path-level $ref
    # may supply it); a bad path-level reference is a 422, not an unknown
    # operation.
    effective_items: Dict[str, Dict[str, Any]] = {}
    reference_errors: List[Dict[str, str]] = []
    for path in {p for _, p in selections}:
        if path not in doc["paths"]:
            continue
        item, error = _resolve_path_item(resolver, doc["paths"][path])
        if error is not None:
            reference_errors.append(error)
            continue
        effective_items[path] = item  # type: ignore[assignment]

    if not reference_errors:
        for method, path in selections:
            if path not in doc["paths"]:
                continue
            item = effective_items[path]
            if method not in {k.lower(): k for k in item if k.lower() in HTTP_METHODS}:
                unknown.append(
                    f"{method.upper()} {path}: operation is not declared"
                )

    if duplicates or unknown:
        messages = [f"duplicate selector: {s}" for s in sorted(set(duplicates))]
        messages += [f"unknown operation: {s}" for s in sorted(set(unknown))]
        raise InvalidSelectorError(messages)

    root_security = doc.get("security")

    skeleton: Dict[str, Any] = {"openapi": doc["openapi"]}
    if "info" in doc:
        skeleton["info"] = copy.deepcopy(doc["info"])

    paths_out: Dict[str, Any] = {}
    # Group selections by path. Insertion order here cannot affect the
    # result: the final contract is rebuilt through canonical (sorted-key)
    # serialization before it is returned.
    methods_by_path: Dict[str, List[str]] = {}
    for method, path in selections:
        methods_by_path.setdefault(path, []).append(method)

    used_schemes: Set[str] = set()

    for path in methods_by_path:
        item = effective_items.get(path)
        if item is None:
            # Reference error for this path item was recorded above.
            continue
        path_item_out = _path_level_fields(item)
        for method in methods_by_path[path]:
            actual_key = next(
                k for k in item if k.lower() == method and k.lower() in HTTP_METHODS
            )
            operation = copy.deepcopy(item[actual_key])
            # Security inheritance: an operation without its own "security"
            # inherits the root-level requirement verbatim.
            if "security" not in operation and root_security is not None:
                operation["security"] = copy.deepcopy(root_security)
            for requirement in operation.get("security") or []:
                if isinstance(requirement, dict):
                    used_schemes.update(requirement)
            path_item_out[method] = operation
        paths_out[path] = path_item_out

    skeleton["paths"] = paths_out

    components_out: Dict[str, Any] = {}
    if used_schemes and isinstance(doc.get("components"), dict):
        all_schemes = doc["components"].get("securitySchemes")
        if isinstance(all_schemes, dict):
            kept = {
                name: copy.deepcopy(all_schemes[name])
                for name in used_schemes
                if name in all_schemes
            }
            if kept:
                components_out["securitySchemes"] = kept
    if components_out:
        skeleton["components"] = components_out

    return skeleton, resolver, reference_errors


# --------------------------------------------------------------------------- #
# $ref closure
# --------------------------------------------------------------------------- #

def _scan_node_refs(node: Any) -> List[str]:
    """Collect every ``$ref`` string directly or transitively in *node*.

    Siblings of a ``$ref`` are scanned too: in OpenAPI 3.1 a ``$ref`` object
    may carry sibling annotations whose content is also part of the slice.
    """
    refs: List[str] = []
    stack: List[Any] = [node]
    visited: Set[int] = set()
    while stack:
        current = stack.pop()
        if isinstance(current, (dict, list)) and id(current) in visited:
            continue
        if isinstance(current, dict):
            visited.add(id(current))
            ref = current.get("$ref")
            if isinstance(ref, str):
                refs.append(ref)
            stack.extend(current.values())
        elif isinstance(current, list):
            visited.add(id(current))
            stack.extend(current)
    return refs


def collect_closure(
    skeleton: Dict[str, Any], resolver: RefResolver
) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Compute the local ``$ref`` closure reachable from *skeleton*.

    Returns ``(targets, errors)`` where *targets* maps each referenced JSON
    Pointer (without leading ``#``) to the original target node. A pointer
    that is part of a reference cycle is copied once: it is marked visited
    before its own content is expanded, so recursion terminates.
    """
    targets: Dict[str, Any] = {}
    errors: Dict[str, Dict[str, str]] = {}
    pending: List[str] = []

    for ref in _scan_node_refs(skeleton):
        if not is_local_ref(ref):
            errors[ref] = {
                "ref": ref,
                "error": (
                    f"unsupported non-local reference '{ref}': only document-"
                    "local references of the form '#/...' are supported"
                ),
            }
            continue
        pending.append(parse_local_ref(ref))

    while pending:
        pointer = pending.pop()
        if pointer == "":
            # "#" targets the whole document root, which cannot be rebuilt
            # at a sub-location inside the sub-contract.
            errors.setdefault(
                "#",
                {
                    "ref": "#",
                    "error": (
                        "reference '#' targets the document root and cannot "
                        "be embedded in a sub-contract"
                    ),
                },
            )
            continue
        if pointer in targets:
            continue
        try:
            # Raw pointer lookup on purpose: the closure copies the node at
            # each pointer verbatim (including pure "$ref" nodes), so chains
            # must not be followed here -- following is what marks the next
            # pending pointer, and the "already copied" check below is what
            # makes cycles terminate with each target copied once.
            target = resolver.pointer(pointer)
        except UnresolvableReferenceError as exc:
            errors.setdefault(
                exc.ref, {"ref": exc.ref, "error": str(exc)}
            )
            continue
        # Mark before expanding: cycles end at the already-marked target.
        targets[pointer] = target
        for ref in _scan_node_refs(target):
            if not is_local_ref(ref):
                if ref not in errors:
                    errors[ref] = {
                        "ref": ref,
                        "error": (
                            f"unsupported non-local reference '{ref}': only "
                            "document-local references of the form '#/...' "
                            "are supported"
                        ),
                    }
                continue
            next_pointer = parse_local_ref(ref)
            if next_pointer not in targets:
                pending.append(next_pointer)

    return targets, [errors[key] for key in sorted(errors)]


# --------------------------------------------------------------------------- #
# Reconstruction at original JSON Pointer locations
# --------------------------------------------------------------------------- #

def _escape_token(token: str) -> str:
    """Escape one JSON Pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _decode_pointer(pointer: str) -> List[str]:
    return [
        token.replace("~1", "/").replace("~0", "~")
        for token in pointer.split("/")[1:]
    ]


def materialize(
    skeleton: Dict[str, Any], targets: Dict[str, Any]
) -> Dict[str, Any]:
    """Place each closure target under its original JSON Pointer.

    Targets are inserted in pointer order so array indices fill
    deterministically. A target location that the skeleton already occupies
    with a concrete node is left untouched (the retained node is the more
    specific copy); object subtrees otherwise merge in place. Closure
    targets may never add HTTP methods to a retained path item: the slice
    selects methods explicitly, so a dependency that happens to live at the
    pointer of an unselected sibling method is not rebuilt there.
    """
    # Methods already present on retained paths are the only method entries
    # closure reconstruction may touch. Pointer tokens arrive JSON-Pointer
    # decoded (e.g. the path "/a/b" appears as token "a~1b"), so key the
    # map by decoded path.
    allowed_methods: Dict[str, Set[str]] = {}
    skeleton_paths = skeleton.get("paths")
    if isinstance(skeleton_paths, dict):
        for path, item in skeleton_paths.items():
            if isinstance(item, dict):
                decoded = _decode_pointer("/" + _escape_token(path))[0]
                allowed_methods[decoded] = {
                    key for key in item if key.lower() in HTTP_METHODS
                }

    result = copy.deepcopy(skeleton)
    for pointer in sorted(targets):
        tokens = _decode_pointer(pointer)
        node: Any = result
        ok = True
        for depth, token in enumerate(tokens):
            last = depth == len(tokens) - 1
            # Closure reconstruction must not smuggle unselected sibling
            # operations back into a retained path item.
            if (
                len(tokens) >= 3
                and tokens[0] == "paths"
                and depth == 2
                and token.lower() in HTTP_METHODS
                and token not in allowed_methods.get(tokens[1], set())
            ):
                ok = False
                break
            if isinstance(node, dict):
                if last:
                    if token not in node:
                        node[token] = copy.deepcopy(targets[pointer])
                    elif isinstance(node[token], dict) and isinstance(
                        targets[pointer], dict
                    ):
                        for sub_key, sub_value in targets[pointer].items():
                            node[token].setdefault(
                                sub_key, copy.deepcopy(sub_value)
                            )
                    break
                if token not in node:
                    node[token] = {}
                node = node[token]
            elif isinstance(node, list):
                index = int(token)
                if last:
                    while len(node) <= index:
                        node.append(None)
                    if node[index] is None:
                        node[index] = copy.deepcopy(targets[pointer])
                    break
                while len(node) <= index:
                    node.append(None)
                if node[index] is None:
                    node[index] = {}
                node = node[index]
            else:
                # Skeleton scalar blocks descent; the retained node wins.
                ok = False
                break
        if not ok:
            continue
    return result


# --------------------------------------------------------------------------- #
# Canonical serialization
# --------------------------------------------------------------------------- #

def canonical_json(doc: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, compact separators, UTF-8.

    Key order therefore depends solely on the document's key/value set,
    never on input object key order or selector order.
    """
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def slice_document(
    doc: Any, raw_selectors: Any
) -> Tuple[Dict[str, Any], str]:
    """End-to-end slicing. Returns ``(sub_contract, sha256_hex)``.

    :raises InvalidDocumentError: *doc* is not an OpenAPI 3.x object.
    :raises InvalidSelectorError: selectors are malformed, duplicated, or
        point at paths/operations the document does not declare.
    :raises SliceReferenceError: a node the slice keeps (or a closure
        target it reaches) contains a dangling/non-local reference.
    """
    if not isinstance(raw_selectors, list) or not raw_selectors:
        raise InvalidSelectorError(
            ["selectors must be a non-empty array of 'METHOD /path' strings"]
        )

    selections: List[Tuple[str, str]] = []
    malformed: List[str] = []
    for raw in raw_selectors:
        parsed = parse_selector(raw)
        if parsed is None:
            malformed.append(repr(raw))
        else:
            selections.append(parsed)
    if malformed:
        raise InvalidSelectorError(
            [
                f"malformed selector {value}: expected 'METHOD /path' "
                "(e.g. 'GET /pets')"
                for value in malformed
            ]
        )

    skeleton, resolver, ref_errors = build_skeleton(doc, selections)
    targets, closure_errors = collect_closure(skeleton, resolver)
    all_errors = ref_errors + closure_errors
    if all_errors:
        # De-duplicate by reference string, keep output order-independent.
        deduped = {error["ref"]: error for error in all_errors}
        raise SliceReferenceError([deduped[key] for key in sorted(deduped)])

    sub_contract = materialize(skeleton, targets)
    encoded = canonical_json(sub_contract)
    # Round-trip through the canonical encoding so the delivered object
    # itself carries stable key order, not just its digest.
    canonical_contract = json.loads(encoded.decode("utf-8"))
    return canonical_contract, hashlib.sha256(encoded).hexdigest()
