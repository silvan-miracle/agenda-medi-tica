#!/usr/bin/env python3
"""
cargar.py · puente entre los JSON de portadas.py y la base de datos (Supabase / Postgres)
-----------------------------------------------------------------------------------------
Lee la cadena de conexión de la variable de entorno DATABASE_URL (en GitHub: un Secret).

  toma          guarda la foto de portada recién hecha en la tabla `tomas`   (paso 2)
                  python cargar.py toma [--archivo out_toma/toma.json]
  bajar-tomas   descarga de `tomas` todas las fotos de un día a una carpeta   (paso 3)
                  python cargar.py bajar-tomas --fecha AAAA-MM-DD [--dir tomas_dl]
  dia           rehace el día en `piezas`, `palabras` y `ejecuciones`          (paso 4)
                  python cargar.py dia --fecha AAAA-MM-DD [--dir out]
  historico     inserta en `tomas` fotos antiguas guardadas como JSON         (paso 16)
                  python cargar.py historico --dir historico/tomas

Reglas:
  · `tomas` solo admite inserciones (el crudo no se toca); repetir una toma no la duplica.
  · `dia` borra las filas de ese día y las vuelve a insertar en una sola transacción:
    repetirlo es seguro y no duplica nada.
"""
import argparse, glob, json, os, re, sys

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:
    sys.exit("Falta psycopg. Instálalo con: pip install -r requirements.txt")


def conectar():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        sys.exit("Falta la variable de entorno DATABASE_URL (cadena de conexión del Session pooler de Supabase).")
    # prepare_threshold=None: compatible con cualquier pooler de Supabase
    return psycopg.connect(url, prepare_threshold=None, connect_timeout=30)


def leer(ruta):
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- toma
def insertar_toma(cur, doc_id, doc):
    cur.execute(
        """insert into tomas (id, fecha, hora, ts, n_por_foto, medios)
           values (%s, %s, %s, %s::timestamptz, %s, %s)
           on conflict (id) do nothing""",
        (doc_id, doc["fecha"], doc["hora"], doc["ts"], doc.get("n_por_foto", 30), Jsonb(doc["medios"])),
    )
    return cur.rowcount == 1


def cmd_toma(a):
    doc = leer(a.archivo)
    ok = [m for m, r in doc["medios"].items() if r.get("ok")]
    if not ok:
        # Si falla TODO es casi seguro un problema de red/bloqueo: no se guarda una foto vacía.
        print("✗ Ningún medio respondió: no se guarda la toma.", file=sys.stderr)
        sys.exit(2)
    doc_id = f"{doc['fecha']}__{doc['hora'].replace(':', '')}"
    with conectar() as con, con.cursor() as cur:
        nueva = insertar_toma(cur, doc_id, doc)
    print(f"{'Toma guardada' if nueva else 'La toma ya existía'}: tomas/{doc_id} · {len(ok)}/{len(doc['medios'])} medios con datos")
    if len(ok) < len(doc["medios"]):
        caidos = sorted(set(doc["medios"]) - set(ok))
        print("  Sin datos: " + ", ".join(caidos))


