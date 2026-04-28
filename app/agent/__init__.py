"""Agentic negotiation and outreach system for Airbnb Automate.

This package provides LangGraph-based AI agents that:
1. **Negotiate** — monitor inbox chats, detect which need replies, and generate
   pro-negotiator responses aiming for free or heavily discounted stays.
2. **Outreach** — pull listing & host data and craft compelling initial messages.
3. **Schedule** — repeat the above every N hours automatically.

All agents are LLM-agnostic: configure ``LLM_PROVIDER`` (openai / gemini / perplexity)
and the matching API key in ``.env``.
"""
