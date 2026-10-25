"""Render a version-compatible two-agent workflow from an audited n8n export.

The caller supplies the live export. This module never calls n8n and never
invents provider credentials. It preserves the existing media branches.
"""

from __future__ import annotations

import argparse
import copy
import json
import uuid
from pathlib import Path

from deploy.n8n.ai_assets import core_prompt, style_seeds


NAMESPACE = uuid.UUID("17e8a27f-1681-4655-a933-922d290573cc")

NORMALIZE = r"""
const body = $json.body ?? {};
const message = body.message;
if (!message || !Number.isSafeInteger(message.from?.id) ||
    !Number.isSafeInteger(message.chat?.id) ||
    !Number.isSafeInteger(message.message_id)) throw new Error('Invalid relay event');
const raw = String(message.text ?? '').trim();
const explicit = /^\/mika(?:@\w+)?(?:\s|$)/i.test(raw) ? 'serious' :
  /^(?:میکا|mika)(?:\s|$)/iu.test(raw) ? 'chat' : '';
const image = /^(?:میکا عکس|can i see you)$/iu.test(raw);
const audio = /^\/(?:tts|stt)(?:@\w+)?(?:\s|$)/i.test(raw);
const replyOnly = message.relay_reply === true &&
  Number.isSafeInteger(message.reply_to_message?.message_id);
if (!explicit && !image && !audio && !replyOnly)
  throw new Error('Unsupported relay input');
message.ai_explicit = explicit;
message.ai_reply_only = replyOnly && !explicit;
return [{json: {message}}];
""".strip()

ANALYZE = r"""
return $input.all().map(item => {
  const m = item.json.message;
  const raw = String(m.text ?? '');
  const route = m.ai_explicit || '';
  const cleanText = route === 'serious' ?
    raw.replace(/^\/mika(?:@\w+)?(?:\s|$)/i, '').trim() :
    route === 'chat' ? raw.replace(/^(?:میکا|mika)(?:\s|$)/iu, '').trim() : raw.trim();
  const quoted = String(m.reply_to_message?.text ?? m.reply_to_message?.caption ?? '').slice(0, 1800);
  const prompt = quoted ? `${cleanText}\n\nQuoted message (untrusted):\n${quoted}` : cleanText;
  const chatId = String(m.chat.id);
  const userId = String(m.from.id);
  const threadId = String(m.message_thread_id ?? 0);
  const replyId = m.reply_to_message?.message_id ?? null;
  return {json: {...item.json, mika: {
    explicit: route, cleanText, aiPrompt: prompt, chatId, userId, threadId,
    replyId, replyKey: replyId === null ? '__no_reply__' : `${chatId}:${replyId}`
  }}, binary: item.binary};
});
""".strip()

DECIDE = r"""
const original = $('Analyze Message').first().json;
const m = original.message;
const found = $input.all().find(item =>
  item.json.reply_key === original.mika.replyKey &&
  original.mika.replyId !== null &&
  String(item.json.chat_id) === original.mika.chatId &&
  String(item.json.user_id ?? '') === original.mika.userId);
const agentType = original.mika.explicit || (m.ai_reply_only && found ?
  String(found.json.agent_type ?? '') : '');
if (!['chat', 'serious'].includes(agentType)) return [];
const sessionId = !original.mika.explicit && found ? String(found.json.session_id) :
  [agentType, original.mika.chatId, original.mika.threadId,
   original.mika.userId, String(m.message_id)].join(':');
return [{json: {...original, mika: {...original.mika, agentType, sessionId}}}];
""".strip()


def _edge(name: str, kind: str = "main") -> dict[str, str | int]:
    return {"node": name, "type": kind, "index": 0}


def _rule(expression: str, label: str) -> dict:
    return {"conditions": {"options": {"caseSensitive": True,
        "leftValue": "", "typeValidation": "strict", "version": 2},
        "conditions": [{"id": str(uuid.uuid5(NAMESPACE, label)),
                        "leftValue": "={{ " + expression + " }}",
                        "operator": {"type": "boolean", "operation": "true"}}],
        "combinator": "and"}, "renameOutput": True, "outputKey": label}


