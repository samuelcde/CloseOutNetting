SELECT
    r.market_type,
    COUNT(*) AS operaciones,
    COUNT(DISTINCT r.ingestion_id) AS archivos
FROM closenetting.raw_report_smbc r
JOIN closenetting.ingestion_files i
    ON i.id = r.ingestion_id
WHERE i.entity = 'SMBC'
  AND i.received_at IS NULL
GROUP BY r.market_type
ORDER BY r.market_type;