# ---------------------------------------------------------------- bajar-tomas
def cmd_bajar_tomas(a):
    from zoneinfo import ZoneInfo
    os.makedirs(a.dir, exist_ok=True)
    for f in glob.glob(os.path.join(a.dir, "*.json")):      # sin restos de ejecuciones anteriores
        os.remove(f)
    with conectar() as con, con.cursor() as cur:
        cur.execute("select id, fecha, hora, ts, n_por_foto, medios from tomas where fecha = %s order by ts", (a.fecha,))
        filas = cur.fetchall()
    if not filas:
        sys.exit(f"No hay tomas del {a.fecha} en la base de datos.")
    madrid = ZoneInfo("Europe/Madrid")
    for doc_id, fecha, hora, ts, n, medios in filas:
        doc = {"fecha": fecha.isoformat(), "hora": hora, "ts": ts.astimezone(madrid).isoformat(timespec="seconds"),
               "n_por_foto": n, "medios": medios}
        with open(os.path.join(a.dir, doc_id + ".json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(filas)} toma(s) del {a.fecha} en {a.dir}: " + ", ".join(r[2] for r in filas))


# ---------------------------------------------------------------- dia
def cmd_dia(a):
    D = a.fecha
    estado = leer(os.path.join(a.dir, "estado", "ultima.json"))
    if estado.get("fecha") != D:
        sys.exit(f"{a.dir}/estado/ultima.json es del {estado.get('fecha')}, no del {D}: ¿se ejecutó portadas.py dia con la fecha correcta?")
    titulares = [leer(f) for f in sorted(glob.glob(os.path.join(a.dir, "titulares", f"{D}__*.json")))]
    ruta_pal = os.path.join(a.dir, "palabras", D + ".json")
    palabras = leer(ruta_pal) if os.path.exists(ruta_pal) else None

    piezas = [(D, d["medio"], i["u"], i["t"], i.get("pos", 0), i.get("h", ""), i.get("fotos", []),
               i.get("temas", []), i.get("actores", []), d.get("origen", "portadas"))
              for d in titulares for i in d["items"]]
    pal = []
    if palabras:
        for amb, claves in palabras.get("ambitos", {}).items():
            for clave, ws in claves.items():
                pal.extend((D, amb, clave, w, n, palabras.get("origen", "portadas")) for w, n in ws.items())
    horas = estado.get("tomas", [])
    ejec = []
    for mid, e in estado["medios"].items():
        m = re.search(r"fotos (\d+)/(\d+)", e.get("metodo", ""))
        ejec.append((estado["ejecutado"], mid, D, estado.get("metodo", "portadas"), bool(e.get("ok")), e.get("n", 0),
                     int(m[1]) if m else 0, int(m[2]) if m else len(horas), horas, e.get("errores", [])))

    with conectar() as con, con.cursor() as cur:                 # una transacción: o entra todo o nada
        if piezas:
            cur.execute("delete from piezas where fecha = %s", (D,))
            cur.execute("delete from palabras where fecha = %s", (D,))
            cur.executemany(
                """insert into piezas (fecha, medio, url, titular, pos, h, fotos, temas, actores, origen)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", piezas)
            if pal:
                cur.executemany("insert into palabras (fecha, ambito, clave, palabra, n, origen) values (%s,%s,%s,%s,%s,%s)", pal)
        else:
            print("⚠ Ningún medio con piezas: se conserva lo que hubiera del día y solo se anota la ejecución.", file=sys.stderr)
        cur.executemany(
            """insert into ejecuciones (ejecutado, medio, fecha, metodo, ok, n, fotos_ok, fotos_total, tomas, errores)
               values (%s::timestamptz,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (ejecutado, medio) do nothing""", ejec)
    ok = sum(1 for e in ejec if e[4])
    print(f"Día {D}: {len(piezas)} piezas · {len(pal)} filas de palabras · {ok}/{len(ejec)} medios con datos")
    for e in ejec:
        if not e[4]:
            print(f"  ✗ {e[1]}: {'; '.join(e[9]) or 'sin datos'}")


# ---------------------------------------------------------------- historico
def cmd_historico(a):
    ficheros = sorted(glob.glob(os.path.join(a.dir, "*.json")))
    if not ficheros:
        sys.exit(f"No hay JSON en {a.dir}")
    nuevas = 0
    with conectar() as con, con.cursor() as cur:
        for f in ficheros:
            doc = leer(f)
            if "medios" not in doc or "ts" not in doc:
                print(f"  (se omite {os.path.basename(f)}: no parece una toma)")
                continue
            nuevas += insertar_toma(cur, os.path.splitext(os.path.basename(f))[0], doc)
    print(f"{nuevas} toma(s) nuevas de {len(ficheros)} archivos (las que ya estaban no se tocan)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("toma"); p.add_argument("--archivo", default="out_toma/toma.json"); p.set_defaults(f=cmd_toma)
    p = sub.add_parser("bajar-tomas"); p.add_argument("--fecha", required=True); p.add_argument("--dir", default="tomas_dl"); p.set_defaults(f=cmd_bajar_tomas)
    p = sub.add_parser("dia"); p.add_argument("--fecha", required=True); p.add_argument("--dir", default="out"); p.set_defaults(f=cmd_dia)
    p = sub.add_parser("historico"); p.add_argument("--dir", default="historico/tomas"); p.set_defaults(f=cmd_historico)
    a = ap.parse_args()
    a.f(a)
