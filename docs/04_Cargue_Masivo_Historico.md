# Manual 04 — Cargue masivo histórico de CloseOutNetting

> **Versión documental:** 1.2 — 10 de noviembre de 2026  
> **Referencia de la aplicación:** rama `main`, commit `3b06bb0`.  
> **Estado:** procedimiento ampliado. La interfaz observada proviene de una copia anterior del código; cotejar `--help` en la versión actual antes de efectuar escrituras.  
> **Regla principal:** simular, verificar destino y respaldar antes de usar `--commit`.

## 1. ¿Para qué sirve este procedimiento?

El cargue masivo incorpora archivos Excel históricos al esquema PostgreSQL `closenetting`. **No recibe correos desde Outlook** y no sustituye el ciclo automático de `main.py`. Su propósito es procesar un conjunto inventariado de archivos, identificando entidad y mercado, normalizando campos, registrando cada ingesta y conservando evidencia de la información de origen.

El ejecutable es `historical_import.py`. Para operar necesita el inventario generado por el escáner histórico y el mapa `historical_schema_map.json`. El inventario identifica archivos y hojas; el mapa contiene reglas de entidad, mercado, columnas y tablas de destino.

## 2. Archivos involucrados

| Archivo | Función | Precaución |
|---|---|---|
| `historical_import.py` | Orquesta la lectura del inventario y el procesamiento por archivo; ofrece `DRY-RUN`, `--commit` y reintentos | Nunca ejecutar con `--commit` sin validar la base destino |
| `historical_reader.py` | Lee el CSV de inventario y agrupa las hojas por archivo; también admite configuración de contraseñas de reportes | No confundir el CSV de carga con el inventario SHA-256 de auditoría |
| `historical_structure_scanner.py` / `historical_structure_profiler.py` | Herramientas relacionadas con exploración de archivos y estructuras históricas | Confirmar en `--help` cuál genera el inventario compatible en la versión instalada |
| `historical_schema_map.json` | Define correspondencias y reglas utilizadas en cargas históricas y por el normalizador automático de ciertas entidades | **No eliminarlo ni modificarlo directamente en producción sin pruebas** |
| `app/postgres_db.py` | Gestión de conexión, ingestas y estados | Revisar el `DATABASE_NAME` efectivo |
| `app/business_normalizer.py` | Normalización de archivos recibidos automáticamente; reutiliza parte del mismo mapa | Un cambio en el mapa puede tener impacto fuera del cargue histórico |
| `.env` | Variables `DATABASE_*` y, si corresponde, `REPORT_PASSWORDS` | Nunca publicar credenciales |
| `closenetting.ingestion_files` | Trazabilidad, hashes, estados y ruta de origen | No duplicar cargas solo para corregir `source_path` |
| `closenetting.raw_report_*` | Tablas financieras por entidad | Comparar filas, tipos y advertencias, no solo el estado de ingesta |

## 3. Diferencia entre inventario de carga e inventario de auditoría

**Inventario del importador:** es el CSV que consume `historical_reader.read_inventory()`. Contiene una fila por hoja y campos como `source_year`, `relative_path`, `filename`, `extension`, `size_bytes`, `file_sha256`, `entity_hint`, `market_hint`, `sheet_name`, `header_row`, `structure_hash`, `headers`, `normalized_headers`, `scan_status`, `open_method` y `error`. El lector usa `csv.DictReader`, por lo que espera encabezados convencionales separados por coma.

**Inventario de trazabilidad:** `inventario_historico_sha256.csv`, usado en noviembre de 2026 para recuperar rutas. Contenía año, ruta, nombre y hash; fue generado con separador `;`. **No debe entregarse directamente como argumento `inventory_csv` del cargue masivo.**

Si no se dispone de un inventario compatible, primero ejecutar el escáner de estructuras vigente y comprobar sus argumentos con `--help`. No inventar encabezados ni transformar el inventario de hashes a ciegas.

## 4. Preparación antes de iniciar

