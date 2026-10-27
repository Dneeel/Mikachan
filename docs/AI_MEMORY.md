# Mika memory and teaching

The active n8n workflow is the visual source of truth. The packaged workflow
starts from its latest exported canvas; reconciliation preserves the coordinates
of every existing node, including owner-moved nodes. Backups are written before
an in-place update. No second Telegram update consumer or database is created.

Mika Chat and Mika Serious have separate PostgreSQL conversation histories.
Durable teaching lives in versioned tables in the **same managed AI PostgreSQL**.
Switching Groq or Cloudflare models cannot erase these tables. Choosing Simple
Memory changes only the chat-history backend; durable teaching stays in
PostgreSQL. Managed migrations run during Mikachan release updates and during
AI Integration repair, without a new installer question.

## Teach in a private Telegram chat

The bot checks the sender's numeric Telegram ID and supplies the trusted Owner
flag. Training phrases are parsed deterministically; ordinary messages do not
make another AI call to decide what to store.

| Chat command | Effect |
| --- | --- |
| `میکا یادت باشه: من کتاب قلعه را دوست دارم` | Private fact for that sender, visible to both agents only in that sender's private chat. |
| `میکا قانون ادبیاتت: درباره کتاب کوتاه بنویس` | Owner-only global Chat writing rule. Use `/mika قانون ادبیاتت: ...` for Serious. |
| `میکا قانون شخصیتت: ...` | Owner-only global personality rule for the selected agent. |
| `میکا نمونه طلایی: سؤال => پاسخ` | Owner-only global example. |
| Reply to an AI answer with `میکا اصلاح جواب: پاسخ بهتر` | A private correction example for that sender. |
| Reply with `میکا اصلاح جواب سراسری: پاسخ بهتر` | An Owner-only global correction example, explicitly shared with all users. |
| `میکا حافظه` / `میکا قوانین` | Show personal facts / Owner's global rules and their IDs. |
| `میکا حافظه ویرایش 12: متن جدید` | Edit a personal item. Use `قانون` for global items. |
| `میکا حافظه حذف 12` / `میکا حافظه بازگردانی 12` | Delete or restore the preceding revision. Use `قانون` for global items. |
| `میکا قانون تایید 12` | Activate a pending global rule or edit after reviewing a possible conflict. |

Global changes that overlap an existing rule type are staged as **pending** and
need Owner confirmation. Attempts to change core identity, credentials or
access rules are rejected. This check is conservative and deterministic; it
cannot prove that two natural-language rules are semantically consistent.
Review pending rules before confirming them. A save confirmation is sent only
after PostgreSQL returns a successful result. Failed writes produce a failure
message, never a success claim.

## Context and privacy

A short immutable core and trusted runtime flags are compiled into
`mikaCompiledSystem` or `seriousCompiledSystem`. Each Agent's System Message
reads only its field. At most three relevant rules, one example and two private
facts are selected from PostgreSQL, under a 900-character memory budget. Simple
greetings skip the memory query. Private facts stay in the user prompt as
unverified data and are never promoted into system instructions. The SQL scope
check and a second Code node check prevent another user or a group from
receiving a private fact. Older explicit private facts are migrated into the
editable table idempotently; the old table remains for rollback.

Postgres Chat Memory keeps the last four conversation turns per agent session.
Ordinary conversation is not automatically treated as a durable fact; teach a
fact explicitly to avoid storing a mistaken inference. Back up or restore the
independent AI database with `n8n-manage` → **AI Memory · PostgreSQL**.
