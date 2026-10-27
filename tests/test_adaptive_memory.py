"""Behavioral checks for scoped learning without an extra language-model call."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from deploy.n8n.adaptive_memory import CORE, COMPILE_JS, INTENT_JS, READ_SQL, install
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


def test_private_conditional_phrase_becomes_search_request_only_when_spoken():
    teaching = _node(INTENT_JS, json.dumps({"original": _sample(
        "اگه گفتم خیار برام اخبار اخیر مهم جنگ رو بگو")}))
    assert teaching["mika"]["management"] is True
    assert teaching["mika"]["teach"] == {"action": "create", "kind": "trigger",
        "scope": "private", "body": "برام اخبار اخیر مهم جنگ رو بگو",
        "example": "خیار", "target": 0, "owner": True, "privateChat": True,
        "agent": "chat"}
    group_teaching = _node(INTENT_JS, json.dumps({"original": _sample(
        "اگه گفتم خیار برام اخبار اخیر مهم جنگ رو بگو", chat_id=-100)}))
    assert group_teaching["mika"]["teach"]["scope"] == "chat"
    trigger = {"memories": [{"scope": "private", "kind": "trigger",
        "example": "خیار", "body": "برام اخبار اخیر مهم جنگ رو بگو"}]}
    spoken = _node(COMPILE_JS, json.dumps({"original": _sample("خیار"),
                                           "input": trigger}))
    assert spoken["mika"]["cleanText"] == "برام اخبار اخیر مهم جنگ رو بگو"
    assert spoken["mika"]["aiPrompt"] == "برام اخبار اخیر مهم جنگ رو بگو"
    assert spoken["mika"]["resolvedTrigger"] == "خیار"
    unrelated = _node(COMPILE_JS, json.dumps({"original": _sample("خیار شور"),
                                              "input": trigger}))
    assert unrelated["mika"]["resolvedTrigger"] == ""
    group = _node(COMPILE_JS, json.dumps({"original": _sample("خیار", chat_id=-100),
                                          "input": trigger}))
    assert group["mika"]["resolvedTrigger"] == ""
    group_trigger = {"memories": [{"scope": "chat", "kind": "trigger",
        "owner_id": 10, "chat_id": -100, "example": "خیار",
        "body": "برام اخبار اخیر مهم جنگ رو بگو"}]}
    same_group = _node(COMPILE_JS, json.dumps({"original": _sample("خیار", chat_id=-100),
                                               "input": group_trigger}))
    assert same_group["mika"]["resolvedTrigger"] == "خیار"
    different_group = _node(COMPILE_JS, json.dumps({"original": _sample("خیار", chat_id=-101),
                                                    "input": group_trigger}))
    assert different_group["mika"]["resolvedTrigger"] == ""
    another_member = _sample("خیار", chat_id=-100)
    another_member["message"]["from"]["id"] = 11
    assert _node(COMPILE_JS, json.dumps({"original": another_member,
                                        "input": group_trigger}))["mika"]["resolvedTrigger"] == ""
    private = _node(COMPILE_JS, json.dumps({"original": _sample("خیار"),
                                            "input": group_trigger}))
    assert private["mika"]["resolvedTrigger"] == ""

    graph = json.loads(ARTIFACT.read_text())
    search = next(node for node in graph["nodes"] if node["name"] == "Search Intent")
    expression = search["parameters"]["conditions"]["conditions"][0]["leftValue"]
    assert expression.startswith("={{ ") and expression.endswith(" }}")
    script = "const x=JSON.parse(process.argv[1]);const j=JSON.parse(process.argv[2]);" \
        "console.log(JSON.stringify(new Function('$json','return '+x)(j)));"
    result = subprocess.run(["node", "-e", script,
        json.dumps(expression[4:-3]), json.dumps(spoken)],
        capture_output=True, text=True, timeout=5, check=True)
    assert json.loads(result.stdout) is True


def test_live_canvas_baseline_and_repeated_reconciliation_keep_positions():
    graph = json.loads(ARTIFACT.read_text())
    read = next(node for node in graph["nodes"] if node["name"] == "Read Relevant Memory")
    assert read["parameters"]["query"] == READ_SQL
    assert "exact_trigger DESC" in READ_SQL
    assert "$json.mika.cleanText" in read["parameters"]["options"]["queryReplacement"]
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
