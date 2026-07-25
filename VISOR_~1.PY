"""
=============================================================
VISOR DE PREEVALUACIÓN TERRITORIAL · VERSIÓN STREAMLIT
Pasantía profesional · Universidad Tecnológica de Panamá
Fundación NATURA · Restauración · Conservación · Monitoreo
=============================================================
ALCANCE: prototipo de preevaluación y priorización de verificaciones.
No es un sistema validado, no certifica cumplimiento EUDR y no sustituye
debida diligencia, revisión documental, verificación de campo ni asesoría legal.

Este archivo es el equivalente en Streamlit/Python del visor publicado como
GEE App (JavaScript, ui.* API). Integra las mismas cinco fuentes satelitales
(Hansen GFC, JRC TMF, ESRI LULC, GEDI, Sentinel-2 NDVI), el mismo índice de
prioridad ponderado y el mismo esquema de reporte técnico + narrativo.
El mapa se mantiene simple (Folium, sin comparador swipe ni herramienta de
dibujo) — esa parte de la interfaz del visor GEE no se portó en esta versión.
"""

import json
import re
from datetime import datetime, timezone
from io import BytesIO

import ee
import folium
import streamlit as st
from google.oauth2 import service_account
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from streamlit_folium import st_folium

st.set_page_config(
    page_title="Visor de preevaluación territorial",
    page_icon="🌿",
    layout="wide",
)

PROYECTO_EE = st.secrets["EE_PROJECT"]

# ═══════════════════════════════════════════════════════════════════════════
# 1. CONFIGURACIÓN — ACTUALIZAR AQUÍ CADA AÑO
# Todo lo que cambia con el tiempo está centralizado en este bloque, igual
# que en el script GEE. El resto del código no necesita modificarse para
# una actualización anual.
# ═══════════════════════════════════════════════════════════════════════════

VERSION_VISOR = "3.4.3-esri-cambios-streamlit"

# ── Assets institucionales ──────────────────────────────────────────────────
# ⚠ IMPORTANTE: mover estos assets a la cuenta institucional de NATURA antes
# de publicar el visor. Si la cuenta personal se desactiva, el visor falla.
ASSET_CUENCA = "projects/ee-julissaguevaravega/assets/CuencasHidrograficadeInteres"
ASSET_FINCAS = "projects/ee-julissaguevaravega/assets/FincasTrinidadv1"

# ── Versiones de datasets — actualizar cuando el proveedor publique nueva versión ─
HANSEN_ASSET = "UMD/hansen/global_forest_change_2025_v1_13"
TMF_ASSET = "projects/JRC/TMF/v1_2025/AnnualChanges"
ESRI_ASSET = "projects/sat-io/open-datasets/landcover/ESRI_Global-LULC_10m_TS"
GEDI_ASSET = "users/openforisearthmap/World_EarthMap/CanopyHeight_GEDI_V27"

# ── Años de referencia — actualizar anualmente ──────────────────────────────
ANO_HANSEN_MAX = 2025
ANO_TMF_MAX = 2025
ANO_ESRI_MIN = 2017
ANO_ESRI_MAX = 2024
ANO_DIAG_TMF = ANO_TMF_MAX  # año de diagnóstico TMF: siempre el más reciente
ANO_NDVI_MAX = 2025

# ── Fecha de corte EUDR — NO cambiar (es un dato legal fijo) ────────────────
CUTOFF_YEAR = 20  # lossyear codifica 2020 como valor 20
CUTOFF_LABEL = "31/12/2020"

# ── Umbrales operativos del sistema de alertas ──────────────────────────────
# Umbrales operativos para priorización de revisión de campo, no definiciones
# legales. Idénticos a los del script GEE — ver ese archivo para la
# justificación metodológica completa de cada valor.
UMBRAL_ALERTA_HANSEN_HA = 0.18
UMBRAL_REVISION_TMF_DEGRAD_HA = 2.0
UMBRAL_REVISION_TMF_DEFOR_HA = 0.5
UMBRAL_PCT_TMF_DEFOR = 1.0
UMBRAL_PCT_TMF_DEGRAD = 5.0
UMBRAL_PCT_ESRI_SALIDA = 5.0
UMBRAL_DOSEL_BAJO_M = 8
UMBRAL_DOSEL_MEDIO_BAJO_M = 15
UMBRAL_COBERTURA_GEDI_PCT = 20

# ── Diccionarios ESRI ────────────────────────────────────────────────────────
ESRI_NOMBRES = {
    1: "Agua", 2: "Árboles", 4: "Vegetación inundada", 5: "Cultivos",
    7: "Área construida", 8: "Suelo desnudo", 9: "Nieve / hielo",
    10: "Nubes", 11: "Pastizal / matorral",
}
ESRI_ORIG = [1, 2, 4, 5, 7, 8, 9, 10, 11]
ESRI_VIS = [1, 2, 3, 4, 5, 6, 7, 8, 9]
ESRI_COLORES = ["1A5BAB", "358221", "87D19E", "FFDB5C", "ED022A", "EDE9E4", "F2FAFF", "C8C8C8", "C6AD8D"]


# ═══════════════════════════════════════════════════════════════════════════
# 2. EARTH ENGINE — INICIALIZACIÓN Y HELPERS BÁSICOS
# ═══════════════════════════════════════════════════════════════════════════

@st.cache_resource
def iniciar_earth_engine():
    informacion = json.loads(st.secrets["EE_SERVICE_ACCOUNT_JSON"])
    credenciales = service_account.Credentials.from_service_account_info(
        informacion,
        scopes=[
            "https://www.googleapis.com/auth/earthengine",
            "https://www.googleapis.com/auth/cloud-platform",
        ],
    )
    ee.Initialize(credentials=credenciales, project=PROYECTO_EE)
    return True


def clave_orden_natural(valor):
    partes = re.split(r"(\d+)", str(valor).strip())
    return tuple(
        (0, int(parte)) if parte.isdigit() else (1, parte.casefold())
        for parte in partes
        if parte
    )


@st.cache_data(ttl=3600)
def obtener_ids_fincas():
    fincas = ee.FeatureCollection(ASSET_FINCAS)
    valores = fincas.aggregate_array("FincaID").distinct().getInfo()
    valores_validos = [valor for valor in valores if valor is not None]
    return sorted(valores_validos, key=clave_orden_natural)


def agregar_capa_ee(mapa, imagen, parametros, nombre, mostrar=True, opacidad=1.0):
    datos_mapa = ee.Image(imagen).getMapId(parametros)
    folium.TileLayer(
        tiles=datos_mapa["tile_fetcher"].url_format,
        attr="Google Earth Engine",
        name=nombre,
        overlay=True,
        control=True,
        show=mostrar,
        opacity=opacidad,
    ).add_to(mapa)


def obtener_limites(objeto):
    coordenadas = objeto.geometry().bounds(1).coordinates().getInfo()[0]
    longitudes = [punto[0] for punto in coordenadas]
    latitudes = [punto[1] for punto in coordenadas]
    return [[min(latitudes), min(longitudes)], [max(latitudes), max(longitudes)]]


def safe_number(v):
    return 0.0 if v is None else float(v)


def dato_ausente(v):
    if v is None:
        return True
    try:
        return not (float(v) == float(v))  # NaN check without importing math
    except (TypeError, ValueError):
        return True


# ═══════════════════════════════════════════════════════════════════════════
# 3. FUENTES SATELITALES — HANSEN, JRC TMF, ESRI, GEDI, NDVI (SENTINEL-2)
# Equivalentes en Python EE a las funciones del script GEE (sección 4).
# ═══════════════════════════════════════════════════════════════════════════

