# Manual 02 — Migración de CloseOutNetting a otra máquina y reversión

**Edición:** 1.4 · 11 de noviembre de 2026  
**Referencia:** rama `main`, commit `3b06bb0` (limpieza); verificar el HEAD desplegado antes del corte.  
**Alcance:** traslado de aplicación Windows, configuración, PostgreSQL, SQLite y continuidad del procesamiento de correo.  
**Estado:** restauración e instalación **validadas en el mismo equipo**; traslado a otro equipo, corte y reversión **pendientes de ensayo**.

> **Principio crítico:** nunca mantener dos consumidores activos del mismo buzón. `main.py --once` procesa mensajes reales; **no** es un `dry-run`. Los respaldos contienen datos financieros y el `.env` contiene secretos: nunca versionarlos ni adjuntarlos a tickets sin protección.

## 1. Objetivo y componentes que deben trasladarse

La migración no consiste únicamente en copiar la carpeta del proyecto. Debe preservar la concordancia de tres estados: **PostgreSQL** (ingestas y datos financieros), **SQLite** (identificadores de mensajes/adjuntos ya procesados) y **Excel almacenados** (archivos físicos o recurso compartido). Si una instalación empieza a consumir correo con una SQLite desactualizada, puede reprocesar mensajes aunque PostgreSQL esté restaurado.

| Componente | Qué trasladar o preparar | Dependencia relevante |
|---|---|---|
| Código | Commit autorizado del repositorio; `app/`, `main.py`, BAT, scripts de uso vigente | Instalar en cualquier carpeta estable con permisos |
| Python | Instalar Python compatible y **crear** `venv` en destino | No copiar `venv` entre máquinas |
| Configuración | `.env`, `config.json`, `historical_schema_map.json` | El JSON de mapeos **también** lo usa `business_normalizer.py` en la automatización |
| PostgreSQL | Dump y restauración del esquema `closenetting`, más dependencias externas identificadas | Verificar usuarios, grants, extensiones, secuencias y compatibilidad |
| SQLite | `state/automation_state.db` desde respaldo consistente | Debe corresponder al mismo punto de corte que PostgreSQL |
| Excel | Acceso a ruta UNC histórica y carpeta automatizada | Cuenta de Windows y permisos SMB/NTFS |
| Ejecución | Tarea programada, cuenta de servicio, BAT, horario | Solo una instancia activa |
| Integraciones | Graph, SMTP, DNS/proxy y conectividad | Credenciales y permisos de la cuenta de ejecución |

## 2. Evidencia disponible de nuestra práctica

**VALIDADO (noviembre de 2026):**

| Prueba | Resultado |
|---|---|
| Python y Git | Python 3.14.7; Git 2.55.0.windows.3 |
| PostgreSQL | Versión 16.15; `pg_dump -Fc`, `pg_restore --list` y restauración satisfactoria |
| Dump de práctica | `closenetting_backup.dump`, 35.799.124 bytes, 132 entradas TOC |
| Base restaurada | `closenetting_practica`, 10 tablas, 177.840 filas |
| Ingestas | 5.718 en total, 5.649 con ruta, 69 sin ruta |
| SQLite | `automation_state_20261104_084946.db`, integridad `ok`, 138 adjuntos |
| Configuración de práctica | DB `closenetting_practica`, descargas locales, SQLite independiente |
| Diagnóstico | `diagnostico_practica_20261109_084608.txt`: **SATISFACTORIO** |

**No validado aún:** instalación en segundo Windows, configuración de tarea programada definitiva, pruebas no invasivas de Graph/SMTP, comparación exhaustiva desde el mismo punto de corte, primer ciclo productivo en destino y reversión después de nuevas escrituras.

Las cifras anteriores son **históricas**, no objetivos fijos para una migración futura.

## 3. Planificación: antes de tocar la máquina de origen

1. Designar equipo origen, equipo destino, responsables de aplicación, bases e infraestructura y ventana de cambio.
2. Identificar **cómo se ejecuta hoy** el BAT o `main.py`: tarea programada, servicio o sesión manual. Capturar configuración sin publicar contraseñas.
3. Elegir ruta estable en el nuevo equipo, por ejemplo `C:\Aplicaciones\CloseOutNetting`; **no depende del usuario `scardenas`**.
4. Definir dónde residirá PostgreSQL: servidor compartido actual, nueva instancia o servidor nuevo; documentar host, puerto, base, rol y red.
5. Confirmar que el usuario de ejecución puede leer/escribir la carpeta de descargas, acceder a UNC, abrir SQLite y generar logs.
6. Confirmar hora, zona `America/Bogota`, acceso a Graph y servidor SMTP. Los secretos se transfieren por canal seguro.
7. Acordar criterios de aceptación, período de observación, punto de no retorno y plan de reversión con responsables.
8. Mantener el arranque del equipo destino **deshabilitado** durante toda la preparación.

