"""
/sandbox/* -- standalone htmx experiments, kept alongside the app rather
than thrown away after use. No auth, no Temporal/Postgres involvement:
these exist purely to poke at htmx behavior in isolation (verifying a
mechanism works, understanding a timing quirk, etc.) using the exact
same htmx/Tailwind CDN pins as the real app (via base.html), before
applying whatever was learned to the real templates.

Each experiment gets its own subpath (`/sandbox/<name>/`) and, if it
needs one, its own tiny backing endpoint under that same subpath. Add
new experiments as additional routes below plus a template under
`templates/sandbox/`, and link them from the index page.
"""

import asyncio
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(prefix="/sandbox", tags=["Sandbox"])
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)


def _render(request: Request, template: str, ctx: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, template, ctx)


@router.get("/", response_class=HTMLResponse)
async def sandbox_index(request: Request):
    return _render(request, "sandbox/index.html", {})


# ------------------------------------------------------------- hx-indicator ---
# Verifies htmx's hx-indicator mechanism end to end: the default injected
# .htmx-indicator/.htmx-request CSS, the doc's own "adjacent spinner" and
# "embedded indicator" examples, the same pattern combined with Tailwind
# and a form-wrapped submit (matching how the real app's dialogs work),
# and the hx-swap="... swap:Nms" delay modifier that gives a fast local
# response enough on-screen time for its indicator to actually be seen.
# See the `htmx4` skill's "Loading indicators and swap timing" section for
# what this proved and the OOB-swap-timing gotcha it depends on.

@router.get("/hx-indicator/", response_class=HTMLResponse)
async def hx_indicator_page(request: Request):
    return _render(request, "sandbox/hx_indicator.html", {})


@router.post("/hx-indicator/slow", response_class=HTMLResponse)
async def hx_indicator_slow():
    # 30s is comfortably under htmx's default 60000ms request timeout, so
    # this exercises a long-running spinner without hitting that limit --
    # the indicator should spin the whole 30s with nothing to interrupt it.
    await asyncio.sleep(30)
    return HTMLResponse("<strong>Done after 30s sleep.</strong>")


@router.post("/hx-indicator/fast", response_class=HTMLResponse)
async def hx_indicator_fast():
    return HTMLResponse("<strong>Done instantly (0ms sleep).</strong>")
