"""Gateway contract composition (``POST /compose``).

Merges 2..20 OpenAPI 3.x source documents -- each tagged with a unique
``service_id`` and mounted under a ``path_prefix`` -- into a single
gateway contract plus a provenance (source) map.

Composition rules
-----------------
* Path prefixes are normalized (leading ``/`` enforced, trailing ``/``
  stripped; ``""`` and ``"/"`` both mount the service at the gateway
  root) and prepended to every source path. Two services contributing
  the same HTTP method to the same composed path is a conflict
  (HTTP 409) reported with both origins; different methods on one
  composed path merge into a single path item (``parameters`` are
  concatenated, every other path-level field is first-wins in request
  order). Conflict detection sees through OpenAPI 3.1 path-level
  ``$ref`` chains: the effective methods of a path item are resolved
  layer by layer (cycle-safe, sibling fields win over referenced ones),
  so a method supplied via ``components/pathItems`` collides with a
  directly declared one just the same. When several services contribute
  path-level ``$ref``\ s to one composed path, the first reference is
  kept and the effective operations of the remaining chains are inlined
  as sibling operations, so every non-conflicting method stays
  resolvable from the composed path item.
* Each source's root-level ``security`` is materialized onto every
  operation that does not declare ``security`` (an explicit
  ``security: []`` is preserved), so the composed contract carries no
  root-level security of its own.
* Every component name, security requirement key, ``operationId`` and tag
  is namespaced with the ``<service_id>_`` prefix so names cannot
  collide across services; every local ``$ref`` of the form
  ``#/components/...`` is rewritten to the prefixed component name.
* References must stay inside ``components``: external references, and
  local references to any other document location, are rejected
  (HTTP 422) together with the JSON Pointer of the offending ``$ref``
  in the source document. Only retained content (``paths``,
  ``components``, ``tags``) is validated -- a bad reference inside a
  dropped root field such as ``webhooks`` never blocks composition.
* The provenance map traces every composed path, operation and component
  back to its source service and source JSON Pointer.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

from .refresolver import (
    MAX_REF_CHAIN,
    RefResolver,
    UnresolvableReferenceError,
    is_local_ref,
    parse_local_ref,
)
from .slicer import HTTP_METHODS, validate_openapi_shape

MIN_SERVICES = 2
MAX_SERVICES = 20

# service_id becomes part of every component key, so it must stay inside
# the OpenAPI component-name alphabet (^[a-zA-Z0-9.\-_]+$).
_SERVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Root-level source fields retained in the composed contract. Everything
# else is either replaced (``info`` comes from the request), consumed
# (root ``security`` is materialized onto operations) or intentionally
# dropped (``servers``, ``webhooks``, ``externalDocs``, ``x-`` ...).
_RETAINED_ROOT_KEYS = frozenset({"paths", "components", "tags"})


class ComposeError(Exception):
    """Base class for compose request failures."""


class InvalidComposeRequestError(ComposeError):
    """The compose request itself is malformed (HTTP 400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class ComposeReferenceError(ComposeError):
    """A retained node carries an unsupported reference (HTTP 422)."""

    def __init__(self, references: List[Dict[str, str]]) -> None:
        self.references = references
        super().__init__(
            "; ".join(f"{r['ref']}: {r['error']}" for r in references)
        )


class OperationConflictError(ComposeError):
    """Two services claim the same composed path + method (HTTP 409)."""

    def __init__(self, conflicts: List[Dict[str, Any]]) -> None:
        self.conflicts = conflicts
        super().__init__(
            "; ".join(
                f"{c['method'].upper()} {c['path']}" for c in conflicts
            )
        )


class _Service(NamedTuple):
    service_id: str
    path_prefix: str
    document: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #

def normalize_path_prefix(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Normalize a mount prefix. Returns ``(prefix, error_message)``.

    Exactly one of the two return values is set. A leading ``/`` is
    enforced and trailing slashes are stripped; ``""`` and ``"/"`` both
    map to ``""`` (the service is mounted at the gateway root). Prefixes
    containing whitespace, ``?`` or ``#`` are rejected: they cannot
    appear in a valid URL path.
    """
    text = raw.strip()
    if text in ("", "/"):
        return "", None
    if any(ch.isspace() for ch in text) or "?" in text or "#" in text:
        return None, (
            f"path_prefix {raw!r} must not contain whitespace, '?' or '#'"
        )
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/"), None


def _validate_request(info: Any, services: Any) -> List[_Service]:
    """Validate the raw compose request. Raises HTTP-400 errors."""
    messages: List[str] = []
    if not isinstance(info, dict):
        messages.append("info must be a JSON object (the composed API's info)")
    if not isinstance(services, list):
        messages.append(
            f"services must be an array of {MIN_SERVICES}..{MAX_SERVICES} "
            "entries"
        )
        raise InvalidComposeRequestError(messages)
    if not MIN_SERVICES <= len(services) <= MAX_SERVICES:
        messages.append(
            f"services must contain between {MIN_SERVICES} and "
            f"{MAX_SERVICES} entries, got {len(services)}"
        )

    specs: List[_Service] = []
    seen: Dict[str, int] = {}
    for index, entry in enumerate(services):
        label = f"services[{index}]"
        if not isinstance(entry, dict):
            messages.append(
                f"{label} must be an object with service_id, path_prefix "
                "and document"
            )
            continue

        sid = entry.get("service_id")
        sid_ok = False
        if not isinstance(sid, str) or not sid:
            messages.append(f"{label}.service_id must be a non-empty string")
        elif not _SERVICE_ID_PATTERN.match(sid):
            messages.append(
                f"{label}.service_id {sid!r} is invalid: use letters, "
                "digits, '.', '_' or '-' (starting with a letter or digit) "
                "so prefixed names stay valid OpenAPI component keys"
            )
        elif sid in seen:
            messages.append(
                f"duplicate service_id {sid!r} (services[{seen[sid]}] and "
                f"services[{index}])"
            )
        else:
            seen[sid] = index
            sid_ok = True

        prefix: Optional[str] = None
        raw_prefix = entry.get("path_prefix")
        if not isinstance(raw_prefix, str):
            messages.append(f"{label}.path_prefix must be a string")
        else:
            prefix, prefix_error = normalize_path_prefix(raw_prefix)
            if prefix_error is not None:
                messages.append(f"{label}: {prefix_error}")

        document = entry.get("document")
        doc_ok = True
        for shape_error in validate_openapi_shape(document):
            messages.append(f"{label}.document: {shape_error}")
            doc_ok = False
        if doc_ok:
            for path in document["paths"]:
                if not isinstance(path, str) or not path.startswith("/"):
                    messages.append(
                        f"{label}.document.paths key {path!r} must start "
                        "with '/'"
                    )
                    doc_ok = False

        if sid_ok and prefix is not None and doc_ok:
            specs.append(_Service(sid, prefix, document))

    if messages:
        raise InvalidComposeRequestError(messages)
    return specs


# --------------------------------------------------------------------------- #
# Per-service namespacing transforms
# --------------------------------------------------------------------------- #

def _prefixed(service_id: str, name: str) -> str:
    """Namespace *name* with the service prefix."""
    return f"{service_id}_{name}"


def _transform_operation(
    operation: Dict[str, Any], root_security: Any, service_id: str
) -> None:
    """Materialize inherited security and namespace operation fields."""
    # Security inheritance: an operation without its own "security"
    # inherits the source's root-level requirement (explicit [] stays).
    if "security" not in operation and root_security is not None:
        operation["security"] = copy.deepcopy(root_security)
    security = operation.get("security")
    if isinstance(security, list):
        for requirement in security:
            if isinstance(requirement, dict):
                renamed = {
                    _prefixed(service_id, str(scheme)): scopes
                    for scheme, scopes in requirement.items()
                }
                requirement.clear()
                requirement.update(renamed)
    operation_id = operation.get("operationId")
    if isinstance(operation_id, str) and operation_id:
        operation["operationId"] = _prefixed(service_id, operation_id)
    tags = operation.get("tags")
    if isinstance(tags, list):
        operation["tags"] = [
            _prefixed(service_id, tag) if isinstance(tag, str) else tag
            for tag in tags
        ]


def _transform_path_item(item: Any, root_security: Any, service_id: str) -> None:
    if not isinstance(item, dict):
        return
    for key, value in item.items():
        if isinstance(key, str) and key.lower() in HTTP_METHODS and isinstance(
            value, dict
        ):
            _transform_operation(value, root_security, service_id)


def _transform_document(doc: Dict[str, Any], service_id: str) -> None:
    """Apply the per-service namespacing transforms in place.

    Operations are transformed wherever they can live: directly under
    ``paths`` and, for OpenAPI 3.1, inside ``components/pathItems``
    (a path item that is itself a ``$ref`` keeps referencing its --
    renamed -- component, so the operations are transformed there).
    Root tag declarations are namespaced like operation tags.
    """
    root_security = doc.get("security")
    if not isinstance(root_security, list):
        root_security = None

    paths = doc.get("paths")
    if isinstance(paths, dict):
        for item in paths.values():
            _transform_path_item(item, root_security, service_id)

    components = doc.get("components")
    if isinstance(components, dict):
        path_items = components.get("pathItems")
        if isinstance(path_items, dict):
            for item in path_items.values():
                _transform_path_item(item, root_security, service_id)

    tags = doc.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict) and isinstance(tag.get("name"), str):
                tag["name"] = _prefixed(service_id, tag["name"])


# --------------------------------------------------------------------------- #
# $ref rewriting
# --------------------------------------------------------------------------- #

def _escape_token(token: str) -> str:
    """Escape one JSON Pointer reference token (RFC 6901)."""
    return token.replace("~", "~0").replace("/", "~1")


def _decode_token(token: str) -> str:
    """Decode one JSON Pointer reference token (RFC 6901 section 4)."""
    return token.replace("~1", "/").replace("~0", "~")


def _rewrite_components_ref(ref: str, service_id: str) -> Optional[str]:
    """Rewrite a local ``#/components/...`` reference to the prefixed name.

    Returns the rewritten reference, or ``None`` when *ref* does not name
    a component (the required shape is
    ``#/components/<section>/<name>[/...]``; deeper pointers into a
    component keep their tail).
    """
    pointer = parse_local_ref(ref)
    tokens = pointer.split("/")[1:] if pointer.startswith("/") else []
    if len(tokens) < 3 or tokens[0] != "components":
        return None
    tokens[2] = _escape_token(_prefixed(service_id, _decode_token(tokens[2])))
    return "#/" + "/".join(tokens)


def _rewrite_references(
    doc: Dict[str, Any], service_id: str
) -> List[Dict[str, str]]:
    """Rewrite every ``$ref`` in *doc* in place; return unsupported ones.

    Every error carries the JSON Pointer of the object holding the
    ``$ref`` in the source document. A node graph shared between several
    locations is processed once (first location wins), so a reference is
    never rewritten twice.
    """
    resolver = RefResolver(doc, label=service_id)
    errors: List[Dict[str, str]] = []
    stack: List[Tuple[Any, str]] = [(doc, "")]
    visited: Set[int] = set()
    while stack:
        node, pointer = stack.pop()
        if id(node) in visited:
            continue
        visited.add(id(node))
        if isinstance(node, dict):
            if "$ref" in node:
                ref = node["$ref"]
                if not isinstance(ref, str) or not ref:
                    errors.append(
                        {
                            "service_id": service_id,
                            "pointer": pointer,
                            "ref": str(ref),
                            "error": "$ref must be a non-empty string",
                        }
                    )
                elif not is_local_ref(ref):
                    errors.append(
                        {
                            "service_id": service_id,
                            "pointer": pointer,
                            "ref": ref,
                            "error": (
                                f"unsupported non-local reference '{ref}': "
                                "only document-local references of the form "
                                "'#/components/...' are supported"
                            ),
                        }
                    )
                else:
                    rewritten = _rewrite_components_ref(ref, service_id)
                    if rewritten is None:
                        errors.append(
                            {
                                "service_id": service_id,
                                "pointer": pointer,
                                "ref": ref,
                                "error": (
                                    f"unsupported reference '{ref}': "
                                    "references must target components "
                                    "('#/components/<section>/<name>'); "
                                    "references to other document locations "
                                    "cannot be rewritten into the composed "
                                    "contract"
                                ),
                            }
                        )
                    else:
                        try:
                            resolver.pointer(parse_local_ref(ref))
                        except UnresolvableReferenceError as exc:
                            errors.append(
                                {
                                    "service_id": service_id,
                                    "pointer": pointer,
                                    "ref": ref,
                                    "error": str(exc),
                                }
                            )
                        else:
                            node["$ref"] = rewritten
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    stack.append((value, f"{pointer}/{_escape_token(key)}"))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                if isinstance(value, (dict, list)):
                    stack.append((value, f"{pointer}/{index}"))
    return errors


# --------------------------------------------------------------------------- #
# Effective path items (OpenAPI 3.1 path-level $ref chains)
# --------------------------------------------------------------------------- #

def _components_ref_pointer(ref: str) -> Optional[str]:
    """Canonical JSON Pointer of a local ``#/components/...`` *ref*.

    Returns ``None`` for anything else (external references, references
    outside ``components``); those are reported by ``_rewrite_references``
    and are simply not followed here.
    """
    if not is_local_ref(ref):
        return None
    pointer = parse_local_ref(ref)
    tokens = pointer.split("/")[1:] if pointer.startswith("/") else []
    if len(tokens) < 3 or tokens[0] != "components":
        return None
    return pointer


def _resolve_pointer(doc: Dict[str, Any], pointer: str) -> Any:
    """Resolve a JSON Pointer (no leading ``#``) against *doc*.

    Only used with pointers derived from the document's own structure
    (see :func:`_effective_path_items`), so every token is known to exist.
    """
    node: Any = doc
    if pointer:
        for raw_token in pointer.split("/")[1:]:
            node = node[_decode_token(raw_token)]
    return node


def _effective_path_items(
    doc: Dict[str, Any], service_id: str
) -> Tuple[Dict[str, Dict[str, Dict[str, str]]], List[Dict[str, str]]]:
    """Resolve the effective methods of every path item, cycle-safely.

    For each path the local ``$ref`` chain is followed layer by layer
    (only ``#/components/...`` references are followed, mirroring the
    rewrite rule) and the terminal target is merged with the sibling
    fields of every intermediate node -- nearer siblings win, so a
    sibling ``get`` on the referencing path item overrides the one the
    chain supplies. Returns ``(views, reference_errors)`` where *views*
    maps each source path to ``{method: {"pointer": <source JSON
    Pointer of the operation>}}`` (method keys lower-cased) and
    *reference_errors* carries the 422-shaped entries for chains that
    loop, exceed the depth ceiling, or do not resolve to an object.
    Dangling/external/non-components references are left to
    ``_rewrite_references`` (they abort composition before merging).
    """
    resolver = RefResolver(doc, label=service_id)
    views: Dict[str, Dict[str, Dict[str, str]]] = {}
    errors: List[Dict[str, str]] = []

    paths = doc.get("paths")
    if not isinstance(paths, dict):
        return views, errors

    for path, raw_item in paths.items():
        item_pointer = "/paths/" + _escape_token(path)
        if not isinstance(raw_item, dict):
            continue

        # Walk the $ref chain, remembering each visited node together
        # with the JSON Pointer of its location in the source document.
        layers: List[Tuple[Dict[str, Any], str]] = [(raw_item, item_pointer)]
        refs: List[str] = []
        seen_pointers: Dict[str, int] = {}
        broken = False
        while True:
            node, node_pointer = layers[-1]
            ref = node.get("$ref")
            if not isinstance(ref, str) or not ref:
                break  # terminal node (or a malformed $ref; 422 elsewhere)
            target_pointer = _components_ref_pointer(ref)
            if target_pointer is None:
                break  # not a followable components reference (422 elsewhere)
            if target_pointer in seen_pointers:
                loop = refs[seen_pointers[target_pointer]:] + [ref]
                errors.append(
                    {
                        "service_id": service_id,
                        "pointer": node_pointer,
                        "ref": ref,
                        "error": (
                            "circular reference detected: "
                            + " -> ".join(loop)
                        ),
                    }
                )
                broken = True
                break
            if len(refs) >= MAX_REF_CHAIN:
                errors.append(
                    {
                        "service_id": service_id,
                        "pointer": node_pointer,
                        "ref": ref,
                        "error": (
                            "reference chain exceeds the maximum depth of "
                            f"{MAX_REF_CHAIN} links (last reference: {ref})"
                        ),
                    }
                )
                broken = True
                break
            seen_pointers[target_pointer] = len(refs)
            refs.append(ref)
            try:
                target = resolver.pointer(target_pointer)
            except UnresolvableReferenceError:
                # Dangling reference: reported by _rewrite_references with
                # the exact failure location; nothing to resolve here.
                broken = True
                break
            if not isinstance(target, dict):
                errors.append(
                    {
                        "service_id": service_id,
                        "pointer": node_pointer,
                        "ref": ref,
                        "error": (
                            f"path item reference '{ref}' does not target "
                            "an object"
                        ),
                    }
                )
                broken = True
                break
            layers.append((target, target_pointer))
        if broken:
            continue

        # Overlay from the terminal target back to the path item itself so
        # nearer sibling fields win; record where each method comes from.
        methods: Dict[str, Dict[str, str]] = {}
        for node, node_pointer in reversed(layers):
            for key in node:
                if (
                    key != "$ref"
                    and isinstance(key, str)
                    and key.lower() in HTTP_METHODS
                ):
                    methods[key.lower()] = {
                        "pointer": node_pointer + "/" + _escape_token(key)
                    }
        views[path] = methods
    return views, errors


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #

def _version_key(version: str) -> Tuple[int, ...]:
    parts: List[int] = []
    for piece in version.split("."):
        if piece.isdigit():
            parts.append(int(piece))
        else:
            break
    return tuple(parts)


def _max_version(versions: List[str]) -> str:
    """The highest ``openapi`` version string among the sources."""
    best = versions[0]
    for version in versions[1:]:
        if _version_key(version) > _version_key(best):
            best = version
    return best


def _merge(
    prepared: List[Tuple[_Service, Dict[str, Any], Dict[str, Dict[str, Dict[str, str]]]]],
    info: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """Merge transformed documents. Returns (contract, provenance, conflicts).

    Each prepared entry carries the effective-methods view of its source
    document (see :func:`_effective_path_items`): methods a path item
    only gains through a path-level ``$ref`` chain take part in conflict
    detection and provenance exactly like directly declared ones. The
    first ``$ref`` contributed to a composed path item is kept (reference
    shape preserved); the effective operations of any further ``$ref``
    chain landing on the same composed path cannot resolve through that
    kept reference, so they are inlined as sibling operations -- OpenAPI
    3.1 merges ``$ref`` siblings over the referenced path item, and
    non-conflicting methods by definition do not overlap the kept chain.
    """
    out_paths: Dict[str, Any] = {}
    out_tags: List[Any] = []
    tag_names: Set[str] = set()
    out_components: Dict[str, Any] = {}
    conflicts: List[Dict[str, Any]] = []
    prov_paths: Dict[str, Any] = {}
    prov_operations: Dict[str, Any] = {}
    prov_components: Dict[str, Any] = {}
    versions: List[str] = []
    # Composed path -> service_id whose path-level ``$ref`` was kept.
    ref_owners: Dict[str, str] = {}

    for service, doc, views in prepared:
        sid = service.service_id
        versions.append(str(service.document.get("openapi")))

        paths = doc.get("paths")
        for path, item in (paths or {}).items():
            composed = service.path_prefix + path
            source_pointer = "/paths/" + _escape_token(path)
            prov_paths.setdefault(composed, []).append(
                {
                    "service_id": sid,
                    "source_path": path,
                    "source_pointer": source_pointer,
                }
            )
            target = out_paths.setdefault(composed, {})
            if not isinstance(item, dict):
                continue

            def claim(method: str, source: Dict[str, str]) -> bool:
                """Record *method*'s origin; False + conflict when taken."""
                operations = prov_operations.setdefault(composed, {})
                if method in operations:
                    conflicts.append(
                        {
                            "path": composed,
                            "method": method,
                            "sources": [operations[method], source],
                        }
                    )
                    return False
                operations[method] = source
                return True

            direct_methods: Set[str] = set()
            for key, value in item.items():
                if isinstance(key, str) and key.lower() in HTTP_METHODS:
                    method = key.lower()
                    direct_methods.add(method)
                    challenger = {
                        "service_id": sid,
                        "source_path": path,
                        "source_pointer": source_pointer
                        + "/"
                        + _escape_token(key),
                    }
                    if claim(method, challenger):
                        target[method] = value
                elif (
                    key == "parameters"
                    and isinstance(target.get("parameters"), list)
                    and isinstance(value, list)
                ):
                    # Path-level parameters merge across services;
                    # identical entries are kept once (first wins).
                    for parameter in value:
                        if parameter not in target["parameters"]:
                            target["parameters"].append(parameter)
                elif key not in target:
                    # Any other path-level field is first-wins.
                    target[key] = value
                    if key == "$ref":
                        ref_owners[composed] = sid

            # Methods this path item only provides through its $ref chain
            # still collide with (or map to) operations of other services.
            view = views.get(path) or {}
            for method in sorted(view):
                if method in direct_methods:
                    continue
                claimed = claim(
                    method,
                    {
                        "service_id": sid,
                        "source_path": path,
                        "source_pointer": view[method]["pointer"],
                    },
                )
                if not claimed or ref_owners.get(composed) == sid:
                    # A conflict is reported as 409; and when the composed
                    # item kept this service's own $ref, the method already
                    # resolves through the referenced pathItems component.
                    continue
                # The kept $ref belongs to another service, so this method
                # would be lost from the composed path: inline the
                # effective (already namespaced) operation as a sibling.
                operation = _resolve_pointer(doc, view[method]["pointer"])
                target[method] = copy.deepcopy(operation)

        for tag in doc.get("tags") or []:
            if isinstance(tag, dict) and isinstance(tag.get("name"), str):
                if tag["name"] not in tag_names:
                    tag_names.add(tag["name"])
                    out_tags.append(tag)

        components = doc.get("components")
        if isinstance(components, dict):
            for section, entries in components.items():
                if not isinstance(entries, dict):
                    # Non-component-map values (e.g. x- extensions directly
                    # under components) are kept verbatim, first wins.
                    out_components.setdefault(section, entries)
                    continue
                section_out = out_components.setdefault(section, {})
                section_prov = prov_components.setdefault(section, {})
                for name, component in entries.items():
                    renamed = _prefixed(sid, name)
                    section_out[renamed] = component
                    section_prov[renamed] = {
                        "service_id": sid,
                        "source_pointer": "/components/"
                        + _escape_token(section)
                        + "/"
                        + _escape_token(name),
                    }

    contract: Dict[str, Any] = {
        "openapi": _max_version(versions),
        "info": copy.deepcopy(info),
    }
    if out_tags:
        contract["tags"] = out_tags
    contract["paths"] = out_paths
    if out_components:
        contract["components"] = out_components

    provenance = {
        "paths": prov_paths,
        "operations": prov_operations,
        "components": prov_components,
    }
    return contract, provenance, conflicts


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def compose_gateway(
    info: Any, services: Any
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """End-to-end composition. Returns ``(contract, provenance)``.

    :raises InvalidComposeRequestError: the request is malformed (HTTP
        400): bad service count, missing/duplicate/invalid service ids,
        invalid prefixes, or documents that are not OpenAPI 3.x.
    :raises ComposeReferenceError: a retained node carries an external,
        dangling, or non-components reference, or a path-level ``$ref``
        chain loops (HTTP 422).
    :raises OperationConflictError: two services declare the same
        composed path and HTTP method (HTTP 409).
    """
    specs = _validate_request(info, services)

    prepared: List[
        Tuple[_Service, Dict[str, Any], Dict[str, Dict[str, Dict[str, str]]]]
    ] = []
    reference_errors: List[Dict[str, str]] = []
    for service in specs:
        doc = copy.deepcopy(service.document)
        # Resolve effective path items before any rewriting so method
        # provenance points into the source document.
        views, view_errors = _effective_path_items(doc, service.service_id)
        reference_errors.extend(view_errors)
        _transform_document(doc, service.service_id)
        # Drop root fields that do not enter the composed contract before
        # validating references: only retained content is checked.
        for key in list(doc):
            if key not in _RETAINED_ROOT_KEYS:
                del doc[key]
        reference_errors.extend(_rewrite_references(doc, service.service_id))
        prepared.append((service, doc, views))

    if reference_errors:
        deduped = {
            (e["service_id"], e["pointer"], e["ref"], e["error"]): e
            for e in reference_errors
        }
        reference_errors = sorted(
            deduped.values(),
            key=lambda e: (e["service_id"], e["pointer"], e["ref"]),
        )
        raise ComposeReferenceError(reference_errors)

    contract, provenance, conflicts = _merge(prepared, info)
    if conflicts:
        raise OperationConflictError(conflicts)
    return contract, provenance
