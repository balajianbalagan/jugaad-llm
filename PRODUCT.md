# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

FastAPI with static HTML, CSS, and JavaScript.

## Users

Developers testing an OpenAI-compatible local LLM endpoint.

## Product Purpose

Provide a small local chat surface that sends prompts to a locally hosted model through LangChain.

## Operating Context

The app is run locally. Connection details and the API key stay in environment variables on the server.

## Capabilities and Constraints

- Send a prompt and optional system instruction to a selectable model.
- Use the OpenAI-compatible chat-completions interface through LangChain.
- Do not expose the API key to browser JavaScript.

## Evidence on Hand

The configured endpoint is `http://localhost:4000/v1`; a local API key was supplied by the user.

## Product Principles

- Make a first local-model test quick.
- Keep connection secrets server-side.
- Show errors clearly enough to diagnose local endpoint issues.
