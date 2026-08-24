from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from openbench.api.dependencies import ContextDep
from openbench.api.schemas import (
    MatrixApplyResultOut,
    MatrixPortOut,
    MatrixProfileCreate,
    MatrixProfileOut,
    MatrixProfileUpdate,
    MatrixValidationOut,
)
from openbench.services.matrix_service import (
    MatrixConflictError,
    MatrixNotFoundError,
    MatrixValidationError,
    SafetyInterlockError,
)

router = APIRouter(prefix="/api/v1/matrix", tags=["matrix"])


def _raise_http(exc: Exception) -> None:
    if isinstance(exc, MatrixNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, MatrixConflictError | SafetyInterlockError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, MatrixValidationError):
        raise HTTPException(
            status_code=422,
            detail={"message": "Matrix profile validation failed", "errors": exc.errors},
        ) from exc
    raise exc


@router.get("/ports", response_model=list[MatrixPortOut])
def list_ports(context: ContextDep) -> list[MatrixPortOut]:
    return [MatrixPortOut.from_domain(item) for item in context.matrix_service.list_ports()]


@router.get("/profiles", response_model=list[MatrixProfileOut])
def list_profiles(
    context: ContextDep,
) -> list[MatrixProfileOut]:
    return [MatrixProfileOut.from_domain(item) for item in context.matrix_service.list_profiles()]


@router.post(
    "/profiles",
    response_model=MatrixProfileOut,
    status_code=status.HTTP_201_CREATED,
)
def create_profile(
    payload: MatrixProfileCreate,
    context: ContextDep,
) -> MatrixProfileOut:
    try:
        profile = context.matrix_service.create_profile(
            payload.name,
            tuple(item.to_domain() for item in payload.connections),
        )
    except Exception as exc:
        _raise_http(exc)
        raise
    return MatrixProfileOut.from_domain(profile)


@router.get("/profiles/{profile_id}", response_model=MatrixProfileOut)
def get_profile(
    profile_id: str,
    context: ContextDep,
) -> MatrixProfileOut:
    try:
        return MatrixProfileOut.from_domain(context.matrix_service.get_profile(profile_id))
    except MatrixNotFoundError as exc:
        _raise_http(exc)
        raise


@router.put("/profiles/{profile_id}", response_model=MatrixProfileOut)
def update_profile(
    profile_id: str,
    payload: MatrixProfileUpdate,
    context: ContextDep,
) -> MatrixProfileOut:
    try:
        profile = context.matrix_service.update_profile(
            profile_id,
            payload.name,
            tuple(item.to_domain() for item in payload.connections),
        )
    except Exception as exc:
        _raise_http(exc)
        raise
    return MatrixProfileOut.from_domain(profile)


@router.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(
    profile_id: str,
    context: ContextDep,
) -> None:
    try:
        context.matrix_service.delete_profile(profile_id)
    except Exception as exc:
        _raise_http(exc)


@router.post("/profiles/{profile_id}/validate", response_model=MatrixValidationOut)
def validate_profile(
    profile_id: str,
    context: ContextDep,
) -> MatrixValidationOut:
    try:
        errors = context.matrix_service.validate_profile(profile_id)
    except MatrixNotFoundError as exc:
        _raise_http(exc)
        raise
    return MatrixValidationOut(valid=not errors, errors=list(errors))


@router.post("/profiles/{profile_id}/apply", response_model=MatrixApplyResultOut)
def apply_profile(
    profile_id: str,
    context: ContextDep,
) -> MatrixApplyResultOut:
    try:
        result = context.matrix_service.apply_profile(profile_id)
    except Exception as exc:
        _raise_http(exc)
        raise
    return MatrixApplyResultOut.from_domain(result)


@router.get("/active", response_model=MatrixApplyResultOut)
def active(context: ContextDep) -> MatrixApplyResultOut:
    return MatrixApplyResultOut.from_domain(context.matrix_service.active())


@router.post("/open-all", response_model=MatrixApplyResultOut)
def open_all(context: ContextDep) -> MatrixApplyResultOut:
    return MatrixApplyResultOut.from_domain(context.matrix_service.open_all())
