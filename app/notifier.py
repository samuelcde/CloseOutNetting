from __future__ import annotations



import html

import logging

import smtplib

import ssl

from dataclasses import dataclass

from datetime import date, datetime

from email.mime.multipart import MIMEMultipart

from email.mime.text import MIMEText

from pathlib import Path



from .config import SmtpSettings





logger = logging.getLogger(__name__)





# ============================================================

# EXCEPCIONES

# ============================================================



class NotificationError(RuntimeError):

    """

    Error enviando una notificación SMTP.

    """



# ============================================================

# MODELO RESUMEN

# ============================================================



@dataclass(frozen=True, slots=True)

class DailyProcessSummary:

    """

    Información que posteriormente recibiremos desde

    processor.py al finalizar el procesamiento.

    """



    started_at: datetime

    finished_at: datetime



    target_date: date | None = None

    successful: bool = True



    messages_reviewed: int = 0

    files_downloaded: int = 0

    files_parsed: int = 0

    files_saved_to_database: int = 0

    duplicate_files: int = 0



    unknown_entity_files: int = 0

    oversized_files: int = 0

    parse_errors: int = 0

    structure_changes: tuple[str, ...] = ()



    details: tuple[str, ...] = ()



# ============================================================

# NOTIFICADOR SMTP

# ============================================================