def get_hansen_capas(hansen_img):
    """Devuelve las 4 capas Hansen usadas en el diagnóstico."""
    tree_cover_2000 = hansen_img.select("treecover2000")
    loss = hansen_img.select("loss")
    loss_year = hansen_img.select("lossyear")

    loss_post_2020 = loss_year.updateMask(loss_year.gt(CUTOFF_YEAR)).rename("loss_post_2020")
    loss_pre_2020 = loss_year.updateMask(
        loss_year.gt(0).And(loss_year.lte(CUTOFF_YEAR))
    ).rename("loss_pre_2020")

    bosque_2000_mask = tree_cover_2000.unmask(0).gte(30)
    sin_perdida = loss.unmask(0).eq(0)
    bosque_linea_base_2020 = (
        bosque_2000_mask.And(sin_perdida.Or(loss_year.unmask(0).gt(CUTOFF_YEAR)))
        .selfMask()
        .rename("bosque_linea_base_2020")
    )
    bosque_libre_post_2020 = (
        bosque_2000_mask.And(sin_perdida).selfMask().rename("bosque_libre_post_2020")
    )
    return loss_post_2020, loss_pre_2020, bosque_linea_base_2020, bosque_libre_post_2020


def get_tmf_year(tmf_col, year):
    return tmf_col.select("Dec" + str(year)).rename("tmf_" + str(year))


def get_tmf_estado(tmf_col, year):
    t = get_tmf_year(tmf_col, year)
    return ee.Image.cat([
        t.eq(1).rename("tmf_e"), t.eq(2).rename("tmf_d"),
        t.eq(3).rename("tmf_f"), t.eq(4).rename("tmf_r"),
        t.eq(5).rename("tmf_a"), t.eq(6).rename("tmf_o"),
    ])


def es_bosque_tmf(img):
    return img.eq(1).Or(img.eq(4))


def get_gan_bosq_tmf(tmf_col, a, b):
    return (
        es_bosque_tmf(get_tmf_year(tmf_col, a)).Not()
        .And(es_bosque_tmf(get_tmf_year(tmf_col, b)))
        .selfMask()
        .rename("gan_bosq_t")
    )


def get_esri_year(esri_col, year):
    # ESRI LULC 10m disponible 2017-2024. Clamp para evitar mosaicos vacíos.
    y_safe = max(ANO_ESRI_MIN, min(ANO_ESRI_MAX, year))
    return esri_col.filterDate(f"{y_safe}-01-01", f"{y_safe}-12-31").mosaic().select(0)


def get_esri_visual_year(esri_col, year):
    return (
        get_esri_year(esri_col, year)
        .remap(ESRI_ORIG, ESRI_VIS)
        .rename(f"esri_v_{year}")
        .selfMask()
    )


def get_cambio_esri(esri_col, a, b):
    ai = get_esri_year(esri_col, a).eq(2)
    bi = get_esri_year(esri_col, b).eq(2)
    return (
        ai.Not().And(bi).multiply(1)
        .where(ai.And(bi.Not()), 2)
        .where(ai.And(bi), 3)
        .selfMask()
        .rename("cambio_esri")
    )


def es_veg_esri(img):
    return img.eq(2).Or(img.eq(4)).Or(img.eq(11))


def get_gan_veg_esri(esri_col, a, b):
    return (
        es_veg_esri(get_esri_year(esri_col, a)).Not()
        .And(es_veg_esri(get_esri_year(esri_col, b)))
        .selfMask()
        .rename("gan_veg_e")
    )


def get_canopy_height(geom):
    return (
        ee.ImageCollection(GEDI_ASSET)
        .filterBounds(geom)
        .mosaic()
        .select(0)
        .rename("canopy_height")
    )


# ── NDVI diferencial · Sentinel-2 SR Harmonized + máscara SCL ──────────────
# Idéntico en metodología al script GEE: mediana anual con relleno temporal
# (gap filling) al año anterior cuando hay nubosidad persistente. El ΔNDVI
# es puramente informativo — no participa en el índice de prioridad, igual
# que en la versión GEE.
def mascara_scl(img):
    scl = img.select("SCL")
    return img.updateMask(scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6)).Or(scl.eq(11)))


def get_ndvi(anio, bounds):
    anio_a = max(anio - 1, 2017)

    def col_s2(yr):
        return (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(bounds)
            .filterDate(f"{yr}-01-01", f"{yr}-12-31")
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
            .map(mascara_scl)
            .select(["B8", "B4"])
            .map(lambda img: img.toFloat())
        )

    col_actual = col_s2(anio)
    col_anterior = col_s2(anio_a)

    fallback = ee.Image.constant([0, 0]).rename(["B8", "B4"]).updateMask(ee.Image(0)).float()

    def mediana_con_fallback(col):
        return col.merge(ee.ImageCollection([fallback])).median()

    med_actual = mediana_con_fallback(col_actual)
    med_anterior = mediana_con_fallback(col_anterior)

    compuesto = ee.ImageCollection([fallback, med_anterior, med_actual]).mosaic()
    return compuesto.normalizedDifference(["B8", "B4"]).rename(f"ndvi_{anio}").float()


# ═══════════════════════════════════════════════════════════════════════════
# 4. CÁLCULO DEL DIAGNÓSTICO INTEGRADO
# Una sola función que reproduce las reducciones regionales del script GEE:
# Hansen + JRC TMF (+ NDVI informativo) en una reducción, ESRI en otra a su
# propia escala/proyección, y GEDI a 100 m. Devuelve un diccionario plano
# igual al que el script GEE arma con ee.Dictionary(...).evaluate(...).
# ═══════════════════════════════════════════════════════════════════════════

def ejecutar_diagnostico(geom, anio_inicial_esri, anio_final_esri, ndvi_anio_ref, es_analisis_cuenca):
    px_a = ee.Image.pixelArea().divide(10000)
    area_ha = geom.area(1).divide(10000)

    hansen = ee.Image(HANSEN_ASSET)
    loss_post_2020, loss_pre_2020, bosque_linea_base_2020, bosque_libre_post_2020 = get_hansen_capas(hansen)

    tmf_col = ee.ImageCollection(TMF_ASSET).filterBounds(geom).mosaic()
    tmf_b = get_tmf_estado(tmf_col, ANO_DIAG_TMF)
    # Ganancia de bosque TMF: período fijo de contexto 2020->año de diagnóstico,
    # equivalente a los valores por defecto del comparador visual en el script GEE.
    gan_bt = get_gan_bosq_tmf(tmf_col, CUTOFF_YEAR + 2000, ANO_DIAG_TMF)

    esri_col = ee.ImageCollection(ESRI_ASSET).filterBounds(geom)
    e_f = get_esri_year(esri_col, anio_final_esri)
    cambio = get_cambio_esri(esri_col, anio_inicial_esri, anio_final_esri)
    gan_ve = get_gan_veg_esri(esri_col, anio_inicial_esri, anio_final_esri)

    img_hansen = ee.Image.cat([
        loss_post_2020.gt(0).unmask(0).multiply(px_a).rename("pp"),
        loss_pre_2020.gt(0).unmask(0).multiply(px_a).rename("ph"),
        bosque_linea_base_2020.unmask(0).multiply(px_a).rename("bl"),
        bosque_libre_post_2020.unmask(0).multiply(px_a).rename("bs"),
    ])
    img_tmf = ee.Image.cat([
        gan_bt.unmask(0).multiply(px_a).rename("gt"),
        tmf_b.select("tmf_e").unmask(0).multiply(px_a).rename("te"),
        tmf_b.select("tmf_d").unmask(0).multiply(px_a).rename("td"),
        tmf_b.select("tmf_f").unmask(0).multiply(px_a).rename("tf"),
        tmf_b.select("tmf_r").unmask(0).multiply(px_a).rename("tr"),
    ])
    mask10 = ee.Image.cat([
        cambio.eq(1).unmask(0).rename("ge"),
        cambio.eq(2).unmask(0).rename("pe"),
        cambio.eq(3).unmask(0).rename("ee_"),
        gan_ve.unmask(0).rename("gv"),
        e_f.eq(1).unmask(0).rename("a1"),
        e_f.eq(2).unmask(0).rename("a2"),
        e_f.eq(4).unmask(0).rename("a4"),
        e_f.eq(5).unmask(0).rename("a5"),
        e_f.eq(7).unmask(0).rename("a7"),
        e_f.eq(8).unmask(0).rename("a8"),
        e_f.eq(11).unmask(0).rename("a11"),
    ]).toFloat()

    es_analisis_cuenca = bool(es_analisis_cuenca)
    escala_esri = 30 if es_analisis_cuenca else 10

    if es_analisis_cuenca:
        mask_esri_analisis = mask10.reduceResolution(
            reducer=ee.Reducer.mean(), maxPixels=1024
        ).reproject(crs=e_f.projection(), scale=30)
        r_forestal = ee.Image.cat([img_hansen, img_tmf]).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=geom, scale=30, maxPixels=1e13, tileScale=4
        )
    else:
        mask_esri_analisis = mask10
        r_hansen = img_hansen.reduceRegion(
            reducer=ee.Reducer.sum(), geometry=geom,
            crs=hansen.projection(), scale=30, maxPixels=1e13, tileScale=4,
        )
        r_tmf = img_tmf.reduceRegion(
            reducer=ee.Reducer.sum(), geometry=geom,
            crs=get_tmf_year(tmf_col, ANO_DIAG_TMF).projection(), scale=30, maxPixels=1e13, tileScale=4,
        )
        r_forestal = ee.Dictionary(r_hansen).combine(ee.Dictionary(r_tmf), True)

    img10 = mask_esri_analisis.multiply(px_a)
    r10 = img10.reduceRegion(
        reducer=ee.Reducer.sum(), geometry=geom,
        crs=e_f.projection(), scale=escala_esri, maxPixels=1e13, tileScale=4,
    )

    canopy_height = get_canopy_height(geom)
    gedi_pack = ee.Image.cat([canopy_height, canopy_height.mask().unmask(0).rename("gedi_frac")])
    r100 = gedi_pack.reduceRegion(reducer=ee.Reducer.mean(), geometry=geom, scale=100, maxPixels=1e13, tileScale=4)

    # NDVI: solo informativo (no se pondera en el índice), igual que en el
    # script GEE. Se reduce por separado a 10 m para no forzar la escala del
    # resto del diagnóstico y para poder omitirlo con gracia si S2 no tiene
    # imágenes limpias (queda enmascarado -> null).
    ndvi_a = get_ndvi(ndvi_anio_ref, geom)
    ndvi_b = get_ndvi(ANO_NDVI_MAX, geom)
    ndvi_delta = ndvi_b.subtract(ndvi_a).rename("ndvi_delta")
    r_ndvi = ndvi_delta.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=geom, scale=10, maxPixels=1e13, tileScale=4, bestEffort=True
    )

    stats = ee.Dictionary({
        "areaHa": area_ha,
        "r30": ee.Dictionary(r_forestal).combine(ee.Dictionary(r10), True),
        "alt": r100.get("canopy_height"),
        "gediPct": r100.get("gedi_frac"),
        "ndviDeltaMedio": r_ndvi.get("ndvi_delta"),
    })
    return stats.getInfo()


