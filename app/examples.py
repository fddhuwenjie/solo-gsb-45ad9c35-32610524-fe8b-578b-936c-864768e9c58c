"""Validation of OpenAPI 3.x media-type examples (``POST /examples/validate``).

The service walks every operation's ``requestBody`` and every response,
picks the JSON media types (``application/json`` and structured ``+json``
types), and validates the examples they carry against the media type's
schema:

* the inline ``example`` member of a Media Type Object;
* every ``examples.<name>.value`` entry, including Example Objects reached
  through a document-local ``$ref`` (``#/components/examples/...``).

Direction semantics mirror the OpenAPI specification:

* a **request** example must not contain ``readOnly`` properties, and a
  **response** example must not contain ``writeOnly`` properties;
* ``required`` only takes effect in the matching direction -- a
  ``readOnly`` property listed in ``required`` is enforced on responses
  only, and a ``writeOnly`` one on requests only; within an ``allOf``
  combination the effective ``properties`` of every branch are aggregated
  cycle-safely before validation, so a direction marker declared on one
  branch (or a property/branch reached via a local ``$ref``) exempts a
  ``required`` member contributed by a sibling branch, while each branch's
  other constraints are still enforced independently;
* OpenAPI 3.0 ``nullable: true`` admits ``null``, whereas OpenAPI 3.1 uses
  the ``"null"`` type-union member and ignores ``nullable``.

Local ``$ref`` references (in schemas and Example Objects) are resolved
cycle-safely. ``externalValue`` entries, non-local references and
dangling local references cannot be checked offline and produce HTTP 422
together with the JSON Pointer of the offending node. Pure schema
violations are never an error response: the endpoint answers ``200``
with ``valid = false`` and a stably sorted error list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from .audit import ptr
from .refresolver import (
    CircularReferenceError,
    RefResolver,
    UnresolvableReferenceError,
    is_local_ref,
)
from .slicer import HTTP_METHODS, validate_openapi_shape

# Defensive ceiling on nested validation (deep inline schemas, deeply
# nested instances) independent of $ref cycle protection.
MAX_VALIDATION_DEPTH = 300

# Floating-point tolerance for multipleOf with fractional divisors.
_MULTIPLE_OF_EPSILON = 1e-9


class ExampleValidationError(Exception):
    """Base class for example-validation request failures."""


class InvalidExampleDocumentError(ExampleValidationError):
    """The submitted document is not a recognizable OpenAPI 3.x doc (400)."""

    def __init__(self, messages: List[str]) -> None:
        self.messages = messages
        super().__init__("; ".join(messages))


class ExampleReferenceError(ExampleValidationError):
    """An example/schema reference cannot be validated offline (HTTP 422)."""

    def __init__(
        self, code: str, message: str, references: List[Dict[str, str]]
    ) -> None:
        self.code = code
        self.message = message
        self.references = references
        super().__init__(
            "; ".join(f"{r['pointer']}: {r['ref']}" for r in references)
        )


@dataclass(frozen=True)
class ExampleError:
    """One example-vs-schema violation."""

    side: str  # "request" | "response"
    example_pointer: str
    instance_pointer: str
    schema_pointer: str
    keyword: str
    reason: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "side": self.side,
            "example_pointer": self.example_pointer,
            "instance_pointer": self.instance_pointer,
            "schema_pointer": self.schema_pointer,
            "keyword": self.keyword,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# Reference issue collection
# --------------------------------------------------------------------------- #

class _RefIssues:
    """Collects 422-class reference problems with their source pointer."""

    def __init__(self) -> None:
        self.external: Dict[Tuple[str, str], Dict[str, str]] = {}
        self.dangling: Dict[Tuple[str, str], Dict[str, str]] = {}
        self.invalid: Dict[Tuple[str, str], Dict[str, str]] = {}

    def _add(
        self,
        bucket: Dict[Tuple[str, str], Dict[str, str]],
        pointer: str,
        ref: str,
        error: str,
    ) -> None:
        bucket.setdefault((pointer, ref), {"pointer": pointer, "ref": ref, "error": error})

    def follow(
        self, resolver: RefResolver, node: Any, pointer: str
    ) -> Tuple[Any, Optional[str]]:
        """Resolve *node* when it is a ``$ref`` object.

        Returns ``(target, physical_pointer)`` where *physical_pointer*
        follows the reference to its final component location (the stable
        place to fix problems, mirroring the audit pointer convention).
        On failure records the issue and returns ``(None, None)``.
        """
        if isinstance(node, dict) and isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            if not is_local_ref(ref):
                self._add(
                    self.external,
                    pointer,
                    ref,
                    f"unsupported non-local reference '{ref}': only document-local "
                    "references of the form '#/...' are supported",
                )
                return None, None
            try:
                target, target_pointer = resolver.resolve(node)
            except UnresolvableReferenceError as exc:
                self._add(self.dangling, pointer, exc.ref, str(exc))
                return None, None
            except CircularReferenceError as exc:
                self._add(self.dangling, pointer, exc.ref, str(exc))
                return None, None
            return target, target_pointer
        return node, pointer

    def external_value(self, pointer: str, url: str) -> None:
        self._add(
            self.external,
            pointer,
            url,
            f"externalValue '{url}' cannot be fetched; examples must be embedded "
            "locally (examples.<name>.value) to be validated",
        )

    def non_object_target(self, pointer: str, ref: Optional[str]) -> None:
        if isinstance(ref, str):
            message = f"example reference '{ref}' does not target an Example Object"
        else:
            message = "example entry is not an Example Object"
        self._add(
            self.invalid,
            pointer,
            ref if isinstance(ref, str) else pointer,
            message,
        )

    def raise_if_any(self) -> None:
        if self.external:
            raise ExampleReferenceError(
                "unsupported_reference",
                "one or more examples use externalValue or a non-local $ref; only "
                "document-local references and embedded values can be validated",
                [self.external[key] for key in sorted(self.external)],
            )
        if self.invalid:
            raise ExampleReferenceError(
                "invalid_example",
                "one or more example references do not target an Example Object",
                [self.invalid[key] for key in sorted(self.invalid)],
            )
        if self.dangling:
            raise ExampleReferenceError(
                "unresolvable_reference",
                "one or more $ref entries reached while validating examples could "
                "not be resolved; fix the references first",
                [self.dangling[key] for key in sorted(self.dangling)],
            )


# --------------------------------------------------------------------------- #
# JSON value helpers
# --------------------------------------------------------------------------- #

def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_equal(left: Any, right: Any) -> bool:
    """JSON-semantics equality (``true`` != 1, deep for arrays/objects)."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if _is_number(left) and _is_number(right):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(
            _json_equal(left[k], right[k]) for k in left
        )
    return False


