# Revisión final y publicación en Git

**Proyecto:** CloseOutNetting  
**Edición documental:** 1.7 — 11/11/2026  
**Commit de referencia informado:** `3b06bb0`  

## Alcance de esta revisión

- Los siete manuales y el índice están presentes y se incluyen en la carpeta `docs/`.
- Se mantienen los resultados históricos del ensayo de restauración como **fotografía del 09/11/2026**, no como estado actual de producción.
- Se cotejaron algunos puntos de alto riesgo con la **copia anterior** `CloseOutNetting.zip`: `main.py`, `iniciar_closenetting.bat`, `historical_import.py`, `historical_reader.py` y `app/config.py`.
- Queda pendiente comparar todas las funciones, las consultas SQL y los comandos contra el checkout local actualizado de la rama `main`. No se ha ejecutado ningún proceso operativo ni realizado un ensayo de migración física.

## Confirmaciones obtenidas de la copia de código

1. `main.py --once` es un procesamiento real y **no es un dry-run**. Atención: en la copia inspeccionada, si se ejecuta durante un **fin de semana**, el flujo hace `continue` antes de llegar a la salida de `--once`; el proceso podría seguir en espera.
2. El BAT `iniciar_closenetting.bat` inicia `python -u main.py` y, si el código de salida es distinto de cero, lo reintenta tras 30 segundos. Antes del mantenimiento detener el BAT o su tarea programada.
3. `historical_import.py` requiere dos argumentos posicionales, `root` e `inventory_csv`. Sin `--commit` simula, y tiene filtros y opciones de reintento en la copia.
4. `historical_reader.py` interpreta un inventario **por hoja**, con atributos de estructura. No debe confundirse con el CSV de SHA-256 utilizado para recuperar `source_path`.
5. `app/config.py` resuelve `state/automation_state.db`, `.env`, `config.json` y `logs/` relativamente al directorio del proyecto.

## Puntos pendientes de verificación antes de un uso productivo

- Contrastar las rutas y funciones citadas con el commit desplegado (`git rev-parse HEAD`).
- Verificar que `historical_schema_map.json` mantiene los esquemas, alias y destinos descritos.
- Confirmar nombres de columnas PostgreSQL y resultados de las consultas contra el esquema desplegado.
- Validar tareas programadas, permisos UNC, gestión de secretos y políticas de respaldo con el equipo destino.
- Ensayar un corte y una reversión **sin dos consumidores simultáneos** de Outlook.
- Revisar manualmente scripts `test_*.py` antes de ejecutarlos: no todos son pruebas aisladas.

## Incorporar la documentación al repositorio local

El ZIP incluye una carpeta `docs/` lista para copiar en la raíz `C:\Users\scardenas\auto-closeoutnetting`. La publicación requiere ejecutar `git add`, `git commit` y `git push` **en la máquina del usuario**; el paquete no modifica su repositorio ni publica contenido por sí mismo.

```powershell
cd C:\Users\scardenas\auto-closeoutnetting
# Tras copiar aquí la carpeta docs del paquete:
git status --short
git add docs
git diff --cached --check
git diff --cached --stat
git commit -m "docs: incorporar manuales tecnicos CloseOutNetting v1.7"
git push origin main
git status -sb
```

Nunca incluir `.env`, archivos financieros, bases de datos ni registros confidenciales en el commit.
