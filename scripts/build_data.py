#!/usr/bin/env python3
"""
Genera datos.json para el dashboard de sell-in del importador (Murken).

Fuente: sigma-bigquery.sigmarepo.bq_ventas
Alcance: SOLO la venta del importador (EMPRESA 0033 y 0034 = Nutregal).
         El sell-out de los distribuidores queda fuera de este reporte.

Ademas del sell-in arma el stock del deposito 282 (sigmarepo.bq_stocks), los
dias de inventario por SKU y el rolling forecast que se edita en Google Sheets
(ver config.json).

Uso:
    python scripts/build_data.py                  # escribe datos.json
    python scripts/build_data.py --check-schema   # solo valida columnas, no consulta
    python scripts/build_data.py --out otro.json

Credenciales: variable de entorno GOOGLE_APPLICATION_CREDENTIALS apuntando al
JSON de la service account, o GCP_SA_KEY con el contenido del JSON (lo que usa
el workflow de GitHub Actions).
"""

from __future__ import annotations

import argparse
import calendar
import csv
import io
import json
import os
import re
import sys
import tempfile
import unicodedata
import urllib.request
import warnings
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Con credenciales de usuario (gcloud) y sin quota project, google-auth avisa en
# cada corrida. No podemos setear el quota project porque la cuenta no tiene
# serviceusage.services.use en el proyecto, y para BigQuery no hace falta: el job
# se factura al proyecto del job. Se silencia solo ESE aviso.
warnings.filterwarnings(
    "ignore",
    message="Your application has authenticated using end user credentials",
    category=UserWarning,
)

from google.cloud import bigquery  # noqa: E402

# --------------------------------------------------------------------------
# CONFIG — si en BigQuery alguna columna se llama distinto, se cambia acá.
#
# Los 13 nombres de COL fueron verificados el 02/09/2026 contra
# sigmarepo.INFORMATION_SCHEMA.COLUMNS. Tipos reales:
#   FECHA DATE · EMPRESA/ESTADO/COMPROBANTE_TIPO/ITEM_PROVEEDOR STRING
#   CLIENTE_ID INT64 · CLIENTE_NOMBRE STRING · ITEM_ARTICULO STRING
#   ITEM_ARTICULO_ID INT64 · BULTOS/KILOS/NETO/UNIDADES_POR_BULTO FLOAT64
# --------------------------------------------------------------------------

PROJECT = "sigma-bigquery"
DATASET = "sigmarepo"
TABLE = "bq_ventas"

COL = {
    "fecha": "FECHA",
    "empresa": "EMPRESA",
    "estado": "ESTADO",
    "comprobante_tipo": "COMPROBANTE_TIPO",
    "proveedor": "ITEM_PROVEEDOR",
    "articulo_id": "ITEM_ARTICULO_ID",
    "articulo": "ITEM_ARTICULO",
    "cliente_id": "CLIENTE_ID",
    "cliente_nombre": "CLIENTE_NOMBRE",
    "bultos": "ITEM_BULTOS",
    "kilos": "ITEM_KILOS",
    "neto": "ITEM_NETO",
    "unidades_por_bulto": "ITEM_UNIDADES_POR_BULTO",
}

PROVEEDOR_MURKEN = "89221"          # campo STRING; el INT64 ITEM_PROVEEDOR_ID no sirve
EMPRESAS_IMPORTADOR = ["0033", "0034"]   # 0033 y 0034 = Nutregal, se consolidan
ESTADOS_VALIDOS = ["Pagado", "Pendiente"]

# Clasificación fija por ITEM_ARTICULO_ID (nunca filtrar por descripción).
# OJO: en la tabla, ITEM_ARTICULO NO es una descripcion sino un codigo numerico
# (201700705 y similares), asi que los nombres legibles salen de este diccionario.
NOMBRES_PRODUCTO = {
    30727: "Rústicas Sal de Mar 115",
    30728: "Rústicas Queso y Orégano 115",
    30729: "Rústicas Cebolla Caramelizada 115",
    30730: "Rústicas Albahaca y Oliva 115",
    30731: "Crema Ciboulette 115",
    30732: "Jamón Serrano 115",
    30733: "Clásicas Corte Liso 80",
    30734: "Clásicas Corte Americano 80",
    30735: "Clásicas Corte Liso 36",
    30736: "Clásicas Corte Americano 36",
}
ARTICULOS_PRODUCTO = list(NOMBRES_PRODUCTO)

