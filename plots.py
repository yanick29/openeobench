#!/usr/bin/env python3
"""
plots.py - Abbildungen der Bachelorarbeit aus benchmark_results.duckdb.

Aufruf:
    python3 plots.py            # alle Abbildungen
    python3 plots.py genauigkeit

Ausgabe als PDF nach abbildungen/, Vektorformat fuer \\includegraphics.
"""
import sys
from pathlib import Path

import duckdb
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

DB = Path("benchmark_results.duckdb")
ZIEL = Path("abbildungen")
# Gliederung nach Forschungsfrage, entspricht der Kapitelstruktur der Arbeit.
ORDNER = {
    "ff1": ZIEL / "ff1_laufzeit",
    "ff2": ZIEL / "ff2_genauigkeit",
    "ff3": ZIEL / "ff3_stabilitaet",
    "anhang": ZIEL / "anhang",
}
for _o in ORDNER.values():
    _o.mkdir(parents=True, exist_ok=True)


ZEITANTEILE = [("bezug", "Bezug der Eingangsdaten", "#7f3b08"),
               ("preprocessing_time", "lokale Vorbereitung", "#e08214"),
               ("annahme", "Jobannahme", "#d9d9d9"),
               ("queue_time", "Warteschlange", "#b2b2b2"),
               ("processing_time", "Rechenzeit Backend", "#1f4e79"),
               ("download_time", "Ergebnisdownload", "#7fb3d5"),
               ("sonstiges", "Sonstiges", "#efefef")]

def ziel(bereich: str, dateiname: str) -> Path:
    """Ablagepfad einer Abbildung. bereich ist ff1, ff2, ff3 oder anhang."""
    return ORDNER[bereich] / dateiname

plt.rcParams.update({
    "font.family": "sans-serif", "font.size": 9,
    "axes.labelsize": 9, "axes.titlesize": 9,
    "xtick.labelsize": 8, "ytick.labelsize": 8, "legend.fontsize": 8,
    "figure.dpi": 150, "savefig.bbox": "tight", "savefig.pad_inches": 0.02,
})

STIL = {
    "local_preprocessing": dict(color="#1f4e79", marker="o", label="local_pp"),
    "onthefly":            dict(color="#e08214", marker="s", label="onthefly"),
}
# Erst die punktweisen Operationen, dann die nachbarschafts- und zeitbezogenen.
OPERATIONEN = ["merge_add", "subtract", "mask", "filter_bbox",
               "focal", "resample", "aggregation"]


def hole(sql: str):
    con = duckdb.connect(str(DB), read_only=True)
    try:
        return con.execute(sql).fetchdf()
    finally:
        con.close()


def genauigkeit_operationen():
    """Genauigkeit je Operation, Berlin und Hamburg nebeneinander.

    Punktdarstellung statt Balken: bei drei Groessenordnungen Abstand ist die
    Balkenlaenge auf logarithmischer Achse nicht mehr interpretierbar, der
    vertikale Abstand zweier Punkte dagegen schon.
    """
    df = hole("""
      SELECT CASE WHEN r.scenario LIKE '%hamburg%' OR r.job_id IN (
                    SELECT job_id FROM runs WHERE scenario LIKE '%hamburg%')
                  THEN 'Hamburg' ELSE 'Berlin' END AS region_hilf,
             r.run_id, r.scenario, r.crs_strategy, r.workflow,
             r.timestamp, a.mae, a.rmse
      FROM runs r JOIN accuracy a ON a.run_id = r.run_id
      WHERE r.extent_size = 'medium' AND r.archived = false
        AND r.status = 'success' AND r.dataset = 'dem'
        AND r.resolution_m = 10 AND r.crs_strategy <> 'local_reference'
        AND r.workflow IN ('merge_add','subtract','mask','filter_bbox',
                           'focal','resample','aggregation')
      ORDER BY r.run_id
    """)

    # Die Region steht bei onthefly nicht im scenario-Namen. Zuordnung über
    # die Blockzuordnung aus den Protokolldateien.
    import csv
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = (z["block"], z["region"])
    df["block"] = df.run_id.map(lambda i: zu.get(i, ("", ""))[0])
    df["region"] = df.run_id.map(lambda i: zu.get(i, ("", ""))[1])
    df = df[df.block.isin(["A1", "A2"])]

    if df.empty:
        print("keine Daten fuer A1/A2, blockzuordnung.csv vorhanden?")
        return

    # Genauigkeitswerte sind je Konfiguration über alle Wiederholungen
    # identisch, ein Wert je Konfiguration genuegt.
    df = df.drop_duplicates(["block", "workflow", "crs_strategy"])

    for spalte, achse, name in (("mae", "MAE", "mae"), ("rmse", "RMSE", "rmse")):
        fig, achsen = plt.subplots(1, 2, figsize=(6.3, 3.0), sharey=True)
        x = np.arange(len(OPERATIONEN))

        for ax, (block, titel) in zip(achsen, [("A1", "Berlin"), ("A2", "Hamburg")]):
            teil = df[df.block == block]
            reihen = {}
            for strategie in ("local_preprocessing", "onthefly"):
                werte = []
                for op in OPERATIONEN:
                    t = teil[(teil.workflow == op) & (teil.crs_strategy == strategie)][spalte]
                    werte.append(float(t.iloc[0]) if len(t) else np.nan)
                reihen[strategie] = werte

            # Verbindungslinie macht den Faktor zwischen den Strategien sichtbar
            for i in range(len(OPERATIONEN)):
                u, o = reihen["local_preprocessing"][i], reihen["onthefly"][i]
                if np.isfinite(u) and np.isfinite(o):
                    ax.plot([i, i], [u, o], color="#c0c0c0", linewidth=0.9, zorder=1)

            for strategie, werte in reihen.items():
                ax.plot(x, werte, linestyle="none", markersize=5,
                        markeredgecolor="white", markeredgewidth=0.5,
                        zorder=3, **STIL[strategie])

            ax.set_yscale("log")
            alle = [w for r in reihen.values() for w in r if np.isfinite(w)]
            if alle:
                ax.set_ylim(min(alle) / 3, max(alle) * 3)
            ax.set_xticks(x)
            ax.set_xticklabels(OPERATIONEN, rotation=45, ha="right")
            ax.set_xlim(-0.6, len(OPERATIONEN) - 0.4)
            ax.set_title(titel)
            ax.grid(axis="y", which="major", linewidth=0.4, color="#d5d5d5")
            ax.set_axisbelow(True)
            for rand in ("top", "right"):
                ax.spines[rand].set_visible(False)

        achsen[0].set_ylabel(achse)
        griffe, namen = achsen[0].get_legend_handles_labels()
        fig.legend(griffe, namen, loc="lower center", ncol=2, frameon=False,
                   bbox_to_anchor=(0.5, -0.02))
        fig.tight_layout(rect=(0, 0.06, 1, 1))
        datei = ziel("ff2", f"a1a2_genauigkeit_operationen_{name}.pdf")
        fig.savefig(datei, format="pdf")
        plt.close(fig)
        print("geschrieben:", datei)



