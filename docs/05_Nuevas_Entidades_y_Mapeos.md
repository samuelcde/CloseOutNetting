# Manual 05 — Incorporación de entidades, formatos y campos

> **Proyecto:** CloseOutNetting  
> **Edición documental:** v1.3 — 10 de noviembre de 2026  
> **Versión de referencia solicitada:** `main`, commit `3b06bb0`  
> **Alcance:** lo descrito como **verificado** procede de fragmentos del código operativo facilitados y de pruebas comunicadas. Existe una copia fuente anterior del proyecto; no se presume que sea idéntica al commit `3b06bb0`. Confirmar código y esquema SQL actuales antes de modificar producción.

## 1. Objetivo

Explicar qué componentes intervenir cuando se recibe (a) un remitente nuevo de una entidad conocida, (b) un formato nuevo de una entidad conocida, (c) una entidad nueva, o (d) columnas adicionales o valores incompatibles. El propósito es incorporar información sin alterar la evidencia original, duplicar operaciones o romper los formatos existentes.

## 2. Identificar primero el tipo de cambio

| Caso | Ejemplo | Alcance probable |
|---|---|---|
| Remitente nuevo, entidad conocida y formato soportado | Un dominio nuevo de SET-ICAP | Correspondencia de `config.json` y prueba de reconocimiento |
| Entidad conocida, formato diferente | SET-ICAP envía columnas nuevas | Mapeo JSON, posible DDL y pruebas de normalización |
| Entidad completamente nueva | `BANCO_NUEVO` | Dominio, motor de normalización, tabla SQL y reglas de mapeo |
| Dato inválido en columna conocida | Texto donde se espera fecha o número | Revisar `validation_issues` y reglas de conversión; no modificar el origen |
| Remitente sin dominio mapeado | Gmail externo | Ruta de `OTROS`, no se crea entidad financiera por defecto |

**Regla de decisión:** reconocer un remitente **no significa** que la estructura de Excel ya esté normalizada.

## 3. Responsabilidad de cada componente

### `config.json` — clasificar remitentes

- Su sección `entities` relaciona dominios de remitente con identificadores de entidad.
- `default_entity` determina la categoría de remitentes no identificados; en el código revisado, el valor predeterminado es `OTROS`.
- También administra parámetros de operación, límites, formatos permitidos y destinatarios de alertas.
- **Modificar cuando:** se incorpora o cambia un dominio reconocido, o se autorizan ajustes operativos.
- **No contiene:** la lógica de conversión financiera ni la definición física de tablas PostgreSQL.
- **Cuidado:** registrar un dominio completo, no la dirección con `@`, pues `app/config.py` valida ese formato.

### `app/config.py` — validar y cargar configuración

- Lee `.env` y `config.json`, comprueba dominios e identificadores y construye la configuración que utiliza `processor.py`.
- En el código revisado la expresión de entidad admite letras mayúsculas, números, guiones y guion bajo dentro de un límite definido; validar el identificador elegido antes del despliegue.
- **Normalmente no hay que editarlo** al incorporar una entidad válida. Solo cambiar si el nuevo requisito exige otra regla de configuración, con pruebas de regresión.

### `app/processor.py` — ejecutar el ciclo de ingesta

- Conecta el flujo de mensajes/adjuntos, clasificación, archivo físico, registro de ingesta, deduplicación y normalización.
- Para entidades desconocidas, el código operativo mostrado conserva el adjunto, registra una incidencia `ENTIDAD DESCONOCIDA`, marca la ingesta `unknown_entity`, actualiza `source_path` y registra SQLite, sin persistencia financiera normalizada para ese adjunto.
- Construye el resumen de ciclo y solicita su envío mediante `EmailNotifier`.
- **Normalmente no hay que editarlo** cuando los nuevos remitentes siguen el flujo genérico. Solo si existe una condición o ramificación específica que impida su procesamiento.
- **No iniciar `main.py --once` como dry-run:** ejecuta un ciclo real.

### `app/business_normalizer.py` — aplicar la normalización

