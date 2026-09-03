# Murken · Sell-in del importador

Dashboard estático del **sell-in del importador** (Nutregal, empresas `0033` y `0034`)
sobre `sigma-bigquery.sigmarepo.bq_ventas`, con refresh diario automático.

El sell-out de los distribuidores **no** está en este reporte: la base mezcla los dos
eslabones y los bultos se cuentan dos veces. Ese reporte viene después.

```
scripts/build_data.py   consulta BigQuery y escribe datos.json
canales.json            asignación manual de canal por cliente (se edita a mano)
index.html              front estático, hace fetch de datos.json y canales.json
.github/workflows/      refresh.yml: cron diario 08:00 ART que regenera y commitea
```

---

## Publicar en GitHub

### 0. Organización y repo

La "área de trabajo" de GitHub es una **organización**. Se crea en
github.com/organizations/plan → **Free** → nombre `Nutregal`, y adentro se crea el
repo del dashboard.

**Visibilidad — leer antes de elegir.** GitHub Pages solo publica desde repos
públicos en los planes gratuitos. Con GitHub Team el repo puede ser privado pero
**el sitio sigue siendo público**; para que la URL sea privada hace falta
Enterprise Cloud. Así que:

- Repo **público** → el dashboard queda accesible para cualquiera con el link.
- Repo **privado** → gratis, pero sin Pages: se abre local con
  `python -m http.server 8000`, o se hostea en otro lado (Railway, Cloudflare
  Pages, Netlify), donde sí se le puede poner una clave.

El workflow del refresh diario funciona igual en las dos opciones: lo que cambia
es solamente cómo se ve el dashboard.

### 0.b El archivo del workflow

`refresh-workflow.txt` es el workflow. Antes del primer push hay que ponerlo en su
lugar (viene con ese nombre porque las herramientas remotas no pueden escribir
dentro de `.github/workflows/`):

```powershell
mkdir .github\workflows
move refresh-workflow.txt .github\workflows\refresh.yml
```

### 1. Service account en GCP

En la consola de GCP, proyecto **`sigma-bigquery`**:

1. **IAM y administración → Cuentas de servicio → Crear cuenta de servicio**
   Nombre sugerido: `murken-dashboard-reader`.
2. Asignarle **solo lectura**, con estos dos roles:
   - `BigQuery Data Viewer` — acotado al dataset `sigmarepo` si querés ser prolijo
     (se hace desde BigQuery → dataset `sigmarepo` → Compartir → Agregar principal).
   - `BigQuery Job User` — a nivel proyecto; sin este rol no puede ejecutar consultas.
3. En la cuenta creada → **Claves → Agregar clave → Crear clave nueva → JSON**.
   Se descarga un archivo. **Ese archivo no va al repo** (`.gitignore` ya lo bloquea).

### 2. Cargar la clave como secret de GitHub

En el repo → **Settings → Secrets and variables → Actions → New repository secret**:

- Nombre: `GCP_SA_KEY`
- Valor: el **contenido completo** del JSON descargado (se abre con el Bloc de notas
  y se copia entero, llaves incluidas).

Así la credencial vive solo en GitHub. Nunca pasa por el repo ni por Claude.

### 3. Publicar

1. Crear el repo en GitHub y pushear esta carpeta.
2. **Settings → Pages → Source: Deploy from a branch**, branch `main`, carpeta `/ (root)`.
3. **Actions → Refresh datos.json → Run workflow** para la primera corrida manual.
   Si sale verde, aparece `datos.json` commiteado y el dashboard queda con datos reales.

A partir de ahí corre solo todos los días a las 08:00 ART.

---

## Correrlo en tu máquina (modo actual)

Mientras no haya service account, el script usa **las credenciales de usuario de
`powerbi@baidist.com.ar`**, que ya tienen permiso para consultar `bq_ventas`.

Una vez, para instalar el SDK y autenticarte:

```powershell
# 1. Instalar Google Cloud CLI (si no lo tenés)
winget install Google.CloudSDK

# 2. Autenticarte (abre el navegador; elegí powerbi@baidist.com.ar)
gcloud auth application-default login

# 3. Decirle qué proyecto factura las consultas
gcloud auth application-default set-quota-project sigma-bigquery
```

