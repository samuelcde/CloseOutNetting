> **Proyecto:** CloseOutNetting  
> **Manual:** 03 — Arquitectura y componentes | **Versión documental:** 1.1 — 10/11/2026  
> **Referencia operativa indicada por el usuario:** rama `main`, commit `3b06bb0`.  
> **Alcance:** combina resultados validados en la instalación operativa y una copia de código del 27/10/2026. Los nombres de funciones citados de esa copia requieren contraste con el repositorio actual antes de modificar código.  
> **Seguridad:** nunca publicar `.env`, credenciales ni archivos financieros.

# Manual 03 — Arquitectura y componentes

## 1. ¿Qué hace CloseOutNetting?
CloseOutNetting revisa un buzón de Outlook a través de Microsoft Graph, identifica la entidad emisora, descarga adjuntos de Excel admitidos, interpreta su estructura y registra tanto el seguimiento de la ingesta como los datos financieros. Mantiene un historial local en SQLite para no procesar dos veces los mismos adjuntos y utiliza PostgreSQL para la información de negocio. Un resumen SMTP comunica resultados e incidencias.

**Dos ejecuciones distintas:** `main.py` coordina la automatización recurrente; `historical_import.py` sirve para cargar archivos históricos. Ambos comparten partes de la interpretación y de la persistencia, pero no se deben confundir.

## 2. Flujo general

```text
Buzón Outlook
  → app/auth.py / app/graph_api.py (acceso Graph)
  → app/entities.py + config.json (remitente → entidad / OTROS)
  → app/processor.py (orquestación y controles)
     ├→ app/security.py (nombre, ruta y almacenamiento físico)
     ├→ app/db.py → SQLite (estado del adjunto, duplicados)
     ├→ app/postgres_db.py → ingestion_files (trazabilidad)
     └→ app/parser.py → app/report_classifier.py
                          → app/business_normalizer.py
                             ├→ historical_schema_map.json
                             └→ PostgreSQL raw_report_* 
  → app/notifier.py (resumen e incidencias por SMTP)
```

Es un diagrama conceptual: la secuencia exacta de llamadas y las condiciones de rechazo se determinan en `app/processor.py`.

## 3. Archivos de arranque y configuración

### `main.py` — punto de entrada
Inicia el proceso, prepara directorios y logs, valida la configuración, inicializa SQLite y obtiene un bloqueo local de instancia. Durante los días hábiles invoca `process_cycle()` y después espera el intervalo configurado. **`--once` ejecuta un ciclo real**, no es un modo de simulación; nunca usarlo como simple prueba de instalación con el buzón operativo.

**Relación:** usa `app/config.py`, `app/db.py`, `app/instance_lock.py`, `app/logging_setup.py` y `app/processor.py`.

### `iniciar_closenetting.bat` — arranque en Windows
Es el script utilizado para facilitar el inicio desde Windows; en la copia examinada resuelve el directorio donde está instalado, emplea el entorno virtual y llama a `main.py`. En la migración hay que verificar el BAT real y la cuenta o tarea programada que lo invoca; no copiar rutas de usuario sin comprobarlas.

**Relación:** entorno `venv`, `main.py` y programación de tareas de Windows.

### `app/config.py` — configuración y rutas internas
Carga credenciales y directorios desde `.env`, parámetros de procesamiento desde `config.json` y valida nombres de entidad y otros límites. Calcula `BASE_DIR`, `STATE_DIR`, `LOG_DIR` y `STATE_DB` relativos a la instalación. `load_settings()` y `load_operational_config()` permiten que el resto del proyecto use configuraciones validadas.

**Relación:** lo consultan el arranque, el procesador y las pruebas. Se modifica cuando cambia el *modelo* de configuración, no simplemente por añadir un dominio válido en `config.json`.

### `.env` — parámetros sensibles del entorno
Contiene credenciales de Microsoft Graph y SMTP, conexión PostgreSQL (`DATABASE_*`), destinatario de buzón (`MAILBOX_USER`) y carpeta de almacenamiento (`DOWNLOAD_DIR`). No debe incluirse en Git ni en los manuales. En otra máquina normalmente se **copia de forma segura y se ajusta**, especialmente host, credenciales y rutas.

**Relación:** leído por `app/config.py` y por `app/postgres_db.py`; la contraseña no debe aparecer en evidencias.

