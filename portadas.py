#!/usr/bin/env python3
"""
Monitor de agenda mediática · método de PORTADAS (desde el 14-oct-2026)
----------------------------------------------------------------------
Los 15 medios se miden igual: se fotografía su portada a horas fijas (mañana,
mediodía y noche) y de cada foto se guardan las N primeras piezas en el orden
en que aparecen. El día de un medio es el conjunto de piezas distintas que
estuvieron entre sus N primeras en alguna de las fotos de ese día.

  toma: python3 portadas.py toma [--dir .]            -> <dir>/out_toma/toma.json
        (documento para la colección "tomas", id AAAA-MM-DD__HHMM)
  dia : python3 portadas.py dia --fecha D --tomas <carpeta con tomas/*.json> [--dir .]
        -> <dir>/out/ (titulares/, resumen/, palabras/, estado/, writes.json)

Solo biblioteca estándar. Necesita junto a él: recolector.py, medios.json, diccionario.json.
"""
import argparse, datetime as dt, glob, gzip, html, json, os, re, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
from recolector import (TZ, UA, _OPENER, descargar, parsear, limpiar, compilar, coincide, sin_tildes,
                        POLITICO, NO_POLITICOS, LEN_DESC, bloques_de, Palabras)

N = 30                 # piezas por foto y medio
METODO = "portadas"    # valor de 'origen' en resumen/titulares
EXCL = re.compile(r"/videos?/|/podcasts?/|/juegos/|horoscopo|/autor|/temas/|/tags?/|/especial(es)?/|/directo-?tv|stories\.rtve", re.I)

# Portada de cada medio: URL y patrón de enlace de pieza. "rss:" = la portada se lee de su RSS de portada (mismo orden).
PORTADAS = {
    "eitb":             ("https://orain.eus/es",                   r"orain\.eus/es/.+/20\d\d/\d\d/\d\d/[\w-]+"),
    "elcorreo":         ("https://www.elcorreo.com/",              r"elcorreo\.com/.+-20\d{12}-n\w*\.html"),
    "diariovasco":      ("https://www.diariovasco.com/",           r"diariovasco\.com/.+-20\d{12}-n\w*\.html"),
    "deia":             ("https://www.deia.eus/",                  r"deia\.eus/.+/20\d\d/\d\d/\d\d/[\w-]+-\d{6,}\.html"),
    "noticiasgipuzkoa": ("https://www.noticiasdegipuzkoa.eus/",    r"noticiasdegipuzkoa\.eus/.+/20\d\d/\d\d/\d\d/[\w-]+-\d{6,}\.html"),
    "berria":           ("https://www.berria.eus/",                r"berria\.eus/.+_\d{5,}_\d+\.html"),
    "naiz":             ("https://www.naiz.eus/es",                r"naiz\.eus/(es|eu)/info/(noticia|albistea)/20\d{6}/"),
    "elpais":           ("rss:https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada", r"elpais\.com/"),
    "elmundo":          ("https://www.elmundo.es/",                r"elmundo\.es/.+/20\d\d/\d\d/\d\d/[0-9a-f]{24}"),
    "abc":              ("https://www.abc.es/",                    r"abc\.es/.+-20\d{12}-n\w*\.html"),
    "lavanguardia":     ("https://www.lavanguardia.com/",          r"lavanguardia\.com/.+/20\d{6}/\d{6,}/"),
    "eldiario":         ("https://www.eldiario.es/",               r"eldiario\.es/.+_\d+_\d{6,}(_\d+)?\.html"),
    "elconfidencial":   ("https://www.elconfidencial.com/",        r"elconfidencial\.com/.+/20\d\d-\d\d-\d\d/[\w-]+_\d+/"),
    "20minutos":        ("https://www.20minutos.es/",              r"20minutos\.es/.+_\d{6,}_0\.html"),
    "rtve":             ("https://www.rtve.es/noticias/",          r"www\.rtve\.es/noticias/20\d{6}/"),
}
DESDE_MAIN = {"deia", "noticiasgipuzkoa"}
NAV = re.compile(r"^(Seguir|Síguenos|Suscríbete|Hazte socio|Encuestas elecciones)", re.I)
SUF = re.compile(r"\s*[|·–—-]\s*(elDiario\.es|Berria|20minutos|El Correo|Diario Vasco|ABC|Deia|EITB|Orain|"
                 r"La Vanguardia|EL PAÍS|El País|El Mundo|El Confidencial|Naiz|NAIZ|Noticias de Gipuzkoa|RTVE\.es|RTVE)\s*$", re.I)