def _contains_equal(value: Any, sequence: List[Any]) -> bool:
    return any(_json_equal(value, item) for item in sequence)


# --------------------------------------------------------------------------- #
# Schema validator
# --------------------------------------------------------------------------- #

class SchemaValidator:
    """Validates one example value against a media-type schema.

    Reference problems encountered while resolving schemas are recorded in
    *issues*; the endpoint turns any of them into 422.
    """

    def __init__(
        self,
        doc: Dict[str, Any],
        resolver: RefResolver,
        side: str,
        example_pointer: str,
        issues: _RefIssues,
    ) -> None:
        self.doc = doc
        self.resolver = resolver
        self.side = side
        self.example_pointer = example_pointer
        self.issues = issues
        self.is31 = str(doc.get("openapi", "")).startswith(("3.1", "3.2"))

    def run(
        self, schema_root: Any, value: Any, schema_pointer: str = ""
    ) -> List[ExampleError]:
        errors: List[ExampleError] = []
        self._validate(
            schema_root, schema_pointer, value, "", errors, set(), 0, None
        )
        return errors

    # -- entry --------------------------------------------------------------

    def _validate(
        self,
        node: Any,
        schema_pointer: str,
        instance: Any,
        instance_pointer: str,
        errors: List[ExampleError],
        stack: Set[Tuple[int, int]],
        depth: int,
        allof_properties: Optional[Dict[str, List[Tuple[Any, str]]]],
    ) -> None:
        if depth > MAX_VALIDATION_DEPTH:
            return

        target, physical = self.issues.follow(
            self.resolver, node, schema_pointer
        )
        if target is None:
            # A 422-class reference problem; abort this branch.
            return
        schema_pointer = physical if physical is not None else schema_pointer

        # JSON Schema 2020-12 (OpenAPI 3.1) allows boolean schemas.
        if target is True:
            return
        if target is False:
            self._emit(
                errors, instance_pointer, schema_pointer, "schema",
                "schema is 'false', which forbids every value",
            )
            return
        if not isinstance(target, dict):
            return

        # Effective properties of this same allOf-combined object, so that a
        # direction marker declared on a sibling branch (or a $ref it uses)
        # can exempt a required property contributed by another branch.
        effective_properties = self._collect_allof_properties(
            target, schema_pointer, allof_properties, depth
        )

        # Cycle guard keyed on the resolved schema node plus the concrete
        # instance: recursive properties/items consume a smaller instance,
        # so they terminate naturally; only a self-referential combinator
        # revisits the exact same pair and is cut here.
        guard = (id(target), id(instance))
        if guard in stack:
            return
        stack = stack | {guard}

        self._check_type(target, schema_pointer, instance, instance_pointer, errors)
        self._check_enum(target, schema_pointer, instance, instance_pointer, errors)
        self._check_const(target, schema_pointer, instance, instance_pointer, errors)

        if isinstance(instance, dict):
            self._check_object(
                target, schema_pointer, instance, instance_pointer,
                errors, stack, depth, effective_properties,
            )
        elif isinstance(instance, list):
            self._check_array(
                target, schema_pointer, instance, instance_pointer,
                errors, stack, depth,
            )
        elif isinstance(instance, str):
            self._check_string(target, schema_pointer, instance, instance_pointer, errors)
        if _is_number(instance):
            self._check_number(target, schema_pointer, instance, instance_pointer, errors)

        self._check_combinators(
            target, schema_pointer, instance, instance_pointer,
            errors, stack, depth, effective_properties,
        )

    def _emit(
        self,
        errors: List[ExampleError],
        instance_pointer: str,
        schema_pointer: str,
        keyword: str,
        reason: str,
    ) -> None:
        errors.append(
            ExampleError(
                side=self.side,
                example_pointer=self.example_pointer,
                instance_pointer=instance_pointer,
                schema_pointer=schema_pointer,
                keyword=keyword,
                reason=reason,
            )
        )

    # -- type / nullability -------------------------------------------------

    def _allowed_types(self, schema: Dict[str, Any]) -> Optional[Set[str]]:
        """Normalize ``type`` plus the dialect-specific null marker."""
        raw = schema.get("type")
        if raw is None:
            return None
        if isinstance(raw, list):
            types = {str(t) for t in raw}
        else:
            types = {str(raw)}
        # OpenAPI 3.0 admits null via "nullable: true"; OpenAPI 3.1 uses the
        # "null" type-union member and does not recognize "nullable".
        if not self.is31 and schema.get("nullable") is True:
            types.add("null")
        return types

    @staticmethod
    def _type_matches(actual: str, instance: Any, allowed: Set[str]) -> bool:
        if actual in allowed:
            return True
        # integer is contained in number (JSON type hierarchy).
        if actual == "integer" and "number" in allowed:
            return True
        # A JSON number without a fractional part also satisfies "integer".
        if actual == "number" and "integer" in allowed:
            return isinstance(instance, float) and instance.is_integer()
        return False

    def _check_type(
        self, schema, schema_pointer, instance, instance_pointer, errors
    ) -> None:
        allowed = self._allowed_types(schema)
        if allowed is None:
            return
        actual = _json_type(instance)
        if not self._type_matches(actual, instance, allowed):
            expected = "|".join(sorted(allowed))
            self._emit(
                errors, instance_pointer, schema_pointer, "type",
                f"expected type {expected}, got {actual}",
            )

    def _check_enum(
        self, schema, schema_pointer, instance, instance_pointer, errors
    ) -> None:
        enum = schema.get("enum")
        if isinstance(enum, list) and not _contains_equal(instance, enum):
            self._emit(
                errors, instance_pointer, schema_pointer, "enum",
                f"value is not one of the {len(enum)} allowed enum values",
            )

    def _check_const(
        self, schema, schema_pointer, instance, instance_pointer, errors
    ) -> None:
        if "const" in schema and not _json_equal(instance, schema["const"]):
            self._emit(
                errors, instance_pointer, schema_pointer, "const",
                "value does not equal the const value",
            )

    # -- objects ------------------------------------------------------------

    def _collect_allof_properties(
        self,
        schema: Dict[str, Any],
        schema_pointer: str,
        inherited: Optional[Dict[str, List[Tuple[Any, str]]]],
        depth: int,
    ) -> Optional[Dict[str, List[Tuple[Any, str]]]]:
        """Aggregate every effective ``properties`` annotation of one object
        that is combined with ``allOf`` (this node plus each branch, with
        branches themselves reachable through a local ``$ref`` and allowed to
        flatten further ``allOf`` chains).

        Returns ``None`` when *schema* is not part of an ``allOf``
        combination; otherwise a property name -> list of
        ``(property-schema node, source pointer)`` pairs. The nodes stay
        unresolved here so that resolving them (and any reference issue) is
        shared with the regular validation path instead of being reported
        eagerly from a side walk; the source pointer is the physical
        location of the properties entry, so reference issues stay
        attributed correctly. Cycle-safe: each resolved schema node is
        visited at most once, and the walk is bounded by
        :data:`MAX_VALIDATION_DEPTH`.
        """
        all_of = schema.get("allOf")
        if not isinstance(all_of, list) and inherited is None:
            return None

        collected: Dict[str, List[Tuple[Any, str]]] = {
            name: list(pairs) for name, pairs in (inherited or {}).items()
        }
        seen_pairs: Dict[str, Set[int]] = {}
        visited: Set[int] = set()

        def add_properties(node: Dict[str, Any], base_pointer: str) -> None:
            properties = node.get("properties")
            if not isinstance(properties, dict):
                return
            for name, prop_node in properties.items():
                key = str(name)
                bucket = collected.setdefault(key, [])
                marker = seen_pairs.setdefault(key, set())
                if id(prop_node) not in marker:
                    marker.add(id(prop_node))
                    bucket.append((prop_node, base_pointer + ptr("properties", key)))

        def walk(node: Any, node_pointer: str, level: int) -> None:
            if level > MAX_VALIDATION_DEPTH:
                return
            target, physical = self.issues.follow(self.resolver, node, node_pointer)
            if not isinstance(target, dict):
                return
            if id(target) in visited:
                return
            visited.add(id(target))
            add_properties(target, physical)
            nested = target.get("allOf")
            if isinstance(nested, list):
                for index, sub in enumerate(nested):
                    walk(sub, physical + ptr("allOf", index), level + 1)

        add_properties(schema, schema_pointer)
        if isinstance(all_of, list):
            for index, branch in enumerate(all_of):
                walk(branch, schema_pointer + ptr("allOf", index), depth + 1)
        return collected

    def _resolve_property_schema(
        self, prop_node: Any, prop_pointer: str
    ) -> Tuple[Any, Optional[str]]:
        return self.issues.follow(self.resolver, prop_node, prop_pointer)

    @staticmethod
    def _flag(schema: Any, name: str) -> bool:
        return isinstance(schema, dict) and schema.get(name) is True

    def _check_object(
        self, schema, schema_pointer, instance, instance_pointer,
        errors, stack, depth, effective_properties=None,
    ) -> None:
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}

        # Within an allOf combination, a property's effective annotations
        # (readOnly/writeOnly) are the union of every branch that declares
        # it; candidates include the local properties and all allOf
        # siblings, each resolved through a local $ref when present.
        if effective_properties is None:
            effective_properties = {
                name: [(prop_node, schema_pointer + ptr("properties", name))]
                for name, prop_node in properties.items()
            }

        # required is direction-aware: readOnly members are required on the
        # response side only, writeOnly members on the request side only.
        # The marker may live on another allOf branch (or a $ref it uses),
        # which is why all effective property schemas are consulted here.
        required = schema.get("required")
        if isinstance(required, list):
            for raw_name in sorted(str(r) for r in required if isinstance(r, str)):
                if raw_name in instance:
                    continue
                skip = False
                for candidate, prop_pointer in effective_properties.get(raw_name, ()):
                    prop_target, _ = self._resolve_property_schema(
                        candidate, prop_pointer
                    )
                    if self.side == "request" and self._flag(prop_target, "readOnly"):
                        skip = True
                        break
                    if self.side == "response" and self._flag(prop_target, "writeOnly"):
                        skip = True
                        break
                if not skip:
                    self._emit(
                        errors, instance_pointer, schema_pointer, "required",
                        f"required property '{raw_name}' is missing",
                    )

        for name in sorted(instance, key=str):
            child_pointer = instance_pointer + ptr(name)
            if name in properties:
                prop_pointer = schema_pointer + ptr("properties", name)
                prop_target, prop_physical = self._resolve_property_schema(
                    properties[name], prop_pointer
                )
                if prop_target is None:
                    continue
                if self.side == "request" and self._flag(prop_target, "readOnly"):
                    self._emit(
                        errors, child_pointer,
                        prop_physical or prop_pointer, "readOnly",
                        f"readOnly property '{name}' must not appear in a request example",
                    )
                elif self.side == "response" and self._flag(prop_target, "writeOnly"):
                    self._emit(
                        errors, child_pointer,
                        prop_physical or prop_pointer, "writeOnly",
                        f"writeOnly property '{name}' must not appear in a response example",
                    )
                self._validate(
                    prop_target, prop_physical or prop_pointer,
                    instance[name], child_pointer, errors, stack, depth + 1,
                    None,
                )
                continue

            additional = schema.get("additionalProperties")
            if additional is False:
                self._emit(
                    errors, child_pointer, schema_pointer, "additionalProperties",
                    f"additional property '{name}' is not allowed",
                )
            elif isinstance(additional, dict):
                self._validate(
                    additional, schema_pointer + "/additionalProperties",
                    instance[name], child_pointer, errors, stack, depth + 1,
                    None,
                )

        size = len(instance)
        minimum = schema.get("minProperties")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and size < minimum:
            self._emit(
                errors, instance_pointer, schema_pointer, "minProperties",
                f"object has {size} properties, minProperties is {minimum}",
            )
        maximum = schema.get("maxProperties")
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and size > maximum:
            self._emit(
                errors, instance_pointer, schema_pointer, "maxProperties",
                f"object has {size} properties, maxProperties is {maximum}",
            )

    # -- arrays -------------------------------------------------------------

    def _check_array(
        self, schema, schema_pointer, instance, instance_pointer,
        errors, stack, depth,
    ) -> None:
        size = len(instance)
        minimum = schema.get("minItems")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and size < minimum:
            self._emit(
                errors, instance_pointer, schema_pointer, "minItems",
                f"array has {size} items, minItems is {minimum}",
            )
        maximum = schema.get("maxItems")
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and size > maximum:
            self._emit(
                errors, instance_pointer, schema_pointer, "maxItems",
                f"array has {size} items, maxItems is {maximum}",
            )
        if schema.get("uniqueItems") is True:
            seen: List[Any] = []
            for index, item in enumerate(instance):
                if _contains_equal(item, seen):
                    self._emit(
                        errors, instance_pointer + ptr(index),
                        schema_pointer, "uniqueItems",
                        f"item at index {index} duplicates an earlier item",
                    )
                    break
                seen.append(item)

        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(instance):
                self._validate(
                    items, schema_pointer + "/items",
                    item, instance_pointer + ptr(index),
                    errors, stack, depth + 1, None,
                )
        elif isinstance(items, list):
            # OpenAPI 3.1 "items" array / prefix-tuple style: positional.
            for index, item in enumerate(instance[: len(items)]):
                self._validate(
                    items[index], schema_pointer + ptr("items", index),
                    item, instance_pointer + ptr(index),
                    errors, stack, depth + 1, None,
                )

    # -- strings ------------------------------------------------------------

    def _check_string(self, schema, schema_pointer, instance, instance_pointer, errors) -> None:
        size = len(instance)
        minimum = schema.get("minLength")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and size < minimum:
            self._emit(
                errors, instance_pointer, schema_pointer, "minLength",
                f"string has length {size}, minLength is {minimum}",
            )
        maximum = schema.get("maxLength")
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and size > maximum:
            self._emit(
                errors, instance_pointer, schema_pointer, "maxLength",
                f"string has length {size}, maxLength is {maximum}",
            )
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and pattern:
            try:
                matched = re.search(pattern, instance) is not None
            except re.error:
                matched = True
            if not matched:
                self._emit(
                    errors, instance_pointer, schema_pointer, "pattern",
                    f"string does not match pattern '{pattern}'",
                )

    # -- numbers ------------------------------------------------------------

    def _check_number(self, schema, schema_pointer, instance, instance_pointer, errors) -> None:
        multiple = schema.get("multipleOf")
        if _is_number(multiple) and multiple > 0:
            ratio = instance / multiple
            if abs(ratio - round(ratio)) > _MULTIPLE_OF_EPSILON:
                self._emit(
                    errors, instance_pointer, schema_pointer, "multipleOf",
                    f"value {instance} is not a multiple of {multiple}",
                )

        lower, lower_strict = self._bound(schema, "minimum", "exclusiveMinimum")
        if lower is not None and (
            instance < lower or (lower_strict and instance == lower)
        ):
            keyword = "exclusiveMinimum" if lower_strict else "minimum"
            self._emit(
                errors, instance_pointer, schema_pointer, keyword,
                f"value {instance} is below the {keyword} {lower}",
            )
        upper, upper_strict = self._bound(schema, "maximum", "exclusiveMaximum")
        if upper is not None and (
            instance > upper or (upper_strict and instance == upper)
        ):
            keyword = "exclusiveMaximum" if upper_strict else "maximum"
            self._emit(
                errors, instance_pointer, schema_pointer, keyword,
                f"value {instance} is above the {keyword} {upper}",
            )

    @staticmethod
    def _bound(
        schema: Dict[str, Any], inclusive: str, exclusive: str
    ) -> Tuple[Optional[float], bool]:
        """Resolve a min/max bound across the 3.0 and 3.1 keyword forms.

        OpenAPI 3.0 uses ``minimum`` together with a boolean
        ``exclusiveMinimum``; JSON Schema 2020-12 (OpenAPI 3.1) lets
        ``exclusiveMinimum`` carry the numeric bound itself.
        """
        raw_exclusive = schema.get(exclusive)
        if _is_number(raw_exclusive):
            return float(raw_exclusive), True
        bound = schema.get(inclusive)
        if not _is_number(bound):
            return None, False
        return float(bound), raw_exclusive is True

    # -- allOf / anyOf / oneOf ----------------------------------------------

    def _check_combinators(
        self, schema, schema_pointer, instance, instance_pointer,
        errors, stack, depth, effective_properties=None,
    ) -> None:
        all_of = schema.get("allOf")
        if isinstance(all_of, list):
            # The branches describe the same object, so they share the
            # aggregated allOf property view for direction-aware required.
            for index, subschema in enumerate(all_of):
                self._validate(
                    subschema, schema_pointer + ptr("allOf", index),
                    instance, instance_pointer, errors, stack, depth + 1,
                    effective_properties,
                )

        for keyword in ("anyOf", "oneOf"):
            variants = schema.get(keyword)
            if not isinstance(variants, list) or not variants:
                continue
            branch_errors: List[List[ExampleError]] = []
            for index, subschema in enumerate(variants):
                branch: List[ExampleError] = []
                # Alternatives are independent: an allOf view from the
                # enclosing schema must not leak across variants.
                self._validate(
                    subschema, schema_pointer + ptr(keyword, index),
                    instance, instance_pointer, branch, stack, depth + 1,
                    None,
                )
                branch_errors.append(branch)
            matches = [i for i, branch in enumerate(branch_errors) if not branch]
            if keyword == "anyOf" and not matches:
                self._emit(
                    errors, instance_pointer, schema_pointer, "anyOf",
                    f"value matches none of the {len(variants)} anyOf subschemas",
                )
            if keyword == "oneOf" and len(matches) != 1:
                self._emit(
                    errors, instance_pointer, schema_pointer, "oneOf",
                    f"value matches {len(matches)} of the {len(variants)} oneOf "
                    "subschemas; exactly one must match",
                )