### `config.json` — reglas operativas
Configura intervalo, zona horaria, límites de adjuntos, extensiones admitidas, destinatarios del resumen y asociación `dominio → entidad`. `default_entity` es `OTROS` si no se establece otro valor; un Gmail no reconocido se clasifica mediante esa regla general. Agregar un dominio aquí no crea por sí solo su tabla ni su normalizador.

**Relación:** `app/config.py` lo valida; `app/entities.py`/`app/processor.py` aplican la clasificación.

### `requirements.txt` — dependencias reproducibles
Lista los paquetes necesarios para reconstruir `venv` en otra instalación. Debe usarse con `python -m pip install -r requirements.txt` y comprobarse con `pip check`; no se recomienda copiar el directorio `venv` entre máquinas.

### `app/__init__.py` y `.gitignore`
`app/__init__.py` identifica `app` como paquete Python; normalmente no contiene lógica de negocio. `.gitignore` establece qué archivos locales o sensibles no deben versionarse; conviene revisar que excluya `.env`, `venv`, bases locales y resultados temporales.

## 4. Comunicación, identificación y descarga

### `app/auth.py` — autenticación con Microsoft Graph
Proporciona el token para que el programa pueda acceder al buzón autorizado. En la copia fuente la función principal es `get_access_token()`. Los fallos aquí impiden obtener mensajes, pero no significan un error de normalización financiera.

**Relación:** las operaciones de Graph necesitan su autenticación; recibe secretos desde la configuración.

### `app/graph_api.py` — mensajes y adjuntos
Encapsula las solicitudes a Microsoft Graph, lectura de mensajes, recuperación de adjuntos y ventanas de fecha. En la copia existen `GraphClient` y utilidades de ventanas de días hábiles. Es la capa de comunicación, no donde se definen los campos financieros de los Excel.

**Relación:** lo usa `app/processor.py`; depende de la autenticación y de los límites operativos.

### `app/entities.py` — identificación del remitente
Extrae la dirección del remitente y determina la entidad a partir del dominio configurado. En la copia fuente aparecen `get_sender_address()` y `resolve_entity()`. Si el dominio no coincide, se utiliza la entidad por defecto; esto permite el caso de prueba con Gmail y `OTROS`.

**Relación:** consulta el mapa de `config.json` a través de la configuración cargada.

### `app/security.py` — nombres y rutas seguras
Sanitiza nombres de adjuntos y construye destinos evitando rutas inválidas o escapadas del directorio permitido. `build_safe_destination()` interviene en la estructura `AÑO(a)/ENTIDAD/archivo`, mientras las utilidades para colisiones impiden sobrescrituras accidentales.

**Relación:** llamado por el procesador antes de conservar el Excel. No cambia la información de sus celdas.

### `app/processor.py` — coordinador principal
Es el núcleo de cada ciclo: recupera correos, identifica entidad, aplica límites, descarga, registra intentos en PostgreSQL y SQLite, evita duplicados, invoca parsing/normalización y recopila incidencias. Para un adjunto desconocido conserva el archivo en `OTROS`, registra `unknown_entity` y no lo inserta como operación financiera. Tras guardar físicamente un archivo, intenta registrar su ruta definitiva en `source_path`.

**Relación:** conecta prácticamente todas las capas, pero **no** es donde conviene añadir cada alias de columna. Conviene revisarlo al modificar reglas de flujo, no necesariamente al incorporar una entidad.

## 5. Lectura, clasificación y normalización financiera

### `app/parser.py` — interpretación física del Excel
Lee hojas, encabezados y valores en una representación estructurada (`ParsedReport`, `ParsedTable` en la copia). Maneja formatos admitidos, límites y archivos que requieren preparación. **No debe deducir o corregir silenciosamente** valores de negocio.

**Relación:** entrega tablas a la clasificación y al normalizador. Se modifica cuando aparece un *formato de archivo* nuevo o una forma de lectura no soportada.

### `app/report_classifier.py` — clasificación de estructuras
Examina señales como nombres de hojas y encabezados para identificar perfiles o tipos de reporte. Incluye utilidades como `classify_table()`, normalización de nombres y huellas de estructura en la copia revisada. Su objetivo es ayudar a interpretar **qué clase de reporte** contiene cada hoja, independientemente de quién envió el correo.

**Relación:** consume tablas del parser y entrega clasificación/observaciones al normalizador.

### `app/business_normalizer.py` — reglas de negocio y persistencia
Convierte datos interpretados a las columnas y tipos esperados para cada entidad. Algunas entidades tienen lógica especializada (GS, SMBC, MORGAN_STANLEY) y otras utilizan un motor común (FICC, CITI, BLADEX, BBVA, SET-ICAP, STANDARD). El motor compartido consulta `historical_schema_map.json` para resolver alias, tablas y otras reglas; detecta columnas no mapeadas y problemas de tipos, y registra evidencia sin reescribir el original.

