# Cambios

## Sin publicar
- `get_status`: el resultado de la última `XYZCAL_CALIBRATE` (`skew_deg`, `xy_skew`, `verdict`, `bad_points`, distancias, escalas y error de cada círculo) se puede leer desde macros como `printer.prusa_xyzcal`.
- **Seguridad sin lámina** (en una MK3 real la boquilla tocó la cama durante `XYZCAL_CALIBRATE` con `z_floor: 1.0`):
  - Búsqueda gruesa: los puntos vecinos ya no bajan hasta `z_floor`; como mucho `coarse_window` (0.3 mm, configurable) por debajo de la mejor Z de disparo medida. Fuera del círculo, sin lámina, el PINDA apenas se activa y antes cada vecino bajaba hasta el límite.
  - Nuevo `nozzle_contact_z` (Z a la que la boquilla toca la cama sin lámina, medida con papel) y `min_clearance` (0.15): ninguna bajada pasa de `nozzle_contact_z + min_clearance`, aunque `z_floor` sea más bajo.
  - Si el barrido lateral de los bordes tuviera que ir por debajo de ese límite (PINDA demasiado alto respecto a la boquilla), `scan_dot` para con un error claro en vez de arrastrar la boquilla.
  - `XYZCAL_ZTEST` informa del margen que tendrá la boquilla al barrer, o avisa si falta `nozzle_contact_z`.
  - README: nueva prueba del papel (Parte 3, paso 7), aviso de que sin lámina el PINDA apenas detecta fuera de los círculos, y ejemplos con `Z_FLOOR` más prudentes.
  - Tests: 5 pruebas nuevas en `test_simulacion.py` que reproducen el contacto (cama sin lámina, círculo de radio 4,5 mm, contacto a Z=1.25) y comprueban que ya no ocurre.
- Si un comando falla por un error no previsto (MCU, límites, G28), la boquilla vuelve siempre a `z_start` y se sincroniza la posición de G-code.
- `z_floor` vale 1.0 por defecto (antes -0.4) y se comprueba contra el `position_min` de Z antes de mover.
- Los puntos o alturas fuera del recorrido dan error en vez de recortarse en silencio.
- `XYZCAL_LIMITS` restaura el límite del eje justo después del movimiento, aunque falle el `G28`.
- Los offsets de la sonda se piden al objeto `probe` (vale también para `[bltouch]` y otras secciones).
- `XYZCAL_GOTO` ya no exige sonda.
- Mensajes claros si faltan funciones internas de Klipper (`_update_skew`, `kin.limits`).
- `fit_surface` ya no puede dividir por cero con casi todos los puntos anómalos.
- Nuevo `tests/test_klipper.py` para la integración con Klipper.

## 0.1.0
- Primera versión publicada.
- Comandos: `XYZCAL_GOTO`, `XYZCAL_ZTEST`, `XYZCAL_POINT`, `XYZCAL_CALIBRATE`, `XYZCAL_APPLY`, `XYZCAL_LOCATE`, `XYZCAL_MAGNETS`, `XYZCAL_ZSCALE`, `XYZCAL_LIMITS`.
- `install.sh` con enlace simbólico y soporte para `update_manager` de Moonraker.
- Tests con simulador (`tests/test_simulacion.py`).
