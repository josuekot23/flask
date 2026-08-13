#!/usr/bin/env python3
"""
Dashboard RF dynamique : interroge InfluxDB à la demande (période + site
choisis dans l'UI) au lieu de générer un HTML statique avec toutes les
données pré-chargées.

Lancement:
    pip install flask influxdb pandas plotly
    python3 app.py
    -> http://localhost:8050
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import colorsys

from flask import Flask, jsonify, render_template, request, url_for
import plotly.express as px

import rf_data as rf

# Choix de fuseau pour l'affichage, sélectionnable depuis l'interface (menu
# dans le header) et mémorisé dans un cookie. "auto" utilise la vraie base de
# fuseaux horaires (bascule été/hiver automatique) ; utc1/utc2 forcent un
# décalage fixe, utile si la bascule automatique s'avère peu fiable sur le
# serveur, ou pour vérifier manuellement.
TZ_CHOICES = {
    "auto": "Auto (Europe/Paris)",
    "utc1": "UTC+1",
    "utc2": "UTC+2",
}
DEFAULT_TZ_CHOICE = "auto"
UTC_TZ = timezone.utc


def get_tz_choice() -> str:
    choice = request.cookies.get("tz_choice", DEFAULT_TZ_CHOICE)
    return choice if choice in TZ_CHOICES else DEFAULT_TZ_CHOICE


def get_display_tz():
    choice = get_tz_choice()
    if choice == "utc1":
        return timezone(timedelta(hours=1), name="UTC+1")
    if choice == "utc2":
        return timezone(timedelta(hours=2), name="UTC+2")
    return ZoneInfo("Europe/Paris")

app = Flask(__name__)


@app.context_processor
def inject_globals():
    return {"current_year": datetime.utcnow().year, "current_tz_choice": get_tz_choice()}


def _generate_distinct_colors(n):
    """
    Génère n couleurs réparties uniformément sur la roue chromatique (teinte
    espacée de 360°/n), pour une séparation visuelle maximale entre courbes
    quel que soit le nombre de fréquences — contrairement à une palette fixe
    qui recycle des teintes proches au-delà d'un certain nombre de séries.
    """
    colors = []
    n = max(n, 1)
    for i in range(n):
        hue = i / n
        r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.95)
        colors.append(f"rgb({int(r * 255)},{int(g * 255)},{int(b * 255)})")
    return colors


MAX_TRACE_POINTS = 2000


def _decimate_minmax(sub, max_points=MAX_TRACE_POINTS):
    """
    Réduit le nombre de points affichés au-delà de max_points, en gardant le
    min ET le max de chaque intervalle (pas une simple décimation qui
    prendrait 1 point sur N) — pour ne jamais masquer un vrai pic ou une
    vraie chute de signal, tout en gardant le tracé lisible visuellement.
    sub doit déjà être trié par temps.
    """
    n = len(sub)
    if n <= max_points:
        return sub

    bucket_count = max(1, max_points // 2)
    bucket_size = n / bucket_count
    keep_idx = set()
    for i in range(bucket_count):
        start = int(i * bucket_size)
        end = min(int((i + 1) * bucket_size), n)
        if start >= end:
            continue
        bucket = sub.iloc[start:end]
        keep_idx.add(bucket["value"].idxmin())
        keep_idx.add(bucket["value"].idxmax())

    return sub.loc[sorted(keep_idx)]


def _freq_sort_key(freq):
    """Tri numérique croissant sur la fréquence (en Hz); les valeurs non
    numériques (ex: "inconnue") sont reléguées à la fin, triées entre elles."""
    return (0, int(freq)) if freq.isdigit() else (1, freq)


def build_traces(df, measurement_label, display_tz):
    """Construit les traces Plotly (format JSON) + les infos de la liste de fréquences."""
    df = df.sort_values("time")

    frequencies = sorted(df["frequence_hz"].unique(), key=_freq_sort_key)
    palette = _generate_distinct_colors(len(frequencies))
    color_map = {f: palette[i] for i, f in enumerate(frequencies)}

    traces = []
    freq_info = []

    for freq in frequencies:
        sub = df[df["frequence_hz"] == freq]
        sub = _decimate_minmax(sub)
        derniere_chaine = sub["chaines"].iloc[-1] if not sub["chaines"].isna().all() else ""
        freq_mhz = int(freq) / 1_000_000 if freq.isdigit() else freq
        label = f"{freq_mhz:.0f} MHz" if isinstance(freq_mhz, float) else freq

        traces.append(
            {
                "type": "scattergl",
                "x": sub["time"].dt.tz_convert(display_tz).dt.strftime("%Y-%m-%dT%H:%M:%S").tolist(),
                "y": sub["value"].tolist(),
                "mode": "lines",
                "name": label,
                "line": {"color": color_map[freq], "width": 1.2, "shape": "spline", "smoothing": 0.7},
                "hovertemplate": (
                    f"<b>{label}</b><br>%{{x}}<br>{measurement_label}: %{{y}}<br>"
                    f"Chaînes: {derniere_chaine}<extra></extra>"
                ),
            }
        )
        freq_info.append(
            {
                "label": label,
                "color": color_map[freq],
                "channels": derniere_chaine or "—",
                "n_points": len(sub),
            }
        )

    return traces, freq_info


def build_temperature_traces(df, display_tz):
    """Une courbe par site (pas de regroupement par fréquence pour la température)."""
    df = df.sort_values("time")
    sites = sorted(df["site"].unique())
    palette = px.colors.qualitative.Set2 + px.colors.qualitative.Pastel
    color_map = {s: palette[i % len(palette)] for i, s in enumerate(sites)}

    traces = []
    for s in sites:
        sub = df[df["site"] == s]
        traces.append(
            {
                "type": "scattergl",
                "x": sub["time"].dt.tz_convert(display_tz).dt.strftime("%Y-%m-%dT%H:%M:%S").tolist(),
                "y": sub["value"].round(2).tolist(),
                "mode": "lines",
                "name": s,
                "line": {"color": color_map[s], "width": 1.5, "shape": "spline", "smoothing": 0.7},
                "hovertemplate": f"<b>{s}</b><br>%{{x}}<br>Température: %{{y:.1f}} °C<extra></extra>",
            }
        )
    return traces


def format_time_ago(minutes):
    if minutes is None:
        return "inconnu"
    if minutes < 1:
        return "à l'instant"
    if minutes < 60:
        return f"il y a {int(round(minutes))} min"
    hours = minutes / 60
    if hours < 24:
        return f"il y a {int(round(hours))} h"
    days = hours / 24
    return f"il y a {int(round(days))} j"


@app.route("/")
def home():
    display_tz = get_display_tz()
    statuses = rf.get_all_sites_status(inactive_threshold_minutes=5)

    for s in statuses:
        s["time_ago"] = format_time_ago(s.get("minutes_since_last"))
        if s.get("last_seen"):
            try:
                dt_utc = datetime.fromisoformat(s["last_seen"])
                s["last_seen_fmt"] = dt_utc.astimezone(display_tz).strftime("%Y-%m-%d %H:%M %Z")
            except Exception:
                s["last_seen_fmt"] = s["last_seen"]
        else:
            s["last_seen_fmt"] = "—"

    # Regroupement par catégorie: en ligne / hors ligne récent (< 5 j) / hors ligne longue durée
    # À l'intérieur de chaque catégorie, tri par récence (site le plus récemment vu en premier).
    # Un site sans donnée (minutes_since_last=None) est relégué en fin de sa catégorie.
    category_order = {"online": 0, "offline_recent": 1, "offline_long": 2}
    statuses.sort(
        key=lambda s: (
            category_order.get(s["category"], 99),
            s["minutes_since_last"] if s["minutes_since_last"] is not None else float("inf"),
            s["site"],
        )
    )

    online_sites = [s for s in statuses if s["category"] == "online"]
    offline_recent_sites = [s for s in statuses if s["category"] == "offline_recent"]
    offline_long_sites = [s for s in statuses if s["category"] == "offline_long"]

    # Sites géolocalisés pour la carte (ceux sans coordonnées connues sont
    # simplement absents de la carte, sans erreur)
    locations = rf.load_site_locations()
    map_sites = []
    for s in statuses:
        loc = locations.get(s["site"])
        if loc:
            map_sites.append(
                {
                    "site": s["site"],
                    "lat": loc["lat"],
                    "lon": loc["lon"],
                    "category": s["category"],
                    "url": url_for("dashboard", site=s["site"]),
                }
            )

    generated_at = datetime.now(display_tz).strftime("%Y-%m-%d %H:%M:%S %Z")

    return render_template(
        "home.html",
        online_sites=online_sites,
        offline_recent_sites=offline_recent_sites,
        offline_long_sites=offline_long_sites,
        online_count=len(online_sites),
        total_count=len(statuses),
        map_sites=map_sites,
        all_measurements=rf.ALL_MEASUREMENTS,
        measurement_labels=rf.MEASUREMENT_LABELS,
        generated_at=generated_at,
    )


JOURNAL_PERIODS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
JOURNAL_PERIOD_LABELS = {"24h": "24 heures", "7d": "7 jours", "30d": "30 jours"}


def _format_journal_dt(iso_str, display_tz):
    if not iso_str:
        return None
    try:
        dt_utc = datetime.fromisoformat(iso_str)
        return dt_utc.astimezone(display_tz).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_str


def format_duration_minutes(minutes):
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} min"
    hours, rem_min = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {rem_min:02d} min" if rem_min else f"{hours} h"
    days, rem_hours = divmod(hours, 24)
    return f"{days} j {rem_hours} h" if rem_hours else f"{days} j"


@app.route("/journal")
def journal():
    display_tz = get_display_tz()
    period_key = request.args.get("period", "7d")
    if period_key not in JOURNAL_PERIODS:
        period_key = "7d"
    span = JOURNAL_PERIODS[period_key]

    end_dt = datetime.utcnow()
    start_dt = end_dt - span
    start_rfc3339 = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_rfc3339 = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    sites = rf.list_databases()
    site_journals = []

    for site in sites:
        try:
            events = rf.get_disconnection_events(site, start_rfc3339, end_rfc3339, gap_threshold_minutes=5)
            error = None
        except Exception as e:
            events, error = [], f"Erreur InfluxDB: {e}"

        for e in events:
            e["start_fmt"] = _format_journal_dt(e["start"], display_tz)
            e["end_fmt"] = _format_journal_dt(e["end"], display_tz)
            e["duration_fmt"] = format_duration_minutes(e["duration_minutes"])

        total_downtime_minutes = sum(e["duration_minutes"] for e in events)

        site_journals.append(
            {
                "site": site,
                "events": events[:50],
                "total_events": len(events),
                "has_more": len(events) > 50,
                "total_downtime_fmt": format_duration_minutes(total_downtime_minutes) if events else "0 min",
                "error": error,
            }
        )

    generated_at = datetime.now(display_tz).strftime("%Y-%m-%d %H:%M:%S %Z")

    return render_template(
        "journal.html",
        site_journals=site_journals,
        period_key=period_key,
        periods=JOURNAL_PERIODS,
        period_labels=JOURNAL_PERIOD_LABELS,
        generated_at=generated_at,
    )


@app.route("/dashboard")
def dashboard():
    sites = rf.list_databases()
    preselected_site = request.args.get("site", "")
    return render_template(
        "dashboard.html",
        sites=sites,
        measurements=rf.AVAILABLE_MEASUREMENTS,
        measurement_labels=rf.MEASUREMENT_LABELS,
        default_measurement=rf.DEFAULT_MEASUREMENT,
        preselected_site=preselected_site,
    )


@app.route("/quick-check")
def quick_check():
    display_tz = get_display_tz()
    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(hours=24)
    start_rfc3339 = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_rfc3339 = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    interval = rf.pick_group_interval(timedelta(hours=24))

    sites = rf.list_databases()
    error = None
    quick_data = {site: {"traces": [], "freq_count": 0} for site in sites}

    try:
        df = rf.fetch_data(
            start_rfc3339, end_rfc3339, database=None, group_interval=interval, measurement="signal"
        )
    except Exception as e:
        df = None
        error = f"Erreur InfluxDB: {e}"

    if df is not None and not df.empty:
        for site in sites:
            site_df = df[df["site"] == site]
            if site_df.empty:
                continue
            traces, freq_info = build_traces(site_df, "Signal (dBm)", display_tz)
            quick_data[site] = {"traces": traces, "freq_count": len(freq_info)}

    generated_at = datetime.now(display_tz).strftime("%Y-%m-%d %H:%M:%S %Z")

    return render_template(
        "quick_check.html",
        sites=sites,
        quick_data=quick_data,
        error=error,
        generated_at=generated_at,
    )


@app.route("/api/signal")
def api_signal():
    display_tz = get_display_tz()
    start = request.args.get("start")
    end = request.args.get("end")
    site = request.args.get("site") or None
    measurement = request.args.get("measurement") or rf.DEFAULT_MEASUREMENT

    if measurement not in rf.AVAILABLE_MEASUREMENTS:
        return jsonify({"error": f"Mesure invalide: {measurement}"}), 400

    if site and site not in rf.list_databases():
        return jsonify({"error": f"Site inconnu: {site}"}), 400

    if not start or not end:
        return jsonify({"error": "Paramètres 'start' et 'end' requis (ISO 8601)."}), 400

    try:
        # Les champs datetime-local du formulaire sont dans le fuseau
        # d'affichage choisi ; on les interprète comme tel avant de
        # convertir en UTC pour interroger InfluxDB (qui stocke en UTC).
        start_dt = datetime.fromisoformat(start).replace(tzinfo=display_tz)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=display_tz)
    except ValueError:
        return jsonify({"error": "Format de date invalide."}), 400

    if end_dt <= start_dt:
        return jsonify({"error": "La date de fin doit être après la date de début."}), 400

    interval = rf.pick_group_interval(end_dt - start_dt)

    start_rfc3339 = start_dt.astimezone(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_rfc3339 = end_dt.astimezone(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        df = rf.fetch_data(
            start_rfc3339, end_rfc3339, database=site, group_interval=interval, measurement=measurement
        )
    except Exception as e:
        return jsonify({"error": f"Erreur InfluxDB: {e}"}), 500

    measurement_label = rf.MEASUREMENT_LABELS.get(measurement, measurement)

    if df.empty:
        return jsonify({"traces": [], "freq_info": [], "interval": interval, "measurement_label": measurement_label})

    traces, freq_info = build_traces(df, measurement_label, display_tz)
    return jsonify(
        {
            "traces": traces,
            "freq_info": freq_info,
            "interval": interval,
            "measurement_label": measurement_label,
        }
    )


@app.route("/api/temperature")
def api_temperature():
    display_tz = get_display_tz()
    start = request.args.get("start")
    end = request.args.get("end")
    site = request.args.get("site") or None

    if site and site not in rf.list_databases():
        return jsonify({"error": f"Site inconnu: {site}"}), 400

    if not start or not end:
        return jsonify({"error": "Paramètres 'start' et 'end' requis (ISO 8601)."}), 400

    try:
        start_dt = datetime.fromisoformat(start).replace(tzinfo=display_tz)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=display_tz)
    except ValueError:
        return jsonify({"error": "Format de date invalide."}), 400

    if end_dt <= start_dt:
        return jsonify({"error": "La date de fin doit être après la date de début."}), 400

    interval = rf.pick_group_interval(end_dt - start_dt)
    start_rfc3339 = start_dt.astimezone(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_rfc3339 = end_dt.astimezone(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        df = rf.fetch_temperature(start_rfc3339, end_rfc3339, database=site, group_interval=interval)
    except Exception as e:
        return jsonify({"error": f"Erreur InfluxDB: {e}"}), 500

    if df.empty:
        return jsonify({"traces": [], "interval": interval})

    traces = build_temperature_traces(df, display_tz)
    return jsonify({"traces": traces, "interval": interval})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=True)