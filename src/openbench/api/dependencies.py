from __future__ import annotations

from typing import Annotated

from fastapi import Request
from fastapi.params import Depends

from openbench.bootstrap import ApplicationContext


def get_context(request: Request) -> ApplicationContext:
    context: ApplicationContext = request.app.state.context
    return context


ContextDep = Annotated[ApplicationContext, Depends(get_context)]
