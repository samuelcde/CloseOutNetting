> **Proyecto:** CloseOutNetting  
> **Documentación:** versión 1.7 — revisión editorial y preparación de publicación, 11 de noviembre de 2026  
> **Versión objetivo:** rama `main`, commit `3b06bb0` (limpieza).  
> **Alcance de la verificación:** los resultados de pruebas provienen de la conversación; algunos detalles del código proceden de una copia del proyecto disponible, anterior al commit objetivo. Confirmar diferencias con el repositorio actual antes de ejecutar procedimientos de escritura.  
> **Seguridad:** no incluir credenciales, `.env` ni bases financieras en repositorios o manuales.

# Índice y control documental

## Finalidad
Este paquete organiza la documentación que permitirá instalar, operar, mantener, ampliar y trasladar CloseOutNetting. Los manuales se prepararon tras la limpieza del proyecto registrada en el commit `3b06bb0`.

| Archivo | Propósito | Estado |
|---|---|---|
| `01_Instalacion_y_Arranque.md` | Instalación y arranque Windows | Ampliado; inicio automatizado requiere validar cuenta y tarea real |
| `02_Migracion_y_Reversion.md` | Migración a otro equipo | Restauración lógica probada; corte/reversión pendientes |
| `03_Arquitectura_y_Componentes.md` | Código, relaciones y flujo | Ampliado; confirmar funciones con el HEAD actual |
| `04_Cargue_Masivo_Historico.md` | Importaciones de Excel históricos | Argumentos confirmados en copia; verificar CLI actual |
| `05_Nuevas_Entidades_y_Mapeos.md` | Incorporación de entidades y columnas | Mapeos y validaciones descritos; requieren pruebas con archivos reales |
| `06_Operacion_y_Problemas.md` | Monitoreo, incidencias, recuperación | Ampliado |
| `07_Pruebas_y_Validacion.md` | Pruebas seguras y demostraciones | Ampliado: matriz, guiones y evidencias |

## Convención de evidencias
- **VALIDADO:** ejecutado con éxito y resultado comunicado.
- **OBSERVADO EN CÓDIGO:** identificado en extractos o copia fuente, sin ejecución en la versión objetivo.
- **PENDIENTE:** procedimiento que requiere autorización, validación funcional o información del equipo destino.

## Control de cambios
| Fecha | Versión | Registro |
|---|---|---|
| 2026-11-09 | 1.0 inicial | Manuales redactados; base de referencia commit `3b06bb0` |

## Expediente de evidencias
Conservar fuera del repositorio, con acceso restringido, las bitácoras de migración, resultados de pruebas, logs, los manifiestos SHA-256 y respaldos de PostgreSQL y SQLite. Evitar publicar identificadores de mensajes, rutas de red o credenciales cuando no sean necesarios.

## Orden sugerido de lectura
Arquitectura → Instalación → Operación → Cargue histórico → Nuevas entidades → Pruebas → Migración y reversión.

## Actualización de la versión 1.1 (10/11/2026)
Se amplió el Manual 03 con explicaciones por archivo, dependencias, situaciones de modificación y pruebas conservadas. El resto de manuales mantiene su contenido de la edición inicial.

## Actualización de la versión 1.2 (10/11/2026)
Se amplió el Manual 04 con responsabilidades por archivo, diferencia entre inventarios, comandos de simulación y ejecución, verificaciones PostgreSQL, tratamiento de duplicados y puntos pendientes de contraste.

## Actualización de la versión 1.3 (10/11/2026)
Se amplió el Manual 05 con responsabilidades por archivo, escenarios de alta de entidades y columnas, matriz de mapeos, validaciones, reversión y controles previos al despliegue.

## Actualización de la versión 1.4 (11/11/2026)
Se amplió el Manual 02 con procedimientos por etapa, comandos de respaldo/restauración probados en ensayo, validaciones de cuenta Windows, plan de corte, criterios de aceptación y reversión antes/después de nuevas escrituras. El traslado físico y la reversión siguen pendientes de validación.

## Actualización de la versión 1.5 (11/11/2026)
Se amplió el Manual 06 con guía operativa, control de instancia y reinicios, consultas PostgreSQL y SQLite de solo lectura, matriz de incidencias, interpretación de `validation_issues`, monitoreo SMTP y procedimiento de recuperación. Manual 07 pendiente de ampliar.

## Actualización de la versión 1.6 (11/11/2026)
Se amplió el Manual 07 con matriz de 15 escenarios, guiones de demostración para OTROS y SET-ICAP, consultas sin escritura, regresiones, controles de tests existentes, evidencia de migración de práctica y acta de aceptación. Los siete manuales tienen una edición ampliada; falta revisión cruzada con el código publicado.

## Actualización de la versión 1.7 (11/11/2026)
Revisión editorial transversal, organización en `docs/` y entrega con instructivo de publicación Git. La comprobación técnica se realizó con una copia de código anterior a `3b06bb0`; **no equivale a certificación del HEAD remoto**. Se conserva la distinción VALIDADO / OBSERVADO EN CÓDIGO / PENDIENTE.