class EmailNotifier:

    def __init__(self, smtp_settings: SmtpSettings, recipients: tuple[str, ...]) -> None:

        if not recipients:

            raise ValueError("Debe existir al menos un " "destinatario de alertas.")



        self.smtp = smtp_settings

        self.recipients = recipients



    # ========================================================

    # PRUEBA SMTP

    # ========================================================



    def send_test(self) -> None:

        """

        Envía un correo simple para comprobar conectividad,

        TLS, autenticación y relay SMTP.

        """



        self._send(

            subject=("[Auto CloseNetting] " "Prueba de notificaciones SMTP"),



            plain_body=(

                "Prueba satisfactoria del módulo SMTP.\n\n"

                "Si recibió este mensaje, la aplicación pudo "

                "conectarse al servidor SMTP y despachar una "

                "notificación."

            ),



            html_body="""

            <html>

              <body>

                <h3>Auto CloseNetting</h3>



                <p>

                  Prueba satisfactoria del módulo

                  <strong>SMTP</strong>.

                </p>



                <p>

                  Si recibió este mensaje, la aplicación pudo

                  conectarse al servidor SMTP y despachar una

                  notificación.

                </p>

              </body>

            </html>

            """,

        )



    # ========================================================

    # 1. ENTIDAD DESCONOCIDA

    # ========================================================



    def notify_unknown_entity(

        self,

        *,

        sender_address: str,

        filename: str,

        saved_path: Path,

    ) -> None:

        sender_safe = html.escape(sender_address)



        filename_safe = html.escape(filename)



        path_safe = html.escape(str(saved_path))



        subject = "[Auto CloseNetting] " "Reporte recibido de entidad no configurada"



        plain_body = (

            "Se recibió un reporte desde un remitente cuyo "

            "dominio no está configurado.\n\n"

            f"Remitente: {sender_address}\n"

            f"Archivo: {filename}\n"

            f"Ubicación: {saved_path}\n"

            "Entidad asignada: OTROS\n\n"

            "El archivo fue descargado correctamente, pero "

            "requiere validación para determinar si debe "

            "agregarse una nueva entidad al config.json."

        )



        html_body = f"""

        <html>

          <body>

            <h3>Entidad no configurada</h3>



            <p>

              Se recibió un reporte desde un remitente cuyo

              dominio no está configurado.

            </p>



            <table border="1"

                   cellpadding="5"

                   cellspacing="0">

              <tr>

                <td><strong>Remitente</strong></td>

                <td>{sender_safe}</td>

              </tr>



              <tr>

                <td><strong>Archivo</strong></td>

                <td>{filename_safe}</td>

              </tr>



              <tr>

                <td><strong>Ubicación</strong></td>

                <td>{path_safe}</td>

              </tr>



              <tr>

                <td><strong>Entidad asignada</strong></td>

                <td>OTROS</td>

              </tr>

            </table>



            <p>

              El archivo fue descargado correctamente,

              pero requiere validación para determinar si

              el dominio debe incorporarse a la configuración.

            </p>

          </body>

        </html>

        """



        self._send(subject=subject, plain_body=plain_body, html_body=html_body)



    # ========================================================

    # 2. ARCHIVO DEMASIADO GRANDE

    # ========================================================



    def notify_attachment_too_large(

        self,

        *,

        sender_address: str,

        filename: str,

        size_bytes: int,

        max_size_bytes: int,

    ) -> None:

        subject = "[Auto CloseNetting] " "Adjunto rechazado por tamaño"



        actual_size = _format_bytes(size_bytes)



        maximum_size = _format_bytes(max_size_bytes)



        plain_body = (

            "Un archivo recibido por Auto CloseNetting "

            "fue rechazado porque supera el límite "

            "permitido.\n\n"

            f"Remitente: {sender_address}\n"

            f"Archivo: {filename}\n"

            f"Tamaño detectado: {actual_size}\n"

            f"Límite permitido: {maximum_size}\n\n"

            "El archivo no fue procesado."

        )



        html_body = f"""

        <html>

          <body>

            <h3>Adjunto rechazado por tamaño</h3>



            <table border="1"

                   cellpadding="5"

                   cellspacing="0">

              <tr>

                <td><strong>Remitente</strong></td>

                <td>{html.escape(sender_address)}</td>

              </tr>



              <tr>

                <td><strong>Archivo</strong></td>

                <td>{html.escape(filename)}</td>

              </tr>



              <tr>

                <td><strong>Tamaño detectado</strong></td>

                <td>{html.escape(actual_size)}</td>

              </tr>



              <tr>

                <td><strong>Límite permitido</strong></td>

                <td>{html.escape(maximum_size)}</td>

              </tr>

            </table>



            <p>

              El archivo no fue procesado.

            </p>

          </body>

        </html>

        """



        self._send(subject=subject, plain_body=plain_body, html_body=html_body)



    # ========================================================

    # 3. ERROR DE PARSEO

    # ========================================================



    def notify_report_parse_error(

        self,

        *,

        entity: str,

        filename: str,

        file_path: Path,

        error_message: str,

    ) -> None:

        subject = "[Auto CloseNetting] " "Error procesando reporte"



        plain_body = (

            "Auto CloseNetting no pudo abrir, descifrar "

            "o interpretar un reporte.\n\n"

            f"Entidad: {entity}\n"

            f"Archivo: {filename}\n"

            f"Ubicación: {file_path}\n"

            f"Error: {error_message}\n\n"

            "El archivo permanece disponible para "

            "revisión manual."

        )



        html_body = f"""

        <html>

          <body>

            <h3>Error procesando reporte</h3>



            <table border="1"

                   cellpadding="5"

                   cellspacing="0">

              <tr>

                <td><strong>Entidad</strong></td>

                <td>{html.escape(entity)}</td>

              </tr>



              <tr>

                <td><strong>Archivo</strong></td>

                <td>{html.escape(filename)}</td>

              </tr>



              <tr>

                <td><strong>Ubicación</strong></td>

                <td>{html.escape(str(file_path))}</td>

              </tr>



              <tr>

                <td><strong>Error</strong></td>

                <td>{html.escape(error_message)}</td>

              </tr>

            </table>



            <p>

              El archivo permanece disponible para

              revisión manual.

            </p>

          </body>

        </html>

        """



        self._send(subject=subject, plain_body=plain_body, html_body=html_body)



    # ========================================================

    # 4. RESUMEN DEL PROCESO

    # ========================================================



    def notify_daily_summary(self, summary: DailyProcessSummary) -> None:

        """

        Envía un resumen operativo compacto del procesamiento.

        """



        duration = summary.finished_at - summary.started_at

        total_seconds = max(0, int(duration.total_seconds()))



        hours, remainder = divmod(total_seconds, 3600)

        minutes, seconds = divmod(remainder, 60)



        if hours:

            duration_text = f"{hours} h {minutes} min {seconds} s"

        elif minutes:

            duration_text = f"{minutes} min {seconds} s"

        else:

            duration_text = f"{seconds} s"



        target_date_text = (

            summary.target_date.strftime("%d/%m/%Y")

            if summary.target_date is not None

            else "No disponible"

        )



        started_text = summary.started_at.strftime("%d/%m/%Y %H:%M:%S")

        finished_text = summary.finished_at.strftime("%d/%m/%Y %H:%M:%S")



        status_text = (

            "FINALIZADO CORRECTAMENTE"

            if summary.successful

            else "FINALIZADO CON ERROR"

        )



        # --------------------------------------------------------

        # TEXTO PLANO

        # --------------------------------------------------------



        if summary.details:

            details_plain = "\n".join(

                f"- {detail}"

                for detail in summary.details

            )

        else:

            details_plain = "Sin incidencias."



        if summary.structure_changes:

            structure_plain = "\n".join(

                f"- {change}"

                for change in summary.structure_changes

            )

        else:

            structure_plain = "Sin cambios de estructura."



        plain_body = (

            "AUTO CLOSENETTING\n"

            "Resumen de procesamiento\n\n"



            f"Estado: {status_text}\n"

            f"Día hábil procesado: {target_date_text}\n"

            f"Correos revisados: {summary.messages_reviewed}\n\n"



            "Resultado\n"

            f"- Archivos transferidos desde Graph: {summary.files_downloaded}\n"

            f"- Archivos interpretados correctamente: {summary.files_parsed}\n"

            f"- Guardados en base de datos: {summary.files_saved_to_database}\n"

            f"- Duplicados omitidos: {summary.duplicate_files}\n"

            f"- Enviados a OTROS: {summary.unknown_entity_files}\n"

            f"- Rechazados por tamaño: {summary.oversized_files}\n"

            f"- Errores de lectura: {summary.parse_errors}\n"

            f"- Cambios de estructura detectados: "
            f"{len(summary.structure_changes)}\n\n"



            f"Ejecución: {started_text} - {finished_text}\n"

            f"Duración: {duration_text}\n\n"



            "Cambios de estructura detectados\n"

            f"{structure_plain}\n\n"



            "Incidencias\n"

            f"{details_plain}"

        )



        # --------------------------------------------------------

        # HTML

        # --------------------------------------------------------



        status_color = "#1B5E20" if summary.successful else "#B42318"

        status_background = "#EEF7EE" if summary.successful else "#FDECEC"



        if summary.details:

            details_html = "".join(

                f"""

                <div style="

                    padding: 6px 0;

                    border-bottom: 1px solid #E5E7EB;

                    font-size: 12px;

                    line-height: 1.45;

                ">

                    {

                        html.escape(detail)

                        .replace(" | ", "<br>")

                    }

                </div>

                """

                for detail in summary.details

            )

        else:

            details_html = """

            <div style="

                font-size: 12px;

                color: #4B5563;

            ">

                Sin incidencias.

            </div>

            """



        if summary.structure_changes:

            structure_html = "".join(

                f"""

                <div style="
                    padding: 6px 0;
                    border-bottom: 1px solid #F3D7A5;
                    font-size: 12px;
                    line-height: 1.45;
                "">

                    {
                        html.escape(change)
                        .replace(" | ", "<br>")
                    }

                </div>

                """

                for change in summary.structure_changes

            )

        else:

            structure_html = """

            <div style="
                font-size: 12px;
                color: #4B5563;
            "">

                Sin cambios de estructura.

            </div>

            """



        html_body = f"""

        <html>

          <body style="

              margin: 0;

              padding: 20px;

              background-color: #F5F6F7;

              font-family: Arial, Helvetica, sans-serif;

              color: #222222;

          ">



            <table role="presentation"

                   width="620"

                   cellpadding="0"

                   cellspacing="0"

                   align="center"

                   style="

                       width: 620px;

                       max-width: 100%;

                       background-color: #FFFFFF;

                       border: 1px solid #DADDE1;

                       border-collapse: collapse;

                   ">



              <!-- CABECERA -->

              <tr>

                <td style="

                    padding: 16px 20px;

                    border-bottom: 1px solid #DADDE1;

                ">

                  <div style="

                      font-size: 17px;

                      font-weight: bold;

                  ">

                    Auto CloseNetting

                  </div>



                  <div style="

                      margin-top: 3px;

                      font-size: 12px;

                      color: #6B7280;

                  ">

                    Resumen de procesamiento

                  </div>

                </td>

              </tr>



              <!-- CONTENIDO -->

              <tr>

                <td style="padding: 16px 20px 20px 20px;">



                  <!-- ESTADO -->

                  <div style="

                      display: inline-block;

                      padding: 4px 8px;

                      margin-bottom: 14px;

                      background-color: {status_background};

                      color: {status_color};

                      font-size: 11px;

                      font-weight: bold;

                  ">

                    {html.escape(status_text)}

                  </div>



                  <!-- PERIODO -->

                  <table role="presentation"

                         width="100%"

                         cellpadding="0"

                         cellspacing="0"

                         style="

                             border-collapse: collapse;

                             font-size: 13px;

                         ">



                    <tr>

                      <td style="

                          padding: 5px 0;

                          width: 60%;

                          color: #4B5563;

                      ">

                        Día hábil procesado

                      </td>

                      <td style="

                          padding: 5px 0;

                          font-weight: bold;

                      ">

                        {html.escape(target_date_text)}

                      </td>

                    </tr>



                    <tr>

                      <td style="

                          padding: 5px 0;

                          color: #4B5563;

                      ">

                        Correos revisados

                      </td>

                      <td style="

                          padding: 5px 0;

                          font-weight: bold;

                      ">

                        {summary.messages_reviewed}

                      </td>

                    </tr>



                  </table>



                  <div style="

                      margin: 14px 0 8px 0;

                      padding-top: 12px;

                      border-top: 1px solid #E5E7EB;

                      font-size: 13px;

                      font-weight: bold;

                  ">

                    Resultado

                  </div>



                  <!-- RESULTADO -->

                  <table role="presentation"

                         width="100%"

                         cellpadding="0"

                         cellspacing="0"

                         style="

                             border-collapse: collapse;

                             font-size: 13px;

                         ">



                    <tr>

                      <td style="padding:4px 0;color:#4B5563;">

                        Archivos transferidos desde Graph

                      </td>

                      <td align="right" style="padding:4px 0;font-weight:bold;">

                        {summary.files_downloaded}

                      </td>

                    </tr>



                    <tr>

                      <td style="padding:4px 0;color:#4B5563;">

                        Archivos interpretados correctamente

                      </td>

                      <td align="right" style="padding:4px 0;font-weight:bold;">

                        {summary.files_parsed}

                      </td>

                    </tr>



                    <tr>

                      <td style="padding:4px 0;color:#4B5563;">

                        Guardados en base de datos

                      </td>

                      <td align="right" style="padding:4px 0;font-weight:bold;">

                        {summary.files_saved_to_database}

                      </td>

                    </tr>



                    <tr>

                      <td style="padding:4px 0;color:#4B5563;">

                        Duplicados omitidos

                      </td>

                      <td align="right" style="padding:4px 0;font-weight:bold;">

                        {summary.duplicate_files}

                      </td>

                    </tr>



                    <tr>

                      <td style="padding:4px 0;color:#4B5563;">

                        Enviados a OTROS

                      </td>

                      <td align="right" style="padding:4px 0;font-weight:bold;">

                        {summary.unknown_entity_files}

                      </td>

                    </tr>



                    <tr>

                      <td style="padding:4px 0;color:#4B5563;">

                        Rechazados por tamaño

                      </td>

                      <td align="right" style="padding:4px 0;font-weight:bold;">

                        {summary.oversized_files}

                      </td>

                    </tr>



                    <tr>

                      <td style="padding:4px 0;color:#4B5563;">

                        Errores de lectura

                      </td>

                      <td align="right" style="padding:4px 0;font-weight:bold;">

                        {summary.parse_errors}

                      </td>

                    </tr>



                    <tr>

                      <td style="padding:4px 0;color:#4B5563;">

                        Cambios de estructura detectados

                      </td>

                      <td align="right" style="padding:4px 0;font-weight:bold;">

                        {len(summary.structure_changes)}

                      </td>

                    </tr>



                  </table>



                  <!-- EJECUCION -->

                  <div style="

                      margin-top: 14px;

                      padding-top: 12px;

                      border-top: 1px solid #E5E7EB;

                      font-size: 12px;

                      color: #6B7280;

                      line-height: 1.6;

                  ">

                    <strong style="color:#374151;">Ejecución:</strong>

                    {html.escape(started_text)}

                    &nbsp;·&nbsp;

                    {html.escape(duration_text)}

                  </div>



                  <!-- CAMBIOS DE ESTRUCTURA -->

                  <div style="
                      margin: 14px 0 7px 0;
                      padding-top: 12px;
                      border-top: 1px solid #E5E7EB;
                      font-size: 13px;
                      font-weight: bold;
                      color: #92400E;
                  ">

                    Cambios de estructura detectados

                  </div>



                  {structure_html}



                  <!-- INCIDENCIAS -->

                  <div style="

                      margin: 14px 0 7px 0;

                      padding-top: 12px;

                      border-top: 1px solid #E5E7EB;

                      font-size: 13px;

                      font-weight: bold;

                  ">

                    Incidencias

                  </div>



                  {details_html}



                </td>

              </tr>



            </table>



          </body>

        </html>

        """



        self._send(

            subject=f"[Auto CloseNetting] Resumen {target_date_text}",

            plain_body=plain_body,

            html_body=html_body,

        )



    # ========================================================

    # ENVÍO SMTP

    # ========================================================



    def _send(self, *, subject: str, plain_body: str, html_body: str) -> None:

        # Defensa frente a inyección de encabezados.

        if "\r" in subject or "\n" in subject:

            raise NotificationError("El asunto contiene caracteres inválidos.")



        message = MIMEMultipart("alternative")



        message["Subject"] = subject

        message["From"] = self.smtp.user



        message["To"] = ", ".join(self.recipients)



        message.attach(MIMEText(plain_body, "plain", "utf-8"))



        message.attach(MIMEText(html_body, "html", "utf-8"))



        logger.info(

            "Enviando notificación SMTP: %s | "

            "destinatarios=%s",

            subject,

            len(self.recipients),

        )



        try:

            with smtplib.SMTP(

                host=self.smtp.host,

                port=self.smtp.port,

                timeout=self.smtp.timeout_seconds,

            ) as smtp:

                smtp.ehlo()



                if self.smtp.use_tls:

                    tls_context = (ssl.create_default_context())



                    smtp.starttls(context=tls_context)



                    smtp.ehlo()



                smtp.login(self.smtp.user, self.smtp.password)



                smtp.sendmail(self.smtp.user, list(self.recipients), message.as_string())



        except smtplib.SMTPAuthenticationError as exc:

            raise NotificationError("El servidor SMTP rechazó " "las credenciales.") from exc



        except smtplib.SMTPRecipientsRefused as exc:

            raise NotificationError("El servidor SMTP rechazó " "uno o más destinatarios.") from exc



        except smtplib.SMTPSenderRefused as exc:

            raise NotificationError(  

                "El servidor SMTP rechazó "

                "el remitente configurado."

            ) from exc



        except smtplib.SMTPNotSupportedError as exc:

            raise NotificationError(

                "El servidor SMTP no soporta "

                "una operación requerida "

                "(TLS o autenticación)."

            ) from exc



        except (smtplib.SMTPException, OSError) as exc:

            raise NotificationError(

                "No fue posible enviar la notificación "

                f"SMTP: {type(exc).__name__}: {exc}"

            ) from exc



        logger.info("Notificación SMTP enviada correctamente.")



# ============================================================

# UTILIDADES

# ============================================================



def _format_bytes(value: int) -> str:

    size = float(max(value, 0))



    units = ("B", "KB", "MB", "GB")



    for unit in units:

        if size < 1024 or unit == units[-1]:

            return (f"{size:.2f} {unit}")



        size /= 1024



    return f"{value} B"
