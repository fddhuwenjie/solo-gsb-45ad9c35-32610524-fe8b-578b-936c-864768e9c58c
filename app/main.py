"""FastAPI application exposing the OpenAPI contract compatibility audit."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .audit import compare_documents, validate_references
from .callbacks import (
    CallbackResolutionError,
    InvalidCallbackRequestError,
    InvalidDocumentError as InvalidCallbackDocumentError,
)
from .callbacks import (
    InvalidSelectorError as InvalidCallbackSelectorError,
)
from .callbacks import resolve_callbacks
from .composer import (
    ComposeReferenceError,
    InvalidComposeRequestError,
    MAX_SERVICES,
    MIN_SERVICES,
    OperationConflictError,
    compose_gateway,
)
from .examples import (
    ExampleReferenceError,
    InvalidExampleDocumentError,
    validate_document_examples,
)
from .materializer import (
    InvalidDocumentError as InvalidMaterializeDocumentError,
)
from .materializer import (
    InvalidRequestError as InvalidMaterializeRequestError,
)
from .materializer import (
    InvalidSelectorError as InvalidMaterializeSelectorError,
)
from .materializer import (
    MaterializationError,
    materialize_request,
)
from .overlay import (
    MAX_ACTIONS,
    MIN_ACTIONS,
    InvalidActionError,
    InvalidOverlayDocumentError,
    InvalidOverlayRequestError,
    NoMatchError,
    apply_overlay,
)
from .slicer import (
    InvalidDocumentError,
    InvalidSelectorError,
    SliceReferenceError,
    slice_document,
)

app = FastAPI(
    title="OpenAPI Contract Compatibility Audit API",
    version="1.0.0",
    description=(
        "Submit a baseline and a candidate OpenAPI 3.x document. The service "
        "resolves local $refs (cycle-safe), checks operation deletions, new "
        "required request parameters, and type/enum/required changes, then "
        "returns a report sorted stably by JSON Pointer. Request constraints "
        "must not be tightened; response constraints must not be loosened. "
        "POST /slice instead extracts the minimal sub-contract for a set of "
        "'METHOD /path' selectors, including inherited security and the "
        "reachable local $ref closure, and returns it with a SHA-256 digest. "
        "POST /compose merges 2..20 OpenAPI services (each with a service_id "
        "and path_prefix) into one gateway contract: paths are mounted under "
        "their normalized prefixes, root security is materialized onto "
        "operations, component names / security keys / operationIds / tags "
        "are namespaced with the service_id prefix, local components $refs "
        "are rewritten, and a provenance map traces every output path, "
        "operation and component back to its source. "
        "POST /examples/validate checks the 'example' and 'examples.*.value' "
        "members of requestBody and response JSON media types against their "
        "media-type schema (local $refs resolved, readOnly/writeOnly and "
        "required applied direction-dependently, 3.0 'nullable' vs 3.1 type "
        "unions honored); externalValue or external/dangling references "
        "return 422, schema violations return 200 with valid=false. "
        "POST /overlay/apply applies 1..50 ordered overlay actions to one "
        "OpenAPI document: each action targets nodes with a restricted "
        "JSONPath ($, dot/bracket members, array indices, wildcards) and "
        "either recursively merges an 'update' value or 'remove's the "
        "matches (never the root). Actions apply sequentially, matches are "
        "frozen per action, array removals run in descending index order, "
        "and any zero-match/invalid action rejects the whole batch with "
        "422. The transformed contract, per-action matched JSON Pointers "
        "and a SHA-256 digest are returned. "
        "POST /requests/materialize resolves one operation (local $refs "
        "cycle-safe, path-level parameters merged with operation-level "
        "ones, the latter overriding the former by (in, name)) and builds "
        "the request target, headers and Cookie from grouped parameter "
        "values using the default/explicit styles (path simple/label/"
        "matrix, query form/spaceDelimited/pipeDelimited/deepObject, "
        "header simple, cookie form), including arrays, objects, "
        "allowReserved, repeated query keys and percent-encoding; missing "
        "required values, unknown or duplicate parameters and dangling "
        "references return 422 with source JSON Pointers. "
        "POST /callbacks/resolve resolves the runtime-expression callback "
        "URLs declared by one operation against a concrete request/response "
        "context: literal callback keys and embedded '{...}' expressions "
        "($url, $method, $statusCode, request path/query/header/body and "
        "response header/body; body fragments are JSON Pointers, header "
        "names case-insensitive, scalar interpolation only) are evaluated "
        "with the corresponding Path Item Object. Callback Object and Path "
        "Item local $refs (incl. OpenAPI 3.1 siblings) resolve cycle-safe; "
        "missing values, illegal expressions, non-scalar interpolation and "
        "dangling, external or circular references return 422 with the "
        "source JSON Pointers of the callback and the expression. Results "
        "are stably sorted by source and the input is never modified."
    ),
)


class AuditRequest(BaseModel):
    baseline: Dict[str, Any] = Field(
        ..., description="The baseline OpenAPI 3.x JSON document."
    )
    candidate: Dict[str, Any] = Field(
        ..., description="The candidate OpenAPI 3.x JSON document."
    )


class SliceRequest(BaseModel):
    document: Dict[str, Any] = Field(
        ..., description="The OpenAPI 3.x JSON document to slice."
    )
    selectors: List[str] = Field(
        ...,
        description=(
            "Unique operation selectors of the form 'METHOD /path', e.g. "
            "'GET /pets/{id}'. Selectors are case-insensitive for the "
            "method and must match declared paths exactly."
        ),
    )


class SliceResponse(BaseModel):
    contract: Dict[str, Any] = Field(
        ...,
        description=(
            "The minimal sub-contract: openapi/info, the selected "
            "operations with their path-level fields, inherited security, "
            "used security schemes, and the reachable local $ref closure, "
            "all rebuilt at their original JSON Pointer locations."
        ),
    )
    sha256: str = Field(
        ...,
        description=(
            "SHA-256 hex digest of the canonical (sorted-key, compact) JSON "
            "encoding of 'contract'. Independent of input key or selector "
            "order."
        ),
    )


class ComposeServiceModel(BaseModel):
    service_id: str = Field(
        ...,
        description=(
            "Unique identifier of the source service; used as the "
            "'<service_id>_' namespace prefix for component names, security "
            "requirement keys, operationIds and tags."
        ),
    )
    path_prefix: str = Field(
        ...,
        description=(
            "Mount prefix for the service's paths. Normalized before "
            "merging: a leading '/' is enforced, trailing slashes are "
            "stripped, and '' or '/' mounts the service at the root."
        ),
    )
    document: Dict[str, Any] = Field(
        ..., description="The service's OpenAPI 3.x JSON document."
    )


class ComposeRequest(BaseModel):
    info: Dict[str, Any] = Field(
        ..., description="The info object of the composed gateway contract."
    )
    services: List[ComposeServiceModel] = Field(
        ...,
        description=(
            f"{MIN_SERVICES}..{MAX_SERVICES} source services to compose. "
            "service_id values must be unique."
        ),
    )


class ExamplesValidateRequest(BaseModel):
    document: Dict[str, Any] = Field(
        ..., description="The OpenAPI 3.x JSON document whose examples are validated."
    )


class OverlayRequest(BaseModel):
    document: Dict[str, Any] = Field(
        ..., description="The OpenAPI 3.x JSON document to transform."
    )
    actions: List[Any] = Field(
        ...,
        description=(
            f"{MIN_ACTIONS}..{MAX_ACTIONS} ordered overlay actions. Each action "
            "is an object with a restricted-JSONPath 'target' plus exactly one "
            "of 'update' (value merged recursively into every match) or "
            "'remove': true (matches deleted; the root '$' cannot be removed)."
        ),
    )


class OverlayActionReport(BaseModel):
    action_index: int = Field(
        ..., description="Zero-based index of the action in the request."
    )
    op: Literal["update", "remove"] = Field(
        ..., description="The operation the action performed."
    )
    target: str = Field(..., description="The action's restricted JSONPath target.")
    matches: List[str] = Field(
        ...,
        description=(
            "JSON Pointers of the nodes the target actually matched, frozen "
            "before the action was applied and sorted in stable pointer order."
        ),
    )


class OverlayResponse(BaseModel):
    contract: Any = Field(
        ...,
        description=(
            "The transformed document, returned with stable (sorted) key "
            "order; identical to the bytes the SHA-256 digest is computed over."
        ),
    )
    actions: List[OverlayActionReport] = Field(
        ..., description="Per-action match report, in request action order."
    )
    sha256: str = Field(
        ...,
        description=(
            "SHA-256 hex digest of the canonical (sorted-key, compact) JSON "
            "encoding of 'contract'."
        ),
    )


class MaterializeRequest(BaseModel):
    document: Dict[str, Any] = Field(
        ..., description="The OpenAPI 3.x JSON document describing the operation."
    )
    selector: str = Field(
        ...,
        description=(
            "The operation to materialize, in the 'METHOD /path' form "
            "(e.g. 'GET /pets/{petId}'); method is case-insensitive."
        ),
    )
    values: Optional[Any] = Field(
        default=None,
        description=(
            "Parameter values grouped by location: 'path', 'query', "
            "'header' and 'cookie', each an object mapping the declared "
            "parameter name to its JSON value. Header name matching is "
            "case-insensitive; null (or omission) skips an optional "
            "parameter and is rejected for required parameters."
        ),
    )


class MaterializeResponse(BaseModel):
    request_target: str = Field(
        ...,
        description=(
            "The origin-form request target: the path template substituted "
            "with the serialized path parameters, followed by '?' and the "
            "serialized query string when query parameters are present."
        ),
    )
    headers: Dict[str, str] = Field(
        ...,
        description=(
            "Serialized HTTP headers contributed by 'in: header' parameters, "
            "keyed by the declared header names. The Cookie header is "
            "reported separately in 'cookie'."
        ),
    )
    cookie: str = Field(
        ...,
        description=(
            "The serialized Cookie header value ('name=value' pairs joined "
            "by '; '), or an empty string when no cookie parameters are "
            "supplied."
        ),
    )


class CallbackResolveRequest(BaseModel):
    document: Dict[str, Any] = Field(
        ..., description="The OpenAPI 3.x JSON document declaring the callbacks."
    )
    selector: str = Field(
        ...,
        description=(
            "The operation whose callbacks are resolved, in the "
            "'METHOD /path' form (e.g. 'POST /subscriptions'); method is "
            "case-insensitive."
        ),
    )
    context: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The concrete request/response context the runtime expressions "
            "are evaluated against. May contain 'request' (url, method, "
            "path, query, header, body) and 'response' (statusCode, header, "
            "body); every member is optional. An empty/omitted context only "
            "supports literal callback URLs."
        ),
    )


class ResolvedCallbackModel(BaseModel):
    name: str = Field(
        ..., description="The operation callbacks map entry (callback name)."
    )
    callback_pointer: str = Field(
        ...,
        description=(
            "JSON Pointer of the callbacks map entry, i.e. the source of the "
            "callback; for a $ref'd Callback Object it points at the $ref "
            "occurrence."
        ),
    )
    expression: str = Field(
        ...,
        description="The original callback key (literal URL or runtime-expression template)."
    )
    expression_pointer: str = Field(
        ...,
        description=(
            "JSON Pointer of the expression key within its effective Callback "
            "Object; for a $ref'd callback this follows the chain to the key "
            "declaration, honoring OpenAPI 3.1 sibling overrides."
        ),
    )
    url: str = Field(
        ..., description="The concrete callback URL after expression evaluation."
    )
    path_item: Dict[str, Any] = Field(
        ...,
        description=(
            "The Path Item Object declared for that URL, with its own local "
            "$ref chain resolved (OpenAPI 3.1 siblings merged) and deep-copied."
        ),
    )

class ExampleErrorModel(BaseModel):
    side: Literal["request", "response"] = Field(
        ..., description="Whether the example belongs to a requestBody or a response."
    )
    example_pointer: str = Field(
        ...,
        description="JSON Pointer of the example value in the document; "
        "a $ref'd Example Object is followed to its components location.",
    )
    instance_pointer: str = Field(
        ..., description="JSON Pointer within the example value where the violation occurred."
    )
    schema_pointer: str = Field(
        ...,
        description="JSON Pointer of the violated schema (local $refs followed "
        "to the component definition).",
    )
    keyword: str = Field(
        ..., description="The JSON Schema keyword that was violated."
    )
    reason: str = Field(..., description="Human-readable explanation")


class ExamplesValidateResponse(BaseModel):
    valid: bool = Field(
        ..., description="True when every checked example satisfies its schema."
    )
    error_count: int
    errors: List[ExampleErrorModel] = Field(
        ...,
        description="Violations sorted stably by example pointer, then "
        "instance pointer, schema pointer, keyword and reason.",
    )


class ComposeResponse(BaseModel):
    contract: Dict[str, Any] = Field(
        ...,
        description=(
            "The composed gateway contract: the request's info, the highest "
            "source openapi version, merged tags, all paths mounted under "
            "their normalized prefixes, and all components namespaced with "
            "the '<service_id>_' prefix. Operations carry materialized "
            "security and every '#/components/...' $ref is rewritten."
        ),
    )
    provenance: Dict[str, Any] = Field(
        ...,
        description=(
            "Source mapping covering every output path, operation and "
            "component: 'paths' maps each composed path to its contributing "
            "services, 'operations' maps each composed path + method to its "
            "source operation, and 'components' maps each section + "
            "prefixed name to its source component. Every entry carries "
            "service_id and a JSON Pointer into the source document."
        ),
    )


class FindingModel(BaseModel):
    pointer: str = Field(..., description="JSON Pointer into the document")
    category: str = Field(..., description="Machine-readable change category")
    severity: Literal["breaking", "info"] = Field(
        ...,
        description="'breaking' violates the compatibility policy; "
        "'info' is a compatible change worth noting",
    )
    reason: str = Field(..., description="Human-readable explanation")


class ReferenceErrorModel(BaseModel):
    document: Literal["baseline", "candidate"]
    ref: str
    error: str


class AuditResponse(BaseModel):
    compatible: bool = Field(
        ...,
        description="True when there are no breaking findings and no "
        "unresolvable references.",
    )
    breaking_count: int
    info_count: int
    findings: List[FindingModel]


def _validate_openapi_shape(doc: Dict[str, Any], label: str) -> Optional[str]:
    """Lightweight structural validation of an OpenAPI 3.x document."""
    if not isinstance(doc, dict):
        return f"{label} must be a JSON object"
    version = doc.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        return (
            f"{label}.openapi must be a version string starting with '3.' "
            f"(OpenAPI 3.x), got {version!r}"
        )
    if "paths" not in doc or not isinstance(doc.get("paths"), dict):
        return f"{label}.paths must be an object"
    return None


@app.get("/", include_in_schema=False)
def root() -> Dict[str, str]:
    return {
        "service": "openapi-contract-audit",
        "docs": "/docs",
        "health": "/health",
        "audit": "POST /audit",
        "slice": "POST /slice",
        "compose": "POST /compose",
        "examples_validate": "POST /examples/validate",
        "overlay_apply": "POST /overlay/apply",
        "requests_materialize": "POST /requests/materialize",
        "callbacks_resolve": "POST /callbacks/resolve",
    }


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/audit",
    response_model=AuditResponse,
    responses={
        400: {"description": "The submitted document is not OpenAPI 3.x"},
        422: {"description": "A $ref cannot be resolved"},
    },
    summary="Audit candidate compatibility against baseline",
)
def audit_contract(payload: AuditRequest) -> JSONResponse:
    shape_errors = [
        message
        for message in (
            _validate_openapi_shape(payload.baseline, "baseline"),
            _validate_openapi_shape(payload.candidate, "candidate"),
        )
        if message
    ]
    if shape_errors:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_openapi_document",
                "messages": shape_errors,
            },
        )

    reference_errors = [
        *validate_references(payload.baseline, "baseline"),
        *validate_references(payload.candidate, "candidate"),
    ]
    if reference_errors:
        return JSONResponse(
            status_code=422,
            content={
                "error": "unresolvable_reference",
                "message": (
                    "one or more $ref entries could not be resolved; fix the "
                    "references before the contract can be audited"
                ),
                "references": reference_errors,
            },
        )

    findings = compare_documents(payload.baseline, payload.candidate)
    breaking = [f for f in findings if f.severity == "breaking"]
    info = [f for f in findings if f.severity == "info"]

    body = AuditResponse(
        compatible=not breaking,
        breaking_count=len(breaking),
        info_count=len(info),
        findings=[FindingModel(**f.as_dict()) for f in findings],
    )
    return JSONResponse(status_code=200, content=body.model_dump())


@app.post(
    "/slice",
    response_model=SliceResponse,
    responses={
        400: {"description": "Bad document shape, duplicate or unknown selector"},
        422: {"description": "A retained node references an unresolvable $ref"},
    },
    summary="Extract a minimal sub-contract for selected operations",
)
def slice_contract(payload: SliceRequest) -> JSONResponse:
    try:
        contract, digest = slice_document(
            payload.document, payload.selectors
        )
    except InvalidDocumentError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_openapi_document",
                "messages": exc.messages,
            },
        )
    except InvalidSelectorError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_selector", "messages": exc.messages},
        )
    except SliceReferenceError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "unresolvable_reference",
                "message": (
                    "a $ref reachable from the selected operations could not "
                    "be resolved; fix the reference or remove the operation "
                    "that uses it"
                ),
                "references": exc.references,
            },
        )
    return JSONResponse(
        status_code=200, content={"contract": contract, "sha256": digest}
    )


@app.post(
    "/compose",
    response_model=ComposeResponse,
    responses={
        400: {
            "description": "Malformed request: bad service count, missing/"
            "duplicate/invalid service_id, invalid path_prefix, or a "
            "document that is not OpenAPI 3.x"
        },
        409: {
            "description": "Two services declare the same composed path "
            "and HTTP method"
        },
        422: {
            "description": "A retained $ref is external, dangling, or "
            "points outside components"
        },
    },
    summary="Compose a gateway contract from multiple OpenAPI services",
)
def compose_contract(payload: ComposeRequest) -> JSONResponse:
    try:
        contract, provenance = compose_gateway(
            payload.info,
            [service.model_dump() for service in payload.services],
        )
    except InvalidComposeRequestError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_compose_request",
                "messages": exc.messages,
            },
        )
    except ComposeReferenceError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "unsupported_reference",
                "message": (
                    "one or more retained $ref entries are external, "
                    "unresolvable, or point outside components; only "
                    "'#/components/...' references can be rewritten into "
                    "the composed contract"
                ),
                "references": exc.references,
            },
        )
    except OperationConflictError as exc:
        return JSONResponse(
            status_code=409,
            content={
                "error": "operation_conflict",
                "message": (
                    "multiple services declare the same composed path and "
                    "HTTP method; adjust the path prefixes or drop one of "
                    "the conflicting operations"
                ),
                "conflicts": exc.conflicts,
            },
        )
    return JSONResponse(
        status_code=200,
        content={"contract": contract, "provenance": provenance},
    )


@app.post(
    "/examples/validate",
    response_model=ExamplesValidateResponse,
    responses={
        400: {"description": "The submitted document is not OpenAPI 3.x"},
        422: {
            "description": "An example uses externalValue, or a reference is "
            "external, dangling, or does not target an Example Object"
        },
    },
    summary="Validate request/response JSON examples against their schemas",
)
def validate_examples(payload: ExamplesValidateRequest) -> JSONResponse:
    try:
        errors = validate_document_examples(payload.document)
    except InvalidExampleDocumentError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_openapi_document",
                "messages": exc.messages,
            },
        )
    except ExampleReferenceError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": exc.code,
                "message": exc.message,
                "references": exc.references,
            },
        )
    body = ExamplesValidateResponse(
        valid=not errors,
        error_count=len(errors),
        errors=[ExampleErrorModel(**error) for error in errors],
    )
    return JSONResponse(status_code=200, content=body.model_dump())


@app.post(
    "/overlay/apply",
    response_model=OverlayResponse,
    responses={
        400: {
            "description": "The document is not OpenAPI 3.x, or the action "
            "count is outside 1..50"
        },
        422: {
            "description": "An action is malformed, its target has a syntax "
            "error, or a target matched zero nodes; the whole batch is "
            "rejected and nothing is applied"
        },
    },
    summary="Apply ordered overlay actions to an OpenAPI document",
)
def overlay_apply(payload: OverlayRequest) -> JSONResponse:
    try:
        contract, reports, digest = apply_overlay(
            payload.document, payload.actions
        )
    except InvalidOverlayDocumentError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_openapi_document",
                "messages": exc.messages,
            },
        )
    except InvalidOverlayRequestError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_overlay_request",
                "messages": exc.messages,
            },
        )
    except InvalidActionError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_action",
                "message": (
                    "one or more overlay actions are malformed or carry an "
                    "invalid target; the whole batch was rejected and the "
                    "document was left untouched"
                ),
                "actions": exc.actions,
            },
        )
    except NoMatchError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "no_match",
                "message": (
                    "an action target matched zero nodes; the whole batch "
                    "was rejected and the document was left untouched"
                ),
                "actions": exc.actions,
            },
        )
    return JSONResponse(
        status_code=200,
        content={"contract": contract, "actions": reports, "sha256": digest},
    )


@app.post(
    "/requests/materialize",
    response_model=MaterializeResponse,
    responses={
        400: {
            "description": "The document is not OpenAPI 3.x, the selector "
            "is malformed/unknown, or the values envelope is malformed"
        },
        422: {
            "description": "A required value is missing/unknown/duplicate, "
            "a value does not fit its declared style, a path template is "
            "inconsistent, or a local $ref is dangling or circular; each "
            "error carries its source JSON Pointer"
        },
    },
    summary="Materialize request target, headers and Cookie for one operation",
)
def requests_materialize(payload: MaterializeRequest) -> JSONResponse:
    try:
        request_target, headers, cookie = materialize_request(
            payload.document, payload.selector, payload.values
        )
    except InvalidMaterializeDocumentError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_openapi_document",
                "messages": exc.messages,
            },
        )
    except InvalidMaterializeSelectorError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_selector", "messages": exc.messages},
        )
    except InvalidMaterializeRequestError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_materialize_request",
                "messages": exc.messages,
            },
        )
    except MaterializationError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "materialization_failed",
                "message": (
                    "the request could not be materialized; fix the reported "
                    "parameters, values or references first"
                ),
                "errors": exc.errors,
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "request_target": request_target,
            "headers": headers,
            "cookie": cookie,
        },
    )


@app.post(
    "/callbacks/resolve",
    response_model=List[ResolvedCallbackModel],
    responses={
        400: {
            "description": "The document is not OpenAPI 3.x, the selector "
            "is malformed/unknown, or the request/response context is malformed"
        },
        422: {
            "description": "A runtime expression is missing a value or "
            "syntactically invalid, an interpolation is non-scalar, a header "
            "name is ambiguous, or a Callback Object / Path Item $ref is "
            "dangling, external or circular; each error carries the source "
            "JSON Pointers of the callback and the expression"
        },
    },
    summary="Resolve the callback URLs declared by one operation",
)
def callbacks_resolve(payload: CallbackResolveRequest) -> JSONResponse:
    try:
        resolved = resolve_callbacks(
            payload.document, payload.selector, payload.context
        )
    except InvalidCallbackDocumentError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_openapi_document",
                "messages": exc.messages,
            },
        )
    except InvalidCallbackSelectorError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_selector", "messages": exc.messages},
        )
    except InvalidCallbackRequestError as exc:
        return JSONResponse(
            status_code=400,
            content={
                "error": "invalid_callback_request",
                "messages": exc.messages,
            },
        )
    except CallbackResolutionError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "callback_resolution_failed",
                "message": (
                    "one or more callback URLs could not be resolved; fix the "
                    "reported expressions, context values or references first"
                ),
                "errors": exc.errors,
            },
        )
    return JSONResponse(
        status_code=200,
        content=[
            {
                "name": item["name"],
                "callback_pointer": item["callback_pointer"],
                "expression": item["expression"],
                "expression_pointer": item["expression_pointer"],
                "url": item["url"],
                "path_item": item["path_item"],
            }
            for item in resolved
        ],
    )
