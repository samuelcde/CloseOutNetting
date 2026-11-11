# Manual 07 — Pruebas, validaciones y evidencias

> **Sistema:** SET ICAP — Auto CloseNetting  
> **Edición:** ampliada — 11 de noviembre de 2026 · documentación v1.6  
> **Referencia de proyecto:** rama `main`, commit de limpieza informado `3b06bb0`  
> **Estado:** combina resultados **VALIDADOS**, comportamientos **OBSERVADOS EN CÓDIGO** y casos **PENDIENTES**. La copia del código revisada no garantiza identidad con el último commit; confirmar diferencias antes de ejecutar pruebas con efectos externos.  
> **Confidencialidad:** no anexar a Git correos reales, contraseñas, `.env`, extractos financieros, identificadores sensibles ni backups.

## 1. Objetivo y criterios de aceptación

Este manual sirve para probar una instalación nueva, una modificación del parser, normalizador, mapeos o reglas de entidades, una restauración de datos y un despliegue. Busca verificar simultáneamente **integridad del archivo original, clasificación, trazabilidad, persistencia financiera, idempotencia y notificaciones**, sin confundir un comando que compila correctamente con una validación funcional.

Una prueba se cierra como **APROBADA** únicamente con: entrada conocida y autorizada; resultado esperado escrito antes; ejecución controlada; evidencia de salida; comprobación de que no se generaron cambios imprevistos; y responsable/fecha. **OBSERVADA** significa identificada en el código, no necesariamente ensayada en la versión publicada. **PENDIENTE** significa que falta ejecución o evidencia suficiente.

## 2. Entornos y riesgos de ejecución

| Entorno | Finalidad | Protección imprescindible |
|---|---|---|
| Proyecto principal `auto-closeoutnetting` | Operación real del proceso | No iniciar otro ciclo ni editar configuración durante un ciclo activo |
| Copia `auto-closeoutnetting-practica` | Pruebas de instalación, base y código | `.env` independiente; `DATABASE_NAME=closenetting_practica`; directorio de descargas local |
| Datos sintéticos controlados | Parser, encabezados, conflictos y duplicados | No usar datos reales innecesariamente; respaldar antes de probar escritura |

**`python main.py --once` NO es un dry-run:** puede consultar Outlook, descargar archivos, escribir datos y enviar SMTP. Algunos `test_*.py` pueden tener efectos reales; revisar el cuerpo del archivo antes de ejecutarlo. El importador histórico sin `--commit` tiene simulación en la copia inspeccionada, pero verificar `--help` en la versión desplegada antes de usarlo. No ejecutar pruebas de correo y BD con el mismo buzón y estado que producción salvo ventana autorizada.

### 2.1 Preparación recomendada

1. Registrar commit, fecha, equipo, cuenta Windows y base de datos objetivo (sin contraseñas).
2. Verificar que ninguna tarea o instancia operativa apunte por accidente al entorno de práctica.
3. Si habrá escritura de prueba, crear backups de PostgreSQL y SQLite y anotar sus hashes.
4. Capturar conteos **antes**; usar `ingestion_id`, SHA-256 del archivo y rango de tiempo para aislar el caso.
5. Definir condición de éxito, condición de rechazo y plan de limpieza/reversión **antes** de correr la prueba.

## 3. Comprobaciones de bajo riesgo (sin correo ni escritura financiera)

Ejecutar desde la raíz del **proyecto de práctica**, con su entorno virtual:

```powershell
cd C:\Users\scardenas\auto-closeoutnetting-practica
.\venv\Scripts\python.exe -m pip check
.\venv\Scripts\python.exe -m compileall -q app main.py historical_import.py
```

Ambos comandos comprueban dependencias y sintaxis; **no** prueban exactitud de resultados financieros. Consultar el commit sin alterarlo:

```powershell
git rev-parse --short HEAD
git status --short
```

### 3.1 PostgreSQL: verificar base e ingestas

Antes de cualquier consulta, confirmar que se está conectado a `closenetting_practica` o a la base expresamente autorizada:

