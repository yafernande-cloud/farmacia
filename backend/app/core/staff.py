"""Personal de la farmacia: tres conceptos SEPARADOS.

* pertenecer     → `app_user.active`      (sigue en el historial aunque se desactive; nunca se elimina)
* disponibilidad → `app_user.available`   (vacaciones, licencia, día libre u otro; puede reportar o no)
* sesión activa  → fila abierta en `actor_session` (quién está reportando AHORA en el teléfono compartido)

Solo una persona activa Y disponible Y con rol que permita reportar puede ser elegida en Telegram.
"""

from enum import StrEnum


class AbsenceReason(StrEnum):
    VACATION = "vacation"
    SICK_LEAVE = "sick_leave"
    DAY_OFF = "day_off"
    OTHER = "other"


ABSENCE_LABELS = {
    AbsenceReason.VACATION: "vacaciones",
    AbsenceReason.SICK_LEAVE: "licencia",
    AbsenceReason.DAY_OFF: "día libre",
    AbsenceReason.OTHER: "otro motivo",
}


def absence_label(reason: str | None) -> str | None:
    try:
        return ABSENCE_LABELS[AbsenceReason(reason)] if reason else None
    except ValueError:
        return reason
