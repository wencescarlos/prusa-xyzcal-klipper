#!/usr/bin/env python3
# Prueba offline de la integración con Klipper (clase PrusaXYZCal, KlipperIO
# y los comandos G-code) sin impresora ni Klipper instalado.
#
# Sustituye los objetos de Klipper (toolhead, probe, gcode, skew_correction,
# homing...) por imitaciones mínimas con la misma interfaz, y usa la máquina
# simulada de test_simulacion.py como "hardware". Comprueba sobre todo qué
# pasa cuando algo falla: que la boquilla vuelve arriba, que la posición de
# G-code se sincroniza y que los límites de los ejes quedan como estaban.
#
# Uso (desde la raíz del repositorio):
#   python3 tests/test_klipper.py
import math
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
import prusa_xyzcal as xc          # noqa: E402
import test_simulacion as ts       # noqa: E402


# ---------------------------------------------------------------------------
# Imitaciones de Klipper
# ---------------------------------------------------------------------------
class CommandError(Exception):
    pass


class Coord:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class FakeStepper:
    def __init__(self, name, rd):
        self.name, self.rd = name, rd

    def get_name(self):
        return self.name

    def get_rotation_distance(self):
        return (self.rd, 200)


class FakeRail:
    def __init__(self, pmin, pmax):
        self.pmin, self.pmax = pmin, pmax

    def get_range(self):
        return (self.pmin, self.pmax)

    def get_homing_info(self):
        return types.SimpleNamespace(speed=40., position_endstop=0.)

    def get_endstops(self):
        return []


class FakeKin:
    def __init__(self, env):
        self.env = env
        self.rails = [FakeRail(-2., 250.), FakeRail(-1., 220.),
                      FakeRail(0., 200.)]
        self.limits = [r.get_range() for r in self.rails]

    def get_steppers(self):
        return [FakeStepper('stepper_x', 32.), FakeStepper('stepper_y', 32.),
                FakeStepper('stepper_z', 8.)]


class FakeToolhead:
    def __init__(self, env):
        self.env = env
        self.kin = FakeKin(env)

    def get_status(self, eventtime):
        mn, mx = self.env.axis_min, self.env.axis_max
        return {'homed_axes': 'xyz', 'axis_minimum': Coord(*mn),
                'axis_maximum': Coord(*mx)}

    def manual_move(self, coord, speed):
        # Klipper rechaza los movimientos fuera de límites
        for i, v in enumerate(coord):
            if v is not None and not (self.env.axis_min[i] - 1e-9 <= v
                                      <= self.env.axis_max[i] + 1e-9):
                raise CommandError("Move out of range")
        self.env.sim.move(*coord)

    def get_position(self):
        return list(self.env.sim.m) + [0.]

    def dwell(self, t):
        pass

    def get_last_move_time(self):
        return 0.

    def wait_moves(self):
        pass

    def get_kinematics(self):
        return self.kin


class FakeProbe:
    def __init__(self, env, offsets=None):
        self.env = env
        self.offsets = offsets
        env_ = env

        class McuProbe:
            @staticmethod
            def query_endstop(print_time):
                env_.queries += 1
                if env_.fail_after is not None \
                        and env_.queries > env_.fail_after:
                    raise RuntimeError("Timeout con el MCU (simulado)")
                return env_.sim.triggered()
        self.mcu_probe = McuProbe()
        if offsets is not None:
            self.get_offsets = lambda gcmd=None: offsets + (1.5,)


class FakeSkew:
    def __init__(self, env):
        self.env = env
        self.xy_factor = self.xz_factor = self.yz_factor = 0.

    def _update_skew(self, xy, xz, yz):
        self.xy_factor, self.xz_factor, self.yz_factor = xy, xz, yz
        self.env.resets += 1


class FakeGcodeMove:
    def __init__(self, env):
        self.env = env

    def reset_last_position(self):
        self.env.resets += 1


