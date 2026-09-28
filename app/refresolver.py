"""Local ``$ref`` resolution for OpenAPI 3.x documents.

Only local references (``#/...`` JSON Pointer fragments, including the
self-reference ``#``) are supported, because the audit API receives two
self-contained documents. Remote / file / URL references cannot be fetched
and are reported as explicit, unresolvable-reference errors.

Circular references are a legitimate pattern (e.g. a ``Pet`` whose
``parent`` is again a ``Pet``), so the resolver itself follows a ``$ref``
chain while tracking every previously seen reference. Recursion loops are
detected and raised as :class:`CircularReferenceError`; callers that walk
schemas (see :mod:`app.audit`) avoid revisiting nodes, which keeps cycles
under control instead of expanding them infinitely.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
from urllib.parse import unquote

# A hard ceiling on the length of a single reference chain. OpenAPI
# documents of realistic size never come close; this is a safety net
# against pathological documents (A->B->C->... hundreds of levels deep).
MAX_REF_CHAIN = 100


class ReferenceError(Exception):
    """Base class for every reference resolution failure."""

    def __init__(self, ref: str, message: str) -> None:
        self.ref = ref
        super().__init__(message)


class UnresolvableReferenceError(ReferenceError):
    """A ``$ref`` points to a missing document location or a remote URI."""


class CircularReferenceError(ReferenceError):
    """A ``$ref`` chain revisits one of its own references."""


def _decode_token(token: str) -> str:
    """Decode one JSON Pointer reference token (RFC 6901 section 4)."""
    # Order matters: "~1" ("/") must be expanded before "~0" so that an
    # escaped tilde followed by "1" is not misread as an escaped slash.
    return unquote(token.replace("~1", "/").replace("~0", "~"))


def is_local_ref(ref: Any) -> bool:
    """True when *ref* is a document-local reference of the form ``#/...``.

    Mirrors the acceptance rule of :meth:`RefResolver.resolve`: remote URLs
    and protocol-relative (``//host/...``) references are not local.
    """
    if not isinstance(ref, str) or not ref or not ref.startswith("#"):
        return False
    return "://" not in ref and not ref.startswith("//")


def parse_local_ref(ref: str) -> str:
    """Return the JSON Pointer fragment of a local *ref* (without ``#``).

    ``#`` maps to the empty pointer (the document root); everything after
    the first ``#`` is the pointer.
    """
    return ref.split("#", 1)[1] if "#" in ref else ""


def _lookup_pointer(doc: Any, pointer: str, chain: List[str]) -> Any:
    """Resolve a JSON Pointer (the fragment after ``#``) inside *doc*."""
    if pointer == "":
        return doc

    if not pointer.startswith("/"):
        raise UnresolvableReferenceError(
            "#" + pointer,
            f"invalid JSON Pointer '#{pointer}': a pointer must start with '/' "
            "or be exactly '#'",
        )

    node: Any = doc
    traversed: List[str] = []
    for raw_token in pointer.split("/")[1:]:
        token = _decode_token(raw_token)
        traversed.append(raw_token)
        if isinstance(node, dict):
            if token not in node:
                full = "#" + pointer
                location = "#/" + "/".join(traversed)
                raise UnresolvableReferenceError(
                    full,
                    f"unresolvable reference '{full}' (fails at '{location}'): "
                    f"object has no property '{token}'",
                )
            node = node[token]
        elif isinstance(node, list):
            if not token.lstrip("-").isdigit():
                raise UnresolvableReferenceError(
                    "#" + pointer,
                    f"unresolvable reference '#{pointer}': '{token}' is not a "
                    "valid array index",
                )
            index = int(token)
            if index < 0 or index >= len(node):
                full = "#" + pointer
                location = "#/" + "/".join(traversed)
                raise UnresolvableReferenceError(
                    full,
                    f"unresolvable reference '{full}' (fails at '{location}'): "
                    f"array index {index} out of range (length {len(node)})",
                )
            node = node[index]
        else:
            full = "#" + pointer
            location = "#/" + "/".join(traversed[:-1]) if len(traversed) > 1 else "#"
            raise UnresolvableReferenceError(
                full,
                f"unresolvable reference '{full}': cannot descend into "
                f"'{token}' from scalar value at '{location}'",
            )
    return node


class RefResolver:
    """Resolves local references against a single OpenAPI document."""

    def __init__(self, doc: Dict[str, Any], *, label: str = "document") -> None:
        self.doc = doc
        self.label = label

    def resolve(
        self,
        node: Any,
        chain: Tuple[str, ...] = (),
    ) -> Tuple[Any, str]:
        """Follow every ``$ref`` starting at *node*.

        Returns ``(target, pointer)`` where *pointer* is the JSON Pointer of
        the final referenced location (``""`` for ``#``). Siblings of a
        ``$ref`` are ignored, as mandated by the JSON Reference / OpenAPI
        specifications.

        :raises UnresolvableReferenceError: the pointer cannot be followed
            or the reference is not local.
        :raises CircularReferenceError: the reference chain loops.
        """
        current = node
        seen: Dict[str, int] = {}

        while isinstance(current, dict) and "$ref" in current:
            ref = current["$ref"]
            if not isinstance(ref, str) or not ref:
                raise UnresolvableReferenceError(
                    str(ref), "$ref must be a non-empty string"
                )

            if "://" in ref or ref.startswith("//") or not ref.startswith("#"):
                raise UnresolvableReferenceError(
                    ref,
                    f"unsupported non-local reference '{ref}': only document-"
                    "local references of the form '#/...' are supported",
                )

            # Strip the fragment marker; everything after the first '#' is
            # the JSON Pointer.
            pointer = ref.split("#", 1)[1] if "#" in ref else ""

            if ref in seen:
                loop_start = list(seen).index(ref)
                loop = list(seen)[loop_start:] + [ref]
                raise CircularReferenceError(
                    ref,
                    f"circular reference detected: {' -> '.join(loop)}",
                )
            if len(chain) + len(seen) >= MAX_REF_CHAIN:
                raise CircularReferenceError(
                    ref,
                    f"reference chain exceeds the maximum depth of "
                    f"{MAX_REF_CHAIN} links (last reference: {ref})",
                )

            seen[ref] = len(seen)
            current = _lookup_pointer(self.doc, pointer, list(chain))

        # The pointer of the last successfully followed ref, if any.
        if not seen:
            return current, ""
        last_ref = next(reversed(seen))
        return current, last_ref.lstrip("#")

    def pointer(self, pointer: str) -> Any:
        """Resolve a JSON Pointer *without* a leading ``#`` against the doc.

        The empty string addresses the document root itself.

        :raises UnresolvableReferenceError: the pointer cannot be followed.
        """
        return _lookup_pointer(self.doc, pointer, [])

    def target(self, node: Any) -> Any:
        """Convenience wrapper returning only the resolved node."""
        return self.resolve(node)[0]
