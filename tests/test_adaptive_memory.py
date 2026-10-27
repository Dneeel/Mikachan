"""Behavioral checks for scoped learning without an extra language-model call."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from deploy.n8n.adaptive_memory import CORE, COMPILE_JS, INTENT_JS, install
from deploy.n8n.ai_assets import core_prompt
from deploy.n8n.reconcile_ai import _configure_search


ARTIFACT = Path(__file__).resolve().parents[1] / "deploy/n8n/mikachan_ai_workflow.json"


def _node(code: str, body: str) -> dict:
    if not shutil.which("node"):
        pytest.skip("Node.js is needed to evaluate n8n Code nodes")
    script = """const c=JSON.parse(process.argv[1]);
const m=JSON.parse(process.argv[2]);
const $=(name)=>({first:()=>({json:m.original})});
const $input={first:()=>({json:m.input??m.original})};
console.log(JSON.stringify(new Function('$input','$',c)($input,$)[0].json));"""
    run = subprocess.run(["node", "-e", script, json.dumps(code), body],
        capture_output=True, text=True, timeout=5, check=True)
    return json.loads(run.stdout)


def _sample(text: str, *, owner: bool = True, chat_id: int = 10) -> dict:
    return {"message": {"from": {"id": 10}, "chat": {"id": chat_id,
        "type": "private" if chat_id == 10 else "supergroup"},
        "message_id": 25, "verified_owner": owner},
        "mika": {"agentType": "chat", "cleanText": text, "aiPrompt": text}}


def test_teaching_intent_and_greeting_are_deterministic():
    global_rule = _node(INTENT_JS, json.dumps({"original": _sample(
        "قانون ادبیاتت: درباره کتاب کوتاه بنویس")}))
    assert global_rule["mika"]["teach"]["scope"] == "global"
    assert global_rule["mika"]["teach"]["kind"] == "writing"
    assert global_rule["mika"]["management"] is True
    personal = _node(INTENT_JS, json.dumps({"original": _sample(
        "یادت باشه: من کتاب قلعه را دوست دارم", owner=False)}))
    assert personal["mika"]["teach"]["agent"] == "both"
    assert personal["mika"]["teach"]["scope"] == "private"
    without_colon = _node(INTENT_JS, json.dumps({"original": _sample(
        "یادت باشه من کتاب قلعه را دوست دارم", owner=False)}))
    assert without_colon["mika"]["teach"]["body"] == "من کتاب قلعه را دوست دارم"
    greeting = _node(INTENT_JS, json.dumps({"original": _sample("سلام")}))
    assert greeting["mika"]["needMemory"] is False
    edit = _node(INTENT_JS, json.dumps({"original": _sample(
        "حافظه ویرایش 12: من کتاب دیگری را دوست دارم", owner=False)}))
    assert edit["mika"]["teach"]["scope"] == "private"
    correction = _sample("اصلاح جواب: جواب کوتاه تر", owner=True)
    correction["message"]["reply_to_message"] = {"text": "جواب قبلی"}
    assert _node(INTENT_JS, json.dumps({"original": correction}))["mika"]["teach"]["scope"] == "private"
    correction["mika"]["cleanText"] = "اصلاح جواب سراسری: جواب کوتاه تر"
    assert _node(INTENT_JS, json.dumps({"original": correction}))["mika"]["teach"]["scope"] == "global"


def test_compilation_enforces_scope_and_context_budget():
    data = _sample("کتاب")
    memory = {"memories": [
        {"scope": "global", "kind": "writing", "body": "درباره کتاب کوتاه بنویس"},
        {"scope": "private", "kind": "fact", "body": "کتاب قلعه را دوست دارم"}]}
    compiled = _node(COMPILE_JS, json.dumps({"original": data, "input": memory}))
    assert "درباره کتاب کوتاه بنویس" in compiled["mikaCompiledSystem"]
    assert "کتاب قلعه" in compiled["mika"]["aiPrompt"]
    assert "کتاب قلعه" not in compiled["mikaCompiledSystem"]
    assert compiled["mika"]["compiledSystemChars"] <= len(CORE["chat"]) + 1100
    group = _node(COMPILE_JS, json.dumps({"original": _sample("کتاب", chat_id=-100),
                                          "input": memory}))
    assert "کتاب قلعه" not in group["mika"]["aiPrompt"]
    other = _node(COMPILE_JS, json.dumps({"original": _sample("هواشناسی"),
                                          "input": {"memories": []}}))
    assert "کتاب قلعه" not in other["mika"]["aiPrompt"]


def test_live_canvas_baseline_and_repeated_reconciliation_keep_positions():
    graph = json.loads(ARTIFACT.read_text())
    original = {node["id"]: node["position"] for node in graph["nodes"]}
    graph["nodes"][0]["position"] = [999, 111]
    original[graph["nodes"][0]["id"]] = [999, 111]
    _configure_search(graph, "test-tavily")
    install(graph, "test-postgres")
    _configure_search(graph, "test-tavily")
    install(graph, "test-postgres")
    assert all(node["position"] == original[node["id"]] for node in graph["nodes"]
               if node["id"] in original)
    assert len({node["id"] for node in graph["nodes"]}) == len(graph["nodes"])


def test_compact_core_is_smaller_than_previous_system_prompt():
    for agent in ("chat", "serious"):
        assert len(CORE[agent]) < len(core_prompt(agent)) * 0.5
