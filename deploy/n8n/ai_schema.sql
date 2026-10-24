-- Mikachan AI metadata. n8n's Postgres Chat Memory nodes create their own
-- mika_chat_history and mika_serious_history layouts; do not pre-create them.
BEGIN;

CREATE TABLE IF NOT EXISTS mika_identity (
    telegram_user_id BIGINT PRIMARY KEY,
    identity_key TEXT UNIQUE NOT NULL,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    adult_verified BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    verified_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mika_profiles (
    telegram_user_id BIGINT PRIMARY KEY REFERENCES mika_identity(telegram_user_id),
    preferred_name TEXT,
    language TEXT NOT NULL DEFAULT 'fa',
    memory_backend TEXT NOT NULL DEFAULT 'postgres'
        CHECK (memory_backend IN ('postgres', 'simple')),
    mommy_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mika_long_term_facts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_user_id BIGINT NOT NULL REFERENCES mika_identity(telegram_user_id),
    owner_user_id BIGINT NOT NULL REFERENCES mika_identity(telegram_user_id),
    fact TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('private', 'group', 'public')),
    source_chat_id BIGINT,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mika_facts_owner_scope
    ON mika_long_term_facts(owner_user_id, visibility, active, created_at DESC);

CREATE TABLE IF NOT EXISTS mika_episodic_memory (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent_type TEXT NOT NULL CHECK (agent_type IN ('chat', 'serious')),
    owner_user_id BIGINT NOT NULL REFERENCES mika_identity(telegram_user_id),
    chat_id BIGINT NOT NULL,
    thread_id BIGINT NOT NULL DEFAULT 0,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_message_id BIGINT,
    visibility TEXT NOT NULL CHECK (visibility IN ('private', 'group')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mika_episodes_scope
    ON mika_episodic_memory(owner_user_id, agent_type, visibility, created_at DESC);

CREATE TABLE IF NOT EXISTS mika_style_rules (
    agent_type TEXT NOT NULL CHECK (agent_type IN ('chat', 'serious')),
    rule_key TEXT NOT NULL,
    body TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    provenance TEXT NOT NULL,
    owner_approved BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(agent_type, rule_key)
);

CREATE TABLE IF NOT EXISTS mika_style_examples (
    example_id TEXT PRIMARY KEY,
    agent_type TEXT NOT NULL CHECK (agent_type IN ('chat', 'serious')),
    category TEXT NOT NULL,
    audience TEXT NOT NULL,
    user_example TEXT NOT NULL,
    assistant_example TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    provenance TEXT NOT NULL,
    owner_approved BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS mika_style_pick
    ON mika_style_examples(agent_type, category, audience, enabled);

CREATE TABLE IF NOT EXISTS mika_reply_provenance (
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    thread_id BIGINT NOT NULL DEFAULT 0,
    root_message_id BIGINT NOT NULL,
    agent_type TEXT NOT NULL CHECK (agent_type IN ('chat', 'serious')),
    session_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS mika_reply_by_session
    ON mika_reply_provenance(session_id, created_at DESC);

COMMIT;
