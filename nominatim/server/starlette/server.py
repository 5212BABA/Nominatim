# SPDX-License-Identifier: GPL-2.0-only
#
# This file is part of Nominatim. (https://nominatim.org)
#
# Copyright (C) 2023 by the Nominatim developer community.
# For a full list of authors see the git log.
"""
Server implementation using the starlette webserver framework.
"""
from typing import Any, Optional, Mapping, Callable, cast, Coroutine
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.requests import Request
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from nominatim.api import NominatimAPIAsync
import nominatim.api.v1 as api_impl
from nominatim.config import Configuration

class ParamWrapper(api_impl.ASGIAdaptor):
    """ Adaptor class for server glue to Starlette framework.
    """

    def __init__(self, request: Request) -> None:
        self.request = request


    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.request.query_params.get(name, default=default)


    def get_header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.request.headers.get(name, default)


    def error(self, msg: str, status: int = 400) -> HTTPException:
        return HTTPException(status, detail=msg,
                             headers={'content-type': self.content_type})


    def create_response(self, status: int, output: str) -> Response:
        return Response(output, status_code=status, media_type=self.content_type)


    def config(self) -> Configuration:
        return cast(Configuration, self.request.app.state.API.config)


def _wrap_endpoint(func: api_impl.EndpointFunc)\
        -> Callable[[Request], Coroutine[Any, Any, Response]]:
    async def _callback(request: Request) -> Response:
        return cast(Response, await func(request.app.state.API, ParamWrapper(request)))

    return _callback


def get_application(project_dir: Path,
                    environ: Optional[Mapping[str, str]] = None,
                    debug: bool = True) -> Starlette:
    """ Create a Nominatim falcon ASGI application.
    """
    config = Configuration(project_dir, environ)

    routes = []
    legacy_urls = config.get_bool('SERVE_LEGACY_URLS')
    for name, func in api_impl.ROUTES:
        endpoint = _wrap_endpoint(func)
        routes.append(Route(f"/{name}", endpoint=endpoint))
        if legacy_urls:
            routes.append(Route(f"/{name}.php", endpoint=endpoint))

    middleware = []
    if config.get_bool('CORS_NOACCESSCONTROL'):
        middleware.append(Middleware(CORSMiddleware, allow_origins=['*']))

    async def _shutdown() -> None:
        await app.state.API.close()

    app = Starlette(debug=debug, routes=routes, middleware=middleware,
                    on_shutdown=[_shutdown])

    app.state.API = NominatimAPIAsync(project_dir, environ)

    return app


def run_wsgi() -> Starlette:
    """ Entry point for uvicorn.
    """
    return get_application(Path('.'), debug=False)
