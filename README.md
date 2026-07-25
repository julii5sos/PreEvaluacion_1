# Visor de preevaluación territorial

Prototipo de preevaluación satelital y priorización de verificaciones de campo, desarrollado en el marco de una pasantía profesional (Universidad Tecnológica de Panamá) para la Fundación NATURA.

> **Alcance.** Esta herramienta es un prototipo de apoyo a la priorización de revisiones. No es un sistema validado, no certifica cumplimiento del Reglamento EUDR y no sustituye debida diligencia, revisión documental, verificación de campo ni asesoría legal.

## Qué hace

Integra cinco fuentes satelitales sobre la cuenca hidrográfica de interés y las fincas de monitoreo (Trinidad):

- **Hansen Global Forest Change** — pérdida arbórea anual (2001–2025), separada en pre- y post-corte EUDR (31/12/2020).
- **JRC Tropical Moist Forest (TMF)** — estado del bosque tropical húmedo (estable, degradación, deforestación, recuperación).
- **ESRI Land Use/Land Cover (10 m)** — uso y cobertura del suelo, comparación entre dos años.
- **GEDI Canopy Height** — altura del dosel (contexto estructural).
- **NDVI (Sentinel-2)** — cambio de vigor vegetal entre dos años. Solo informativo: no se pondera en el índice.

Combina las señales en un índice de prioridad ponderado (TMF y Hansen pesan más porque detectan un evento fechado; ESRI pesa menos porque solo compara dos instantes; GEDI es contexto estructural, no evento) y genera un reporte descargable en PDF con dos secciones: una técnica y otra en lenguaje llano para el productor.

## Requisitos

- Python 3.10+
- Una cuenta de servicio de Google Earth Engine con acceso a los assets usados (ver más abajo)
- Los siguientes paquetes (agregar a `requirements.txt`):

```
streamlit
streamlit-folium
folium
earthengine-api
google-auth
reportlab
```

## Configuración de secretos

El script lee las credenciales desde `st.secrets`. Crear un archivo `.streamlit/secrets.toml` (local) o configurar los "Secrets" del despliegue (Streamlit Community Cloud) con:

```toml
EE_PROJECT = "nombre-del-proyecto-de-earth-engine"
EE_SERVICE_ACCOUNT_JSON = '''
{
  "type": "service_account",
  "project_id": "...",
  "private_key_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n",
  "client_email": "...",
  ...
}
'''
```

La cuenta de servicio debe tener acceso de lectura a los assets de Earth Engine listados abajo, y el proyecto debe tener habilitada la API de Earth Engine.

## Ejecutar localmente

```bash
pip install -r requirements.txt
streamlit run visor_preevaluacion_territorial.py
```

## Assets y datasets usados

| Fuente | Asset / Colección | Notas |
|---|---|---|
| Cuenca hidrográfica | `projects/ee-julissaguevaravega/assets/CuencasHidrograficadeInteres` | ⚠ cuenta personal — mover a proyecto institucional de NATURA antes de publicar |
| Fincas Trinidad | `projects/ee-julissaguevaravega/assets/FincasTrinidadv1` | ⚠ cuenta personal — mover a proyecto institucional de NATURA antes de publicar |
| Hansen GFC | `UMD/hansen/global_forest_change_2025_v1_13` | actualizar versión cuando UMD publique el nuevo año (~octubre) |
| JRC TMF | `projects/JRC/TMF/v1_2025/AnnualChanges` | actualizar año cuando el JRC publique el nuevo año (~junio) |
| ESRI LULC | `projects/sat-io/open-datasets/landcover/ESRI_Global-LULC_10m_TS` | disponible 2017–2024 |
| GEDI Canopy Height | `users/openforisearthmap/World_EarthMap/CanopyHeight_GEDI_V27` | snapshot estructural, no detecta cambios recientes |
| NDVI | `COPERNICUS/S2_SR_HARMONIZED` | calculado en el propio script (mediana anual + máscara SCL) |

## Mantenimiento anual

Todo lo que cambia con el tiempo está centralizado al inicio del script (`visor_preevaluacion_territorial.py`, sección de configuración): versiones de Hansen y JRC TMF, años máximos/mínimos de cada dataset, y la fecha de corte EUDR (esta última no debe modificarse, es un dato legal fijo). Actualizar esas constantes cada año es suficiente; el resto del código no requiere cambios.

## Limitaciones conocidas

- Los assets de la cuenca y las fincas están bajo una cuenta personal de Earth Engine; si se desactiva, el visor deja de funcionar.
- El NDVI es puramente informativo: no participa en el índice de prioridad ni en las alertas.
- El análisis de "toda la cuenca" usa una escala más gruesa (30 m) para ESRI por rendimiento; las fincas individuales se calculan a 10 m.
- Los umbrales de alerta son operativos (para priorizar visitas de campo), no definiciones legales.
- El mapa no incluye comparador de barrido (swipe) ni herramienta de dibujo de polígonos — a diferencia de la versión publicada como GEE App, esta versión Streamlit se limita a fincas predefinidas y a toda la cuenca.