# --------------------------------------------------------------------------- #
# Document traversal
# --------------------------------------------------------------------------- #

def _is_json_media_type(media: Any) -> bool:
    return isinstance(media, str) and (
        media == "application/json" or media.endswith("+json")
    )


def _validate_one_example(
    doc: Dict[str, Any],
    resolver: RefResolver,
    issues: _RefIssues,
    side: str,
    schema: Any,
    schema_pointer: str,
    value: Any,
    example_pointer: str,
    errors: List[ExampleError],
) -> None:
    if schema is None:
        # A media type without a schema places no constraints; nothing to
        # validate (externalValue is still rejected by the caller).
        return
    validator = SchemaValidator(doc, resolver, side, example_pointer, issues)
    errors.extend(validator.run(schema, value, schema_pointer))


def _walk_examples_of_media_type(
    doc: Dict[str, Any],
    resolver: RefResolver,
    issues: _RefIssues,
    side: str,
    media_node: Dict[str, Any],
    media_pointer: str,
    errors: List[ExampleError],
) -> None:
    schema = media_node.get("schema")
    schema_pointer = media_pointer + "/schema"

    if "example" in media_node:
        _validate_one_example(
            doc, resolver, issues, side, schema, schema_pointer,
            media_node["example"], media_pointer + "/example", errors,
        )

    examples = media_node.get("examples")
    if not isinstance(examples, dict):
        return
    for name in sorted(examples, key=str):
        entry_pointer = media_pointer + ptr("examples", name)
        entry, physical = issues.follow(
            resolver, examples[name], entry_pointer
        )
        if entry is None:
            continue
        if not isinstance(entry, dict):
            issues.non_object_target(
                entry_pointer,
                examples[name].get("$ref")
                if isinstance(examples[name], dict)
                else None,
            )
            continue
        if isinstance(entry.get("externalValue"), str):
            issues.external_value(physical + "/externalValue", entry["externalValue"])
            continue
        if "value" in entry:
            _validate_one_example(
                doc, resolver, issues, side, schema, schema_pointer,
                entry["value"], physical + "/value", errors,
            )