1. Identificar la carpeta física de Excel históricos; verificar que está accesible, por ejemplo mediante UNC en Windows.
2. Confirmar que los Excel originales no serán modificados por el ensayo.
3. Comprobar que el CSV de inventario pertenece a esa misma raíz y que `relative_path` se resuelve correctamente.
4. Revisar entidad y reglas de mercado en `historical_schema_map.json`.
5. Confirmar un respaldo PostgreSQL y un destino de pruebas; nunca utilizar la base operativa por omisión.
6. Validar la conexión PostgreSQL efectiva antes de ejecutar el importador.
7. Registrar fecha, alcance, entidad, años, inventario y versión Git en la bitácora.

### Verificar versión y ayuda del importador

```powershell
cd C:\Ruta\CloseOutNetting
.\venv\Scripts\python.exe .\historical_import.py --help
```

### Verificar base PostgreSQL (solo lectura)

```powershell
@'
from app.postgres_db import get_connection
with get_connection() as conn:
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT current_database(), current_user')
            print('Base y usuario:', cur.fetchone())
    finally:
        conn.rollback()
'@ | .\venv\Scripts\python.exe -
```

En el ensayo de noviembre se utilizó `closenetting_practica`. En una nueva máquina o un cargue autorizado, **sustituir el nombre esperado y verificarlo de nuevo**.

## 5. Parámetros observados de `historical_import.py`

| Argumento | Uso |
|---|---|
| `root` | Carpeta raíz donde están los archivos Excel |
| `inventory_csv` | CSV compatible de inventario por hoja |
| `--map` | Ruta del JSON de mapeo; por defecto `historical_schema_map.json` |
| `--min-year` / `--max-year` | Delimitación de años; en la copia consultada, 2018 a 2026 |
| `--entity` | Filtra la entidad final; puede repetirse |
| `--limit-files` | Limita el número de archivos; `0` significa sin límite |
| `--commit` | Habilita escritura real; **sin él, DRY-RUN** |
| `--retry-failed` | Reintentos sobre ingestas anteriores no finalizadas, bajo las condiciones del código |
| `--retry-partial` | Reintentos sobre ingestas con estado `partial`, conservando el identificador cuando aplique |

Los nombres de estado y la política exacta de reintentos se deben confirmar en la versión desplegada. No forzar reintentos solo porque una ingesta muestre advertencias.

## 6. Simulación controlada

Se recomienda empezar con **un archivo** de una entidad específica y una base restaurada para pruebas. Sustituir los marcadores siguientes por rutas existentes:

```powershell
$root = 'RUTA_RAIZ_DE_EXCEL_HISTORICOS'
$inventario = 'RUTA_CSV_INVENTARIO_COMPATIBLE'

.\venv\Scripts\python.exe .\historical_import.py `
  $root $inventario `
  --entity SET-ICAP `
  --limit-files 1
```

En la copia disponible, la salida identifica `Modo: DRY-RUN`, años, entidades y número de archivos candidatos. Registrar mensajes de errores de lectura, ausencia de mapeos, mercados desconocidos y filas previstas. La simulación debe ser **sin modificaciones**; confirmar en la versión actual los efectos de inicialización o lectura de configuración antes de usarla contra producción.

**Criterios para aprobar la simulación:** base correcta, archivo esperado, entidad correcta, hojas y mercado coherentes, correspondencias válidas, ninguna conversión silenciosa y ausencia de duplicaciones imprevistas.

## 7. Ejecución real (solo con autorización)

Repetir los mismos parámetros del ensayo, agregando `--commit`:

```powershell
# Solo tras comprobar respaldo, inventario, simulación y base destino.
.\venv\Scripts\python.exe .\historical_import.py `
  $root $inventario `
  --entity SET-ICAP `
  --limit-files 1 `
  --commit
```

La importación real puede generar filas tanto en `ingestion_files` como en la tabla financiera correspondiente. La cantidad de filas financieras no tiene por qué coincidir con la cantidad de Excel: un libro puede tener varias hojas y operaciones.

Después de una primera carga aprobada, ampliar el alcance por entidad y año, por lotes controlados y con comparación de resultados.

## 8. Verificar lo insertado

### Ingestas recientes

```sql
SELECT id, entity, source_year, source_filename, status,
       source_path, file_sha256, content_sha256, processed_at
