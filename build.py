"""Genera site/index.html a partir de partidos.csv, modelos.csv y los CSV de EPA."""
import os, io, re, json, base64, difflib, unicodedata, warnings
import requests, pandas as pd
from PIL import Image
warnings.filterwarnings("ignore")

TZ = "America/Mexico_City"   # zona horaria en la que capturas la columna 'hora'
LOGOS, SITE = "logos", "site"
URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/teams"
os.makedirs(LOGOS, exist_ok=True)
os.makedirs(SITE, exist_ok=True)

# Nombres de tu hoja que ESPN escribe distinto:  alias -> nombre de ESPN
OVERRIDES = {
    "Miami (Ohio)": "Miami (OH)", "Louisiana-Lafayette": "Louisiana",
    "Florida Intl.": "Florida International", "Central Florida": "UCF",
    "Massachusetts": "UMass", "Appalachian St.": "App State",
    "Sam Houston St.": "Sam Houston",
    "Connecticut": "UConn", "West Va.": "West Virginia",
    "Kent": "Kent State", "Mississippi": "Ole Miss",
    "Southern Miss.": "Southern Miss", "Texas-San Antonio": "UTSA",
    "Troy St.": "Troy", "Western Mich.": "Western Michigan",
    "Missouri St.": "Missouri State",
    "Louisiana-Monroe": "UL Monroe", "Miami (Fla.)": "Miami",
    "Middle Tenn.": "Middle Tennessee",
}
ABR = [("st.", "state"), ("mich.", "michigan"), ("intl.", "international"), ("ill.", "illinois")]
# Equipos que el directorio de ESPN suele omitir: se piden por ID
EXTRA_IDS = {"Tennessee": "2633", "Purdue": "2509", "Toledo": "2649", "Tulane": "2655",
             "Wyoming": "2751", "UTEP": "2638", "South Carolina": "2579", "Kent State": "2309",
             "Ole Miss": "145", "Missouri State": "2623", "Sam Houston": "2534",
             "Southern Miss": "2572", "UTSA": "2636", "Troy": "2653", "Western Michigan": "2711",
             "SMU": "2567", "TCU": "2628", "Texas Tech": "2641", "UL Monroe": "2433",
             "Miami": "2390", "Middle Tennessee": "2393"}
COLS = ["alias", "nombre_completo", "abreviatura", "espn_id", "color"]

resumen, avisos = [], []

def aviso(titulo, msg):
    print(f"::warning title={titulo}::{msg}")
    avisos.append(f"- **{titulo}:** {msg}")

limpia = lambda s: re.sub(r"\s+", "", str(s).strip().lower())

def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    for a, b in ABR:
        s = re.sub(r"\b" + re.escape(a) + r"(?=\s|$)", b, s)
    return re.sub(r"[^a-z0-9]", "", s)

def leer_csv(ruta, **kw):
    for enc in ("utf-8-sig", "latin-1"):
        for sep in (None, ","):
            try:
                return pd.read_csv(ruta, sep=sep, engine="python", encoding=enc, **kw)
            except UnicodeDecodeError:
                break
            except Exception:
                continue
    raise SystemExit(f"No pude leer {ruta}")

# ---------- 1) Partidos y modelos ----------
if not os.path.exists("partidos.csv"):
    raise SystemExit("Falta partidos.csv en el repositorio")
p = leer_csv("partidos.csv")
p.columns = [limpia(c) for c in p.columns]
for c in ("visitante", "local", "linea_apertura", "linea_actual"):
    if c not in p.columns:
        raise SystemExit(f"Falta la columna '{c}' en partidos.csv. Columnas encontradas: {list(p.columns)}")

if os.path.exists("modelos.csv"):
    mod = leer_csv("modelos.csv")
    mod.columns = [limpia(c) for c in mod.columns]
else:
    mod = pd.DataFrame(columns=["columna", "nombre"])
    aviso("modelos.csv", "no existe; se usarán los nombres técnicos de las columnas")

