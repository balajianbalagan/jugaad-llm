import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

load_dotenv()

BASE_DIR = Path(__file__).parent
app = FastAPI(title="Local LLM Tester")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    model: Optional[str] = Field(default=None, max_length=200)
    system_prompt: Optional[str] = Field(default=None, max_length=10_000)
    temperature: float = Field(default=0.7, ge=0, le=2)


def message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/config")
async def config() -> dict[str, str]:
    return {
        "base_url": os.getenv("OPENAI_BASE_URL", "http://localhost:4000/v1"),
        "model": os.getenv("LOCAL_MODEL", ""),
    }


@app.post("/api/chat")
async def chat(request: ChatRequest) -> dict[str, str]:
    base_url = os.getenv("OPENAI_BASE_URL", "http://localhost:4000/v1")
    api_key = os.getenv("OPENAI_API_KEY")
    model = request.model or os.getenv("LOCAL_MODEL")

    if not api_key:
        raise HTTPException(500, "OPENAI_API_KEY is not configured on the server.")
    if not model:
        raise HTTPException(400, "Enter a model name or set LOCAL_MODEL in .env.")

    messages = []
    if request.system_prompt and request.system_prompt.strip():
        messages.append(SystemMessage(content=request.system_prompt.strip()))
    messages.append(HumanMessage(content=request.prompt.strip()))

    try:
        llm = ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=request.temperature,
        )
        response = await llm.ainvoke(messages)
        return {"reply": message_text(response.content)}
    except Exception as exc:
        raise HTTPException(502, f"Local LLM request failed: {exc}") from exc
