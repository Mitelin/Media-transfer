# Media-transfer

Python finalizer pro Sonarr post-import/post-upgrade workflow. První verze je bezpečně nastavená na `dry_run: true`, takže po doplnění endpointů a API klíčů nejdřív jen loguje vyhodnocení a plánované akce.

Důležité: script pracuje jen s údržbovými knihovnami v `source_prefix`. Cílovou knihovnu nevyhodnocuje a nepoužívá jako Sonarr pracovní stav. Pokud je cílem CZ, údržbová knihovna je typicky English a z ní se hotová season převádí do CZ cíle.

Aktuální pravidla této verze:

- jazyk se vyhodnocuje po relevantních monitored epizodách
- fyzická `Season NN` složka se přesouvá jen jako celek, nikdy částečně
- volné soubory bez `Season NN` složky můžou vytvořit partial per-file plán
- partial přesun bere k videu i externí titulkové sidecary jako `S01E01.en.srt`
- monitored/relevant epizoda bez souboru je hard stop pro celou season
- whole-season move odmonitoruje Sonarr season a zároveň konkrétní episode IDs jako pojistku
- reálný move je chráněný kombinací `safety.dry_run: false`, `--execute`, Docker URL režimu a preflight kontrol

## 1. Doplnění konfigurace

Vytvoř lokální config ze šablony:

```bash
cp config/sonarr-finalizer.example.yml config/sonarr-finalizer.yml
```

Potom uprav soubor:

```text
config/sonarr-finalizer.yml
```

Lokální `config/sonarr-finalizer.yml` obsahuje API klíče a je záměrně v `.gitignore`.

Doplň hlavně:

```yaml
active_instance: "anime"

sonarr_instances:
  anime:
    url: "http://sonarr-anime:8989"
    lan_url: "http://LAN_SONARR_HOST:8990"
    tailscale_url: "http://TAILSCALE_SONARR_HOST:8990"
    api_key: "PUT_ANIME_SONARR_API_KEY_HERE"

  tv:
    url: "http://sonarr:8989"
    lan_url: "http://LAN_SONARR_HOST:8989"
    tailscale_url: "http://TAILSCALE_SONARR_HOST:8989"
    api_key: "PUT_TV_SONARR_API_KEY_HERE"
```

A zkontroluj path mappingy. `source_prefix` je údržbová knihovna sledovaná Sonarrem, `target_prefix` je cílové umístění pro přesun:

```yaml
paths:
  mappings:
    - source_prefix: "/anime-jp"
      target_prefix: "/anime-en"
      final_language: "en"
```

Pokud Sonarr vrátí season mimo `source_prefix`, script ji přeskočí ještě před jazykovou evaluací a nic neunmonitoruje.

`url` je produkční Docker URL používaná uvnitř Docker sítě. `lan_url` a `tailscale_url` jsou vývojové/testovací fallbacky zvenku.

Pro read-only vývoj z Windows jsou v configu také lokální mount překlady:

```yaml
paths:
  local_mounts:
    - docker_prefix: "/anime-en"
      local_prefix: '\\NAS_HOST\share\ANIME\English'
    - docker_prefix: "/anime-jp"
      local_prefix: '\\NAS_HOST\share\ANIME\Japanese'
    - docker_prefix: "/tv-cz"
      local_prefix: '\\NAS_HOST\share\SERIALY\Czech'
    - docker_prefix: "/tv-en"
      local_prefix: '\\NAS_HOST\share\SERIALY\English'
```

Tyto překlady se používají jen pro lokální čtení souborů a `ffprobe` testy. Plánovaný move a Sonarr logika stále pracují s Docker cestami.

Lokální překlady jsou defaultně vypnuté. Zapínají se pouze při vývoji z Windows pomocí:

```bash
--enable-local-mounts
```

Po nasazení na stroj s Dockerem tento přepínač nepoužívej; script tam má číst přímo `/anime-*` a `/tv-*` mounty.

## 2. Instalace závislostí

```bash
pip install -r requirements.txt
```

V Sonarr Docker containeru musí být dostupný také `ffprobe`, typicky z balíku `ffmpeg`.

## 3. Ruční dry-run test

