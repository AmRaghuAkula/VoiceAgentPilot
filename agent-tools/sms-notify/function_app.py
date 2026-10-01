"""Azure Functions shell (plan P3): one anonymous route; auth is done in code (spec K9).

Only adapts ``azure.functions.HttpRequest`` to the framework-free dispatcher.
"""

from __future__ import annotations

import logging
import os

import azure.functions as func

from sms_notify.http.dispatcher import ROUTE, Dispatcher, Request
from sms_notify.ports import SystemClock
from sms_notify.runtime import RuntimeFactory

# Keep transport and SDK request logging (URLs, headers) out of the logs; our one line is enough.
for _name in ("httpx", "httpcore", "azure", "azure.identity", "azure.core"):
    logging.getLogger(_name).setLevel(logging.WARNING)
logging.getLogger("sms_notify").setLevel(logging.INFO)

_clock = SystemClock()
_dispatcher = Dispatcher(RuntimeFactory(lambda: os.environ, _clock), _clock)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="v1/follow-up-sms", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def follow_up_sms(req: func.HttpRequest) -> func.HttpResponse:
    response = await _dispatcher.handle(
        Request(
            method=req.method,
            path=ROUTE,
            headers={k.lower(): v for k, v in req.headers.items()},
            body=req.get_body() or b"",
        )
    )
    return func.HttpResponse(body=response.body, status_code=response.status, headers=response.headers)