fijas = ["visitante", "local", "linea_apertura", "linea_actual", "fecha", "hora"]
cols_mod = [c for c in p.columns if c not in fijas]
for c in ["linea_apertura", "linea_actual"] + cols_mod:
    p[c] = pd.to_numeric(p[c], errors="coerce")
vacias = [c for c in cols_mod if p[c].isna().all()]
if vacias:
    print("Columnas sin datos (se omiten):", vacias)
    resumen.append(f"Columnas de modelo sin datos (omitidas): {', '.join(vacias)}")
cols_mod = [c for c in cols_mod if c not in vacias]
nombres = dict(zip(mod["columna"].map(limpia), mod["nombre"].astype(str).str.strip())) if len(mod) else {}
models = [nombres.get(c, c) for c in cols_mod]
sin_nombre = [c for c in cols_mod if c not in nombres]
if sin_nombre:
    aviso("Modelos sin nombre", f"estas columnas no están en modelos.csv: {', '.join(sin_nombre)}")
p = p.dropna(subset=["linea_actual"])
if p.empty:
    raise SystemExit("partidos.csv no tiene filas con linea_actual")

# ---------- 2) Fechas ----------
HOY = pd.Timestamp.now(tz=TZ).tz_localize(None).normalize()
fechas_corregidas = set()

def f_fecha(v):
    """Lee la fecha. Si día y mes parecen invertidos, elige la lectura más cercana a hoy."""
    if v is None or pd.isna(v) or str(v).strip() == "": return ""
    s = str(v).strip()
    d = pd.to_datetime(s, errors="coerce")
    if pd.isna(d): return ""
    if d.day <= 12 and d.day != d.month:
        alt = pd.Timestamp(year=d.year, month=d.day, day=d.month)
        if abs((alt - HOY).days) < abs((d - HOY).days):
            fechas_corregidas.add((s, alt.strftime("%Y-%m-%d")))
            d = alt
    return d.strftime("%Y-%m-%d")

def f_hora(v):
    if v is None or pd.isna(v) or str(v).strip() == "": return ""
    h = pd.to_datetime(str(v).strip(), errors="coerce")
    return "" if pd.isna(h) else h.strftime("%H:%M")

def ts_ms(fecha, hora):
    if not fecha: return None
    t = pd.Timestamp(f"{fecha} {hora or '23:59'}").tz_localize(TZ, ambiguous=True, nonexistent="shift_forward")
    return int(t.timestamp() * 1000)

games = [{"road": str(r["visitante"]).strip(), "home": str(r["local"]).strip(),
          "open": None if pd.isna(r["linea_apertura"]) else float(r["linea_apertura"]),
          "line": float(r["linea_actual"]),
          "date": f_fecha(r.get("fecha")), "time": f_hora(r.get("hora")),
          "ts": ts_ms(f_fecha(r.get("fecha")), f_hora(r.get("hora"))),
          "m": [None if pd.isna(r[c]) else float(r[c]) for c in cols_mod]} for _, r in p.iterrows()]

sin_fecha = sum(1 for g in games if not g["date"])
sin_hora = sum(1 for g in games if not g["time"])
if sin_fecha or sin_hora:
    aviso("Fechas", f"juegos sin fecha legible: {sin_fecha}; sin hora legible: {sin_hora}")
if fechas_corregidas:
    resumen.append("Fechas con día y mes invertidos que se corrigieron: " +
                   "; ".join(f"{a} → {b}" for a, b in sorted(fechas_corregidas)))

# ---------- 3) Catálogo de equipos y logos ----------
aliases = sorted({g["road"] for g in games} | {g["home"] for g in games})
eq = leer_csv("equipos.csv", dtype=str).fillna("") if os.path.exists("equipos.csv") else pd.DataFrame(columns=COLS)
eq.columns = [limpia(c) for c in eq.columns]
filas = {str(r["alias"]).strip(): {c: str(r.get(c, "")) for c in COLS}
         for _, r in eq.iterrows() if str(r.get("espn_id", "")).strip()}