# Codigo de articulo (ITEM_ARTICULO en bq_ventas, ARTICULO_CODIGO en bq_stocks).
# En las dos tablas viene con espacios adelante: siempre comparar con TRIM.
CODIGO_PRODUCTO = {
    30727: "201700697",
    30728: "201700698",
    30729: "201700699",
    30730: "201700701",
    30731: "201700703",
    30732: "201700704",
    30733: "201700705",
    30734: "201700706",
    30735: "201700721",
    30736: "201700722",
}
ID_POR_CODIGO = {v: k for k, v in CODIGO_PRODUCTO.items()}

# Material POP: se compra y factura sin valor comercial, infla bultos. Excluir.
ARTICULOS_POP = [31851, 31956, 31852, 31957, 32047, 32048]

# Reconocimientos: se reportan aparte, no ensucian el analisis comercial.
ARTICULOS_RECONOCIMIENTO = {
    32208: "Sell-in roturas",
    32209: "Sell-out comerciales",
    32210: "Retiros",
    34786: "Productos defectuosos (Nutregal)",
}

# SKUs cuyo "bulto" no es comparable en el tiempo: 36GRX66 fue renombrado a
# 36GRX60, mismo articulo_id con distinto ITEM_UNIDADES_POR_BULTO segun periodo.
ARTICULOS_BULTO_NO_COMPARABLE = [30735, 30736]

# Umbral para marcar filas con bultos pero practicamente sin facturacion.
UMBRAL_NETO_POR_BULTO = 100.0  # $ por bulto


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

BASE_CTE = f"""
WITH base AS (
  SELECT
    {COL['fecha']}                               AS fecha,
    DATE_TRUNC({COL['fecha']}, MONTH)            AS mes,
    CAST({COL['articulo_id']} AS INT64)          AS articulo_id,
    {COL['articulo']}                            AS articulo,
    CAST({COL['cliente_id']} AS STRING)          AS cliente_id,
    {COL['cliente_nombre']}                      AS cliente,
    {COL['comprobante_tipo']}                    AS comprobante_tipo,
    {COL['unidades_por_bulto']}                  AS unidades_por_bulto,
    COALESCE({COL['bultos']}, 0)                 AS bultos,
    COALESCE({COL['kilos']}, 0)                  AS kilos,
    COALESCE({COL['neto']}, 0)                   AS neto
  FROM `{PROJECT}.{DATASET}.{TABLE}`
  WHERE {COL['proveedor']} = @proveedor
    AND {COL['empresa']} IN UNNEST(@empresas)
    AND {COL['estado']} IN UNNEST(@estados)
)
"""

Q_MENSUAL_CLIENTE = BASE_CTE + """
SELECT
  FORMAT_DATE('%Y-%m', mes)      AS mes,
  cliente_id,
  ANY_VALUE(cliente)             AS cliente,
  SUM(bultos)                    AS cajas,
  SUM(kilos)                     AS kilos,
  SUM(neto)                      AS neto
FROM base
WHERE articulo_id IN UNNEST(@productos)
GROUP BY mes, cliente_id
ORDER BY mes, neto DESC
"""

Q_MENSUAL_SKU = BASE_CTE + """
SELECT
  FORMAT_DATE('%Y-%m', mes)      AS mes,
  articulo_id,
  ANY_VALUE(articulo)            AS articulo,
  SUM(bultos)                    AS cajas,
  SUM(kilos)                     AS kilos,
  SUM(neto)                      AS neto
FROM base
WHERE articulo_id IN UNNEST(@productos)
GROUP BY mes, articulo_id
ORDER BY mes, neto DESC
"""

# Cliente x SKU x mes: alimenta la base automatica del forecast y la
# comparacion real vs forecast.
Q_MENSUAL_CLIENTE_SKU = BASE_CTE + """
SELECT
  FORMAT_DATE('%Y-%m', mes)      AS mes,
  cliente,
  articulo_id,
  SUM(bultos)                    AS cajas
FROM base
WHERE articulo_id IN UNNEST(@productos)
GROUP BY mes, cliente, articulo_id
"""