def bajar_html(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*", "Accept-Encoding": "gzip",
                                               "Accept-Language": "es-ES,es;q=0.9"})
    with _OPENER.open(req, timeout=timeout) as r:
        data = r.read(); cs = r.headers.get_content_charset()
    if data[:2] == b"\x1f\x8b": data = gzip.decompress(data)
    if not cs:
        m = re.search(rb'charset=["\']?([\w-]+)', data[:5000]); cs = m.group(1).decode() if m else "utf-8"
    try: return data.decode(cs, "replace")
    except LookupError: return data.decode("utf-8", "replace")

def norm(u):
    p = urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))

def piezas_html(s, base, patron, desde_main=False):
    rx = re.compile(patron, re.I); out, vistos = [], set()
    if desde_main:                        # Deia y Noticias de Gipuzkoa abren con una tira de galería antes del contenido
        i = s.find("<main")
        if i > 0: s = s[i:]
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", s, flags=re.S | re.I):
        h = re.search(r'href=["\']([^"\']+)', m[1])
        if not h: continue
        u = norm(urllib.parse.urljoin(base, html.unescape(h[1]).strip()))
        if u in vistos or not rx.search(u) or EXCL.search(u): continue
        t = SUF.sub("", limpiar(m[2])).strip()
        if len(t) < 25 or NAV.search(t): continue          # imagen o antetítulo: se espera al enlace con el titular
        vistos.add(u); out.append({"t": t[:300], "u": u[:500]})
        if len(out) >= N: break
    return out

def piezas_rss(url, patron):
    rx = re.compile(patron, re.I); out, vistos = [], set()
    for it in parsear(descargar(url)):
        u = norm(it["u"])
        if not u or u in vistos or not rx.search(u) or EXCL.search(u) or len(it["t"]) < 10: continue
        vistos.add(u); out.append({"t": SUF.sub("", it["t"]).strip()[:300], "u": u[:500]})   # sin entradilla: se clasifica solo el titular, como en el resto
        if len(out) >= N: break
    return out

def foto(mid):
    url, patron = PORTADAS[mid]
    errs = []
    for i in range(2):
        try:
            if url.startswith("rss:"): L = piezas_rss(url[4:], patron)
            else: L = piezas_html(bajar_html(url), url, patron, mid in DESDE_MAIN)
            if len(L) < 10: raise ValueError(f"solo {len(L)} piezas reconocidas en la portada")
            return {"ok": True, "fuente": url, "n": len(L), "items": [dict(x, p=k + 1) for k, x in enumerate(L)]}
        except Exception as e:
            errs.append(f"{type(e).__name__}: {str(e)[:120]}")
    return {"ok": False, "fuente": url, "n": 0, "items": [], "errores": errs}

