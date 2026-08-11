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

from datetime import datetime, timedelta
import colorsys

from flask import Flask, jsonify, render_template, request, url_for
import plotly.express as px

import rf_data as rf

app = Flask(__name__)


@app.context_processor
def inject_globals():
    return {"current_year": datetime.utcnow().year}


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


def build_traces(df, measurement_label):
    """Construit les traces Plotly (format JSON) + les infos de la liste de fréquences."""
    df = df.sort_values("time")

    frequencies = sorted(df["frequence_hz"].unique(), key=lambda x: (len(x), x))
    palette = _generate_distinct_colors(len(frequencies))
    color_map = {f: palette[i] for i, f in enumerate(frequencies)}

    traces = []
    freq_info = []

    for freq in frequencies:
        sub = df[df["frequence_hz"] == freq]
        derniere_chaine = sub["chaines"].iloc[-1] if not sub["chaines"].isna().all() else ""
        freq_mhz = int(freq) / 1_000_000 if freq.isdigit() else freq
        label = f"{freq_mhz:.0f} MHz" if isinstance(freq_mhz, float) else freq

        traces.append(
            {
                "x": sub["time"].dt.strftime("%Y-%m-%dT%H:%M:%S").tolist(),
                "y": sub["value"].tolist(),
                "mode": "lines",
                "name": label,
                "line": {"color": color_map[freq], "width": 1.5, "shape": "spline", "smoothing": 0.9},
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


def build_temperature_traces(df):
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
                "x": sub["time"].dt.strftime("%Y-%m-%dT%H:%M:%S").tolist(),
                "y": sub["value"].round(2).tolist(),
                "mode": "lines",
                "name": s,
                "line": {"color": color_map[s], "width": 1.5, "shape": "spline", "smoothing": 0.9},
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
    statuses = rf.get_all_sites_status(inactive_threshold_minutes=5)

    for s in statuses:
        s["time_ago"] = format_time_ago(s.get("minutes_since_last"))
        if s.get("last_seen"):
            try:
                s["last_seen_fmt"] = datetime.fromisoformat(s["last_seen"]).strftime("%Y-%m-%d %H:%M UTC")
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

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

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
            traces, freq_info = build_traces(site_df, "Signal (dBm)")
            quick_data[site] = {"traces": traces, "freq_count": len(freq_info)}

    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    return render_template(
        "quick_check.html",
        sites=sites,
        quick_data=quick_data,
        error=error,
        generated_at=generated_at,
    )


@app.route("/api/signal")
def api_signal():
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
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        return jsonify({"error": "Format de date invalide."}), 400

    if end_dt <= start_dt:
        return jsonify({"error": "La date de fin doit être après la date de début."}), 400

    interval = rf.pick_group_interval(end_dt - start_dt)

    start_rfc3339 = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_rfc3339 = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        df = rf.fetch_data(
            start_rfc3339, end_rfc3339, database=site, group_interval=interval, measurement=measurement
        )
    except Exception as e:
        return jsonify({"error": f"Erreur InfluxDB: {e}"}), 500

    measurement_label = rf.MEASUREMENT_LABELS.get(measurement, measurement)

    if df.empty:
        return jsonify({"traces": [], "freq_info": [], "interval": interval, "measurement_label": measurement_label})

    traces, freq_info = build_traces(df, measurement_label)
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
    start = request.args.get("start")
    end = request.args.get("end")
    site = request.args.get("site") or None

    if site and site not in rf.list_databases():
        return jsonify({"error": f"Site inconnu: {site}"}), 400

    if not start or not end:
        return jsonify({"error": "Paramètres 'start' et 'end' requis (ISO 8601)."}), 400

    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        return jsonify({"error": "Format de date invalide."}), 400

    if end_dt <= start_dt:
        return jsonify({"error": "La date de fin doit être après la date de début."}), 400

    interval = rf.pick_group_interval(end_dt - start_dt)
    start_rfc3339 = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_rfc3339 = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        df = rf.fetch_temperature(start_rfc3339, end_rfc3339, database=site, group_interval=interval)
    except Exception as e:
        return jsonify({"error": f"Erreur InfluxDB: {e}"}), 500

    if df.empty:
        return jsonify({"traces": [], "interval": interval})

    traces = build_temperature_traces(df)
    return jsonify({"traces": traces, "interval": interval})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=True)