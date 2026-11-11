# Manual 01 — Instalación y puesta en marcha de CloseOutNetting (Windows)

**Edición:** 1.2 · 10 de noviembre de 2026  
**Versión de referencia:** `main`, commit `3b06bb0` (limpieza del proyecto).  
**Alcance:** instalación inicial, preparación de un nuevo equipo y arranque controlado.  
**Nivel de verificación:** la instalación de práctica fue validada en noviembre de 2026. Los ejemplos del BAT y `main.py` proceden de una copia de código facilitada previamente; verificar contra el repositorio actual antes de habilitar producción.

> **Advertencia:** `main.py --once` **procesa correos reales**; no es un modo de simulación. No iniciar dos equipos que consulten simultáneamente el mismo buzón operativo. Conservar los respaldos y un plan de reversión.

## 1. ¿Qué instala este procedimiento?

CloseOutNetting consulta adjuntos de correo mediante Microsoft Graph, clasifica remitentes y archivos, guarda Excel en un directorio configurado, normaliza información para PostgreSQL y conserva el estado de adjuntos en SQLite. Envía resúmenes operativos mediante SMTP. Los módulos esenciales son `main.py`, `app/`, `config.json`, `.env`, `historical_schema_map.json` y `state/automation_state.db`.

**Dos escenarios distintos:**

- **Instalación nueva:** crear la base PostgreSQL y preparar una SQLite inicial solamente siguiendo el esquema de la versión instalada. Este manual **no presupone** que `pg_restore` cree una base desde cero sin un respaldo o DDL.
- **Traslado de instalación existente:** restaurar PostgreSQL y la SQLite de un respaldo coordinado, conservando el estado de mensajes ya procesados. Consultar el Manual 02 para corte y reversión.

## 2. Requisitos previos

| Elemento | Referencia comprobada | Verificar en destino |
|---|---|---|
| Windows | Equipo de pruebas con PowerShell | Versión, políticas y cuenta de ejecución |
| Python | 3.14.7 | `py -3.14 --version` |
| PostgreSQL | 16.15 | Versión servidor y herramientas cliente compatibles |
| Git | 2.55.0.windows.3 en pruebas | Acceso al repositorio y commit correcto |
| Dependencias | `requirements.txt` | `pip install` y `pip check` |
| Red | Microsoft Graph, SMTP y recurso compartido | DNS, proxy, firewall y permisos reales |
| Bases | PostgreSQL + SQLite | Respaldo, restauración y validación |

Los paquetes declarados en la copia consultada de `requirements.txt` son `msal`, `requests`, `python-dotenv`, `urllib3`, `pandas`, `openpyxl`, `xlrd`, `msoffcrypto-tool` y `psycopg[binary]`. **Consultar el `requirements.txt` del commit desplegado** porque es la fuente de verdad.

## 3. Elegir la carpeta de instalación

No es necesario que el nuevo equipo tenga el usuario Windows `scardenas`. Se recomienda una carpeta estable, por ejemplo `C:\Aplicaciones\CloseOutNetting`, con acceso restringido a administradores y a la cuenta que ejecutará el proceso.

El código revisado calcula rutas internas a partir de la ubicación del proyecto:

```python
BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
STATE_DB = STATE_DIR / "automation_state.db"
```

Esto hace que SQLite sea **relativa al proyecto**. Sin embargo, los valores de `.env`, la tarea programada y accesos a recursos compartidos deben validarse aparte.

## 4. Obtener el proyecto y comprobar su versión

Una opción es clonar el repositorio aprobado mediante Git. Sustituir el marcador de URL por la dirección real del repositorio; no introducir credenciales dentro del comando.

```powershell
$destino = 'C:\Aplicaciones\CloseOutNetting'
# git clone <URL_DEL_REPOSITORIO_AUTORIZADO> $destino
Set-Location $destino
git status --short
git log -1 --oneline
```

**No ejecutar el ejemplo comentado hasta disponer de la URL autorizada.** Si se traslada una copia del directorio, excluir `venv`, caches y temporales, pero conservar los archivos operativos y las configuraciones que correspondan. La copia del proyecto y el traslado de datos son pasos separados.

## 5. Crear el entorno Python

Desde la raíz del proyecto:

```powershell
py -3.14 -m venv venv
.\venv\Scripts\python.exe --version
.\venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Falló la instalación de dependencias' }
.\venv\Scripts\python.exe -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependencias inconsistentes' }
```

No copiar `venv` de un equipo a otro. Para reproducibilidad, comparar también las versiones instaladas contra el inventario de dependencias del equipo origen, si está disponible.

## 6. Configurar `.env` de forma segura

Este archivo contiene secretos y **no debe subirse a Git, adjuntarse a tickets públicos ni copiarse a documentación**. Durante una migración, transferirlo por un medio seguro y modificar únicamente parámetros que cambien de servidor o cuenta. Establecer ACL de Windows apropiadas.

