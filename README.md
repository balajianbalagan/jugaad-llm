# Local LLM Tester

A tiny FastAPI app that sends prompts to an OpenAI-compatible local LLM using LangChain.

Project landing page: [Jugaad LLM](https://balajianbalagan.github.io/jugaad-llm/) · Personal site: [balajianbalagan.pages.dev](https://balajianbalagan.pages.dev)

## Run it

1. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install dependencies and create your local configuration:

   ```powershell
   pip install -r requirements.txt
   Copy-Item .env.example .env
   ```

3. In `.env`, set `LOCAL_MODEL` to the exact model ID served by your local LLM, then start the app:

   ```powershell
   uvicorn app:app --reload
   ```

Open `http://127.0.0.1:8000`. The key remains on the FastAPI server and is never sent to the browser.

See [examples/README.md](examples/README.md) for a walkthrough, screenshots, provider selection, and the latest verification results.
