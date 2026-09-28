class ConfigurationError(RuntimeError):
    """Configuración inválida o incompleta."""


class AuthenticationError(RuntimeError):
    """Error autenticando contra Microsoft Entra ID."""


class GraphApiError(RuntimeError):
    """Error comunicándose con Microsoft Graph."""


class SecurityValidationError(RuntimeError):
    """Una entrada no supera las validaciones de seguridad."""


class AttachmentTooLargeError(RuntimeError):
    """El adjunto supera el tamaño máximo permitido."""

class ReportParseError(RuntimeError):
    """No fue posible leer o normalizar un reporte descargado."""