class FakeGcode:
    def __init__(self, env):
        self.env = env
        self.cmds = {}

    def register_command(self, name, func, desc=None):
        self.cmds[name] = func

    def run_script_from_command(self, script):
        self.env.scripts.append(script)
        if script.startswith('G28'):
            if self.env.g28_fails:
                raise CommandError("G28 falla (simulado)")
            # el homing real restablece los límites de la cinemática
            kin = self.env.toolhead.kin
            kin.limits = [r.get_range() for r in kin.rails]


class FakeReactor:
    def monotonic(self):
        return 0.


DROP = object()     # en cfg=: quita la opción para usar el valor por defecto


class Env:
    """Una impresora simulada completa y el módulo cargado en ella."""

    def __init__(self, cfg=None, probe=True, offsets=(0., 0.), skew=True,
                 axis_min=(-2., -1., -2.), axis_max=(250., 220., 200.),
                 probe_section=None):
        self.sim = ts.SimIO(math.radians(0.05), math.radians(-0.1),
                            (0., -12.))
        self.axis_min, self.axis_max = axis_min, axis_max
        self.queries = 0
        self.fail_after = None
        self.resets = 0
        self.scripts = []
        self.g28_fails = False
        self.toolhead = FakeToolhead(self)
        self.objs = {'toolhead': self.toolhead, 'gcode_move': FakeGcodeMove(self),
                     'gcode': FakeGcode(self)}
        if probe:
            self.objs['probe'] = FakeProbe(self, offsets)
        if skew is True:
            self.objs['skew_correction'] = FakeSkew(self)
        elif skew is not None:
            self.objs['skew_correction'] = skew
        env = self

        class Printer:
            command_error = CommandError

            def lookup_object(self, name, default=None):
                return env.objs.get(name, default)

            def get_reactor(self):
                return FakeReactor()
        self.printer = Printer()
        values = {'z_floor': -0.4}
        values.update(cfg or {})
        for k in [k for k, v in values.items() if v is DROP]:
            del values[k]
        sections = {'probe': probe_section} if probe_section else {}

        class Section:
            def __init__(self, d):
                self.d = d

            def getfloat(self, k, default=None, **kw):
                return self.d.get(k, default)

        class Config:
            error = CommandError

            def get_printer(self):
                return env.printer

            def getfloat(self, k, default=None, **kw):
                return values.get(k, default)

            def getint(self, k, default=None, **kw):
                return values.get(k, default)

            def getfloatlist(self, k, default=None, count=None):
                return values.get(k, default)

            def has_section(self, name):
                return name in sections

            def getsection(self, name):
                return Section(sections[name])
        self.mod = xc.load_config(Config())
        self.out = []

    def run(self, name, **params):
        env = self

        class GCmd:
            def get(self, k, default=None):
                return str(params[k]) if k in params else default

            def get_int(self, k, default=None, **kw):
                return int(params.get(k, default))

            def get_float(self, k, default=None, **kw):
                v = params.get(k, default)
                if v is None:
                    raise CommandError("Falta %s" % k)
                return float(v)

            def respond_info(self, msg):
                env.out.append(msg)

            def error(self, msg):
                return CommandError(msg)
        self.objs['gcode'].cmds[name](GCmd())

    @property
    def z(self):
        return self.sim.m[2]


# ---------------------------------------------------------------------------
# Casos
# ---------------------------------------------------------------------------
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(cond)
    print("%-62s %s%s" % (name, "OK" if cond else "FALLO",
                          "" if cond or not detail else "  (%s)" % detail))


def expect_error(func, *a, **kw):
    try:
        func(*a, **kw)
    except Exception as e:
        return e
    return None


def case_calibrate_apply():
    env = Env()
    env.run('XYZCAL_CALIBRATE')
    rep = env.mod.last_report
    check("CALIBRATE mide los 4 puntos y deja Z en z_start",
          rep is not None and abs(env.z - 3.0) < 1e-9, "z=%.3f" % env.z)
    check("CALIBRATE detecta el skew simulado (+-0.01 grados)",
          abs(rep['skew_deg'] - math.degrees(math.radians(-0.15))) < 0.01,
          "%.4f" % rep['skew_deg'])
    env.run('XYZCAL_APPLY', SAVE='p')
    sk = env.objs['skew_correction']
    check("APPLY aplica xy_skew y guarda el perfil",
          abs(sk.xy_factor - rep['xy_factor']) < 1e-12
          and 'SKEW_PROFILE SAVE=p' in env.scripts)


