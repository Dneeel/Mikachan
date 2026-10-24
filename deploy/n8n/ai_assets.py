"""Validated, versioned source assets for Mikachan's n8n AI routes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).with_name("ai_assets")
FILES = {
    "chat": ("01_MIKA_CHAT_SYSTEM.md", "58116aef57c03b26bdbf512116a269f049a8530099750bbd766c88cc4fe7b8fa"),
    "serious": ("02_MIKA_SERIOUS_SYSTEM.md", "f628fbb818db4d3aa12d0f856ddf7cbf78caa0fbad73181e7ada1b1f9a407e7e"),
    "chat_dna": ("03_MIKA_CHAT_STYLE_DNA.md", "c91786325f27b3a02292450086acaf7f36a96f9a714244470b812215be3da261"),
    "serious_dna": ("04_MIKA_SERIOUS_WRITING_DNA.md", "0854c3c5875a1aa01d7bcac9f293654334b12bd1bb07f684bbb97cab23d30907"),
    "seeds": ("05_STYLE_SEEDS.json", "4ddb68854250bad7f1922eaafa29daf2f25734e90ef83a3029a2ec3f1fdfb85e"),
}


def read_asset(key: str) -> str:
    name, expected = FILES[key]
    data = (ROOT / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"AI asset checksum mismatch: {name}")
    return data.decode("utf-8")


def core_prompt(agent: str) -> str:
    marker = "## IDENTITY\n" if agent == "chat" else "## IDENTITY, RELATIONSHIP, AND ROLE\n"
    source = read_asset(agent)
    if source.count(marker) != 1:
        raise ValueError(f"Cannot locate canonical {agent} prompt")
    return marker + source.split(marker, 1)[1].strip()


def style_seeds() -> dict:
    data = json.loads(read_asset("seeds"))
    if data.get("schema_version") != "1.1" or data.get("policy", {}).get("user_approved") is not False:
        raise ValueError("Unreviewed or incompatible AI style seeds")
    for agent in ("chat", "serious"):
        ids = [row["id"] for row in data[agent]["examples"]]
        if len(ids) != len(set(ids)) or any(row.get("owner_approved") for row in data[agent]["examples"]):
            raise ValueError("AI style seed provenance is invalid")
    return data