# Venta de los ultimos @ventana dias por SKU, contados desde la ULTIMA fecha
# cargada (Sigma carga bq_ventas con retraso: contar desde hoy subestima).
Q_VENTA_VENTANA = BASE_CTE + """
, ref AS (
  SELECT MAX(fecha) AS ultima FROM base WHERE articulo_id IN UNNEST(@productos)
)
SELECT
  b.articulo_id                  AS articulo_id,
  SUM(b.bultos)                  AS cajas,
  ANY_VALUE(ref.ultima)          AS ultima
FROM base b CROSS JOIN ref
WHERE b.articulo_id IN UNNEST(@productos)
  AND b.fecha > DATE_SUB(ref.ultima, INTERVAL @ventana DAY)
GROUP BY b.articulo_id
"""

# Venta del mes en curso por SKU (lo ya facturado cuenta contra el forecast).
Q_VENTA_MES_CURSO = BASE_CTE + """
SELECT
  articulo_id,
  SUM(bultos)                    AS cajas,
  MAX(fecha)                     AS ultima
FROM base
WHERE articulo_id IN UNNEST(@productos)
  AND mes = DATE_TRUNC(CURRENT_DATE('America/Argentina/Buenos_Aires'), MONTH)
GROUP BY articulo_id
"""

# Stock del deposito 282. bq_stocks es una foto: se reescribe entera todos los
# dias y no tiene columna de fecha. ARTICULO_STOCK esta en UNIDADES (paquetes).
Q_STOCK = f"""
SELECT
  TRIM(ARTICULO_CODIGO)          AS codigo,
  ANY_VALUE(TRIM(DEPOSITO))      AS deposito,
  ANY_VALUE(ARTICULO_NOMBRE)     AS nombre,
  SUM(ARTICULO_STOCK)            AS stock_unidades,
  SUM(ARTICULO_RESERVA)          AS reserva_unidades,
  SUM(ARTICULO_BLOQUEO)          AS bloqueo_unidades,
  MAX(ARTICULO_UXB)              AS uxb,
  MAX(ARTICULO_UNIKG)            AS kg_unidad
FROM `{PROJECT}.{DATASET}.bq_stocks`
WHERE STARTS_WITH(TRIM(DEPOSITO), @deposito)
  AND TRIM(ARTICULO_CODIGO) IN UNNEST(@codigos)
GROUP BY codigo
"""

Q_STOCK_FOTO = f"""
SELECT TIMESTAMP_MILLIS(last_modified_time) AS modificada
FROM `{PROJECT}.{DATASET}.__TABLES__`
WHERE table_id = 'bq_stocks'
"""

Q_RECONOCIMIENTOS = BASE_CTE + """
SELECT
  FORMAT_DATE('%Y-%m', mes)      AS mes,
  articulo_id,
  SUM(neto)                      AS neto
FROM base
WHERE articulo_id IN UNNEST(@reconocimientos)
GROUP BY mes, articulo_id
ORDER BY mes
"""

# Bultos bonificados: producto real entregado sin cargo (inversion comercial).
# Se separa del material POP, que ya quedo excluido por articulo_id.
Q_BONIFICADOS = BASE_CTE + """
SELECT
  FORMAT_DATE('%Y-%m', mes)      AS mes,
  cliente_id,
  ANY_VALUE(cliente)             AS cliente,
  SUM(bultos)                    AS cajas
FROM base
WHERE articulo_id IN UNNEST(@productos)
  AND bultos > 0
  AND SAFE_DIVIDE(neto, bultos) < @umbral
GROUP BY mes, cliente_id
HAVING cajas > 0
ORDER BY mes
"""

# Control: cuanto "bulto" aportaria el material POP si no lo excluyeramos.
Q_POP = BASE_CTE + """
SELECT
  SUM(bultos) AS cajas_pop,
  SUM(neto)   AS neto_pop
FROM base
WHERE articulo_id IN UNNEST(@pop)
"""

# Control: articulos que aparecen y no estan clasificados en ninguna lista.
Q_SIN_CLASIFICAR = BASE_CTE + """
SELECT
  articulo_id,
  ANY_VALUE(articulo) AS articulo,
  SUM(bultos)         AS cajas,
  SUM(neto)           AS neto
FROM base
WHERE articulo_id NOT IN UNNEST(@productos)
  AND articulo_id NOT IN UNNEST(@pop)
  AND articulo_id NOT IN UNNEST(@reconocimientos)
GROUP BY articulo_id
"""
# Sin ORDER BY a proposito: el alias "neto" colisiona con la columna "neto" de la
# CTE y BigQuery lo lee como agregacion de agregacion. Se ordena en Python.