### 3.1 Inventario del equipo origen (solo lectura)

Desde PowerShell en la instalación operativa:

```powershell
Set-Location 'C:\Users\scardenas\auto-closeoutnetting'
python --version
python -m pip --version
python -m pip check
git rev-parse HEAD
git status --short
& 'C:\Program Files\PostgreSQL\16\bin\pg_dump.exe' --version
```

Registrar resultados en la bitácora. No migrar cambios locales sin confirmar su inclusión en el commit aprobado.

### 3.2 Inspeccionar la tarea Windows (solo lectura)

```powershell
Get-ScheduledTask | Where-Object {
    $_.TaskName -match 'closenetting|closeout|netting' -or
    (($_.Actions | Out-String) -match 'closenetting|closeout|netting')
} | Select-Object TaskName, TaskPath, State, Actions, Principal, Triggers
```

Registrar `TaskName` **real**, cuenta, disparadores, acción y directorio de trabajo. **No asumir** un nombre de tarea ni detenerla con comandos genéricos. Los detalles pueden contener rutas o cuentas internas: revisar antes de compartir.

## 4. Preparar el destino sin iniciar la automatización

### 4.1 Software y proyecto

Instalar Python 3.14.x y herramientas PostgreSQL compatibles con el servidor origen; se validó PostgreSQL 16.15. Elegir una carpeta libre y preparar el código con el commit aprobado. Una opción es clonar mediante Git; el comando se completa con la URL **real** del repositorio, no con una URL inventada.

```powershell
$installDir = 'C:\Aplicaciones\CloseOutNetting'
# Clonar desde el repositorio aprobado en $installDir (sin lanzar el BAT).
# En la carpeta ya preparada:
Set-Location $installDir
py -3.14 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Falló la instalación de dependencias' }
.\venv\Scripts\python.exe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Existen conflictos de dependencias' }
```

No copiar `.git`, cachés, `.pyc`, logs antiguos o `venv` desde otra instalación por defecto. Si se usa `robocopy` en lugar de Git, revisar el resultado: los códigos de salida **0 a 7** suelen ser no fatales; **8 o superiores** indican fallos.

### 4.2 Configuración

Transferir `.env` por un medio autorizado y restringir permisos. Ajustar **solo las variables necesarias** según la nueva infraestructura:

