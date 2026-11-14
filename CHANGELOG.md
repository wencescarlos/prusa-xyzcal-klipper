# Cambios

## Sin publicar
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