# Control: clientes con mas de un cliente_id para el mismo nombre.
Q_CLIENTES_DUPLICADOS = BASE_CTE + """
SELECT
  cliente,
  ARRAY_AGG(DISTINCT cliente_id ORDER BY cliente_id) AS ids
FROM base
WHERE articulo_id IN UNNEST(@productos)
GROUP BY cliente
HAVING COUNT(DISTINCT cliente_id) > 1
"""

# Control: bulto no comparable por el cambio 36GRX66 -> 36GRX60.
Q_UNIDADES_POR_BULTO = BASE_CTE + """
SELECT
  articulo_id,
  FORMAT_DATE('%Y-%m', mes)                        AS mes,
  ARRAY_AGG(DISTINCT unidades_por_bulto IGNORE NULLS
            ORDER BY unidades_por_bulto)           AS unidades
FROM base
WHERE articulo_id IN UNNEST(@no_comparables)
GROUP BY articulo_id, mes
ORDER BY articulo_id, mes
"""


CONFIG = {}


def _params():
    st = CONFIG.get("stock", {})
    return [
        bigquery.ScalarQueryParameter("deposito", "STRING", st.get("deposito_prefijo", "282-")),
        bigquery.ArrayQueryParameter("codigos", "STRING", list(CODIGO_PRODUCTO.values())),
        bigquery.ScalarQueryParameter("ventana", "INT64", int(st.get("ventana_dias", 90))),
        bigquery.ScalarQueryParameter("proveedor", "STRING", PROVEEDOR_MURKEN),
        bigquery.ArrayQueryParameter("empresas", "STRING", EMPRESAS_IMPORTADOR),
        bigquery.ArrayQueryParameter("estados", "STRING", ESTADOS_VALIDOS),
        bigquery.ArrayQueryParameter("productos", "INT64", ARTICULOS_PRODUCTO),
        bigquery.ArrayQueryParameter("pop", "INT64", ARTICULOS_POP),
        bigquery.ArrayQueryParameter(
            "reconocimientos", "INT64", list(ARTICULOS_RECONOCIMIENTO)
        ),
        bigquery.ArrayQueryParameter(
            "no_comparables", "INT64", ARTICULOS_BULTO_NO_COMPARABLE
        ),
        bigquery.ScalarQueryParameter("umbral", "FLOAT64", UMBRAL_NETO_POR_BULTO),
    ]


def _client() -> bigquery.Client:
    """Cliente de BigQuery. Acepta GCP_SA_KEY (contenido) o el path estandar."""
    raw = os.environ.get("GCP_SA_KEY")
    if raw and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write(raw)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
    return bigquery.Client(project=PROJECT)


def _run(client: bigquery.Client, sql: str) -> list[dict]:
    cfg = bigquery.QueryJobConfig(query_parameters=_params())
    return [dict(row) for row in client.query(sql, job_config=cfg).result()]


def check_schema(client: bigquery.Client) -> int:
    """Valida que existan las columnas que asume el script."""
    table = client.get_table(f"{PROJECT}.{DATASET}.{TABLE}")
    existentes = {f.name for f in table.schema}
    faltantes = {k: v for k, v in COL.items() if v not in existentes}
    print(f"Tabla: {PROJECT}.{DATASET}.{TABLE} — {len(existentes)} columnas")
    if faltantes:
        print("\nColumnas que el script espera y NO estan en la tabla:")
        for clave, col in faltantes.items():
            print(f"  {clave:20s} -> {col}")
        print("\nCorregir el diccionario COL al principio de este archivo.")
        return 1
    print("Todas las columnas esperadas existen.")
    return 0


# --------------------------------------------------------------------------
# Stock, dias de inventario y forecast
# --------------------------------------------------------------------------

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")


def _norm(s: str) -> str:
    s = (s or "").upper()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def _mes_mas(iso: str, n: int) -> str:
    y, m = map(int, iso.split("-"))
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def _dias_mes(iso: str) -> int:
    y, m = map(int, iso.split("-"))
    return calendar.monthrange(y, m)[1]