def case_goto_without_probe():
    env = Env(probe=False)
    env.run('XYZCAL_GOTO', POINT=2, Z=5)
    check("GOTO funciona sin sonda configurada",
          env.sim.m[:2] == [220., 18.] and env.z == 5.0, str(env.sim.m))


def case_error_restores_z():
    env = Env()
    env.fail_after = 40          # el MCU "falla" a mitad del punto
    e = expect_error(env.run, 'XYZCAL_POINT', POINT=1)
    check("Error no previsto en POINT: se propaga", isinstance(e, RuntimeError),
          repr(e))
    check("  ...y la boquilla vuelve a z_start", abs(env.z - 3.0) < 1e-9,
          "z=%.3f" % env.z)
    check("  ...y se sincroniza la posición de G-code", env.resets >= 1)

    for cmd, kw in (('XYZCAL_CALIBRATE', {}), ('XYZCAL_LOCATE', {'REPEAT': 1}),
                    ('XYZCAL_ZTEST', {'POINT': 3})):
        env = Env()
        env.fail_after = 25
        expect_error(env.run, cmd, **kw)
        check("Error no previsto en %s: Z vuelve a z_start" % cmd,
              abs(env.z - 3.0) < 1e-9 and env.resets >= 1, "z=%.3f" % env.z)


def case_locate_g28_fails():
    env = Env()
    env.g28_fails = True
    e = expect_error(env.run, 'XYZCAL_LOCATE', REPEAT=2, POINTS='1')
    check("LOCATE con G28 fallido: error y Z en z_start",
          e is not None and abs(env.z - 3.0) < 1e-9, "z=%.3f" % env.z)


def case_z_floor_below_min():
    env = Env(axis_min=(-2., -1., 0.))
    moves = env.sim.moves
    e = expect_error(env.run, 'XYZCAL_POINT', POINT=1)
    check("z_floor < position_min de Z: error antes de mover",
          isinstance(e, CommandError) and 'position_min' in str(e)
          and env.sim.moves == moves, str(e))
    env = Env(axis_min=(-2., -1., 0.))
    env.run('XYZCAL_ZTEST', POINT=1, Z_FLOOR=0.2)
    check("  ...con Z_FLOOR dentro del recorrido sí mide",
          any('se activa a Z=' in m for m in env.out))


def case_default_z_floor():
    env = Env(cfg={'z_floor': DROP})
    check("z_floor por defecto coincide con el ejemplo de config (1.0)",
          env.mod.defaults['z_floor'] == 1.0, env.mod.defaults['z_floor'])


def case_out_of_range_point():
    env = Env(cfg={'point1': (300., 18.)})
    e = expect_error(env.run, 'XYZCAL_POINT', POINT=1, COARSE=0)
    check("Punto fuera del recorrido: error claro, sin recortar",
          isinstance(e, CommandError) and 'fuera del recorrido' in str(e)
          and env.sim.m[0] < 250., str(e))
    check("  ...y la boquilla vuelve a z_start", abs(env.z - 3.0) < 1e-9,
          "z=%.3f" % env.z)
    env = Env(cfg={'point1': (300., 18.)})
    e = expect_error(env.run, 'XYZCAL_GOTO', POINT=1)
    check("GOTO fuera del recorrido: error claro",
          isinstance(e, CommandError) and 'fuera del recorrido' in str(e))


def case_probe_offsets():
    env = Env(offsets=(23., 5.))
    check("Offsets leídos del objeto probe (get_offsets)",
          env.mod._probe_offsets(None) == (23., 5.))
    env = Env(offsets=None, probe_section={'x_offset': 24., 'y_offset': 6.})
    check("  ...si no hay get_offsets, se usan los de [probe]",
          env.mod._probe_offsets(None) == (24., 6.))
    # con offset X 23 la sonda no llega a X=0 (boquilla a -23 < -2)
    env = Env(offsets=(23., 5.))
    e = expect_error(env.run, 'XYZCAL_ZSCALE', THICKNESS=5, BLOCK_X=10,
                     BLOCK_Y=100)
    check("ZSCALE usa los offsets de la sonda para el alcance",
          isinstance(e, CommandError) and 'fuera del alcance' in str(e),
          str(e))


