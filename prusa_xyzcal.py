# prusa_xyzcal.py -- Calibración XYZ estilo Prusa MK3 para Klipper
#
# Mide con el PINDA/SuperPINDA los 4 puntos de calibración de la cama MK3
# (esquinas de la cama calefactora, SIN la lámina de acero) y calcula la
# desviación de perpendicularidad entre X e Y, igual que hace el firmware
# original de Prusa (mesh_bed_calibration.cpp).
#
# Basado en el algoritmo del Prusa-Firmware (GPLv3):
#   - búsqueda del punto: primer contacto en Z, bajar 0.1 mm, bordes en X y en Y,
#     repetir centrando y promediar (find_bed_induction_sensor_point_xy)
#   - ajuste por mínimos cuadrados Gauss-Newton con escala unidad
#     (calculate_machine_skew_and_offset_LS)
#   - umbrales de aviso: 0.12 grados (leve) y 0.25 grados (extremo)
#
# Diferencia con el firmware original: Prusa para el movimiento en cuanto el
# PINDA cambia de estado (parada por hardware). Aquí se hace por sondeo: se
# mueve en pasos pequeños, se consulta el sensor (como QUERY_PROBE) y se
# afina el borde por bisección. Es más lento pero no toca la configuración
# del endstop. (XYZCAL_APPLY y XYZCAL_LIMITS sí usan funciones internas de
# Klipper; si faltan, dan un error claro.)
#
# Comandos: XYZCAL_GOTO, XYZCAL_ZTEST, XYZCAL_POINT, XYZCAL_CALIBRATE,
# XYZCAL_APPLY, XYZCAL_LOCATE, XYZCAL_MAGNETS, XYZCAL_ZSCALE, XYZCAL_LIMITS.
#
# Instalación: ver README.md (install.sh enlaza este archivo en
# ~/klipper/klippy/extras/). Estado de las pruebas de cada comando: ver README.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import math

# ---------------------------------------------------------------------------
# Constantes tomadas del firmware de Prusa
# ---------------------------------------------------------------------------
SKEW_MILD = math.radians(0.12)      # bed_skew_angle_mild
SKEW_EXTREME = math.radians(0.25)   # bed_skew_angle_extreme
MAX_POINT_ERROR = 0.8               # BED_CALIBRATION_POINT_OFFSET_MAX_EUCLIDIAN
SCALE_TOL = 0.0005                  # +-0.05 %: no se sugiere cambiar nada
SPREAD_WARN = 0.05                  # mm de diferencia entre lados opuestos
LOCATE_WARN = 0.1                   # mm de dispersión entre rondas de LOCATE


class XYZCalError(Exception):
    pass


# ---------------------------------------------------------------------------
# Matemáticas puras (sin dependencias de Klipper, se pueden probar aparte)
# ---------------------------------------------------------------------------
def _solve_linear(A, b):
    """Resuelve A x = b por eliminación gaussiana con pivote parcial."""
    n = len(b)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            raise XYZCalError("Sistema singular en el ajuste (faltan datos)")
        M[col], M[piv] = M[piv], M[col]
        for r in range(col + 1, n):
            f = M[r][col] / M[col][col]
            for c in range(col, n + 1):
                M[r][c] -= f * M[col][c]
    x = [0.] * n
    for i in range(n - 1, -1, -1):
        s = M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))
        x[i] = s / M[i][i]
    return x


def fit_bed(measured, true_pts, wx=None, wy=None, max_iter=100):
    """Ajuste de mínimos cuadrados no lineal (Gauss-Newton), escala unidad.

    Modelo (idéntico al del firmware de Prusa):
        mundo_x = cos(a1)*mx - sin(a2)*my + cx
        mundo_y = sin(a1)*mx + cos(a2)*my + cy
    donde (mx, my) son las coordenadas de máquina medidas y (mundo) son las
    posiciones reales de los puntos en la cama. Por tanto:
        eje X de la máquina apunta a (cos a1, sin a1) en la cama
        eje Y de la máquina apunta a (-sin a2, cos a2) en la cama
    Devuelve a1, a2 (rad), cx, cy y los residuos por punto.
    """
    n = len(measured)
    if wx is None:
        wx = [1.] * n
    if wy is None:
        wy = [1.] * n
    cx = cy = a1 = a2 = 0.
    for _ in range(max_iter):
        c1, s1, c2, s2 = math.cos(a1), math.sin(a1), math.cos(a2), math.sin(a2)
        A = [[0.] * 4 for _ in range(4)]
        g = [0.] * 4
        for i in range(n):
            mx, my = measured[i]
            tx, ty = true_pts[i]
            rx = c1 * mx - s2 * my + cx - tx
            ry = s1 * mx + c2 * my + cy - ty
            jx = [1., 0., -s1 * mx, -c2 * my]
            jy = [0., 1., c1 * mx, -s2 * my]
            for r in range(4):
                for c in range(4):
                    A[r][c] += wx[i] * jx[r] * jx[c] + wy[i] * jy[r] * jy[c]
                g[r] += wx[i] * jx[r] * rx + wy[i] * jy[r] * ry
        h = _solve_linear(A, [-v for v in g])
        cx += h[0]
        cy += h[1]
        a1 += h[2]
        a2 += h[3]
        if max(abs(v) for v in h) < 1e-13:
            break
    c1, s1, c2, s2 = math.cos(a1), math.sin(a1), math.cos(a2), math.sin(a2)
    residuals = []
    for i in range(n):
        mx, my = measured[i]
        tx, ty = true_pts[i]
        ex = tx - (c1 * mx - s2 * my + cx)
        ey = ty - (s1 * mx + c2 * my + cy)
        residuals.append((ex, ey, math.hypot(ex, ey)))
    return {'a1': a1, 'a2': a2, 'cx': cx, 'cy': cy, 'residuals': residuals}


def xy_skew_factor(a1, a2):
    """Factor xy_skew de [skew_correction] de Klipper.

    Klipper corrige con: x_motor = x_gcode - y_gcode * factor. El eje Y real
    apunta a (-sin a2, cos a2) y el X real a (cos a1, sin a1); para que la
    cuadrícula impresa quede ortogonal hace falta factor = tan(a1 - a2).
    """
    return math.tan(a1 - a2)


def find_edge(sample, rearm, start, direction, max_span, coarse=1.0,
              tol=0.02):
    """Busca el borde de la zona activa del sensor a lo largo de un eje.

    sample(p): mueve el sensor a la coordenada escalar p y devuelve True si
    el sensor está activado.  rearm(): devuelve el sensor al centro de la
    zona activa (donde seguro está activado).  'start' es la coordenada del
    centro.

    Todas las lecturas se toman SALIENDO desde el centro hacia fuera (en la
    bisección se vuelve al centro antes de cada prueba), de modo que la
    histéresis del PINDA es siempre la misma y afecta por igual a los dos
    lados de un punto; al promediar los bordes opuestos se cancela.
    Devuelve la coordenada del borde o None si no se llega a él.
    """
    d = 1. if direction >= 0 else -1.
    if max_span <= 0:
        return None
    s_in = 0.
    s_out = None
    s = coarse
    while s < max_span:
        if not sample(start + d * s):
            s_out = s
            break
        s_in = s
        s += coarse
    if s_out is None:
        if not sample(start + d * max_span):
            s_out = max_span
        else:
            return None            # el sensor sigue activo en el límite
    while s_out - s_in > tol:
        mid = 0.5 * (s_in + s_out)
        rearm()
        if sample(start + d * mid):
            s_in = mid
        else:
            s_out = mid
    return start + d * 0.5 * (s_in + s_out)


# ---------------------------------------------------------------------------
# Escáner de puntos (independiente de Klipper: recibe un objeto 'io')
# ---------------------------------------------------------------------------
class ScanParams:
    def __init__(self, **kw):
        self.z_start = 3.0
        self.z_floor = -0.4
        self.z_coarse = 0.25
        self.z_bisect_steps = 5
        self.scan_lower = 0.1
        self.edge_span = 16.0
        self.edge_coarse = 1.0
        self.edge_bisect_tol = 0.02
        self.iterations = 3
        self.coarse_step = 2.0
        self.coarse_rounds = 3
        # Búsqueda gruesa: los puntos vecinos no bajan más de coarse_window
        # por debajo de la mejor Z de disparo ya medida. Fuera del círculo
        # (sin lámina) el PINDA apenas se activa y, sin este límite, cada
        # vecino bajaría hasta z_floor.
        self.coarse_window = 0.3
        # Z a la que la boquilla toca la cama SIN lámina (medida con papel).
        # None = desconocida. Si se conoce, ninguna bajada pasa de
        # z_contact + min_clearance y el barrido lateral se niega a ir más
        # bajo (la boquilla arrastraría por la cama).
        self.z_contact = None
        self.min_clearance = 0.15
        self.travel_speed = 100.
        self.scan_speed = 30.
        self.z_speed = 10.
        for k, v in kw.items():
            if not hasattr(self, k):
                raise KeyError(k)
            setattr(self, k, v)