# ═══════════════════════════════════════════════════════════════════════════
# 5. ÍNDICE PONDERADO, CONSISTENCIA, SEMÁFORO Y ACCIÓN SUGERIDA
# Traducción directa de evaluarConsistencia / evalSemaforo / evalAccion del
# script GEE. Mismos pesos, mismos umbrales, mismo texto de salida.
# ═══════════════════════════════════════════════════════════════════════════

def evaluar_consistencia(area, tmf_d, tmf_f, tmf_r, per_e, gan_e, perd_post):
    pct_tf = (tmf_f / area) * 100 if area > 0 else 0
    pct_td = (tmf_d / area) * 100 if area > 0 else 0
    pct_pe = (per_e / area) * 100 if area > 0 else 0

    j_det = (
        tmf_f >= UMBRAL_REVISION_TMF_DEFOR_HA or pct_tf >= UMBRAL_PCT_TMF_DEFOR
        or tmf_d >= UMBRAL_REVISION_TMF_DEGRAD_HA or pct_td >= UMBRAL_PCT_TMF_DEGRAD
    )
    # Unidad mínima cartográfica ESRI: al menos 0.10 ha Y 5% del área.
    e_det = per_e >= 0.10 and pct_pe >= UMBRAL_PCT_ESRI_SALIDA
    e_rec = gan_e > per_e and gan_e > 0
    h_rec = perd_post >= UMBRAL_ALERTA_HANSEN_HA
    t_rec = tmf_r > (tmf_f + tmf_d)
    cnt = int(j_det) + int(e_det) + int(h_rec)

    if cnt >= 3:
        return {"nivel": "Alta consistencia", "tipo": "deterioro",
                "texto": "Tres fuentes presentan señales convergentes de posible deterioro. El resultado requiere verificación independiente."}
    if j_det and h_rec and not e_det:
        return {"nivel": "Consistencia parcial", "tipo": "deterioro_localizado",
                "texto": "El mapa forestal y las alertas de pérdida arbórea detectan cambio, pero el mapa de uso del suelo no. El cambio puede ser en un sector pequeño o muy reciente."}
    if j_det and e_det and not h_rec:
        return {"nivel": "Consistencia parcial", "tipo": "cambio_cobertura",
                "texto": "El mapa forestal y el mapa de uso del suelo detectan cambio. Puede ser deforestación, deterioro del bosque o un cambio temporal."}
    if j_det and e_rec:
        return {"nivel": "Lectura mixta", "tipo": "mosaico",
                "texto": "El mapa forestal detecta pérdida pero el mapa de uso del suelo muestra ganancia de árboles. El área tiene sectores muy diferentes entre sí."}
    if (e_rec or t_rec) and not h_rec and not j_det:
        return {"nivel": "Tendencia favorable", "tipo": "recuperacion",
                "texto": "Las capas favorecen estabilidad o recuperación. Sin señales fuertes de pérdida reciente."}
    if cnt == 1:
        return {"nivel": "Señal aislada", "tipo": "verificacion",
                "texto": "Solo una fuente muestra señal relevante. Revisar el mapa por sectores antes de concluir."}
    return {"nivel": "Sin señal consistente", "tipo": "estable",
            "texto": "No hay coincidencia suficiente de señales de deterioro. Mantener en seguimiento periódico."}


def evaluar_semaforo(pct_linea, tmf_d, tmf_f, per_e, gan_e, perd_post, consist, puntaje_det):
    e_st = gan_e == 0 and per_e == 0
    tex_e = (
        "ESRI no registra transición neta árbol/no árbol; " if e_st
        else f"ESRI registra {per_e:.1f} ha de salida y {gan_e:.1f} ha de ganancia; "
    )
    tex_base = (
        f"JRC TMF: {tmf_f:.1f} ha deforestación, {tmf_d:.1f} ha degradación; "
        f"{tex_e}Hansen: {perd_post:.2f} ha. "
    )
    if pct_linea < 10 and puntaje_det < 1.5:
        return {"color": "#5c3000", "nivel": "Contexto no forestal",
                "titulo": "ÁREA SIN COBERTURA FORESTAL RELEVANTE",
                "texto": f"Cobertura arbórea de 2000 persistente hasta 2020 limitada ({pct_linea:.1f}%). " + consist["texto"]}
    if puntaje_det >= 3.0:
        return {"color": "#7a0000", "nivel": "Alta",
                "titulo": "PRIORIDAD ALTA · Dos o más fuentes detectan pérdida o deterioro forestal",
                "texto": tex_base + consist["texto"]}
    if puntaje_det >= 1.5:
        return {"color": "#5c3000", "nivel": "Media",
                "titulo": "PRIORIDAD MEDIA · Se detectaron cambios que requieren revisión",
                "texto": tex_base + consist["texto"]}
    if puntaje_det >= 0.5:
        return {"color": "#4a3a00", "nivel": "Preventiva",
                "titulo": "PRIORIDAD PREVENTIVA · Condición de riesgo sin cambio reciente confirmado",
                "texto": f"Dosel bajo con datos válidos suficientes en el producto de altura · cobertura arbórea persistente: {pct_linea:.1f}%. " + consist["texto"]}
    return {"color": "#1a3d10", "nivel": "Baja",
            "titulo": "PRIORIDAD BAJA · Sin señales de deterioro en ninguna fuente",
            "texto": "Sin señales activas. " + consist["texto"]}