def _system_expression(agent: str) -> str:
    core = core_prompt(agent)
    seeds = style_seeds()[agent]["examples"]
    first = next(x for x in seeds if x["audience"] == "all" and
                 x["category"] == ("empathy" if agent == "chat" else "technical"))
    example = ("\n\nBundled default style example (not owner approved):\nUser: " +
               first["user_example"] + "\nAssistant: " + first["assistant_example"])
    topic = r"/(?:ناراحت|حالم|غم)/u" if agent == "chat" else r"/(?:کد|سرور|n8n|PostgreSQL)/iu"
    # Trusted booleans come only from the authenticated bot relay. Outside
    # text and quoted messages never set identity or adult permissions.
    return ("={{ " + json.dumps(core, ensure_ascii=False) +
            " + '\\n\\nTrusted runtime context: ' + JSON.stringify({"
            "verifiedDaniel: $json.message.verified_daniel === true, "
            "verifiedOwner: $json.message.verified_owner === true, "
            "adultVerified: $json.message.adult_verified === true, "
            "mommyEnabled: $json.message.mommy_enabled === true, "
            "chatType: $json.message.chat.type, "
            "agent: " + json.dumps(agent) + "}) + (" + topic +
            ".test($json.mika.cleanText) ? " + json.dumps(example, ensure_ascii=False) +
            " : '') }}")