def _num(txt) -> float | None:
    """Numero de una celda exportada por Google Sheets, en formato AR o US."""
    if txt is None:
        return None
    t = str(txt).strip().replace(" ", "").replace(" ", "")
    if t in ("", "-", "—"):
        return None
    if "," in t and "." in t:          # 1.234,5
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:                      # 36,7
        t = t.replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _mes_de_encabezado(h: str) -> str | None:
    h = (h or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", h)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", h)   # 1/10/2026 si Sheets lo tomo como fecha
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}"
    return None


def leer_planilla(sheet_id: str, gid: str) -> list[dict]:
    """Lee la pestana del forecast como CSV. Requiere que la planilla este
    compartida con 'cualquier persona con el enlace'. Devuelve filas crudas."""
    url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
           f"?format=csv&gid={gid}")
    req = urllib.request.Request(url, headers={"User-Agent": "nutregal-dashboard"})
    with urllib.request.urlopen(req, timeout=40) as r:
        ctype = r.headers.get("Content-Type", "")
        raw = r.read().decode("utf-8-sig", errors="replace")
    if "text/csv" not in ctype and "<html" in raw[:500].lower():
        raise RuntimeError("Google devolvio una pagina de login: la planilla no esta "
                           "compartida como 'cualquier persona con el enlace'.")
    filas = list(csv.reader(io.StringIO(raw)))
    if not filas:
        raise RuntimeError("La planilla esta vacia.")
    enc = [c.strip() for c in filas[0]]
    idx = {_norm(c): i for i, c in enumerate(enc)}
    if "CLIENTE" not in idx or not any(k.startswith("CODIGO") for k in idx):
        raise RuntimeError("No encontre las columnas 'Cliente' y 'Codigo' en la fila 1.")
    i_cli = idx["CLIENTE"]
    i_cod = next(v for k, v in idx.items() if k.startswith("CODIGO"))
    i_base = next((v for k, v in idx.items() if k.startswith("BASE")), None)
    cols_mes = [(i, _mes_de_encabezado(c)) for i, c in enumerate(enc)]
    cols_mes = [(i, m) for i, m in cols_mes if m]
    out = []
    for f in filas[1:]:
        if len(f) <= max(i_cli, i_cod) or not f[i_cli].strip():
            continue
        cod = re.sub(r"\D", "", f[i_cod])
        if cod not in ID_POR_CODIGO:
            continue
        out.append({
            "cliente": f[i_cli].strip(),
            "codigo": cod,
            "base_planilla": _num(f[i_base]) if i_base is not None and i_base < len(f) else None,
            "meses": {m: (_num(f[i]) if i < len(f) else None) for i, m in cols_mes},
        })
    return out


def armar_forecast(cliente_sku: list[dict], hoy: date) -> dict:
    fc = CONFIG.get("forecast", {})
    n_base = int(fc.get("meses_base", 3))
    horizonte = int(fc.get("horizonte_meses", 6))
    mes_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    meses_fc = [_mes_mas(mes_actual, i + 1) for i in range(horizonte)]
    meses_base = [_mes_mas(mes_actual, -(n_base - i)) for i in range(n_base)]

    # Base automatica por cliente (nombre normalizado) x SKU.
    base: dict[tuple, float] = {}
    nombre: dict[str, str] = {}
    for r in cliente_sku:
        if r["mes"] in meses_base:
            k = (_norm(r["cliente"]), r["articulo_id"])
            base[k] = base.get(k, 0.0) + (r["cajas"] or 0) / n_base
            nombre.setdefault(_norm(r["cliente"]), r["cliente"])

    info = {
        "sheet_id": fc.get("sheet_id"),
        "sheet_url": (f"https://docs.google.com/spreadsheets/d/{fc['sheet_id']}/edit"
                      if fc.get("sheet_id") else None),
        "meses": meses_fc,
        "meses_base": meses_base,
        "fuente": "base automática",
        "error": None,
        "filas": [],
    }
    planilla = []
    if fc.get("sheet_id"):
        try:
            planilla = leer_planilla(fc["sheet_id"], str(fc.get("gid", "0")))
            info["fuente"] = "planilla"
        except Exception as e:  # noqa: BLE001 — cualquier falla cae a la base automatica
            info["error"] = str(e)[:300]

    vistos = set()
    for p in planilla:
        aid = ID_POR_CODIGO[p["codigo"]]
        k = (_norm(p["cliente"]), aid)
        vistos.add(k)
        b = base.get(k, 0.0)
        ref = p["base_planilla"]
        fila = {"cliente": p["cliente"], "articulo_id": aid, "codigo": p["codigo"],
                "base": round(b, 2), "meses": {}, "ajustado": []}
        for m in meses_fc:
            v = p["meses"].get(m)
            if v is None:
                v = b                      # celda vacia o mes que la planilla no tiene
            elif ref is not None and abs(v - round(ref)) > 0.01:
                fila["ajustado"].append(m)
            fila["meses"][m] = round(max(v, 0.0), 2)
        info["filas"].append(fila)
    for k, b in base.items():
        if k in vistos or b <= 0:
            continue
        cli, aid = k
        info["filas"].append({"cliente": nombre[cli], "articulo_id": aid,
                              "codigo": CODIGO_PRODUCTO[aid], "base": round(b, 2),
                              "meses": {m: round(b, 2) for m in meses_fc},
                              "ajustado": [], "fuera_de_planilla": True})
    return info


