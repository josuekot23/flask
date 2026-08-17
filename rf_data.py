"""
Accès aux données InfluxDB pour le dashboard RF.
Toute la logique de requête est paramétrée (start, end, site) pour permettre
une interrogation dynamique depuis l'API Flask, au lieu d'un export statique.
"""

import csv
import json
import os
import re
from datetime import timedelta

import pandas as pd
from influxdb import InfluxDBClient

# ============================================================
# CONFIGURATION - surchargeable via variables d'environnement
# (fallback sur les valeurs ci-dessous si non définies, pour ne pas
# casser un déploiement local existant)
# ============================================================
INFLUX_HOST = os.environ.get("INFLUX_HOST", "localhost")
INFLUX_PORT = int(os.environ.get("INFLUX_PORT", "8086"))
INFLUX_USER = os.environ.get("INFLUX_USER") or None
INFLUX_PASSWORD = os.environ.get("INFLUX_PASSWORD") or None

# Bases système à ne jamais proposer dans la liste des "sites"
IGNORED_DATABASES = {"_internal"}

# Mesures disponibles (measurement InfluxDB == nom du champ, par convention du schéma)
AVAILABLE_MEASUREMENTS = ["signal", "cn", "extrapolation", "postber", "preber"]

MEASUREMENT_LABELS = {
    "signal": "Signal (dBm)",
    "cn": "C/N (dB)",
    "extrapolation": "Extrapolation",
    "postber": "Post-BER",
    "preber": "Pre-BER",
}

DEFAULT_MEASUREMENT = "signal"

# Fichier CSV des coordonnées de sites: site,latitude,longitude
SITE_LOCATIONS_FILE = os.environ.get(
    "SITE_LOCATIONS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites_locations.csv"),
)