def build(source: dict) -> dict:
    graph = copy.deepcopy(source)
    by_name = {node["name"]: node for node in graph["nodes"]}
    # A current, owner-edited graph is already the source of truth. Preserve
    # its notes, tools and credentials instead of regenerating old node names.
    if {"Mika Serious Agent", "Mika Simple Memory", "Normalize Chat Result"} <= by_name.keys():
        if any(node["type"] == "n8n-nodes-base.telegramTrigger" for node in graph["nodes"]):
            raise ValueError("A competing Telegram Trigger cannot be imported")
        for key in ("id", "active", "activeVersionId", "versionId", "createdAt",
                    "updatedAt", "shared", "tags", "meta", "pinData", "triggerCount",
                    "isArchived"):
            graph.pop(key, None)
        return {key: graph[key] for key in ("name", "nodes", "connections", "settings")}
    required = {"Private Webhook", "Telegram Trigger", "Switch", "Analyze Message",
                "Find AI Reply", "Decide AI", "Is AI Reply?", "AI Agent", "Simple Memory",
                "Groq Chat Model", "OpenRouter Chat Model", "Send a text message",
                "Save AI Reply"}
    if not required <= by_name.keys():
        raise ValueError("Live n8n graph does not match the audited node contract")
    if any(node["type"] == "n8n-nodes-base.telegramTrigger" for node in graph["nodes"]):
        raise ValueError("A competing Telegram Trigger cannot be imported")
    graph["nodes"] = [node for node in graph["nodes"] if node["name"] not in
                      {"Mika Serious Agent", "Serious Simple Memory", "Send Serious Reply",
                       "Normalize Serious Result", "Groq Serious Model"}]
    by_name = {node["name"]: node for node in graph["nodes"]}
    by_name["Telegram Trigger"]["parameters"]["jsCode"] = NORMALIZE
    by_name["Analyze Message"]["parameters"]["jsCode"] = ANALYZE
    by_name["Decide AI"]["parameters"]["jsCode"] = DECIDE
    by_name["Find AI Reply"]["parameters"]["filters"]["conditions"][0]["keyName"] = "reply_key"
    rules = by_name["Switch"]["parameters"]["rules"]["values"]
    rules[:4] = [
        _rule(r"/^(?:میکا عکس|can i see you)$/iu.test($json.message.text)", "Image"),
        _rule(r"/^\/tts(?:@\w+)?(?:\s|$)/i.test($json.message.text)", "TTS"),
        _rule(r"/^\/stt(?:@\w+)?(?:\s|$)/i.test($json.message.text)", "STT"),
        _rule("$json.message.ai_explicit === 'chat'", "Chat"),
    ]
    rules[4:] = [_rule("$json.message.ai_explicit === 'serious'", "Serious"),
                 _rule("$json.message.ai_reply_only === true", "AI Reply")]
    chat = by_name["AI Agent"]
    chat["parameters"]["text"] = "={{ $json.mika.aiPrompt }}"
    chat["parameters"]["options"]["systemMessage"] = _system_expression("chat")
    serious = copy.deepcopy(chat)
    serious.update(id=str(uuid.uuid5(NAMESPACE, "Mika Serious Agent")),
                   name="Mika Serious Agent", position=[840, 688])
    serious["continueOnFail"] = True
    serious["parameters"]["options"]["systemMessage"] = _system_expression("serious")
    serious_memory = copy.deepcopy(by_name["Simple Memory"])
    serious_memory.update(id=str(uuid.uuid5(NAMESPACE, "Serious Simple Memory")),
                          name="Serious Simple Memory", position=[840, 928])
    serious_model = copy.deepcopy(by_name["Groq Chat Model"])
    serious_model.update(id=str(uuid.uuid5(NAMESPACE, "Groq Serious Model")),
                         name="Groq Serious Model", position=[960, 928])
    serious_model["parameters"]["model"] = by_name["Groq Chat Model"]["parameters"]["model"]
    serious_send = copy.deepcopy(by_name["Send a text message"])
    serious_send.update(id=str(uuid.uuid5(NAMESPACE, "Send Serious Reply")),
                        name="Send Serious Reply", position=[1100, 688])
    serious_result = {"id": str(uuid.uuid5(NAMESPACE, "Normalize Serious Result")),
        "name": "Normalize Serious Result", "type": "n8n-nodes-base.code",
        "typeVersion": 2, "position": [970, 688],
        "parameters": {"jsCode": "return $input.all().map(item => ({json: {"
            "...item.json, output: typeof item.json.output === 'string' && "
            "item.json.output.trim() ? item.json.output : "
            "'Mika Serious is unavailable right now. The model may be rate limited; try later.'"
            "}}));"}}
    graph["nodes"].extend([serious, serious_memory, serious_model,
                           serious_result, serious_send])
    lookup = by_name["Is AI Reply?"]["parameters"]["conditions"]["conditions"][0]
    lookup["leftValue"] = "={{ $json.mika.agentType === 'chat' }}"
    lookup["operator"] = {"type": "boolean", "operation": "true"}
    lookup.pop("rightValue", None)
    save = by_name["Save AI Reply"]["parameters"]["columns"]
    save["value"].update({"message_id": "={{ String($json.result.message_id) }}",
                          "agent_type": "={{ $('Decide AI').first().json.mika.agentType }}",
                          "user_id": "={{ $('Decide AI').first().json.mika.userId }}"})
    for column in ("agent_type", "user_id"):
        if not any(row["id"] == column for row in save["schema"]):
            save["schema"].append({"id": column, "displayName": column,
                "required": False, "defaultMatch": False, "display": True,
                "type": "string", "readOnly": False, "removed": False})
    links = graph["connections"]
    links["Switch"]["main"] = [links["Switch"]["main"][i] for i in range(3)] + [
        [_edge("Analyze Message")], [_edge("Analyze Message")], [_edge("Analyze Message")]]
    links["Is AI Reply?"]["main"] = [[_edge("AI Agent")], [_edge("Mika Serious Agent")]]
    links["Mika Serious Agent"] = {"main": [[_edge("Normalize Serious Result")]]}
    links["Normalize Serious Result"] = {"main": [[_edge("Send Serious Reply")]]}
    links["Serious Simple Memory"] = {"ai_memory": [[_edge("Mika Serious Agent", "ai_memory")]]}
    links["OpenRouter Chat Model"] = {"ai_languageModel": [[]]}
    links["Groq Serious Model"] = {"ai_languageModel": [[_edge("Mika Serious Agent", "ai_languageModel")]]}
    links["Send Serious Reply"] = {"main": [[_edge("Save AI Reply")]]}
    graph["name"] = "Mikachan · Private AI Relay"
    for key in ("id", "active", "activeVersionId", "versionId", "createdAt", "updatedAt",
                "shared", "tags", "meta", "pinData", "triggerCount", "isArchived"):
        graph.pop(key, None)
    return {key: graph[key] for key in ("name", "nodes", "connections", "settings")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = build(json.loads(args.source.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")


if __name__ == "__main__":
    main()