def armar_stock(stock_rows, ventana_rows, mes_curso_rows, forecast, foto, hoy: date) -> dict:
    st = CONFIG.get("stock", {})
    ventana = int(st.get("ventana_dias", 90))
    umb = st.get("umbrales_dias", {"critico": 30, "bajo": 60, "exceso": 120})

    def estado(d):
        if d is None:
            return "sin_venta"
        if d < umb["critico"]:
            return "critico"
        if d < umb["bajo"]:
            return "bajo"
        if d <= umb["exceso"]:
            return "optimo"
        return "exceso"

    por_cod = {r["codigo"]: r for r in stock_rows}
    venta = {r["articulo_id"]: r for r in ventana_rows}
    curso = {r["articulo_id"]: r for r in mes_curso_rows}
    ultima = max((r["ultima"] for r in ventana_rows if r.get("ultima")), default=None)

    fc_sku: dict[int, dict[str, float]] = {}
    for f in forecast["filas"]:
        d = fc_sku.setdefault(f["articulo_id"], {})
        for m, v in f["meses"].items():
            d[m] = d.get(m, 0.0) + v

    mes_actual = f"{hoy.year:04d}-{hoy.month:02d}"
    dias_restantes = _dias_mes(mes_actual) - hoy.day + 1
    skus = []
    for aid in ARTICULOS_PRODUCTO:
        cod = CODIGO_PRODUCTO[aid]
        r = por_cod.get(cod, {})
        uxb = r.get("uxb") or 0
        kgu = r.get("kg_unidad") or 0
        stk = r.get("stock_unidades") or 0
        res = r.get("reserva_unidades") or 0
        blo = r.get("bloqueo_unidades") or 0
        disp_u = stk - res - blo
        disp = disp_u / uxb if uxb else 0.0
        v90 = (venta.get(aid) or {}).get("cajas") or 0.0
        diaria = v90 / ventana if v90 > 0 else 0.0
        doi_h = (disp / diaria) if diaria > 0 else None

        # Proyeccion con el forecast: el resto del mes en curso al ritmo
        # historico, despues mes a mes al ritmo del forecast. Sin ingresos.
        fcm = fc_sku.get(aid, {})
        saldo = disp
        dias = 0.0
        quiebre = None
        proy = []
        tramos = [(mes_actual, dias_restantes, diaria * dias_restantes)]
        tramos += [(m, _dias_mes(m), fcm.get(m, 0.0)) for m in forecast["meses"]]
        for m, dd, dem in tramos:
            if quiebre is None and dem > 0 and saldo - dem < 0 and saldo >= 0:
                quiebre = dias + dd * (saldo / dem)
            saldo -= dem
            dias += dd
            proy.append({"mes": m, "demanda": round(dem, 2), "saldo": round(saldo, 2)})
        if quiebre is None and disp <= 0:
            quiebre = 0.0
        doi_f = quiebre  # None = alcanza para todo el horizonte (o no hay demanda)
        horizonte_dias = dias
        prox = forecast["meses"][0] if forecast["meses"] else None

        skus.append({
            "articulo_id": aid,
            "codigo": cod,
            "articulo": NOMBRES_PRODUCTO[aid],
            "uxb": uxb,
            "kg_unidad": kgu,
            "stock_unidades": stk,
            "reserva_unidades": res,
            "bloqueo_unidades": blo,
            "disponible_unidades": disp_u,
            "stock_cajas": round(stk / uxb, 2) if uxb else 0.0,
            "disponible_cajas": round(disp, 2),
            "disponible_kg": round(disp_u * kgu, 2),
            "venta_ventana_cajas": round(v90, 2),
            "venta_diaria_cajas": round(diaria, 3),
            "venta_mes_curso_cajas": round((curso.get(aid) or {}).get("cajas") or 0.0, 2),
            "doi_historico": round(doi_h, 1) if doi_h is not None else None,
            "estado_historico": estado(doi_h),
            "doi_forecast": round(doi_f, 1) if doi_f is not None else None,
            "doi_forecast_supera_horizonte": doi_f is None and any(t[2] > 0 for t in tramos),
            "estado_forecast": (estado(doi_f) if doi_f is not None
                                else ("exceso" if any(t[2] > 0 for t in tramos) else "sin_venta")),
            "fecha_quiebre": ((hoy + timedelta(days=int(doi_f))).isoformat()
                              if doi_f is not None else None),
            "forecast_prox_mes": round(fcm.get(prox, 0.0), 2) if prox else None,
            "proyeccion": proy,
            "en_tabla_stock": cod in por_cod,
        })

    tot_disp = sum(x["disponible_cajas"] for x in skus)
    tot_diaria = sum(x["venta_diaria_cajas"] for x in skus)
    return {
        "deposito": next((r.get("deposito") for r in stock_rows if r.get("deposito")), None),
        "foto_tabla": foto,
        "ventana_dias": ventana,
        "venta_hasta": ultima,
        "hoy": hoy.isoformat(),
        "horizonte_dias": horizonte_dias if skus else None,
        "umbrales_dias": umb,
        "skus": skus,
        "totales": {
            "disponible_cajas": round(tot_disp, 2),
            "disponible_kg": round(sum(x["disponible_kg"] for x in skus), 2),
            "venta_diaria_cajas": round(tot_diaria, 3),
            "doi_historico": round(tot_disp / tot_diaria, 1) if tot_diaria else None,
        },
    }


