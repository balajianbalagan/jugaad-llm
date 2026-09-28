from fastapi import APIRouter, Request

router = APIRouter(tags=["models"])


@router.get("/v1/models")
async def list_models(request: Request):
    return {"object": "list", "data": request.app.state.gateway.models}