def evaluar_accion(pct_linea, alt, tmf_d, tmf_f, tmf_r, per_e, gan_e, perd_post, esri_nom,
                    puntaje_det, s_tmf, s_esri, s_hansen, s_gedi, s_ndvi):
    mot = []
    if s_tmf:
        mot.append(f"JRC TMF: defor. {tmf_f:.1f} ha, degrad. {tmf_d:.1f} ha [peso 2.0]")
    if s_hansen:
        mot.append(f"Hansen: pérdida post-{CUTOFF_LABEL} {perd_post:.2f} ha [peso 2.0]")
    if s_esri:
        mot.append(f"ESRI: salida de árboles {per_e:.1f} ha [peso 1.5]")
    if s_ndvi:
        mot.append("ΔNDVI: pérdida de vigor vegetal [peso 1.0]")
    if s_gedi:
        mot.append(f"GEDI: dosel bajo {alt:.1f} m [peso 0.5 — contexto]")
    if tmf_r > tmf_d + tmf_f and gan_e >= per_e:
        mot.append("señales de recuperación presentes")
    if pct_linea < 35:
        mot.append(f"cobertura arbórea de 2000 persistente hasta 2020 baja ({pct_linea:.1f}%)")

    if esri_nom in ("Cultivos", "Pastizal / matorral"):
        rec = "Revisar prácticas productivas, franjas ribereñas y opciones de restauración."
    elif esri_nom == "Suelo desnudo":
        rec = "Priorizar revegetación y control de erosión."
    elif esri_nom == "Área construida":
        rec = "Revisar infraestructura verde y protección de cauces."
    else:
        rec = "Definir acción con inspección local."

    m_str = "; ".join(mot) + ". " if mot else "Sin señales activas. "

    if puntaje_det >= 3.0:
        return {"nivel": "Alta", "titulo": "ACCIÓN SUGERIDA · Visita de campo prioritaria",
                "texto": "Varias fuentes presentan señales convergentes. Motivos: " + m_str + rec}
    if puntaje_det >= 1.5:
        return {"nivel": "Media", "titulo": "ACCIÓN SUGERIDA · Revisar imágenes y programar visita",
                "texto": "Se detectó cambio pero sin confirmación de múltiples fuentes. Motivos: " + m_str + rec}
    if puntaje_det >= 0.5:
        return {"nivel": "Preventiva", "titulo": "ACCIÓN SUGERIDA · Monitoreo preventivo",
                "texto": "Solo se detectó una condición de riesgo estructural. Motivos: " + m_str}
    return {"nivel": "Conservación", "titulo": "ACCIÓN SUGERIDA · Conservación y monitoreo anual",
            "texto": "Ninguna fuente satelital detectó señales de deterioro. Mantener protección y monitoreo."}


def nom_esri(v):
    try:
        return ESRI_NOMBRES.get(int(v), "Sin dato")
    except (TypeError, ValueError):
        return "Sin dato"


def dominante(hist):
    if not hist:
        return None
    mk, mv = None, -1
    for k, v in hist.items():
        if v > mv:
            mv, mk = v, k
    return mk


# ═══════════════════════════════════════════════════════════════════════════
# 6. PROCESAMIENTO COMPLETO: índice, diagnóstico y reportes
# ═══════════════════════════════════════════════════════════════════════════