| Variables | Propósito | Revisión |
|---|---|---|
| `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET` | Autenticación Microsoft Graph | Permisos y vigencia de credenciales |
| `MAILBOX_USER` | Buzón monitorizado | Confirmar buzón de producción |
| `DOWNLOAD_DIR` | Raíz donde guardar adjuntos | Ruta UNC y permisos de cuenta |
| `REPORT_PASSWORDS` | Contraseñas para reportes protegidos | Formato, conservación segura |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_USE_TLS` | Notificaciones por correo | Conectividad y autenticación |
| `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD` | PostgreSQL | Base y servidor de destino correctos |

**No asumir que cambiar `MAILBOX_USER` desactiva Outlook**; para pruebas controladas, no iniciar el procesador. `app/config.py` emplea `load_dotenv(..., override=False)` y `app/postgres_db.py` carga variables del entorno: variables preexistentes en Windows pueden tener preferencia frente al `.env`.

Para conocer solo las claves, sin mostrar contraseñas:

```powershell
Get-Content .\.env |
  Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*\s*=' } |
  ForEach-Object { ($_ -split '=',2)[0].Trim() }
```

## 7. Revisar `config.json` y el mapeo

`config.json` contiene configuración operativa: dominios→entidades (`entities`), `default_entity` (habitualmente `OTROS`), intervalo de consulta, zona horaria, extensiones, límites, destinatarios de alerta y parámetros del parser.

`historical_schema_map.json` **no pertenece solo al importador masivo**: `app/business_normalizer.py` lo consulta durante la normalización de FICC, CITI, BLADEX, BBVA, SET-ICAP y STANDARD, entre otros casos previstos en el código. **No excluirlo al desplegar.**

Comprobación estructural sin mostrar valores:

```powershell
.\venv\Scripts\python.exe -m json.tool .\config.json > $null
if ($LASTEXITCODE -ne 0) { throw 'config.json inválido' }
.\venv\Scripts\python.exe -m json.tool .\historical_schema_map.json > $null
if ($LASTEXITCODE -ne 0) { throw 'historical_schema_map.json inválido' }
```

## 8. Restaurar o preparar las bases de datos

### PostgreSQL

La práctica confirmó que un respaldo `pg_dump -Fc -n closenetting` de `closenetting_dev` se restauró sin errores en `closenetting_practica` utilizando PostgreSQL 16.15. Para una migración real, usar **un respaldo final recién generado después de detener la automatización**, no el respaldo de noviembre de 2026.

Ejemplo de restauración **solo en una base de destino nueva y vacía**, después de confirmar host, credenciales y que la base es la correcta:

```powershell
$pgBin = 'C:\Program Files\PostgreSQL\16\bin'
$backup = 'C:\RUTA_SEGURA\closenetting_backup.dump'
# Reemplazar NOMBRE_DB_DESTINO por la base validada y existente.
# & "$pgBin\pg_restore.exe" -h localhost -p 5432 -U postgres `
#     -d NOMBRE_DB_DESTINO --no-owner --no-acl `
#     --exit-on-error --single-transaction -W $backup
```

**No ejecutar sobre la base original.** Revisar extensiones, roles y dependencias fuera del esquema; el backup con `-n closenetting` no garantiza incluirlas.

### SQLite

El archivo `state/automation_state.db` conserva el historial de adjuntos. En la práctica, la copia respaldada contenía 138 registros en `processed_attachments`, con `PRAGMA integrity_check = ok`. Restaurar una copia tomada en una ventana coordinada con PostgreSQL.

Prueba de solo lectura:

```powershell
@'
from pathlib import Path
import sqlite3
p = Path('state/automation_state.db').resolve()
if not p.is_file(): raise SystemExit('Falta SQLite')
with sqlite3.connect(p.as_uri() + '?mode=ro', uri=True) as db:
    print('Integridad:', db.execute('PRAGMA integrity_check').fetchone()[0])
    print('Adjuntos:', db.execute('SELECT COUNT(*) FROM processed_attachments').fetchone()[0])
'@ | .\venv\Scripts\python.exe -
```

## 9. Validaciones sin iniciar Outlook

Antes de activar, comprobar las rutas y base reales mediante los módulos de la aplicación. Esta consulta no inicia `main.py`:

```powershell
@'
from pathlib import Path
from app.config import BASE_DIR, STATE_DB, load_settings, load_operational_config
from app.postgres_db import get_connection
settings = load_settings()
config = load_operational_config()
print('Proyecto:', BASE_DIR)
print('SQLite:', STATE_DB)
print('Descargas:', settings.download_dir)
print('Entidades configuradas:', len(config.entities))
with get_connection() as conn:
    try:
        with conn.cursor() as cur:
            cur.execute('SET TRANSACTION READ ONLY')
            cur.execute('SELECT current_database(), current_user')
            print('PostgreSQL:', cur.fetchone())
    finally:
        conn.rollback()
