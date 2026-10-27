# Mikachan Intelligence Desk · v2

This is a fictional owner tool for terms observed in approved Telegram groups.
It is not affiliated with an intelligence agency. It starts counting only after
the owner adds a term. It cannot read historic Telegram messages or messages
Telegram never delivered to Mikachan.

Open private `/settings` → **Intelligence Desk**. Add a category, then add
words or phrases. Categories start private. Toggle group visibility to show
that category's count in a group member's `/id` Details panel. The owner panel
always shows every category. Disabling a term stops future matching and keeps
its previously recorded evidence. Export a category, term, or person-category
as TXT, JSON, or offline HTML; exports are assembled in memory and sent as
Telegram documents. The current export contains the 10,000 latest recorded
occurrences for the selected scope, and the owner UI should be used for smaller
scopes if more history exists. No permanent web server is started.

In an approved group, reply to a member with `/id` or `آیدی`/`ایدی`/`Id`.
Summary and Details use the authenticated sender ID. Counts are scoped to the
current group, and manual nicknames come only from that group's settings.
Only the owner can edit global identity fields; editing takes place by private
message to avoid putting personal details in the group. The Manage screen
allows the owner to toggle a registered person's private-chat access. Group
administrators cannot change global access.

Each matching occurrence is recorded with the group ID, user ID, message ID,
time and up to 1,000 characters of the original text. This can include private
content from group conversations. Keep the SQLite database and exports
protected, and do not publish exported files or expose them through public
HTTP. Counts reflect observed messages and retained per-user history, not the
entire group's lifetime.
