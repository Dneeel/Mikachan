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
    source_message_id BIGINT,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE mika_long_term_facts
    ADD COLUMN IF NOT EXISTS source_message_id BIGINT;
CREATE INDEX IF NOT EXISTS mika_facts_owner_scope
    ON mika_long_term_facts(owner_user_id, visibility, active, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS mika_facts_explicit_message
    ON mika_long_term_facts(owner_user_id, source_chat_id, source_message_id)
    WHERE source_message_id IS NOT NULL;

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

-- Versioned, separately scoped durable teaching. Chat history remains in
-- n8n's own Postgres Chat Memory tables and is never mined automatically.
CREATE TABLE IF NOT EXISTS mika_teaching_entries (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('private', 'global')),
    owner_user_id BIGINT NOT NULL,
    agent_type TEXT NOT NULL CHECK (agent_type IN ('chat', 'serious', 'both')),
    kind TEXT NOT NULL CHECK (kind IN ('fact', 'personality', 'writing', 'example')),
    body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 600),
    example_input TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'pending', 'deleted')),
    revision INTEGER NOT NULL DEFAULT 1,
    source_chat_id BIGINT NOT NULL,
    source_message_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, source_chat_id, source_message_id)
);
CREATE INDEX IF NOT EXISTS mika_teaching_scope
    ON mika_teaching_entries (scope, owner_user_id, agent_type, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS mika_teaching_revisions (
    entry_id BIGINT NOT NULL REFERENCES mika_teaching_entries(id),
    revision INTEGER NOT NULL,
    body TEXT NOT NULL,
    example_input TEXT,
    status TEXT NOT NULL,
    changed_by BIGINT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (entry_id, revision)
);
CREATE TABLE IF NOT EXISTS mika_teaching_proposals (
    entry_id BIGINT PRIMARY KEY REFERENCES mika_teaching_entries(id),
    body TEXT NOT NULL,
    example_input TEXT,
    proposed_by BIGINT NOT NULL,
    proposed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mika_teaching_operations (
    actor_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    result JSONB NOT NULL,
    PRIMARY KEY (actor_id, chat_id, message_id)
);

-- Bring previously explicit private facts into the editable, versioned store.
-- The old table remains intact for rollback; retries cannot duplicate items.
INSERT INTO mika_teaching_entries
  (scope,owner_user_id,agent_type,kind,body,status,source_chat_id,
   source_message_id,created_at,updated_at)
SELECT 'private', owner_user_id, 'both', 'fact', fact, 'active',
  COALESCE(source_chat_id,owner_user_id), COALESCE(source_message_id,-id),
  created_at, updated_at
FROM mika_long_term_facts
WHERE active AND visibility='private' AND length(fact) BETWEEN 1 AND 600
ON CONFLICT (owner_user_id,source_chat_id,source_message_id) DO NOTHING;
INSERT INTO mika_teaching_revisions
  (entry_id,revision,body,example_input,status,changed_by,changed_at)
SELECT id,1,body,example_input,status,owner_user_id,created_at
FROM mika_teaching_entries ON CONFLICT (entry_id,revision) DO NOTHING;

CREATE OR REPLACE FUNCTION mika_teach(
    p_actor BIGINT, p_chat BIGINT, p_message BIGINT, p_owner BOOLEAN,
    p_action TEXT, p_scope TEXT, p_agent TEXT, p_kind TEXT,
    p_target BIGINT, p_body TEXT, p_example TEXT
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    row_item mika_teaching_entries%ROWTYPE;
    old_revision mika_teaching_revisions%ROWTYPE;
    proposal mika_teaching_proposals%ROWTYPE;
    answer JSONB;
    chosen_status TEXT;
BEGIN
    IF p_actor IS NULL OR p_actor <= 0 OR p_chat <> p_actor OR
       p_message IS NULL OR p_message <= 0 THEN
        RETURN jsonb_build_object('status', 'DENIED', 'reason', 'Private chat required');
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
      p_actor::text || ':' || p_message::text, 0));
    SELECT result INTO answer FROM mika_teaching_operations
      WHERE actor_id=p_actor AND chat_id=p_chat AND message_id=p_message;
    IF FOUND THEN RETURN answer; END IF;
    IF p_scope = 'global' AND NOT p_owner THEN
        answer := jsonb_build_object('status', 'DENIED', 'reason', 'Owner required');
    ELSIF p_action = 'list' THEN
        SELECT jsonb_build_object('status', 'LIST', 'items',
          COALESCE(jsonb_agg(jsonb_build_object('id',id,'kind',kind,'agent',agent_type,
            'body',left(body,120),'status',status) ORDER BY updated_at DESC), '[]'::jsonb))
          INTO answer FROM (SELECT * FROM mika_teaching_entries
            WHERE scope=p_scope AND (p_scope='global' OR owner_user_id=p_actor)
              AND status <> 'deleted' ORDER BY updated_at DESC LIMIT 12) recent;
    ELSIF p_action = 'create' THEN
        IF p_scope NOT IN ('private','global') OR
           p_agent NOT IN ('chat','serious','both') OR
           p_kind NOT IN ('fact','personality','writing','example') OR
           p_body IS NULL OR length(btrim(p_body)) NOT BETWEEN 1 AND 600 OR
           (p_scope='global' AND p_kind='fact') THEN
            answer := jsonb_build_object('status','INVALID');
        ELSIF p_scope='global' AND p_body ~* 'verifiedDaniel|verifiedOwner|adultVerified|توکن|رمز عبور|مجوز دسترسی|افشای اسرار' THEN
            answer := jsonb_build_object('status','CORE_CONFLICT');
        ELSE
            chosen_status := 'active';
            IF p_scope='global' AND EXISTS (SELECT 1 FROM mika_teaching_entries
               WHERE scope='global' AND agent_type=p_agent AND kind=p_kind
                 AND status='active') THEN
                chosen_status := 'pending';
            END IF;
            INSERT INTO mika_teaching_entries
              (scope,owner_user_id,agent_type,kind,body,example_input,status,
               source_chat_id,source_message_id)
              VALUES (p_scope,p_actor,p_agent,p_kind,btrim(p_body),
                nullif(btrim(p_example),''),chosen_status,p_chat,p_message)
              RETURNING * INTO row_item;
            INSERT INTO mika_teaching_revisions
              (entry_id,revision,body,example_input,status,changed_by)
              VALUES (row_item.id,1,row_item.body,row_item.example_input,
                      chosen_status,p_actor);
            answer := jsonb_build_object('status',upper(chosen_status),'id',row_item.id);
        END IF;
    ELSIF p_action IN ('edit','delete','restore','confirm') THEN
        SELECT * INTO row_item FROM mika_teaching_entries WHERE id=p_target FOR UPDATE;
        IF NOT FOUND OR (row_item.scope='global' AND NOT p_owner) OR
           (row_item.scope='private' AND row_item.owner_user_id<>p_actor) THEN
            answer := jsonb_build_object('status','DENIED');
        ELSIF p_action='edit' THEN
            IF p_body IS NULL OR length(btrim(p_body)) NOT BETWEEN 1 AND 600 THEN
                answer := jsonb_build_object('status','INVALID');
            ELSIF row_item.scope='global' AND p_body ~*
              'verifiedDaniel|verifiedOwner|adultVerified|توکن|رمز عبور|مجوز دسترسی|افشای اسرار' THEN
                answer := jsonb_build_object('status','CORE_CONFLICT');
            ELSIF row_item.scope='global' THEN
                INSERT INTO mika_teaching_proposals(entry_id,body,example_input,proposed_by)
                VALUES (row_item.id,btrim(p_body),row_item.example_input,p_actor)
                ON CONFLICT (entry_id) DO UPDATE SET body=EXCLUDED.body,
                  proposed_by=EXCLUDED.proposed_by,proposed_at=now();
                answer := jsonb_build_object('status','PENDING','id',row_item.id);
            ELSE
                UPDATE mika_teaching_entries SET body=btrim(p_body),
                  revision=revision+1,updated_at=now() WHERE id=row_item.id
                  RETURNING * INTO row_item;
                INSERT INTO mika_teaching_revisions
                  (entry_id,revision,body,example_input,status,changed_by)
                  VALUES (row_item.id,row_item.revision,row_item.body,
                          row_item.example_input,row_item.status,p_actor);
                answer := jsonb_build_object('status','UPDATED','id',row_item.id);
            END IF;
        ELSIF p_action='delete' THEN
            UPDATE mika_teaching_entries SET status='deleted',revision=revision+1,
              updated_at=now() WHERE id=row_item.id RETURNING * INTO row_item;
            INSERT INTO mika_teaching_revisions
              (entry_id,revision,body,example_input,status,changed_by)
              VALUES (row_item.id,row_item.revision,row_item.body,
                      row_item.example_input,row_item.status,p_actor);
            answer := jsonb_build_object('status','DELETED','id',row_item.id);
        ELSIF p_action='restore' THEN
            SELECT * INTO old_revision FROM mika_teaching_revisions
              WHERE entry_id=row_item.id AND revision=row_item.revision-1;
            IF NOT FOUND THEN
                answer := jsonb_build_object('status','NO_PREVIOUS_VERSION');
            ELSE
                UPDATE mika_teaching_entries SET body=old_revision.body,
                  example_input=old_revision.example_input,status=old_revision.status,
                  revision=revision+1,updated_at=now() WHERE id=row_item.id
                  RETURNING * INTO row_item;
                INSERT INTO mika_teaching_revisions
                  (entry_id,revision,body,example_input,status,changed_by)
                  VALUES (row_item.id,row_item.revision,row_item.body,
                          row_item.example_input,row_item.status,p_actor);
                answer := jsonb_build_object('status','RESTORED','id',row_item.id);
            END IF;
        ELSE
            SELECT * INTO proposal FROM mika_teaching_proposals
              WHERE entry_id=row_item.id FOR UPDATE;
            IF row_item.status='pending' THEN
                UPDATE mika_teaching_entries SET status='active',
                  revision=revision+1,updated_at=now() WHERE id=row_item.id
                  RETURNING * INTO row_item;
            ELSIF FOUND THEN
                UPDATE mika_teaching_entries SET body=proposal.body,
                  example_input=proposal.example_input,revision=revision+1,
                  updated_at=now() WHERE id=row_item.id RETURNING * INTO row_item;
                DELETE FROM mika_teaching_proposals WHERE entry_id=row_item.id;
            ELSE
                RETURN jsonb_build_object('status','NO_PENDING_CHANGE');
            END IF;
            INSERT INTO mika_teaching_revisions
              (entry_id,revision,body,example_input,status,changed_by)
              VALUES (row_item.id,row_item.revision,row_item.body,
                      row_item.example_input,row_item.status,p_actor);
            answer := jsonb_build_object('status','ACTIVE','id',row_item.id);
        END IF;
    ELSE
        answer := jsonb_build_object('status','INVALID');
    END IF;
    INSERT INTO mika_teaching_operations(actor_id,chat_id,message_id,result)
      VALUES (p_actor,p_chat,p_message,answer);
    RETURN answer;
END;
$$;

COMMIT;
