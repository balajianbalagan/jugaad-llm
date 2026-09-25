const form = document.querySelector("#chat-form");
const promptInput = document.querySelector("#prompt");
const modelInput = document.querySelector("#model");
const systemInput = document.querySelector("#system-prompt");
const temperature = document.querySelector("#temperature");
const temperatureValue = document.querySelector("#temperature-value");
const reply = document.querySelector("#reply");
const status = document.querySelector("#status");
const send = document.querySelector("#send");
const copy = document.querySelector("#copy");

fetch("/api/config")
  .then((response) => response.json())
  .then(({ base_url, model }) => {
    document.querySelector("#endpoint").textContent = base_url;
    modelInput.value = model;
  })
  .catch(() => { document.querySelector("#endpoint").textContent = "Endpoint unavailable"; });

temperature.addEventListener("input", () => { temperatureValue.value = temperature.value; });

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  send.disabled = true;
  send.textContent = "Sending…";
  copy.disabled = true;
  status.textContent = "Contacting local model…";
  reply.textContent = "";
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: promptInput.value,
        model: modelInput.value || null,
        system_prompt: systemInput.value || null,
        temperature: Number(temperature.value),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Request failed");
    reply.textContent = data.reply || "(The model returned an empty response.)";
    status.textContent = "Response received";
    copy.disabled = false;
  } catch (error) {
    reply.textContent = `Error: ${error.message}`;
    status.textContent = "Request failed";
  } finally {
    send.disabled = false;
    send.textContent = "Send prompt";
  }
});

copy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(reply.textContent);
    copy.textContent = "Copied";
    status.textContent = "Response copied";
  } catch {
    status.textContent = "Copy failed — select the response text manually.";
  } finally {
    setTimeout(() => { copy.textContent = "Copy"; }, 1400);
  }
});