```sql
SELECT current_database() AS base, current_user AS usuario;

SELECT COUNT(*) AS ingestas,
       COUNT(source_path) AS con_ruta,
       COUNT(*) FILTER (WHERE source_path IS NULL) AS sin_ruta
FROM closenetting.ingestion_files;

SELECT entity, status, COUNT(*) AS cantidad
FROM closenetting.ingestion_files
GROUP BY entity, status
ORDER BY entity, status;
```

**Referencia histórica de práctica (09/11/2026):** 5.718 ingestas, 5.649 con ruta y 69 sin ruta. Esta es una **fotografía** de esa restauración, no una condición permanente de producción.

### 3.2 SQLite: integridad sin actualizaciones

```powershell
@'
from pathlib import Path
import sqlite3
p = Path('state/automation_state.db').resolve()
with sqlite3.connect(p.as_uri() + '?mode=ro', uri=True) as db:
    print('integrity_check:', db.execute('PRAGMA integrity_check').fetchone()[0])
    print('adjuntos:', db.execute('SELECT COUNT(*) FROM processed_attachments').fetchone()[0])
    print('ejecuciones:', db.execute('SELECT COUNT(*) FROM execution_history').fetchone()[0])
'@ | .\venv\Scripts\python.exe -
```

**Referencia histórica:** `integrity_check = ok` y 138 adjuntos tras la restauración de práctica. El control demuestra legibilidad del archivo, no sincronía perfecta con Outlook ni con PostgreSQL.

### 3.3 Verificar tablas de detalle y columnas

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'closenetting'
  AND table_name LIKE 'raw_report_%'
ORDER BY table_name;

SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'closenetting'
  AND table_name = 'raw_report_set_icap'
ORDER BY ordinal_position;
```

Estas consultas ayudan a detectar discrepancias entre `historical_schema_map.json`, el normalizador y el esquema restaurado. Evitar modificar columnas con una prueba de validación.

## 4. Matriz de pruebas funcionales

| ID | Escenario | Resultado esperado / evidencia | Estado documental |
|---|---|---|---|
| T01 | Remitente Gmail sin dominio registrado | Adjunto en `OTROS`, ingesta `unknown_entity`, ruta real, marca SQLite; sin filas financieras normalizadas | Demostración informada; evidencias detalladas pendientes |
| T02 | SET-ICAP con columna con valor no mapeada | Evidencia en `original_message` y `validation_issues` con `UNMAPPED_HISTORICAL_COLUMN`, cuando hay fila persistible | Demostración informada; corroborar por fila |
| T03 | SET-ICAP compatible con mapeo | Ingesta y filas correctas en `raw_report_set_icap`, sin incidencia inesperada | Demostración informada; cotejo de valores pendiente |
| T04 | Archivo idéntico repetido | Aplicar regla de deduplicación vigente; no duplicar filas financieras | Pendiente de prueba controlada |
| T05 | Mismo Excel con otro nombre | Política de hash y procedencia expresamente verificada | Pendiente |
| T06 | Encabezados equivalentes con valores diferentes | `DUPLICATE_HEADER_CONFLICT` sin perder valores originales | Pendiente |
| T07 | Dos alias hacia el mismo campo con valores incompatibles | `COLUMN_ALIAS_CONFLICT` | Pendiente |
| T08 | Columna mapeada ausente en PostgreSQL | `DESTINATION_COLUMN_MISSING`, sin escritura parcial silenciosa | Pendiente |
| T09 | Mercado no reconocido | `UNCLASSIFIED_MARKET` e investigación de hoja/regla | Pendiente |
| T10 | Fecha o valor numérico no convertible | Problema trazable; el original no debe ser sobrescrito | Pendiente |
| T11 | Error de conexión PostgreSQL | Error registrado, estado e idempotencia revisados tras recuperarse | Pendiente |
| T12 | Error SMTP | Evidencia de envío fallido, sin presumir fallo de persistencia financiera | Pendiente |
| T13 | Falta de acceso a UNC | Diagnóstico de permisos bajo cuenta ejecutora; no crear rutas falsas | Pendiente |
| T14 | Instalación práctica restaurada | Conexiones y conteos correctos, SQLite íntegro, sin Graph/SMTP | **VALIDADO 09/11/2026** |
| T15 | Migración y reversión después de nuevas ingestas | Consistencia entre archivos, PostgreSQL y SQLite sin reconsumo | Pendiente; requiere ventana y plan de conciliación |

Los casos T01–T03 corresponden a pruebas descritas en la conversación. No deben marcarse como **aprobadas con evidencia completa** hasta adjuntar identificador de ingesta, consultas y logs/correo correspondientes.

## 5. Tres demostraciones operativas: guion reproducible

### 5.1 Caso T01 — Gmail / entidad desconocida

**Preparación:** remitente de prueba no reconocido por `config.json`, adjunto permitido y ventana aprobada; verificar `default_entity`. No incluir Gmail como dominio de una entidad financiera solo para forzar el resultado.

**Comprobar:**
1. Localizar `Archivo recibido en OTROS` u otra traza equivalente en el log del ciclo.
2. Confirmar el archivo físico en la subcarpeta `OTROS` dentro de `DOWNLOAD_DIR` y calcular su hash.
3. Confirmar `status='unknown_entity'` y `source_path` real en `closenetting.ingestion_files`.
4. Revisar estado del adjunto en SQLite.
5. Asegurarse de que no se generaron filas de operaciones normalizadas para ese archivo.
6. Examinar resumen SMTP si se emitió; no exigir un correo individual que el flujo no garantice.

**Rechazar** si el archivo se pierde, si recibe una entidad financiera incorrecta o si `source_path` apunta a una ruta inexistente.

### 5.2 Caso T02 — SET-ICAP con encabezado nuevo

**Preparación:** Excel de prueba compatible con SET-ICAP, agregar un encabezado nuevo **con valor** y no crear su alias/columna de destino previamente. Respaldar el archivo original y calcular hash.

**Comprobar:**

```sql
SELECT ingestion_id, source_sheet_name, market_type,
       original_message, validation_issues
