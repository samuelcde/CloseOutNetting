AUTO CLOSENETTING - POSTGRESQL - FASE 1
=======================================

Objetivo
--------
Crear el modelo inicial acordado:
- tablas centrales de control/auditoría;
- una tabla física por entidad + tipo de reporte;
- piloto inicial para Goldman Sachs (GS).

Se crean:
- closenetting.ingestion_files
- closenetting.report_schemas
- closenetting.report_table_registry
- closenetting.gs_forward
- closenetting.gs_options
- closenetting.gs_iris
- closenetting.gs_css

Importante
----------
Todavía NO se definen columnas financieras específicas para FORWARD,
OPTIONS, IRIS o CSS. Primero debemos validar muestras reales y el
resultado exacto del parser.

Por ahora:
- source_cells conserva columnas/valores originales y su orden;
- normalized_data queda preparado para la representación normalizada;
- report_schema_id permite detectar/versionar cambios de estructura.

Ejecución
---------
Dentro de la base PostgreSQL seleccionada:

psql -d NOMBRE_BASE -f database/001_closenetting_schema.sql

Para verificar:

psql -d NOMBRE_BASE -f database/002_verify_schema.sql

Si PostgreSQL está en otro host:

psql -h SERVIDOR -p 5432 -U USUARIO -d NOMBRE_BASE -f database/001_closenetting_schema.sql

Siguiente fase
--------------
1. Tomar un XLSX de GS que el parser ya interpreta correctamente.
2. Inspeccionar los nombres de tablas/hojas que devuelve parse_report().
3. Clasificar cada salida como FORWARD / OPTIONS / IRIS / CSS.
4. Crear el mapeo explícito en Python.
5. Implementar app/report_repository.py.
6. Insertar una carga de prueba.
7. Verificar que una segunda ejecución no duplique datos.

Nota
----
Los .xls quedan fuera del alcance actual y deben omitirse desde
allowed_extensions en config.json.
