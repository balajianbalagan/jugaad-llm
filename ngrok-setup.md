# Hackathon Public Tunnel Setup (Ngrok)

You can expose your laptop's local Browser LLM Gateway to the internet as a temporary, public OpenAI-compatible API so your hackathon teammates, frontend apps, Discord bots, and remote demos can use it.

## Method 1: Automatic Built-in Tunnel (Recommended)

1. Get a free ngrok authtoken at [ngrok.com](https://dashboard.ngrok.com/get-started/your-authtoken) (takes 30 seconds).
2. Add it to your `.env` file:
   ```bash
   NGROK_AUTHTOKEN=your_token_here
   ```
3. Start the gateway with the `--tunnel` flag:
   ```bash
   python start.py --tunnel
   ```

The gateway will automatically launch the ngrok tunnel and print the exact HTTPS URL and hackathon `.env` configuration to copy into your app:

```env
OPENAI_BASE_URL="https://xxxx-xx-xx.ngrok-free.app/v1"
OPENAI_API_KEY="sk-hackathon-token"
```

## Method 2: Manual Ngrok CLI

If you already have the ngrok CLI installed:

```bash
# 1. Start gateway locally
python start.py

# 2. In a separate terminal, expose port 4000
ngrok http 4000
```

Copy the shown HTTPS forwarding URL (e.g. `https://xxxx.ngrok-free.app/v1`) into your client application.

## Testing Your Public Tunnel

```bash
curl https://xxxx.ngrok-free.app/v1/chat/completions \
  -H "Authorization: Bearer sk-hackathon-token" \
  -H "Content-Type: application/json" \
  -d '{"model":"chatgpt","messages":[{"role":"user","content":"Hello world!"}]}'
```