FROM closenetting.raw_report_set_icap
WHERE ingestion_id = 12345; -- Sustituir por el ID de esta prueba
```

Verificar preservación de la columna de origen y código de advertencia, si el procesamiento produjo una fila. Distinguir una advertencia en `validation_issues` de una alerta SMTP: **no son equivalentes**. La evidencia de origen debe permanecer, aunque la columna no tenga aún destino normalizado.

**Rechazar** si el valor desaparece sin trazabilidad, si se asigna a un destino financiero incorrecto o si hay persistencia parcial no investigada.

### 5.3 Caso T03 — SET-ICAP compatible

**Preparación:** archivo cuya estructura, mercado y encabezados tengan mapeo reconocido; contar filas útiles y conocer valores de muestra de manera autorizada.

**Comprobar:**
1. La ingesta referencia el archivo correcto y su hash.
2. Las filas esperadas aparecen bajo su `ingestion_id`.
3. Mercado, fechas y valores clave coinciden con el Excel **y** sus reglas de transformación.
4. `validation_issues` no presenta códigos inesperados.
5. No aparecen filas extra por repetición del mismo adjunto.

**Rechazar** si solo coinciden los conteos, pero hay campos financieros divergentes: la prueba debe comparar valores, no únicamente cantidades.

## 6. Verificación de normalización y regresión

Las entidades con normalización especializada (`GS`, `SMBC`, `MORGAN_STANLEY`) y las adicionales (`FICC`, `CITI`, `BLADEX`, `BBVA`, `SET-ICAP`, `STANDARD`) no deben tratarse como si usaran todas el mismo conjunto de reglas. En la copia inspeccionada `app/business_normalizer.py` consulta `historical_schema_map.json` en flujos del procesamiento automático; conservar el mapa en cada instalación.

Cuando se cambia un alias, mercado, tipo de columna o parser:
1. Seleccionar un Excel de referencia **por cada entidad afectada**, con valores de salida esperados.
2. Comparar hash, hojas leídas, cantidad de filas, `source_sheet_name`, `market_type` y campos financieros.
3. Validar `original_message` y `validation_issues` para detectar pérdida o conversión impropia.
4. Probar un caso negativo: columna desconocida, hoja incorrecta o valor inválido.
5. Volver a ejecutar con las mismas entradas en base de prueba para verificar ausencia de duplicación.
6. Registrar diferencias justificadas y obtener aceptación del responsable de negocio.

**No asumir** que una fila con `validation_issues=[]` prueba exactitud económica: hace falta cotejar el contenido contra el archivo y las reglas de negocio.

## 7. Tests existentes: qué ejecutar y qué no

En la limpieza de `3b06bb0` se retiraron tres tests antiguos incompatibles: `test_postgres_classifier.py`, `test_postgres_raw.py` y `test_smbc_forward.py`. Los cuatro archivos de pruebas que permanecieron requieren revisión individual:

| Archivo | Cobertura prevista | Riesgo antes de ejecutar |
|---|---|---|
| `test_parser.py` | Lectura de archivos Excel | Verificar entradas y ruta de datos; sin asumir seguridad por el nombre |
| `test_classifier.py` | Clasificación de hojas/mercados | Revisar archivos de prueba y dependencias |
| `test_new_storage.py` | Escritura y normalización de datos | **Podría escribir en PostgreSQL**; aislar base antes |
| `test_notifier.py` | Construcción/envío de notificaciones | **Podría enviar SMTP real**; usar sustitutos de prueba o servidor aislado |

Estos archivos usan una función `main()` y no constituyen necesariamente una batería reconocida por `unittest discover`. La ausencia de tests descubiertos, por sí sola, **no** constituye aprobación ni fallo del programa. No ejecutar pruebas de integración con credenciales productivas sin autorización.

## 8. Validación de restauración y migración (caso T14)

**Evidencia VALIDADA:** ensayo de restauración del 9 de noviembre de 2026, Python 3.14.7, PostgreSQL 16.15, `closenetting_practica`, SQLite respaldado en archivo independiente; diagnóstico terminado con `DIAGNOSTICO: SATISFACTORIO`.

| Indicador de referencia | Valor observado |
|---|---:|
| Ingestas PostgreSQL | 5.718 |
| Ingestas con `source_path` | 5.649 |
| Ingestas sin `source_path` | 69 |
| Adjuntos procesados en SQLite | 138 |
| Integridad SQLite | `ok` |
| Consulta real de Outlook/Graph | **No ejecutada** |
| Envío SMTP | **No ejecutado** |
| Modificaciones a BD durante diagnóstico | **Ninguna** |

Se confirmó restauración y conectividad del entorno de práctica, **no** el funcionamiento integral de Graph/SMTP ni una migración productiva completada. Los 69 `source_path=NULL` tienen historial de ambigüedad/falta de correspondencia; **no inventar rutas** ni asumir corrupción por el simple NULL.

## 9. Evidencias y acta de prueba

Guardar la bitácora y las capturas en un repositorio **restringido** o carpeta de evidencias fuera del Git público, preferiblemente separada por fecha y caso. A cada caso asignar:

```text
ID de prueba:
Fecha y hora (America/Bogota):
Entorno / equipo / commit:
Responsable:
Objetivo / entrada:
Hash SHA-256 (si aplica):
Base de datos objetivo (sin contraseñas):
ID de ingesta / nombre de entidad:
Resultado esperado:
Resultado observado:
Evidencias (ruta segura / consulta / referencia de log):
Impacto PostgreSQL / SQLite / archivos / SMTP:
Estado: APROBADO | RECHAZADO | PENDIENTE
Acción correctiva y responsable:
```

No copiar la totalidad de `original_message` o adjuntos a informes de distribución amplia; documentar identificadores de evidencia protegida y comparaciones puntuales.

## 10. Cierre de una versión y autorización del despliegue

La liberación se considera preparada cuando: (a) dependencias y sintaxis están bien; (b) pruebas de regresión para entidades afectadas pasan; (c) PostgreSQL/SQLite son coherentes; (d) la configuración activa identifica correctamente el entorno; (e) existe respaldo recuperable y plan de reversión; (f) los resultados y riesgos están aprobados. Graph y SMTP solo pueden declararse **validados** después de una prueba de integración autorizada y documentada.

**Siguiente tarea documental:** revisión cruzada de los siete manuales contra el código desplegado, actualización de comandos/rutas, matriz de versiones y publicación de una edición estable. No ejecutar pruebas productivas únicamente para completar casillas de documentación.
