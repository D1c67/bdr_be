"""Rate-limit wiring on the delta endpoints that send outbound email or invoke a
paid LLM. These are one-line route dependencies that are easy to drop silently;
the codebase's convention is that every outbound-email route carries
outbound_email_rate_limit and every LLM route carries ai_rate_limit. Assert the
dependencies are present so a regression is caught before it reaches prod."""

from app.core.ratelimit import ai_rate_limit, outbound_email_rate_limit
from app.routers.pm_field import router as pm_field_router
from app.routers.submittals import router as submittals_router


def _route(router, path, method):
    for r in router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r
    raise AssertionError(f"route {method} {path} not found on {router.prefix}")


def _has_dependency(route, dep) -> bool:
    # Route-level dependencies (dependencies=[Depends(dep)]) are flattened into
    # the route's resolved dependant; each carries the dependency callable as .call.
    return any(d.call is dep for d in route.dependant.dependencies)


def test_rfi_send_has_outbound_email_rate_limit():
    route = _route(
        pm_field_router, "/pm/projects/{project_id}/rfis/{rfi_id}/send", "POST"
    )
    assert _has_dependency(route, outbound_email_rate_limit)


def test_submittal_llm_routes_have_ai_rate_limit():
    # Every route that can call openai_text.alt_material_names must be throttled.
    for path, method in (
        ("/submittals", "POST"),                                  # create (aliases)
        ("/submittals/group", "POST"),                            # group create
        ("/submittals/{material_id}", "PATCH"),                   # rename regenerates
        ("/submittals/{material_id}/aliases/regenerate", "POST"), # pure LLM call
    ):
        route = _route(submittals_router, path, method)
        assert _has_dependency(route, ai_rate_limit), f"{method} {path} missing ai_rate_limit"
