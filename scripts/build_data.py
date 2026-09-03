#!/usr/bin/env python3
"""
Genera datos.json para el dashboard de sell-in del importador (Murken).

Fuente: sigma-bigquery.sigmarepo.bq_ventas
Alcance: SOLO la venta del importador (EMPRESA 0033 y 0034 = Nutregal).
         El sell-out de los distribuidores queda fuera de este reporte.

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
import json
import os
import sys
import tempfile
import warnings
from datetime import datetime, timezone

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


def _params():
    return [
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
    args = ap.parse_args()

    client = _client()

    if args.check_schema:
        return check_schema(client)

    datos = build(client)
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
    sin_clas = datos["calidad"]["articulos_sin_clasificar"]
    if sin_clas:
        print(f"  ATENCION: {len(sin_clas)} articulo(s) sin clasificar — revisar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