class Scanner:
    """io debe ofrecer: move(x=None,y=None,z=None,speed=None),
    triggered() -> bool y limits() -> (xmin, xmax, ymin, ymax)."""

    def __init__(self, io, params, log=None):
        self.io = io
        self.p = params
        self.log = log or (lambda msg: None)

    # -- límites de seguridad en Z -------------------------------------------
    def safe_floor(self, floor=None):
        """Z mínima permitida: z_floor, subida a z_contact + min_clearance si
        se conoce el contacto, y a 'floor' si se pide un límite más alto."""
        p = self.p
        f = p.z_floor
        if p.z_contact is not None:
            f = max(f, p.z_contact + p.min_clearance)
        if floor is not None:
            f = max(f, floor)
        return f

    # -- altura de primer contacto en (x, y) -------------------------------
    def z_hit(self, x, y, floor=None):
        """Z de disparo bajando desde z_start; None si no dispara hasta el
        límite (safe_floor). 'floor' permite un límite más alto que z_floor."""
        p, io = self.p, self.io
        z_min = self.safe_floor(floor)
        io.move(z=p.z_start, speed=p.z_speed)
        io.move(x=x, y=y, speed=p.travel_speed)
        if io.triggered():
            raise XYZCalError("El sensor ya está activado a Z=%.2f. Revisa "
                              "z_start, la cama o si hay algo metálico debajo"
                              % p.z_start)
        hi = p.z_start
        lo = None
        z = p.z_start - p.z_coarse
        while z >= z_min - 1e-9:
            io.move(z=z, speed=p.z_speed)
            if io.triggered():
                lo = z
                break
            hi = z
            z -= p.z_coarse
        if lo is None:
            if hi > z_min + 1e-6:
                io.move(z=z_min, speed=p.z_speed)
                if io.triggered():
                    lo = z_min
            if lo is None:
                io.move(z=p.z_start, speed=p.z_speed)
                return None
        for _ in range(p.z_bisect_steps):
            mid = 0.5 * (lo + hi)
            # El PINDA tiene histéresis (Prusa da 50-250 um): subiendo desde
            # una altura donde ya estaba activado se mantiene activado hasta
            # un punto MAS ALTO que el de disparo. Para medir siempre el
            # disparo bajando (como el homing), cada prueba parte de arriba.
            io.move(z=p.z_start, speed=p.z_speed)
            io.move(z=mid, speed=p.z_speed)
            if io.triggered():
                lo = mid
            else:
                hi = mid
        io.move(z=p.z_start, speed=p.z_speed)
        return lo

    def z_track(self, x, y, guess, window=0.35, step=0.1, bisect=4):
        """Como z_hit pero empezando cerca de una altura esperada (mucho más
        rápido). Si el sensor ya está activo en guess+window, recurre a z_hit.
        Devuelve la Z de disparo, o None si no dispara hasta z_floor."""
        p, io = self.p, self.io
        z_min = self.safe_floor()
        top = min(guess + window, p.z_start)
        io.move(z=p.z_start, speed=p.z_speed)
        io.move(x=x, y=y, speed=p.travel_speed)
        if top < z_min:
            # la Z esperada ya está por debajo del límite seguro
            io.move(z=p.z_start, speed=p.z_speed)
            return None
        io.move(z=top, speed=p.z_speed)
        if io.triggered():
            return self.z_hit(x, y)
        hi, lo = top, None
        z = top - step
        while z >= z_min - 1e-9:
            io.move(z=z, speed=p.z_speed)
            if io.triggered():
                lo = z
                break
            hi = z
            z -= step
        if lo is None:
            if hi > z_min + 1e-6:
                io.move(z=z_min, speed=p.z_speed)
                if io.triggered():
                    lo = z_min
            if lo is None:
                io.move(z=p.z_start, speed=p.z_speed)
                return None
        for _ in range(bisect):
            mid = 0.5 * (lo + hi)
            io.move(z=top, speed=p.z_speed)      # siempre bajando (histéresis)
            io.move(z=mid, speed=p.z_speed)
            if io.triggered():
                lo = mid
            else:
                hi = mid
        io.move(z=p.z_start, speed=p.z_speed)
        return lo

    # -- búsqueda gruesa del punto ------------------------------------------
    def coarse_search(self, cx, cy):
        p = self.p
        xmin, xmax, ymin, ymax = self.io.limits()
        cache = {}

        def hit(x, y, floor=None):
            key = (round(x, 3), round(y, 3))
            if key not in cache:
                cache[key] = self.z_hit(x, y, floor)
                self.log("  prueba (%.2f, %.2f): z_hit=%s" % (
                    x, y, "--" if cache[key] is None else "%.3f" % cache[key]))
            return cache[key]

        for _ in range(p.coarse_rounds):
            center_z = hit(cx, cy)
            best = (cx, cy, center_z)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    x = cx + dx * p.coarse_step
                    y = cy + dy * p.coarse_step
                    if not (xmin <= x <= xmax and ymin <= y <= ymax):
                        continue
                    # Solo interesa un vecino MÁS ALTO que el mejor: no hace
                    # falta bajar mucho por debajo de él (fuera del círculo
                    # bajaría hasta z_floor).
                    floor = (None if best[2] is None
                             else best[2] - p.coarse_window)
                    z = hit(x, y, floor)
                    if z is not None and (best[2] is None or z > best[2] + 0.01):
                        best = (x, y, z)
            if (best[0], best[1]) == (cx, cy):
                break
            cx, cy = best[0], best[1]
        z = hit(cx, cy)
        if z is None:
            raise XYZCalError("No se detecta el punto cerca de (%.1f, %.1f). "
                              "Comprueba que has quitado la lámina, z_floor y "
                              "la posición nominal (XYZCAL_GOTO)." % (cx, cy))
        return cx, cy, z

    # -- bordes -------------------------------------------------------------
    def _edge(self, axis, cx, cy, direction):
        p, io = self.p, self.io
        xmin, xmax, ymin, ymax = io.limits()
        if axis == 'x':
            start, lo, hi = cx, xmin, xmax

            def sample(v):
                io.move(x=v, y=cy, speed=p.scan_speed)
                return io.triggered()
        else:
            start, lo, hi = cy, ymin, ymax

            def sample(v):
                io.move(x=cx, y=v, speed=p.scan_speed)
                return io.triggered()

        def rearm():
            io.move(x=cx, y=cy, speed=p.scan_speed)
        rearm()
        room = (hi - start) if direction > 0 else (start - lo)
        span = min(p.edge_span, room)
        return find_edge(sample, rearm, start, direction, span, p.edge_coarse,
                         p.edge_bisect_tol)

    def _edge_pair(self, axis, cx, cy, zh, zs_box, front_ok=False):
        """Bordes (+, -) a lo largo de un eje. Si el sensor sigue activo al
        final del recorrido (barrido demasiado bajo o zona alta alrededor)
        sube la altura de barrido y reintenta, hasta 3 veces."""
        p, io = self.p, self.io
        for attempt in range(4):
            plus = self._edge(axis, cx, cy, +1)
            minus = self._edge(axis, cx, cy, -1)
            if (plus is not None and minus is not None) or (
                    front_ok and plus is not None):
                return plus, minus
            if attempt == 3 or zs_box[0] >= zh - 0.03:
                break
            zs_box[0] = min(zs_box[0] + 0.08, zh - 0.03)
            side = ("+" if plus is None else "") + ("-" if minus is None else "")
            self.log("  sin borde %s%s a %.0f mm: subo Z de barrido a %.3f"
                     % (axis.upper(), side, p.edge_span, zs_box[0]))
            io.move(x=cx, y=cy, speed=p.travel_speed)
            io.move(z=zs_box[0], speed=p.z_speed)
        if axis == 'x':
            raise XYZCalError(
                "No se encuentra el borde X (%s) del punto: el sensor sigue "
                "activo a %.1f mm del centro, incluso subiendo Z de barrido "
                "hasta %.3f. Revisa la posición con XYZCAL_GOTO o usa "
                "COARSE=1" % ("+" if plus is None else "-", p.edge_span,
                              zs_box[0]))
        return plus, minus

    # -- punto completo -----------------------------------------------------
    def scan_dot(self, nx, ny, coarse=True):
        p, io = self.p, self.io
        if coarse:
            cx, cy, zh = self.coarse_search(nx, ny)
        else:
            zh = self.z_hit(nx, ny)
            cx, cy = nx, ny
            if zh is None:
                raise XYZCalError("Sin respuesta del sensor en (%.1f, %.1f) "
                                  "hasta Z=%.2f" % (nx, ny, self.safe_floor()))
        z_min = self.safe_floor()
        zs_wanted = zh - p.scan_lower
        if p.z_contact is not None and zs_wanted < z_min:
            # El barrido lateral necesita ir por debajo de la Z de disparo;
            # si eso queda bajo el límite seguro, la boquilla arrastraría
            # por la cama: mejor parar con un error claro.
            raise XYZCalError(
                "Margen insuficiente: el barrido lateral iría a Z=%.3f y la "
                "boquilla toca la cama a Z=%.3f (mínimo %.2f mm de margen). "
                "El PINDA está demasiado alto respecto a la boquilla."
                % (zs_wanted, p.z_contact, p.min_clearance))
        zs = max(zs_wanted, z_min)
        io.move(x=cx, y=cy, speed=p.travel_speed)
        io.move(z=zs, speed=p.z_speed)
        tries = 0
        while not io.triggered():
            tries += 1
            if tries > 3 or zs - 0.05 < z_min:
                raise XYZCalError("El sensor no se activa en el centro del "
                                  "punto a Z=%.2f" % zs)
            zs -= 0.05
            io.move(z=zs, speed=p.z_speed)
        self.log("  centro provisional (%.3f, %.3f), Z de barrido %.3f"
                 % (cx, cy, zs))
        acc = []
        y_ok = True
        rx = ry = None
        zs_box = [zs]            # altura de barrido; sube si no hay borde
        for it in range(p.iterations):
            xp, xm = self._edge_pair('x', cx, cy, zh, zs_box)
            cx = 0.5 * (xp + xm)
            rx = 0.5 * (xp - xm)
            io.move(x=cx, y=cy, speed=p.scan_speed)
            yp, ym = self._edge_pair('y', cx, cy, zh, zs_box, front_ok=True)
            if yp is None:
                raise XYZCalError("No se encuentra el borde trasero (+Y) del "
                                  "punto (el sensor sigue activo a %.1f mm "
                                  "del centro)" % p.edge_span)
            if ym is None:
                # borde frontal fuera de alcance: se estima con el radio en X
                y_ok = False
                cy = yp - rx
            else:
                y_ok = True
                cy = 0.5 * (yp + ym)
                ry = 0.5 * (yp - ym)
            io.move(x=cx, y=cy, speed=p.scan_speed)
            self.log("  iteración %d: (%.3f, %.3f) radio X=%.2f%s" % (
                it + 1, cx, cy, rx, "" if y_ok else "  [Y estimado]"))
            if it > 0 or p.iterations == 1:
                acc.append((cx, cy))
        io.move(z=p.z_start, speed=p.z_speed)
        mx = sum(a[0] for a in acc) / len(acc)
        my = sum(a[1] for a in acc) / len(acc)
        return {'x': mx, 'y': my, 'z_scan': zs, 'z_hit': zh, 'rx': rx,
                'ry': ry, 'y_ok': y_ok,
                'spread': max(max(a[0] for a in acc) - min(a[0] for a in acc),
                              max(a[1] for a in acc) - min(a[1] for a in acc))}


