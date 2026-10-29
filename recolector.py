#!/usr/bin/env python3
"""
Monitor de agenda mediática · recolector diario
------------------------------------------------
Descarga los RSS de los medios definidos en medios.json, clasifica cada
titular por temas y actores con diccionario.json y deja en ./out los
documentos JSON listos para cargar en la base de datos del dashboard.

Solo usa la biblioteca estándar de Python.

Uso:  python3 recolector.py [--fecha AAAA-MM-DD] [--horas 36] [--dir .]

Salida (./out):
  titulares/<fecha>__<medio>.json   titulares clasificados de un medio y día
  resumen/<fecha>.json              recuentos por medio, tema y actor
  estado/ultima.json                resultado de la recogida por medio
  writes.json                       lista de escrituras para ArtifactData batch
"""
import argparse, datetime as dt, email.utils, gzip, html, json, os, re, sys
import unicodedata, urllib.parse, urllib.request, xml.etree.ElementTree as ET

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Madrid")
except Exception:  # sin tzdata: aproximación
    TZ = dt.timezone(dt.timedelta(hours=1))

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 MonitorAgendaMediatica/1.0")
MAX_ITEMS = 150          # por medio y día
LEN_DESC = 220           # caracteres de entradilla guardados y usados para clasificar
POLITICO = "Política y partidos"   # se añade si el titular menciona un actor político
NO_POLITICOS = {"Donald Trump", "Unión Europea", "Tribunales (TS / TC)"}