def procesar_resultado(d, nombre_area, anio_inicial_esri, anio_final_esri, ndvi_anio_ref, es_analisis_cuenca):
    r = d.get("r30") or {}
    campos = ["pp", "ph", "bl", "bs", "te", "td", "tf", "tr", "ge", "pe", "ee_",
              "a1", "a2", "a4", "a5", "a7", "a8", "a11"]
    faltantes = [k for k in campos if dato_ausente(r.get(k))]
    if dato_ausente(d.get("areaHa")):
        faltantes.insert(0, "areaHa")
    if faltantes:
        return {"error": f"Campos sin dato: {', '.join(faltantes)}. No se asignaron ceros en su lugar."}

    area = safe_number(d.get("areaHa"))
    gedi_disponible = (
        not dato_ausente(d.get("alt")) and not dato_ausente(d.get("gediPct"))
        and safe_number(d.get("gediPct")) > 0
    )
    alt = safe_number(d.get("alt")) if gedi_disponible else None
    pct_cobertura_gedi = safe_number(d.get("gediPct")) * 100 if gedi_disponible else 0

    ndvi_delta_medio = d.get("ndviDeltaMedio")
    ndvi_disponible = not dato_ausente(ndvi_delta_medio)
    ndvi_delta_medio = safe_number(ndvi_delta_medio) if ndvi_disponible else None

    pp, ph, bl, bs = safe_number(r.get("pp")), safe_number(r.get("ph")), safe_number(r.get("bl")), safe_number(r.get("bs"))
    ge, pe, ee2, gv, gt = (
        safe_number(r.get("ge")), safe_number(r.get("pe")), safe_number(r.get("ee_")),
        safe_number(r.get("gv")), safe_number(r.get("gt")),
    )
    a1, a2, a4, a5, a7, a8, a11 = (
        safe_number(r.get("a1")), safe_number(r.get("a2")), safe_number(r.get("a4")),
        safe_number(r.get("a5")), safe_number(r.get("a7")), safe_number(r.get("a8")), safe_number(r.get("a11")),
    )
    te, td, tf, tr = safe_number(r.get("te")), safe_number(r.get("td")), safe_number(r.get("tf")), safe_number(r.get("tr"))

    pct_linea = (bl / area) * 100 if area > 0 else 0
    pct_post = (pp / area) * 100 if area > 0 else 0
    pct_libre = (bs / area) * 100 if area > 0 else 0
    pct_arb = (a2 / area) * 100 if area > 0 else 0
    pct_ganancia_esri = (ge / area) * 100 if area > 0 else 0
    pct_salida_esri = (pe / area) * 100 if area > 0 else 0
    pct_tmf_defor = (tf / area) * 100 if area > 0 else 0
    pct_tmf_degrad = (td / area) * 100 if area > 0 else 0

    esri_nom = nom_esri(dominante({"1": a1, "2": a2, "4": a4, "5": a5, "7": a7, "8": a8, "11": a11}))

    brecha_anios = abs(ANO_DIAG_TMF - anio_final_esri)
    advertencia_anios = ""
    if brecha_anios >= 3:
        advertencia_anios += (
            f" ⚠ El año JRC TMF ({ANO_DIAG_TMF}) y el año ESRI ({anio_final_esri}) difieren en "
            f"{brecha_anios} años; la consistencia entre capas puede estar afectada."
        )
    if es_analisis_cuenca:
        advertencia_anios += (
            " ⚠ El resumen de toda la cuenca es exploratorio: ESRI usa 30 m y Hansen/TMF se "
            "procesan conjuntamente para mejorar el rendimiento. Las fincas conservan 10 m para "
            "ESRI y proyecciones nativas separadas."
        )

    consist = evaluar_consistencia(area, td, tf, tr, pe, ge, pp)

    señal_tmf = (
        tf >= UMBRAL_REVISION_TMF_DEFOR_HA or pct_tmf_defor >= UMBRAL_PCT_TMF_DEFOR
        or td >= UMBRAL_REVISION_TMF_DEGRAD_HA or pct_tmf_degrad >= UMBRAL_PCT_TMF_DEGRAD
    )
    señal_esri = pe >= 0.10 and pct_salida_esri >= UMBRAL_PCT_ESRI_SALIDA
    señal_hansen = pp >= UMBRAL_ALERTA_HANSEN_HA
    señal_gedi = (
        gedi_disponible and pct_cobertura_gedi >= UMBRAL_COBERTURA_GEDI_PCT
        and alt < UMBRAL_DOSEL_BAJO_M and pct_linea >= 10
    )
    señal_ndvi = False  # NDVI: solo visual/informativo — sin ponderación, igual que en GEE

    puntaje_det = (
        (2 if señal_tmf else 0) + (2 if señal_hansen else 0) + (1.5 if señal_esri else 0)
        + (1 if señal_ndvi else 0) + (0.5 if señal_gedi else 0)
    )

    sem = evaluar_semaforo(pct_linea, td, tf, pe, ge, pp, consist, puntaje_det)
    accion = evaluar_accion(pct_linea, alt if gedi_disponible else 0, td, tf, tr, pe, ge, pp,
                             esri_nom, puntaje_det, señal_tmf, señal_esri, señal_hansen, señal_gedi, señal_ndvi)

    n_fuentes_det = sum([señal_tmf, señal_esri, señal_hansen, señal_gedi, señal_ndvi])
    n_fuentes_disp = 3 + (1 if gedi_disponible else 0)
    conf_nivel = "Alta" if puntaje_det >= 3 else "Media" if puntaje_det >= 1.5 else "Preventiva" if puntaje_det >= 0.5 else "Sin señal"

    fecha = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return {
        "nombre_area": nombre_area, "area": area, "fecha": fecha,
        "gedi_disponible": gedi_disponible, "alt": alt, "pct_cobertura_gedi": pct_cobertura_gedi,
        "ndvi_disponible": ndvi_disponible, "ndvi_delta_medio": ndvi_delta_medio,
        "ndvi_anio_ref": ndvi_anio_ref, "ano_ndvi_max": ANO_NDVI_MAX,
        "pp": pp, "ph": ph, "bl": bl, "bs": bs, "ge": ge, "pe": pe, "ee2": ee2, "gv": gv, "gt": gt,
        "te": te, "td": td, "tf": tf, "tr": tr,
        "pct_linea": pct_linea, "pct_post": pct_post, "pct_libre": pct_libre, "pct_arb": pct_arb,
        "pct_ganancia_esri": pct_ganancia_esri, "pct_salida_esri": pct_salida_esri,
        "esri_nom": esri_nom, "anio_tmf": ANO_DIAG_TMF, "anio_inicial_esri": anio_inicial_esri,
        "anio_final_esri": anio_final_esri, "advertencia_anios": advertencia_anios,
        "consist": consist, "sem": sem, "accion": accion,
        "señal_tmf": señal_tmf, "señal_esri": señal_esri, "señal_hansen": señal_hansen,
        "señal_gedi": señal_gedi, "señal_ndvi": señal_ndvi,
        "puntaje_det": puntaje_det, "n_fuentes_det": n_fuentes_det, "n_fuentes_disp": n_fuentes_disp,
        "conf_nivel": conf_nivel, "es_analisis_cuenca": es_analisis_cuenca,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 7. TEXTOS DEL REPORTE — versión técnica y versión en lenguaje llano
# Traducción directa de reporteDetalladoAnterior / reporteCompleto del GEE.
# ═══════════════════════════════════════════════════════════════════════════

def construir_reporte_tecnico(v):
    ndvi_linea = (
        f"Promedio de ΔNDVI ({v['ndvi_anio_ref']} → {v['ano_ndvi_max']}): {v['ndvi_delta_medio']:.3f} "
        "(informativo, no ponderado en el índice)."
        if v["ndvi_disponible"] else
        f"ΔNDVI ({v['ndvi_anio_ref']} → {v['ano_ndvi_max']}): sin imágenes limpias suficientes en el período (informativo, no ponderado)."
    )
    if v["gedi_disponible"]:
        gedi_linea = f"{v['alt']:.1f} m ({v['pct_cobertura_gedi']:.0f}% del área con datos)"
    else:
        gedi_linea = "sin datos válidos suficientes"
    return (
        "FICHA DE PREEVALUACIÓN TERRITORIAL POR FINCA\n"
        f"Unidad: {v['nombre_area']}  ·  Área: {v['area']:.1f} ha\n"
        f"Fecha: {v['fecha']}  ·  Zona: Fincas de monitoreo · Trinidad\n"
        f"Año diagnóstico JRC TMF: {v['anio_tmf']}  ·  Años ESRI: {v['anio_inicial_esri']}-{v['anio_final_esri']}\n\n"
        "1. RESUMEN TÉCNICO\n"
        f"{v['bl']:.1f} ha de cobertura arbórea del año 2000 que permaneció sin pérdida registrada hasta el "
        f"cierre de 2020 ({v['pct_linea']:.1f}% del área). Cobertura de árboles ESRI en {v['anio_final_esri']}: "
        f"{v['pct_arb']:.1f}% del área. Clase dominante ESRI {v['anio_final_esri']}: {v['esri_nom']}.\n\n"
        "2. RESULTADOS PRINCIPALES\n"
        f"JRC TMF ({v['anio_tmf']}): bosque estable {v['te']:.1f} ha · degradación {v['td']:.1f} ha · "
        f"deforestación {v['tf']:.1f} ha · recuperación {v['tr']:.1f} ha · ganancia de bosque {v['gt']:.1f} ha.\n"
        f"ESRI ({v['anio_inicial_esri']}-{v['anio_final_esri']}): ganancia árboles {v['ge']:.1f} ha · "
        f"ganancia veg. amplia {v['gv']:.1f} ha · salida árboles {v['pe']:.1f} ha · estables {v['ee2']:.1f} ha.\n"
        f"Hansen: pérdida post-{CUTOFF_LABEL} {v['pp']:.2f} ha ({v['pct_post']:.2f}%) · "
        f"pérdida histórica 2001-2020 {v['ph']:.2f} ha (contexto).\n"
        f"GEDI: {gedi_linea}.\n"
        f"{ndvi_linea}\n\n"
        "3. INTERPRETACIÓN INTEGRADA\n"
        f"Consistencia entre capas: {v['consist']['nivel']}. {v['consist']['texto']}\n\n"
        "4. PRIORIDAD DE REVISIÓN\n"
        f"{v['sem']['nivel']}. {v['sem']['texto']}{v['advertencia_anios']}\n\n"
        "5. ACCIÓN SUGERIDA\n"
        f"{v['accion']['texto']}\n\n"
        "6. NOTA METODOLÓGICA\n"
        "Resoluciones: ESRI 10 m (30 m si el área es toda la cuenca) · Hansen/JRC TMF 30 m · GEDI ~100 m. "
        "No se comparan píxeles exactos; se integran señales por área.\n"
        f"JRC TMF: clasificación del estado forestal en el año de diagnóstico ({v['anio_tmf']}), siempre el más "
        "reciente disponible — decisión deliberada para evitar sesgo de selección.\n"
        f"Hansen GFC: lossyear registra el año exacto de pérdida de cada píxel. Pérdida post-2020 "
        f"({v['pp']:.2f} ha) y pérdida histórica 2001-2020 ({v['ph']:.2f} ha) son acumulados.\n"
        f"ESRI LULC: comparación puntual entre {v['anio_inicial_esri']} y {v['anio_final_esri']}. Cambios en "
        "años intermedios no son detectados.\n"
        "ΔNDVI (Sentinel-2 SR, 10 m/px, máscara SCL): diferencia de NDVI entre medianas anuales. Solo "
        "informativo — no participa en el índice de prioridad. Puede estar ausente si hay alta nubosidad anual.\n"
        "Los umbrales usados son operativos, no legales.\n\n"
        "DIAGNÓSTICO POR FUENTE (resultados específicos de esta finca):\n"
        f"JRC TMF {v['anio_tmf']}: "
        + (f"⚠ Señal — deforestación {v['tf']:.1f} ha, degradación {v['td']:.1f} ha\n" if v["señal_tmf"]
           else "✓ Sin deforestación ni degradación relevante\n")
        + f"ESRI LULC {v['anio_final_esri']}: "
        + (f"⚠ Señal — salida de árboles {v['pe']:.1f} ha\n" if v["señal_esri"]
           else "✓ Sin salida de árboles que supere 0.10 ha y 5% del área\n")
        + "Hansen GFC: "
        + (f"⚠ Señal — pérdida post-{CUTOFF_LABEL}: {v['pp']:.2f} ha\n" if v["señal_hansen"]
           else f"✓ Sin pérdida arbórea posterior al {CUTOFF_LABEL}\n")
        + "GEDI dosel: "
        + (f"⚠ Señal contextual — dosel bajo {v['alt']:.1f} m\n" if v["señal_gedi"]
           else ("Sin datos válidos suficientes en el producto de altura\n" if not v["gedi_disponible"]
                 else f"Dosel {v['alt']:.1f} m\n"))
        + f"Fuentes con señal: {v['n_fuentes_det']} de {v['n_fuentes_disp']} · "
        f"Índice ponderado: {v['puntaje_det']:.1f}/6.0 · Prioridad: {v['conf_nivel']}\n"
        "(Pesos preliminares de criterio experto; no representan una probabilidad ni una conclusión legal.)\n\n"
        "NOTA FINAL: Esta es una preevaluación satelital para priorizar revisiones. No es un sistema "
        "validado, no determina cumplimiento EUDR y no sustituye verificación en campo, revisión "
        "documental ni asesoría legal."
    )


def construir_reporte_narrativo(v):
    prioridad = v["conf_nivel"] if v["conf_nivel"] != "Sin señal" else "Baja"
    descripcion_cobertura = (
        "mantiene una cobertura arbórea importante" if v["pct_arb"] >= 50
        else "presenta una cobertura arbórea limitada" if v["pct_arb"] < 20
        else "combina áreas arboladas y áreas productivas"
    )
    resultado_general = (
        "señales de pérdida o deterioro" if v["n_fuentes_det"] >= 2
        else "una señal localizada de cambio" if v["n_fuentes_det"] == 1
        else "ninguna señal relevante de deterioro reciente"
    )
    significado_cobertura = (
        "predominio de cobertura arbórea" if v["pct_arb"] >= 50
        else "presencia importante de árboles, aunque no dominante" if v["pct_arb"] >= 20
        else "baja presencia de cobertura arbórea"
    )
    coincidencia_fuentes = (
        "Dos o más fuentes independientes presentan señales en la misma dirección. Esta coincidencia "
        "aumenta la necesidad de revisar los sectores señalados." if v["n_fuentes_det"] >= 2
        else "Las fuentes no muestran el mismo resultado. Esto puede ocurrir cuando el cambio es pequeño, "
        "reciente, temporal o se encuentra en bordes de distintas coberturas. Se requiere interpretación "
        "adicional antes de concluir." if v["n_fuentes_det"] == 1
        else "Las fuentes evaluadas no muestran señales relevantes de deterioro reciente. El resultado debe "
        "mantenerse bajo seguimiento periódico."
    )
    gedi_suficiente = v["gedi_disponible"] and v["pct_cobertura_gedi"] >= UMBRAL_COBERTURA_GEDI_PCT
    if gedi_suficiente:
        clase_dosel = "vegetación joven o intervenida" if v["alt"] < 10 else "bosque secundario" if v["alt"] < 20 else "vegetación arbórea desarrollada"
        texto_dosel = f"La altura promedio del dosel fue de {v['alt']:.1f} metros. Este valor es compatible con {clase_dosel}."
    else:
        texto_dosel = "El producto de altura del dosel no presenta información suficiente para interpretar esta finca."
    lectura_paisaje = (
        "conserva una proporción importante de cobertura arbórea" if v["pct_arb"] >= 50
        else "presenta un paisaje mixto de áreas arboladas y productivas" if v["pct_arb"] >= 20
        else "tiene poca cobertura arbórea"
    )
    lectura_senales = (
        "Sin embargo, se identificaron varias señales coincidentes que podrían estar relacionadas con "
        "reducción de árboles, intervención del dosel o cambio de uso del suelo." if v["n_fuentes_det"] >= 2
        else "Sin embargo, se identificó una señal localizada que podría estar relacionada con reducción de "
        "árboles, intervención del dosel o cambio de uso del suelo." if v["n_fuentes_det"] == 1
        else "No se identificaron señales relevantes de deterioro reciente en las fuentes evaluadas."
    )
    donde_revisar = (
        "Las principales señales corresponden a los sectores con mayor cambio detectado por JRC TMF, "
        "ESRI y Hansen; conviene priorizarlos en la próxima recorrida." if v["n_fuentes_det"] > 0
        else "No se identificaron sectores que requieran revisión inmediata. Mantener una referencia para "
        "el seguimiento periódico."
    )
    accion_reporte = (
        "Incluir los sectores señalados como puntos prioritarios de la próxima visita de campo. Revisar "
        "imágenes recientes, registros de manejo y antecedentes de uso del suelo." if prioridad == "Alta"
        else "Realizar una revisión visual detallada de los sectores señalados y determinar si deben "
        "incorporarse a una visita de verificación." if prioridad == "Media"
        else "Mantener los sectores bajo seguimiento y comparar nuevamente los resultados durante la "
        "siguiente actualización del visor." if prioridad == "Preventiva"
        else "No se identificaron señales que requieran una acción inmediata. Mantener el monitoreo "
        "periódico de la finca."
    )
    resultado_conclusion = (
        "una coincidencia entre varias señales" if v["n_fuentes_det"] >= 2
        else "una señal aislada" if v["n_fuentes_det"] == 1
        else "ausencia de cambios relevantes"
    )

    return (
        "FICHA DE PREEVALUACIÓN TERRITORIAL\n\n"
        f"Finca evaluada: {v['nombre_area']}\n"
        f"Superficie total: {v['area']:.1f} ha\n"
        f"Fecha del análisis: {v['fecha'][:10]}\n\n"
        "RESULTADO GENERAL\n\n"
        f"PRIORIDAD {prioridad.upper()} DE REVISIÓN\n\n"
        f"La finca {descripcion_cobertura}. El análisis identificó {resultado_general}.\n\n"
        "Este resultado no confirma por sí solo que haya ocurrido deforestación. Su función es indicar si "
        "existen sectores que deben revisarse con mayor detalle.\n\n"
        "¿QUÉ SE ENCONTRÓ?\n\n"
        "1. Estado actual de la cobertura\n\n"
        f"En el año más reciente analizado se identificó una cobertura de árboles equivalente a "
        f"aproximadamente {v['pct_arb']:.1f}% de la finca, lo que significa que la finca presenta "
        f"{significado_cobertura}.\n\n"
        "2. Cambios que requieren atención\n\n"
        f"Se identificaron aproximadamente {v['pe']:.1f} hectáreas donde la cobertura clasificada como "
        f"árboles pasó a otra cobertura entre {v['anio_inicial_esri']} y {v['anio_final_esri']} "
        f"({v['pct_salida_esri']:.1f}% del área). En la dirección opuesta, {v['ge']:.1f} hectáreas pasaron "
        f"de otra cobertura a la clase árboles ({v['pct_ganancia_esri']:.1f}%). Además, las alertas de "
        f"pérdida arbórea registraron {v['pp']:.2f} hectáreas de cambio después del {CUTOFF_LABEL}.\n\n"
        f"{coincidencia_fuentes}\n\n"
        "3. Condición del bosque y la vegetación\n\n"
        "El análisis forestal identificó:\n\n"
        f"- {v['te']:.1f} ha de bosque que se mantiene estable.\n"
        f"- {v['td']:.1f} ha con posibles señales de degradación.\n"
        f"- {v['tf']:.1f} ha clasificadas con señales de deforestación.\n"
        f"- {v['tr']:.1f} ha con señales de recuperación.\n\n"
        f"{texto_dosel}\n\n"
        "¿QUÉ SIGNIFICAN ESTOS RESULTADOS?\n\n"
        f"La finca {lectura_paisaje}. {lectura_senales}\n\n"
        "Las imágenes satelitales permiten reconocer dónde pudo ocurrir un cambio, pero no permiten "
        "establecer automáticamente su causa. El cambio podría corresponder a manejo productivo, cosecha "
        "de plantaciones, limpieza de áreas, regeneración, nubosidad residual o una modificación real de "
        "la cobertura forestal.\n\n"
        "¿DÓNDE SE DEBE REVISAR?\n\n"
        f"{donde_revisar}\n\n"
        "ACCIÓN RECOMENDADA\n\n"
        f"{accion_reporte}\n\n"
        "CONCLUSIÓN DE LA PREEVALUACIÓN\n\n"
        f"La finca presenta una prioridad {prioridad.lower()} de revisión. El resultado indica "
        f"{resultado_conclusion}. La decisión final debe complementarse con información del productor, "
        "documentación del predio, imágenes recientes y verificación de campo cuando corresponda.\n\n"
        "INFORMACIÓN TÉCNICA DE RESPALDO\n\n"
        "La preevaluación integró información sobre estado del bosque, pérdida anual de cobertura arbórea, "
        "cambios de uso y cobertura del suelo y altura del dosel. Las fuentes utilizadas fueron JRC Tropical "
        "Moist Forest, Hansen Global Forest Change, ESRI Land Use/Land Cover y el producto de altura del "
        "dosel basado en GEDI.\n\n"
        "Los resultados corresponden a una preevaluación territorial. No constituyen una certificación, "
        "una determinación legal ni una confirmación definitiva de deforestación."
    )


# ═══════════════════════════════════════════════════════════════════════════
# 8. GENERACIÓN DE PDF — reportlab.platypus (multi-página)
# ═══════════════════════════════════════════════════════════════════════════

def generar_pdf(v):
    memoria = BytesIO()
    doc = SimpleDocTemplate(
        memoria, pagesize=A4,
        topMargin=1.8 * cm, bottomMargin=1.8 * cm, leftMargin=2 * cm, rightMargin=2 * cm,
        title="Ficha de preevaluación territorial",
    )
    estilos = getSampleStyleSheet()
    titulo = ParagraphStyle("TituloFicha", parent=estilos["Title"], fontSize=16, spaceAfter=6)
    subtitulo = ParagraphStyle("Subtitulo", parent=estilos["Normal"], fontSize=9, textColor=colors.grey, spaceAfter=12)
    h2 = ParagraphStyle("H2", parent=estilos["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4)
    cuerpo = ParagraphStyle("Cuerpo", parent=estilos["Normal"], fontSize=9.5, leading=13.5, spaceAfter=6)
    nota = ParagraphStyle("Nota", parent=estilos["Normal"], fontSize=8, textColor=colors.HexColor("#555555"), leading=11)

    color_prioridad = {
        "Alta": colors.HexColor("#7a0000"), "Media": colors.HexColor("#5c3000"),
        "Preventiva": colors.HexColor("#4a3a00"), "Baja": colors.HexColor("#1a3d10"),
        "Sin señal": colors.HexColor("#1a3d10"),
    }.get(v["conf_nivel"], colors.grey)

    elementos = []

    def parrafos_multilinea(texto, estilo):
        return [Paragraph(linea if linea.strip() else "&nbsp;", estilo) for linea in texto.split("\n")]

    # ── Portada / resumen ────────────────────────────────────────────────
    elementos.append(Paragraph("FICHA DE PREEVALUACIÓN TERRITORIAL", titulo))
    elementos.append(Paragraph(
        f"Pasantía profesional · Universidad Tecnológica de Panamá &nbsp;·&nbsp; Fundación NATURA · "
        f"Versión {VERSION_VISOR}", subtitulo,
    ))
    elementos.append(Paragraph(
        f"<b>Unidad evaluada:</b> {v['nombre_area']} &nbsp;·&nbsp; "
        f"<b>Superficie:</b> {v['area']:,.2f} ha &nbsp;·&nbsp; <b>Fecha:</b> {v['fecha']}",
        cuerpo,
    ))

    tabla_prioridad = Table(
        [[Paragraph(f"<b>{v['sem']['titulo']}</b>", ParagraphStyle("p", parent=cuerpo, textColor=colors.white))],
         [Paragraph(
             f"Índice de prioridad: {v['puntaje_det']:.1f}/6.0 · TMF(2) + Hansen(2) + ESRI(1.5) + GEDI(0.5) · ΔNDVI: solo informativo",
             ParagraphStyle("p2", parent=cuerpo, textColor=colors.HexColor("#ffe082"), fontSize=8.5),
         )],
         [Paragraph(v["sem"]["texto"] + v.get("advertencia_anios", ""),
                    ParagraphStyle("p3", parent=cuerpo, textColor=colors.white))],
         [Paragraph(f"→ {v['accion']['texto']}",
                    ParagraphStyle("p4", parent=cuerpo, textColor=colors.HexColor("#a8e6a0")))]],
        colWidths=[16 * cm],
    )
    tabla_prioridad.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color_prioridad),
        ("BOX", (0, 0), (-1, -1), 1, colors.white),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
    ]))
    elementos.append(Spacer(1, 6))
    elementos.append(tabla_prioridad)
    elementos.append(Spacer(1, 10))

    # ── Tabla de métricas por fuente ─────────────────────────────────────
    elementos.append(Paragraph("Métricas del área evaluada", h2))
    filas_metricas = [
        ["Fuente", "Métrica", "Valor"],
        ["Hansen GFC", f"Pérdida post-{CUTOFF_LABEL}", f"{v['pp']:.2f} ha ({v['pct_post']:.2f}%)"],
        ["Hansen GFC", "Pérdida histórica 2001–2020", f"{v['ph']:.2f} ha"],
        ["Hansen GFC", "Cob. arbórea 2000 persistente a 2020", f"{v['bl']:.1f} ha ({v['pct_linea']:.1f}%)"],
        ["Hansen GFC", "Sin pérdida 2001–2025", f"{v['bs']:.1f} ha ({v['pct_libre']:.1f}%)"],
        ["JRC TMF", f"Bosque estable ({v['anio_tmf']})", f"{v['te']:.1f} ha"],
        ["JRC TMF", "Degradación", f"{v['td']:.1f} ha"],
        ["JRC TMF", "Deforestación", f"{v['tf']:.1f} ha"],
        ["JRC TMF", "Recuperación", f"{v['tr']:.1f} ha"],
        ["ESRI LULC", f"Árboles ({v['anio_final_esri']})", f"{v['pct_arb']:.1f}% del área"],
        ["ESRI LULC", "Clase dominante", v["esri_nom"]],
        ["ESRI LULC", "No árbol → árboles", f"{v['ge']:.1f} ha"],
        ["ESRI LULC", "Árboles → no árbol", f"{v['pe']:.1f} ha"],
        ["GEDI", "Dosel promedio",
         f"{v['alt']:.1f} m ({v['pct_cobertura_gedi']:.0f}% con datos)" if v["gedi_disponible"] else "Sin datos suficientes"],
        ["NDVI (informativo)", f"ΔNDVI {v['ndvi_anio_ref']}→{v['ano_ndvi_max']}",
         f"{v['ndvi_delta_medio']:.3f}" if v["ndvi_disponible"] else "Sin imágenes limpias suficientes"],
    ]
    tabla = Table(filas_metricas, colWidths=[3.5 * cm, 8 * cm, 4.5 * cm], repeatRows=1)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3a1a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elementos.append(tabla)
    elementos.append(Spacer(1, 8))

    resumen_fuentes = (
        f"{v['n_fuentes_det']} de {v['n_fuentes_disp']} fuentes satelitales detectan alguna señal "
        f"relevante en esta finca (índice ponderado {v['puntaje_det']:.1f}/6.0, prioridad {v['conf_nivel']})."
    )
    elementos.append(Paragraph(resumen_fuentes, cuerpo))

    # ── Parte 1: reporte técnico ─────────────────────────────────────────
    elementos.append(PageBreak())
    elementos.append(Paragraph("Parte 1 · Reporte técnico", h2))
    reporte_tecnico = construir_reporte_tecnico(v)
    for bloque in reporte_tecnico.split("\n\n"):
        elementos.append(Paragraph(bloque.replace("\n", "<br/>"), cuerpo))

    # ── Parte 2: reporte en lenguaje llano ───────────────────────────────
    elementos.append(PageBreak())
    elementos.append(Paragraph("Parte 2 · Ficha en lenguaje llano", h2))
    reporte_narrativo = construir_reporte_narrativo(v)
    for bloque in reporte_narrativo.split("\n\n"):
        elementos.append(Paragraph(bloque.replace("\n", "<br/>"), cuerpo))

    elementos.append(Spacer(1, 10))
    elementos.append(Paragraph(
        "Nota metodológica: ESRI 10 m (30 m si el área es toda la cuenca) · Hansen/JRC TMF 30 m · GEDI "
        "~100 m. No se comparan píxeles exactos; se integran señales por área. Los umbrales son operativos, "
        "no legales. Esta es una preevaluación satelital. No sustituye validación en campo ni revisión "
        "documental.", nota,
    ))

    doc.build(elementos)
    memoria.seek(0)
    return memoria.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# 9. INTERFAZ STREAMLIT