def historial_stock(path_previo: str, stock: dict, hoy: date) -> list[dict]:
    """bq_stocks no guarda historia: se la guardamos nosotros, una foto por dia.
    Vive dentro del mismo datos.json: se lee la version anterior (la que esta en
    disco antes de sobrescribirla) y se le agrega la foto de hoy."""
    try:
        with open(path_previo, encoding="utf-8") as fh:
            hist = (json.load(fh).get("stock") or {}).get("historial") or []
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        hist = []
    hist = [h for h in hist if h.get("fecha") != hoy.isoformat()]
    hist.append({
        "fecha": hoy.isoformat(),
        "cajas": {x["codigo"]: x["disponible_cajas"] for x in stock["skus"]},
    })
    return sorted(hist, key=lambda h: h["fecha"])[-400:]


def build(client: bigquery.Client) -> dict:
    por_cliente = _run(client, Q_MENSUAL_CLIENTE)
    por_sku = _run(client, Q_MENSUAL_SKU)
    reconocimientos = _run(client, Q_RECONOCIMIENTOS)
    bonificados = _run(client, Q_BONIFICADOS)
    pop = _run(client, Q_POP)
    sin_clasificar = sorted(
        _run(client, Q_SIN_CLASIFICAR),
        key=lambda r: abs(r.get("neto") or 0),
        reverse=True,
    )
    clientes_dup = _run(client, Q_CLIENTES_DUPLICADOS)
    unidades = _run(client, Q_UNIDADES_POR_BULTO)
    cliente_sku = _run(client, Q_MENSUAL_CLIENTE_SKU)
    ventana = _run(client, Q_VENTA_VENTANA)
    mes_curso = _run(client, Q_VENTA_MES_CURSO)
    stock_rows = _run(client, Q_STOCK)
    try:
        foto = _run(client, Q_STOCK_FOTO)
        foto = foto[0]["modificada"].isoformat() if foto else None
    except Exception:  # noqa: BLE001 — el metadato es opcional
        foto = None

    hoy = datetime.now(TZ_AR).date()
    forecast = armar_forecast(cliente_sku, hoy)
    stock = armar_stock(stock_rows, ventana, mes_curso, forecast, foto, hoy)
    for r in ventana + mes_curso:
        if r.get("ultima") is not None:
            r["ultima"] = r["ultima"].isoformat()
    if stock.get("venta_hasta") and not isinstance(stock["venta_hasta"], str):
        stock["venta_hasta"] = stock["venta_hasta"].isoformat()

    for reco in reconocimientos:
        reco["concepto"] = ARTICULOS_RECONOCIMIENTO.get(
            reco["articulo_id"], str(reco["articulo_id"])
        )

    # El codigo crudo de ITEM_ARTICULO no le dice nada a nadie: se reemplaza por
    # el nombre comercial y el codigo queda aparte.
    for fila in por_sku:
        fila["codigo"] = fila.get("articulo")
        fila["articulo"] = NOMBRES_PRODUCTO.get(
            fila["articulo_id"], str(fila["articulo_id"])
        )

    meses = sorted({fila["mes"] for fila in por_cliente})
    serie = []
    for mes in meses:
        filas = [f for f in por_cliente if f["mes"] == mes]
        cajas = sum(f["cajas"] for f in filas)
        kilos = sum(f["kilos"] for f in filas)
        neto = sum(f["neto"] for f in filas)
        serie.append(
            {
                "mes": mes,
                "cajas": round(cajas, 2),
                "kilos": round(kilos, 2),
                "neto": round(neto, 2),
                "neto_por_kg": round(neto / kilos, 2) if kilos else None,
            }
        )

    total_cajas = sum(f["cajas"] for f in por_cliente)
    total_kilos = sum(f["kilos"] for f in por_cliente)
    total_neto = sum(f["neto"] for f in por_cliente)

    return {
        "generado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "alcance": "Sell-in del importador (EMPRESA 0033 y 0034). No incluye sell-out.",
        "periodo": {"desde": meses[0], "hasta": meses[-1]} if meses else {},
        "totales": {
            "cajas": round(total_cajas, 2),
            "kilos": round(total_kilos, 2),
            "neto": round(total_neto, 2),
            "neto_por_kg": round(total_neto / total_kilos, 2) if total_kilos else None,
        },
        "serie_mensual": serie,
        "por_cliente": por_cliente,
        "por_sku": por_sku,
        "reconocimientos": reconocimientos,
        "por_cliente_sku": cliente_sku,
        "stock": stock,
        "forecast": forecast,
        "calidad": {
            "bonificados_sin_facturar": bonificados,
            "material_pop_excluido": pop[0] if pop else {},
            "articulos_sin_clasificar": sin_clasificar,
            "clientes_con_varios_ids": clientes_dup,
            "unidades_por_bulto_variables": unidades,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="datos.json")
    ap.add_argument("--check-schema", action="store_true")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()

    global CONFIG
    try:
        with open(args.config, encoding="utf-8") as fh:
            CONFIG = json.load(fh)
    except FileNotFoundError:
        CONFIG = {}

    client = _client()

    if args.check_schema:
        return check_schema(client)

    datos = build(client)
    datos["stock"]["historial"] = historial_stock(
        args.out, datos["stock"], datetime.now(TZ_AR).date()
    )
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(datos, fh, ensure_ascii=False, indent=2, default=str)

    def ar(valor: float) -> str:
        """Miles con punto, como se escriben los numeros en Argentina."""
        return f"{valor:,.0f}".replace(",", ".")

    tot = datos["totales"]
    print(f"OK — {args.out} escrito.")
    print(
        f"  {len(datos['serie_mensual'])} meses | {ar(tot['cajas'])} cajas | "
        f"{ar(tot['kilos'])} kg | ${ar(tot['neto'])}"
    )
    stk = datos["stock"]
    print(
        f"  stock depo 282: {ar(stk['totales']['disponible_cajas'])} cajas disponibles | "
        f"cobertura {stk['totales']['doi_historico']} dias al ritmo de los ultimos "
        f"{stk['ventana_dias']} dias"
    )
    fc = datos["forecast"]
    print(f"  forecast: {len(fc['filas'])} filas cliente x SKU, fuente {fc['fuente']}"
          + (f" (no se pudo leer la planilla: {fc['error']})" if fc["error"] else ""))
    sin_clas = datos["calidad"]["articulos_sin_clasificar"]
    if sin_clas:
        print(f"  ATENCION: {len(sin_clas)} articulo(s) sin clasificar — revisar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