manual = {}
if os.path.exists("equipos_manual.csv"):
    try:
        m = leer_csv("equipos_manual.csv", dtype=str).fillna("")
        m.columns = [limpia(c) for c in m.columns]
        manual = {str(a).strip(): str(i).strip() for a, i in zip(m["alias"], m["espn_id"]) if str(a).strip() and str(i).strip()}
    except Exception as e:
        aviso("equipos_manual.csv", f"no se pudo leer ({e})")

pendientes = [a for a in aliases if a not in filas or (a in manual and filas[a]["espn_id"] != manual[a])]
teams, nuevos, sin_resolver = {}, [], []

def por_id(tid):
    try:
        r = requests.get(f"{URL}/{tid}", timeout=30); r.raise_for_status()
        return r.json()["team"]
    except Exception as e:
        print(f"  no se pudo leer el equipo {tid}: {e}")
        return None

if pendientes:
    print("Equipos por resolver:", pendientes)
    for grupo in (80, 81):
        try:
            r = requests.get(URL, params={"groups": grupo, "limit": 500}, timeout=30); r.raise_for_status()
            for t in r.json()["sports"][0]["leagues"][0]["teams"]:
                teams[str(t["team"]["id"])] = t["team"]
        except Exception as e:
            print(f"Grupo {grupo}: no se pudo leer ({e})")
    for _, tid in EXTRA_IDS.items():
        if tid not in teams:
            t = por_id(tid)
            if t: teams[str(t["id"])] = t
    idx = {}
    for campo in ("location", "displayName", "shortDisplayName"):
        for t in teams.values():
            if t.get(campo):
                idx.setdefault(norm(t[campo]), t)
    for a in pendientes:
        t = por_id(manual[a]) if a in manual else idx.get(norm(OVERRIDES.get(a, a)))
        if t:
            filas[a] = {"alias": a, "nombre_completo": t["displayName"], "abreviatura": t.get("abbreviation", ""),
                        "espn_id": str(t["id"]), "color": ("#" + t["color"]) if t.get("color") else ""}
            nuevos.append(f"{a} → {t['displayName']}")
        else:
            sin_resolver.append(a)
    locs = [t["location"] for t in teams.values() if t.get("location")]
    for a in sin_resolver:
        sug = difflib.get_close_matches(a, locs, n=3, cutoff=0.4)
        aviso("Equipo sin catálogo",
              f'"{a}" no se pudo emparejar (sugerencias: {sug}). Agrega una fila "{a},ID" en equipos_manual.csv; '
              f"el ID está en la URL de la página del equipo en ESPN")
    pd.DataFrame(list(filas.values()), columns=COLS).sort_values("alias").to_csv("equipos.csv", index=False)
    if nuevos:
        resumen.append("Equipos nuevos en el catálogo (revisa que sean los correctos): " + "; ".join(nuevos))

def bajar_logo(tid):
    ruta = f"{LOGOS}/{tid}.png"
    if os.path.exists(ruta): return True
    cands = []
    t = teams.get(str(tid))
    if t:
        h = (t.get("logos") or [{}])[0].get("href")
        if h: cands.append(h)
    cands.append(f"https://a.espncdn.com/i/teamlogos/ncaa/500/{tid}.png")
    for u in cands:
        try:
            im = Image.open(io.BytesIO(requests.get(u, timeout=30).content)).convert("RGBA")
            im.thumbnail((96, 96)); im.save(ruta, optimize=True)
            return True
        except Exception:
            continue
    return False

teams_js, sin_logo = {}, []
for a in aliases:
    r = filas.get(a)
    if not r:
        teams_js[a] = {"name": a, "abbr": "", "color": "", "logo": ""}
        continue
    ok = bajar_logo(r["espn_id"])
    logo = ""
    if ok:
        logo = "data:image/png;base64," + base64.b64encode(open(f"{LOGOS}/{r['espn_id']}.png", "rb").read()).decode()
    else:
        sin_logo.append(a)
    teams_js[a] = {"name": r["nombre_completo"] or a, "abbr": r["abreviatura"], "color": r["color"], "logo": logo}