def load_site_locations() -> dict:
    """
    Charge les coordonnées des sites depuis SITE_LOCATIONS_FILE (CSV avec
    colonnes site,latitude,longitude). Renvoie {site: {"lat": float, "lon": float}}.
    Un site absent du fichier, une ligne mal formée, ou le fichier lui-même
    manquant ne provoquent pas d'erreur: le site sera simplement absent de la carte.

    Le séparateur (virgule ou point-virgule) est détecté automatiquement, car
    un CSV édité/exporté depuis Excel en France utilise souvent ";" plutôt
    que ",". encoding="utf-8-sig" gère aussi le BOM qu'Excel ajoute parfois.
    """
    locations = {}
    if not os.path.exists(SITE_LOCATIONS_FILE):
        return locations

    with open(SITE_LOCATIONS_FILE, newline="", encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        except csv.Error:
            dialect = csv.excel  # repli sur la virgule si la détection échoue

        reader = csv.DictReader(f, dialect=dialect)
        for row in reader:
            site = (row.get("site") or "").strip()
            lat_raw = row.get("latitude")
            lon_raw = row.get("longitude")
            if not site or lat_raw is None or lon_raw is None:
                continue
            try:
                locations[site] = {"lat": float(lat_raw), "lon": float(lon_raw)}
            except (ValueError, TypeError):
                continue
    return locations

# Seuil (en minutes) au-delà duquel un site hors ligne passe en 3e catégorie
OFFLINE_LONG_THRESHOLD_MINUTES = 5 * 24 * 60  # 5 jours


def get_client(database: str = None) -> InfluxDBClient:
    return InfluxDBClient(
        host=INFLUX_HOST,
        port=INFLUX_PORT,
        username=INFLUX_USER,
        password=INFLUX_PASSWORD,
        database=database,
    )


# ============================================================
# LISTE DES BASES DISPONIBLES (= sites), pour peupler le sélecteur
# ============================================================
def list_databases() -> list:
    client = get_client()
    try:
        result = client.query("SHOW DATABASES")
    except Exception:
        return []
    names = sorted(
        p["name"] for p in result.get_points() if p["name"] not in IGNORED_DATABASES
    )
    return names


# ============================================================
# EXTRACTION DU PRÉFIXE NUMÉRIQUE DE FRÉQUENCE
# ============================================================
def extract_frequency_prefix(raw_frequence: str) -> str:
    """
    '474000000: France2 France4 franceinfo: F3Midi-Pyrénées F3Aquitaine'
    -> '474000000'

    Certains points ont un format légèrement différent (ancien firmware de
    sonde, espace en tête, etc.) où le numéro de fréquence ne se trouve pas
    strictement en tête de chaîne. Sans repli, ces points tombaient dans un
    groupe distinct portant la chaîne brute entière comme "fréquence" (ex:
    une entrée "514 MHz" propre ET une entrée "514000000: M6 W9..." séparée
    pour la même fréquence physique). On cherche donc le numéro n'importe où
    dans la chaîne en repli, avant d'abandonner sur "inconnue".
    """
    if raw_frequence is None:
        return "inconnue"
    text = str(raw_frequence).strip()

    match = re.match(r"^(\d{5,})", text)
    if match:
        return match.group(1)

    # Repli: numéro de fréquence (5 chiffres ou plus, pour ne pas confondre
    # avec un numéro de chaîne) trouvé ailleurs dans la chaîne.
    match = re.search(r"(\d{5,})", text)
    if match:
        return match.group(1)

    return "inconnue"


def extract_channels(raw_frequence: str) -> str:
    if raw_frequence is None:
        return ""
    parts = str(raw_frequence).split(":", 1)
    return parts[1].strip() if len(parts) > 1 else ""


# ============================================================
# GRANULARITÉ AUTOMATIQUE (évite de charger des millions de points
# quand la période demandée est large)
# ============================================================
def pick_group_interval(span: timedelta) -> str:
    if span <= timedelta(hours=2):
        return "10s"
    if span <= timedelta(hours=24):
        return "1m"
    if span <= timedelta(days=7):
        return "5m"
    if span <= timedelta(days=30):
        return "15m"
    if span <= timedelta(days=90):
        return "1h"
    if span <= timedelta(days=180):
        return "3h"
    # Au-delà de 6 mois (ex: un an complet), des buckets de 3h donneraient
    # encore ~2900 points par fréquence. On passe à 6h pour rester lisible
    # même sur de très longues périodes (ex: 1 an -> ~1460 points/fréquence).
    return "6h"


# Mesures où "frequence" est un TAG InfluxDB (donc groupable via GROUP BY
# InfluxQL, avec agrégation par intervalle de temps directement côté
# InfluxDB). Sur "signal", "frequence" est un FIELD (texte), pas un tag —
# InfluxQL ne peut pas grouper dessus, donc "signal" est agrégé côté pandas
# à la place (même résultat final: une moyenne par intervalle de temps et
# par fréquence), voir _fetch_from_database.
# À ajuster si le schéma d'écriture des sondes change.
MEASUREMENTS_WITH_FREQUENCE_TAG = {"cn", "extrapolation", "postber", "preber"}


# ============================================================
# STATUT ET STATISTIQUES PAR SITE (pour la page d'accueil)
# ============================================================
ALL_MEASUREMENTS = AVAILABLE_MEASUREMENTS + ["temperature"]


def list_measurements(database: str) -> list:
    client = get_client(database=database)
    try:
        result = client.query("SHOW MEASUREMENTS")
    except Exception:
        return []
    return sorted(p["name"] for p in result.get_points())


def classify_site_status(minutes_since_last, online_threshold_minutes) -> str:
    """
    Retourne une des 3 catégories :
    - "online"         : dernière donnée dans le seuil "en ligne" (ex: 5 min)
    - "offline_recent" : hors ligne, mais dernière donnée reçue il y a moins de 5 jours
    - "offline_long"    : hors ligne depuis plus de 5 jours (ou aucune donnée connue)
    """
    if minutes_since_last is None:
        return "offline_long"
    if minutes_since_last <= online_threshold_minutes:
        return "online"
    if minutes_since_last <= OFFLINE_LONG_THRESHOLD_MINUTES:
        return "offline_recent"
    return "offline_long"


def get_site_status(database: str, inactive_threshold_minutes: float = 5) -> dict:
    """
    Statut d'un site (base InfluxDB) : en ligne/hors ligne (basé sur la dernière
    mesure "signal", inactif si > inactive_threshold_minutes), + un maximum de
    stats annexes (fréquences actives, dernière température, volume de points).

    Le champ "category" classe le site en 3 groupes :
    online / offline_recent (< 5 jours) / offline_long (>= 5 jours ou jamais vu).
    """
    status = {
        "site": database,
        "online": False,
        "category": None,
        "last_seen": None,
        "minutes_since_last": None,
        "active_frequencies": None,
        "last_temperature": None,
        "points_last_hour": None,
        "measurements": [],
        "error": None,
    }

    client = get_client(database=database)

    try:
        result = client.query('SELECT last("signal") AS value FROM "signal"')
        points = list(result.get_points())
    except Exception as e:
        status["error"] = str(e)
        status["category"] = classify_site_status(None, inactive_threshold_minutes)
        return status

    if not points:
        status["error"] = "Aucune donnée dans la measurement signal"
        status["measurements"] = list_measurements(database)
        status["category"] = classify_site_status(None, inactive_threshold_minutes)
        return status

    last_time = pd.to_datetime(points[0]["time"], utc=True)
    now = pd.Timestamp.now(tz="UTC")
    delta_minutes = (now - last_time).total_seconds() / 60

    status["last_seen"] = last_time.isoformat()
    status["minutes_since_last"] = round(delta_minutes, 1)
    status["online"] = delta_minutes <= inactive_threshold_minutes
    status["category"] = classify_site_status(delta_minutes, inactive_threshold_minutes)

    # Nombre de fréquences ayant reporté dans les 10 dernières minutes
    try:
        freq_result = client.query(
            'SELECT mean("signal") AS value FROM "signal" WHERE time > now() - 10m '
            'GROUP BY "frequence" fill(none)'
        )
        status["active_frequencies"] = sum(1 for _ in freq_result.items())
    except Exception:
        pass

    # Dernière température connue
    try:
        temp_result = client.query('SELECT last("temperature") AS value FROM "temperature"')
        temp_points = list(temp_result.get_points())
        if temp_points:
            status["last_temperature"] = round(temp_points[0]["value"], 1)
    except Exception:
        pass

    # Volume de points sur la dernière heure (indicateur de débit/santé)
    try:
        count_result = client.query('SELECT count("signal") AS count FROM "signal" WHERE time > now() - 1h')
        count_points = list(count_result.get_points())
        if count_points:
            status["points_last_hour"] = count_points[0].get("count")
    except Exception:
        pass

    status["measurements"] = list_measurements(database)

    return status


def get_disconnection_events(database: str, start_iso: str, end_iso: str, gap_threshold_minutes: int = 5) -> list:
    """
    Reconstruit l'historique des déconnexions d'un site à partir des
    timestamps reçus sur la mesure "signal" (utilisée comme "battement de
    coeur" de la sonde, cohérent avec le seuil déjà utilisé pour le statut
    en ligne/hors ligne). Un intervalle sans aucune donnée pendant au moins
    gap_threshold_minutes minutes est considéré comme une déconnexion.

    Renvoie une liste d'événements (du plus récent au plus ancien):
    [{"start": iso str UTC, "end": iso str UTC ou None si en cours,
      "duration_minutes": float, "ongoing": bool}, ...]

    Limite connue: si une déconnexion a commencé avant "start_iso", sa durée
    affichée est tronquée à la fenêtre demandée (sous-estimée dans ce cas).
    """
    client = get_client(database=database)
    start_iso = _ensure_rfc3339(start_iso)
    end_iso = _ensure_rfc3339(end_iso)

    query = f"""
        SELECT count("signal") AS n
        FROM "signal"
        WHERE time >= '{start_iso}' AND time <= '{end_iso}'
        GROUP BY time(1m) fill(0)
    """
    try:
        result = client.query(query)
    except Exception:
        return []

    rows = list(result.get_points())
    if not rows:
        return []

    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"])
    df["online"] = df["n"] > 0
    df["group"] = (df["online"] != df["online"].shift()).cumsum()

    last_bucket_time = df["time"].iloc[-1]
    events = []

    for _, g in df[~df["online"]].groupby("group"):
        duration_minutes = len(g)  # 1 bucket = 1 minute
        if duration_minutes < gap_threshold_minutes:
            continue
        start_time = g["time"].iloc[0]
        end_bucket_time = g["time"].iloc[-1]
        ongoing = end_bucket_time == last_bucket_time
        events.append(
            {
                "start": start_time.isoformat(),
                "end": None if ongoing else (end_bucket_time + pd.Timedelta(minutes=1)).isoformat(),
                "duration_minutes": duration_minutes,
                "ongoing": ongoing,
            }
        )

    events.sort(key=lambda e: e["start"], reverse=True)
    return events


# Fichier JSON des liens d'accès terminal (ex: PiConnect) par site
TERMINAL_LINKS_FILE = os.environ.get(
    "TERMINAL_LINKS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "terminal_links.json"),
)


def load_terminal_links() -> dict:
    """
    Charge {site: url} depuis TERMINAL_LINKS_FILE. Fichier absent ou
    illisible -> dict vide, sans erreur bloquante.
    """
    if not os.path.exists(TERMINAL_LINKS_FILE):
        return {}
    try:
        with open(TERMINAL_LINKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_terminal_link(site: str, url: str) -> None:
    """
    Enregistre (ou retire, si url est vide) le lien terminal d'un site.
    Note: lecture-modification-écriture simple, pas de verrou fichier —
    suffisant pour un usage à quelques utilisateurs, mais deux sauvegardes
    strictement simultanées pourraient en écraser une (cas très improbable
    ici, non traité).
    """
    links = load_terminal_links()
    if url:
        links[site] = url
    else:
        links.pop(site, None)
    with open(TERMINAL_LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(links, f, ensure_ascii=False, indent=2)


def get_all_sites_status(inactive_threshold_minutes: float = 5) -> list:
    return [get_site_status(db, inactive_threshold_minutes) for db in list_databases()]


def _ensure_rfc3339(ts: str) -> str:
    """S'assure que le timestamp a un suffixe de timezone (Z ou +HH:MM),
    sinon InfluxDB renvoie 'invalid timestamp string'."""
    if ts.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", ts):
        return ts
    return ts + "Z"


def _influx_interval_to_pandas_freq(interval: str) -> str:
    """
    Convertit une durée InfluxQL ('10s', '1m', '5m', '15m', '1h', '3h', '6h')
    en fréquence pandas équivalente pour pd.Grouper. Nécessaire car InfluxQL
    utilise 'm' pour les minutes, alors qu'en pandas 'm' signifie "mois"
    (il faut 'min').
    """
    match = re.match(r"^(\d+)([smh])$", interval or "")
    if not match:
        return "1min"
    value, unit = match.groups()
    pandas_unit = {"s": "s", "m": "min", "h": "h"}[unit]
    return f"{value}{pandas_unit}"


def _fetch_from_database(database: str, start_iso: str, end_iso: str, group_interval: str, measurement: str) -> pd.DataFrame:
    if measurement not in AVAILABLE_MEASUREMENTS:
        raise ValueError(f"Mesure inconnue: {measurement}")

    client = get_client(database=database)

    start_iso = _ensure_rfc3339(start_iso)
    end_iso = _ensure_rfc3339(end_iso)
    where_sql = f"time >= '{start_iso}' AND time <= '{end_iso}'"

    if measurement in MEASUREMENTS_WITH_FREQUENCE_TAG:
        # "frequence" est un tag ici: InfluxQL peut grouper dessus directement,
        # l'agrégation par intervalle se fait donc côté InfluxDB.
        query = f"""
            SELECT mean("{measurement}") AS value
            FROM "{measurement}"
            WHERE {where_sql}
            GROUP BY time({group_interval}), "frequence" fill(none)
        """
        result = client.query(query)

        rows = []
        for (meas, tags), series in result.items():
            raw_freq = (tags or {}).get("frequence", "inconnue")
            for point in series:
                rows.append(
                    {
                        "time": point["time"],
                        "value": point["value"],
                        "raw_frequence": raw_freq,
                        "site": database,
                    }
                )
        df = pd.DataFrame(rows)

    else:
        # "frequence" est un field (texte) ici, ex: "signal". InfluxQL ne peut
        # pas faire GROUP BY dessus, donc on récupère les points bruts puis
        # on agrège par intervalle de temps + fréquence en pandas — même
        # principe que la branche tag ci-dessus, juste calculé côté Flask
        # plutôt que par InfluxDB. Nécessaire pour rester lisible sur de
        # longues périodes (le brut devient vite illisible/lourd).
        query = f"""
            SELECT "{measurement}" AS value, "frequence" AS raw_frequence
            FROM "{measurement}"
            WHERE {where_sql}
        """
        result = client.query(query)
        rows = list(result.get_points())
        df = pd.DataFrame(rows)

        if not df.empty:
            df["site"] = database
            df["time"] = pd.to_datetime(df["time"])
            pandas_freq = _influx_interval_to_pandas_freq(group_interval)
            df = (
                df.groupby([pd.Grouper(key="time", freq=pandas_freq), "raw_frequence", "site"])["value"]
                .mean()
                .reset_index()
            )

    if df.empty:
        return df

    df["time"] = pd.to_datetime(df["time"])
    df["frequence_hz"] = df["raw_frequence"].apply(extract_frequency_prefix)
    df["chaines"] = df["raw_frequence"].apply(extract_channels)
    return df


def _fetch_temperature_from_database(database: str, start_iso: str, end_iso: str, group_interval: str) -> pd.DataFrame:
    client = get_client(database=database)

    start_iso = _ensure_rfc3339(start_iso)
    end_iso = _ensure_rfc3339(end_iso)

    where_sql = f"time >= '{start_iso}' AND time <= '{end_iso}'"
    query = f"""
        SELECT mean("temperature") AS value
        FROM "temperature"
        WHERE {where_sql}
        GROUP BY time({group_interval}) fill(none)
    """

    result = client.query(query)

    rows = []
    for (meas, tags), series in result.items():
        for point in series:
            rows.append({"time": point["time"], "value": point["value"], "site": database})

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["time"] = pd.to_datetime(df["time"])
    return df


def fetch_temperature(start_iso: str, end_iso: str, database: str = None, group_interval: str = None) -> pd.DataFrame:
    """
    Récupère la température des sondes. Pas de regroupement par fréquence
    (un seul capteur de température par site). Si database est None, interroge
    toutes les bases et fusionne, une courbe par site.
    """
    if database:
        return _fetch_temperature_from_database(database, start_iso, end_iso, group_interval)

    frames = []
    for db in list_databases():
        try:
            df = _fetch_temperature_from_database(db, start_iso, end_iso, group_interval)
        except Exception:
            continue
        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)


def fetch_data(
    start_iso: str,
    end_iso: str,
    database: str = None,
    group_interval: str = None,
    measurement: str = DEFAULT_MEASUREMENT,
) -> pd.DataFrame:
    """
    start_iso / end_iso: chaînes ISO 8601 / RFC3339 (ex: '2026-07-20T00:00:00Z').
    database: nom de la base InfluxDB (= site) à interroger. Si None, interroge
              TOUTES les bases disponibles (hors IGNORED_DATABASES) et fusionne
              les résultats, avec une colonne "site" indiquant la base d'origine.
    group_interval: granularité InfluxDB (ex: '1m'). Si None, calculée automatiquement.
    measurement: une valeur parmi AVAILABLE_MEASUREMENTS (signal, cn, extrapolation,
                 postber, preber).
    """
    if database:
        return _fetch_from_database(database, start_iso, end_iso, group_interval, measurement)

    frames = []
    for db in list_databases():
        try:
            df = _fetch_from_database(db, start_iso, end_iso, group_interval, measurement)
        except Exception:
            # On ignore une base en échec (ex: measurement absente) et on continue les autres
            continue
        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    return pd.concat(frames, ignore_index=True)