def _walk_content(
    doc: Dict[str, Any],
    resolver: RefResolver,
    issues: _RefIssues,
    side: str,
    content: Dict[str, Any],
    content_base_pointer: str,
    errors: List[ExampleError],
) -> None:
    for media in sorted(content, key=str):
        if not _is_json_media_type(media):
            continue
        media_node = content[media]
        if not isinstance(media_node, dict):
            continue
        media_pointer = content_base_pointer + ptr("content", media)
        _walk_examples_of_media_type(
            doc, resolver, issues, side, media_node, media_pointer, errors
        )


def _walk_operation(
    doc: Dict[str, Any],
    resolver: RefResolver,
    issues: _RefIssues,
    operation: Dict[str, Any],
    operation_pointer: str,
    errors: List[ExampleError],
) -> None:
    body = operation.get("requestBody")
    if body is not None:
        body_node, body_pointer = issues.follow(
            resolver, body, operation_pointer + "/requestBody"
        )
        if isinstance(body_node, dict) and isinstance(
            body_node.get("content"), dict
        ):
            _walk_content(
                doc, resolver, issues, "request",
                body_node["content"], body_pointer, errors,
            )

    responses = operation.get("responses")
    if isinstance(responses, dict):
        for code in sorted(responses, key=str):
            response_node, response_pointer = issues.follow(
                resolver, responses[code], operation_pointer + ptr("responses", code)
            )
            if isinstance(response_node, dict) and isinstance(
                response_node.get("content"), dict
            ):
                _walk_content(
                    doc, resolver, issues, "response",
                    response_node["content"], response_pointer, errors,
                )