if sin_logo:
    aviso("Sin logo", ", ".join(sin_logo))

# ---------- 4) Adj EPA/Play (tablas copiadas a mano de gameonpaper.com) ----------
TOTAL_MEJOR_ALTO = True   # en la tabla total, True si un valor más alto es mejor; cámbialo a False si es al revés

def desdoblar(nombre):
    """Limpia lo que trae el sitio al copiar: 'ESPN team id 251 TexasTexasTexas' -> ('251', 'Texas')."""
    n = str(nombre).strip()
    m = re.match(r"(?i)^ESPN team id\s*(\d+)\s*(.*)$", n)
    tid, resto = (m.group(1), m.group(2).strip()) if m else (None, n)
    r = re.fullmatch(r"(.+?)\1+", resto)
    return tid, (r.group(1).strip() if r else resto)

def leer_epa(ruta, mejor_alto):
    """Devuelve ({clave: (valor, ranking, nombre)}, total). Las claves son 'id:<ID de ESPN>' y el nombre normalizado."""
    if not os.path.exists(ruta):
        return {}, 0
    try:
        d = leer_csv(ruta)
    except SystemExit:
        aviso("EPA", f"no pude leer {ruta}")
        return {}, 0
    d.columns = [limpia(c) for c in d.columns]
    col_v = next((c for c in d.columns if c.startswith("adjepa")), None)
    if col_v is None or "team" not in d.columns:
        aviso("EPA", f"{ruta}: no encontré las columnas 'Team' y 'Adj EPA/Play'. Columnas: {list(d.columns)}")
        return {}, 0
    v = pd.to_numeric(d[col_v].astype(str).str.replace("\u2212", "-", regex=False).str.replace(",", ".", regex=False), errors="coerce")
    par = [desdoblar(x) for x in d["team"]]
    t = pd.DataFrame({"id": [x[0] for x in par], "t": [x[1] for x in par], "v": v.to_numpy()}).dropna(subset=["v"])
    t = t[t["t"] != ""].drop_duplicates("t")
    t["r"] = t["v"].rank(ascending=not mejor_alto, method="min").astype(int)
    tabla = {}
    for tid, n, x, r in zip(t["id"], t["t"], t["v"], t["r"]):
        reg = (float(x), int(r), n)
        tabla[norm(n)] = reg
        if tid:
            tabla[f"id:{tid}"] = reg
    return tabla, len(t)

TABLAS = {   # clave: (archivo, ¿más alto es mejor?)
    "t": ("epa_total.csv", TOTAL_MEJOR_ALTO),
    "o": ("epa_ofensiva.csv", True),
    "d": ("epa_defensiva.csv", False),
}
epa_tablas, epa_n = {}, {}
for k, (ruta, alto) in TABLAS.items():
    epa_tablas[k], epa_n[k] = leer_epa(ruta, alto)

def buscar_epa(tabla, alias):
    r = filas.get(alias)
    if r and r.get("espn_id") and f"id:{r['espn_id']}" in tabla:
        return tabla[f"id:{r['espn_id']}"], False          # por ID de ESPN: exacto
    for k in (norm(alias), norm(OVERRIDES.get(alias, alias))):
        if k in tabla:
            return tabla[k], False
    if r and tabla:
        nc = norm(r["nombre_completo"])
        cand = [k for k in tabla if not k.startswith("id:") and nc.startswith(k)]
        if cand:
            return tabla[max(cand, key=len)], True
    return None, False