| Variable / recurso | Validación |
|---|---|
| `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD` | Base y rol correctos del destino; nunca registrar contraseña |
| `DOWNLOAD_DIR` | Ruta real y permisos bajo la identidad de ejecución |
| `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`, `MAILBOX_USER` | Revisar continuidad y permisos de Graph sin consumir correo aún |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_USE_TLS` | Revisar conectividad y autenticación sin notificar destinatarios reales accidentalmente |
| `REPORT_PASSWORDS` | Transferir protegida, no imprimir su valor |
| `config.json` | Dominios/entidades, límites, horarios, zona y destinatarios de alerta |
| `historical_schema_map.json` | Debe permanecer: también es dependencia del normalizador automático |

El código observado calcula `BASE_DIR` desde `__file__` y SQLite como `BASE_DIR / 'state' / 'automation_state.db'`; por eso no es obligatorio replicar `C:\Users\scardenas`. **Pero** sí deben inspeccionarse BAT, tareas y cualquier otra ruta absoluta.

> `load_dotenv(..., override=False)` no sustituye variables de entorno ya definidas en Windows. Confirmar en tiempo de ejecución la base efectiva con `current_database()`.

## 5. Preparar respaldos de ensayo (sin corte)

Estos permiten confirmar compatibilidad, pero **no reemplazan los respaldos finales coordinados**.

### 5.1 PostgreSQL con `pg_dump` (solo lectura)

```powershell
$pgBin = 'C:\Program Files\PostgreSQL\16\bin'
$pgHost = Read-Host 'Host PostgreSQL origen'
$pgPort = Read-Host 'Puerto PostgreSQL'
$pgDatabase = Read-Host 'Base de origen'
$pgUser = Read-Host 'Usuario autorizado'
$backupDir = 'C:\Resguardos\CloseOutNetting'
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$backup = Join-Path $backupDir ("closenetting_{0}.dump" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
& "$pgBin\pg_dump.exe" -h $pgHost -p $pgPort -U $pgUser -d $pgDatabase -n closenetting -Fc --no-owner --no-acl -W -f $backup
if ($LASTEXITCODE -ne 0) { throw 'ERROR: pg_dump falló' }
& "$pgBin\pg_restore.exe" --list $backup | Select-Object -First 20
if ($LASTEXITCODE -ne 0) { throw 'ERROR: catálogo no legible' }
Get-FileHash -Algorithm SHA256 -LiteralPath $backup
```

El dump de un solo esquema **no garantiza** incluir roles, extensiones, permisos ni otros objetos externos. Inventariarlos y definir el procedimiento de recreación. El formato `-Fc` se restaura con `pg_restore`, no como SQL plano.

### 5.2 SQLite consistente

Durante ensayos puede utilizarse `Connection.backup()`. **En el corte definitivo, primero detener el procesamiento**.

```powershell
# Ejecutar desde la raíz de CloseOutNetting, usando su Python.
@'
from pathlib import Path
from datetime import datetime
import sqlite3
source = Path('state/automation_state.db')
if not source.is_file():
    raise SystemExit('Falta SQLite de origen')
target_dir = Path(r'C:\Resguardos\CloseOutNetting')
target_dir.mkdir(parents=True, exist_ok=True)
target = target_dir / f'automation_state_{datetime.now():%Y%m%d_%H%M%S}.db'
with sqlite3.connect(source) as original, sqlite3.connect(target) as backup:
    original.backup(backup)
with sqlite3.connect(target) as check:
    integrity = check.execute('PRAGMA integrity_check').fetchone()[0]
    count = check.execute('SELECT COUNT(*) FROM processed_attachments').fetchone()[0]
if integrity != 'ok':
    raise SystemExit('Respaldo SQLite inconsistente')
print('SQLite:', target, '| Integridad:', integrity, '| Adjuntos:', count)
'@ | .\venv\Scripts\python.exe -
```

Se debe registrar nombre, tamaño, SHA-256 y conteos; mantener los respaldos fuera del repositorio y con acceso restringido.

## 6. Restaurar en el servidor de destino

**Solo ejecutar luego de verificar de forma independiente**: host de destino, usuario autorizado, nombre de base a crear y que la base no existe. No restaurar sobre el origen ni sobre una base con datos.

### 6.1 Crear base vacía

```powershell
$pgBin = 'C:\Program Files\PostgreSQL\16\bin'
$destHost = Read-Host 'Host PostgreSQL DESTINO'
$destPort = Read-Host 'Puerto PostgreSQL DESTINO'
$destDb = 'closenetting_destino'
$destAdmin = Read-Host 'Administrador PostgreSQL DESTINO'
& "$pgBin\psql.exe" -h $destHost -p $destPort -U $destAdmin -d postgres -W -v ON_ERROR_STOP=1 -c 'SELECT current_database(), inet_server_addr(), inet_server_port();'
if ($LASTEXITCODE -ne 0) { throw 'No se pudo validar servidor destino' }
& "$pgBin\psql.exe" -h $destHost -p $destPort -U $destAdmin -d postgres -W -v ON_ERROR_STOP=1 -c "SELECT datname FROM pg_database WHERE datname = '$destDb';"
if ($LASTEXITCODE -ne 0) { throw 'Falló consulta de existencia de base' }
# Continuar con CREATE DATABASE únicamente después de comprobar que no existe.
& "$pgBin\psql.exe" -h $destHost -p $destPort -U $destAdmin -d postgres -W -v ON_ERROR_STOP=1 -c "CREATE DATABASE $destDb ENCODING 'UTF8' TEMPLATE template0;"
if ($LASTEXITCODE -ne 0) { throw 'Falló CREATE DATABASE; no restaurar' }
```

Si la base ya existe, **detenerse**: no ejecutar `DROP DATABASE` ni sobreescribirla automáticamente.

### 6.2 Restaurar dump

```powershell
# $backup contiene la ruta del dump que ya se transfirió y cuyo hash coincide.
& "$pgBin\pg_restore.exe" -h $destHost -p $destPort -U $destAdmin -d $destDb --no-owner --no-acl --exit-on-error --single-transaction -W $backup
if ($LASTEXITCODE -ne 0) { throw 'Error durante la restauración' }
Write-Host 'Restauración completada'
```

La transacción única evita una restauración parcial **dentro de la transacción del restore**, pero no borra automáticamente la base recién creada si el comando falla. Reconstruir/autorizar roles, permisos y extensiones según el inventario; evitar que la aplicación funcione permanentemente como superusuario `postgres`.

### 6.3 Colocar SQLite restaurada

Copiar la SQLite respaldada a `state\automation_state.db` en la **nueva carpeta**; comprobar SHA-256 e integridad. No copiar la SQLite mientras la instalación origen sigue avanzando y luego activar destino: sería una fotografía desfasada.

## 7. Verificaciones antes de activar Outlook

### 7.1 Validar PostgreSQL y tablas

```sql
SELECT current_database() AS base, current_user AS usuario;
SELECT COUNT(*) AS ingestas,
       COUNT(source_path) AS con_ruta,
       COUNT(*) FILTER (WHERE source_path IS NULL) AS sin_ruta
FROM closenetting.ingestion_files;
```

Comparar conteos **exactos** de las 10 tablas contra el mismo punto de corte, incluyendo claves, índices, restricciones y secuencias. En el ensayo anterior se restauraron:

| Tabla | Filas en el ensayo (NO usar como valores del próximo corte) |
|---|---:|
| `ingestion_files` | 5.718 |
| `raw_report_bbva` | 19 |
| `raw_report_bladex` | 5 |
| `raw_report_citi` | 59 |
| `raw_report_ficc` | 16.195 |
| `raw_report_gs` | 6.710 |
| `raw_report_morgan_stanley` | 145.432 |
| `raw_report_set_icap` | 614 |
| `raw_report_smbc` | 153 |
| `raw_report_standard` | 2.935 |
| **Total** | **177.840** |

### 7.2 Diagnóstico desde Python

El script `diagnostico_migracion.py` fue creado en la **copia de práctica**; comprobar que exista en la instalación destino. No asumir que está versionado en Git.

```powershell
Set-Location 'C:\Aplicaciones\CloseOutNetting'
if (-not (Test-Path '.\diagnostico_migracion.py')) { throw 'Falta el diagnóstico; copiar versión validada por canal seguro' }
.\venv\Scripts\python.exe .\diagnostico_migracion.py --expected-db closenetting_destino
if ($LASTEXITCODE -ne 0) { throw 'Diagnóstico con observaciones' }
```

En migración real, añadir cantidades esperadas tomadas del respaldo final. Este diagnóstico verifica configuración, SQLite y lectura PostgreSQL; **no** demuestra la conectividad de Graph/SMTP ni valida todos los permisos externos.

### 7.3 Validar cuenta Windows y red

- Probar acceso UNC desde el **contexto de la cuenta que ejecutará la tarea**, no solo desde la sesión del administrador.
- Verificar lectura de Excel históricos y escritura/renombrado/borrado de un archivo temporal **únicamente** en la carpeta autorizada; no alterar archivos existentes.
- Verificar que `DOWNLOAD_DIR` no apunte a un directorio de la máquina anterior.
- Comprobar la configuración de tareas y que el programa de destino sigue **deshabilitado**.
- Diseñar pruebas controladas de Graph y SMTP para no consumir correo ni notificar destinatarios reales accidentalmente.

## 8. Corte real: secuencia operativa

> **PENDIENTE DE ENSAYO.** Requiere aprobación de responsables. El ensayo en `closenetting_practica` fue en un único equipo, no una migración física.

| Paso | Acción | Evidencia requerida | Si falla |
|---|---|---|---|
| 1 | Confirmar ventana y detener recepción/procesamiento | Autorización y bitácora | No comenzar |
| 2 | Deshabilitar **tarea** de origen y esperar fin del ciclo | Sin procesos Python activos de CloseOutNetting | Mantener origen detenido |
| 3 | Registrar estado final, conteos y último procesamiento | Consulta PG, SQLite y logs | Investigar diferencias |
| 4 | Respaldar PG y SQLite con origen detenido | Hashes y verificaciones `pg_restore --list`, SQLite `ok` | No avanzar |
| 5 | Copiar datos/configuración al nuevo equipo por canal protegido | Hashes de archivos coincidentes | Repetir transferencia |
| 6 | Restaurar y comparar datos contra el mismo punto de corte | Conteos por tabla, rutas, esquemas, secuencias | No activar Outlook |
| 7 | Validar permisos, red y configuración en destino | Diagnóstico y prueba de acceso controlada | Corregir sin consumir mensajes |
| 8 | Mantener origen deshabilitado y habilitar solo destino | Una sola tarea/instancia activa | Detener destino |
| 9 | Supervisar el primer ciclo real y su resumen | Log, ingestas, SQLite, Excel, alertas y SMTP | Evaluar reversión |
| 10 | Aceptar cambio, preservar respaldos y registrar cierre | Acta/bitácora | Mantener observación |

**Detención correcta:** deshabilitar la tarea evita nuevos disparos, pero si ya hay un ciclo activo, confirmar su terminación. No terminar un proceso en medio de un `os.replace`, descarga o transacción sin evaluar consistencia.

## 9. Reversión (rollback) — dos escenarios

### A. Antes de procesar correos en destino

1. Dejar destino deshabilitado o detenerlo.
2. Confirmar que no se han producido escrituras en PG/SQLite destino ni archivos nuevos.
3. Confirmar que el origen sigue consistente; reactivar **solo** la tarea antigua si está autorizado.
4. Registrar la causa del fallo y mantener el dump/copia de SQLite disponibles.

### B. Después de procesar correos en destino

**No reactivar directamente la instalación antigua con su SQLite y PostgreSQL desactualizados.** Esa acción puede volver a descargar correos o introducir duplicados.

1. Detener y deshabilitar destino, documentar último mensaje, adjunto, ingesta y archivo físico procesado.
2. Comparar diferencias del estado **posterior al corte**: PostgreSQL, SQLite y archivos Excel, incluidos estados `processed`, `unknown_entity`, duplicados y errores de lectura.
3. Determinar si las escrituras pueden trasladarse de forma consistente al origen o si se necesita un respaldo/restauración supervisados. No improvisar actualizaciones manuales de la información financiera.
4. Reconciliar mensajes e ingestas y **validar las cantidades y hashes** bajo una ventana de mantenimiento.
5. Solo tras aprobación, reactivar origen y vigilar que exista un único consumidor.

Esta reversión con actividad nueva **no ha sido ensayada**; documentar el procedimiento específico antes del corte.

## 10. Bitácora por migración y criterios de aceptación

Registrar fecha/hora (zona `America/Bogota`), equipo, operador, tarea, comandos, cambios, resultados, archivo de evidencia, hashes y autorizaciones. Usar `CloseOutNetting_Bitacora_Ejecucion_Plantilla.md`.

**Aceptación mínima:**

- [ ] Origen detenido; no hay instancias duplicadas.
- [ ] Código/commit y dependencias validados.
- [ ] `.env`, `config.json` y `historical_schema_map.json` presentes, sin secretos expuestos.
- [ ] PostgreSQL restaurado; 10 tablas y conteos exactos de ese corte.
- [ ] SQLite consistente; cantidad e identificadores reconciliados.
- [ ] Rutas, permisos UNC y cuenta Windows validados.
- [ ] Graph/SMTP validados de forma controlada.
- [ ] Primer ciclo ejecutado solo en destino y resultados supervisados.
- [ ] Decisión de aceptación o reversión registrada.

## 11. Problemas frecuentes y respuesta

| Síntoma | Posible causa | Revisión segura |
|---|---|---|
| `password authentication failed` | Contraseña/rol erróneo | Verificar host, puerto y cuenta; no editar `pg_hba.conf` prematuramente |
| `pg_dump` versión incompatible | Cliente más viejo que servidor | Instalar cliente compatible con versión del servidor |
| Import `psycopg2` fallido | El proyecto actual usa `psycopg` | Usar `requirements.txt`; no añadir `psycopg2` por inercia |
| Rutas `C:\Users\scardenas` no existen | Cambio de usuario | Ajustar tarea/BAT y `.env`; rutas internas derivan del proyecto |
| SQLite con registros inferiores | Copia de otro momento | Detener ambos consumidores y reconciliar snapshots |
| Dump restaura, app falla por permisos | Roles/grants no exportados | Inventariar y aplicar permisos mínimos aprobados |
| Correo reprocesado | Segundo consumidor o SQLite antigua | Detener, reconciliar identificadores/hashes y estados |

## 12. Cierre documental y próximos ensayos

**VALIDADO:** preparación en copia, restauración lógica, conexión desde código, `pip check`, diagnóstico integral de solo lectura.  
**PENDIENTE:** instalación física, cuenta de ejecución, inicio/detención reales, test de Graph/SMTP sin efectos, corte y reversión con escrituras.  

La siguiente edición deberá incorporar el nombre real de la tarea programada y un ensayo de migración entre equipos sin datos de producción; nunca inventar estos elementos.