def case_magnets():
    env = Env()
    env.run('XYZCAL_MAGNETS', STEP=25, X_MIN=40, X_MAX=200, Y_MIN=20,
            Y_MAX=180)
    check("MAGNETS termina y deja Z en z_start",
          abs(env.z - 3.0) < 1e-9 and any('Mapa de anomalías' in m
                                          for m in env.out))


def install_fake_homing(env, trig_over=0.7, fail=False):
    mod = types.ModuleType('fake_homing')
    seen = {}

    class HomingMove:
        def __init__(self, printer, endstops, toolhead=None):
            pass

        def homing_move(self, pos, speed, probe_pos=False,
                        check_triggered=True):
            kin = env.toolhead.kin
            seen['limits_during'] = list(kin.limits)
            if fail:
                raise CommandError("No trigger after full movement")
            idx = 0 if pos[0] > 240 else 1
            trig = list(pos)
            trig[idx] = kin.rails[idx].pmax + trig_over
            env.sim.m[idx] = trig[idx]
            return trig

    class PrinterHoming:
        pass
    PrinterHoming.__module__ = 'fake_homing'
    mod.HomingMove = HomingMove
    mod.PrinterHoming = PrinterHoming
    sys.modules['fake_homing'] = mod
    env.objs['homing'] = PrinterHoming()
    return seen


def case_limits():
    env = Env()
    seen = install_fake_homing(env)
    orig = list(env.toolhead.kin.limits)
    env.run('XYZCAL_LIMITS', AXES='X', REPEAT=1)
    check("LIMITS amplía el límite solo durante el movimiento",
          seen['limits_during'][0][1] > orig[0][1]
          and env.toolhead.kin.limits == orig)
    check("  ...e informa del tope medido",
          any('tope en 250.700' in m for m in env.out))

    env = Env()
    install_fake_homing(env, fail=True)
    env.g28_fails = True
    orig = list(env.toolhead.kin.limits)
    e = expect_error(env.run, 'XYZCAL_LIMITS', AXES='X', REPEAT=1)
    check("LIMITS con fallo y G28 fallido: límites restaurados",
          e is not None and env.toolhead.kin.limits == orig,
          str(env.toolhead.kin.limits))


def case_apply_without_private_api():
    env = Env(skew=types.SimpleNamespace(xz_factor=0., yz_factor=0.))
    env.run('XYZCAL_CALIBRATE')
    e = expect_error(env.run, 'XYZCAL_APPLY')
    check("APPLY sin _update_skew: mensaje claro con el valor",
          isinstance(e, CommandError) and 'xy_skew' in str(e), str(e))


def case_fit_surface_all_outliers():
    # rejilla con ruido muy grande y umbral fijo muy pequeño: casi todos los
    # puntos superan el umbral; antes podía dividir por cero
    import random
    rng = random.Random(3)
    pts = [(x, y, rng.uniform(-1, 1)) for x in range(0, 100, 10)
           for y in range(0, 100, 10)]
    e = expect_error(xc.fit_surface, pts, fixed_thr=1e-4)
    check("fit_surface con casi todo anómalo no falla", e is None, repr(e))
    e = expect_error(xc.fit_surface, pts, min_thr=1e-6, k=0.01)
    check("  ...ni con umbral automático minúsculo", e is None, repr(e))


def main():
    for case in (case_calibrate_apply, case_goto_without_probe,
                 case_error_restores_z, case_locate_g28_fails,
                 case_z_floor_below_min, case_default_z_floor,
                 case_out_of_range_point,
                 case_probe_offsets, case_magnets, case_limits,
                 case_apply_without_private_api,
                 case_fit_surface_all_outliers):
        try:
            case()
        except Exception as e:     # un caso roto no debe ocultar los demás
            check(case.__name__ + " (excepción inesperada)", False, repr(e))
    ok = all(RESULTS)
    print("\nRESULTADO:", "TODO CORRECTO" if ok else "HAY FALLOS")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
