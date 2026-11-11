# Manual 06 — Operación, monitoreo y solución de problemas

> **Sistema:** SET ICAP — Auto CloseNetting  
> **Edición:** ampliada, 11 de noviembre de 2026 (documentación v1.5)  
> **Referencia de proyecto:** rama `main`, commit informado `3b06bb0`.  
> **Alcance:** procedimientos de consulta seguros y pautas de diagnóstico. Los detalles de implementación se contrastaron con fragmentos compartidos y una copia del código del proyecto; antes de ejecutar cambios operativos, verificar la versión actualmente desplegada.  
> **Regla esencial:** nunca publicar `.env`, contraseñas, adjuntos financieros, tokens ni volcados de bases de datos.

## 1. Qué debe revisar un operador

CloseOutNetting recupera adjuntos mediante Microsoft Graph, identifica la entidad con `config.json`, conserva el Excel, interpreta sus hojas, normaliza datos y registra trazabilidad en PostgreSQL y estado de adjuntos en SQLite. Finalmente construye un resumen de ciclo y procura enviarlo por SMTP.

| Componente | Dónde se configura o registra | Qué se comprueba |
|---|---|---|
| Correo y autenticación | `.env`, `app/auth.py`, `app/graph_api.py` | Acceso a buzón y permisos Graph, sin repetir consumos |
| Remitentes y reglas | `config.json`, `app/entities.py` | Dominio conocido o entidad predeterminada `OTROS` |
| Descargas | `DOWNLOAD_DIR`, `app/security.py`, `app/processor.py` | Acceso al directorio y archivo físico conservado |
| Procesamiento | `app/parser.py`, `app/report_classifier.py`, `app/business_normalizer.py` | Hojas, encabezados, mercados, conversión y advertencias |
| Persistencia financiera | `app/postgres_db.py`, `closenetting.ingestion_files`, `raw_report_*` | Estado de la ingesta y filas efectivamente guardadas |
| Seguimiento de adjuntos | `app/db.py`, `state/automation_state.db` | IDs ya vistos, duplicados y estado |
| Alertas | `app/notifier.py`, `app/processor.py` | Resumen SMTP y errores de entrega |
| Ejecución | `main.py`, `iniciar_closenetting.bat`, `app/instance_lock.py` | Solo una instancia activa y programación conocida |

## 2. Inicio, ejecución y detención controlada

### 2.1 Antes del inicio

1. Confirmar ruta y cuenta Windows que ejecutarán el proceso; no asumir el usuario `scardenas` en máquinas futuras.
2. Confirmar que `.env`, `config.json` y `historical_schema_map.json` estén presentes. **El mapa histórico también se usa en normalización automática de entidades adicionales.**
3. Confirmar servicio PostgreSQL, acceso a SQLite y permisos en `DOWNLOAD_DIR` (incluidos recursos UNC).
4. Comprobar que no haya otra instalación activa contra el mismo buzón/estado.
5. Confirmar el intervalo de `poll_interval_minutes` del equipo que se va a operar. En la práctica se observó `720` minutos; no asumir que sea idéntico en producción.

### 2.2 Métodos de inicio

- `iniciar_closenetting.bat` cambia al directorio del propio BAT, activa `venv\Scripts\activate.bat` e inicia `python -u main.py`.
- Si Python devuelve error, el BAT **intenta reiniciarlo tras 30 segundos**; si devuelve código cero, finaliza normalmente.
- `python main.py --once` ejecuta **un ciclo real** en días hábiles y no es una simulación. No utilizar para pruebas de solo lectura ni sin revisar correo pendiente.
- En la copia examinada, `main.py` espera durante fines de semana y realiza `process_cycle` en días hábiles; el modo `--once` no debe entenderse como prueba inocua ni como garantía de finalizar inmediatamente en fin de semana.

Para identificar tareas programadas en Windows (solo lectura):

```powershell
Get-ScheduledTask | Where-Object {
    $_.TaskName -match 'closenetting|closeout|netting' -or
    ($_.Actions | Where-Object {
        $_.Execute -match 'closenetting|closeout|netting' -or
        $_.Arguments -match 'closenetting|closeout|netting'
    })
} | Select-Object TaskName, TaskPath, State
```