**Relación:** recibe `ParsedReport` y clasificación; consulta mapeos; utiliza la conexión PostgreSQL para almacenar `raw_report_*`.

### `historical_schema_map.json` — mapeos compartidos
Es una **configuración operativa**, aunque su nombre aluda a histórico: contiene equivalencias de columnas, tablas y reglas especiales que también usa `business_normalizer.py` al procesar ciertos correos actuales. Una columna con datos sin destino válido puede producir `UNMAPPED_HISTORICAL_COLUMN`; otra puede quedar sin guardar por incompatibilidad del esquema o tipo.

**Relación:** lo usan `historical_import.py` y la normalización de entidades adicionales. **No borrarlo** ni asumir que solo afecta el cargue masivo.

### `app/fingerprint.py` — huella lógica del contenido
Construye una representación de contenido para comparar reportes más allá del nombre del archivo o de su hash binario. Permite detectar adjuntos cuyo contenido lógico coincide aunque cambie su presentación física, según los controles del procesador.

**Relación:** apoya la deduplicación; no sustituye el registro de origen ni la trazabilidad de `ingestion_files`.

## 6. Bases de datos, notificación y soporte

### `app/postgres_db.py` — acceso a PostgreSQL
Crea conexiones mediante `psycopg`, lee `DATABASE_*` y contiene funciones para registrar intentos, consultar duplicados, actualizar estados y establecer `source_path`. `ingestion_files` conserva trazabilidad del archivo; las operaciones se insertan en tablas `raw_report_*` a través del motor de persistencia.

**Relación:** utilizado por el procesador, el normalizador y utilidades de carga. Cambiarlo solo cuando lo requiere el contrato de persistencia.

### `app/db.py` — estado local SQLite
Gestiona `state/automation_state.db`, especialmente `processed_attachments`: IDs de correo y adjunto, hashes, ruta local, estado y referencias a duplicados. Permite que otra ejecución sepa qué adjuntos ya se revisaron. **No es la base financiera:** esa corresponde a PostgreSQL.

**Relación:** el procesador la consulta y actualiza por cada adjunto. Durante una migración real debe respaldarse coordinadamente con PostgreSQL.

### `app/notifier.py` — correo de resumen y alertas
Construye y envía notificaciones mediante SMTP a los destinatarios configurados. El resumen incluye contadores e incidencias recopiladas durante el ciclo. Una observación en `validation_issues` **no implica automáticamente** un correo individual: depende de cómo el procesador la incorpore al resumen.

**Relación:** recibe resultados de `app/processor.py`, así como la configuración SMTP y destinatarios.

### `app/instance_lock.py` — exclusión de instancias
Mantiene un lock local que evita dos ejecuciones simultáneas sobre la **misma instalación**. No sirve por sí solo para bloquear dos equipos diferentes que acceden al mismo buzón: durante un traslado, el equipo antiguo debe detenerse antes de activar el nuevo.

### `app/logging_setup.py` — registro de eventos
Configura el logging del proyecto para investigar ciclos, errores y comportamiento del proceso. Los logs no sustituyen los registros de estado en SQLite ni la persistencia en PostgreSQL; son evidencia de diagnóstico.

### `app/exceptions.py` — tipos de error propios
Agrupa excepciones como `ConfigurationError`, `AuthenticationError`, `GraphApiError` y `ReportParseError`. Facilita que el procesador distinga problemas de configuración, comunicación y lectura de archivos.

## 7. Herramientas históricas y auxiliares

### `historical_import.py` — importador histórico
Permite recorrer y cargar archivos históricos a partir de un directorio e inventario CSV. En la copia analizada admite `--map` y opera en modo de simulación por defecto. Es una herramienta de **cargue controlado**, no el proceso de vigilancia de Outlook. Antes de una ejecución real hay que verificar sus argumentos contra `--help` del código instalado.

**Relación:** usa `historical_schema_map.json`, herramientas de lectura y PostgreSQL. No debe relanzarse indiscriminadamente sobre los mismos documentos.

### `historical_reader.py` — lectura auxiliar histórica
Interpreta inventarios, selecciona archivos por entidad y obtiene filas de distintas fuentes, incluidas variantes históricas. Apoya el importador y las herramientas de análisis de estructura; no es quien monitorea Outlook.