# ---------------------------------------------------------------- texto
def sin_tildes(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")

def limpiar(s):
    if not s:
        return ""
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()

def compilar(terminos):
    """Devuelve lista de (regex, sensible_a_mayusculas)."""
    out = []
    for t in terminos:
        sens = t.startswith("=")
        t = t[1:] if sens else t
        pref = t.endswith("*")
        t = t.rstrip("*")
        t = sin_tildes(t) if sens else sin_tildes(t).lower()
        pat = r"(?<!\w)" + re.escape(t) + (r"\w*" if pref else r"(?!\w)")
        out.append((re.compile(pat), sens))
    return out

def coincide(reglas, texto_min, texto_may):
    for rx, sens in reglas:
        if rx.search(texto_may if sens else texto_min):
            return True
    return False


# ---------------------------------------------------------------- red / RSS
import http.cookiejar
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

def descargar(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip"})
    with _OPENER.open(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data

def fecha_rss(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except Exception:
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return d

def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()

def parsear(xml_bytes):
    """Lista de dicts {t, d, u, f} desde RSS 2.0, RSS 1.0 o Atom."""
    items = []
    try:
        root = ET.fromstring(xml_bytes)
        for el in root.iter():
            if _local(el.tag) not in ("item", "entry"):
                continue
            it = {"t": "", "d": "", "u": "", "f": None}
            for ch in el:
                n = _local(ch.tag)
                if n == "title":
                    it["t"] = limpiar("".join(ch.itertext()))
                elif n in ("description", "summary") and not it["d"]:
                    it["d"] = limpiar("".join(ch.itertext()))
                elif n == "link":
                    it["u"] = (ch.get("href") or (ch.text or "")).strip() or it["u"]
                elif n in ("pubdate", "date", "published", "updated") and not it["f"]:
                    it["f"] = fecha_rss(ch.text)
                elif n == "guid" and not it["u"] and (ch.text or "").startswith("http"):
                    it["u"] = ch.text.strip()
            if it["t"]:
                items.append(it)
        # Sitemaps de Google News (<url><loc/><news:news><news:title/>...)
        for el in root.iter():
            if _local(el.tag) != "url":
                continue
            it = {"t": "", "d": "", "u": "", "f": None}
            for ch in el.iter():
                n = _local(ch.tag)
                if n == "loc" and not it["u"]:
                    it["u"] = (ch.text or "").strip()
                elif n == "title":
                    it["t"] = limpiar(ch.text)
                elif n == "publication_date":
                    it["f"] = fecha_rss(ch.text)
            if it["t"]:
                items.append(it)
        return items
    except ET.ParseError:
        pass
    # Respaldo con expresiones regulares para XML mal formado
    txt = xml_bytes.decode("utf-8", "replace")
    for bloque in re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", txt, flags=re.S | re.I):
        g = lambda tag: (re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", bloque, flags=re.S | re.I) or [None, ""])[1]
        link = g("link") or (re.search(r'<link[^>]*href="([^"]+)"', bloque) or [None, ""])[1]
        it = {"t": limpiar(g("title")), "d": limpiar(g("description") or g("summary")),
              "u": limpiar(link), "f": fecha_rss(limpiar(g("pubDate") or g("dc:date") or g("published")))}
        if it["t"]:
            items.append(it)
    return items


def parsear_html(data, patron, base):
    """Titulares de una portada HTML: enlaces cuyo href casa con `patron`."""
    try:
        s = data.decode("utf-8")
    except UnicodeDecodeError:
        s = data.decode("cp1252", "replace")
    items, vistos = [], set()
    for href, inner in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', s, flags=re.S):
        if not re.search(patron, href):
            continue
        t = limpiar(inner)
        if len(t) < 25 or href in vistos:
            continue
        vistos.add(href)
        u = urllib.parse.urljoin(base, href)
        f = None
        m = re.search(r"/(20\d{2})(\d{2})(\d{2})/", href)
        if m:
            try: f = dt.datetime(int(m[1]), int(m[2]), int(m[3]), 12, tzinfo=TZ)
            except ValueError: f = None
        items.append({"t": t, "d": "", "u": u, "f": f})
    return items



# ---------------------------------------------------------------- bloques y palabras
STOP = set("""a al algo algun alguna algunas alguno algunos ante antes aqui asi aun aunque bajo bien cada casi como con contra cual cuales cuando de del desde donde dos durante e el ella ellas ellos en entre era es esa esas ese eso esos esta estan estas este esto estos fue fueron ha hace hacen hacer hasta hay he la las le les lo los mas me mi mientras muy nada ni no nos nuestra nuestro o os otra otras otro otros para pero poco por porque pues que quien se sea segun ser si sido sin sobre su sus tambien tan tanto te tiene tienen todo todos tras tu un una uno unos unas y ya yo
años ano anos dia dias hoy ayer manana semana mes vez veces tras sera seran esta este estos nuevo nueva nuevos nuevas primer primera gran grandes mejor mejores peor
directo ultima ultimo hora horas minuto video videos fotos foto imagenes asi claves sabe sabemos quiere puede pueden dice dicen tras toda todas despues ahora mismo misma solo tres cuatro cinco seis siete ocho nueve diez mil millones euros
enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre lunes martes miercoles jueves viernes sabado domingo pide piden acusa exige deja abre vuelve llega hace san
eta da du dute dira zen ere bat ez ditu dio izan egin dago daude baina edo beste hori hau hura bere gaur atzo bihar ere bi hiru lau bost behar dute dela den duela zuen""".split())

def tokens(texto):
    out = []
    for w in re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{3,}", texto or ""):
        k = sin_tildes(w).lower()
        if k in STOP or len(k) < 3:
            continue
        out.append((k, w.lower()))
    return out

def bloques_de(acts, bloques):
    return [b for b, miembros in bloques.items() if any(a in miembros for a in acts)]

class Palabras:
    """Acumula frecuencias de palabras por ámbito y tema (y por bloque político, clave '@bloque')."""
    def __init__(self):
        self.c = {}      # ambito -> clave -> palabra -> n
        self.forma = {}  # palabra normalizada -> {forma: n}
    def add(self, ambito, titulo, temas, bloques_item):
        tk = tokens(titulo)
        if not tk:
            return
        for k, f in tk:
            self.forma.setdefault(k, {}).setdefault(f, 0)
            self.forma[k][f] += 1
        for clave in list(temas) + ["@" + b for b in bloques_item]:
            d = self.c.setdefault(ambito, {}).setdefault(clave, {})
            for k, _ in set(tk):
                d[k] = d.get(k, 0) + 1
    def doc(self, fecha, top=40, origen=None):
        def forma(k):
            fs = self.forma.get(k, {k: 1})
            con_tilde = {f: n for f, n in fs.items() if f != sin_tildes(f)}
            return max(con_tilde or fs, key=lambda f: (con_tilde or fs)[f])
        res = {"fecha": fecha, "ambitos": {}}
        if origen:
            res["origen"] = origen
        for amb, claves in self.c.items():
            res["ambitos"][amb] = {cl: {forma(k): n for k, n in sorted(d.items(), key=lambda x: -x[1])[:top] if n >= 2}
                                   for cl, d in claves.items()}
        return res

# ---------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fecha", help="fecha a la que se asigna la recogida (por defecto hoy, Europe/Madrid)")
    ap.add_argument("--horas", type=int, default=36, help="antigüedad máxima de los titulares")
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)))
    a = ap.parse_args()

    ahora = dt.datetime.now(TZ)
    fecha = a.fecha or ahora.date().isoformat()
    limite = ahora - dt.timedelta(hours=a.horas)

    medios = json.load(open(os.path.join(a.dir, "medios.json"), encoding="utf-8"))["medios"]
    dic = json.load(open(os.path.join(a.dir, "diccionario.json"), encoding="utf-8"))
    temas = {k: compilar(v) for k, v in dic["temas"].items()}
    actores = {k: compilar(v) for k, v in dic["actores"].items()}
    bloques = dic.get("bloques", {})
    pal = Palabras()

    out = os.path.join(a.dir, "out")
    for sub in ("titulares", "resumen", "estado", "palabras"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)

    resumen = {"fecha": fecha, "actualizado": ahora.isoformat(timespec="seconds"), "medios": {}}
    estado = {"ejecutado": ahora.isoformat(timespec="seconds"), "fecha": fecha, "horas": a.horas, "medios": {}}
    writes = []

    for m in medios:
        vistos, items, usados, errores = set(), [], [], []
        for grupo in m["feeds"]:
            for url in [u.strip() for u in grupo.split("|") if u.strip()]:
                try:
                    if url.startswith("html:"):
                        url = url[5:]
                        brutos = parsear_html(descargar(url), m.get("patron_enlace", "."), url)
                    else:
                        brutos = parsear(descargar(url))
                    if not brutos:
                        raise ValueError("feed sin titulares")
                    nuevos = 0
                    for it in brutos:
                        clave = it["u"] or it["t"]
                        if clave in vistos:
                            continue
                        if it["f"] is not None and it["f"] < limite:
                            continue
                        vistos.add(clave)
                        items.append(it)
                        nuevos += 1
                    if not nuevos:
                        raise ValueError(f"feed sin titulares de las últimas {a.horas} h (posible feed abandonado)")
                    usados.append(url)
                    break
                except Exception as e:
                    errores.append(f"{url}: {type(e).__name__}: {str(e)[:120]}")
        items = items[:MAX_ITEMS]

        cnt_t, cnt_a, cnt_b, filas = {}, {}, {}, []
        for it in items:
            base = sin_tildes(f"{it['t']} . {it['d'][:LEN_DESC]}")
            mn = base.lower()
            ac = [k for k, r in actores.items() if coincide(r, mn, base)]
            ts = [k for k, r in temas.items() if coincide(r, mn, base)]
            if POLITICO in temas and POLITICO not in ts and any(x not in NO_POLITICOS for x in ac):
                ts.append(POLITICO)
            ts = ts or ["Otros"]
            for k in ts: cnt_t[k] = cnt_t.get(k, 0) + 1
            for k in ac: cnt_a[k] = cnt_a.get(k, 0) + 1
            bl = bloques_de(ac, bloques)
            for k in bl: cnt_b[k] = cnt_b.get(k, 0) + 1
            pal.add(m["ambito"], it["t"], ts, bl)
            filas.append({"t": it["t"][:300], "d": it["d"][:LEN_DESC], "u": it["u"][:500],
                          "h": it["f"].astimezone(TZ).strftime("%Y-%m-%d %H:%M") if it["f"] else "",
                          "temas": ts, "actores": ac})

        ok = bool(usados)
        estado["medios"][m["id"]] = {"nombre": m["nombre"], "ambito": m["ambito"], "ok": ok,
                                     "n": len(filas), "feeds": usados, "errores": errores[:4]}
        if not ok:
            print(f"✗ {m['nombre']}: sin datos", file=sys.stderr)
            continue
        resumen["medios"][m["id"]] = {"nombre": m["nombre"], "ambito": m["ambito"], "n": len(filas),
                                      "temas": cnt_t, "actores": cnt_a, "bloques": cnt_b}
        doc_id = f"{fecha}__{m['id']}"
        doc = {"fecha": fecha, "medio": m["id"], "nombre": m["nombre"], "ambito": m["ambito"],
               "idioma": m.get("idioma", "es"), "n": len(filas), "items": filas}
        p = os.path.join(out, "titulares", doc_id + ".json")
        json.dump(doc, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        writes.append({"op": "set", "collection": "titulares", "doc_id": doc_id, "file_path": os.path.abspath(p)})
        print(f"✓ {m['nombre']}: {len(filas)} titulares")

    p = os.path.join(out, "resumen", fecha + ".json")
    json.dump(resumen, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    if resumen["medios"]:
        writes.append({"op": "set", "collection": "resumen", "doc_id": fecha, "file_path": os.path.abspath(p)})
    p = os.path.join(out, "palabras", fecha + ".json")
    json.dump(pal.doc(fecha), open(p, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    if resumen["medios"]:
        writes.append({"op": "set", "collection": "palabras", "doc_id": fecha, "file_path": os.path.abspath(p)})
    p = os.path.join(out, "estado", "ultima.json")
    json.dump(estado, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    writes.append({"op": "set", "collection": "estado", "doc_id": "ultima", "file_path": os.path.abspath(p)})

    json.dump(writes, open(os.path.join(out, "writes.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    okn = sum(1 for v in estado["medios"].values() if v["ok"])
    print(f"\n{okn}/{len(medios)} medios con datos · {sum(v['n'] for v in estado['medios'].values())} titulares · fecha {fecha}")
    print(f"Escrituras preparadas: {os.path.join(out, 'writes.json')}")

if __name__ == "__main__":
    main()