Nejdřív ověř samotný tvar konfigurace bez volání Sonarr API a bez čtení médií:

```bash
python3 scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --validate-config
```

Prázdné API klíče v `config/sonarr-finalizer.example.yml` jsou jen warning, protože šablona nesmí obsahovat secrets. V lokálním `config/sonarr-finalizer.yml` je před reálným během doplň.

Nejdřív lze otestovat samotné API a root folders bez Sonarr eventu:

```bash
python3 scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --instance anime --url-mode lan --test-api
python3 scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --instance tv --url-mode lan --test-api
```

Z Windows/PowerShell použij stejný příkaz s lokálním Pythonem nebo aktivním `.venv`. Pro test přes Tailscale změň `--url-mode lan` na `--url-mode tailscale`.

Pro nalezení reálného `series_id`:

```bash
python3 scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --instance anime --url-mode lan --list-series --limit 20
python3 scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --instance tv --url-mode lan --list-series --filter "Body" --limit 10
python3 scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --instance tv --url-mode lan --list-series --root-prefix "/tv-en" --limit 20
```

Pro rychlou inspekci season bez `ffprobe` a bez sahání na soubory:

```bash
python3 scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --instance anime --url-mode lan --series-id 1 --season-number 1 --inspect-season
```

Script preferuje season z fyzické cesty souboru (`Season 04`, `S04E01`) před `seasonNumber` metadaty ze Sonarru. To je důležité pro anime, kde Sonarr někdy drží všechny epizody jako season 1, ale soubory jsou správně rozdělené do složek `Season 01`, `Season 02` atd. Epizody bez souboru se berou podle Sonarr season metadat, takže untrackované epizody bez path neblokují přesun jiné fyzické season.

Plná evaluace bez `--inspect-season` potřebuje běžet tam, kde existují Sonarr media cesty jako `/anime-en`, `/anime-jp`, `/tv-cz` a `/tv-en`. Z Windows přes LAN API tedy čekej bezpečný výsledek typu `file does not exist on disk`, pokud tyto Docker paths nejsou lokálně namountované.

Pokud chceš z Windows použít lokální UNC mounty a reálný `ffprobe`, přidej `--enable-local-mounts`:

```bash
python3 scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --instance tv --url-mode lan --series-id 24 --season-number 1 --enable-local-mounts
```

Config má zapnutý vývojový fallback:

```yaml
allow_sonarr_language_fallback: true
```

Když soubor není lokálně dostupný, script může pro diagnostiku použít `episodefile.languages` a `episodefile.mediaInfo` ze Sonarr API. Při běhu uvnitř Dockeru s dostupnými media mounty má přednost `ffprobe`.

Pokud `ffprobe` najde jen část jazyků, ale Sonarr metadata obsahují další audio/subtitle jazyky, script je při zapnutém fallbacku sloučí. V logu je takový případ označený jako `ffprobe-sonarr-api-merged`.

Pro ruční simulaci eventu v dry-run režimu:

```bash
python3 scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --instance anime --url-mode lan --series-id 1 --season-number 1 --imported-file-path "/anime-jp/Test/Season 01/Test S01E01.mkv"
```

Když season projde jazykovou evaluací, dry-run vypíše strukturovaný `Move plan` JSON a potom čitelné kroky:

```text
Move plan: {...}
DRY RUN: would move /tv-en/Example/Season 02 to /tv-cz/Example/Season 02
DRY RUN: would use temporary destination /tv-cz/Example/Season 02.__moving__
DRY RUN: would unmonitor moved episodes for season 2: [1234, 1235]
DRY RUN: would rescan series 123
```

Unmonitoring se u whole-season přesunu plánuje na dvou úrovních: script odmonitoruje Sonarr season a zároveň odmonitoruje konkrétní přesouvané episode IDs. To chrání běžné chování Sonarru i případy, kde Sonarr fyzickou `Season 04` stále interně eviduje jako epizody jedné dlouhé season 1. U partial loose-folder přesunu se odmonitorují jen konkrétní přesunuté episode IDs.