def genauigkeitsfaktor():
    """Faktor zwischen den Strategien je Operation, gruppierte Balken.

    Dargestellt wird onthefly geteilt durch local_preprocessing. Die
    logarithmische Ordinate ist hier unbedenklich, weil die Basislinie eines
    Verhaeltnisses bei eins liegt und log(1)=0 ergibt. Die Balken wachsen also
    von einer echten Nulllinie aus, anders als bei einer logarithmischen
    Darstellung der Absolutwerte.
    """
    import csv
    df = hole("""
      SELECT r.run_id, r.crs_strategy, r.workflow, a.mae, a.rmse
      FROM runs r JOIN accuracy a ON a.run_id = r.run_id
      WHERE r.extent_size = 'medium' AND r.archived = false
        AND r.status = 'success' AND r.dataset = 'dem'
        AND r.resolution_m = 10 AND r.crs_strategy <> 'local_reference'
      ORDER BY r.run_id
    """)
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = z["block"]
    df["block"] = df.run_id.map(lambda i: zu.get(i, ""))
    df = df[df.block.isin(["A1", "A2"])].drop_duplicates(
        ["block", "workflow", "crs_strategy"])
    if df.empty:
        print("keine Daten fuer A1/A2")
        return

    fig, ax = plt.subplots(figsize=(6.3, 3.0))
    x = np.arange(len(OPERATIONEN))
    breite = 0.38
    grau = {"A1": "#1f4e79", "A2": "#e08214"}
    titel = {"A1": "Berlin", "A2": "Hamburg"}

    for k, block in enumerate(["A1", "A2"]):
        teil = df[df.block == block]
        faktoren = []
        for op in OPERATIONEN:
            u = teil[(teil.workflow == op) & (teil.crs_strategy == "local_preprocessing")].mae
            o = teil[(teil.workflow == op) & (teil.crs_strategy == "onthefly")].mae
            faktoren.append(float(o.iloc[0]) / float(u.iloc[0])
                            if len(u) and len(o) and float(u.iloc[0]) > 0 else np.nan)
        balken = ax.bar(x + (k - 0.5) * breite, faktoren, breite, bottom=0,
                        color=grau[block], label=titel[block],
                        edgecolor="white", linewidth=0.4)
        for rechteck, wert in zip(balken, faktoren):
            if np.isfinite(wert):
                ax.annotate(f"{wert:.0f}" if wert >= 10
                            else f"{wert:.1f}".replace(".", ","),
                            (rechteck.get_x() + rechteck.get_width() / 2, wert),
                            textcoords="offset points", xytext=(0, 2),
                            ha="center", fontsize=6.5, color="#444444")

    ax.set_yscale("log")
    ax.set_ylim(1, 4000)
    ax.axhline(1, color="#444444", linewidth=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(OPERATIONEN, rotation=45, ha="right")
    ax.set_xlim(-0.6, len(OPERATIONEN) - 0.4)
    ax.set_ylabel("Faktor MAE onthefly zu local_pp")
    ax.grid(axis="y", which="major", linewidth=0.4, color="#d5d5d5")
    ax.set_axisbelow(True)
    for rand in ("top", "right"):
        ax.spines[rand].set_visible(False)
    ax.legend(frameon=False, loc="upper right")

    fig.tight_layout()
    datei = ziel("ff2", "a1a2_genauigkeitsfaktor_operationen.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)



def genauigkeit_regionen():
    """MAE von merge_add über neun Regionen, beide Strategien.

    Der Regionsvergleich variiert das Ziel-CRS. Die Regionen liegen in acht
    UTM-Zonen und auf beiden Hemisphaeren, sortiert nach dem Verhaeltnis
    beider Strategien. Das Verhaeltnis selbst steht im Text, die Abbildung
    zeigt die Absolutwerte, aus denen es sich ergibt.
    """
    import csv
    df = hole("""
      SELECT r.run_id, r.crs_strategy, a.mae, a.rmse
      FROM runs r JOIN accuracy a ON a.run_id = r.run_id
      WHERE r.extent_size='medium' AND r.archived=false AND r.status='success'
        AND r.dataset='dem' AND r.resolution_m=10 AND r.workflow='merge_add'
        AND r.crs_strategy <> 'local_reference'
        AND r.backend_url NOT LIKE '%terrascope%'
      ORDER BY r.run_id
    """)
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = (z["block"], z["region"])
    df["block"] = df.run_id.map(lambda i: zu.get(i, ("", ""))[0])
    df["region"] = df.run_id.map(lambda i: zu.get(i, ("", ""))[1])
    df = df[df.block.isin(["A1", "A2", "B"])]
    df = df.drop_duplicates(["region", "crs_strategy"])
    if df.empty:
        print("keine Daten"); return

    ZONE = {"newyork": 18, "amsterdam": 31, "hamburg": 32, "zuerich": 32,
            "berlin": 33, "rom": 33, "wien": 33, "kapstadt": 34, "tokio": 54}
    NAME = {"amsterdam": "Amsterdam", "berlin": "Berlin", "hamburg": "Hamburg",
            "kapstadt": "Kapstadt", "newyork": "New York", "rom": "Rom",
            "tokio": "Tokio", "wien": "Wien", "zuerich": "Z\u00fcrich"}

    def faktor(r):
        u = df[(df.region == r) & (df.crs_strategy == "local_preprocessing")].mae
        o = df[(df.region == r) & (df.crs_strategy == "onthefly")].mae
        return float(o.iloc[0]) / float(u.iloc[0]) if len(u) and len(o) else 0

    regionen = sorted(df.region.unique(), key=faktor)
    x = np.arange(len(regionen))

    fig, achsen = plt.subplots(2, 1, figsize=(6.3, 4.8), sharex=True)

    for ax, spalte, name in ((achsen[0], "mae", "MAE"),
                             (achsen[1], "rmse", "RMSE")):
        reihen = {}
        for strategie in ("local_preprocessing", "onthefly"):
            werte = []
            for r in regionen:
                tr = df[(df.region == r) & (df.crs_strategy == strategie)][spalte]
                werte.append(float(tr.iloc[0]) if len(tr) else np.nan)
            reihen[strategie] = werte
        for i in range(len(regionen)):
            u, o = reihen["local_preprocessing"][i], reihen["onthefly"][i]
            if np.isfinite(u) and np.isfinite(o):
                ax.plot([i, i], [u, o], color="#c0c0c0", linewidth=0.9, zorder=1)
        for strategie, werte in reihen.items():
            ax.plot(x, werte, linestyle="none", markersize=5,
                    markeredgecolor="white", markeredgewidth=0.5,
                    zorder=3, **STIL[strategie])
        ax.set_yscale("log")
        alle = [w for r in reihen.values() for w in r if np.isfinite(w)]
        ax.set_ylim(min(alle) / 3, max(alle) * 3)
        ax.set_ylabel(name)
        ax.set_xlim(-0.6, len(regionen) - 0.4)
        ax.grid(axis="y", linewidth=0.4, color="#d5d5d5")
        ax.set_axisbelow(True)
        for rand in ("top", "right"):
            ax.spines[rand].set_visible(False)

    achsen[1].set_xticks(x)
    achsen[1].set_xticklabels([f"{NAME.get(r, r)}\nZone {ZONE.get(r, '?')}"
                               for r in regionen], rotation=45, ha="right")

    ax = achsen[0]
    griffe, namen = ax.get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=2, frameon=False,
               bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    datei = ziel("ff2", "b_genauigkeit_regionen.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)
    for r in regionen:
        print(f"{NAME.get(r, r):10s} Zone {ZONE.get(r, '?'):2}  Faktor {faktor(r):7.0f}")


def _zeitdaten():
    """Läufe aus A1, A2 und B mit je Lauf gebildeten Zeitanteilen.

    Die Anteile werden je Lauf berechnet und erst danach gemittelt.
    Spaltenweise Kennzahlen stammen aus verschiedenen Läufen und ergaeben in
    der Summe keinen realen Lauf.
    """
    import csv
    df = hole("""
      SELECT r.run_id, r.crs_strategy, r.workflow,
             r.queue_time, r.processing_time, r.download_time,
             r.preprocessing_time, r.dem_download_time,
             r.job_execution_time, r.total_time
      FROM runs r
      WHERE r.extent_size='medium' AND r.archived=false AND r.status='success'
        AND r.dataset='dem' AND r.resolution_m=10
        AND r.crs_strategy <> 'local_reference'
      ORDER BY r.run_id
    """).fillna(0)
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = z["block"]
    df["block"] = df.run_id.map(lambda i: zu.get(i, ""))
    df = df[df.block.isin(["A1", "A2", "B"])].copy()
    # Jobannahme: Übergabe bis Eintritt in die Warteschlange.
    df["annahme"] = (df.job_execution_time - df.queue_time
                     - df.processing_time).clip(lower=0)
    # Rest bis zur Gesamtzeit: Upload, STAC-Erzeugung, Abruf der Jobdetails.
    df["sonstiges"] = (df.total_time - df.job_execution_time
                       - df.download_time - df.preprocessing_time).clip(lower=0)
    return df


def _zeitbalken(ax, m, anzahl, zeilen, beschriftung):
    y = np.arange(len(zeilen))
    links = np.zeros(len(zeilen))
    for feld, name, farbe in ZEITANTEILE:
        werte = np.array([float(m.loc[z, feld]) if z in m.index else 0.0
                          for z in zeilen])
        dunkel = farbe in ("#7f3b08", "#1f4e79")
        ax.barh(y, werte, 0.55, left=links, color=farbe, label=name,
                edgecolor="white", linewidth=0.6)
        for i, w in enumerate(werte):
            if w > 18:
                ax.annotate(f"{w:.0f}", (links[i] + w / 2, y[i]),
                            ha="center", va="center", fontsize=6.5,
                            color="white" if dunkel else "#333333")
        links += werte
    for i, z in enumerate(zeilen):
        ax.annotate(f"{links[i]:.0f} s", (links[i], y[i]),
                    textcoords="offset points", xytext=(5, 0),
                    va="center", fontsize=7.5, color="#333333")
    ax.set_yticks(y)
    ax.set_yticklabels(beschriftung)
    ax.invert_yaxis()
    ax.set_xlim(0, links.max() * 1.16)
    ax.grid(axis="x", linewidth=0.4, color="#d5d5d5")
    ax.set_axisbelow(True)
    for rand in ("top", "right", "left"):
        ax.spines[rand].set_visible(False)
    return links


def zeitanteile():
    """Zusammensetzung der Laufzeit bei merge_add, Haupttext.

    Beschraenkt auf eine Operation, weil die Rechenzeit zwischen den
    Operationen um Faktor 1,8 streut und ein Mischwert keine benennbare
    Konfiguration beschreibt.
    """
    df = _zeitdaten()
    df = df[df.workflow == "merge_add"]
    if df.empty:
        print("keine Daten")
        return
    strategien = ["local_preprocessing", "onthefly"]
    m = df.groupby("crs_strategy").mean(numeric_only=True)
    anzahl = df.groupby("crs_strategy").size()

    fig, ax = plt.subplots(figsize=(6.3, 2.6))
    _zeitbalken(ax, m, anzahl, strategien,
                [f"{s}\n(n={anzahl.get(s, 0)})" for s in strategien])
    ax.set_xlabel("Zeit in Sekunden, Mittel über die Läufe, "
                  "merge_add bei Ausdehnung medium")
    griffe, namen = ax.get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=4, frameon=False,
               bbox_to_anchor=(0.5, -0.20))
    fig.tight_layout(rect=(0, 0.16, 1, 1))
    datei = ziel("ff1", "a1a2b_zeitanteile_strategien.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)

    felder = [f for f, _, _ in ZEITANTEILE]
    k = m[felder].round(1)
    k["summe"] = k.sum(axis=1).round(1)
    k["soll"] = (m.total_time + m.dem_download_time).round(1)
    print(k.to_string())


def zeitanteile_operationen():
    """Dieselbe Aufschluesselung je Operation, fuer den Anhang.

    Zwei Panels, links local_preprocessing, rechts onthefly, in identischer
    Operationsreihenfolge und auf gemeinsamer Skala. Der Modus steht damit
    einmal als Panel-Titel statt in jeder Zeilenbeschriftung, der
    Stichprobenumfang als Fussnote.
    """
    df = _zeitdaten()
    strategien = [("local_preprocessing", "local_pp"),
                  ("onthefly", "onthefly")]
    df["zeile"] = df.workflow + " | " + df.crs_strategy
    m = df.groupby("zeile").mean(numeric_only=True)
    anzahl = df.groupby("zeile").size()
    vorhanden = set(df.zeile)

    # nur Operationen, die in beiden Panels vorliegen; sonst waere die
    # Zeilenreihenfolge zwischen links und rechts nicht deckungsgleich
    ops = [o for o in OPERATIONEN
           if all(f"{o} | {s}" in vorhanden for s, _ in strategien)]
    if not ops:
        print("keine Daten fuer die Operationen")
        return

    fig, achsen = plt.subplots(1, 2, sharey=True,
                              figsize=(6.9, 0.30 * len(ops) + 1.9),
                              gridspec_kw={"wspace": 0.05})

    enden = []
    for ax, (schluessel, titel) in zip(achsen, strategien):
        zeilen = [f"{o} | {schluessel}" for o in ops]
        enden.append(_zeitbalken(ax, m, anzahl, zeilen, ops))
        ax.set_title(titel, fontsize=8.5, pad=4)
        ax.tick_params(axis="both", labelsize=7.5)

    # gemeinsame x-Grenze, sonst sind die Panels optisch nicht vergleichbar
    grenze = max(e.max() for e in enden) * 1.16
    for ax in achsen:
        ax.set_xlim(0, grenze)
    achsen[1].tick_params(left=False)

    # Stichprobenumfang als Fussnote statt in jeder Zeilenbeschriftung
    n_je_op = {o: int(anzahl.get(f"{o} | {strategien[0][0]}", 0)) for o in ops}
    haeufig = max(set(n_je_op.values()), key=list(n_je_op.values()).count)
    abweichend = [f"{o} n={v}" for o, v in n_je_op.items() if v != haeufig]
    hinweis = f"n={haeufig} je Operation und Strategie"
    if abweichend:
        hinweis += ", abweichend: " + ", ".join(abweichend)

    # feste Raender statt tight_layout: supxlabel und Legende ausserhalb der
    # Achsen vertragen sich nicht mit tight_layout
    hoehe = fig.get_figheight()
    fig.subplots_adjust(left=0.155, right=0.985, top=1 - 0.32 / hoehe,
                        bottom=1.02 / hoehe)
    fig.text(0.57, 0.72 / hoehe, "Zeit in Sekunden, Mittel über die Läufe, "
             "Ausdehnung medium", ha="center", fontsize=8.5)
    fig.text(0.57, 0.06 / hoehe, hinweis, ha="center", fontsize=6.5,
             color="#555555")

    griffe, namen = achsen[0].get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=4, frameon=False,
               fontsize=7.5, bbox_to_anchor=(0.57, 0.16 / hoehe),
               handlelength=1.4, columnspacing=1.2)

    datei = ziel("ff1", "a1a2b_zeitanteile_operationen.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)



def laufzeitstreuung():
    """Einzelne Laufzeiten je Operation und Strategie.

    Gestapelte Balken zeigen Mittelwerte und verbergen, wie stark die Laufzeit
    schwankt. Hier steht jeder Lauf als eigener Punkt, dazu ein Strich fuer den
    Median. Bei sechs Läufen je Operation waere ein Boxplot irrefuehrend, weil
    er eine Verteilung suggeriert, die aus so wenigen Werten nicht ablesbar ist.
    """
    import csv
    df = hole("""
      SELECT r.run_id, r.crs_strategy, r.workflow, r.processing_time
      FROM runs r
      WHERE r.extent_size='medium' AND r.archived=false AND r.status='success'
        AND r.dataset='dem' AND r.resolution_m=10
        AND r.crs_strategy <> 'local_reference'
      ORDER BY r.run_id
    """)
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = z["block"]
    df["block"] = df.run_id.map(lambda i: zu.get(i, ""))
    df = df[df.block.isin(["A1", "A2", "B"])]
    if df.empty:
        print("keine Daten fuer die Streuung")
        return

    fig, ax = plt.subplots(figsize=(6.3, 3.2))
    x = np.arange(len(OPERATIONEN))
    versatz = {"local_preprocessing": -0.18, "onthefly": 0.18}
    rng = np.random.default_rng(0)

    for strategie, dx in versatz.items():
        farbe = STIL[strategie]["color"]
        for i, op in enumerate(OPERATIONEN):
            werte = df[(df.workflow == op)
                       & (df.crs_strategy == strategie)].processing_time.values
            if not len(werte):
                continue
            # Box fuer die Streuung, Einzelpunkte darüber. Bei sechs Läufen je
            # Operation sagen Quartile allein wenig aus, deshalb bleiben die
            # Rohwerte sichtbar. Die Verteilung ist ausserdem zweigipflig, das
            # verdeckt eine Box sonst.
            ax.boxplot([werte], positions=[i + dx], widths=0.26,
                       showfliers=False, patch_artist=True,
                       medianprops=dict(color=farbe, linewidth=1.6),
                       boxprops=dict(facecolor="white", edgecolor=farbe,
                                     linewidth=0.9),
                       whiskerprops=dict(color=farbe, linewidth=0.9),
                       capprops=dict(color=farbe, linewidth=0.9), zorder=2)
            jitter = rng.uniform(-0.06, 0.06, size=len(werte))
            ax.plot(np.full(len(werte), i + dx) + jitter, werte,
                    linestyle="none", marker=STIL[strategie]["marker"],
                    color=farbe, markersize=3.2, alpha=0.6,
                    markeredgewidth=0, zorder=3,
                    label=STIL[strategie]["label"] if i == 0 else None)

    ax.set_xticks(x)
    ax.set_xticklabels(OPERATIONEN, rotation=45, ha="right")
    ax.set_xlim(-0.6, len(OPERATIONEN) - 0.4)
    # Nullpunkt erzwingen: bei abgeschnittener Achse erscheint der Abstand
    # zwischen 62 und 126 Sekunden größer, als er ist.
    ax.set_ylim(0, df.processing_time.max() * 1.08)
    ax.set_ylabel("Rechenzeit im Backend in Sekunden")
    ax.grid(axis="y", linewidth=0.4, color="#d5d5d5")
    ax.set_axisbelow(True)
    for rand in ("top", "right"):
        ax.spines[rand].set_visible(False)

    griffe, namen = ax.get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=2, frameon=False,
               bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    datei = ziel("ff1", "a1a2b_laufzeitstreuung_operationen.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)

    z = df.groupby(["workflow", "crs_strategy"]).processing_time.agg(
        ["count", "median", "min", "max", "std"]).round(1)
    print(z.to_string())



def datenzugriff_operationen():
    """Datenzugriff und Rechenzeit je Operation, nur local_preprocessing.

    Alle Operationen laden dasselbe vorbereitete DEM-Asset von 4,80 MB. Der
    Datenzugriff wird über das Zugriffsprotokoll des Hosting-Servers gemessen
    und ist deshalb unabhaengig vom Backend beobachtbar.

    Der Vergleich beantwortet, ob CDSE den Zuschnitt von filter_bbox in den
    Datenzugriff hineinzieht oder erst auf dem vollstaendig gelesenen Wuerfel
    anwendet. filter_bbox schneidet auf die mittleren 50 Prozent der Kanten zu,
    also auf ein Viertel der Flaeche.
    """
    import csv
    df = hole("""
      SELECT r.run_id, r.workflow, r.asset_bytes, r.processing_time,
             (SELECT count(*) FROM nginx_access_log n WHERE n.run_id=r.run_id) AS anfragen,
             (SELECT sum(bytes_sent) FROM nginx_access_log n WHERE n.run_id=r.run_id) AS bytes
      FROM runs r
      WHERE r.extent_size='medium' AND r.archived=false AND r.status='success'
        AND r.dataset='dem' AND r.resolution_m=10
        AND r.crs_strategy='local_preprocessing'
      ORDER BY r.run_id
    """)
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = z["block"]
    df["block"] = df.run_id.map(lambda i: zu.get(i, ""))
    df = df[df.block.isin(["A1", "A2"])]
    if df.empty:
        print("keine Daten fuer den Datenzugriff")
        return

    m = df.groupby("workflow").agg(
        bytes=("bytes", "mean"), anfragen=("anfragen", "mean"),
        asset=("asset_bytes", "mean"), n=("run_id", "count"))
    zeit = df.groupby("workflow").processing_time

    x = np.arange(len(OPERATIONEN))
    fig, (links, rechts) = plt.subplots(1, 2, figsize=(6.3, 3.0))

    # Links: übertragenes Volumen, Bezugslinie ist die Dateigröße
    mb = [float(m.loc[o, "bytes"]) / 1e6 if o in m.index else np.nan
          for o in OPERATIONEN]
    farben = ["#e08214" if o == "filter_bbox" else "#1f4e79" for o in OPERATIONEN]
    links.bar(x, mb, 0.62, color=farben, edgecolor="white", linewidth=0.5)
    asset_mb = float(m.asset.iloc[0]) / 1e6
    links.axhline(asset_mb, color="#666666", linewidth=0.9, linestyle=(0, (4, 3)))
    links.annotate(f"Dateigröße {asset_mb:.2f} MB", (len(OPERATIONEN) - 0.5, asset_mb),
                   textcoords="offset points", xytext=(0, 3), ha="right",
                   fontsize=7, color="#555555")
    for i, w in enumerate(mb):
        if np.isfinite(w):
            links.annotate(f"{w:.2f}", (i, w), textcoords="offset points",
                           xytext=(0, 2), ha="center", fontsize=6.5,
                           color="#444444")
    links.set_ylabel("Übertragenes Volumen in MB")
    links.set_ylim(0, asset_mb * 1.25)

    # Rechts: Rechenzeit, Einzelwerte plus Median
    rng = np.random.default_rng(0)
    for i, o in enumerate(OPERATIONEN):
        werte = zeit.get_group(o).values if o in zeit.groups else np.array([])
        if not len(werte):
            continue
        farbe = "#e08214" if o == "filter_bbox" else "#1f4e79"
        rechts.plot(np.full(len(werte), i) + rng.uniform(-0.13, 0.13, len(werte)),
                    werte, linestyle="none", marker="o", color=farbe,
                    markersize=3.4, alpha=0.6, markeredgewidth=0, zorder=2)
        med = float(np.median(werte))
        rechts.plot([i - 0.26, i + 0.26], [med, med], color=farbe,
                    linewidth=1.8, zorder=3)
    rechts.set_ylabel("Rechenzeit im Backend in Sekunden")
    rechts.set_ylim(0, None)

    for ax, titel in ((links, "Datenzugriff"), (rechts, "Rechenzeit")):
        ax.set_title(titel)
        ax.set_xticks(x)
        ax.set_xticklabels(OPERATIONEN, rotation=45, ha="right")
        ax.set_xlim(-0.6, len(OPERATIONEN) - 0.4)
        ax.grid(axis="y", linewidth=0.4, color="#d5d5d5")
        ax.set_axisbelow(True)
        for rand in ("top", "right"):
            ax.spines[rand].set_visible(False)

    fig.tight_layout()
    datei = ziel("ff1", "a1a2_datenzugriff_operationen.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)

    k = m.copy()
    k["mb"] = (k.bytes / 1e6).round(2)
    k["anteil"] = (k.bytes / k.asset).round(3)
    k["zeit_median"] = zeit.median().round(1)
    print(k[["n", "anfragen", "mb", "anteil", "zeit_median"]].round(1).to_string())



def fehlerstruktur_regionen():
    """MAE gegen RMSE je Region, beide Strategien.

    Das Verhaeltnis von RMSE zu MAE beschreibt die Struktur des Fehlers. Liegen
    beide Masse dicht beieinander, ist die Abweichung gleichmaessig über die
    Flaeche verteilt. Ein weit hoeherer RMSE bedeutet, dass die Abweichung fast
    überall klein ist und an wenigen Stellen sehr gross wird, weil der RMSE
    quadriert und grosse Einzelwerte deshalb staerker gewichtet.

    Die gestrichelten Linien markieren feste Verhaeltnisse. Auf der untersten
    liegen Punkte mit RMSE gleich MAE.
    """
    import csv
    df = hole("""
      SELECT r.run_id, r.crs_strategy, a.mae, a.rmse
      FROM runs r JOIN accuracy a ON a.run_id = r.run_id
      WHERE r.extent_size='medium' AND r.archived=false AND r.status='success'
        AND r.dataset='dem' AND r.resolution_m=10 AND r.workflow='merge_add'
        AND r.crs_strategy <> 'local_reference'
      ORDER BY r.run_id
    """)
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = (z["block"], z["region"])
    df["block"] = df.run_id.map(lambda i: zu.get(i, ("", ""))[0])
    df["region"] = df.run_id.map(lambda i: zu.get(i, ("", ""))[1])
    df = df[df.block.isin(["A1", "A2", "B"])].drop_duplicates(
        ["region", "crs_strategy"])
    if df.empty:
        print("keine Daten"); return

    NAME = {"amsterdam": "Amsterdam", "berlin": "Berlin", "hamburg": "Hamburg",
            "kapstadt": "Kapstadt", "newyork": "New York", "rom": "Rom",
            "tokio": "Tokio", "wien": "Wien", "zuerich": "Z\u00fcrich"}

    fig, ax = plt.subplots(figsize=(6.3, 3.6))

    # Hilfslinien fuer feste Verhaeltnisse RMSE zu MAE
    grenzen = [1e-4, 1e3]
    for faktor, beschriftung in ((1, "RMSE = MAE"), (2, "Faktor 2"),
                                 (10, "Faktor 10"), (100, "Faktor 100")):
        ax.plot(grenzen, [g * faktor for g in grenzen], color="#cfcfcf",
                linewidth=0.7, linestyle=(0, (4, 3)), zorder=1)
        ax.annotate(beschriftung, (grenzen[1], grenzen[1] * faktor),
                    textcoords="offset points", xytext=(-4, 2), ha="right",
                    fontsize=6.5, color="#999999")

    for strategie in ("local_preprocessing", "onthefly"):
        teil = df[df.crs_strategy == strategie]
        ax.plot(teil.mae, teil.rmse, linestyle="none", markersize=5.5,
                markeredgecolor="white", markeredgewidth=0.5, zorder=3,
                **STIL[strategie])
        for _, z in teil.iterrows():
            versatz = (6, -8) if z.region == "rom" else (5, 3)
            ax.annotate(NAME.get(z.region, z.region), (z.mae, z.rmse),
                        textcoords="offset points", xytext=versatz,
                        fontsize=6.5, color="#555555")

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(3e-4, 6)
    ax.set_ylim(5e-4, 3e2)
    ax.set_xlabel("MAE")
    ax.set_ylabel("RMSE")
    ax.grid(linewidth=0.4, color="#e6e6e6")
    ax.set_axisbelow(True)
    for rand in ("top", "right"):
        ax.spines[rand].set_visible(False)

    griffe, namen = ax.get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=2, frameon=False,
               bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    datei = ziel("ff2", "b_fehlerstruktur_regionen.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)

    df["verhaeltnis"] = (df.rmse / df.mae).round(1)
    print(df.pivot_table(index="region", columns="crs_strategy",
                         values="verhaeltnis").to_string())



def skalierung():
    """Rechenzeit gegen Eingangsgröße über fuenf Ausdehnungen.

    Aufgetragen ist die vom Backend abgerechnete Eingangsgröße, nicht die
    Kategorie small bis xxlarge. Aus der Steigung auf doppelt logarithmischen
    Achsen laesst sich ablesen, wie die Zeit mit der Datenmenge waechst. Eine
    Steigung von eins bedeutet proportionales Wachstum, eine flachere Steigung
    heisst, dass feste Anteile dominieren.

    Die Rechenzeit im Backend statt der Gesamtzeit, weil die Warteschlange
    tageweise um Faktor zwei schwankt und die Stufen an verschiedenen Tagen
    gemessen wurden.
    """
    df = hole("""
      SELECT r.run_id, r.crs_strategy, r.extent_size, r.input_pixels_mp,
             r.processing_time, r.total_time, r.status
      FROM runs r
      WHERE r.archived=false AND r.dataset='dem' AND r.resolution_m=10
        AND r.workflow='merge_add' AND r.crs_strategy <> 'local_reference'
        AND (r.dem_layout='cog' OR r.dem_layout IS NULL)
        AND r.run_id > 878
      ORDER BY r.run_id
    """)
    ok = df[(df.status == "success") & df.input_pixels_mp.notna()]
    if ok.empty:
        print("keine Daten fuer die Skalierung"); return

    fig, ax = plt.subplots(figsize=(6.3, 3.4))

    for strategie in ("local_preprocessing", "onthefly"):
        teil = ok[ok.crs_strategy == strategie]
        if teil.empty:
            continue
        ax.plot(teil.input_pixels_mp, teil.processing_time, linestyle="none",
                marker=STIL[strategie]["marker"], color=STIL[strategie]["color"],
                markersize=3.6, alpha=0.45, markeredgewidth=0, zorder=2)
        med = teil.groupby("extent_size").agg(
            x=("input_pixels_mp", "median"),
            y=("processing_time", "median")).sort_values("x")
        ax.plot(med.x, med.y, color=STIL[strategie]["color"], linewidth=1.4,
                marker=STIL[strategie]["marker"], markersize=6,
                markeredgecolor="white", markeredgewidth=0.6, zorder=3,
                label=STIL[strategie]["label"])

    # Gescheiterte Stufen markieren: dort existiert kein Messwert, die Luecke
    # ist selbst ein Ergebnis.
    fehl = df[(df.status != "success")].groupby("extent_size").size()
    if len(fehl):
        bezug = ok[ok.crs_strategy == "local_preprocessing"].groupby(
            "extent_size").input_pixels_mp.median()
        for stufe, n in fehl.items():
            if stufe in bezug.index:
                ax.annotate(f"onthefly scheitert\n({n} von {n} Läufen)",
                            (bezug[stufe], ax.get_ylim()[0]),
                            textcoords="offset points", xytext=(0, 14),
                            ha="center", fontsize=6.5, color="#b35806")

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Eingangsgröße in Megapixeln, vom Backend abgerechnet")
    ax.set_ylabel("Rechenzeit im Backend in Sekunden")
    ax.grid(linewidth=0.4, color="#e0e0e0")
    ax.set_axisbelow(True)
    for rand in ("top", "right"):
        ax.spines[rand].set_visible(False)
    ax.legend(frameon=False, loc="upper left")

    fig.tight_layout()
    datei = ziel("ff1", "c_skalierung_rechenzeit.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)

    k = ok.groupby(["extent_size", "crs_strategy"]).agg(
        n=("run_id", "count"),
        mpixel=("input_pixels_mp", "median"),
        rechenzeit=("processing_time", "median"),
        gesamt=("total_time", "median")).round(1)
    print(k.to_string())
    print()
    print("gescheitert je Stufe:")
    print(df[df.status != "success"].groupby(
        ["extent_size", "crs_strategy"]).size().to_string())



def backendvergleich():
    """Genauigkeit je Operation auf CDSE und Terrascope, Absolutwerte.

    Darstellung wie beim Operationsvergleich, damit beide Fehler sichtbar
    bleiben und nicht nur ihr Verhaeltnis. Ein Faktor allein laesst offen,
    ob er aus einem kleinen Nenner oder einem grossen Zaehler stammt.

    Beide Installationen fahren dieselbe Backend-Familie, halten aber
    verschiedene Bestaende vor. Fuer denselben Ausschnitt und Zeitraum liefert
    CDSE 16 Sentinel-2-Aufnahmen, Terrascope 3, davon eine zu 56 Prozent leer.
    Jede Installation wird gegen eine eigene lokale Referenz gemessen, die
    Absolutwerte sind zwischen den Installationen deshalb nicht streng
    vergleichbar. Das Muster über die Operationen ist es.

    focal fehlt fuer Terrascope: die 3x3-Faltung mittelt den Nodata-Sentinel
    32767 mit gueltigen Nachbarn zu Zwischenwerten, die kein Sentinel mehr
    sind und deshalb in den Vergleich einlaufen.
    """
    import csv
    df = hole("""
      SELECT r.run_id, r.crs_strategy, r.workflow, r.backend_url, a.mae
      FROM runs r JOIN accuracy a ON a.run_id = r.run_id
      WHERE r.extent_size='medium' AND r.archived=false AND r.status='success'
        AND r.dataset='dem' AND r.resolution_m=10
        AND r.crs_strategy <> 'local_reference'
      ORDER BY r.run_id
    """)
    df["backend"] = df.backend_url.map(
        lambda u: "Terrascope" if "terrascope" in str(u) else "CDSE")
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = z["block"]
    df = df[(df.backend == "Terrascope")
            | (df.run_id.map(lambda i: zu.get(i, "")) == "A1")]
    df = df.drop_duplicates(["backend", "workflow", "crs_strategy"])
    if df.empty:
        print("keine Daten fuer den Backendvergleich"); return

    AUSGESCHLOSSEN = {("Terrascope", "focal")}
    fig, achsen = plt.subplots(1, 2, figsize=(6.3, 3.1), sharey=True)
    x = np.arange(len(OPERATIONEN))

    for ax, backend in zip(achsen, ["CDSE", "Terrascope"]):
        reihen = {}
        for strategie in ("local_preprocessing", "onthefly"):
            werte = []
            for op in OPERATIONEN:
                if (backend, op) in AUSGESCHLOSSEN:
                    werte.append(np.nan); continue
                tr = df[(df.backend == backend) & (df.workflow == op)
                        & (df.crs_strategy == strategie)].mae
                werte.append(float(tr.iloc[0]) if len(tr) else np.nan)
            reihen[strategie] = werte

        for i in range(len(OPERATIONEN)):
            u, o = reihen["local_preprocessing"][i], reihen["onthefly"][i]
            if np.isfinite(u) and np.isfinite(o):
                ax.plot([i, i], [u, o], color="#c0c0c0", linewidth=0.9, zorder=1)
        for strategie, werte in reihen.items():
            ax.plot(x, werte, linestyle="none", markersize=5,
                    markeredgecolor="white", markeredgewidth=0.5,
                    zorder=3, **STIL[strategie])
        for op in OPERATIONEN:
            if (backend, op) in AUSGESCHLOSSEN:
                ax.annotate("nicht\nauswertbar", (OPERATIONEN.index(op), 0.03),
                            ha="center", va="center", fontsize=6,
                            color="#999999")

        ax.set_yscale("log")
        ax.set_xticks(x)
        ax.set_xticklabels(OPERATIONEN, rotation=45, ha="right")
        ax.set_xlim(-0.6, len(OPERATIONEN) - 0.4)
        ax.set_title(backend)
        ax.grid(axis="y", which="major", linewidth=0.4, color="#d5d5d5")
        ax.set_axisbelow(True)
        for rand in ("top", "right"):
            ax.spines[rand].set_visible(False)

    alle = [w for ax_i, backend in enumerate(["CDSE", "Terrascope"])
            for w in []]
    achsen[0].set_ylim(2e-4, 1e1)
    achsen[0].set_ylabel("MAE")
    griffe, namen = achsen[0].get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=2, frameon=False,
               bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    datei = ziel("ff3", "t_backendvergleich_genauigkeit.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)
    print(df.pivot_table(index="workflow", columns=["backend", "crs_strategy"],
                         values="mae").round(5).to_string())


def fehlerverteilung():
    """Genauigkeitswerte je Operation, alle Konfigurationen aus A1, A2 und B.

    Die Streuung stammt nicht aus Messrauschen, sondern aus dem Operationstyp.
    Deshalb steht hier je Operation eine eigene Position, nicht eine gemeinsame
    Wolke je Strategie. Bei merge_add liegen zusaetzlich die neun Regionen des
    Regionsvergleichs, bei den uebrigen Operationen Berlin und Hamburg.

    Werte einer Konfiguration sind über die Wiederholungen identisch, gezeigt
    wird die Streuung zwischen den Konfigurationen.
    """
    import csv
    df = hole("""
      SELECT r.run_id, r.crs_strategy, r.workflow, a.mae, a.rmse
      FROM runs r JOIN accuracy a ON a.run_id = r.run_id
      WHERE r.extent_size='medium' AND r.archived=false AND r.status='success'
        AND r.dataset='dem' AND r.resolution_m=10
        AND r.crs_strategy <> 'local_reference'
        AND r.backend_url NOT LIKE '%terrascope%'
      ORDER BY r.run_id
    """)
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = (z["block"], z["region"])
    df["block"] = df.run_id.map(lambda i: zu.get(i, ("", ""))[0])
    df["region"] = df.run_id.map(lambda i: zu.get(i, ("", ""))[1])
    df = df[df.block.isin(["A1", "A2", "B"])]
    df = df.drop_duplicates(["block", "region", "workflow", "crs_strategy"])
    if df.empty:
        print("keine Daten"); return

    fig, achsen = plt.subplots(2, 1, figsize=(6.3, 5.2), sharex=True)
    rng = np.random.default_rng(0)
    x = np.arange(len(OPERATIONEN))
    versatz = {"local_preprocessing": -0.17, "onthefly": 0.17}

    for ax, (spalte, name) in zip(achsen, [("mae", "MAE"), ("rmse", "RMSE")]):
        for strategie, dx in versatz.items():
            for i, op in enumerate(OPERATIONEN):
                werte = df[(df.workflow == op)
                           & (df.crs_strategy == strategie)][spalte].dropna().values
                if not len(werte):
                    continue
                jitter = rng.uniform(-0.055, 0.055, size=len(werte))
                ax.plot(np.full(len(werte), i + dx) + jitter, werte,
                        linestyle="none", marker=STIL[strategie]["marker"],
                        color=STIL[strategie]["color"], markersize=4, alpha=0.6,
                        markeredgewidth=0, zorder=2,
                        label=STIL[strategie]["label"] if i == 0 else None)
                med = float(np.median(werte))
                ax.plot([i + dx - 0.13, i + dx + 0.13], [med, med],
                        color=STIL[strategie]["color"], linewidth=1.8, zorder=3)
        ax.set_yscale("log")
        ax.set_ylabel(name)
        ax.grid(axis="y", linewidth=0.4, color="#d5d5d5")
        ax.set_axisbelow(True)
        for rand in ("top", "right"):
            ax.spines[rand].set_visible(False)

    # Trennlinie zwischen punktweisen und uebrigen Operationen
    for ax in achsen:
        ax.axvline(3.5, color="#bbbbbb", linewidth=0.8, linestyle=(0, (4, 3)))
    achsen[0].annotate("punktweise", (1.5, achsen[0].get_ylim()[1]),
                       textcoords="offset points", xytext=(0, -10),
                       ha="center", fontsize=7, color="#888888")
    achsen[0].annotate("nachbarschafts- und zeitbezogen", (5, achsen[0].get_ylim()[1]),
                       textcoords="offset points", xytext=(0, -10),
                       ha="center", fontsize=7, color="#888888")

    achsen[1].set_xticks(x)
    achsen[1].set_xticklabels(OPERATIONEN, rotation=45, ha="right")
    achsen[1].set_xlim(-0.6, len(OPERATIONEN) - 0.4)

    griffe, namen = achsen[0].get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=2, frameon=False,
               bbox_to_anchor=(0.5, -0.015))
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    datei = ziel("ff2", "a1a2b_fehlerverteilung.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)
    print(df.groupby(["workflow", "crs_strategy"]).agg(
        n=("run_id", "count"), mae_min=("mae", "min"), mae_max=("mae", "max"),
        rmse_max=("rmse", "max")).round(5).to_string())


def zugriff_formate():
    """Datenzugriff bei merge_add und filter_bbox über fuenf DEM-Formate.

    Nur zwei Operationen, weil alle Operationen ohne raeumlichen Zuschnitt
    denselben Zugriff erzeugen. Fuer cog und striped ist das über alle sieben
    Operationen geprueft, dort liegen die Werte bitgleich. merge_add steht
    deshalb stellvertretend fuer diese Gruppe, filter_bbox ist die einzige
    Operation mit Zuschnitt, hier auf ein Viertel der Flaeche.

    Die gestrichelte Linie je Format markiert die Dateigröße des
    hochgeladenen Assets. Wird sie erreicht, liest das Backend die Datei
    vollstaendig.
    """
    df = hole("""
      SELECT r.run_id, r.workflow, r.dem_format, r.dem_layout, r.asset_bytes,
             (SELECT count(*) FROM nginx_access_log n WHERE n.run_id=r.run_id) AS anfragen,
             (SELECT sum(bytes_sent) FROM nginx_access_log n WHERE n.run_id=r.run_id) AS bytes
      FROM runs r
      WHERE r.extent_size='medium' AND r.archived=false AND r.status='success'
        AND r.dataset='dem' AND r.resolution_m=10
        AND r.crs_strategy='local_preprocessing'
        AND r.workflow IN ('merge_add','filter_bbox')
        AND r.backend_url NOT LIKE '%terrascope%'
      ORDER BY r.run_id
    """)
    if df.empty:
        print("keine Daten"); return

    df["format"] = df.apply(
        lambda z: "cog" if z.dem_layout == "cog"
        else ("gtiff striped" if z.dem_format == "gtiff" and z.dem_layout == "striped"
              else ("gtiff tiled,\nunkomprimiert" if z.dem_layout == "tiled_uncompressed"
                    else z.dem_format)), axis=1)
    ORDNUNG = ["cog", "gtiff striped", "gtiff tiled,\nunkomprimiert", "netcdf", "zarr"]
    formate = [f for f in ORDNUNG if f in set(df["format"])]

    fig, (oben, unten) = plt.subplots(2, 1, figsize=(6.3, 4.6), sharex=True,
                                      gridspec_kw={"height_ratios": [1.25, 1]})
    x = np.arange(len(formate))
    breite = 0.36
    FARBE = {"merge_add": "#1f4e79", "filter_bbox": "#e08214"}
    NAME = {"merge_add": "merge_add (ohne Zuschnitt)",
            "filter_bbox": "filter_bbox (Zuschnitt auf 1/4)"}

    for k, op in enumerate(["merge_add", "filter_bbox"]):
        mb, anf = [], []
        for f in formate:
            teil = df[(df["format"] == f) & (df.workflow == op)]
            mb.append(float(teil.bytes.mean()) / 1e6 if len(teil) else np.nan)
            anf.append(float(teil.anfragen.mean()) if len(teil) else np.nan)
        pos = x + (k - 0.5) * breite
        for ax, werte, fmt in ((oben, mb, "{:.2f}"), (unten, anf, "{:.0f}")):
            ax.bar(pos, werte, breite, color=FARBE[op],
                   label=NAME[op] if ax is oben else None,
                   edgecolor="white", linewidth=0.5)
            for xi, w in zip(pos, werte):
                if np.isfinite(w):
                    ax.annotate(fmt.format(w), (xi, w),
                                textcoords="offset points", xytext=(0, 2),
                                ha="center", fontsize=6.5, color="#444444")

    # Dateigröße je Format als kurze Linie über der Gruppe
    for i, f in enumerate(formate):
        gr = float(df[df["format"] == f].asset_bytes.max()) / 1e6
        oben.plot([i - 0.42, i + 0.42], [gr, gr], color="#666666",
                  linewidth=1.0, linestyle=(0, (4, 3)), zorder=4)
    oben.plot([], [], color="#666666", linewidth=1.0, linestyle=(0, (4, 3)),
              label="Dateigröße des Assets")

    oben.set_ylabel("Übertragenes Volumen in MB")
    oben.set_ylim(0, 6.6)
    unten.set_ylabel("Anfragen")
    unten.set_ylim(0, max(df.anfragen) * 1.22)
    unten.set_xticks(x)
    unten.set_xticklabels(formate)
    unten.set_xlim(-0.6, len(formate) - 0.4)

    for ax in (oben, unten):
        ax.grid(axis="y", linewidth=0.4, color="#d5d5d5")
        ax.set_axisbelow(True)
        for rand in ("top", "right"):
            ax.spines[rand].set_visible(False)

    griffe, namen = oben.get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=3, frameon=False,
               bbox_to_anchor=(0.5, -0.015), fontsize=7.5)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    datei = ziel("ff1", "a1d_zugriff_formate.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)

    k = df.groupby(["format", "workflow"]).agg(
        n=("run_id", "count"), anfragen=("anfragen", "mean"),
        mb=("bytes", lambda s: s.mean() / 1e6),
        asset_mb=("asset_bytes", lambda s: s.max() / 1e6)).round(2)
    k["anteil"] = (k.mb / k.asset_mb).round(2)
    print(k.to_string())


def credits_aufschluesselung():
    """Abgerechnete Credits je Strategie und Ausdehnung.

    Die Credits sind die Abrechnungsgröße des Backends und damit die
    einzige Kostenkennzahl, die unabhaengig von der Laufzeit vorliegt. Bei
    local_preprocessing faellt zusaetzlich der vorgelagerte Bezug des
    Hoehenmodells an, der als eigener Batch-Job eigene Credits kostet.
    """
    df = hole("""
      SELECT extent_size, crs_strategy, credits, cpu_seconds, run_id
      FROM runs
      WHERE archived=false AND status='success' AND dataset='dem'
        AND resolution_m=10 AND workflow='merge_add'
        AND crs_strategy <> 'local_reference'
        AND backend_url NOT LIKE '%terrascope%'
        AND (dem_layout='cog' OR dem_layout IS NULL)
    """)
    if df.empty:
        print("keine Daten"); return

    stufen = [s for s in ("small", "medium", "large", "xlarge", "xxlarge")
              if s in set(df.extent_size)]
    strategien = ["local_preprocessing", "onthefly"]
    x = np.arange(len(stufen))
    breite = 0.38

    fig, ax = plt.subplots(figsize=(6.3, 2.9))
    for k, strategie in enumerate(strategien):
        werte = []
        for s in stufen:
            t = df[(df.extent_size == s) & (df.crs_strategy == strategie)].credits
            werte.append(float(t.median()) if len(t) else np.nan)
        balken = ax.bar(x + (k - 0.5) * breite, werte, breite,
                        color=STIL[strategie]["color"],
                        label=STIL[strategie]["label"],
                        edgecolor="white", linewidth=0.5)
        for rechteck, w in zip(balken, werte):
            mitte = rechteck.get_x() + rechteck.get_width() / 2
            if np.isfinite(w):
                ax.annotate(f"{w:.0f}", (mitte, w),
                            textcoords="offset points", xytext=(0, 2),
                            ha="center", fontsize=7, color="#444444")

    ax.set_xticks(x)
    ax.set_xticklabels(stufen)
    ax.set_xlim(-0.6, len(stufen) - 0.4)
    ax.set_ylabel("Credits je Lauf, Median")
    ax.grid(axis="y", linewidth=0.4, color="#d5d5d5")
    ax.set_axisbelow(True)
    for rand in ("top", "right"):
        ax.spines[rand].set_visible(False)
    ax.legend(frameon=False, loc="upper left")

    fig.tight_layout()
    datei = ziel("ff1", "c_credits_strategien.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)
    print(df.groupby(["extent_size", "crs_strategy"]).agg(
        n=("run_id", "count"), credits=("credits", "median"),
        cpu=("cpu_seconds", "median")).round(1).to_string())



def tagesabhaengigkeit():
    """Rechenzeit im Backend nach Messtag, merge_add bei mittlerer Ausdehnung.

    Die Rechenzeit verteilt sich zweigipflig auf einen Bereich um 60 bis 75 und
    einen um 115 bis 135 Sekunden. Diese Abbildung zeigt, dass die Zugehoerigkeit
    am Messtag haengt und nicht an Region, Operation oder Lauftyp. Damit wird aus
    einer Beobachtung ein Beleg, und Laufzeitvergleiche zwischen Konfigurationen
    aus verschiedenen Messtagen lassen sich entsprechend einordnen.

    Gezeigt sind alle merge_add-Läufe bei mittlerer Ausdehnung aus dem
    Operationsvergleich, der Regionsstichprobe und dem Regionsvergleich.
    """
    import csv
    df = hole("""
      SELECT run_id, substr(timestamp,1,10) AS tag, crs_strategy, run_type,
             processing_time
      FROM runs
      WHERE extent_size='medium' AND archived=false AND status='success'
        AND dataset='dem' AND resolution_m=10 AND workflow='merge_add'
        AND crs_strategy <> 'local_reference'
        AND backend_url NOT LIKE '%terrascope%'
        AND (dem_layout='cog' OR dem_layout IS NULL)
      ORDER BY run_id
    """)
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = (z["block"], z["region"])
    df["block"] = df.run_id.map(lambda i: zu.get(i, ("", ""))[0])
    df["region"] = df.run_id.map(lambda i: zu.get(i, ("", ""))[1])
    df = df[df.block.isin(["A1", "A2", "B"])]
    if df.empty:
        print("keine Daten"); return

    tage = sorted(df.tag.unique())
    x = np.arange(len(tage))
    rng = np.random.default_rng(0)
    versatz = {"local_preprocessing": -0.14, "onthefly": 0.14}

    fig, ax = plt.subplots(figsize=(6.3, 3.2))

    # Bereiche der beiden Haeufungen hinterlegen
    ax.axhspan(55, 80, color="#f0f4f8", zorder=0)
    ax.axhspan(110, 140, color="#f0f4f8", zorder=0)
    ax.annotate("untere Haeufung", (len(tage) - 0.45, 67.5),
                ha="right", va="center", fontsize=6.5, color="#8a8a8a")
    ax.annotate("obere Haeufung", (len(tage) - 0.45, 125),
                ha="right", va="center", fontsize=6.5, color="#8a8a8a")

    for strategie, dx in versatz.items():
        for i, tag in enumerate(tage):
            werte = df[(df.tag == tag)
                       & (df.crs_strategy == strategie)].processing_time.values
            if not len(werte):
                continue
            jitter = rng.uniform(-0.05, 0.05, size=len(werte))
            ax.plot(np.full(len(werte), i + dx) + jitter, werte,
                    linestyle="none", marker=STIL[strategie]["marker"],
                    color=STIL[strategie]["color"], markersize=4.2, alpha=0.65,
                    markeredgewidth=0, zorder=3,
                    label=STIL[strategie]["label"] if i == 0 else None)
            med = float(np.median(werte))
            ax.plot([i + dx - 0.11, i + dx + 0.11], [med, med],
                    color=STIL[strategie]["color"], linewidth=1.8, zorder=4)

    anzahl = df.groupby("tag").size()
    ax.set_xticks(x)
    ax.set_xticklabels([f"{t[8:10]}.{t[5:7]}.\n(n={anzahl.get(t, 0)})" for t in tage])
    ax.set_xlim(-0.5, len(tage) - 0.5)
    ax.set_ylim(0, df.processing_time.max() * 1.08)
    ax.set_ylabel("Rechenzeit im Backend in Sekunden")
    ax.set_xlabel("Messtag")
    ax.grid(axis="y", linewidth=0.4, color="#d5d5d5")
    ax.set_axisbelow(True)
    for rand in ("top", "right"):
        ax.spines[rand].set_visible(False)

    griffe, namen = ax.get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=2, frameon=False,
               bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    datei = ziel("ff3", "a1a2b_tagesabhaengigkeit.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)

    print(df.groupby("tag").processing_time.agg(
        ["count", "median", "min", "max", "std"]).round(1).to_string())
    print()
    df["gruppe"] = df.processing_time.map(lambda v: "hoch" if v > 100 else "niedrig")
    print(df.groupby(["tag", "gruppe"]).size().to_string())
    print()
    print("Kontrolle cold/hot:")
    print(df.groupby(["gruppe", "run_type"]).size().to_string())



def skalierung_backends():
    """Rechenzeit über die Gebietsgröße, beide Installationen nebeneinander.

    Aufgetragen ist die Rechenzeit im Backend gegen die Kantenlänge des
    Ausschnitts, beide Achsen logarithmisch. Aus der Steigung laesst sich
    ablesen, wie die Zeit mit der Flaeche waechst. Eine Steigung von 2 bedeutet
    proportionales Wachstum mit der Flaeche, eine flachere Steigung heisst, dass
    feste Anteile mitlaufen.

    Die Rechenzeit statt der Gesamtzeit, weil die Warteschlange tageweise um den
    Faktor zwei schwankt und die Stufen an verschiedenen Tagen gemessen wurden.

    Die Absolutwerte sind zwischen den Installationen nur eingeschraenkt
    vergleichbar. Fuer denselben Ausschnitt und Zeitraum liefert CDSE 16
    Sentinel-2-Aufnahmen, Terrascope 3, dort faellt entsprechend weniger Arbeit
    an. Vergleichbar ist der Verlauf und die Stelle, an der eine Strategie
    ausfaellt.
    """
    df = hole("""
      SELECT run_id, crs_strategy, extent_size, processing_time, status,
             CASE WHEN backend_url LIKE '%terrascope%' THEN 'Terrascope'
                  ELSE 'CDSE' END AS backend
      FROM runs
      WHERE archived=false AND dataset='dem' AND resolution_m=10
        AND workflow='merge_add' AND crs_strategy IN ('local_preprocessing','onthefly')
        AND (dem_layout='cog' OR dem_layout IS NULL)
      ORDER BY run_id
    """)
    if df.empty:
        print("keine Daten"); return

    # Kantenlänge des Ausschnitts in km, aus der Messmatrix
    KANTE = {"small": 5, "medium": 12, "large": 50, "xlarge": 100, "xxlarge": 200}
    stufen = ["small", "medium", "large", "xlarge", "xxlarge"]

    fig, achsen = plt.subplots(1, 2, figsize=(6.3, 3.3), sharey=True)

    for ax, backend in zip(achsen, ["CDSE", "Terrascope"]):
        teil = df[df.backend == backend]
        for strategie in ("local_preprocessing", "onthefly"):
            ok = teil[(teil.crs_strategy == strategie) & (teil.status == "success")]
            xs, ys = [], []
            for s in stufen:
                w = ok[ok.extent_size == s].processing_time
                if len(w):
                    xs.append(KANTE[s])
                    ys.append(float(w.median()))
            if xs:
                ax.plot(xs, ys, color=STIL[strategie]["color"],
                        marker=STIL[strategie]["marker"], markersize=5.5,
                        linewidth=1.4, markeredgecolor="white",
                        markeredgewidth=0.6, zorder=3,
                        label=STIL[strategie]["label"])
            # Stufen, auf denen die Strategie ausfaellt
            for s in stufen:
                fehl = teil[(teil.crs_strategy == strategie)
                            & (teil.extent_size == s) & (teil.status != "success")]
                erf = ok[ok.extent_size == s]
                if len(fehl) and not len(erf):
                    hoehe = ys[-1] if ys else 100
                    ax.plot([KANTE[s]], [hoehe], linestyle="none",
                            marker="x", markersize=9, markeredgewidth=2.2,
                            color=STIL[strategie]["color"], zorder=5)

        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xticks([KANTE[s] for s in stufen])
        ax.set_xticklabels([f"{KANTE[s]}" for s in stufen])
        ax.set_xlabel("Kantenlänge des Ausschnitts in km")
        ax.set_title(backend)
        ax.grid(which="major", linewidth=0.4, color="#d5d5d5")
        ax.set_axisbelow(True)
        for rand in ("top", "right"):
            ax.spines[rand].set_visible(False)

    achsen[0].set_ylabel("Rechenzeit im Backend in Sekunden")
    griffe, namen = achsen[0].get_legend_handles_labels()
    from matplotlib.lines import Line2D
    griffe.append(Line2D([0], [0], linestyle="none", marker="x", markersize=9,
                         markeredgewidth=2.2, color=STIL["onthefly"]["color"]))
    namen.append("scheitert in allen 5 Läufen, Backendfehler")
    fig.legend(griffe, namen, loc="lower center", ncol=3, frameon=False,
               bbox_to_anchor=(0.5, -0.02), fontsize=7.5)
    fig.tight_layout(rect=(0, 0.07, 1, 1))
    datei = ziel("ff1", "ct_skalierung_backends.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)

    k = df[df.status == "success"].groupby(
        ["backend", "extent_size", "crs_strategy"]).agg(
        n=("run_id", "count"), rechenzeit=("processing_time", "median")).round(1)
    print(k.to_string())
    print()
    print("Fehlschlaege:")
    print(df[df.status != "success"].groupby(
        ["backend", "extent_size", "crs_strategy"]).size().to_string())



def tradeoff():
    """Genauigkeit und Gesamtdauer je Operation, beide Strategien, zwei Teilbilder.

    Links der MAE gegen die Referenz, rechts die Gesamtdauer je Lauf. Beide
    Teilbilder haben denselben Aufbau, je Operation ein Markerpaar mit
    Verbindungslinie, damit der Leser die Abstaende direkt vergleichen kann.
    Links reicht der Abstand von drei Groessenordnungen bis fast null, rechts
    liegt er bei allen Operationen um hundert Sekunden.

    Die Gesamtdauer enthaelt bei local_pp den vorgelagerten DEM-Bezug, weil
    bei onthefly dieselbe Transformationsarbeit im Messlauf steckt. Die
    MAE-Achse ist logarithmisch, die Zeitachse beginnt bei null.
    Werte sind Mediane über Berlin und Hamburg.
    """
    import csv
    df = hole("""
      SELECT r.run_id, r.crs_strategy, r.workflow, r.total_time,
             r.dem_download_time, a.mae
      FROM runs r JOIN accuracy a ON a.run_id = r.run_id
      WHERE r.extent_size='medium' AND r.archived=false AND r.status='success'
        AND r.dataset='dem' AND r.resolution_m=10
        AND r.crs_strategy <> 'local_reference'
        AND r.backend_url NOT LIKE '%terrascope%'
        AND (r.dem_layout='cog' OR r.dem_layout IS NULL)
      ORDER BY r.run_id
    """).fillna({"dem_download_time": 0})
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = z["block"]
    df["block"] = df.run_id.map(lambda i: zu.get(i, ""))
    df = df[df.block.isin(["A1", "A2"])]
    if df.empty:
        print("keine Daten"); return
    df["zeit"] = df.total_time + df.dem_download_time

    m = df.groupby(["workflow", "crs_strategy"]).agg(
        mae=("mae", "median"), zeit=("zeit", "median")).reset_index()

    x = np.arange(len(OPERATIONEN))
    fig, (links, rechts) = plt.subplots(1, 2, figsize=(6.3, 3.3))

    for ax, spalte in ((links, "mae"), (rechts, "zeit")):
        reihen = {}
        for strategie in ("local_preprocessing", "onthefly"):
            werte = []
            for op in OPERATIONEN:
                tr = m[(m.workflow == op) & (m.crs_strategy == strategie)][spalte]
                werte.append(float(tr.iloc[0]) if len(tr) else np.nan)
            reihen[strategie] = werte
        for i in range(len(OPERATIONEN)):
            u, o = reihen["local_preprocessing"][i], reihen["onthefly"][i]
            if np.isfinite(u) and np.isfinite(o):
                ax.plot([i, i], [u, o], color="#c0c0c0", linewidth=0.9, zorder=1)
        for strategie, werte in reihen.items():
            ax.plot(x, werte, linestyle="none", markersize=5,
                    markeredgecolor="white", markeredgewidth=0.5,
                    zorder=3, **STIL[strategie])
        ax.set_xticks(x)
        ax.set_xticklabels(OPERATIONEN, rotation=45, ha="right")
        ax.set_xlim(-0.6, len(OPERATIONEN) - 0.4)
        ax.axvline(3.5, color="#bbbbbb", linewidth=0.8, linestyle=(0, (4, 3)))
        ax.grid(axis="y", linewidth=0.4, color="#d5d5d5")
        ax.set_axisbelow(True)
        for rand in ("top", "right"):
            ax.spines[rand].set_visible(False)

    links.set_yscale("log")
    alle = m.mae.dropna()
    links.set_ylim(alle.min() / 3, alle.max() * 3)
    links.set_ylabel("MAE gegen die Referenz")
    links.set_title("Genauigkeit")

    rechts.set_ylim(0, m.zeit.max() * 1.15)
    rechts.set_ylabel("Gesamtdauer je Lauf in Sekunden")
    rechts.set_title("Laufzeit, local_pp mit DEM-Bezug")

    griffe, namen = links.get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=2, frameon=False,
               bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    datei = ziel("ff2", "a1a2_tradeoff_genauigkeit_laufzeit.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)
    print(m.pivot_table(index="workflow", columns="crs_strategy",
                        values=["mae", "zeit"]).round(4).to_string())


def gitterversatz():
    """Versatz zwischen Backend-Gitter und Referenzgitter je Gebietsstufe.

    Die lokale Referenz reprojiziert immer auf exakt 10,0 m. Weicht die
    Zellgröße des Backend-Ergebnisses davon ab, laufen beide Gitter über die
    Breite auseinander. Gezeigt ist der daraus folgende Versatz am Rand des
    Ausschnitts in Zellen, also Abweichung mal Spaltenzahl. Ab einer Zelle
    vergleicht ein pixelweiser Vergleich nicht mehr denselben Ort.

    Die Abweichung je Zelle steht im Text, sie ergibt sich aus dem Versatz
    geteilt durch die Spaltenzahl.

    Werte aus den Ergebnisdateien der local_preprocessing-Läufe bei berlin,
    merge_add, cog, gelesen am 02.09.2026:
      small   run_20260826_125724  10.000000000 m   529 x   528
      medium  run_20260819_162031  10.000000000 m  1152 x  1070
      large   run_20260826_142009   9.998138149 m  5362 x  5371
      xlarge  run_20260826_185921   9.981381493 m 10718 x 10742
      xxlarge run_20260826_215638  10.000000000 m 21437 x 21376
    xxlarge lief mit --dem-tiles 4, das DEM lag also in vier Kacheln vor.
    --force-target-crs allein aendert bei large nichts, geprueft am 02.09.
    mit run 1310, Zellgröße weiterhin 9,998138149.
    """
    stufen = ["small", "medium", "large", "xlarge", "xxlarge"]
    zell = [10.000000000, 10.000000000, 9.998138149, 9.981381493, 10.000000000]
    spalten = [528, 1070, 5371, 10742, 21376]
    versatz_zellen = [(10.0 - z) * s / 10.0 for z, s in zip(zell, spalten)]

    fig, ax = plt.subplots(figsize=(6.3, 2.9))
    x = np.arange(len(stufen))
    farben = ["#1f4e79" if v == 0 else "#c0392b" for v in versatz_zellen]

    ax.bar(x, versatz_zellen, 0.55, color=farben, edgecolor="white", linewidth=0.5)
    ax.axhline(1, color="#666666", linewidth=0.9, linestyle=(0, (4, 3)),
               label="eine Zelle, Grenze des pixelweisen Vergleichs")
    for i, v in enumerate(versatz_zellen):
        ax.annotate(f"{v:.1f}" if v else "0", (i, v),
                    textcoords="offset points", xytext=(0, 3),
                    ha="center", fontsize=7.5, color="#444444")

    ax.set_xticks(x)
    ax.set_xticklabels(stufen)
    ax.set_xlim(-0.6, len(stufen) - 0.4)
    ax.set_ylim(0, max(versatz_zellen) * 1.2)
    ax.set_ylabel("Versatz am Rand des Ausschnitts\nin Zellen")
    ax.grid(axis="y", linewidth=0.4, color="#d5d5d5")
    ax.set_axisbelow(True)
    for rand in ("top", "right"):
        ax.spines[rand].set_visible(False)

    griffe, namen = ax.get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=1, frameon=False,
               bbox_to_anchor=(0.5, -0.02), fontsize=7.5)
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    datei = ziel("ff3", "c_gitterversatz.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)
    for s, z, sp, v in zip(stufen, zell, spalten, versatz_zellen):
        print(f"{s:8s} {z:.9f} m  {sp:5d} Spalten  "
              f"{(10.0-z)*1000:6.2f} mm/Zelle  {v:5.1f} Zellen Versatz")

def genauigkeit_einzeln():
    """Genauigkeit je Operation als vier Einzeldateien fuer eine 2x2-Subfigure.

    Je Region und Fehlermass eine Datei mit einem einzigen Panel. Die Legende
    steht nicht im Bild, sondern gehoert in die Bildunterschrift der
    übergeordneten Abbildung, damit sie nicht viermal wiederholt wird.
    Dieselbe Ordinatenskala in allen vier, damit die Panels vergleichbar sind.
    """
    import csv
    df = hole("""
      SELECT r.run_id, r.crs_strategy, r.workflow, a.mae, a.rmse
      FROM runs r JOIN accuracy a ON a.run_id = r.run_id
      WHERE r.extent_size='medium' AND r.archived=false AND r.status='success'
        AND r.dataset='dem' AND r.resolution_m=10
        AND r.crs_strategy <> 'local_reference'
        AND r.backend_url NOT LIKE '%terrascope%'
      ORDER BY r.run_id
    """)
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = z["block"]
    df["block"] = df.run_id.map(lambda i: zu.get(i, ""))
    df = df[df.block.isin(["A1", "A2"])].drop_duplicates(
        ["block", "workflow", "crs_strategy"])
    if df.empty:
        print("keine Daten"); return

    x = np.arange(len(OPERATIONEN))
    for spalte, name in (("mae", "mae"), ("rmse", "rmse")):
        # gemeinsame Skala je Fehlermass über beide Regionen
        alle = df[spalte].dropna()
        ylim = (alle.min() / 3, alle.max() * 3)
        for block, region, kurz in (("A1", "Berlin", "berlin"),
                                    ("A2", "Hamburg", "hamburg")):
            teil = df[df.block == block]
            fig, ax = plt.subplots(figsize=(3.2, 2.7))
            reihen = {}
            for strategie in ("local_preprocessing", "onthefly"):
                werte = []
                for op in OPERATIONEN:
                    tr = teil[(teil.workflow == op)
                              & (teil.crs_strategy == strategie)][spalte]
                    werte.append(float(tr.iloc[0]) if len(tr) else np.nan)
                reihen[strategie] = werte
            for i in range(len(OPERATIONEN)):
                u, o = reihen["local_preprocessing"][i], reihen["onthefly"][i]
                if np.isfinite(u) and np.isfinite(o):
                    ax.plot([i, i], [u, o], color="#c0c0c0", linewidth=0.9, zorder=1)
            for strategie, werte in reihen.items():
                ax.plot(x, werte, linestyle="none", markersize=5,
                        markeredgecolor="white", markeredgewidth=0.5,
                        zorder=3, **STIL[strategie])
            ax.set_yscale("log")
            ax.set_ylim(*ylim)
            ax.set_xticks(x)
            ax.set_xticklabels(OPERATIONEN, rotation=45, ha="right", fontsize=7)
            ax.set_xlim(-0.6, len(OPERATIONEN) - 0.4)
            ax.set_ylabel(name.upper())
            ax.set_title(region, fontsize=9)
            ax.grid(axis="y", which="major", linewidth=0.4, color="#d5d5d5")
            ax.set_axisbelow(True)
            for rand in ("top", "right"):
                ax.spines[rand].set_visible(False)
            fig.tight_layout()
            datei = ziel("ff2", f"a12_genauigkeit_{name}_{kurz}.pdf")
            fig.savefig(datei, format="pdf")
            plt.close(fig)
            print("geschrieben:", datei)



def genauigkeit_vier():
    """Genauigkeit je Operation als 2x2-Gitter in einer Datei.

    Oben MAE, unten RMSE, links Berlin, rechts Hamburg. Eine gemeinsame
    Legende unter allen vier Feldern, damit sie nicht viermal erscheint.
    Je Fehlermass dieselbe Ordinatenskala, damit die beiden Regionen
    vergleichbar sind.
    """
    import csv
    df = hole("""
      SELECT r.run_id, r.crs_strategy, r.workflow, a.mae, a.rmse
      FROM runs r JOIN accuracy a ON a.run_id = r.run_id
      WHERE r.extent_size='medium' AND r.archived=false AND r.status='success'
        AND r.dataset='dem' AND r.resolution_m=10
        AND r.crs_strategy <> 'local_reference'
        AND r.backend_url NOT LIKE '%terrascope%'
      ORDER BY r.run_id
    """)
    zu = {}
    pfad = Path("blockzuordnung.csv")
    if pfad.exists():
        for z in csv.DictReader(open(pfad)):
            zu[int(z["run_id"])] = z["block"]
    df["block"] = df.run_id.map(lambda i: zu.get(i, ""))
    df = df[df.block.isin(["A1", "A2"])].drop_duplicates(
        ["block", "workflow", "crs_strategy"])
    if df.empty:
        print("keine Daten"); return

    x = np.arange(len(OPERATIONEN))
    fig, achsen = plt.subplots(2, 2, figsize=(6.3, 5.4), sharex=True)

    for reihe, (spalte, name) in enumerate((("mae", "MAE"), ("rmse", "RMSE"))):
        alle = df[spalte].dropna()
        ylim = (alle.min() / 3, alle.max() * 3)
        for sp, (block, region) in enumerate((("A1", "Berlin"),
                                              ("A2", "Hamburg"))):
            ax = achsen[reihe][sp]
            teil = df[df.block == block]
            reihen = {}
            for strategie in ("local_preprocessing", "onthefly"):
                werte = []
                for op in OPERATIONEN:
                    tr = teil[(teil.workflow == op)
                              & (teil.crs_strategy == strategie)][spalte]
                    werte.append(float(tr.iloc[0]) if len(tr) else np.nan)
                reihen[strategie] = werte
            for i in range(len(OPERATIONEN)):
                u, o = reihen["local_preprocessing"][i], reihen["onthefly"][i]
                if np.isfinite(u) and np.isfinite(o):
                    ax.plot([i, i], [u, o], color="#c0c0c0", linewidth=0.9,
                            zorder=1)
            for strategie, werte in reihen.items():
                ax.plot(x, werte, linestyle="none", markersize=5,
                        markeredgecolor="white", markeredgewidth=0.5,
                        zorder=3, **STIL[strategie])
            ax.set_yscale("log")
            ax.set_ylim(*ylim)
            ax.set_xlim(-0.6, len(OPERATIONEN) - 0.4)
            ax.axvline(3.5, color="#bbbbbb", linewidth=0.8, linestyle=(0, (4, 3)))
            ax.grid(axis="y", which="major", linewidth=0.4, color="#d5d5d5")
            ax.set_axisbelow(True)
            for rand in ("top", "right"):
                ax.spines[rand].set_visible(False)
            if reihe == 0:
                ax.set_title(region, fontsize=10)
            if sp == 0:
                ax.set_ylabel(name)
            else:
                ax.set_yticklabels([])

    for ax in achsen[1]:
        ax.set_xticks(x)
        ax.set_xticklabels(OPERATIONEN, rotation=45, ha="right", fontsize=8)

    griffe, namen = achsen[0][0].get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=2, frameon=False,
               bbox_to_anchor=(0.5, -0.01))
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    datei = ziel("ff2", "a1a2_genauigkeit_vier.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)


def grenzfall_fullpp():
    """Laufzeit der drei Strategien im Vergleich, small bis large.

    full_pp transformiert alle Eingangsdaten lokal, also sechzehn
    Sentinel-2-Aufnahmen und das Hoehenmodell, und stellt sie über load_stac
    bereit. Das Backend transformiert dann nichts mehr, fuehrt aber die
    Operation weiterhin aus.

    Einschraenkung: full_pp gibt netCDF aus, weil mit GeoTIFF alle neun Läufe
    an einer beschaedigten Zwischendatei scheiterten. local_pp und onthefly
    geben GeoTIFF aus. Der Vergleich der Gesamtdauer enthaelt damit einen
    Formatunterschied. Vergleichbar ist die Vorverarbeitung, sie laeuft in
    allen Faellen lokal und unabhaengig vom Ausgabeformat.

    Der Bezug der Eingangsdaten vom Backend steht ausserhalb von total_time und
    ist mitgerechnet, bei full_pp Sentinel-2 und Hoehenmodell, bei local_pp nur
    das Hoehenmodell, bei onthefly gar nicht.
    """
    df = hole("""
      SELECT run_id, crs_strategy, extent_size, save_format,
             COALESCE(queue_time, 0) AS queue_time,
             COALESCE(processing_time, 0) AS processing_time,
             COALESCE(download_time, 0) AS download_time,
             COALESCE(preprocessing_time, 0) AS preprocessing_time,
             COALESCE(dem_download_time, 0) AS dem_download_time,
             COALESCE(s2_download_time, 0) AS s2_download_time,
             COALESCE(job_execution_time, 0) AS job_execution_time,
             total_time
      FROM runs
      WHERE archived=false AND status='success' AND dataset='dem'
        AND resolution_m=10 AND workflow='merge_add'
        AND backend_url NOT LIKE '%terrascope%'
        AND extent_size IN ('small','medium','large')
        AND (dem_layout='cog' OR dem_layout IS NULL)
        AND crs_strategy IN ('full_preprocessing','local_preprocessing','onthefly')
    """)
    if df.empty:
        print("keine Daten"); return

    df["bezug"] = df.dem_download_time + df.s2_download_time
    df["annahme"] = (df.job_execution_time - df.queue_time
                     - df.processing_time).clip(lower=0)
    df["sonstiges"] = (df.total_time - df.job_execution_time
                       - df.download_time - df.preprocessing_time).clip(lower=0)

    ANTEILE = ZEITANTEILE
    KURZ = {"onthefly": "onthefly", "local_preprocessing": "local_pp",
            "full_preprocessing": "full_pp"}
    strategien = ["onthefly", "local_preprocessing", "full_preprocessing"]
    stufen = ["small", "medium", "large"]

    m = df.groupby(["extent_size", "crs_strategy"]).mean(numeric_only=True)
    anzahl = df.groupby(["extent_size", "crs_strategy"]).size()

    zeilen = [(s, st) for s in stufen for st in strategien]
    y = np.arange(len(zeilen))
    links = np.zeros(len(zeilen))

    fig, ax = plt.subplots(figsize=(6.3, 4.4))
    for feld, name, farbe in ANTEILE:
        werte = np.array([float(m.loc[z, feld]) if z in m.index else 0.0
                          for z in zeilen])
        dunkel = farbe in ("#7f3b08", "#1f4e79")
        ax.barh(y, werte, 0.62, left=links, color=farbe, label=name,
                edgecolor="white", linewidth=0.6)
        for k, w in enumerate(werte):
            if w > 30:
                ax.annotate(f"{w:.0f}", (links[k] + w / 2, y[k]),
                            ha="center", va="center", fontsize=6.5,
                            color="white" if dunkel else "#333333")
        links += werte

    for k, z in enumerate(zeilen):
        ax.annotate(f"{links[k]:.0f} s", (links[k], y[k]),
                    textcoords="offset points", xytext=(5, 0),
                    va="center", fontsize=7.5, color="#333333")

    ax.set_yticks(y)
    ax.set_yticklabels([f"{s}, {KURZ[st]}" for s, st in zeilen], fontsize=8)
    ax.invert_yaxis()
    # Trennlinien zwischen den Gebietsstufen
    for grenze in (2.5, 5.5):
        ax.axhline(grenze, color="#cccccc", linewidth=0.8)

    ax.set_xlim(0, links.max() * 1.14)
    ax.set_xlabel("Zeit in Sekunden, Mittel \u00fcber die L\u00e4ufe")
    ax.grid(axis="x", linewidth=0.4, color="#d5d5d5")
    ax.set_axisbelow(True)
    for rand in ("top", "right", "left"):
        ax.spines[rand].set_visible(False)

    griffe, namen = ax.get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=4, frameon=False,
               bbox_to_anchor=(0.5, -0.14), fontsize=7.5)
    fig.tight_layout(rect=(0.09, 0.11, 1, 1))
    datei = ziel("ff3", "g_grenzfall_fullpp.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)

    felder = [f for f, _, _ in ANTEILE]
    k = m[felder].round(1)
    k["summe"] = k.sum(axis=1).round(1)
    k["soll"] = (m.total_time + m.bezug).round(1)
    print(k.to_string())



def aufschlag():
    """Mengen- und Laufzeitaufschlag von local_pp gegenüber onthefly.

    Links der Anteil, um den das Backend bei local_pp mehr Eingangsmenge
    abrechnet, rechts der Anteil, um den die Gesamtdauer laenger ausfaellt.
    Beide beziehen sich auf die Skalierungsreihe bei merge_add.

    Der Mengenaufschlag bleibt über die Stufen konstant, die Laufzeitdifferenz
    tritt erst bei large auf. Der größere Ausschnitt erklaert die Differenz
    damit nur zum Teil.
    """
    df = hole("""
      SELECT extent_size, crs_strategy,
             median(input_pixels_mp) AS mp,
             median(total_time + COALESCE(dem_download_time, 0)) AS zeit
      FROM runs WHERE archived=false AND status='success' AND workflow='merge_add'
        AND dataset='dem' AND resolution_m=10 AND backend_url NOT LIKE '%terrascope%'
        AND (dem_layout='cog' OR dem_layout IS NULL) AND run_id BETWEEN 1040 AND 1100
        AND crs_strategy IN ('local_preprocessing','onthefly')
      GROUP BY 1,2
    """)
    stufen = [s for s in ["small","medium","large"]
              if len(df[df.extent_size == s]) == 2]
    x = np.arange(len(stufen))
    mengen, zeiten = [], []
    for s in stufen:
        l = df[(df.extent_size == s) & (df.crs_strategy == "local_preprocessing")]
        o = df[(df.extent_size == s) & (df.crs_strategy == "onthefly")]
        mengen.append((float(l.mp.iloc[0]) / float(o.mp.iloc[0]) - 1) * 100)
        zeiten.append((float(l.zeit.iloc[0]) / float(o.zeit.iloc[0]) - 1) * 100)

    fig, ax = plt.subplots(figsize=(4.6, 2.6))
    breite = 0.36
    ax.bar(x - breite/2, mengen, breite, color="#1f4e79",
           label="abgerechnete Eingangsmenge", edgecolor="white", linewidth=0.5)
    ax.bar(x + breite/2, zeiten, breite, color="#e08214",
           label="Gesamtdauer", edgecolor="white", linewidth=0.5)
    for xi, w in zip(x - breite/2, mengen):
        ax.annotate(f"{w:+.0f}", (xi, w), textcoords="offset points",
                    xytext=(0, 3 if w >= 0 else -11), ha="center", fontsize=7,
                    color="#444444")
    for xi, w in zip(x + breite/2, zeiten):
        ax.annotate(f"{w:+.0f}", (xi, w), textcoords="offset points",
                    xytext=(0, 3 if w >= 0 else -11), ha="center", fontsize=7,
                    color="#444444")
    ax.axhline(0, color="#444444", linewidth=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(stufen)
    ax.set_xlim(-0.6, len(stufen) - 0.4)
    ax.set_ylabel("Aufschlag von local_pp\ngegen\u00fcber onthefly in Prozent")
    ax.grid(axis="y", linewidth=0.4, color="#d5d5d5")
    ax.set_axisbelow(True)
    for rand in ("top", "right"):
        ax.spines[rand].set_visible(False)
    griffe, namen = ax.get_legend_handles_labels()
    fig.legend(griffe, namen, loc="lower center", ncol=2, frameon=False,
               bbox_to_anchor=(0.5, -0.02), fontsize=7.5)
    fig.tight_layout(rect=(0, 0.09, 1, 1))
    datei = ziel("ff1", "c_aufschlag.pdf")
    fig.savefig(datei, format="pdf")
    plt.close(fig)
    print("geschrieben:", datei)
    for s, m, z in zip(stufen, mengen, zeiten):
        print(f"{s:8s} Menge {m:+6.1f} %   Zeit {z:+6.1f} %")


ABBILDUNGEN = {"genauigkeit": genauigkeit_operationen,
               "aufschlag": aufschlag,
               "genauigkeit_vier": genauigkeit_vier,
               "genauigkeit_einzeln": genauigkeit_einzeln,
               "gitter": gitterversatz,
               "tradeoff": tradeoff,
               "skal_backends": skalierung_backends,
               "tage": tagesabhaengigkeit,
               "verteilung": fehlerverteilung,
               "zugriff_formate": zugriff_formate,
               "credits": credits_aufschluesselung,
               "backends": backendvergleich,
               "grenzfall": grenzfall_fullpp,
               "skalierung": skalierung,
               "zeit": zeitanteile,
               "zugriff": datenzugriff_operationen,
               "streuung": laufzeitstreuung,
               "zeit_ops": zeitanteile_operationen,
               "regionen": genauigkeit_regionen,
               "fehlerstruktur": fehlerstruktur_regionen,
               "faktor": genauigkeitsfaktor}


def main() -> int:
    if not DB.exists():
        print("Datenbank nicht gefunden:", DB)
        return 1
    wahl = sys.argv[1:] or list(ABBILDUNGEN)
    for name in wahl:
        if name not in ABBILDUNGEN:
            print("unbekannt:", name, "| verfuegbar:", ", ".join(ABBILDUNGEN))
            return 1
        ABBILDUNGEN[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