FROM closenetting.ingestion_files
ORDER BY id DESC
LIMIT 30;
```

### Comprobar un archivo SET-ICAP concreto

```sql
SELECT ingestion_id, market_type, source_sheet_name,
       original_message, validation_issues
FROM closenetting.raw_report_set_icap
WHERE ingestion_id = 12345; -- reemplazar por ID real
```

`original_message` conserva información original de las celdas; `validation_issues` registra advertencias como columnas sin mapeo, alias en conflicto, mercado sin clasificar o conversiones no válidas. Un registro almacenado puede contener incidencias: revisar su contenido antes de declararlo correcto.

Para otras entidades, utilizar la tabla `raw_report_*` correspondiente, verificada contra el mapa y el esquema de la base.

## 9. Duplicados, estados parciales y recuperación

- **Duplicado físico:** comparar hash SHA-256 y registros de ingesta existentes; cambiar el nombre del archivo no garantiza que sea nuevo.
- **Contenido lógico repetido:** revisar `content_sha256` y la política de deduplicación antes de reimportar.
- **Ingesta fallida o parcial:** consultar `status`, `ingestion_id`, errores y filas financieras ya persistidas antes de utilizar los indicadores de reintento.
- **Ruta de origen:** para históricos, `source_path` debe ser la ruta original verificada. En noviembre de 2026 se recuperaron 5.644 rutas sin volver a ejecutar el cargue; 69 quedaron pendientes.
- **Reprocesamiento:** no eliminar registros financieros ni reiniciar secuencias como respuesta automática a un error. Investigar y documentar primero.

## 10. Cuando aparece un formato nuevo

1. Guardar una copia de muestra con datos de prueba.
2. Identificar encabezados, hojas, tipos de mercado, formatos numéricos y de fechas.
3. Revisar `historical_schema_map.json` y la tabla PostgreSQL de destino.
4. Simular el cargue y revisar `validation_issues` y `original_message`.
5. Si hay columnas no mapeadas, ajustar mapeos y, si es necesario, DDL, siempre sobre entorno de práctica primero.
6. Validar que el cambio al JSON **no rompa la normalización automática** de las otras entidades.
7. Publicar código y mapeos, dejar respaldo, y solo entonces ejecutar sobre información real autorizada.

## 11. Problemas frecuentes

| Síntoma | Comprobación inicial |
|---|---|
| No encuentra el inventario | Verificar ruta absoluta y existencia del CSV |
| No selecciona ningún archivo | Revisar rango de años, entidad final, `entity_remap` y `relative_path` |
| No reconoce una hoja | Revisar `sheet_name`, encabezados, `market_overrides` y estado del escáner |
| Campo no mapeado | Revisar alias, nombre de columna destino y `validation_issues` |
| Error al convertir un dato | Comparar valor original, tipo PostgreSQL y advertencia de conversión |
| Archivo protegido | Revisar `REPORT_PASSWORDS` sin revelar claves |
| Duplicado o estado parcial | Consultar ingesta existente y hashes antes de un reintento |
| Escritura en base equivocada | Suspender el proceso y revisar `DATABASE_*`; no continuar por inercia |

## 12. Evidencias para la bitácora

Guardar fuera del repositorio: versión Git, fecha y operador, origen y alcance, inventario empleado, hash de muestras, base y esquema de destino, parámetros de ejecución, salida del DRY-RUN, autorización para `--commit`, IDs de ingestas, conteos y discrepancias. Nunca incluir credenciales en los reportes.

## 13. Puntos pendientes de contraste con el código más reciente

- Comando exacto del escáner para generar inventario compatible.
- Comportamiento de inicialización en modo DRY-RUN de la versión desplegada.
- Semántica actual de `--retry-failed` y `--retry-partial`.
- Estado de los mapeos específicos si se han incorporado entidades nuevas.

Estos puntos no invalidan las verificaciones históricas, pero impiden presentar esta edición como una certificación de ejecución del commit `3b06bb0`.
