#!/usr/bin/env python3
# Prueba offline de prusa_xyzcal.py sin impresora.
# Simula una cama MK3 con 4 puntos, un PINDA con histéresis y ruido, y una
# máquina con ejes torcidos. Comprueba que el skew recuperado coincide con el
# real y que aplicarlo con el skew_correction REAL de Klipper deja la
# máquina perpendicular.
#
# Uso (desde la raíz del repositorio):
#   python3 tests/test_simulacion.py [ruta_a_klipper/klippy/extras/skew_correction.py]
import importlib.util
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import prusa_xyzcal as xc

SCALE_OK = 0.0003      # tolerancia de la escala medida sin escala real


class SimIO:
    """Máquina simulada. Posición física = M * coordenadas_máquina + c."""

    def __init__(self, a1, a2, offset, limits=(-2, 250, -1, 220),
                 noise=0.004, hyst=0.05, seed=1, dot_radius=6.0,
                 dot_gain=0.9, base=-0.35, spacing=(208., 192.),
                 scale=(1., 1.)):
        self.a1, self.a2 = a1, a2
        self.scale = scale                # >1: se mueve más de lo mandado
        self.c = offset                   # posición física con m=(0,0)
        self.lim = limits
        self.noise, self.hyst = noise, hyst
        self.rng = random.Random(seed)
        self.R, self.gain, self.base = dot_radius, dot_gain, base
        self.m = [0., 0., 10.]
        self.state = False
        # puntos reales en la cama (posición del sensor sobre cada círculo)
        # el primero está en (12, 6) + error de origen
        self.dots = [(12., 6.), (12. + spacing[0], 6.),
                     (12. + spacing[0], 6. + spacing[1]), (12., 6. + spacing[1])]
        self.moves = 0
        self.queries = 0
        self.zscale = 1.0                 # >1: Z se mueve más de lo mandado
        self.warp = None                  # (amplitud, cx, cy, radio)
        self.bumps = []                   # (x0, y0, x1, y1, dz)

    def phys(self, mx, my):
        c1, s1, c2, s2 = (math.cos(self.a1), math.sin(self.a1),
                          math.cos(self.a2), math.sin(self.a2))
        mx, my = mx * self.scale[0], my * self.scale[1]
        return (c1 * mx - s2 * my + self.c[0], s1 * mx + c2 * my + self.c[1])

    def height(self, px, py):
        best = self.base
        if self.warp:
            a, wx, wy, wr = self.warp
            best += a * (((px - wx) ** 2 + (py - wy) ** 2) / wr ** 2)
        for x0, y0, x1, y1, dz in self.bumps:
            if x0 <= px <= x1 and y0 <= py <= y1:
                best += dz
        for dx, dy in self.dots:
            r = math.hypot(px - dx, py - dy)
            if r < self.R:
                best = max(best, self.base + self.gain * (1 - (r / self.R) ** 2))
        return best

    def limits(self):
        return self.lim

    def move(self, x=None, y=None, z=None, speed=None):
        xmin, xmax, ymin, ymax = self.lim
        if x is not None:
            self.m[0] = min(max(x, xmin), xmax)
        if y is not None:
            self.m[1] = min(max(y, ymin), ymax)
        if z is not None:
            if z < -0.5:
                raise RuntimeError("¡Colisión simulada! z=%.2f" % z)
            self.m[2] = z
        self.moves += 1
        self._update()

    def _update(self):
        px, py = self.phys(self.m[0], self.m[1])
        h = self.height(px, py) + self.rng.gauss(0, self.noise)
        # histéresis: se activa por debajo de h, se suelta por encima de h+hyst
        if self.state:
            self.state = self.m[2] * self.zscale < h + self.hyst
        else:
            self.state = self.m[2] * self.zscale < h

    def triggered(self):
        self.queries += 1
        self._update()
        return self.state