Después, cada vez:

```powershell
pip install -r requirements.txt

# Chequeo de esquema: barato, no consulta datos
python scripts/build_data.py --check-schema

# Genera datos.json (~1,5 GB de escaneo, centavos)
python scripts/build_data.py

# Levanta el front
python -m http.server 8000
```

Las credenciales de usuario sirven para correrlo a mano en tu compu, pero **no**
para GitHub Actions: el token se renueva contra la sesión del navegador y no se
comparte con un runner. Para el refresh diario automático hace falta la service
account.

Ojo con un detalle: `powerbi@baidist.com.ar` parece una cuenta compartida, armada
para Power BI. Si alguien le cambia la contraseña o le revocan el acceso, este
script deja de andar sin aviso. Otra razón para migrar a una service account
propia apenas sistemas la entregue.

Sin `datos.json`, `index.html` muestra **datos de muestra** con un cartel rojo arriba.
Sirve para ver la pantalla, pero los números no son reales.

### Validación hecha el 02/09/2026

Se corrieron las reglas de negocio directamente en la consola de BigQuery. El
filtro completo (proveedor 89221 + empresas 0033/0034 + estados Pagado/Pendiente
+ los 10 artículos de producto) da **17.555 cajas · 29.632 kg · $417.324.331**
para nov-25 a ago-26, contra las 17.234 cajas / 29.080 kg / $408,8M del
relevamiento anterior — la diferencia son las semanas de datos agregadas desde
entonces. Abril da exactamente 258 cajas, el mismo colapso ya identificado.

---

## Reglas de negocio que aplica el script

Están todas como constantes arriba de `build_data.py`, no escondidas en el SQL.

- **Filtro base:** `ITEM_PROVEEDOR = '89221'` (campo STRING; el INT64
  `ITEM_PROVEEDOR_ID` no tiene ese valor), `EMPRESA IN ('0033','0034')`,
  `ESTADO IN ('Pagado','Pendiente')`.
- **Producto real (10 artículos):** `30727`–`30736`. Se clasifica por
  `ITEM_ARTICULO_ID`, nunca por descripción.
- **Material POP excluido (6):** exhibidores, ganchera, IN OU pallets, tira de
  plástico. Se compran y facturan sin valor comercial e inflan los bultos.
  El total excluido queda reportado en el panel de calidad de datos.
- **Reconocimientos separados (4):** `32208` roturas, `32209` comerciales,
  `32210` retiros, `34786` defectuosos. Se muestran aparte, no ensucian el
  análisis comercial.
- **Bonificados sin facturar:** producto real entregado sin cargo como inversión
  comercial. **Cuenta como volumen** y se marca en calidad de datos.
- **`30735` y `30736`:** el 36GRX66 pasó a 36GRX60 con el mismo
  `ITEM_ARTICULO_ID`, así que el "bulto" no es comparable en el tiempo para esos
  dos SKUs. Por eso el dashboard tiene el gráfico de **kilos**, que sí lo es.
- **Canal:** no está en la base. Se asigna a mano en `canales.json`, por nombre
  normalizado, para que un cliente con dos `cliente_id` (Supermercados La
  Economía) caiga en un solo canal.

### Pendiente tuyo

`canales.json` sale con **todos los clientes en `SIN ASIGNAR`**: la lista de canales
la definís vos. Cargá los valores en `canales_disponibles` y asignáselos a cada
cliente. Hasta que lo hagas la matriz muestra una sola fila.

---

## Referencia rápida de la tabla

`bq_ventas` tiene ~20,8 millones de líneas — es la operación completa del
distribuidor, no solo Murken. El grano es la **línea de comprobante**, no el
comprobante.

- `COMPROBANTE_TIPO`: `F` factura, `C` nota de crédito (importes ya negativos),
  `D` nota de débito.
- `ESTADO`: `Pagado` / `Pendiente` / `Anulado`. Los anulados son 0,2% en plata pero
  18% de las líneas y arrastran clientes fantasma, así que se filtran igual.
- `ITEM_BULTOS` = caja (validado contra las cajas del IDV del Business Review).
