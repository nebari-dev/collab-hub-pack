from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Query, Request, status

from ..dependencies import get_user_directory_client
from ..frames.auth import AuthContext, get_auth_context
from ..routers.frames import error_response
from ..user_directory import (
    UserDirectoryClient,
    UserDirectoryGroup,
    UserDirectoryUnavailableError,
    UserDirectoryUser,
)

router = APIRouter(prefix="/user-directory", tags=["user-directory"])

AuthDep = Annotated[AuthContext, Depends(get_auth_context)]
UserDirectoryDep = Annotated[UserDirectoryClient, Depends(get_user_directory_client)]


@router.get("/users", response_model=list[UserDirectoryUser])
def search_users(
    _auth: AuthDep,
    user_directory: UserDirectoryDep,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[UserDirectoryUser]:
    return user_directory.search_users(q, limit=limit)


@router.get("/groups", response_model=list[UserDirectoryGroup])
def search_groups(
    _auth: AuthDep,
    user_directory: UserDirectoryDep,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[UserDirectoryGroup]:
    return user_directory.search_groups(q, limit=limit)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(UserDirectoryUnavailableError)
    async def user_directory_unavailable_handler(_request: Request, exc: UserDirectoryUnavailableError):
        return error_response(status.HTTP_503_SERVICE_UNAVAILABLE, "user_directory_unavailable", str(exc))
