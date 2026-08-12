let isolatedIndex = null;
let currentTraces = [];

function pad(n) {
    return String(n).padStart(2, "0");
}

function toLocalInputValue(date) {
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function setDefaultRange(startId, endId) {
    const end = new Date();
    const start = new Date(end.getTime() - 24 * 3600 * 1000);
    document.getElementById(startId).value = toLocalInputValue(start);
    document.getElementById(endId).value = toLocalInputValue(end);
}

function applyPreselectedSite() {
    if (typeof PRESELECTED_SITE === "undefined" || !PRESELECTED_SITE) return;
    const siteSelect = document.getElementById("site-select");
    const tempSiteSelect = document.getElementById("temp-site-select");
    if ([...siteSelect.options].some((o) => o.value === PRESELECTED_SITE)) {
        siteSelect.value = PRESELECTED_SITE;
    }
    if ([...tempSiteSelect.options].some((o) => o.value === PRESELECTED_SITE)) {
        tempSiteSelect.value = PRESELECTED_SITE;
    }
}

function setStatus(msg, isError = false) {
    const el = document.getElementById("status");
    el.textContent = msg;
    el.style.color = isError ? "#ff6b6b" : "#9aa1b1";
}

async function loadData() {
    const start = document.getElementById("start-date").value;
    const end = document.getElementById("end-date").value;
    const site = document.getElementById("site-select").value;
    const measurement = document.getElementById("measurement-select").value;

    if (!start || !end) {
        setStatus("Merci de choisir une date de début et de fin.", true);
        return;
    }

    setStatus("Interrogation d'InfluxDB…");

    const params = new URLSearchParams({ start, end, measurement });
    if (site) params.set("site", site);

    try {
        const resp = await fetch(`/api/signal?${params.toString()}`);
        const data = await resp.json();

        if (data.error) {
            setStatus(data.error, true);
            return;
        }

        currentTraces = data.traces;
        isolatedIndex = null;
        renderPlot(data.traces, data.measurement_label);
        renderFreqList(data.freq_info);
        setStatus(`${data.traces.length} fréquence(s) — granularité: ${data.interval}`);
    } catch (e) {
        setStatus("Erreur réseau: " + e, true);
    }
}

function computeYRange(traces, minSpan = 15, paddingRatio = 0.1) {
    let min = Infinity;
    let max = -Infinity;
    traces.forEach((t) => {
        (t.y || []).forEach((v) => {
            if (v === null || v === undefined || Number.isNaN(v)) return;
            if (v < min) min = v;
            if (v > max) max = v;
        });
    });
    if (!Number.isFinite(min) || !Number.isFinite(max)) return undefined;

    let span = max - min;
    if (span < minSpan) {
        const center = (max + min) / 2;
        min = center - minSpan / 2;
        max = center + minSpan / 2;
        span = minSpan;
    }
    const pad = span * paddingRatio;
    return [min - pad, max + pad];
}

function renderPlot(traces, measurementLabel) {
    const layout = {
        title: `${measurementLabel || "Valeur"} par fréquence (DVB-T/TNT)`,
        xaxis: { title: "Temps (heure de Paris)" },
        yaxis: { title: measurementLabel || "Valeur", range: computeYRange(traces) },
        hovermode: "closest",
        showlegend: false,
        height: 700,
        margin: { r: 40 },
        paper_bgcolor: "#111318",
        plot_bgcolor: "#111318",
        font: { color: "#e8e8e8" },
    };
    Plotly.react("rf-plot", traces, layout, { displaylogo: false });
}

function renderFreqList(freqInfo) {
    const container = document.getElementById("freq-list");
    container.innerHTML = "";
    document.getElementById("freq-count").textContent = freqInfo.length;

    freqInfo.forEach((f, i) => {
        const li = document.createElement("li");
        li.className = "freq-item";
        li.dataset.index = i;
        li.innerHTML = `
            <span class="swatch" style="background:${f.color}"></span>
            <span class="freq-label">${f.label}</span>
            <span class="freq-channels">${f.channels}</span>
            <span class="freq-count">(${f.n_points} pts)</span>
        `;
        li.addEventListener("click", () => toggleFrequency(i));
        container.appendChild(li);
    });
}

function toggleFrequency(idx) {
    const items = document.querySelectorAll(".freq-item");
    const numTraces = currentTraces.length;
    const allIndices = Array.from({ length: numTraces }, (_, i) => i);

    if (isolatedIndex === idx) {
        Plotly.restyle("rf-plot", { visible: allIndices.map(() => true) }, allIndices);
        items.forEach((it) => it.classList.remove("active"));
        isolatedIndex = null;
    } else {
        Plotly.restyle("rf-plot", { visible: allIndices.map((i) => i === idx) }, allIndices);
        items.forEach((it, i) => it.classList.toggle("active", i === idx));
        isolatedIndex = idx;
    }
}

// ============================================================
// Graphique indépendant : température (seul paramètre = site)
// ============================================================
function setTempStatus(msg, isError = false) {
    const el = document.getElementById("temp-status");
    if (!el) return;
    el.textContent = msg;
    el.style.color = isError ? "#ff6b6b" : "#9aa1b1";
}

async function loadTemperatureData() {
    const start = document.getElementById("temp-start-date").value;
    const end = document.getElementById("temp-end-date").value;
    const site = document.getElementById("temp-site-select").value;

    if (!start || !end) return;

    setTempStatus("Interrogation d'InfluxDB…");

    const params = new URLSearchParams({ start, end });
    if (site) params.set("site", site);

    try {
        const resp = await fetch(`/api/temperature?${params.toString()}`);
        const data = await resp.json();

        if (data.error) {
            setTempStatus(data.error, true);
            return;
        }

        renderTemperaturePlot(data.traces);
        setTempStatus(`${data.traces.length} site(s) — granularité: ${data.interval}`);
    } catch (e) {
        setTempStatus("Erreur réseau: " + e, true);
    }
}

function renderTemperaturePlot(traces) {
    const layout = {
        title: "Température par site",
        xaxis: { title: "Temps (heure de Paris)" },
        yaxis: { title: "Température (°C)" },
        hovermode: "closest",
        showlegend: true,
        legend: { orientation: "h", y: 1.15 },
        height: 400,
        margin: { r: 40 },
        paper_bgcolor: "#111318",
        plot_bgcolor: "#111318",
        font: { color: "#e8e8e8" },
    };
    Plotly.react("temp-plot", traces, layout, { displaylogo: false });
}

document.getElementById("apply-btn").addEventListener("click", loadData);
document.getElementById("measurement-select").addEventListener("change", loadData);
document.getElementById("site-select").addEventListener("change", loadData);
document.getElementById("reset-btn").addEventListener("click", () => {
    setDefaultRange("start-date", "end-date");
    loadData();
});

document.getElementById("temp-apply-btn").addEventListener("click", loadTemperatureData);
document.getElementById("temp-site-select").addEventListener("change", loadTemperatureData);
document.getElementById("temp-reset-btn").addEventListener("click", () => {
    setDefaultRange("temp-start-date", "temp-end-date");
    loadTemperatureData();
});

window.addEventListener("DOMContentLoaded", () => {
    setDefaultRange("start-date", "end-date");
    setDefaultRange("temp-start-date", "temp-end-date");
    applyPreselectedSite();
    loadData();
    loadTemperatureData();
});