#!/usr/bin/env python3
"""Supervisor ARK SE clusteru - vsechny mapy z jedne instalace, v jedne instanci AMP.

Spousti se misto ShooterGameServer. Mapy dostane jako argumenty (kazde
zaskrtavatko v AMP se rozvine na jmeno mapy nebo prazdno), zbytek konfigurace
cte z promennych prostredi, ktere naplni sablona.

Prikazy prijima na stdin - tim je konzole AMP zaroven ovladacim panelem.
"""
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from rcon import RconClient, RconError

# Poradi je zavazne: urcuje prirazeni portu a musi sedet s arkclusterports.json.
CANONICAL_MAPS = [
    "TheIsland", "TheCenter", "ScorchedEarth_P", "Ragnarok", "Aberration_P",
    "Extinction", "Valguero_P", "Genesis", "CrystalIsles", "Gen2",
    "LostIsland", "Fjordur", "Aquatica",
]

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE.parent / "supervisor-state.json"

_print_lock = threading.Lock()


def emit(source, line):
    """Jediny zpusob, jak z supervisoru neco vypsat.

    Prefix je nutny - konzole AMP sliva 13 zdroju do jednoho okna a bez nej
    by se v tom nedalo vyznat. Zaroven na tenhle format ciluji Console.*Regex
    v sablone, takze se nesmi menit bez upravy arkcluster.kvp.
    """
    with _print_lock:
        sys.stdout.write(f"[{source}] {line}\n")
        sys.stdout.flush()


def log(msg):
    emit("supervisor", msg)


# --------------------------------------------------------------------------
# Konfigurace
# --------------------------------------------------------------------------

def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_int(name, default):
    try:
        return int(float(env(name) or default))
    except ValueError:
        return int(default)


def env_bool(name):
    return env(name) not in ("", "0", "False", "false")


class Config:
    def __init__(self):
        base = env("ARK_BASE_DIR")
        # Fallback pro rucni spousteni mimo AMP: supervisor/ je pod base dir.
        self.base = Path(base) if base else HERE.parent
        self.game = self.base / "376030"
        self.binary = self.game / "ShooterGame/Binaries/Linux/ShooterGameServer"
        self.workdir = self.game / "ShooterGame/Binaries/Win64"
        self.config_dir = self.game / "ShooterGame/Saved/Config/LinuxServer"
        self.cluster_dir = self.base / "clusterdata"
        self.log_dir = self.base / "logs"

        self.session_name = env("ARK_SESSION_NAME") or "ARK Cluster"
        self.cluster_id = env("ARK_CLUSTER_ID") or "arkcluster"
        self.rcon_password = env("ARK_RCON_PASSWORD")
        self.server_password = env("ARK_SERVER_PASSWORD")
        self.max_players = env_int("ARK_MAX_PLAYERS", 70)
        self.start_delay = env_int("ARK_START_DELAY", 90)
        self.ready_timeout = env_int("ARK_READY_TIMEOUT", 600)
        self.save_timeout = env_int("ARK_SAVE_TIMEOUT", 180)
        self.cpu_pinning = env_bool("ARK_CPU_PINNING")
        self.auto_restart = env_bool("ARK_AUTO_RESTART")
        self.cross_chat = env_bool("ARK_CROSS_CHAT")
        self.rate_preset = env("ARK_RATE_PRESET") or "normal"
        self.custom_options = env("ARK_CUSTOM_OPTIONS")
        self.bind_ip = env("ARK_BIND_IP") or "0.0.0.0"

        self.game_port = env_int("ARK_GAME_PORT", 7777)
        self.query_port = env_int("ARK_QUERY_PORT", 27015)
        self.rcon_port = env_int("ARK_RCON_PORT", 27100)

    def ports_for(self, index):
        """Porty se odvozuji od kanonickeho indexu mapy, ne od poradi spusteni.

        Diky tomu ma mapa porad stejny port, i kdyz se jina odskrtne.
        """
        return {
            "game": self.game_port + 2 * index,
            "query": self.query_port + index,
            "rcon": self.rcon_port + index,
        }


# --------------------------------------------------------------------------
# Topologie CPU
# --------------------------------------------------------------------------