- Contiene rutinas especializadas para algunas entidades y un motor compartido para otras.
- En el código operativo mostrado: `_ADDITIONAL_ENTITIES` incluye `FICC`, `CITI`, `BLADEX`, `BBVA`, `SET-ICAP` y `STANDARD`.
- El motor compartido llama a `_historical_mapping()`, que **lee** `historical_schema_map.json` y comprueba que sus secciones `tables` y `columns` incluyan las entidades requeridas.
- Interpreta encabezados, mapea columnas, identifica mercado, convierte tipos y genera incidencias. Su salida puede conservar `original_message` y `validation_issues`.
- **Modificar cuando:** la nueva entidad necesita ingresar a la ruta compartida, requiere un normalizador especializado o tiene reglas que no pueden resolverse declarativamente.
- **Importante:** no agregar una entidad indiscriminadamente a la lista sin su tabla y configuración válidas; la validación global de `_historical_mapping()` puede fallar si falta un mapeo requerido.

### `historical_schema_map.json` — reglas de correspondencia

- **No es exclusivo del importador histórico.** El `business_normalizer.py` actual también lo consulta para normalizar entidades del motor compartido.
- Secciones identificadas en una copia del proyecto: `tables`, `columns`, `common_column_aliases`, `entity_column_aliases`, `market_overrides`, `ignored_sheet_rules`, `market_normalization`, `morgan_canonical_projections`, `entity_remap`. Confirmar su esquema y significado en el archivo de producción.
- **Modificar cuando:** se agregan alias reconocidos, asociaciones entre encabezados y columnas de destino, una entidad nueva o reglas de mercado justificadas.
- **No borrar** el archivo: podría romper las rutas de normalización automática.
- Las reglas de una entidad especializada pueden coexistir en este JSON sin reemplazar su lógica Python.

### `app/postgres_db.py` — ingestas y acceso PostgreSQL

- Contiene el acceso mediante `psycopg` y funciones de ingesta, estados y trazabilidad de rutas.
- **Solo modificar si** el flujo nuevo requiere una función diferente, hay restricciones efectivas de tablas/entidades o se debe cambiar el modelo persistente.
- La selección segura de nombres de tabla debe proceder de estructuras internas controladas, nunca directamente de un encabezado Excel o un correo.

### PostgreSQL `closenetting` — persistencia

- Separa `ingestion_files` de las tablas de registros financieros `raw_report_*`.
- En la práctica se confirmó la tabla `raw_report_set_icap`. Para una entidad nueva, comprobar DDL, columnas, tipos, claves, índices, restricciones y relación con `ingestion_id` antes de construir su tabla.
- **Modificar cuando:** falta una tabla o columna indispensable para almacenar un campo definido como normalizado. Si el campo debe conservarse solo como evidencia, no necesariamente se agrega una columna SQL.
- Realizar cambios de esquema mediante una migración SQL versionada y previamente ensayada en `closenetting_practica`.

### `historical_import.py` — importación retrospectiva

- Se usa para carga histórica de archivos desde carpeta e inventario. El parámetro `--map` apunta por defecto a `historical_schema_map.json` en la copia inspeccionada.
- **Revisar si** la entidad se importará también mediante cargue masivo o hay restricciones de año, formato o filtro. No asumir cambios obligatorios para un remitente nuevo por Outlook.
- Para el detalle operativo y los controles de `--commit`, consultar el Manual 04.

### `app/parser.py`, `app/report_classifier.py`, `app/db.py`, `app/notifier.py`

- `parser.py`: extrae tablas y celdas del libro; modificar solo si el formato físico no puede leerse con las capacidades existentes.
- `report_classifier.py`: interpreta la estructura/tipo de reporte; modificar solo si las reglas actuales no reconocen un mercado o estructura legítima.
- `db.py`: conserva el estado de adjuntos en SQLite, incluido su estado y huellas; normalmente no necesita cambios al agregar entidades.
- `notifier.py`: genera y transmite mensajes SMTP; comprobar las condiciones de notificación, sin asumir alerta individual por cada columna nueva.

## 4. Relación entre archivos: de correo a base de datos

```text
Correo con Excel
    |
    v
config.json + app/config.py   -> dominio -> entidad (o OTROS)
    |
    v
app/processor.py             -> descarga, ruta, hash, ingesta, duplicados
    |                    \
    |                     -> app/db.py -> SQLite (adjuntos procesados)
    v
app/parser.py                -> hojas, encabezados, celdas originales
    |
    v
app/report_classifier.py    -> tipo/estructura de reporte
    |
    v
app/business_normalizer.py  -> reglas y conversiones
    ^
    |
historical_schema_map.json  -> tablas/alias/mercados (motor compartido)
    |
    v
PostgreSQL closenetting      -> ingestion_files + raw_report_ENTIDAD
    |
    v
app/notifier.py             -> resumen SMTP según eventos del procesador
```