NOMBRES_EPA = (("t", "total"), ("o", "ataque"), ("d", "defensa"))
epa_equipo = {}
if any(epa_tablas.values()):
    sin_epa, parciales = [], []
    for a in aliases:
        reg, parcial_ = {}, False
        for k, tabla in epa_tablas.items():
            r_, par_ = buscar_epa(tabla, a)
            reg[k] = r_
            parcial_ = parcial_ or (par_ and r_ is not None)
        if all(v is None for v in reg.values()):
            sin_epa.append(a)
            continue
        epa_equipo[a] = {}
        for k, r_ in reg.items():
            epa_equipo[a][k] = r_[0] if r_ else None
            epa_equipo[a]["r" + k] = r_[1] if r_ else None
        if parcial_:
            parciales.append(f"{a} → {next(r_[2] for r_ in reg.values() if r_)}")
    resumen.append("Adj EPA/Play cargado: " + ", ".join(f"{nom} {epa_n[k]} equipos" for k, nom in NOMBRES_EPA if epa_n[k]) +
                   f"; {len(epa_equipo)} de {len(aliases)} equipos de la jornada con datos")
    for k, nom in NOMBRES_EPA:
        if epa_n[k]:
            top = sorted({v[2]: v for key, v in epa_tablas[k].items() if not key.startswith("id:")}.values(), key=lambda v: v[1])[:3]
            resumen.append(f"Los 3 mejores en {nom} según el script (compáralos con el sitio): " +
                           ", ".join(f"{v[2]} ({v[0]:+.2f})" for v in top))
    faltan_t = [nom for k, nom in NOMBRES_EPA if not epa_n[k]]
    if faltan_t:
        aviso("EPA incompleto", "no se cargó la tabla de: " + ", ".join(faltan_t))
    if sin_epa:
        aviso("Sin datos EPA", ", ".join(sin_epa) + ". Los equipos FCS no suelen aparecer en el sitio; "
              "para otros, revisa que el equipo esté en las tablas copiadas")
    if parciales:
        resumen.append("Equipos de EPA emparejados por nombre parcial (revísalos): " + "; ".join(parciales))
epa_nota = " Adj EPA/Play: gameonpaper.com (captura manual)." if epa_equipo else ""

# ---------- 5) Logo de la liga ----------
logo_liga = ""
for nombre in ("logo_ncaaf.png", "logo_ncaaf.jpg", "logo_ncaaf.jpeg", "logo_ncaaf.webp"):
    if os.path.exists(nombre):
        im = Image.open(nombre).convert("RGBA")
        im.thumbnail((160, 160))
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=True)
        logo_liga = '<span class="lgl"><img src="data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode() + '" alt="NCAAF"></span>'
        break
else:
    aviso("Logo de liga", "no encontré logo_ncaaf.png; el título saldrá sin logo")

# ---------- 6) Generar la página ----------
gen = pd.Timestamp.now(tz=TZ).strftime("%d/%m/%Y %H:%M") + f" ({TZ})"
datos = {"models": models, "teams": teams_js, "games": games, "epa": epa_equipo, "total_alto": TOTAL_MEJOR_ALTO}
html = (open("template.html", encoding="utf-8").read()
        .replace("__DATA__", json.dumps(datos, ensure_ascii=False))
        .replace("__TZ__", TZ).replace("__LOGO__", logo_liga)
        .replace("__GEN__", gen).replace("__EPANOTE__", epa_nota))
with open(f"{SITE}/index.html", "w", encoding="utf-8") as f:
    f.write(html)

# ---------- 7) Resumen de la corrida ----------
r0 = p.iloc[0]
lineas = [f"## Visor actualizado: {len(games)} partidos, {len(models)} modelos",
          f"Generado: {gen} · tamaño: {len(html)/1e6:.2f} MB", "",
          f"**Primer juego:** {r0['visitante']} @ {r0['local']} (apertura {r0['linea_apertura']}, línea {r0['linea_actual']})",
          "", "**Asignación de columnas (verifica que coincida con tu hoja):**"]
lineas += [f"- `{c}` → {n}: {r0[c]}" for c, n in zip(cols_mod, models)]
lineas += ["", "**Fechas en el visor:** " + (", ".join(sorted({g['date'] for g in games if g['date']})) or "ninguna")]
if resumen:
    lineas += [""] + [f"- {x}" for x in resumen]
if avisos:
    lineas += ["", "### Avisos"] + avisos
texto = "\n".join(lineas)
print("\n" + texto)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
        f.write(texto + "\n")
