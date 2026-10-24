"""Offline contract tests for the owner's two n8n agents and source assets."""

import json
from pathlib import Path

from deploy.n8n.ai_assets import core_prompt, style_seeds
from deploy.n8n.build_ai_workflow import build


ARTIFACT = Path(__file__).resolve().parents[1] / "deploy/n8n/mikachan_ai_workflow.json"


def test_canonical_assets_and_seed_provenance():
    assert "دن" in core_prompt("chat") and "هورنی" in core_prompt("chat")
    assert "## IDENTITY, RELATIONSHIP, AND ROLE" in core_prompt("serious")
    seeds = style_seeds()
    assert len(seeds["chat"]["examples"]) == 40
    assert len(seeds["serious"]["examples"]) == 22
    assert all(not row["owner_approved"] for agent in ("chat", "serious")
               for row in seeds[agent]["examples"])


def test_artifact_has_two_isolated_agents_and_preserves_media_edges():
    graph = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    rebuilt = build(graph)
    assert len(rebuilt["nodes"]) == len(graph["nodes"])
    nodes = {node["name"]: node for node in graph["nodes"]}
    assert len([node for node in graph["nodes"] if node["type"] ==
                "n8n-nodes-base.telegramTrigger"]) == 0
    assert nodes["AI Agent"]["parameters"]["options"]["systemMessage"] != \
        nodes["Mika Serious Agent"]["parameters"]["options"]["systemMessage"]
    links = graph["connections"]
    assert links["Groq Chat Model"]["ai_languageModel"][0][0]["node"] == "AI Agent"
    assert links["OpenRouter Chat Model"]["ai_languageModel"][0][0]["node"] == \
        "Mika Serious Agent"
    assert links["Simple Memory"]["ai_memory"][0][0]["node"] == "AI Agent"
    assert links["Serious Simple Memory"]["ai_memory"][0][0]["node"] == \
        "Mika Serious Agent"
    assert links["Get Mikasa"]["main"][0][0]["node"] == "Nude"
    assert links["Get Voice File"]["main"][0][0]["node"] == "TTS ElevenLabs"
    assert links["STT ElevenLabs"]["main"][0][0]["node"] == "Code in JavaScript"
    assert "reply_key" in nodes["Save AI Reply"]["parameters"]["columns"]["value"]
    assert "agent_type" in nodes["Save AI Reply"]["parameters"]["columns"]["value"]
