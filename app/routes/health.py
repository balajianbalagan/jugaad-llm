from fastapi import APIRouter, Request

router = APIRouter(tags=["operations"])


@router.get("/health")
async def health(request: Request):
    models = request.app.state.gateway.models
    providers = sorted({model["owned_by"] for model in models})
    return {"status": "ok", "configured_models": len(models), "providers": {provider: "configured" for provider in providers}}


@router.get("/stats")
async def stats(request: Request):
    return await request.app.state.store.stats()