def run_case(name, a1_deg, a2_deg, offset, **kw):
    a1, a2 = math.radians(a1_deg), math.radians(a2_deg)
    io = SimIO(a1, a2, offset, **kw)
    # la máquina real: el sensor está sobre el punto nominal cuando
    # phys(m_nominal) = dot. Se ajusta 'c' para que (12,6) caiga cerca.
    sx, sy = io.phys(12., 6.)
    io.c = (offset[0] + (12. - sx), offset[1] + (6. - sy))
    params = xc.ScanParams()
    sc = xc.Scanner(io, params)
    pts = []
    for i, (nx, ny) in enumerate([(12., 6.), (220., 6.), (220., 198.),
                                  (12., 198.)]):
        pts.append(sc.scan_dot(nx, ny, coarse=True))
    rep = xc.evaluate(pts, 208., 192.)
    true_skew = a2_deg - a1_deg
    err = rep['skew_deg'] - true_skew
    print("%-34s skew real %+.4f°  medido %+.4f°  error %+.4f°  "
          "factor %+.7f  [%s]  (%d movs, %d lecturas)" % (
              name, true_skew, rep['skew_deg'], err, rep['xy_factor'],
              rep['verdict'], io.moves, io.queries))
    return io, rep, true_skew, err


def check_with_klipper_skew(io, rep, path):
    """Aplica el factor con el calc_skew REAL de Klipper y mide la
    perpendicularidad física resultante de un cuadrado de 100 mm."""
    spec = importlib.util.spec_from_file_location("skew_correction", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sk = mod.PrinterSkew.__new__(mod.PrinterSkew)
    sk.xy_factor, sk.xz_factor, sk.yz_factor = rep['xy_factor'], 0., 0.

    def physical(gx, gy):
        mx, my = sk.calc_skew([gx, gy, 0.])[:2]   # gcode -> motor
        return io.phys(mx, my)

    o = physical(0, 0)
    px = physical(100, 0)
    py = physical(0, 100)
    vx = (px[0] - o[0], px[1] - o[1])
    vy = (py[0] - o[0], py[1] - o[1])
    cosang = (vx[0] * vy[0] + vx[1] * vy[1]) / (math.hypot(*vx) * math.hypot(*vy))
    ang = math.degrees(math.acos(cosang))
    # sin corrección:
    sk.xy_factor = 0.
    o = physical(0, 0)
    px = physical(100, 0)
    py = physical(0, 100)
    vx = (px[0] - o[0], px[1] - o[1])
    vy = (py[0] - o[0], py[1] - o[1])
    cosang0 = (vx[0] * vy[0] + vx[1] * vy[1]) / (math.hypot(*vx) * math.hypot(*vy))
    ang0 = math.degrees(math.acos(cosang0))
    return ang0, ang


def main():
    klipper_skew = (sys.argv[1] if len(sys.argv) > 1 else
                    "/home/claude/klipper/klippy/extras/skew_correction.py")
    ok = True
    cases = [
        ("ejes perfectos", 0.0, 0.0, (1.3, -0.8)),
        ("Y inclinado +0.14°", 0.0, 0.14, (-1.0, 1.2)),
        ("Y inclinado -0.14°", 0.0, -0.14, (2.0, 0.5)),
        ("X girado +0.10°, Y +0.00°", 0.10, 0.0, (0.5, 0.5)),
        ("giro puro +0.20° (sin skew)", 0.20, 0.20, (-1.5, -1.0)),
        ("skew -0.24°, giro +0.05°", 0.05, -0.19, (1.0, 1.0)),
    ]
    for name, a1d, a2d, off in cases:
        io, rep, true_skew, err = run_case(name, a1d, a2d, off)
        if abs(err) > 0.02:
            ok = False
            print("   FALLO: error de skew demasiado grande")
        if os.path.exists(klipper_skew):
            ang0, ang = check_with_klipper_skew(io, rep, klipper_skew)
            print("   ángulo X-Y físico: sin corrección %.4f°  con skew_correction %.4f°"
                  % (ang0, ang))
            if abs(ang - 90.) > 0.02:
                ok = False
                print("   FALLO: la corrección de Klipper no deja 90°")
    # punto frontal fuera de alcance (límite Y alto): debe marcar y_ok False
    print()
    io = SimIO(0., math.radians(0.1), (0., 0.), limits=(-2, 250, 5.0, 220))
    sx, sy = io.phys(12., 6.)
    io.c = (12. - sx, 6. - sy)
    sc = xc.Scanner(io, xc.ScanParams())
    pts = [sc.scan_dot(nx, ny, True) for nx, ny in
           [(12., 6.), (220., 6.), (220., 198.), (12., 198.)]]
    rep = xc.evaluate(pts, 208., 192.)
    print("Puntos frontales inalcanzables en -Y: y_ok=%s  skew medido %+.4f° (real +0.1000°)"
          % ([p['y_ok'] for p in pts], rep['skew_deg']))
    if pts[0]['y_ok'] or pts[1]['y_ok'] or abs(rep['skew_deg'] - 0.1) > 0.03:
        ok = False
        print("   FALLO en el caso de puntos inalcanzables")

    # --- casos duros ---------------------------------------------------
    print()
    hard = [
        ("origen desplazado 3.5 mm", dict(), (3.5, -3.0)),
        ("ruido 0.012 e histéresis 0.12", dict(noise=0.012, hyst=0.12), (1.0, 1.0)),
        ("punto más pequeño (R=4.5)", dict(dot_radius=4.5), (-1.0, 2.0)),
        ("histéresis 0.23 (calibración completa)", dict(hyst=0.23), (0.5, -1.5)),
    ]
    for name, kw, off in hard:
        io, rep, true_skew, err = run_case(name, 0.02, 0.17, off, **kw)
        if abs(err) > 0.03:
            ok = False
            print("   FALLO: error de skew demasiado grande")
    # escala: la máquina se mueve ~0.2 % más de lo que se le manda (como en
    # la calibración real del usuario). La escala medida debe ser 1/s y el
    # rotation_distance sugerido = actual / escala = actual * s.
    print()
    s = (1.00196, 1.00183)
    io, rep, true_skew, err = run_case("escala +0.196 % / +0.183 %", 0.02, 0.17,
                                       (1., -1.), scale=s)
    for ax, sc_true, key in (('X', s[0], 'scale_x'), ('Y', s[1], 'scale_y')):
        got = rep[key]
        sug = 32. / got
        print("   escala %s medida %.5f (esperada %.5f)  rotation_distance "
              "sugerido %.4f (esperado %.4f)" % (ax, got, 1. / sc_true, sug,
                                                 32. * sc_true))
        if abs(got - 1. / sc_true) > 0.0003 or abs(sug - 32. * sc_true) > 0.01:
            ok = False
            print("   FALLO: escala o rotation_distance sugerido incorrectos")
    if abs(err) > 0.03:
        ok = False
        print("   FALLO: la escala estropea el cálculo del skew")
    io, rep, true_skew, err = run_case("sin escala (control)", 0.02, 0.17, (1., -1.))
    for key in ('scale_x', 'scale_y'):
        if abs(rep[key] - 1.) > SCALE_OK:
            ok = False
            print("   FALLO: %s = %.5f debería ser 1" % (key, rep[key]))
    # histéresis grande (Prusa da 50-250 um para el PINDA): la altura medida
    # debe ser la de DISPARO bajando (como en el homing), no la de liberación
    for hyst in (0.05, 0.23):
        io = SimIO(0., 0., (0., 0.), hyst=hyst, noise=0.003)
        sc = xc.Scanner(io, xc.ScanParams())
        zh = sc.z_hit(12., 6.)           # altura real de disparo: 0.55
        print("z_hit con histéresis %.2f: %.3f (real 0.550)" % (hyst, zh))
        if zh is None or abs(zh - 0.55) > 0.03:
            ok = False
            print("   FALLO: z_hit depende de la histéresis")
    # sin respuesta del sensor: debe dar error claro y no chocar
    io = SimIO(0., 0., (0., 0.), dot_gain=0.0, base=-2.0)
    sc = xc.Scanner(io, xc.ScanParams())
    try:
        sc.scan_dot(12., 6., True)
        ok = False
        print("FALLO: debería haber dado error sin respuesta del sensor")
    except xc.XYZCalError as e:
        print("Sin respuesta del sensor -> error controlado: %s" % str(e)[:60])
    # sensor ya activado en z_start (algo metálico debajo / cama caliente)
    io = SimIO(0., 0., (0., 0.), base=5.0)
    sc = xc.Scanner(io, xc.ScanParams())
    try:
        sc.z_hit(12., 6.)
        ok = False
        print("FALLO: debería avisar de sensor activo en z_start")
    except xc.XYZCalError as e:
        print("Sensor activo en z_start -> error controlado: %s" % str(e)[:60])
    # --- XYZCAL_LOCATE: precisión del centro frente a la verdad simulada ---
    for lname, kw in (("LOCATE normal", {}),
                      ("LOCATE ruido+histéresis", dict(noise=0.012, hyst=0.23))):
        rounds = []
        for rnd in range(3):
            # cada ronda = "re-homing" con un origen distinto (repetibilidad)
            off = (0.3 * (rnd - 1), -0.25 * (rnd - 1))
            io = SimIO(0., 0., (3.5 + off[0], -2.0 + off[1]),
                       seed=10 + rnd, **kw)
            sc = xc.Scanner(io, xc.ScanParams())
            res = []
            for (nx, ny) in [(12., 6.), (220., 6.), (220., 198.), (12., 198.)]:
                r = sc.scan_dot(nx, ny, coarse=True)
                # centro en coordenadas de máquina verdadero = dot - origen
                res.append((r, (io.dots[len(res)][0] - io.c[0],
                                io.dots[len(res)][1] - io.c[1])))
            rounds.append(res)
        st = xc.locate_stats([[r for r, _ in rd] for rd in rounds])
        worst = 0.
        for k, s_ in enumerate(st):
            # error de cada ronda frente a su verdad (sin el cambio de origen)
            for rd in rounds:
                r, t = rd[k]
                worst = max(worst, abs(r['x'] - t[0]), abs(r['y'] - t[1]))
        spreads = max(max(s_['x_spread'], s_['y_spread']) for s_ in st)
        print("%-26s error máx. vs verdad %.3f mm, dispersión entre rondas %.3f mm"
              % (lname, worst, spreads))
        if worst > 0.06:
            ok = False
            print("   FALLO: LOCATE impreciso")
    # borde que no aparece a la primera: debe subir Z de barrido y recuperarse
    io = SimIO(0., 0., (3.5, -2.0), seed=5)
    sc = xc.Scanner(io, xc.ScanParams())
    orig = sc._edge
    fails = {'n': 0}

    def flaky(axis, cx, cy, direction):
        if axis == 'x' and direction > 0 and fails['n'] < 1:
            fails['n'] += 1
            return None
        return orig(axis, cx, cy, direction)
    sc._edge = flaky
    try:
        r = sc.scan_dot(12., 6., True)
        tx, ty = io.dots[0][0] - io.c[0], io.dots[0][1] - io.c[1]
        if abs(r['x'] - tx) > 0.06 or r['z_scan'] <= 0.4:
            ok = False
            print("FALLO: reintento de borde impreciso", r['x'], tx, r['z_scan'])
        else:
            print("Borde ausente -> sube Z de barrido y se recupera (z=%.3f)"
                  % r['z_scan'])
    except xc.XYZCalError as e:
        ok = False
        print("FALLO: no se recuperó:", e)
    sc._edge = lambda a, cx, cy, d: None
    try:
        sc.scan_dot(12., 6., True)
        ok = False
        print("FALLO: debería dar error si nunca hay borde")
    except xc.XYZCalError as e:
        print("Sin borde nunca -> error claro: %s" % str(e)[:70])
    # --- imanes: detección de regiones frente a rectángulos simulados ---
    io = SimIO(0., 0., (0., 0.), noise=0.008, hyst=0.05, seed=21, base=1.5,
               dot_gain=0.0)
    io.warp = (0.15, 130., 105., 150.)          # alabeo suave de 0.15 mm
    true_boxes = [(100., 187., 122., 229., 0.12), (125., -18., 147., 24., -0.10),
                  (54., 97., 96., 119., 0.14), (154., 97., 196., 119., 0.11)]
    io.bumps = [b for b in true_boxes]
    sc = xc.Scanner(io, xc.ScanParams(z_start=3.0, z_floor=0.6))
    xs = [35. + i * 205. / 25 for i in range(26)]
    ys = [6. + j * 192. / 24 for j in range(25)]
    zmap = xc.magnet_scan(sc, xs, ys, lambda x, y: (x, y))
    res = xc.magnet_analyze(zmap, xs, ys, margin=2.0)
    print("Imanes: %d regiones, sigma %.3f, umbral %.3f, %d lecturas"
          % (len(res['regions']), res['sigma'], res['thr'], io.queries))
    # cobertura: cada rectángulo real (recortado a la malla) debe quedar dentro
    # de alguna región detectada, y no debe haber regiones sin imán
    def inter(a, b):
        w = min(a[2], b[2]) - max(a[0], b[0])
        h = min(a[3], b[3]) - max(a[1], b[1])
        return max(w, 0) * max(h, 0)
    grid = (35., 6., 240., 198.)
    for tb in true_boxes:
        clip = (max(tb[0], grid[0]), max(tb[1], grid[1]),
                min(tb[2], grid[2]), min(tb[3], grid[3]))
        area = (clip[2] - clip[0]) * (clip[3] - clip[1])
        if area <= 0:
            continue
        cov = max(inter(clip, r) for r in res['regions']) / area \
            if res['regions'] else 0
        print("   imán %s cubierto al %.0f%%" % (tb[:4], 100 * cov))
        if cov < 0.97:
            ok = False
            print("   FALLO: imán no cubierto")
    for r in res['regions']:
        if not any(inter(r[:4], tb[:4]) > 0 for tb in true_boxes):
            ok = False
            print("   FALLO: región falsa", r)
        # no debe ser mucho mayor que el imán (margen de la malla + 2 mm)
        best = max(inter(r[:4], tb[:4]) for tb in true_boxes)
        if (r[2] - r[0]) * (r[3] - r[1]) > 2.2 * best + 400:
            ok = False
            print("   FALLO: región demasiado grande", r)
    if len(res['regions']) != 4:
        ok = False
        print("   FALLO: se esperaban 4 regiones, hay", len(res['regions']))
    # sin imanes no debe marcar nada (solo alabeo y ruido)
    io = SimIO(0., 0., (0., 0.), noise=0.008, seed=22, base=1.5, dot_gain=0.0)
    io.warp = (0.15, 130., 105., 150.)
    sc = xc.Scanner(io, xc.ScanParams(z_start=3.0, z_floor=0.6))
    zmap = xc.magnet_scan(sc, xs, ys, lambda x, y: (x, y))
    res = xc.magnet_analyze(zmap, xs, ys)
    print("Sin imanes: %d regiones (esperado 0)" % len(res['regions']))
    if res['regions']:
        ok = False
        print("   FALLO: falsos positivos")
    # --- escala Z con bloque de acero de espesor conocido ---
    for zs_true, extra, tref in ((1.0, 0.0, 0.0), (1.002, 0.0, 0.0),
                                 (0.9985, 0.0, 0.0), (1.002, 0.3, 10.0)):
        T = 20.0
        io = SimIO(0., 0., (0., 0.), noise=0.006, hyst=0.05, seed=31,
                   base=1.5, dot_gain=0.0)
        io.zscale = zs_true
        io.warp = (0.05, 130., 105., 150.)
        # bloque de espesor T (más 'extra' de diferencia de material);
        # con referencia gruesa tref, las dos referencias están sobre bloque
        io.bumps = [(90., 95., 110., 115., T + extra)]
        refs = [(60., 105.), (140., 105.)]
        if tref:
            io.bumps += [(50., 95., 70., 115., tref + extra),
                         (130., 95., 150., 115., tref + extra)]
        p = xc.ScanParams(z_start=T + 3., z_floor=0.6, z_coarse=0.5,
                          z_bisect_steps=6)
        sc = xc.Scanner(io, p)
        ref, blk = xc.zscale_measure(sc, refs, (100., 105.), 3,
                                     lambda x, y: (x, y))
        rep = xc.zscale_report(ref, blk, T, tref, 8.0)
        print("Escala Z real k=%.4f material +%.1f ref %.0f -> medida k=%.5f "
              "(rd sugerido %.5f, incert. %.3f%%)" % (
                  zs_true, extra, tref, rep['k'], rep['rd_new'],
                  100 * rep['rel_unc']))
        # k real = zs_true (la máquina se mueve zs_true veces lo mandado)
        tol = 0.0006 if extra == 0 else 0.0006
        if abs(rep['k'] - zs_true) > tol:
            ok = False
            print("   FALLO: escala Z incorrecta")
    # estadística pura
    fake = [[dict(x=10.0, y=20.0, rx=3., z_hit=1.5, y_ok=True)],
            [dict(x=10.2, y=19.9, rx=3.2, z_hit=1.4, y_ok=True)]]
    st = xc.locate_stats(fake)[0]
    if not (abs(st['x'] - 10.1) < 1e-9 and abs(st['y_spread'] - 0.1) < 1e-9
            and abs(st['x_spread'] - 0.2) < 1e-9):
        ok = False
        print("FALLO: locate_stats")
    else:
        print("locate_stats correcto")
    print("Z mínima alcanzada en todas las pruebas respetó z_floor (-0.4): sin colisión simulada")
    print("\nRESULTADO:", "TODO CORRECTO" if ok else "HAY FALLOS")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