Este esquema es conceptual: la secuencia exacta y el manejo de errores se verificarán en la revisión de código del commit desplegado.

## 5. Incorporación de una entidad nueva: procedimiento

**Paso 0 — Definir evidencia de entrada.** Solicitar un Excel representativo, remitente/dominio, hojas utilizadas, periodicidad, semántica de columnas, mercado, campos obligatorios y resultados esperados. Guardar el Excel original sin modificar y calcular SHA-256; utilizar una copia para pruebas.

**Paso 1 — Inspeccionar el proyecto sin ejecutarlo.** En la versión de trabajo, comprobar: dominio en `config.json`; ruta de entidad en `business_normalizer.py`; secciones JSON; tabla existente en PostgreSQL; posibles excepciones en `processor.py` o `postgres_db.py`. Identificar si basta con cambiar dominio o si requiere DDL/normalización.

**Paso 2 — Diseñar matriz de mapeo.** Preparar una tabla por cada columna relevante:

| Encabezado Excel | Ejemplo ficticio | Columna SQL deseada | Tipo SQL | Regla | Validación |
|---|---|---|---|---|---|
| `Trade Date` | `2026-11-10` | Por definir | `date` si aplica | Fechas inequívocas | Rechazar/advertir ambigüedad |
| `Notional` | `1250000` | Por definir | `numeric` si aplica | Sin redondeo silencioso | Registrar error de tipo |
| `New Field` | `ABC-01` | Por definir | Por definir | Decide negocio | Evidencia en original |

Los nombres SQL son ilustrativos, **no representan columnas existentes**.

**Paso 3 — Definir el modelo.** Si es entidad nueva, crear o adaptar una tabla siguiendo el DDL real de las tablas `raw_report_*`. Si es nueva columna, elegir tipo y restricciones según los datos. Versionar el DDL, prever rollback y ensayar con datos de prueba.

**Paso 4 — Definir reglas JSON.** Incorporar identificador de entidad en las secciones pertinentes (`tables`, `columns`, alias y mercado) con el formato exacto del JSON existente. No cambiar alias generales si el comportamiento solo corresponde a una entidad; preferir el ámbito específico. Comprobar que el JSON cargue correctamente.

**Paso 5 — Habilitar motor.** Confirmar que `business_normalizer.py` soporte la entidad. Si se utiliza la ruta compartida, verificar registro de la entidad y contrato de mapeos. Si el formato necesita reglas propias, escribirlas con casos de prueba antes de modificar producción.

**Paso 6 — Configurar remitente.** Actualizar el mapa `entities` en `config.json` con el dominio y código aprobados; evitar modificar `default_entity` para forzar el reconocimiento.

**Paso 7 — Ensayar sin servicios externos.** Utilizar copia del proyecto, `.env` apuntando a `closenetting_practica`, SQLite independiente, descargas locales y Excel de prueba. No iniciar Outlook ni SMTP. Ejecutar parser/normalizador controladamente únicamente después de comprobar que los métodos seleccionados no usan conexiones operativas.

**Paso 8 — Validar resultados.** Verificar tablas, conteos, vínculo de ingesta, valores originales, advertencias y deduplicación; comparar cada campo esperado con el Excel, sin transformar datos por conveniencia.

**Paso 9 — Desplegar.** Registrar cambios en Git, ejecutar migración SQL, instalar código/configuración en una ventana aprobada, realizar un único ciclo real coordinado y revisar logs, persistencia y correo de resumen. No ejecutar dos instancias concurrentes.

**Paso 10 — Documentar.** Registrar nombre de entidad, versión/commit, muestra de prueba, matriz, DDL, estados, evidencias, responsable y procedimiento de reversión.

## 6. Cómo detectar campos nuevos y errores de conversión

En el código operativo revisado, el motor compartido:

- Normaliza el encabezado y busca correspondencias en alias comunes y por entidad.
- Cuando la columna de destino no está permitida, agrega `UNMAPPED_HISTORICAL_COLUMN` si hay valor no vacío.
- Si aparecen encabezados duplicados con valores diferentes, agrega `DUPLICATE_HEADER_CONFLICT`.
- Si dos fuentes alimentan el mismo destino con datos incompatibles, agrega `COLUMN_ALIAS_CONFLICT`.
- Si no existe columna SQL esperada, agrega `DESTINATION_COLUMN_MISSING`.
- Los errores de conversión y mercados no clasificados también pueden quedar registrados como incidencias.
- Inserta las celdas originales en `original_message` y las advertencias acumuladas en `validation_issues` cuando ese registro efectivamente alcanza la persistencia.

