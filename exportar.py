#!/usr/bin/env python3
"""
exportar.py · Supabase -> JSON estáticos para GitHub Pages
-----------------------------------------------------------
Lee la base de datos (DATABASE_URL) y escribe en docs/data/ lo que lee el dashboard:

  resumen.json              un documento por día: recuentos por medio, tema, actor y bloque
  estado.json               resultado de la última ejecución, por medio
  palabras/AAAA-MM-DD.json  frecuencias de palabras del día
  titulares/AAAA-MM-DD.json piezas del día (una lista con un documento por medio)
  indice.json               fechas disponibles de cada colección

Solo reescribe un archivo si su contenido ha cambiado, así Git solo ve diferencias reales.

  python exportar.py                 # todos los días
  python exportar.py --ultimos 3     # solo reescribe palabras/titulares de los 3 últimos días
                                     # (resumen.json, estado.json e indice.json siempre completos)

Los días anteriores al método de portadas (carpeta historico/archivo/) se incorporan tal cual a
resumen.json y palabras/ cuando la base de datos no tiene ese día.
"""
import argparse, collections, glob, json, os, sys
from zoneinfo import ZoneInfo

try:
    import psycopg
except ImportError:
    sys.exit("Falta psycopg. Instálalo con: pip install -r requirements.txt")

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
from recolector import bloques_de          # misma definición de bloques políticos que en portadas.py

MADRID = ZoneInfo("Europe/Madrid")


def escribir(ruta, obj):
    """Escribe JSON compacto y estable; devuelve True si el archivo cambió."""
    texto = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            if f.read() == texto:
                return False
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(texto)
    return True