def validate_document_examples(doc: Any) -> List[Dict[str, Any]]:
    """Validate every request/response JSON example in *doc*.

    :raises InvalidExampleDocumentError: *doc* is not an OpenAPI 3.x object.
    :raises ExampleReferenceError: an example or schema uses
        ``externalValue``, a non-local reference, a dangling local
        reference, or an example reference that does not target an object.
    """
    messages = validate_openapi_shape(doc)
    if messages:
        raise InvalidExampleDocumentError(messages)

    resolver = RefResolver(doc, label="document")
    issues = _RefIssues()
    errors: List[ExampleError] = []

    paths = doc.get("paths")
    for url_path in sorted(paths, key=str):
        raw_item = paths[url_path]
        item, item_pointer = issues.follow(
            resolver, raw_item, ptr("paths", url_path)
        )
        if not isinstance(item, dict):
            continue
        methods = sorted(
            (key for key in item if key.lower() in HTTP_METHODS), key=str.lower
        )
        for method in methods:
            operation = item[method]
            if not isinstance(operation, dict):
                continue
            _walk_operation(
                doc, resolver, issues, operation,
                item_pointer + ptr(method.lower()), errors,
            )

    issues.raise_if_any()

    # Stable order: example location, then instance location within the
    # example, then the violated schema location/keyword/reason.
    unique = list(dict.fromkeys(errors))
    unique.sort(
        key=lambda e: (
            e.example_pointer,
            e.instance_pointer,
            e.schema_pointer,
            e.keyword,
            e.reason,
        )
    )
    return [error.as_dict() for error in unique]