Para consultar procesos Python activos antes del mantenimiento (solo lectura):

```powershell
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^(python|pythonw|cmd)\.exe$' -and
    $_.CommandLine -match 'closenetting|main\.py'
} | Select-Object ProcessId, ParentProcessId, Name, CommandLine
```

**Detención:** identificar primero si se inició desde una consola, un BAT o el Programador de tareas; suspender futuras invocaciones programadas y solicitar una finalización normal del proceso. Evitar `taskkill /F` como mecanismo rutinario, porque puede interrumpir una descarga o una transacción. Confirmar después que no quedan dos consumidores activos.

## 3. Logs y resumen SMTP

### 3.1 Archivos y rotación

En `app/logging_setup.py` se define `LOG_DIR` relativo al proyecto (`logs/`) y el archivo `closenetting.log`. La copia de código inspeccionada utiliza `TimedRotatingFileHandler` con rotación diaria y retención de 30 copias. Confirmar que esa configuración siga vigente en el despliegue.

```powershell
$log = Join-Path (Get-Location).Path 'logs\closenetting.log'
if (Test-Path $log) {
    Get-Content -LiteralPath $log -Tail 100
} else {
    Write-Host 'No se encontro el log en esta carpeta'
}
```

Para filtrar incidencias sin cambiar ningún dato:

```powershell
Select-String -Path '.\logs\closenetting*.log' -Pattern `
    'ERROR','WARNING','ENTIDAD DESCONOCIDA',` 
    'DUPLICADO','CAMPO NUEVO','UNMAPPED','SMTP' `
    -ErrorAction SilentlyContinue |
    Select-Object -Last 80
```

### 3.2 Interpretar el resumen

El procesador cuenta correos revisados, archivos descargados, interpretados, guardados, duplicados, enviados a `OTROS`, rechazados por tamaño y errores de lectura. Asimismo acumula `structure_changes` y `cycle_incidents`. Al terminar el ciclo, intenta enviar el resumen SMTP incluso si durante el ciclo hubo incidencias.

**Importante:** una incidencia en `validation_issues` no garantiza por sí sola un aviso individual por correo. El resumen muestra los cambios e incidencias que el procesador agregue explícitamente a sus colecciones; para cada formato, comprobar el enlace entre el resultado del normalizador y `structure_changes`.

Si un correo de resumen no llega, revisar en este orden: log de intento SMTP, destinatarios de `config.json`, configuración SMTP en `.env` (sin imprimir secretos), conectividad y rechazos del servidor. El procesamiento de los archivos puede haberse completado aunque falle el correo de resumen.

## 4. Consultas de PostgreSQL exclusivamente de lectura

Conectar de forma explícita a la base correcta; antes de investigar, ejecutar:

```sql
SELECT current_database(), current_user;
```

### 4.1 Últimas ingestas y estados

```sql
SELECT id, entity, source_filename, status, source_path,
       received_at, processed_at
FROM closenetting.ingestion_files
ORDER BY id DESC
LIMIT 40;
```

```sql
SELECT entity, status, COUNT(*) AS cantidad
FROM closenetting.ingestion_files
GROUP BY entity, status
ORDER BY entity, status;
```

### 4.2 Trazabilidad de un archivo concreto

```sql
SELECT id, entity, source_filename, file_sha256, content_sha256,
       source_path, status, received_at, processed_at
FROM closenetting.ingestion_files
WHERE id = 12345; -- Sustituir por ID real
```

Para una entidad específica con tabla ya conocida, consultar filas por `ingestion_id`:

```sql
SELECT ingestion_id, market_type, source_sheet_name,
       original_message, validation_issues