def evaluate(points, spacing_x, spacing_y):
    """Ajusta los 4 puntos medidos y devuelve el informe completo."""
    true_pts = [(0., 0.), (spacing_x, 0.), (spacing_x, spacing_y),
                (0., spacing_y)]
    measured = [(pt['x'], pt['y']) for pt in points]
    wx = [1.] * 4
    wy = [1. if pt['y_ok'] else 0. for pt in points]
    fit = fit_bed(measured, true_pts, wx, wy)
    a1, a2 = fit['a1'], fit['a2']
    angle_diff = abs(a2 - a1)
    verdict = 'perfecto'
    if angle_diff > SKEW_MILD:
        verdict = 'extremo' if angle_diff > SKEW_EXTREME else 'leve'
    if abs(a1) > SKEW_EXTREME or abs(a2) > SKEW_EXTREME:
        verdict = 'extremo'
    bad = [i for i, r in enumerate(fit['residuals'])
           if r[2] > MAX_POINT_ERROR and (points[i]['y_ok'] or abs(r[0]) > MAX_POINT_ERROR)]
    dx_front = math.hypot(measured[1][0] - measured[0][0],
                          measured[1][1] - measured[0][1])
    dx_rear = math.hypot(measured[2][0] - measured[3][0],
                         measured[2][1] - measured[3][1])
    dy_right = math.hypot(measured[2][0] - measured[1][0],
                          measured[2][1] - measured[1][1])
    dy_left = math.hypot(measured[3][0] - measured[0][0],
                         measured[3][1] - measured[0][1])
    # Escala de los ejes: separación medida (en coordenadas de máquina) entre
    # la separación real de los círculos. Si es < 1 la máquina se mueve MÁS
    # de lo que se le manda y hay que AUMENTAR el rotation_distance:
    #     rotation_distance_nuevo = rotation_distance_actual / escala
    mean_x = 0.5 * (dx_front + dx_rear)
    mean_y = 0.5 * (dy_right + dy_left)
    y_reliable = all(pt['y_ok'] for pt in points)
    return {
        'fit': fit, 'a1': a1, 'a2': a2,
        'skew_deg': math.degrees(a2 - a1),
        'rot_x_deg': math.degrees(a1), 'rot_y_deg': math.degrees(a2),
        'xy_factor': xy_skew_factor(a1, a2),
        'verdict': verdict, 'bad_points': bad,
        'dist_x': (dx_front, dx_rear), 'dist_y': (dy_right, dy_left),
        'mean_x': mean_x, 'mean_y': mean_y,
        'scale_x': mean_x / spacing_x,
        'scale_y': (mean_y / spacing_y) if y_reliable else None,
    }


def _poly_terms(deg):
    return [(i, j) for i in range(deg + 1) for j in range(deg + 1 - i)]


def fit_surface(pts, deg=3, k=4.0, min_thr=0.04, fixed_thr=None, iters=12):
    """Ajuste robusto de una superficie polinómica suave z(x, y).

    pts: lista de (x, y, z). La alabeo de la cama es suave; un imán produce un
    salto brusco y local. Se ajusta el polinomio descartando iterativamente los
    puntos que se desvían más que el umbral. Devuelve dict con 'resid' (por
    punto), 'flag' (True = anómalo), 'sigma' y 'thr'.
    """
    terms = _poly_terms(deg)
    n = len(pts)
    if n < 3 * len(terms):
        raise XYZCalError("Pocos puntos (%d) para ajustar la superficie" % n)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cx, hx = 0.5 * (min(xs) + max(xs)), 0.5 * (max(xs) - min(xs)) or 1.
    cy, hy = 0.5 * (min(ys) + max(ys)), 0.5 * (max(ys) - min(ys)) or 1.
    rows = []
    for x, y, _ in pts:
        u, v = (x - cx) / hx, (y - cy) / hy
        rows.append([u ** i * v ** j for i, j in terms])
    inl = [True] * n
    resid = [0.] * n
    sigma = thr = 0.
    for _ in range(iters):
        m = len(terms)
        A = [[0.] * m for _ in range(m)]
        b = [0.] * m
        for r, p, ok in zip(rows, pts, inl):
            if not ok:
                continue
            for a in range(m):
                b[a] += r[a] * p[2]
                for c in range(m):
                    A[a][c] += r[a] * r[c]
        for a in range(m):
            A[a][a] += 1e-9
        coef = _solve_linear(A, b)
        resid = [p[2] - sum(c * v for c, v in zip(coef, r))
                 for r, p in zip(rows, pts)]
        used = [e for e, ok in zip(resid, inl) if ok]
        if fixed_thr is not None:
            thr = fixed_thr
            sigma = 0.
        else:
            med = sorted(abs(e) for e in resid)[len(resid) // 2]
            if _ == 0:
                sigma = 1.4826 * med
            else:
                sigma = math.sqrt(sum(e * e for e in used) / max(len(used), 1))
            thr = max(min_thr, k * sigma)
        new = [abs(e) <= thr for e in resid]
        if new == inl:
            break
        if sum(new) < 3 * len(terms):
            # quedarían muy pocos puntos "normales" para ajustar: se para
            # aquí y se conservan las marcas de la iteración anterior
            break
        inl = new
    return {'resid': resid, 'flag': [not ok for ok in inl],
            'sigma': sigma, 'thr': thr}


def magnet_regions(cells, xs, ys, margin, min_cells=2):
    """Agrupa celdas anómalas y devuelve rectángulos (x0, y0, x1, y1, n).

    cells: conjunto de (ix, iy) marcadas. xs, ys: coordenadas de la malla.
    Cada grupo (vecindad de 8) se convierte en su rectángulo envolvente
    ampliado en 'margin' (por la mitad del paso más el margen extra). Los
    rectángulos que se solapan o se tocan se fusionan (bed_mesh no admite
    regiones solapadas).
    """
    todo = set(cells)
    boxes = []
    while todo:
        stack = [todo.pop()]
        comp = []
        while stack:
            c = stack.pop()
            comp.append(c)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    q = (c[0] + dx, c[1] + dy)
                    if q in todo:
                        todo.remove(q)
                        stack.append(q)
        if len(comp) < min_cells:
            continue
        boxes.append([min(xs[c[0]] for c in comp) - margin,
                      min(ys[c[1]] for c in comp) - margin,
                      max(xs[c[0]] for c in comp) + margin,
                      max(ys[c[1]] for c in comp) + margin, len(comp)])
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] \
                        and b[1] <= a[3]:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]),
                                max(a[2], b[2]), max(a[3], b[3]), a[4] + b[4]]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    boxes.sort(key=lambda r: (r[1], r[0]))
    return [tuple(b) for b in boxes]


def magnet_scan(sc, xs, ys, to_nozzle, log=None):
    """Mide la Z de disparo en la malla xs x ys (coordenadas de sonda).

    Recorre las filas en serpentina. Devuelve dict (ix, iy) -> z, None (no
    dispara hasta z_floor) o 'skip' (fuera del alcance de la máquina).
    """
    xmin, xmax, ymin, ymax = sc.io.limits()
    zmap = {}
    guess = None
    for iy, y in enumerate(ys):
        order = range(len(xs)) if iy % 2 == 0 else range(len(xs) - 1, -1, -1)
        for ix in order:
            nx, ny = to_nozzle(xs[ix], y)
            if not (xmin <= nx <= xmax and ymin <= ny <= ymax):
                zmap[(ix, iy)] = 'skip'
                continue
            if guess is None:
                z = sc.z_hit(nx, ny)
            else:
                z = sc.z_track(nx, ny, guess)
            zmap[(ix, iy)] = z
            if z is not None:
                guess = z
        if log:
            log("fila %d/%d (Y=%.1f)" % (iy + 1, len(ys), y))
    return zmap


