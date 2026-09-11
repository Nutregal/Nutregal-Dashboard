#!/usr/bin/env python3
"""
Controla el datos.json recien generado contra el de la corrida anterior y deja
un resumen legible en el summary del workflow.

Corre DENTRO del GitHub Action, justo despues de build_data.py y antes del
commit. No necesita red ni credenciales: compara el archivo nuevo contra la
version que ya esta en git.

Sale con codigo 1 si hay algo que requiere una decision humana. GitHub manda
mail automaticamente cuando un workflow falla, asi que ese exit es la alerta.

Uso:
    python scripts/check_data.py                 # compara contra HEAD
    python scripts/check_data.py --no-fail       # reporta pero nunca falla
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone

DIAS_PARA_ALERTA = 2.0          # antigüedad de generado_en que se considera rancia
UMBRAL_VARIACION_PCT = 15.0     # salto de cajas del último mes que amerita mirar


def ar(v: float) -> str:
    return f"{v:,.0f}".replace(",", ".")


def pesos(v: float) -> str:
    a = abs(v)
    if a >= 1e6:
        return "$" + f"{v/1e6:,.1f}".replace(",", "@").replace(".", ",").replace("@", ".") + "M"
    return "$" + ar(v)


def norm(s: str) -> str:
    s = (s or "").upper()
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    return " ".join(s.split())


def version_anterior(path: str) -> dict | None:
    """datos.json tal como esta commiteado en HEAD, o None si es la primera vez."""
    try:
        out = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            capture_output=True, text=True, check=True,
        ).stdout
        return json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--datos", default="datos.json")
    ap.add_argument("--canales", default="canales.json")
    ap.add_argument("--no-fail", action="store_true")
    args = ap.parse_args()

    with open(args.datos, encoding="utf-8") as fh:
        nuevo = json.load(fh)
    try:
        with open(args.canales, encoding="utf-8") as fh:
            canales = json.load(fh)
    except FileNotFoundError:
        canales = {"por_cliente": {}, "excluir": []}

    viejo = version_anterior(args.datos)

    lineas: list[str] = []      # el resumen que siempre se escribe
    alertas: list[str] = []     # lo que hace fallar el workflow

    # --- frescura ---------------------------------------------------------
    gen = nuevo.get("generado_en")
    if gen:
        edad = (datetime.now(timezone.utc) - datetime.fromisoformat(gen)).total_seconds() / 86400
        lineas.append(f"Datos generados: `{gen}` ({edad:.1f} días de antigüedad)")
        if edad > DIAS_PARA_ALERTA:
            alertas.append(
                f"Los datos tienen {edad:.1f} días. El refresh diario no está corriendo."
            )
    else:
        alertas.append("El archivo no tiene campo `generado_en`.")

    # --- totales ----------------------------------------------------------
    t = nuevo.get("totales", {})
    lineas.append(
        f"Totales: **{ar(t.get('cajas', 0))} cajas** · "
        f"{ar(t.get('kilos', 0))} kg · {pesos(t.get('neto', 0))}"
    )
    if viejo:
        tv = viejo.get("totales", {})
        d_cajas = t.get("cajas", 0) - tv.get("cajas", 0)
        d_neto = t.get("neto", 0) - tv.get("neto", 0)
        if abs(d_cajas) < 0.5 and abs(d_neto) < 1:
            lineas.append("Sin cambios respecto de la corrida anterior.")
        else:
            lineas.append(
                f"Variación vs corrida anterior: {d_cajas:+,.0f} cajas".replace(",", ".")
                + f" · {pesos(d_neto)}"
            )

    # --- último mes -------------------------------------------------------
    serie = nuevo.get("serie_mensual", [])
    if serie:
        ult = serie[-1]
        lineas.append(
            f"Último mes ({ult['mes']}): {ar(ult.get('cajas', 0))} cajas · "
            f"{pesos(ult.get('neto', 0))}"
        )
        if viejo:
            sv = {d["mes"]: d for d in viejo.get("serie_mensual", [])}
            prev = sv.get(ult["mes"])
            if prev and prev.get("cajas"):
                var = ((ult.get("cajas", 0) - prev["cajas"]) / prev["cajas"]) * 100
                if abs(var) >= UMBRAL_VARIACION_PCT:
                    alertas.append(
                        f"El mes {ult['mes']} se movió {var:+.1f}% en cajas de un día "
                        f"para el otro. Vale la pena mirar qué entró."
                    )

    # --- artículos sin clasificar ----------------------------------------
    q = nuevo.get("calidad", {})
    sin_clas = q.get("articulos_sin_clasificar", [])
    if sin_clas:
        ids = ", ".join(str(a.get("articulo_id")) for a in sin_clas)
        alertas.append(
            f"{len(sin_clas)} artículo(s) de Murken sin clasificar: {ids}. "
            f"Hay que decidir si son producto, material POP o reconocimiento "
            f"y agregarlos a scripts/build_data.py."
        )

    # --- clientes sin canal ----------------------------------------------
    mapa = {norm(k) for k in (canales.get("por_cliente") or {})}
    excluidos = {norm(e) for e in (canales.get("excluir") or [])}
    sin_canal = sorted({
        r["cliente"] for r in nuevo.get("por_cliente", [])
        if norm(r["cliente"]) not in mapa and norm(r["cliente"]) not in excluidos
    })
    if sin_canal:
        alertas.append(
            f"{len(sin_canal)} cliente(s) sin canal asignado: {', '.join(sin_canal)}. "
            f"Agregalos a canales.json o caen en SIN ASIGNAR."
        )

    # --- bonificados ------------------------------------------------------
    bon = sum(b.get("cajas", 0) for b in q.get("bonificados_sin_facturar", []))
    if bon:
        linea = f"Bonificados sin facturar: {ar(bon)} cajas"
        if viejo:
            bv = sum(b.get("cajas", 0) for b in
                     viejo.get("calidad", {}).get("bonificados_sin_facturar", []))
            if bv and abs(bon - bv) > 0.5:
                linea += f" ({bon - bv:+,.0f} vs ayer)".replace(",", ".")
        lineas.append(linea)
        lineas.append(
            "_Recordatorio: los meses con $/kg bajo son mix de bonificados, "
            "no caída de precio._"
        )

    # --- salida -----------------------------------------------------------
    titulo = "## Murken sell-in — control diario\n"
    cuerpo = "\n".join(f"- {l}" for l in lineas)
    if alertas:
        cuerpo += "\n\n### Requiere atención\n" + "\n".join(f"- {a}" for a in alertas)
    else:
        cuerpo += "\n\nSin observaciones."

    print(titulo + cuerpo)
    resumen = os.environ.get("GITHUB_STEP_SUMMARY")
    if resumen:
        with open(resumen, "a", encoding="utf-8") as fh:
            fh.write(titulo + cuerpo + "\n")

    if alertas and not args.no_fail:
        print(f"\n::error::{len(alertas)} punto(s) requieren atención — ver el resumen.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