def physical_cores():
    """Vrati seznam fyzickych jader jako mnoziny logickych CPU (vcetne SMT).

    Cte se ze sysfs, ne z lscpu - je to spolehlivejsi a bez zavislosti na
    formatu vystupu.
    """
    cores = []
    seen = set()
    base = Path("/sys/devices/system/cpu")
    try:
        cpu_dirs = sorted(base.glob("cpu[0-9]*"),
                          key=lambda p: int(p.name[3:]))
    except OSError:
        return cores
    for cpu_dir in cpu_dirs:
        siblings_file = cpu_dir / "topology/thread_siblings_list"
        try:
            raw = siblings_file.read_text().strip()
        except OSError:
            continue
        cpus = set()
        for part in raw.split(","):
            if "-" in part:
                lo, hi = part.split("-")
                cpus.update(range(int(lo), int(hi) + 1))
            else:
                cpus.add(int(part))
        key = tuple(sorted(cpus))
        if key not in seen:
            seen.add(key)
            cores.append(cpus)
    return cores


def assign_cores(map_names):
    """Kazda mapa dostane vlastni fyzicke jadro. Zadne dve nesdileji vlakno."""
    cores = physical_cores()
    if not cores:
        log("VAROVANI: topologii CPU se nepodarilo precist, pinning vypnut")
        return {}
    # Jadro 0 nechavame systemu, pokud je jader dost.
    pool = cores[1:] if len(cores) > len(map_names) else cores
    if len(pool) < len(map_names):
        log(f"VAROVANI: {len(map_names)} map na {len(pool)} fyzickych jader - "
            f"nektere se o jadro podeli")
    return {name: pool[i % len(pool)] for i, name in enumerate(map_names)}


# --------------------------------------------------------------------------
# Presety a stav
# --------------------------------------------------------------------------

def load_json(path, fallback):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        log(f"VAROVANI: {path.name} nelze precist ({exc}), pouzivam vychozi")
        return fallback


class State:
    """Prezije restart instance.

    Bez toho by se cluster po necekanem restartu tise vratil na normal
    uprostred eventu a nikdo by si toho hned nevsiml.
    """

    def __init__(self, default_preset):
        self.data = {"preset": default_preset, "pending": None}
        if STATE_FILE.exists():
            loaded = load_json(STATE_FILE, None)
            if isinstance(loaded, dict):
                self.data.update(loaded)

    @property
    def preset(self):
        return self.data.get("preset") or "normal"

    @property
    def pending(self):
        return self.data.get("pending")

    def set_pending(self, name):
        self.data["pending"] = name
        self.save()

    def apply_pending(self):
        """Zavola se pri startu mapy - tim se preset 'veze' na restartu."""
        if self.data.get("pending"):
            self.data["preset"] = self.data.pop("pending")
            self.data["pending"] = None
            self.save()
        return self.preset

    def save(self):
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(STATE_FILE)
        except OSError as exc:
            log(f"VAROVANI: stav se nepodarilo ulozit: {exc}")


# --------------------------------------------------------------------------
# Generovani sdilene konfigurace
# --------------------------------------------------------------------------

def write_shared_config(cfg, presets, preset_name):
    """Slozi Game.ini a GameUserSettings.ini ze sablon a zamkne je.

    Soubory se generuji zamerne: ARK si GameUserSettings.ini pri vypnuti
    prepisuje vlastnimi hodnotami, takze bez chmod 444 by se konfigurace
    postupne rozesla mezi mapami.
    """
    cfg.config_dir.mkdir(parents=True, exist_ok=True)
    preset = presets.get(preset_name) or {}
    gameini_rates = preset.get("gameini", {})

    rates = "\n".join(f"{key}={value}" for key, value in sorted(gameini_rates.items()))
    if not rates:
        rates = "; (preset 'normal' - zadne prepisy)"

    targets = [
        ("Game.ini.template", "Game.ini", {"{{RATES}}": rates}),
        ("GameUserSettings.ini.template", "GameUserSettings.ini", {
            "{{RCON_PASSWORD}}": cfg.rcon_password,
            "{{SESSION_NAME}}": cfg.session_name,
            "{{MAX_PLAYERS}}": str(cfg.max_players),
        }),
    ]
    for template_name, out_name, substitutions in targets:
        template = HERE / "config" / template_name
        if not template.exists():
            log(f"VAROVANI: chybi sablona {template_name}, {out_name} se negeneruje")
            continue
        text = template.read_text()
        for needle, value in substitutions.items():
            text = text.replace(needle, value)
        out = cfg.config_dir / out_name
        try:
            if out.exists():
                out.chmod(0o644)
            out.write_text(text)
            out.chmod(0o444)
        except OSError as exc:
            log(f"CHYBA: {out_name} nelze zapsat: {exc}")
    log(f"Sdileny konfig vygenerovan, preset '{preset_name}', zamceno na 444")