def cmd_toma(a):
    ahora = dt.datetime.now(TZ)
    medios = json.load(open(os.path.join(a.dir, "medios.json"), encoding="utf-8"))["medios"]
    with ThreadPoolExecutor(8) as ex: R = dict(zip([m["id"] for m in medios], ex.map(foto, [m["id"] for m in medios])))
    doc = {"fecha": ahora.date().isoformat(), "hora": ahora.strftime("%H:%M"), "ts": ahora.isoformat(timespec="seconds"),
           "n_por_foto": N, "medios": R}
    out = os.path.join(a.dir, "out_toma"); os.makedirs(out, exist_ok=True)
    p = os.path.join(out, "toma.json"); json.dump(doc, open(p, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    for m in medios:
        r = R[m["id"]]; print(("✓ " if r["ok"] else "✗ ") + f"{m['nombre']}: {r['n']}" + ("" if r["ok"] else f"  {r['errores'][-1]}"))
    doc_id = f"{doc['fecha']}__{ahora.strftime('%H%M')}"
    print(f"Toma: {p} -> tomas/{doc_id}")

def cmd_dia(a):
    D = a.fecha
    medios = json.load(open(os.path.join(a.dir, "medios.json"), encoding="utf-8"))["medios"]
    dic = json.load(open(os.path.join(a.dir, "diccionario.json"), encoding="utf-8"))
    temas = {k: compilar(v) for k, v in dic["temas"].items()}
    actores = {k: compilar(v) for k, v in dic["actores"].items()}
    bloques = dic.get("bloques", {})
    def clasificar(t, d=""):
        base = sin_tildes(f"{t} . {(d or '')[:LEN_DESC]}"); mn = base.lower()
        ac = [k for k, r in actores.items() if coincide(r, mn, base)]
        ts = [k for k, r in temas.items() if coincide(r, mn, base)]
        if POLITICO in temas and POLITICO not in ts and any(x not in NO_POLITICOS for x in ac): ts.append(POLITICO)
        return (ts or ["Otros"]), ac
    tomas = []
    for f in glob.glob(os.path.join(a.tomas, "**", "*.json"), recursive=True):
        x = json.load(open(f, encoding="utf-8"))
        if x.get("fecha") == D and "medios" in x: tomas.append(x)
    tomas.sort(key=lambda x: x["ts"])
    if not tomas: sys.exit(f"No hay tomas del {D} en {a.tomas}")
    horas = [x["hora"] for x in tomas]
    out = os.path.join(a.dir, "out")
    for sub in ("titulares", "resumen", "palabras", "estado"): os.makedirs(os.path.join(out, sub), exist_ok=True)
    ahora = dt.datetime.now(TZ); pal = Palabras(); writes = []
    resumen = {"fecha": D, "origen": METODO, "actualizado": ahora.isoformat(timespec="seconds"), "tomas": horas, "n_por_foto": N, "medios": {}}
    estado = {"ejecutado": ahora.isoformat(timespec="seconds"), "fecha": D, "metodo": "portadas", "tomas": horas, "medios": {}}
    for m in medios:
        mid = m["id"]; piezas = {}; fuentes, errs, fotos_ok = [], [], 0
        for x in tomas:
            r = x["medios"].get(mid)
            if not r: continue
            if not r.get("ok"): errs.append(f"{x['hora']}: " + "; ".join(r.get("errores", [])[-1:])); continue
            fotos_ok += 1; fuentes.append(r["fuente"])
            for it in r["items"]:
                p = piezas.get(it["u"])
                if not p: piezas[it["u"]] = p = {"t": it["t"], "d": "", "u": it["u"], "h": f"{D} {x['hora']}", "pos": it["p"], "fotos": []}
                p["pos"] = min(p["pos"], it["p"]); p["fotos"].append(x["hora"]); p["t"] = it["t"]
        filas = sorted(piezas.values(), key=lambda i: (i["h"], i["pos"]))
        for i in filas:
            i["temas"], i["actores"] = clasificar(i["t"], i["d"])
        ok = bool(filas)
        estado["medios"][mid] = {"nombre": m["nombre"], "ambito": m["ambito"], "panel": True, "ok": ok, "n": len(filas),
                                 "metodo": f"portada · {N} primeras piezas por foto · fotos {fotos_ok}/{len(tomas)}",
                                 "feeds": sorted(set(fuentes))[:2], "errores": errs[:4]}
        if not ok: print(f"✗ {m['nombre']}: sin fotos válidas", file=sys.stderr); continue
        ct, ca, cb = {}, {}, {}
        for i in filas:
            for k in i["temas"]: ct[k] = ct.get(k, 0) + 1
            for k in i["actores"]: ca[k] = ca.get(k, 0) + 1
            bl = bloques_de(i["actores"], bloques)
            for k in bl: cb[k] = cb.get(k, 0) + 1
            pal.add(m["ambito"], i["t"], i["temas"], bl)
        resumen["medios"][mid] = {"nombre": m["nombre"], "ambito": m["ambito"], "n": len(filas), "fotos": fotos_ok, "temas": ct, "actores": ca, "bloques": cb}
        doc = {"fecha": D, "medio": mid, "nombre": m["nombre"], "ambito": m["ambito"], "idioma": m.get("idioma", "es"),
               "origen": METODO, "panel": True, "tomas": horas, "n": len(filas), "items": filas}
        p = os.path.join(out, "titulares", f"{D}__{mid}.json")
        json.dump(doc, open(p, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        writes.append({"op": "set", "collection": "titulares", "doc_id": f"{D}__{mid}", "file_path": os.path.abspath(p)})
        print(f"✓ {m['nombre']}: {len(filas)} piezas ({fotos_ok} fotos)", file=sys.stderr)
    if resumen["medios"]:
        p = os.path.join(out, "palabras", D + ".json")
        json.dump(pal.doc(D, origen=METODO), open(p, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        writes.append({"op": "set", "collection": "palabras", "doc_id": D, "file_path": os.path.abspath(p)})
        p = os.path.join(out, "resumen", D + ".json")
        json.dump(resumen, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        writes.append({"op": "set", "collection": "resumen", "doc_id": D, "file_path": os.path.abspath(p)})
    p = os.path.join(out, "estado", "ultima.json")
    json.dump(estado, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    writes.append({"op": "set", "collection": "estado", "doc_id": "ultima", "file_path": os.path.abspath(p)})
    json.dump(writes, open(os.path.join(out, "writes.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    okn = sum(1 for v in estado["medios"].values() if v["ok"])
    print(f"\nDía {D} · fotos {', '.join(horas)} · {okn}/{len(medios)} medios · {sum(v['n'] for v in resumen['medios'].values())} piezas")
    print(f"Escrituras: {os.path.join(out, 'writes.json')} ({len(writes)})")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("modo", choices=["toma", "dia"])
    ap.add_argument("--fecha"); ap.add_argument("--tomas", default="tomas"); ap.add_argument("--dir", default=AQUI)
    a = ap.parse_args()
    if a.modo == "dia" and not a.fecha: a.fecha = dt.datetime.now(TZ).date().isoformat()
    (cmd_toma if a.modo == "toma" else cmd_dia)(a)