### `historical_structure_scanner.py` — exploración de formatos
Inspecciona archivos históricos y produce información de hojas, encabezados o estructuras para descubrir formatos. Se utiliza durante análisis y preparación de mapeos, no como parte obligatoria del ciclo de correo.

### `historical_structure_profiler.py` — perfilamiento de estructura
Resume características de columnas y representantes de distintos formatos para estudiar consistencia y cambios. Sirve como apoyo técnico al diseñar reglas de normalización y al investigar diferencias entre años.

### `scan_downloaded_products.py` — utilidad puntual de inspección
Script auxiliar relacionado con el examen de archivos descargados. Antes de ejecutarlo debe revisarse su línea de comandos y sus posibles efectos; no se considera un módulo requerido para el arranque normal.

### Tests conservados
- `test_classifier.py`: comprobaciones manuales del clasificador con archivos de prueba.
- `test_parser.py`: revisión manual de lectura e interpretación de Excel.
- `test_new_storage.py`: prueba de persistencia; **puede escribir en PostgreSQL**, usar solo ambiente aislado tras revisar código.
- `test_notifier.py`: prueba de notificación; **puede enviar SMTP real**, usar solo con destinatarios controlados.

Estos scripts no equivalen necesariamente a tests `unittest` automáticos. Los tres tests obsoletos (`test_postgres_classifier.py`, `test_postgres_raw.py` y `test_smbc_forward.py`) fueron archivados al limpiar el proyecto; no forman parte del estado objetivo.

### `state/`, `logs/`, `venv/`, `backups/`
`state/` contiene SQLite y el lock local; `logs/` los registros de ejecución; `venv/` dependencias instaladas en el equipo. Los respaldos de mantenimiento anteriores se archivaron fuera de la raíz operativa. Estas carpetas tienen funciones diferentes y no deben tratarse como código fuente versionado.

## 8. ¿Dónde se modifica cada necesidad?

| Necesidad | Primer archivo a revisar | Además comprobar |
|---|---|---|
| Nuevo dominio de correo para entidad existente | `config.json` | Envío de prueba y resolución de dominio |
| Entidad totalmente nueva | `config.json`, `historical_schema_map.json`, `app/business_normalizer.py` | Tabla PostgreSQL y posibles listas cerradas |
| Columna nueva en Excel conocido | `historical_schema_map.json` o normalizador especializado | Columna y tipo SQL, `validation_issues` |
| Formato Excel que no se puede abrir | `app/parser.py` | Extensiones y límites en `config.json` |
| Mercado u hoja mal clasificados | `app/report_classifier.py` / normalizador | Reglas y evidencia de hojas |
| Error de conexión a PostgreSQL | `.env`, `app/postgres_db.py` | Usuario, red, permisos, esquema |
| Adjunto duplicado | `app/db.py`, `app/fingerprint.py`, `app/processor.py` | Estados y hashes; no borrar filas sin investigar |
| No llega el correo de resumen | `app/notifier.py`, `app/processor.py` | SMTP y destinatarios sin revelar secretos |
| Cambio de máquina o usuario Windows | `.env`, BAT y tarea programada | `BASE_DIR`, permisos de carpetas y recurso UNC |
| Reprocesar años históricos | `historical_import.py` | Inventario, `historical_schema_map.json` y dry-run |

## 9. Datos y controles que nunca deben confundirse

| Elemento | Qué conserva |
|---|---|
| `original_message` (PostgreSQL) | Celdas y valores originales extraídos para auditoría |
| `validation_issues` (PostgreSQL) | Advertencias: columnas no mapeadas, conflictos, conversiones, mercado sin identificar |
| `source_path` (`ingestion_files`) | Ruta física del Excel original histórico o de la descarga final de Outlook |
| `processed_attachments` (SQLite) | Estado de adjuntos y deduplicación entre ciclos |
| Logs (`logs/`) | Eventos y diagnósticos de ejecución |

## 10. Comprobaciones pendientes para la edición definitiva
1. Contrastar nombres y firmas de funciones con el repositorio que contiene el commit `3b06bb0`; el ZIP inspeccionado corresponde a una versión anterior.
2. Confirmar qué alertas por `validation_issues` se incluyen en el resumen SMTP y cuáles quedan únicamente en PostgreSQL.
3. Verificar la tarea programada real, identidad de ejecución y permisos UNC del equipo nuevo.
4. Mantener este manual actualizado si cambia el normalizador, el mapeo JSON o el esquema de PostgreSQL.