# ═══════════════════════════════════════════════════════════════════════════

st.title("Visor de preevaluación territorial")
st.info(
    "Prototipo para identificar señales y priorizar revisiones. No determina cumplimiento EUDR. "
    "Integra Hansen GFC, JRC TMF, ESRI LULC, GEDI y NDVI (Sentinel-2, solo informativo)."
)

try:
    iniciar_earth_engine()
    cuenca = ee.FeatureCollection(ASSET_CUENCA)
    fincas = ee.FeatureCollection(ASSET_FINCAS)

    st.sidebar.header("Área de análisis")
    tipo_area = st.sidebar.radio("Seleccione el área:", ["Toda la cuenca", "Finca de monitoreo"])

    if tipo_area == "Finca de monitoreo":
        ids_fincas = obtener_ids_fincas()
        finca_seleccionada = st.sidebar.selectbox("Seleccione la finca:", ids_fincas, format_func=lambda v: str(v))
        area_seleccionada = fincas.filter(ee.Filter.eq("FincaID", finca_seleccionada))
        nombre_area = f"Finca {finca_seleccionada}"
    else:
        area_seleccionada = cuenca
        nombre_area = "Cuenca hidrográfica de interés"

    es_analisis_cuenca = tipo_area == "Toda la cuenca"

    st.sidebar.header("Parámetros de análisis")
    st.sidebar.caption(
        f"El año de diagnóstico JRC TMF es siempre el más reciente disponible ({ANO_DIAG_TMF}) — "
        "no es seleccionable, para evitar sesgo de confirmación."
    )
    anios_esri_ini = list(range(ANO_ESRI_MIN, ANO_ESRI_MAX))
    anio_inicial_esri = st.sidebar.selectbox("Año inicial ESRI LULC:", anios_esri_ini, index=0)
    anios_esri_fin = [a for a in range(ANO_ESRI_MIN + 1, ANO_ESRI_MAX + 1) if a > anio_inicial_esri]
    anio_final_esri = st.sidebar.selectbox("Año final ESRI LULC:", anios_esri_fin, index=len(anios_esri_fin) - 1)

    anios_ndvi = list(range(ANO_NDVI_MAX - 5, ANO_NDVI_MAX))
    ndvi_anio_ref = st.sidebar.selectbox(
        "Año inicial ΔNDVI (informativo):", anios_ndvi, index=len(anios_ndvi) - 3,
        help="El año final del ΔNDVI es siempre el más reciente disponible. Períodos largos (>5 años) "
             "pueden incluir variación climática y no solo deterioro real.",
    )

    superficie_ha = area_seleccionada.geometry().area(1).divide(10000).getInfo()

    columna_1, columna_2 = st.columns(2)
    with columna_1:
        st.metric("Área seleccionada", nombre_area)
    with columna_2:
        st.metric("Superficie aproximada", f"{superficie_ha:,.1f} ha")

    mapa = folium.Map(location=[8.7, -80.0], zoom_start=8, tiles=None, control_scale=True)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Imagen satelital", overlay=False, control=True, max_zoom=20,
    ).add_to(mapa)

    imagen_cuenca = cuenca.style(color="FF4444", fillColor="00000000", width=3)
    imagen_fincas = fincas.style(color="CC55FF", fillColor="00000000", width=2)
    imagen_seleccion = area_seleccionada.style(color="00FFFF", fillColor="00FFFF22", width=4)

    agregar_capa_ee(mapa, imagen_cuenca, {}, "Límite de la cuenca", mostrar=True)
    agregar_capa_ee(mapa, imagen_fincas, {}, "Fincas de monitoreo", mostrar=(tipo_area == "Finca de monitoreo"))
    agregar_capa_ee(mapa, imagen_seleccion, {}, "Área seleccionada", mostrar=True)

    mapa.fit_bounds(obtener_limites(area_seleccionada))
    folium.LayerControl(collapsed=False).add_to(mapa)

    st.subheader("Mapa del área evaluada")
    st_folium(mapa, width=1200, height=550, returned_objects=[], key=f"mapa-{tipo_area}-{nombre_area}")

    st.divider()
    st.subheader("Diagnóstico integrado")
    st.caption(
        "Combina cinco fuentes satelitales. 🔴 Alta: dos o más fuentes convergen — visita de campo "
        "prioritaria. 🟡 Media/Preventiva: revisar antes de actuar. 🟢 Sin señales: continuar monitoreo anual."
    )

    ejecutar = st.button("Ejecutar análisis", type="primary")

    if ejecutar:
        with st.spinner("Calculando diagnóstico integrado (Hansen, JRC TMF, ESRI, GEDI, NDVI)..."):
            geom = area_seleccionada.geometry(ee.ErrorMargin(1))
            d = ejecutar_diagnostico(geom, anio_inicial_esri, anio_final_esri, ndvi_anio_ref, es_analisis_cuenca)
            resultado = procesar_resultado(d, nombre_area, anio_inicial_esri, anio_final_esri, ndvi_anio_ref, es_analisis_cuenca)
        st.session_state["resultado"] = resultado

    resultado = st.session_state.get("resultado")

    if resultado and "error" in resultado:
        st.error(resultado["error"])
    elif resultado:
        v = resultado
        color_badge = {"Alta": "🔴", "Media": "🟡", "Preventiva": "🟡", "Baja": "🟢", "Sin señal": "🟢"}.get(v["conf_nivel"], "⚪")
        st.markdown(f"### {color_badge} {v['sem']['titulo']}")
        st.markdown(
            f"**Índice de prioridad:** {v['puntaje_det']:.1f}/6.0 &nbsp;·&nbsp; "
            f"TMF(2) + Hansen(2) + ESRI(1.5) + GEDI(0.5) &nbsp;·&nbsp; ΔNDVI: solo informativo"
        )
        st.write(v["sem"]["texto"] + v.get("advertencia_anios", ""))
        st.write(f"→ {v['accion']['texto']}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric(f"Pérd. Hansen post-{CUTOFF_LABEL}", f"{v['pp']:.2f} ha", f"{v['pct_post']:.2f}% del área")
        c2.metric("Deforestación JRC TMF", f"{v['tf']:.1f} ha")
        c3.metric("Degradación JRC TMF", f"{v['td']:.1f} ha")
        c4.metric("Salida árboles ESRI", f"{v['pe']:.1f} ha", f"{v['pct_salida_esri']:.1f}% del área")

        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Cob. arbórea persistente", f"{v['pct_linea']:.1f}%")
        c6.metric("Árboles ESRI (año final)", f"{v['pct_arb']:.1f}%")
        c7.metric("Dosel GEDI", f"{v['alt']:.1f} m" if v["gedi_disponible"] else "Sin datos")
        c8.metric("Consistencia entre capas", v["consist"]["nivel"])

        st.caption(
            f"{v['n_fuentes_det']} de {v['n_fuentes_disp']} fuentes satelitales detectan señal en esta finca. "
            f"ΔNDVI {v['ndvi_anio_ref']}→{v['ano_ndvi_max']}: "
            + (f"{v['ndvi_delta_medio']:.3f} (informativo)" if v["ndvi_disponible"] else "sin imágenes limpias suficientes")
        )

        with st.expander("Reporte técnico completo"):
            st.text(construir_reporte_tecnico(v))
        with st.expander("Ficha en lenguaje llano (para el productor)"):
            st.text(construir_reporte_narrativo(v))

        archivo_pdf = generar_pdf(v)
        st.download_button(
            label="Descargar ficha PDF completa",
            data=archivo_pdf,
            file_name="ficha_preevaluacion_territorial.pdf",
            mime="application/pdf",
            type="primary",
        )
    else:
        st.info('Presiona "Ejecutar análisis" para calcular el diagnóstico integrado de esta área.')

except Exception as error:
    st.error("No fue posible cargar el visor territorial.")
    st.exception(error)