FROM closenetting.raw_report_set_icap
WHERE ingestion_id = 12345;
```

Evitar extraer o enviar públicamente los valores financieros de `original_message`.

### 4.3 Incidencias de normalización

En las tablas `raw_report_*`, `original_message` conserva evidencia de celdas originales y `validation_issues` contiene advertencias del análisis/conversión. Códigos observados en el normalizador compartido:

| Código | Significado | Siguiente comprobación |
|---|---|---|
| `UNMAPPED_HISTORICAL_COLUMN` | Campo con valor, sin columna válida de destino | Revisar alias del mapa y columnas PostgreSQL |
| `DUPLICATE_HEADER_CONFLICT` | Encabezados equivalentes con valores incompatibles | Comparar encabezados y celdas originales |
| `COLUMN_ALIAS_CONFLICT` | Dos orígenes apuntan al mismo destino con valores distintos | Revisar prioridad de alias |
| `DESTINATION_COLUMN_MISSING` | Columna de destino inexistente en la tabla | Comparar mapa con `information_schema.columns` |
| `UNCLASSIFIED_MARKET` | El mercado no pudo inferirse con seguridad | Revisar hoja y reglas `market_overrides` |
| Conversión inválida | El valor no corresponde al tipo requerido | Consultar evidencia original; no forzar conversiones |

La presencia de advertencias no implica por sí misma que todas las filas hayan fallado. Validar el resultado en la tabla y el `status` de la ingesta.

## 5. Estado SQLite: lectura segura

La base local es `state\automation_state.db`; no compartir el mismo archivo simultáneamente entre instalaciones. En diagnóstico, abrir en modo lectura:

```powershell
@'
from pathlib import Path
import sqlite3
p = Path('state/automation_state.db').resolve()
with sqlite3.connect(p.as_uri() + '?mode=ro', uri=True) as db:
    print('integrity_check:', db.execute('PRAGMA integrity_check').fetchone()[0])
    print('processed_attachments:', db.execute('SELECT COUNT(*) FROM processed_attachments').fetchone()[0])
    print('execution_history:', db.execute('SELECT COUNT(*) FROM execution_history').fetchone()[0])