def magnet_analyze(zmap, xs, ys, deg=3, thr=None, margin=2.0, min_cells=2,
                   min_thr=0.04):
    """Detecta celdas anómalas y las agrupa en regiones."""
    keys = [k for k, v in zmap.items() if v not in (None, 'skip')]
    pts = [(xs[k[0]], ys[k[1]], zmap[k]) for k in keys]
    fit = fit_surface(pts, deg=deg, fixed_thr=thr, min_thr=min_thr)
    flags = {}
    resid = {}
    for k, e, f in zip(keys, fit['resid'], fit['flag']):
        resid[k] = e
        if f:
            flags[k] = e
    for k, v in zmap.items():
        if v is None:
            flags[k] = float('nan')        # no dispara: también anómalo
    step_x = (xs[-1] - xs[0]) / (len(xs) - 1) if len(xs) > 1 else 0.
    step_y = (ys[-1] - ys[0]) / (len(ys) - 1) if len(ys) > 1 else 0.
    m = 0.5 * max(step_x, step_y) + margin
    regions = magnet_regions(set(flags), xs, ys, m, min_cells)
    return {'sigma': fit['sigma'], 'thr': fit['thr'], 'resid': resid,
            'flags': flags, 'regions': regions, 'n_used': len(pts)}


def zscale_measure(sc, refs, block, repeat, to_nozzle, log=None):
    """Mide alternando referencia(s) y bloque (cancela la deriva térmica).

    refs: lista de (x, y) de referencia; block: (x, y) del bloque, ambos en
    coordenadas de sonda. Devuelve ([z_ref...], [z_bloque...]).
    """
    seq = []
    for ref in refs:
        seq += [('ref', ref), ('bloque', block)]
    last = {}
    out = {'ref': [], 'bloque': []}
    for r in range(repeat):
        for kind, (x, y) in seq:
            nx, ny = to_nozzle(x, y)
            key = (kind, x, y)
            if key in last:
                z = sc.z_track(nx, ny, last[key], bisect=6)
            else:
                z = sc.z_hit(nx, ny)
            if z is None:
                raise XYZCalError("El sensor no dispara en %s (%.1f, %.1f) "
                                  "hasta Z=%.2f" % (kind, x, y, sc.safe_floor()))
            last[key] = z
            out[kind].append(z)
            if log:
                log("ronda %d/%d %s (%.1f, %.1f): Z=%.4f"
                    % (r + 1, repeat, kind, x, y, z))
    return out['ref'], out['bloque']


def zscale_report(ref, blk, thickness, ref_thickness=0., rd=None):
    """Escala del eje Z a partir de las Z de disparo sobre referencia y bloque.

    Si el bloque (espesor T) y la referencia (espesor T0) son de verdad
    T - T0 más altos, la Z mandada entre ambos debe ser T - T0. Con
    k = (T - T0) / dZ_mandada: la máquina se mueve k veces lo mandado y
    rotation_distance_nuevo = rotation_distance_actual * k.
    """
    mr = sum(ref) / len(ref)
    mb = sum(blk) / len(blk)
    dz = mb - mr
    dt = thickness - ref_thickness
    if dt <= 0:
        raise XYZCalError("THICKNESS debe ser mayor que REF_THICKNESS")
    if dz <= 0.05:
        raise XYZCalError("El bloque no sube la Z de disparo (dZ=%.3f): ¿está "
                          "bajo la sonda y es de acero?" % dz)
    sr = max(ref) - min(ref)
    sb = max(blk) - min(blk)
    k = dt / dz
    return {'mean_ref': mr, 'mean_blk': mb, 'dz': dz, 'dt': dt, 'k': k,
            'spread_ref': sr, 'spread_blk': sb,
            'rel_unc': (sr + sb) / dz,
            'rd_new': rd * k if rd else None}


def limits_report(readings, configured_max, margin=1.0):
    """Resume medidas del tope máximo de un eje.

    readings: posiciones (mm) a las que se detectó el tope en cada repetición.
    Devuelve media, dispersión y position_max sugerido (media - margen).
    """
    n = len(readings)
    mean = sum(readings) / n
    return {'mean': mean, 'spread': max(readings) - min(readings), 'n': n,
            'diff': mean - configured_max, 'suggested': mean - margin}


def locate_stats(rounds):
    """Resume varias rondas de medida de los mismos puntos.

    rounds: lista de rondas; cada ronda es una lista de resultados de
    scan_dot en el mismo orden de puntos. Devuelve, por punto, el centro
    medio, la dispersión (máximo - mínimo) entre rondas y el radio medio.
    """
    out = []
    n = len(rounds)
    for k in range(len(rounds[0])):
        xs = [rnd[k]['x'] for rnd in rounds]
        ys = [rnd[k]['y'] for rnd in rounds]
        out.append({
            'x': sum(xs) / n, 'y': sum(ys) / n,
            'x_spread': max(xs) - min(xs), 'y_spread': max(ys) - min(ys),
            'rx': sum(rnd[k]['rx'] for rnd in rounds) / n,
            'z_hit': sum(rnd[k]['z_hit'] for rnd in rounds) / n,
            'y_ok': all(rnd[k]['y_ok'] for rnd in rounds),
            'n': n,
        })
    return out


# ---------------------------------------------------------------------------
# Integración con Klipper
# ---------------------------------------------------------------------------
class KlipperIO:
    def __init__(self, printer, query, reads, settle):
        self.printer = printer
        self.toolhead = printer.lookup_object('toolhead')
        self.query = query
        self.reads = reads
        self.settle = settle
        self.xy_speed = 100.
        eventtime = printer.get_reactor().monotonic()
        st = self.toolhead.get_status(eventtime)
        homed = st.get('homed_axes', '')
        if not all(a in homed for a in 'xyz'):
            raise printer.command_error("Haz G28 antes de calibrar")
        self._mn = st['axis_minimum']
        self._mx = st['axis_maximum']

    def limits(self):
        return (self._mn.x, self._mx.x, self._mn.y, self._mx.y)

    def z_limits(self):
        return (self._mn.z, self._mx.z)

    def move(self, x=None, y=None, z=None, speed=None):
        """Mueve la boquilla. Si una coordenada queda fuera del recorrido
        de la máquina da error en vez de recortarla en silencio (se mediría
        en otro sitio sin avisar). Solo se tolera un error de redondeo."""
        xmin, xmax, ymin, ymax = self.limits()
        zmin, zmax = self.z_limits()
        out = []
        for name, v, lo, hi in (('X', x, xmin, xmax), ('Y', y, ymin, ymax),
                                ('Z', z, zmin, zmax)):
            if v is None:
                out.append(None)
                continue
            if not (lo - 1e-3 <= v <= hi + 1e-3):
                raise XYZCalError(
                    "Movimiento fuera del recorrido de la máquina: %s=%.3f "
                    "(límites %.2f..%.2f). Revisa la posición del punto, "
                    "z_start/z_floor o position_min/position_max."
                    % (name, v, lo, hi))
            out.append(min(max(v, lo), hi))
        self.toolhead.manual_move(out, speed or self.xy_speed)

    def z(self):
        return self.toolhead.get_position()[2]

    def triggered(self):
        votes = 0
        for _ in range(self.reads):
            if self.settle > 0.:
                self.toolhead.dwell(self.settle)
            print_time = self.toolhead.get_last_move_time()
            if self.query(print_time):
                votes += 1
        return votes * 2 > self.reads