Pro fyzické složky `Season 01`, `Season 02` atd. platí whole-season režim: pokud je season mix final a ne-final souborů, script nepřesune nic a čeká, dokud všechny relevantní epizody nejsou ve finálním jazyce. Pokud jsou soubory volně v kořenové/random složce bez `Season NN` adresáře, script může vytvořit partial plán po jednotlivých souborech: final EN/multilang EN soubory přesune a unmonitoruje, JP-only nebo missing epizody nechá na místě a dál monitored. Při partial přesunu přibalí externí titulky ve stejné složce se stejným základem názvu, například `A Gatherer's Adventure in Isekai S01E01.en.srt` k `A Gatherer's Adventure in Isekai S01E01.mp4`.

Monitored/relevant epizoda bez souboru je hard stop pro celou danou season. Script ji bere jako signál, že season ještě vychází nebo není kompletní, a nepřesune nic ani v loose-folder režimu.

Linux/Sonarr container styl:

```bash
export sonarr_eventtype=Download
export sonarr_series_id=123
export sonarr_episodefile_seasonnumber=2
export sonarr_episodefile_path="/anime-jp/Test/Season 02/Test S02E01.mkv"
python3 scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --url-mode docker
```

PowerShell styl:

```powershell
$env:sonarr_eventtype = "Download"
$env:sonarr_series_id = "123"
$env:sonarr_episodefile_seasonnumber = "2"
$env:sonarr_episodefile_path = "/anime-jp/Test/Season 02/Test S02E01.mkv"
python scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --url-mode lan
```

## 4. Zapnutí skutečných změn

Nejdřív nech `safety.dry_run: true` a ověř logy. Skutečné přesuny a Sonarr API změny povol až po testu na dummy sérii:

```yaml
safety:
  dry_run: false
```

Potom spusť script s přepínačem:

```bash
python3 scripts/sonarr_post_import_finalizer.py --config config/sonarr-finalizer.yml --execute
```

Bez `--execute` a bez vypnutého `dry_run` script nic nepřesune.

Skutečný non-dry-run běh je defaultně povolený jen s `--url-mode docker`, aby se omylem neprovedly Sonarr změny z Windows/LAN testu. Přepínač `--allow-non-docker-execute` existuje jen pro vědomé pokročilé testování.

Před skutečným přesunem script provede move preflight:

```text
source folder musí existovat a být složka
destination nesmí existovat
temporary destination nesmí existovat
destination parent se případně vytvoří
```

Pokud preflight najde chybu, script nepřesune soubory, neunmonitoruje epizody a nespustí rescan.

Pokud selže skutečný přesun až po přesunutí do temporary cíle nebo před finálním rename, script se pokusí přesunuté soubory nebo složku vrátit zpět na původní místo. Sonarr unmonitoring a rescan se spustí až po úspěšném dokončení move kroku.

## 5. Sonarr Custom Script wrappery

Pro nasazení jsou připravené dva wrappery:

```text
scripts/sonarr_finalizer_tv.sh
scripts/sonarr_finalizer_anime.sh
```

Po zkopírování na Docker host například do `/srv/scripts/sonarr` nastav práva:

```text
/srv/scripts/sonarr/sonarr_post_import_finalizer.py
/srv/scripts/sonarr/sonarr_finalizer_tv.sh
/srv/scripts/sonarr/sonarr_finalizer_anime.sh
/srv/scripts/sonarr/sonarr-finalizer.yml
```

Soubor `sonarr-finalizer.yml` je kopie lokálního `config/sonarr-finalizer.yml`.

```bash
chmod +x /srv/scripts/sonarr/sonarr_finalizer_tv.sh
chmod +x /srv/scripts/sonarr/sonarr_finalizer_anime.sh
chmod +x /srv/scripts/sonarr/sonarr_post_import_finalizer.py
```

V Sonarr TV Custom Script nastav cestu:

```text
/scripts/sonarr_finalizer_tv.sh
```

V Sonarr Anime Custom Script nastav cestu:

```text
/scripts/sonarr_finalizer_anime.sh
```

Wrappery používají `--url-mode docker` a nepoužívají `--enable-local-mounts`. V Sonarr containeru musí být dostupné `python3`, Python balíčky z `requirements.txt` a `ffprobe`.