'@ | .\venv\Scripts\python.exe -
```

**Verificar manualmente** que la base mostrada y la ruta de descargas sean las previstas. Esta comprobación no garantiza conectividad Graph, capacidad SMTP ni permisos de escritura al recurso UNC.

En la práctica del 9 de noviembre se ejecutó también `diagnostico_migracion.py` (creado en la carpeta de práctica, no necesariamente versionado), con resultado satisfactorio para 5.718 ingestas, 5.649 rutas y 138 adjuntos SQLite. Son valores **históricos de referencia**, no cantidades que deban exigirse en producción futura.

## 10. Arranque operativo y programación Windows

En la copia consultada, `iniciar_closenetting.bat`:

1. Cambia al directorio propio con `cd /d "%~dp0"`.
2. Comprueba y activa `venv\Scripts\activate.bat`.
3. Ejecuta `python -u main.py`.
4. Si termina con código distinto de cero, espera 30 segundos e intenta reiniciar.
5. Si termina con código cero, sale del bucle (el BAT termina con `pause`).

`main.py` carga configuración, comprueba el destino de descargas, inicializa SQLite y utiliza un `SingleInstanceLock` local en `state/auto-closenetting.lock`. Durante días hábiles puede llamar a `process_cycle()`; en fines de semana espera el siguiente intervalo. **El bloqueo de instancia es local al directorio y no impide que dos máquinas ejecuten simultáneamente la automatización.**

La tarea programada de Windows **no se ha validado suficientemente en los datos aportados**. Antes del despliegue real, documentar su nombre, trigger, cuenta, acciones, directorio de trabajo, política de reintento y permisos. Evitar usar un BAT con `pause` en contexto no interactivo sin comprobar el comportamiento. No copiar ciegamente la configuración de una tarea anterior.

## 11. Activación controlada (producción)

1. Acordar ventana de corte y responsables.
2. Deshabilitar la tarea del equipo anterior, detener el proceso y confirmar que no hay ciclo en curso.
3. Generar respaldos finales de PostgreSQL y SQLite; protegerlos y registrar hash, fecha y origen.
4. Restaurar en destino y verificar conteos, esquema, integridad SQLite y `source_path`.
5. Verificar cuenta ejecutora, rutas UNC, acceso Graph y SMTP mediante pruebas autorizadas.
6. Confirmar que solo **una** instalación queda habilitada para consultar el buzón.
7. Activar la automatización nueva bajo supervisión; comprobar el primer ciclo, notificaciones y logs.
8. Documentar evidencia y decidir formalmente aceptación o reversión.

Si el equipo nuevo ya procesó correos, **no se debe simplemente reactivar el antiguo con su SQLite anterior**: primero hay que conciliar los mensajes, archivos y registros producidos en el nuevo entorno para evitar duplicados o pérdidas.

## 12. Diagnóstico de fallos habituales

| Síntoma | Revisión prioritaria |
|---|---|
| `ModuleNotFoundError` | Entorno `venv` correcto, `pip install -r requirements.txt`, imports actuales |
| Fallo de autenticación PostgreSQL | Servidor/puerto/usuario/contraseña y variables de entorno; no cambiar `pg_hba.conf` de entrada |
| SQLite ausente | Ruta del proyecto, archivo en `state/`, permisos de la cuenta |
| Error de escritura Excel | `DOWNLOAD_DIR`, acceso UNC desde la cuenta de tarea, bloqueo de archivo |
| Gmail no mapeado en `OTROS` | `default_entity`, reglas `entities`, registro `unknown_entity`, resumen SMTP |
| Columna no mapeada | `historical_schema_map.json`, `business_normalizer.py`, `validation_issues` |
| No llegan notificaciones | SMTP, alert_recipients, logs y conectividad |
| No ejecuta en fin de semana | Comportamiento de `main.py`; revisar día e intervalo |

## 13. Lista de aceptación

- [ ] Se verificó commit y versión de Python.
- [ ] Dependencias instaladas y `pip check` satisfactorio.
- [ ] `.env` privado y `config.json`/mapeo presentes y válidos.
- [ ] PostgreSQL restaurado o inicializado según procedimiento autorizado; conexión a base correcta.
- [ ] SQLite íntegro y consistente con la instantánea PostgreSQL.
- [ ] Directorio de descargas y recurso UNC accesibles para la cuenta de ejecución.
- [ ] Tarea programada documentada, con cuenta y permisos válidos.
- [ ] Instalación anterior deshabilitada antes de arrancar la nueva.
- [ ] Pruebas externas y primer ciclo controlado aprobados.
- [ ] Bitácora de ejecución y procedimiento de reversión disponibles.

**Documentos relacionados:** Manual 02 (migración y reversión), Manual 03 (arquitectura), Manual 04 (cargue histórico), Manual 05 (nuevas entidades), Manual 06 (operación) y Manual 07 (pruebas).