def leer(ruta):
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def iso(dt):
    return dt.astimezone(MADRID).isoformat(timespec="seconds")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--salida", default=os.path.join(AQUI, "docs", "data"))
    ap.add_argument("--ultimos", type=int, default=0, help="reescribir palabras/titulares solo de los N últimos días (0 = todos)")
    ap.add_argument("--archivo", default=os.path.join(AQUI, "historico", "archivo"))
    a = ap.parse_args()

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        sys.exit("Falta la variable de entorno DATABASE_URL.")
    bloques = leer(os.path.join(AQUI, "diccionario.json")).get("bloques", {})

    with psycopg.connect(url, prepare_threshold=None, connect_timeout=30) as con, con.cursor() as cur:
        cur.execute("select id, nombre, ambito, idioma, portada_url from medios order by orden")
        medios = {r[0]: {"nombre": r[1], "ambito": r[2], "idioma": r[3], "url": r[4]} for r in cur.fetchall()}
        orden = list(medios)

        cur.execute("select fecha, medio, url, titular, pos, h, fotos, temas, actores, origen from piezas order by fecha, medio, h, pos")
        piezas = cur.fetchall()
        cur.execute("select fecha, hora, n_por_foto from tomas order by ts")
        tomas = cur.fetchall()
        cur.execute("select ejecutado, medio, fecha, metodo, ok, n, fotos_ok, fotos_total, tomas, errores from ejecuciones order by ejecutado")
        ejecs = cur.fetchall()
        cur.execute("select fecha, ambito, clave, palabra, n, origen from palabras order by fecha, ambito, clave, n desc, palabra")
        pals = cur.fetchall()

    horas, n_foto = collections.defaultdict(list), {}
    for f, h, n in tomas:
        horas[f.isoformat()].append(h); n_foto[f.isoformat()] = n

    # última ejecución de cada (día, medio) y fecha de actualización del día
    ult, actualizado = {}, {}
    for e in ejecs:
        f = e[2].isoformat(); ult[(f, e[1])] = e; actualizado[f] = max(actualizado.get(f, e[0]), e[0])

    por_dia = collections.defaultdict(lambda: collections.defaultdict(list))
    origen_dia = {}
    for f, medio, u, t, pos, h, fotos, temas, actores, origen in piezas:
        f = f.isoformat()
        por_dia[f][medio].append({"t": t, "d": "", "u": u, "h": h, "pos": pos, "fotos": fotos, "temas": temas, "actores": actores})
        origen_dia[f] = origen
    dias_bd = sorted(por_dia)

    # ---------------------------------------------------------------- resumen.json
    resumen = []
    for f in dias_bd:
        doc = {"fecha": f, "origen": origen_dia[f], "tomas": horas.get(f, []), "n_por_foto": n_foto.get(f, 30), "medios": {}}
        if f in actualizado:
            doc["actualizado"] = iso(actualizado[f])
        for mid in orden:
            items = por_dia[f].get(mid)
            if not items:
                continue
            ct, ca, cb = {}, {}, {}
            for i in items:
                for k in i["temas"]: ct[k] = ct.get(k, 0) + 1
                for k in i["actores"]: ca[k] = ca.get(k, 0) + 1
                for k in bloques_de(i["actores"], bloques): cb[k] = cb.get(k, 0) + 1
            e = ult.get((f, mid))
            doc["medios"][mid] = {"nombre": medios[mid]["nombre"], "ambito": medios[mid]["ambito"], "n": len(items),
                                  "fotos": e[6] if e else 0, "temas": ct, "actores": ca, "bloques": cb}
        resumen.append(doc)

    # días anteriores al método de portadas (resumen ya calculado, se copian tal cual)
    seed_res = {os.path.basename(p)[:-5]: p for p in glob.glob(os.path.join(a.archivo, "resumen", "*.json"))}
    for f, p in seed_res.items():
        if f not in por_dia:
            resumen.append(leer(p))
    resumen.sort(key=lambda r: r["fecha"], reverse=True)

    cambios = 0
    cambios += escribir(os.path.join(a.salida, "resumen.json"), resumen)

    # ---------------------------------------------------------------- estado.json (última ejecución)
    if ejecs:
        ultimo = max(e[0] for e in ejecs)
        grupo = [e for e in ejecs if e[0] == ultimo]
        f = grupo[0][2].isoformat()
        nf = n_foto.get(f, 30)
        est = {"ejecutado": iso(ultimo), "fecha": f, "metodo": grupo[0][3], "tomas": grupo[0][8], "medios": {}}
        for e in sorted(grupo, key=lambda e: orden.index(e[1]) if e[1] in orden else 99):
            est["medios"][e[1]] = {"nombre": medios[e[1]]["nombre"], "ambito": medios[e[1]]["ambito"], "panel": True,
                                   "ok": e[4], "n": e[5],
                                   "metodo": f"portada · {nf} primeras piezas por foto · fotos {e[6]}/{e[7]}",
                                   "feeds": [medios[e[1]]["url"]], "errores": list(e[9])[:4]}
        cambios += escribir(os.path.join(a.salida, "estado.json"), est)

    # ---------------------------------------------------------------- palabras y titulares por día
    dias_export = dias_bd[-a.ultimos:] if a.ultimos > 0 else dias_bd
    pal_dia = collections.defaultdict(dict)
    origen_pal = {}
    for f, amb, clave, w, n, origen in pals:
        f = f.isoformat()
        pal_dia[f].setdefault(amb, {}).setdefault(clave, {})[w] = n
        origen_pal[f] = origen
    for f in dias_export:
        if f in pal_dia:
            cambios += escribir(os.path.join(a.salida, "palabras", f + ".json"),
                                {"fecha": f, "origen": origen_pal[f], "ambitos": pal_dia[f]})
        docs = []
        for mid in orden:
            items = por_dia[f].get(mid)
            if items:
                docs.append({"fecha": f, "medio": mid, "nombre": medios[mid]["nombre"], "ambito": medios[mid]["ambito"],
                             "idioma": medios[mid]["idioma"], "origen": origen_dia[f], "panel": True,
                             "tomas": horas.get(f, []), "n": len(items), "items": items})
        cambios += escribir(os.path.join(a.salida, "titulares", f + ".json"), docs)

    # palabras de los días de archivo (solo si la base de datos no tiene ese día)
    for p in glob.glob(os.path.join(a.archivo, "palabras", "*.json")):
        f = os.path.basename(p)[:-5]
        if f not in pal_dia:
            cambios += escribir(os.path.join(a.salida, "palabras", f + ".json"), leer(p))

    # ---------------------------------------------------------------- indice.json
    def fechas(sub):
        return sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(a.salida, sub, "*.json")))
    cambios += escribir(os.path.join(a.salida, "indice.json"),
                        {"resumen": [r["fecha"] for r in resumen], "palabras": fechas("palabras"), "titulares": fechas("titulares")})

    # diccionario.json: el dashboard lo lee desde docs/; la copia de la raíz es la que se edita
    ruta_dic = os.path.join(os.path.dirname(os.path.abspath(a.salida)), "diccionario.json")
    with open(os.path.join(AQUI, "diccionario.json"), encoding="utf-8") as f:
        dic_txt = f.read()
    if not os.path.exists(ruta_dic) or open(ruta_dic, encoding="utf-8").read() != dic_txt:
        with open(ruta_dic, "w", encoding="utf-8") as f:
            f.write(dic_txt)
        cambios += 1

    print(f"Exportado a {a.salida}: {len(dias_bd)} día(s) de la base de datos + {sum(1 for f in seed_res if f not in por_dia)} de archivo · "
          f"{cambios} archivo(s) cambiados")


if __name__ == "__main__":
    main()
