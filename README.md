# ARK: Survival Evolved Cluster — AMP template

Celý ARK cluster v **jedné** instanci AMP. Zaškrtneš mapy, které chceš, a Python
supervisor uvnitř je spustí nad **jednou** instalací hry se **sdílenou** konfigurací.

Řeší to bolest 13 samostatných instancí: 13 × 15 GB stejných souborů, update, při
kterém se musí všechny zastavit a každá přepsat zvlášť, a konfigurace, která se
mezi nimi postupně rozejde.

## Co to umí

- **13 map z jedné instalace** — ~15 GB místo ~195 GB, jeden SteamCMD zápis při updatu
- **Ovládání po mapách zůstává** — konzole AMP je příkazový kanál (`AdminMethod=STDIO`)
- **Postupný start** s prodlevou; 13 serverů naráz stroj neustojí
- **CPU pinning** — každá mapa vlastní fyzické jádro včetně SMT sourozence
- **Cross-server chat** — nahrazuje Cross-Ark-Chat, bez druhého procesu
- **Evolution eventy** (2×/4×) — přepnou se při nejbližším restartu
- **Restartové fixy per mapa** — `DestroyWildDinos`, úly, hnízda, vejce
- **Zálohy za běhu** — `quiesce`/`dequiesce`, doteď to uměl jen Minecraft
- **Seznam hráčů, chat a kick/ban tlačítka** v UI AMP

## Instalace

V AMP přidej repozitář (Configuration → Instance Deployment):

```
cechacek/AMP-ARK-cluster-Supervisor-template-:main      produkce
cechacek/AMP-ARK-cluster-Supervisor-template-:staging   testovani
```

Na hostiteli musí být **systémový Python** verze zadané v nastavení instance
(výchozí 3.11) — z něj se staví venv:

```bash
sudo apt install python3.11 python3.11-venv     # Debian/Ubuntu
```

Pak vytvoř instanci ze šablony **ARK: Survival Evolved (Cluster)**, zaškrtni mapy
v sekci *Maps*, **nastav RCON heslo** (bez něj supervisor mapy neovládá) a spusť Update.

## Příkazy v konzoli

```
status                prehled map: bezi/nebezi, PID, jadro, hraci
players               hraci po mapach
start|stop|restart <mapa>
broadcast <zprava>    na vsechny mapy
say <mapa> <zprava>
rcon <mapa> <prikaz>  |  rconall <prikaz>
saveall
kick|ban <hrac>       supervisor mapu dohleda sam
whereis <hrac>
event list|status|set <preset>|apply
quiesce | dequiesce
DoExit                korektni ukonceni celeho clusteru
```

## Eventy

ARK čte multiplikátory **jen při startu** — živě je přepnout nejde. Event se proto
veze na ranním restartu:

```
event set 2x        nastavi preset, projevi se pri PRISTIM startu mapy
event apply         rolling restart hned, mapa po mape
```

Nebo přepni *Rate Preset* v nastavení instance. Pro opakující se víkendové eventy
nech scheduler AMP poslat `event set 2x` v pátek a `event set normal` v pondělí.

**Multiplikátory nežijí na jednom místě** a `presets.json` to respektuje:

| kde | co |
|---|---|
| příkazová řádka (`?Klic=hodnota`) | XP, taming, harvest, egg hatch, baby mature, mating interval, baby food |
| `Game.ini` — přes `?` to **nejde** | `LayEggIntervalMultiplier`, `BabyCuddleIntervalMultiplier` |

Proto supervisor `Game.ini` **generuje** ze šablony v tomhle repu a pak ho zamkne
na `chmod 444`. ARK si ten soubor jinak při vypnutí přepisuje vlastními hodnotami.

**Pozor na směr:** intervalové hodnoty se **snižují**. Napsat u „4×" všude `4.0`
by breeding čtyřnásobně *zpomalilo*.

## Konfigurace

Konfigurace je generovaná — needituj ji v instanci, přepíše se. Uprav místo toho:

| soubor | co |
|---|---|
| `supervisor/config/Game.ini.template` | stackování, zakázané spawny, rates |
| `supervisor/config/GameUserSettings.ini.template` | základní nastavení serveru |
| `supervisor/presets.json` | násobky eventů |
| `supervisor/mapfixes.json` | úklid před vypnutím, per mapa |

### Stackování

`ItemStackSizeMultiplier` funguje na většinu věcí, ale **kazící se maso ho ignoruje**.
Prime meat a mutton proto mají vlastní `ConfigOverrideItemMaxQuantity` s
`bIgnoreMultiplier=True`. U **muttonu** je hlášené, že to v některých verzích
nefunguje — ověř ve hře, ne jen v souboru.

### Restartové fixy

`DestroyWildDinos` **nemaže struktury**, které dinosauři postavili — proto se úly
hromadí a `DestroyAll BeeHive_C` je jeho nutný protějšek, ne doplněk.

Úklid běží **před** vypnutím mapy, aby repopulace (5–10 min) proběhla při bootu,
kdy nikdo nehraje. `DestroyAll` vyžaduje **přesnou** shodu názvu třídy a koncový
argument `1`; překlep tiše neudělá nic, takže supervisor loguje odpověď RCON.

Ledové wyverny na Ragnaroku (`Ragnarok_Wyvern_Override_Ice_C`) jsou v šabloně
**zakomentované** — odkomentuj, pokud tě trápí jejich overspawn.

## Porty

| | rozsah |
|---|---|
| Game | 7777 + 2×index (+1 peer) |
| Query | 27015 + index |
| RCON | 27100 + index |

Port se odvozuje z **kanonického** pořadí mapy, ne z pořadí spuštění — mapa má pořád
stejný port, i když jinou odškrtneš.

## Vývoj

`staging` na testování, `main` na produkci. Supervisor jde spustit i mimo AMP:

```bash
cd supervisor
ARK_BASE_DIR=/cesta/k/instanci/ ARK_RCON_PASSWORD=tajne \
  python3 arkclustersupervisor.py TheIsland Ragnarok
```