'@ | .\venv\Scripts\python.exe -
```

En la práctica de restauración se observaron **138 adjuntos** y `integrity_check=ok`; esa es una fotografía histórica, no una cantidad fija esperada para producción futura.

No borrar registros de SQLite para provocar que un correo se descargue de nuevo. La deduplicación puede considerar `message_id`, `attachment_id`, hash binario, hash de contenido y coincidencias con PostgreSQL; corregir solo con diagnóstico y respaldo.

## 6. Árbol de decisión de incidencias

| Síntoma | Comprobaciones prioritarias | Qué no hacer |
|---|---|---|
| No aparecen correos nuevos | Confirmar día y horario, filtro de consulta Graph, intervalo de ciclo, credenciales, permisos y log | Lanzar una segunda instancia sin revisar la primera |
| Gmail desconocido | Dominio en `config.json`, `default_entity=OTROS`, extensión y tamaño permitido | Añadir Gmail a una entidad real para forzar la demo |
| Adjunto en `OTROS` | Ruta física por año, ingesta `unknown_entity`, marca SQLite, resumen | Reprocesar indiscriminadamente todas las ingestas |
| Excel reconocido, cero filas | Parser, hojas, mercado, encabezados, `validation_issues`, logs | Introducir valores inventados para completar columnas |
| Aparece columna no mapeada | `historical_schema_map.json`, mapa de alias, tipos de tabla | Eliminar ese JSON porque diga «historical» |
| Archivo duplicado | IDs, SHA-256 binario/contenido, estado SQLite e ingesta | Quitar hashes o estado sin respaldo |
| `source_path=NULL` | Estado, destino realmente conservado, duplicado o ambigüedad física | Inventar una ruta de origen |
| PostgreSQL rechaza conexión | Host/puerto/base, rol, contraseña, servicio, red | Alterar base de producción para solucionar práctica |
| SQLite bloqueado | Doble instancia, permisos, disco y procesos abiertos | Copiar el fichero en caliente sin respaldo consistente |
| SMTP no entrega | Log, destinatarios, credenciales, servidor y TLS | Exponer secretos o reenviar datos financieros a terceros |
| Errores de permisos UNC | Cuenta efectiva Windows, ACL, conectividad y directorio de descarga | Suponer que permisos de tu sesión equivalen a los de la tarea |

## 7. Caso específico: entidad desconocida (`OTROS`)

En fragmentos de la versión operativa aportados durante las pruebas, el flujo de `app/processor.py` cuando `is_unknown_entity` es verdadero registra la incidencia, conserva físicamente el archivo, registra `source_path` (si es posible), marca la ingesta `unknown_entity` y registra el adjunto en SQLite; no persiste operaciones normalizadas. El resumen operativo contabiliza archivos enviados a `OTROS`.

**Verificación:** localizar el Excel dentro del directorio configurado, consultar `ingestion_files` por nombre/hash/ID y comprobar la marca SQLite. No prometer una alerta SMTP individual; lo observado es el envío de resumen de ciclo, sujeto a disponibilidad SMTP.

## 8. Caso específico: SET-ICAP con campo nuevo

SET-ICAP pertenece al grupo de entidades adicionales del normalizador compartido. `historical_schema_map.json` contiene correspondencias usadas en el procesamiento automático; no es solo una herramienta de importación histórica. Si una columna con contenido no encuentra destino permitido, puede producir `UNMAPPED_HISTORICAL_COLUMN`, preservando la evidencia en `original_message` y `validation_issues` cuando se generan filas persistibles.

**Verificación segura:** identificar el `ingestion_id`, consultar `raw_report_set_icap`, revisar `validation_issues`, y comparar contra el mapa y columnas reales de PostgreSQL. No asegurar que una columna nueva siempre genere un correo individual.

## 9. Procedimiento de recuperación y cambios

1. **Contener:** registrar incidente y detener nuevas ejecuciones si hay riesgo de duplicidad o escritura incorrecta.
2. **Evidenciar:** fecha/hora, máquina, commit, entidad, nombre de Excel, `ingestion_id`, estado, extracto de log y error. Proteger información financiera.
3. **Respaldar:** PostgreSQL con `pg_dump -Fc` y SQLite con `sqlite3.Connection.backup()` en ventana coordinada. Verificar archivo y restaurabilidad.
4. **Diagnosticar:** reproducir primero en `closenetting_practica` y copia local de SQLite; nunca reutilizar el `.env` operativo sin aislamiento.
5. **Corregir y comprobar:** modificar la pieza mínima necesaria, ejecutar pruebas y comparar conteos/estados.
6. **Retomar de manera controlada:** asegurar una sola instalación activa y observar el siguiente ciclo.
7. **Documentar:** causas, comandos utilizados, cambios y resultados en la bitácora. No mezclar historial de incidentes con código operativo.

### Cuándo escalar

- **IT/infraestructura:** permisos UNC, programación Windows, discos, conectividad/servicios PostgreSQL.
- **Desarrollo:** clasificación, mapeos, parser, deduplicación, normalización o errores del código.
- **Correo/M365:** permisos Microsoft Graph, buzón, credenciales y restricciones SMTP.
- **Negocio/datos:** interpretación de campos, cambios de estructura financiera y validación de resultados.

## 10. Checklist operativo reutilizable

- [ ] Confirmé servidor, base, usuario efectivo Windows y versión del proyecto.
- [ ] Confirmé una sola instancia activa y revisé la tarea programada.
- [ ] Revisé los últimos logs y el resultado del último ciclo.
- [ ] Verifiqué las últimas ingestas PostgreSQL y su `source_path`.
- [ ] Comprobé que las incidencias relevantes están identificadas.
- [ ] Revisé el estado de SQLite si hubo duplicados o descargas faltantes.
- [ ] Confirmé el correo de resumen o documenté su fallo independientemente del procesamiento.
- [ ] No ejecuté importaciones, reintentos o correcciones financieras sin respaldo y autorización.

## 11. Validación documental pendiente

Este manual se amplió con resultados de las pruebas realizadas y fragmentos del proyecto accesibles. El paquete fuente disponible puede preceder al commit `3b06bb0`, por lo que los nombres exactos de funciones, mensajes y políticas de envío deben contrastarse en el despliegue antes de ejecutar automatizaciones o correcciones. **No se realizó ninguna intervención sobre el proyecto operativo ni sobre bases de datos al elaborar este manual.**
