BEGIN;

CREATE SCHEMA IF NOT EXISTS closenetting;

CREATE TABLE IF NOT EXISTS closenetting.ingestion_files (
    id BIGSERIAL PRIMARY KEY,
    entity TEXT NOT NULL,
    report_type TEXT,
    source_filename TEXT NOT NULL,
    source_path TEXT,
    message_id TEXT,
    attachment_id TEXT,
    file_sha256 CHAR(64),
    content_sha256 CHAR(64),
    size_bytes BIGINT,
    report_date DATE,
    status TEXT NOT NULL DEFAULT 'processed',
    duplicate_of_ingestion_id BIGINT,
    received_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_ingestion_entity_not_blank CHECK (btrim(entity) <> ''),
    CONSTRAINT ck_ingestion_filename_not_blank CHECK (btrim(source_filename) <> ''),
    CONSTRAINT ck_ingestion_size_nonnegative CHECK (size_bytes IS NULL OR size_bytes >= 0),
    CONSTRAINT ck_ingestion_status CHECK (
        status IN (
            'processed',
            'duplicate_binary',
            'duplicate_content',
            'parse_error',
            'unmapped_report_type'
        )
    ),
    CONSTRAINT fk_ingestion_duplicate_of
        FOREIGN KEY (duplicate_of_ingestion_id)
        REFERENCES closenetting.ingestion_files(id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ingestion_graph_attachment
    ON closenetting.ingestion_files (message_id, attachment_id)
    WHERE message_id IS NOT NULL AND attachment_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_ingestion_entity_content_processed
    ON closenetting.ingestion_files (entity, content_sha256)
    WHERE content_sha256 IS NOT NULL AND status = 'processed';

CREATE UNIQUE INDEX IF NOT EXISTS uq_ingestion_entity_file_processed
    ON closenetting.ingestion_files (entity, file_sha256)
    WHERE file_sha256 IS NOT NULL AND status = 'processed';

CREATE INDEX IF NOT EXISTS ix_ingestion_entity
    ON closenetting.ingestion_files (entity);

CREATE INDEX IF NOT EXISTS ix_ingestion_report_type
    ON closenetting.ingestion_files (entity, report_type);

CREATE INDEX IF NOT EXISTS ix_ingestion_report_date
    ON closenetting.ingestion_files (report_date);

CREATE INDEX IF NOT EXISTS ix_ingestion_processed_at
    ON closenetting.ingestion_files (processed_at);

CREATE TABLE IF NOT EXISTS closenetting.report_schemas (
    id BIGSERIAL PRIMARY KEY,
    entity TEXT NOT NULL,
    report_type TEXT NOT NULL,
    schema_hash CHAR(64) NOT NULL,
    columns_json JSONB NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_report_schema UNIQUE (entity, report_type, schema_hash),
    CONSTRAINT ck_report_schema_entity_not_blank CHECK (btrim(entity) <> ''),
    CONSTRAINT ck_report_schema_type_not_blank CHECK (btrim(report_type) <> ''),
    CONSTRAINT ck_report_schema_columns_array CHECK (jsonb_typeof(columns_json) = 'array')
);

CREATE INDEX IF NOT EXISTS ix_report_schemas_entity_type
    ON closenetting.report_schemas (entity, report_type);

CREATE TABLE IF NOT EXISTS closenetting.report_table_registry (
    id BIGSERIAL PRIMARY KEY,
    entity TEXT NOT NULL,
    report_type TEXT NOT NULL,
    destination_table TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_report_table_registry_entity_type UNIQUE (entity, report_type),
    CONSTRAINT uq_report_table_registry_destination UNIQUE (destination_table),
    CONSTRAINT ck_registry_entity_not_blank CHECK (btrim(entity) <> ''),
    CONSTRAINT ck_registry_type_not_blank CHECK (btrim(report_type) <> ''),
    CONSTRAINT ck_registry_destination_not_blank CHECK (btrim(destination_table) <> '')
);

CREATE TABLE IF NOT EXISTS closenetting.gs_forward (
    id BIGSERIAL PRIMARY KEY,
    ingestion_id BIGINT NOT NULL,
    report_schema_id BIGINT NOT NULL,
    report_type TEXT NOT NULL DEFAULT 'FORWARD',
    source_sheet_name TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    trade_date DATE,
    source_cells JSONB NOT NULL,
    normalized_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_gs_forward_ingestion
        FOREIGN KEY (ingestion_id)
        REFERENCES closenetting.ingestion_files(id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_gs_forward_schema
        FOREIGN KEY (report_schema_id)
        REFERENCES closenetting.report_schemas(id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_gs_forward_source_row UNIQUE (ingestion_id, source_sheet_name, source_row_number),
    CONSTRAINT ck_gs_forward_report_type CHECK (report_type = 'FORWARD'),
    CONSTRAINT ck_gs_forward_row_number CHECK (source_row_number > 0),
    CONSTRAINT ck_gs_forward_source_cells CHECK (jsonb_typeof(source_cells) = 'array'),
    CONSTRAINT ck_gs_forward_normalized_data CHECK (jsonb_typeof(normalized_data) = 'object')
);

CREATE INDEX IF NOT EXISTS ix_gs_forward_ingestion
    ON closenetting.gs_forward (ingestion_id);
CREATE INDEX IF NOT EXISTS ix_gs_forward_trade_date
    ON closenetting.gs_forward (trade_date);
CREATE INDEX IF NOT EXISTS ix_gs_forward_normalized_data
    ON closenetting.gs_forward USING GIN (normalized_data);

CREATE TABLE IF NOT EXISTS closenetting.gs_options (
    id BIGSERIAL PRIMARY KEY,
    ingestion_id BIGINT NOT NULL,
    report_schema_id BIGINT NOT NULL,
    report_type TEXT NOT NULL DEFAULT 'OPTIONS',
    source_sheet_name TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    trade_date DATE,
    source_cells JSONB NOT NULL,
    normalized_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_gs_options_ingestion
        FOREIGN KEY (ingestion_id)
        REFERENCES closenetting.ingestion_files(id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_gs_options_schema
        FOREIGN KEY (report_schema_id)
        REFERENCES closenetting.report_schemas(id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_gs_options_source_row UNIQUE (ingestion_id, source_sheet_name, source_row_number),
    CONSTRAINT ck_gs_options_report_type CHECK (report_type = 'OPTIONS'),
    CONSTRAINT ck_gs_options_row_number CHECK (source_row_number > 0),
    CONSTRAINT ck_gs_options_source_cells CHECK (jsonb_typeof(source_cells) = 'array'),
    CONSTRAINT ck_gs_options_normalized_data CHECK (jsonb_typeof(normalized_data) = 'object')
);

CREATE INDEX IF NOT EXISTS ix_gs_options_ingestion
    ON closenetting.gs_options (ingestion_id);
CREATE INDEX IF NOT EXISTS ix_gs_options_trade_date
    ON closenetting.gs_options (trade_date);
CREATE INDEX IF NOT EXISTS ix_gs_options_normalized_data
    ON closenetting.gs_options USING GIN (normalized_data);

CREATE TABLE IF NOT EXISTS closenetting.gs_iris (
    id BIGSERIAL PRIMARY KEY,
    ingestion_id BIGINT NOT NULL,
    report_schema_id BIGINT NOT NULL,
    report_type TEXT NOT NULL DEFAULT 'IRIS',
    source_sheet_name TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    trade_date DATE,
    source_cells JSONB NOT NULL,
    normalized_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_gs_iris_ingestion
        FOREIGN KEY (ingestion_id)
        REFERENCES closenetting.ingestion_files(id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_gs_iris_schema
        FOREIGN KEY (report_schema_id)
        REFERENCES closenetting.report_schemas(id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_gs_iris_source_row UNIQUE (ingestion_id, source_sheet_name, source_row_number),
    CONSTRAINT ck_gs_iris_report_type CHECK (report_type = 'IRIS'),
    CONSTRAINT ck_gs_iris_row_number CHECK (source_row_number > 0),
    CONSTRAINT ck_gs_iris_source_cells CHECK (jsonb_typeof(source_cells) = 'array'),
    CONSTRAINT ck_gs_iris_normalized_data CHECK (jsonb_typeof(normalized_data) = 'object')
);

CREATE INDEX IF NOT EXISTS ix_gs_iris_ingestion
    ON closenetting.gs_iris (ingestion_id);
CREATE INDEX IF NOT EXISTS ix_gs_iris_trade_date
    ON closenetting.gs_iris (trade_date);
CREATE INDEX IF NOT EXISTS ix_gs_iris_normalized_data
    ON closenetting.gs_iris USING GIN (normalized_data);

CREATE TABLE IF NOT EXISTS closenetting.gs_css (
    id BIGSERIAL PRIMARY KEY,
    ingestion_id BIGINT NOT NULL,
    report_schema_id BIGINT NOT NULL,
    report_type TEXT NOT NULL DEFAULT 'CSS',
    source_sheet_name TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    trade_date DATE,
    source_cells JSONB NOT NULL,
    normalized_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT fk_gs_css_ingestion
        FOREIGN KEY (ingestion_id)
        REFERENCES closenetting.ingestion_files(id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_gs_css_schema
        FOREIGN KEY (report_schema_id)
        REFERENCES closenetting.report_schemas(id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_gs_css_source_row UNIQUE (ingestion_id, source_sheet_name, source_row_number),
    CONSTRAINT ck_gs_css_report_type CHECK (report_type = 'CSS'),
    CONSTRAINT ck_gs_css_row_number CHECK (source_row_number > 0),
    CONSTRAINT ck_gs_css_source_cells CHECK (jsonb_typeof(source_cells) = 'array'),
    CONSTRAINT ck_gs_css_normalized_data CHECK (jsonb_typeof(normalized_data) = 'object')
);

CREATE INDEX IF NOT EXISTS ix_gs_css_ingestion
    ON closenetting.gs_css (ingestion_id);
CREATE INDEX IF NOT EXISTS ix_gs_css_trade_date
    ON closenetting.gs_css (trade_date);
CREATE INDEX IF NOT EXISTS ix_gs_css_normalized_data
    ON closenetting.gs_css USING GIN (normalized_data);

INSERT INTO closenetting.report_table_registry (
    entity,
    report_type,
    destination_table,
    is_active
)
VALUES
    ('GS', 'FORWARD', 'closenetting.gs_forward', TRUE),
    ('GS', 'OPTIONS', 'closenetting.gs_options', TRUE),
    ('GS', 'IRIS', 'closenetting.gs_iris', TRUE),
    ('GS', 'CSS', 'closenetting.gs_css', TRUE)
ON CONFLICT (entity, report_type)
DO UPDATE SET
    destination_table = EXCLUDED.destination_table,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

COMMIT;