# --------------------------------------------------------------------------
# Jedna mapa
# --------------------------------------------------------------------------

class MapServer:
    READY_RE = re.compile(r"\bjoined this ARK|\bServer: |Full Startup", re.I)
    JOIN_RE = re.compile(r"^[\d.]+_[\d.]+: (?P<user>.+?) joined this ARK!")
    LEAVE_RE = re.compile(r"^[\d.]+_[\d.]+: (?P<user>.+?) left this ARK!")

    def __init__(self, name, index, cfg, cores):
        self.name = name
        self.index = index
        self.cfg = cfg
        self.cores = cores
        self.ports = cfg.ports_for(index)
        self.proc = None
        self.rcon = RconClient("127.0.0.1", self.ports["rcon"], cfg.rcon_password)
        self.ready = False
        self.restarts = 0
        self.players = set()
        self._reader = None

    # --- spousteni ---

    def command_line(self, preset_rates):
        opts = [
            self.name,
            "listen",
            f"Port={self.ports['game']}",
            f"QueryPort={self.ports['query']}",
            "RCONEnabled=True",
            f"RCONPort={self.ports['rcon']}",
            f"ServerAdminPassword={self.cfg.rcon_password}",
            f"MaxPlayers={self.cfg.max_players}",
            # Kazda mapa MUSI mit vlastni save adresar, jinak si prepisou svet.
            f"AltSaveDirectoryName={self.name}",
            f'SessionName="{self.cfg.session_name} - {self.name}"',
            f"MultiHome={self.cfg.bind_ip}",
            "RCONServerGameLogBuffer=600",
        ]
        if self.cfg.server_password:
            opts.append(f"ServerPassword={self.cfg.server_password}")
        for key, value in sorted(preset_rates.items()):
            opts.append(f"{key}={value}")
        for extra in self.cfg.custom_options.replace("\n", "?").split("?"):
            extra = extra.strip()
            if extra:
                opts.append(extra)

        args = [str(self.cfg.binary), "?".join(opts)]
        args += [
            f"-ClusterDirOverride={self.cfg.cluster_dir}",
            f"-clusterid={self.cfg.cluster_id}",
            "-AutoManagedMods",
            "-Crossplay",
            "-server",
            "-log",
            "-servergamelog",
        ]
        return args

    def start(self, preset_rates):
        if self.running:
            emit(self.name, "uz bezi, start preskocen")
            return
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.cluster_dir.mkdir(parents=True, exist_ok=True)
        self.ready = False
        self.players.clear()

        args = self.command_line(preset_rates)
        emit(self.name, f"start na portech game={self.ports['game']} "
                        f"query={self.ports['query']} rcon={self.ports['rcon']}")
        self.proc = subprocess.Popen(
            args,
            cwd=str(self.cfg.workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=1,
            text=True,
            errors="replace",
        )
        if self.cfg.cpu_pinning and self.cores:
            try:
                os.sched_setaffinity(self.proc.pid, self.cores)
                emit(self.name, f"pin na CPU {sorted(self.cores)}")
            except OSError as exc:
                emit(self.name, f"VAROVANI: pinning selhal: {exc}")

        self._reader = threading.Thread(target=self._pump_output, daemon=True)
        self._reader.start()

    def _pump_output(self):
        """Cte vystup serveru a normalizuje ho na format, ktery ceka AMP."""
        log_path = self.cfg.log_dir / f"{self.name}.log"
        try:
            handle = open(log_path, "a", errors="replace")
        except OSError:
            handle = None
        try:
            for raw in self.proc.stdout:
                line = raw.rstrip("\n")
                if handle:
                    handle.write(line + "\n")
                    handle.flush()
                join = self.JOIN_RE.search(line)
                leave = self.LEAVE_RE.search(line)
                if join:
                    self.players.add(join.group("user"))
                    emit(self.name, f">>> {join.group('user')} joined this ARK!")
                elif leave:
                    self.players.discard(leave.group("user"))
                    emit(self.name, f"<<< {leave.group('user')} left this ARK!")
                elif line.strip():
                    emit(self.name, line)
        except (OSError, ValueError):
            pass
        finally:
            if handle:
                handle.close()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def wait_ready(self, timeout):
        """Ready = RCON odpovida. Spolehlivejsi nez hledani hlasky v logu."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running:
                emit(self.name, "CHYBA: proces skoncil driv, nez nabehl")
                return False
            try:
                self.rcon.command("ListPlayers")
                self.ready = True
                emit(self.name, "ready")
                return True
            except (RconError, OSError):
                time.sleep(5)
        emit(self.name, f"VAROVANI: nenabehla do {timeout} s")
        return False

    # --- ukonceni ---

    def stop(self, fixes, save_timeout):
        """Zebrik: uklid -> SaveWorld -> DoExit -> SIGINT -> SIGKILL.

        SaveWorld navic je cely rozdil mezi 'server se vypnul' a 'neprisel
        jsi o svet'. LinuxGSM ho pred zabitim nedela.
        """
        if not self.running:
            return
        if self.ready:
            self._run_fixes(fixes)
            self._rcon_quiet("cheat SaveWorld", wait=save_timeout)
            self._rcon_quiet("cheat DoExit")

        if self._wait_exit(save_timeout):
            emit(self.name, "ukonceno korektne")
            return
        # ARK poslouncha na SIGINT, ne SIGTERM - proto tenhle krok.
        emit(self.name, "neukoncil se sam, posilam SIGINT")
        self._signal(signal.SIGINT)
        if self._wait_exit(60):
            emit(self.name, "ukonceno po SIGINT")
            return
        emit(self.name, "CHYBA: nereaguje, SIGKILL (svet muze byt starsi)")
        self._signal(signal.SIGKILL)
        self._wait_exit(15)

    def _run_fixes(self, commands):
        """Uklid patri PRED vypnuti - repopulace pak probehne pri bootu."""
        for cmd in commands:
            try:
                reply = self.rcon.command(cmd)
            except (RconError, OSError) as exc:
                emit(self.name, f"FIX SELHAL {cmd!r}: {exc}")
                continue
            # Preklep v nazvu tridy tise nedela nic - proto se loguje odpoved.
            emit(self.name, f"FIX {cmd} -> {reply or '(prazdna odpoved)'}")

    def _rcon_quiet(self, cmd, wait=0):
        try:
            self.rcon.command(cmd)
            if wait:
                emit(self.name, f"{cmd} odeslano")
        except (RconError, OSError) as exc:
            emit(self.name, f"VAROVANI: {cmd} selhalo: {exc}")

    def _signal(self, sig):
        try:
            self.proc.send_signal(sig)
        except (OSError, AttributeError):
            pass

    def _wait_exit(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.running:
                return True
            time.sleep(1)
        return not self.running


# --------------------------------------------------------------------------
# Supervisor
# --------------------------------------------------------------------------

class Supervisor:
    MAX_RESTARTS = 5

    def __init__(self, cfg, map_names):
        self.cfg = cfg
        self.presets = load_json(HERE / "presets.json", {"normal": {}})
        self.fixes = load_json(HERE / "mapfixes.json", {})
        self.state = State(cfg.rate_preset)
        self.stopping = threading.Event()
        self.quiesced = False

        cores = assign_cores(map_names) if cfg.cpu_pinning else {}
        self.maps = {}
        for name in map_names:
            index = CANONICAL_MAPS.index(name)
            self.maps[name] = MapServer(name, index, cfg, cores.get(name))

    # --- presety ---

    def rates(self):
        preset = self.presets.get(self.state.preset) or {}
        return preset.get("cmdline", {})

    def fixes_for(self, name):
        value = self.fixes.get(name)
        if isinstance(value, list):
            return value
        default = self.fixes.get("_default")
        return default if isinstance(default, list) else []

    # --- zivotni cyklus ---

    def start_all(self):
        preset = self.state.apply_pending()
        write_shared_config(self.cfg, self.presets, preset)
        log(f"Startuji {len(self.maps)} map, preset '{preset}', "
            f"prodleva {self.cfg.start_delay} s mezi mapami")
        for position, server in enumerate(self.maps.values()):
            if self.stopping.is_set():
                return
            server.start(self.rates())
            server.wait_ready(self.cfg.ready_timeout)
            # Prodleva az mezi mapami, ne po posledni.
            if position < len(self.maps) - 1 and self.cfg.start_delay:
                log(f"cekam {self.cfg.start_delay} s pred dalsi mapou")
                self.stopping.wait(self.cfg.start_delay)
        log("vsechny mapy nastartovany")

    def stop_all(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        log("ukoncuji cluster - kazda mapa se nejdriv uklidi a ulozi")
        for server in self.maps.values():
            server.stop(self.fixes_for(server.name), self.cfg.save_timeout)
        for server in self.maps.values():
            try:
                server.rcon.close()
            except OSError:
                pass
        log("cluster ukoncen")

    def restart_map(self, server):
        server.stop(self.fixes_for(server.name), self.cfg.save_timeout)
        preset = self.state.apply_pending()
        write_shared_config(self.cfg, self.presets, preset)
        server.start(self.rates())
        server.wait_ready(self.cfg.ready_timeout)

    # --- smycky na pozadi ---

    def watchdog_loop(self):
        while not self.stopping.wait(30):
            if not self.cfg.auto_restart:
                continue
            for server in self.maps.values():
                if self.stopping.is_set():
                    return
                if server.proc is None or server.running:
                    continue
                if server.restarts >= self.MAX_RESTARTS:
                    continue
                server.restarts += 1
                emit(server.name, f"spadla, restart {server.restarts}/"
                                  f"{self.MAX_RESTARTS}")
                server.start(self.rates())
                server.wait_ready(self.cfg.ready_timeout)

    def chat_loop(self):
        """Preposila chat mezi mapami. Nahrazuje Cross-Ark-Chat."""
        while not self.stopping.wait(5):
            if not self.cfg.cross_chat or self.quiesced:
                continue
            for server in self.maps.values():
                if not server.ready or not server.running:
                    continue
                try:
                    chat = server.rcon.command("GetChat")
                except (RconError, OSError):
                    continue
                for line in chat.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    self._relay(server, line)

    def _relay(self, origin, line):
        match = re.match(r"^(?:[\d.]+_[\d.]+:\s*)?(.+?)\s*\((.+?)\):\s*(.+)$", line)
        if not match:
            return
        _steam, player, message = match.groups()
        emit(origin.name, f"<{player}> {message}")
        payload = f"[{origin.name}] {player}: {message}"
        for server in self.maps.values():
            if server is origin or not server.ready or not server.running:
                continue
            try:
                server.rcon.command(f"ServerChat {payload}")
            except (RconError, OSError):
                pass

    def metrics_loop(self):
        while not self.stopping.wait(60):
            up = sum(1 for s in self.maps.values() if s.running)
            players = sum(len(s.players) for s in self.maps.values())
            # Na tenhle radek cili Console.MetricsRegex - z toho jsou grafy v AMP.
            emit("supervisor", f"METRICS maps_up={up} players={players}")

    # --- prikazy z konzole AMP ---

    def find(self, name):
        if name in self.maps:
            return self.maps[name]
        matches = [s for key, s in self.maps.items()
                   if key.lower().startswith(name.lower())]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            log(f"mapa '{name}' nebezi nebo neexistuje")
        else:
            log(f"'{name}' je nejednoznacne: "
                f"{', '.join(s.name for s in matches)}")
        return None

    def handle(self, raw):
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()
        if not parts:
            return
        cmd, args = parts[0].lower(), parts[1:]
        rest = raw.split(None, 1)[1] if len(parts) > 1 else ""

        if cmd in ("doexit", "exit", "stop") and not args:
            self.stop_all()
        elif cmd == "help":
            self.cmd_help()
        elif cmd == "status":
            self.cmd_status()
        elif cmd == "players":
            self.cmd_players()
        elif cmd == "start" and args:
            self._start_one(args[0])
        elif cmd == "stop" and args:
            self._stop_one(args[0])
        elif cmd == "restart" and args:
            self._restart_one(args[0])
        elif cmd == "broadcast" and rest:
            self.cmd_all_rcon(f"Broadcast {rest}", quiet=True)
            log(f"broadcast: {rest}")
        elif cmd == "say" and len(args) >= 2:
            server = self.find(args[0])
            if server:
                server.rcon.command(f"ServerChat {rest.split(None, 1)[1]}")
        elif cmd == "rcon" and len(args) >= 2:
            self.cmd_rcon(args[0], rest.split(None, 1)[1])
        elif cmd == "rconall" and rest:
            self.cmd_all_rcon(rest)
        elif cmd == "saveall":
            self.cmd_all_rcon("cheat SaveWorld")
        elif cmd in ("kick", "ban") and args:
            self.cmd_moderate(cmd, args[0])
        elif cmd == "whereis" and args:
            self.cmd_whereis(args[0])
        elif cmd == "event":
            self.cmd_event(args)
        elif cmd == "quiesce":
            self.cmd_quiesce(True)
        elif cmd == "dequiesce":
            self.cmd_quiesce(False)
        else:
            log(f"neznamy prikaz '{raw}' - napis 'help'")

    def cmd_help(self):
        for line in [
            "status                prehled map",
            "players               hraci po mapach",
            "start|stop|restart <mapa>",
            "broadcast <zprava>    na vsechny mapy",
            "say <mapa> <zprava>",
            "rcon <mapa> <prikaz>  |  rconall <prikaz>",
            "saveall               SaveWorld na vsech mapach",
            "kick|ban <hrac>       supervisor mapu dohleda sam",
            "whereis <hrac>",
            "event list|status|set <preset>|apply",
            "quiesce | dequiesce   pauza zapisu pro zalohu za behu",
            "DoExit                korektni ukonceni celeho clusteru",
        ]:
            log("  " + line)

    def cmd_status(self):
        log(f"preset '{self.state.preset}'"
            + (f", ceka '{self.state.pending}' na pristi restart"
               if self.state.pending else ""))
        for server in self.maps.values():
            if server.running:
                state = "ready" if server.ready else "startuje"
                pid = server.proc.pid
            else:
                state, pid = "STOJI", "-"
            cores = sorted(server.cores) if server.cores else "-"
            log(f"  {server.name:<16} {state:<9} pid={pid:<8} "
                f"game={server.ports['game']} hracu={len(server.players)} cpu={cores}")

    def cmd_players(self):
        total = 0
        for server in self.maps.values():
            if not server.running:
                continue
            names = sorted(server.players)
            total += len(names)
            log(f"  {server.name:<16} {len(names):>3}  "
                f"{', '.join(names) if names else '-'}")
        log(f"  celkem {total} hracu")

    def _start_one(self, name):
        server = self.find(name)
        if server:
            server.start(self.rates())
            server.wait_ready(self.cfg.ready_timeout)

    def _stop_one(self, name):
        server = self.find(name)
        if server:
            server.stop(self.fixes_for(server.name), self.cfg.save_timeout)

    def _restart_one(self, name):
        server = self.find(name)
        if server:
            self.restart_map(server)

    def cmd_rcon(self, name, command):
        server = self.find(name)
        if not server:
            return
        try:
            emit(server.name, server.rcon.command(command) or "(prazdna odpoved)")
        except (RconError, OSError) as exc:
            emit(server.name, f"RCON selhalo: {exc}")

    def cmd_all_rcon(self, command, quiet=False):
        for server in self.maps.values():
            if not server.ready or not server.running:
                continue
            try:
                reply = server.rcon.command(command)
                if not quiet:
                    emit(server.name, reply or "(prazdna odpoved)")
            except (RconError, OSError) as exc:
                emit(server.name, f"RCON selhalo: {exc}")

    def _locate(self, player):
        for server in self.maps.values():
            for known in server.players:
                if known.lower() == player.lower() or player.lower() in known.lower():
                    return server, known
        return None, None

    def cmd_whereis(self, player):
        server, known = self._locate(player)
        if server:
            log(f"{known} je na mape {server.name}")
        else:
            log(f"hrac '{player}' nenalezen na zadne mape")

    def cmd_moderate(self, action, player):
        """kick/ban z tlacitka v AMP - supervisor mapu dohleda sam."""
        server, known = self._locate(player)
        if not server:
            log(f"hrac '{player}' nenalezen, {action} neprovedeno")
            return
        command = f"KickPlayer {known}" if action == "kick" else f"BanPlayer {known}"
        try:
            emit(server.name, f"{action}: {server.rcon.command(command) or 'OK'}")
        except (RconError, OSError) as exc:
            emit(server.name, f"{action} selhalo: {exc}")

    def cmd_event(self, args):
        available = [k for k in self.presets if not k.startswith("_")]
        if not args or args[0] == "status":
            log(f"aktivni preset: {self.state.preset}")
            log(f"ceka na restart: {self.state.pending or '(nic)'}")
            return
        if args[0] == "list":
            for name in available:
                marker = " <- aktivni" if name == self.state.preset else ""
                log(f"  {name}{marker}")
            return
        if args[0] == "apply":
            log("aplikuji preset rolling restartem, mapa po mape")
            threading.Thread(target=self._rolling_restart, daemon=True).start()
            return
        if args[0] == "set" and len(args) > 1:
            name = args[1]
            if name not in available:
                log(f"preset '{name}' neexistuje, dostupne: {', '.join(available)}")
                return
            self.state.set_pending(name)
            log(f"preset '{name}' nastaven - projevi se pri PRISTIM startu mapy. "
                f"Rani restart ho vezme s sebou, nebo pouzij 'event apply'.")
            return
        log("pouziti: event list | status | set <preset> | apply")

    def _rolling_restart(self):
        for server in self.maps.values():
            if self.stopping.is_set():
                return
            if not server.running:
                continue
            self.restart_map(server)
        log("rolling restart hotov")

    def cmd_quiesce(self, on):
        """Umozni zalohu bez vypnuti serveru.

        AMP tohle vola pres App.QuiesceCommand - doted to umel jen Minecraft,
        protoze slo o dvojici prikazu do konzole. Nasi konzoli pisem my.
        """
        if on:
            self.cmd_all_rcon("cheat SaveWorld", quiet=True)
            self.quiesced = True
            log("QUIESCED - svety ulozeny, zaloha muze bezet")
        else:
            self.quiesced = False
            log("DEQUIESCED - normalni provoz")


# --------------------------------------------------------------------------
# Vstupni bod
# --------------------------------------------------------------------------

def main():
    cfg = Config()

    selected, unknown = [], []
    for arg in sys.argv[1:]:
        name = arg.strip()
        if not name:
            continue
        if name in CANONICAL_MAPS:
            if name not in selected:
                selected.append(name)
        else:
            unknown.append(name)
    for name in unknown:
        log(f"VAROVANI: neznama mapa '{name}', ignoruji")

    if not selected:
        log("CHYBA: nevybrana zadna mapa. Zaskrtni aspon jednu v nastaveni "
            "instance (sekce Maps) a spust znovu.")
        return 1
    if not cfg.binary.exists():
        log(f"CHYBA: server nenalezen na {cfg.binary}. Spust nejdriv Update.")
        return 1
    if not cfg.rcon_password:
        log("CHYBA: neni nastavene RCON heslo. Bez nej supervisor mapy neovlada "
            "- doplnit v nastaveni instance (RCON Password).")
        return 1

    # Kanonicke poradi, ne poradi argumentu - porty musi sedet s ports.json.
    selected.sort(key=CANONICAL_MAPS.index)
    supervisor = Supervisor(cfg, selected)

    def on_signal(signum, _frame):
        log(f"signal {signum}, ukoncuji")
        supervisor.stop_all()

    # SIGINT je to, co posila LinuxGSM; SIGTERM to, co posila AMP a systemd.
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    log(f"ARK cluster supervisor: {', '.join(selected)}")
    threading.Thread(target=supervisor.start_all, daemon=True).start()
    for loop in (supervisor.watchdog_loop, supervisor.chat_loop,
                 supervisor.metrics_loop):
        threading.Thread(target=loop, daemon=True).start()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                supervisor.handle(line)
            except Exception as exc:  # konzole nesmi shodit supervisor
                log(f"prikaz selhal: {exc}")
            if supervisor.stopping.is_set():
                break
    except KeyboardInterrupt:
        pass
    finally:
        supervisor.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
