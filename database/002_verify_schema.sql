SELECT
    table_schema,
    table_name
FROM information_schema.tables
WHERE table_schema = 'closenetting'
ORDER BY table_name;

SELECT
    entity,
    report_type,
    destination_table,
    is_active
FROM closenetting.report_table_registry
ORDER BY entity, report_type;

SELECT
    indexname,
    tablename
FROM pg_indexes
WHERE schemaname = 'closenetting'
ORDER BY tablename, indexname;