class PrusaXYZCal:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.first_x = config.getfloat('first_point_x', 12.0)
        self.first_y = config.getfloat('first_point_y', 18.0)
        self.spacing_x = config.getfloat('dot_spacing_x', 208.0, above=10.)
        self.spacing_y = config.getfloat('dot_spacing_y', 192.0, above=10.)
        self.reads = config.getint('sensor_reads', 1, minval=1, maxval=9)
        self.settle = config.getfloat('settle_time', 0.02, minval=0.)
        self.defaults = dict(
            z_start=config.getfloat('z_start', 3.0),
            # 1.0 coincide con el ejemplo de config y el README; un valor
            # negativo solo tiene sentido tras comprobarlo con XYZCAL_ZTEST
            z_floor=config.getfloat('z_floor', 1.0),
            z_coarse=config.getfloat('z_coarse_step', 0.25, above=0.02),
            scan_lower=config.getfloat('scan_lower', 0.1, minval=0.),
            edge_span=config.getfloat('edge_span', 16.0, above=1.),
            iterations=config.getint('iterations', 3, minval=1, maxval=10),
            coarse_step=config.getfloat('coarse_step', 2.0, above=0.2),
            coarse_window=config.getfloat('coarse_window', 0.3, above=0.05),
            # Z a la que la boquilla toca la cama sin lámina (prueba del
            # papel). Opcional pero muy recomendable: activa los límites de
            # seguridad de Scanner.safe_floor y del barrido lateral.
            z_contact=config.getfloat('nozzle_contact_z', None),
            min_clearance=config.getfloat('min_clearance', 0.15, minval=0.),
            travel_speed=config.getfloat('travel_speed', 100., above=1.),
            scan_speed=config.getfloat('scan_speed', 30., above=1.),
            z_speed=config.getfloat('z_speed', 10., above=1.),
        )
        if self.defaults['z_floor'] >= self.defaults['z_start']:
            raise config.error("prusa_xyzcal: z_floor debe ser menor que "
                               "z_start")
        zc = self.defaults['z_contact']
        if zc is not None and (zc + self.defaults['min_clearance']
                               >= self.defaults['z_start']):
            raise config.error("prusa_xyzcal: nozzle_contact_z + "
                               "min_clearance debe ser menor que z_start")
        # Posiciones exactas opcionales (salida de XYZCAL_LOCATE): sustituyen
        # a las calculadas con first_point_x/y y dot_spacing_x/y.
        self.overrides = {}
        for n in range(1, 5):
            v = config.getfloatlist('point%d' % n, None, count=2)
            if v is not None:
                self.overrides[n - 1] = (v[0], v[1])
        # Respaldo por si el objeto probe no ofrece get_offsets(); el valor
        # bueno se pide al objeto en cada comando (_probe_offsets)
        self.probe_off = None
        if config.has_section('probe'):
            psec = config.getsection('probe')
            self.probe_off = (psec.getfloat('x_offset', 0., note_valid=False),
                              psec.getfloat('y_offset', 0., note_valid=False))
        self.last_report = None
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('XYZCAL_GOTO', self.cmd_GOTO,
                               desc=self.cmd_GOTO_help)
        gcode.register_command('XYZCAL_ZTEST', self.cmd_ZTEST,
                               desc=self.cmd_ZTEST_help)
        gcode.register_command('XYZCAL_POINT', self.cmd_POINT,
                               desc=self.cmd_POINT_help)
        gcode.register_command('XYZCAL_CALIBRATE', self.cmd_CALIBRATE,
                               desc=self.cmd_CALIBRATE_help)
        gcode.register_command('XYZCAL_APPLY', self.cmd_APPLY,
                               desc=self.cmd_APPLY_help)
        gcode.register_command('XYZCAL_LOCATE', self.cmd_LOCATE,
                               desc=self.cmd_LOCATE_help)
        gcode.register_command('XYZCAL_MAGNETS', self.cmd_MAGNETS,
                               desc=self.cmd_MAGNETS_help)
        gcode.register_command('XYZCAL_ZSCALE', self.cmd_ZSCALE,
                               desc=self.cmd_ZSCALE_help)
        gcode.register_command('XYZCAL_LIMITS', self.cmd_LIMITS,
                               desc=self.cmd_LIMITS_help)

    # -- utilidades ---------------------------------------------------------
    def _nominal(self, idx):
        pts = [(self.first_x, self.first_y),
               (self.first_x + self.spacing_x, self.first_y),
               (self.first_x + self.spacing_x, self.first_y + self.spacing_y),
               (self.first_x, self.first_y + self.spacing_y)]
        return self.overrides.get(idx, pts[idx])

    def _point_index(self, gcmd):
        n = gcmd.get_int('POINT', minval=1, maxval=4)
        return n - 1

    def _query_func(self, gcmd):
        probe = self.printer.lookup_object('probe', None)
        mcu_probe = getattr(probe, 'mcu_probe', None)
        q = getattr(mcu_probe, 'query_endstop', None)
        if q is None:
            q = getattr(getattr(mcu_probe, 'mcu_endstop', None),
                        'query_endstop', None)
        if q is None:
            raise gcmd.error("No encuentro [probe] con query_endstop")
        return q

    def _probe_offsets(self, gcmd):
        """(x_offset, y_offset) de la sonda: posición de sonda = boquilla
        + offset. Se piden al objeto probe (vale para [probe], [bltouch],
        etc.); si no se puede, se usa lo leído de [probe] en la config."""
        probe = self.printer.lookup_object('probe', None)
        get = getattr(probe, 'get_offsets', None)
        if get is not None:
            try:
                off = get()
            except TypeError:
                off = get(gcmd)
            return (off[0], off[1])
        if self.probe_off is not None:
            return self.probe_off
        raise gcmd.error("No puedo leer x_offset/y_offset de la sonda")

    def _params(self, gcmd, kw=None):
        if kw is None:
            kw = dict(self.defaults)
            for key, name in (('z_start', 'Z_START'), ('z_floor', 'Z_FLOOR')):
                kw[key] = gcmd.get_float(name, kw[key])
        if kw['z_floor'] >= kw['z_start']:
            raise gcmd.error("Z_FLOOR debe ser menor que Z_START")
        return ScanParams(**kw)

    def _check_z(self, gcmd, io, params):
        """Comprueba antes de mover que z_floor y z_start caben en el
        recorrido de Z (si no, Klipper cortaría a mitad de una medida)."""
        zmin, zmax = io.z_limits()
        if params.z_floor < zmin:
            raise gcmd.error("z_floor (%.2f) está por debajo del position_min "
                             "de Z (%.2f). Sube z_floor o usa Z_FLOOR="
                             % (params.z_floor, zmin))
        if params.z_start > zmax:
            raise gcmd.error("z_start (%.2f) supera el position_max de Z "
                             "(%.2f)" % (params.z_start, zmax))

    def _make(self, gcmd, kw=None):
        io = KlipperIO(self.printer, self._query_func(gcmd), self.reads,
                       self.settle)
        params = self._params(gcmd, kw)
        self._check_z(gcmd, io, params)
        return io, Scanner(io, params, log=gcmd.respond_info)

    def _finish(self, io, params):
        """Deja la boquilla a z_start (solo sube, nunca baja) y sincroniza
        la posición de G-code. Se llama siempre, también tras un error."""
        try:
            if io.z() < params.z_start:
                io.move(z=params.z_start, speed=params.z_speed)
        except Exception:
            pass
        gcode_move = self.printer.lookup_object('gcode_move')
        gcode_move.reset_last_position()

    # -- comandos -----------------------------------------------------------
    cmd_GOTO_help = ("Lleva el PINDA a la posición nominal de un punto para "
                     "comprobarla a ojo (POINT=1..4 [Z=5])")

    def cmd_GOTO(self, gcmd):
        idx = self._point_index(gcmd)
        z = gcmd.get_float('Z', 5.0)
        # no hace falta la sonda: solo se mueve
        io = KlipperIO(self.printer, lambda t: False, 1, 0.)
        p = self._params(gcmd)
        x, y = self._nominal(idx)
        try:
            io.move(z=max(z, p.z_start), speed=p.z_speed)
            io.move(x=x, y=y, speed=p.travel_speed)
            io.move(z=z, speed=p.z_speed)
        except XYZCalError as e:
            raise gcmd.error(str(e))
        finally:
            self.printer.lookup_object('gcode_move').reset_last_position()
        gcmd.respond_info("Punto %d: boquilla en X=%.2f Y=%.2f Z=%.2f. El "
                          "PINDA queda desplazado de la boquilla según "
                          "x_offset/y_offset de [probe] y debe quedar sobre "
                          "el círculo de la cama." % (idx + 1, x, y, z))

    cmd_ZTEST_help = ("Baja en la posición nominal y dice a qué Z se activa "
                      "el PINDA (POINT=1..4 [Z_START=3 Z_FLOOR=-0.4])")

    def cmd_ZTEST(self, gcmd):
        idx = self._point_index(gcmd)
        io, sc = self._make(gcmd)
        x, y = self._nominal(idx)
        try:
            z = sc.z_hit(x, y)
        except XYZCalError as e:
            raise gcmd.error(str(e))
        finally:
            self._finish(io, sc.p)
        if z is None:
            gcmd.respond_info("Sin activación hasta Z=%.2f en (%.1f, %.1f)"
                              % (sc.safe_floor(), x, y))
        else:
            msg = "El PINDA se activa a Z=%.3f en (%.1f, %.1f)" % (z, x, y)
            zc = sc.p.z_contact
            if zc is not None:
                margin = z - sc.p.scan_lower - zc
                msg += (". Margen de la boquilla al barrer: %.3f mm (mínimo "
                        "%.2f)%s" % (margin, sc.p.min_clearance,
                                     "" if margin >= sc.p.min_clearance
                                     else " -> INSUFICIENTE: el PINDA está "
                                     "demasiado alto"))
            else:
                msg += (". Aviso: no está configurado nozzle_contact_z; mide "
                        "con papel a qué Z toca la boquilla sin lámina")
            gcmd.respond_info(msg)

    cmd_POINT_help = ("Mide un solo punto sin calcular nada "
                      "(POINT=1..4 [COARSE=1])")

    def cmd_POINT(self, gcmd):
        idx = self._point_index(gcmd)
        coarse = bool(gcmd.get_int('COARSE', 1))
        io, sc = self._make(gcmd)
        x, y = self._nominal(idx)
        try:
            res = sc.scan_dot(x, y, coarse)
        except XYZCalError as e:
            raise gcmd.error(str(e))
        finally:
            self._finish(io, sc.p)
        gcmd.respond_info("Punto %d: X=%.3f Y=%.3f (nominal %.1f, %.1f) "
                          "dispersión %.3f mm" % (idx + 1, res['x'], res['y'],
                                                  x, y, res['spread']))

    cmd_LOCATE_help = ("Mide varias veces el centro exacto de los círculos y "
                       "da los valores para el printer.cfg "
                       "([POINTS=1,2,3,4] [REPEAT=2] [REHOME=1] [COARSE=1])")

    def cmd_LOCATE(self, gcmd):
        try:
            idxs = sorted(set(int(v) - 1 for v in
                              gcmd.get('POINTS', '1,2,3,4').split(',')))
            if not idxs or idxs[0] < 0 or idxs[-1] > 3:
                raise ValueError
        except ValueError:
            raise gcmd.error("POINTS debe ser una lista como 1,2,3,4")
        repeat = gcmd.get_int('REPEAT', 2, minval=1, maxval=6)
        rehome = bool(gcmd.get_int('REHOME', 1 if repeat > 1 else 0))
        coarse = bool(gcmd.get_int('COARSE', 1))
        io, sc = self._make(gcmd)
        gcode = self.printer.lookup_object('gcode')
        rounds = []
        centers = {}
        i = idxs[0]
        try:
            for rnd in range(repeat):
                if rnd > 0 and rehome:
                    gcmd.respond_info("Ronda %d/%d: G28 X Y para medir también "
                                      "la repetibilidad del homing"
                                      % (rnd + 1, repeat))
                    self._finish(io, sc.p)
                    gcode.run_script_from_command("G28 X Y")
                this = []
                for i in idxs:
                    if i in centers:
                        x, y = centers[i]
                        use_coarse = False
                    else:
                        x, y = self._nominal(i)
                        use_coarse = coarse
                    gcmd.respond_info("Ronda %d/%d, punto %d"
                                      % (rnd + 1, repeat, i + 1))
                    try:
                        res = sc.scan_dot(x, y, use_coarse)
                    except XYZCalError as e:
                        if use_coarse or not coarse:
                            raise
                        gcmd.respond_info("Punto %d: %s -> repito con "
                                          "búsqueda gruesa desde el nominal"
                                          % (i + 1, e))
                        x, y = self._nominal(i)
                        res = sc.scan_dot(x, y, True)
                    centers[i] = (res['x'], res['y'])
                    this.append(res)
                rounds.append(this)
        except XYZCalError as e:
            raise gcmd.error("Punto %d: %s" % (i + 1, e))
        finally:
            self._finish(io, sc.p)
        stats = locate_stats(rounds)
        gcmd.respond_info("\n".join(
            self._locate_lines(idxs, stats, repeat, rehome)))

    def _locate_lines(self, idxs, stats, repeat, rehome):
        lines = ["=== Centro de los círculos (coordenadas de BOQUILLA, %d "
                 "ronda%s) ===" % (repeat, "" if repeat == 1 else "s")]
        worst = 0.
        for i, st in zip(idxs, stats):
            nx, ny = self._nominal(i)
            worst = max(worst, st['x_spread'], st['y_spread'])
            lines.append("Punto %d: X=%.3f Y=%.3f (nominal %.1f, %.1f; desvío "
                         "%+.2f, %+.2f)  dispersión X %.3f Y %.3f  radio %.2f "
                         "mm  Z de disparo %.3f%s" % (
                             i + 1, st['x'], st['y'], nx, ny,
                             st['x'] - nx, st['y'] - ny,
                             st['x_spread'], st['y_spread'], st['rx'],
                             st['z_hit'],
                             "" if st['y_ok'] else "  (Y estimada)"))
        if repeat == 1:
            lines.append("Con REPEAT=1 no hay dispersión que comprobar: usa "
                         "REPEAT=2 o más para saber cuánto puedes fiarte.")
        elif worst > LOCATE_WARN:
            lines.append("AVISO: dispersión máxima %.3f mm (> %.2f). %s"
                         % (worst, LOCATE_WARN,
                            "Con REHOME incluye la repetibilidad del homing "
                            "sensorless de X/Y: las coordenadas no son "
                            "fiables más allá de ese margen."
                            if rehome else "Repite la medida."))
        else:
            lines.append("Dispersión máxima entre rondas: %.3f mm%s."
                         % (worst, " (incluye un G28 X Y entre rondas)"
                            if rehome else ""))
        by = dict(zip(idxs, stats))
        lines.append("--- Para pegar en printer.cfg (redondeado a 0.01 mm) ---")
        if 0 in by:
            lines.append("[safe_z_home]")
            lines.append("home_xy_position: %.2f, %.2f"
                         % (by[0]['x'], by[0]['y']))
        if 0 in by and 1 in by:
            lines.append("[z_tilt]")
            lines.append("points:")
            lines.append("  %.2f, %.2f" % (by[0]['x'], by[0]['y']))
            lines.append("  %.2f, %.2f" % (by[1]['x'], by[1]['y']))
        lines.append("[prusa_xyzcal]")
        for i in idxs:
            lines.append("point%d: %.2f, %.2f" % (i + 1, by[i]['x'], by[i]['y']))
        if any(not st['y_ok'] for st in stats):
            lines.append("AVISO: en los puntos con Y estimada no uses esa Y.")
        lines.append("Notas: son posiciones de la boquilla (el PINDA queda "
                     "desplazado según x_offset/y_offset de [probe]). Para "
                     "G28 y z_tilt basta con estar dentro del círculo; "
                     "homear justo en el centro puede mover la Z de "
                     "referencia unas centésimas de mm. Con point1..point4 "
                     "definidos, XYZCAL_CALIBRATE puede usar COARSE=0 y "
                     "tarda menos. No se ha cambiado nada.")
        return lines

    cmd_LIMITS_help = ("Mide el tope máximo de X/Y con la detección de "
                       "bloqueo del driver ([AXES=XY] [REPEAT=2] [SPEED=] "
                       "[OVERRUN=15] [MARGIN=1])")

    def cmd_LIMITS(self, gcmd):
        axes = gcmd.get('AXES', 'XY').upper()
        if not axes or any(a not in 'XY' for a in axes):
            raise gcmd.error("AXES debe ser X, Y o XY")
        repeat = gcmd.get_int('REPEAT', 2, minval=1, maxval=10)
        overrun = gcmd.get_float('OVERRUN', 15., minval=2., maxval=60.)
        margin = gcmd.get_float('MARGIN', 1., minval=0., maxval=10.)
        toolhead = self.printer.lookup_object('toolhead')
        kin = toolhead.get_kinematics()
        rails = getattr(kin, 'rails', None)
        if not rails or len(rails) < 2:
            raise gcmd.error("XYZCAL_LIMITS solo funciona con cinemática "
                             "cartesiana")
        lim = getattr(kin, 'limits', None)
        if lim is None:
            raise gcmd.error("Esta versión de Klipper no expone los límites "
                             "de la cinemática (kin.limits): no puedo medir "
                             "el tope con seguridad")
        phoming = self.printer.lookup_object('homing')
        import sys
        HomingMove = getattr(sys.modules.get(type(phoming).__module__),
                             'HomingMove', None)
        if HomingMove is None:
            raise gcmd.error("Esta versión de Klipper no ofrece HomingMove")
        io = KlipperIO(self.printer, lambda t: False, 1, 0.)   # exige G28
        gcode = self.printer.lookup_object('gcode')
        z_safe = max(self.defaults['z_start'], 8.)
        results = {}
        try:
            for ax in axes:
                idx = 'XY'.index(ax)
                rail = rails[idx]
                pmin, pmax = rail.get_range()
                hi = rail.get_homing_info()
                speed = gcmd.get_float('SPEED', hi.speed, above=1.)
                readings = []
                for rep in range(repeat):
                    th = toolhead.get_position()
                    if th[2] < z_safe - 1e-6:
                        toolhead.manual_move([None, None, z_safe], 10.)
                    mid = [0.5 * (r.get_range()[0] + r.get_range()[1])
                           for r in rails[:2]]
                    toolhead.manual_move([mid[0], mid[1], None], 100.)
                    toolhead.wait_moves()
                    pos = list(toolhead.get_position())
                    pos[idx] = pmax + overrun
                    gcmd.respond_info("%s: hacia el tope máximo a %.0f mm/s "
                                      "(hasta %.1f)" % (ax, speed, pos[idx]))
                    # amplía solo el límite de este eje para poder rebasar
                    # position_max y lo restaura en cuanto acaba el
                    # movimiento, pase lo que pase (no depende del G28)
                    orig = lim[idx]
                    lim[idx] = (pmin, pmax + overrun + 1.)
                    try:
                        hm = HomingMove(self.printer, rail.get_endstops())
                        trig = hm.homing_move(pos, speed, probe_pos=True,
                                              check_triggered=True)
                    finally:
                        lim[idx] = orig
                    readings.append(trig[idx])
                    gcmd.respond_info("%s: tope en %.3f" % (ax, trig[idx]))
                    gcode.run_script_from_command("G28 %s" % " ".join(axes))
                results[ax] = (limits_report(readings, pmax, margin),
                               pmin, pmax, hi.position_endstop)
        except Exception as e:
            try:
                gcode.run_script_from_command("G28 %s" % " ".join(axes))
            except Exception:
                pass
            raise gcmd.error("XYZCAL_LIMITS: %s" % (e,))
        self.printer.lookup_object('gcode_move').reset_last_position()
        gcmd.respond_info("\n".join(self._limits_lines(results, margin)))

    def _limits_lines(self, results, margin):
        lines = ["=== Tope máximo (detección de bloqueo) ==="]
        for ax, (r, pmin, pmax, pend) in sorted(results.items()):
            lines.append("%s: tope en %.3f mm (dispersión %.3f mm, %d "
                         "medidas); position_max configurado %.1f (%+.2f)"
                         % (ax, r['mean'], r['spread'], r['n'], pmax,
                            r['diff']))
            lines.append("   recorrido desde el cero del eje (%.1f): %.2f mm"
                         % (pend, r['mean'] - pend))
            lines.append("   position_max sugerido: %.1f (tope - %.1f mm de "
                         "holgura)" % (r['suggested'], margin))
            if r['diff'] < -0.5:
                lines.append("   AVISO: el tope físico está %.1f mm ANTES de "
                             "tu position_max: la máquina podría chocar en "
                             "movimientos largos. Baja position_max."
                             % -r['diff'])
            if r['spread'] > 0.5:
                lines.append("   AVISO: dispersión grande (%.2f mm): el "
                             "bloqueo se detecta a distinta profundidad; "
                             "ajusta driver_SGTHRS o la velocidad."
                             % r['spread'])
        lines.append("El tope incluye la holgura/elasticidad del choque "
                     "(décimas de mm). Sirve para comprobar límites, no como "
                     "referencia de escala: no conoces la distancia real "
                     "hasta el tope. Se ha vuelto a hacer G28 en los ejes "
                     "medidos y no se ha cambiado ninguna configuración.")
        return lines

    cmd_ZSCALE_help = ("Escala del eje Z con un bloque de acero de espesor "
                       "conocido (THICKNESS= BLOCK_X= BLOCK_Y= [REF_X1= "
                       "REF_Y1= REF_X2= REF_Y2= SINGLE=1 REF_THICKNESS= "
                       "REPEAT=3])")

    def cmd_ZSCALE(self, gcmd):
        T = gcmd.get_float('THICKNESS', minval=2., maxval=100.)
        T0 = gcmd.get_float('REF_THICKNESS', 0., minval=0.)
        bx = gcmd.get_float('BLOCK_X')
        by = gcmd.get_float('BLOCK_Y')
        d = gcmd.get_float('REF_DIST', 40., minval=10.)
        refs = [(gcmd.get_float('REF_X1', bx - d), gcmd.get_float('REF_Y1', by))]
        if not gcmd.get_int('SINGLE', 0):
            refs.append((gcmd.get_float('REF_X2', bx + d),
                         gcmd.get_float('REF_Y2', by)))
        repeat = gcmd.get_int('REPEAT', 3, minval=1, maxval=10)
        kw = dict(self.defaults)
        kw['z_start'] = gcmd.get_float('Z_START', max(T, T0) + 3.0)
        kw['z_floor'] = gcmd.get_float('Z_FLOOR', kw['z_floor'])
        kw['z_coarse'] = 0.5
        kw['z_bisect_steps'] = 6
        io, sc = self._make(gcmd, kw)
        ox, oy = self._probe_offsets(gcmd)
        xmin, xmax, ymin, ymax = io.limits()
        for (x, y) in refs + [(bx, by)]:
            if not (xmin <= x - ox <= xmax and ymin <= y - oy <= ymax):
                raise gcmd.error("La posición de sonda (%.1f, %.1f) queda "
                                 "fuera del alcance de la máquina" % (x, y))
        gcmd.respond_info("Medida de escala Z: bloque de %.2f mm en sonda "
                          "(%.1f, %.1f), %d referencia(s), %d rondas. "
                          "Z de inicio %.1f." % (T, bx, by, len(refs), repeat,
                                                 kw['z_start']))
        try:
            ref, blk = zscale_measure(sc, refs, (bx, by), repeat,
                                      lambda x, y: (x - ox, y - oy),
                                      log=gcmd.respond_info)
        except XYZCalError as e:
            raise gcmd.error(str(e))
        finally:
            self._finish(io, sc.p)
        rds = {}
        for name in ('stepper_z', 'stepper_z1'):
            v = self._rotation_distance(name)
            if v is not None:
                rds[name] = v
        rd = rds.get('stepper_z')
        try:
            r = zscale_report(ref, blk, T, T0, rd)
        except XYZCalError as e:
            raise gcmd.error(str(e))
        gcmd.respond_info("\n".join(self._zscale_lines(
            r, rds, len(refs), T0)))

    def _zscale_lines(self, r, rds, n_refs, T0):
        lines = ["=== Escala del eje Z ===",
                 "Referencia: Z de disparo media %.4f mm (dispersión %.4f)"
                 % (r['mean_ref'], r['spread_ref']),
                 "Bloque:     Z de disparo media %.4f mm (dispersión %.4f)"
                 % (r['mean_blk'], r['spread_blk']),
                 "Diferencia de Z mandada %.4f mm para un desnivel real de "
                 "%.3f mm" % (r['dz'], r['dt']),
                 "Factor k = real / mandado = %.5f (%+.3f %%)"
                 % (r['k'], (r['k'] - 1.) * 100.),
                 "Incertidumbre de la medida: ~%.3f %%" % (r['rel_unc'] * 100.)]
        if rds:
            for name, v in sorted(rds.items()):
                lines.append("%s: rotation_distance actual %.6f -> sugerido "
                             "%.6f" % (name, v, v * r['k']))
        dev = abs(r['k'] - 1.)
        if dev > 0.02:
            lines.append("AVISO: desviación mayor del 2 %: casi seguro es "
                         "un error de espesor, de material del bloque o de "
                         "colocación, no del eje Z. No cambies nada.")
        elif dev <= max(0.001, r['rel_unc']):
            lines.append("La diferencia está dentro del error de medida: el "
                         "eje Z está bien, no cambies nada.")
        else:
            lines.append("La desviación supera el error de medida. Repite la "
                         "prueba (otra posición o bloque) y solo cambia el "
                         "rotation_distance si se confirma. Con dos motores Z "
                         "cambia los dos por igual.")
        if n_refs == 1:
            lines.append("Con una sola referencia el desnivel de la cama "
                         "entra en el resultado; usa dos (una a cada lado).")
        if T0 == 0.:
            lines.append("Material: sobre un bloque grueso el PINDA dispara a "
                         "otra altura que sobre la lámina. Eso suma un error "
                         "fijo a la diferencia. Para evitarlo usa como "
                         "referencia otro bloque de acero (REF_THICKNESS=) y "
                         "compara dos espesores distintos del mismo material.")
        lines.append("No se ha cambiado nada.")
        return lines

    cmd_MAGNETS_help = ("Mide la Z de disparo en una malla y propone las "
                        "faulty_region de [bed_mesh] ([STEP=8] [THRESHOLD=] "
                        "[MARGIN=2] [X_MIN= Y_MIN= X_MAX= Y_MAX=] [CSV=ruta])")

    def _mesh_bounds(self):
        mn, mx = (35., 6.), (240., 198.)
        try:
            cfg = self.printer.lookup_object('configfile').read_main_config()
            if cfg.has_section('bed_mesh'):
                sec = cfg.getsection('bed_mesh')
                mn = tuple(sec.getfloatlist('mesh_min', mn, count=2))
                mx = tuple(sec.getfloatlist('mesh_max', mx, count=2))
        except Exception:
            pass
        return mn, mx

    def cmd_MAGNETS(self, gcmd):
        (dx0, dy0), (dx1, dy1) = self._mesh_bounds()
        x0 = gcmd.get_float('X_MIN', dx0)
        y0 = gcmd.get_float('Y_MIN', dy0)
        x1 = gcmd.get_float('X_MAX', dx1)
        y1 = gcmd.get_float('Y_MAX', dy1)
        step = gcmd.get_float('STEP', 8., minval=2., maxval=30.)
        thr = gcmd.get_float('THRESHOLD', 0., minval=0.)
        margin = gcmd.get_float('MARGIN', 2., minval=0., maxval=20.)
        min_cells = gcmd.get_int('MIN_CELLS', 2, minval=1)
        csv_path = gcmd.get('CSV', None)
        if x1 - x0 < 2 * step or y1 - y0 < 2 * step:
            raise gcmd.error("Área demasiado pequeña para STEP=%.1f" % step)
        nx = int(math.ceil((x1 - x0) / step)) + 1
        ny = int(math.ceil((y1 - y0) / step)) + 1
        xs = [x0 + i * (x1 - x0) / (nx - 1) for i in range(nx)]
        ys = [y0 + j * (y1 - y0) / (ny - 1) for j in range(ny)]
        io, sc = self._make(gcmd)
        ox, oy = self._probe_offsets(gcmd)
        gcmd.respond_info(
            "Mapa de la Z de disparo: %d x %d = %d puntos (sonda X %.1f..%.1f, "
            "Y %.1f..%.1f, paso %.2f x %.2f). Tiempo estimado: ~%.0f min. "
            "Cama fría y con la lámina que uses al imprimir."
            % (nx, ny, nx * ny, x0, x1, y0, y1, xs[1] - xs[0], ys[1] - ys[0],
               nx * ny * 2.0 / 60.))
        try:
            zmap = magnet_scan(sc, xs, ys, lambda x, y: (x - ox, y - oy),
                               log=gcmd.respond_info)
        except XYZCalError as e:
            raise gcmd.error(str(e))
        finally:
            self._finish(io, sc.p)
        try:
            res = magnet_analyze(zmap, xs, ys, thr=thr or None, margin=margin,
                                 min_cells=min_cells)
        except XYZCalError as e:
            raise gcmd.error(str(e))
        if csv_path:
            try:
                with open(csv_path, 'w') as f:
                    f.write("x_sonda,y_sonda,z_disparo,residuo\n")
                    for (ix, iy), z in sorted(zmap.items()):
                        if z in (None, 'skip'):
                            continue
                        f.write("%.3f,%.3f,%.4f,%.4f\n" % (
                            xs[ix], ys[iy], z, res['resid'].get((ix, iy), 0.)))
                gcmd.respond_info("Datos guardados en %s" % csv_path)
            except IOError as e:
                gcmd.respond_info("No se pudo escribir %s: %s" % (csv_path, e))
        gcmd.respond_info("\n".join(
            self._magnet_lines(xs, ys, zmap, res, thr > 0.)))

    def _magnet_lines(self, xs, ys, zmap, res, fixed):
        lines = ["=== Mapa de anomalías (sonda; arriba = Y máx.) "
                 "'.' normal  '+' Z más alta  '-' Z más baja  '?' sin disparo "
                 "' ' fuera de alcance ==="]
        for iy in range(len(ys) - 1, -1, -1):
            row = []
            for ix in range(len(xs)):
                z = zmap[(ix, iy)]
                if z == 'skip':
                    row.append(' ')
                elif z is None:
                    row.append('?')
                elif (ix, iy) in res['flags']:
                    row.append('+' if res['resid'][(ix, iy)] > 0 else '-')
                else:
                    row.append('.')
            lines.append("Y%6.1f  %s" % (ys[iy], ''.join(row)))
        lines.append("        X de %.1f a %.1f, %d columnas"
                     % (xs[0], xs[-1], len(xs)))
        n_meas = res['n_used'] + sum(1 for v in zmap.values() if v is None)
        n_flag = len(res['flags'])
        lines.append("Superficie base (alabeo de la cama): sigma %.3f mm; "
                     "umbral de anomalía %.3f mm%s; %d de %d puntos anómalos."
                     % (res['sigma'], res['thr'],
                        " (fijado por ti)" if fixed else " (automático)",
                        n_flag, n_meas))
        if n_meas and n_flag > 0.4 * n_meas:
            lines.append("AVISO: demasiados puntos anómalos; sube THRESHOLD "
                         "(p. ej. THRESHOLD=%.2f) o revisa la lámina/cama."
                         % (res['thr'] * 2))
        regs = res['regions']
        if not regs:
            lines.append("No se detecta ninguna región anómala. Si esperas "
                         "imanes, baja THRESHOLD (mín. práctico ~0.03) o "
                         "reduce STEP.")
            return lines
        lines.append("--- %d región(es) para pegar en [bed_mesh] "
                     "(redondeo 0.1 mm, coordenadas de sonda) ---" % len(regs))
        for n, (a, b, c, d, cnt) in enumerate(regs, 1):
            lines.append("faulty_region_%d_min: %.1f, %.1f" % (n, a, b))
            lines.append("faulty_region_%d_max: %.1f, %.1f" % (n, c, d))
        sizes = ", ".join("%d: %.0fx%.0f mm (%d celdas)"
                          % (n, c - a, d - b, cnt)
                          for n, (a, b, c, d, cnt) in enumerate(regs, 1))
        lines.append("Tamaños: " + sizes)
        lines.append("Notas: la resolución es el paso de la malla; cada "
                     "región lleva medio paso más MARGIN de holgura por lado. "
                     "Para ajustar más, baja STEP. Se sustituyen tus "
                     "faulty_region actuales solo si las pegas tú; no se "
                     "cambia nada. Quita las regiones antiguas antes de "
                     "pegar (no pueden solaparse).")
        return lines

    cmd_CALIBRATE_help = ("Mide los 4 puntos y calcula el skew XY "
                          "([COARSE=1] [APPLY=0] [SAVE=perfil])")

    def cmd_CALIBRATE(self, gcmd):
        coarse = bool(gcmd.get_int('COARSE', 1))
        apply_now = bool(gcmd.get_int('APPLY', 0))
        io, sc = self._make(gcmd)
        points = []
        try:
            for i in range(4):
                x, y = self._nominal(i)
                gcmd.respond_info("Midiendo punto %d/4 (nominal %.1f, %.1f)"
                                  % (i + 1, x, y))
                points.append(sc.scan_dot(x, y, coarse))
        except XYZCalError as e:
            raise gcmd.error("Punto %d: %s" % (len(points) + 1, e))
        finally:
            self._finish(io, sc.p)
        report = evaluate(points, self.spacing_x, self.spacing_y)
        report['points'] = points
        self.last_report = report
        self._print_report(gcmd, report)
        if apply_now:
            self._apply(gcmd, report, gcmd.get('SAVE', None))

    def _rotation_distance(self, stepper_name):
        toolhead = self.printer.lookup_object('toolhead')
        for s in toolhead.get_kinematics().get_steppers():
            if s.get_name() == stepper_name:
                get = getattr(s, 'get_rotation_distance', None)
                return get()[0] if get is not None else None
        return None

    def _scale_lines(self, r):
        lines = ["--- Escala de los ejes (rotation_distance) ---"]
        axes = (('X', 'stepper_x', self.spacing_x, 'frontal', 'trasero'),
                ('Y', 'stepper_y', self.spacing_y, 'derecho', 'izquierdo'))
        for ax, sname, nominal, side_a, side_b in axes:
            k = ax.lower()
            scale = r['scale_' + k]
            if scale is None:
                lines.append("%s: sin datos fiables (algún punto frontal "
                             "estaba fuera de alcance)" % ax)
                continue
            d = r['dist_' + k]
            lines.append("%s: medido %.3f mm (%s %.3f / %s %.3f) sobre %.1f "
                         "nominal -> escala %.5f (%+.3f %%)" % (
                             ax, r['mean_' + k], side_a, d[0], side_b, d[1],
                             nominal, scale, (scale - 1.) * 100.))
            rd = self._rotation_distance(sname)
            if rd is None:
                lines.append("   no he podido leer el rotation_distance de "
                             "[%s]" % sname)
            elif abs(scale - 1.) < SCALE_TOL:
                lines.append("   dentro de tolerancia (+-%.2f %%): no cambies "
                             "el rotation_distance (actual %.6f)"
                             % (SCALE_TOL * 100., rd))
            else:
                lines.append("   rotation_distance de [%s]: actual %.6f -> "
                             "sugerido %.6f" % (sname, rd, rd / scale))
            if abs(d[0] - d[1]) > SPREAD_WARN:
                lines.append("   AVISO: los lados opuestos difieren %.3f mm. "
                             "Repite la medida antes de fiarte de la escala."
                             % abs(d[0] - d[1]))
        lines.append("OJO: la escala solo es válida si la separación real de "
                     "los círculos de tu cama es exactamente %.0f x %.0f mm "
                     "(la tolerancia de fabricación no la conozco). Antes de "
                     "cambiar nada, imprime un cuadrado de 100 mm, mídelo con "
                     "calibre y aplica nuevo = actual x (medido / 100): si "
                     "sale más grande que 100, el rotation_distance sube."
                     % (self.spacing_x, self.spacing_y))
        return lines

    def _print_report(self, gcmd, r):
        lines = ["=== Resultado calibración XYZ (PINDA) ==="]
        for i, pt in enumerate(r['points']):
            err = r['fit']['residuals'][i][2]
            lines.append("Punto %d: X=%.3f Y=%.3f  error tras ajuste %.3f mm%s"
                         % (i + 1, pt['x'], pt['y'], err,
                            "" if pt['y_ok'] else "  (Y no fiable)"))
        lines.append("Separación X medida: %.3f / %.3f mm (nominal %.1f)"
                     % (r['dist_x'][0], r['dist_x'][1], self.spacing_x))
        lines.append("Separación Y medida: %.3f / %.3f mm (nominal %.1f)"
                     % (r['dist_y'][0], r['dist_y'][1], self.spacing_y))
        lines.append("Giro del eje X: %.4f grados, giro del eje Y: %.4f grados"
                     % (r['rot_x_deg'], r['rot_y_deg']))
        lines.append("Desviación de perpendicularidad X-Y: %.4f grados "
                     "(%s)" % (r['skew_deg'], r['verdict']))
        lines.append("xy_skew equivalente para Klipper: %.8f" % r['xy_factor'])
        lines.extend(self._scale_lines(r))
        if r['bad_points']:
            lines.append("AVISO: error > %.1f mm en los puntos %s. Repite la "
                         "medida o revisa la cama/PINDA." % (
                             MAX_POINT_ERROR,
                             ", ".join(str(i + 1) for i in r['bad_points'])))
        lines.append("No se ha cambiado nada. Usa XYZCAL_APPLY [SAVE=nombre] "
                     "para aplicar el xy_skew (el rotation_distance nunca se "
                     "modifica solo).")
        gcmd.respond_info("\n".join(lines))

    cmd_APPLY_help = ("Aplica el xy_skew de la última medida "
                      "([SAVE=perfil] para guardarlo en un perfil)")

    def cmd_APPLY(self, gcmd):
        if self.last_report is None:
            raise gcmd.error("Primero ejecuta XYZCAL_CALIBRATE")
        self._apply(gcmd, self.last_report, gcmd.get('SAVE', None))

    def _apply(self, gcmd, report, save_name):
        skew = self.printer.lookup_object('skew_correction', None)
        if skew is None:
            raise gcmd.error("Necesitas [skew_correction] en printer.cfg")
        f = report['xy_factor']
        update = getattr(skew, '_update_skew', None)
        if update is None or not hasattr(skew, 'xz_factor'):
            raise gcmd.error("Esta versión de Klipper no permite aplicar el "
                             "skew desde el módulo. Pon a mano xy_skew = "
                             "%.8f en tu perfil de skew_correction." % f)
        update(f, skew.xz_factor, skew.yz_factor)
        gcmd.respond_info("xy_skew = %.8f aplicado (xz e yz sin cambios)" % f)
        if save_name:
            gcode = self.printer.lookup_object('gcode')
            gcode.run_script_from_command("SKEW_PROFILE SAVE=%s" % save_name)
            gcmd.respond_info("Perfil '%s' guardado en la sesión. Ejecuta "
                              "SAVE_CONFIG para hacerlo permanente." %
                              save_name)


def load_config(config):
    return PrusaXYZCal(config)