**No asumir que `validation_issues` significa fallo total.** Una fila puede guardarse con campos normalizados válidos y advertencias sobre otros campos. Tampoco significa automáticamente que SMTP envíe una alerta individual: comprobar cómo el procesador transfiere esas incidencias al resumen.

### Consultas de comprobación (solo lectura)

```sql
-- Comprobar estructura de SET-ICAP (adaptar entidad de interés)
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'closenetting'
  AND table_name = 'raw_report_set_icap'
ORDER BY ordinal_position;

-- Buscar registros de una ingesta específica ya identificada
SELECT ingestion_id, source_sheet_name, market_type,
       original_message, validation_issues
FROM closenetting.raw_report_set_icap
WHERE ingestion_id = 12345; -- Reemplazar por el ID real de prueba
```

**Precaución:** estas consultas presuponen las columnas observadas en la versión inspeccionada; confirmar el DDL de la instalación activa.

## 7. Ejemplos de incorporación

### A. Gmail no reconocido

- No es alta de entidad nueva; sirve para validar la ruta de desconocidos.
- Se espera carpeta `OTROS` en el año automatizado, `unknown_entity` en `ingestion_files` y SQLite, sin inserción financiera normalizada.
- El resumen SMTP recoge el evento según flujo revisado; confirmar recepción y detalles del correo en una prueba autorizada.

### B. SET-ICAP con columna extra

- SET-ICAP ya figura en la ruta compartida y tiene tabla de destino.
- Preparar un Excel con encabezados válidos y **una** columna adicional no vacía.
- Revisar `original_message` y advertencias; no asumir que se deba alterar el esquema por todo campo extra.
- Si negocio requiere normalizar esa columna, acordar tipo SQL, alias y reglas, luego ensayar nuevamente.

### C. SET-ICAP con formato conocido

- Mantener hojas/encabezados, pero usar datos ficticios diferentes y marcados como prueba.
- Comprobar que se guarde la operación y que los valores correspondan a lo esperado.
- Evitar datos ficticios en la base operativa salvo autorización y estrategia de limpieza posterior.

### D. Entidad realmente nueva

- Confirmar identificador, dominio, tabla y motor de normalización.
- Registrar mapping JSON y DDL según esquema real; los ejemplos de este manual no constituyen código SQL para producción.
- Validar aislamiento de base, SQLite y ruta de descarga antes de prueba real.

## 8. Reversión y riesgos

- Para revertir configuración/código: conservar versión anterior en Git y archivos de configuración respaldados; detener la instancia antes del cambio.
- Para revertir DDL: diseñar migración inversa según datos realmente insertados; **no borrar tablas/columnas automáticamente** si contienen información posterior al despliegue.
- Para revertir mensajes procesados: coordinar PostgreSQL, SQLite y archivos físicos; no restaurar SQLite antiguo aislado tras nuevas ingestas porque se podrían reprocesar adjuntos.
- Si falla la normalización, preservar Excel original, `source_path`, hash, IDs y logs; no editar manualmente valores financieros sin registro de decisión.

## 9. Lista de verificación para publicar una entidad

- [ ] Dominio/identificador autorizado y sin colisión con entidades existentes.
- [ ] Excel de muestra y SHA-256 conservados.
- [ ] Matriz de campos aprobada por negocio.
- [ ] Tabla y columnas SQL existentes o migración DDL validada.
- [ ] Motor de normalización admite la entidad.
- [ ] JSON válido y mapeos completos.
- [ ] Clasificación y parser comprobados con muestra real.
- [ ] Originales y `validation_issues` verificados.
- [ ] Duplicados y estados controlados.
- [ ] Prueba en entorno aislado satisfactoria.
- [ ] Primer ciclo real coordinado y evidencia conservada.
- [ ] Manuales y bitácora actualizados.

## 10. Qué falta confirmar en el código de producción

Antes de utilizar este manual como instrucción de despliegue, revisar en el commit `3b06bb0` los puntos exactos de despacho `save_report_data`, mecanismos de nombres de tabla, disparadores de `structure_changes`, manejo de errores y los formatos reales de reglas JSON. Esos detalles no se deben dar por verificados únicamente a partir de la copia fuente anterior.
