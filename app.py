from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import fitz
import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile
from contextlib import asynccontextmanager, suppress
import asyncio
import qrcode
from io import BytesIO
import random

# ------------ CONFIG ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
OUTPUT_DIR = "documentos"
PLANTILLA_OAXACA = "oaxaca_plantilla_imagen.pdf"
PLANTILLA_OAXACA_SEGUNDA = "oaxacaverga.pdf"

# URL de consulta base (sin /consulta_folio al final)
URL_CONSULTA_BASE = "https://oaxaca-gob-semovi.onrender.com"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ------------ SUPABASE ------------
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------ BOT ------------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ------------ FOLIO OAXACA ------------
folio_counter = {"count": 769}
def nuevo_folio() -> str:
    folio = f"1{folio_counter['count']}"
    folio_counter["count"] += 1
    return folio

# ------------ FSM STATES ------------
class PermisoForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()

# ------------ COORDENADAS OAXACA (SIN FOLIO TEXTO) ------------
coords_oaxaca = {
    # ELIMINAMOS "folio" porque ahora será QR dinámico
    "fecha1": (168,130,12,(0,0,0)),
    "fecha2": (140,540,10,(0,0,0)),
    "marca": (50,215,12,(0,0,0)),
    "serie": (200,258,12,(0,0,0)),
    "linea": (200,215,12,(0,0,0)),
    "motor": (360,258,12,(0,0,0)),
    "anio": (360,215,12,(0,0,0)),
    "color": (50,258,12,(0,0,0)),
    "vigencia": (410,130,12,(0,0,0)),
    "nombre": (133,149,10,(0,0,0)),
}

# COORDENADAS PARA EL QR DINÁMICO (DONDE ANTES ESTABA EL TEXTO DEL FOLIO)
coords_qr_dinamico = {
    "x": 553,      # Misma X donde estaba el texto del folio
    "y": 76,       # Ajustada para centrar el QR donde estaba el texto
    "ancho": 40,   # Tamaño apropiado para que sea visible pero no muy grande
    "alto": 40     # Mismo alto que ancho para mantener proporción
}

coords_oaxaca_segunda = {
    "fecha_exp": (136, 141, 10, (0,0,0)),
    "numero_serie": (136, 166, 10, (0,0,0)),
    "hora": (146, 206, 10, (0,0,0)),
}

# ------------ FUNCIÓN QR DINÁMICO MEJORADA ------------
def generar_qr_dinamico_oaxaca(folio):
    """
    Genera QR compacto y optimizado para insertar donde estaba el texto
    """
    try:
        url_directa = f"{URL_CONSULTA_BASE}/consulta/{folio}"
        
        qr = qrcode.QRCode(
            version=1,  # Versión más pequeña para menor tamaño
            error_correction=qrcode.constants.ERROR_CORRECT_L,  # Menor corrección = menor tamaño
            box_size=3,  # Tamaño de caja más pequeño
            border=1     # Borde mínimo
        )
        qr.add_data(url_directa)
        qr.make(fit=True)

        img_qr = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        
        print(f"[QR DINÁMICO] Generado para folio {folio} -> {url_directa}")
        print(f"[POSICIÓN] X:{coords_qr_dinamico['x']}, Y:{coords_qr_dinamico['y']}")
        return img_qr, url_directa
        
    except Exception as e:
        print(f"[ERROR QR] {e}")
        return None, None

# ------------ GENERACIÓN PDF OAXACA CON QR EN LUGAR DEL TEXTO ------------
def generar_pdf_oaxaca_completo(folio, datos, fecha_exp, fecha_ven):
    """
    Genera AMBAS plantillas de Oaxaca con QR dinámico REEMPLAZANDO el texto del folio
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    doc_original = fitz.open(PLANTILLA_OAXACA)
    pg1 = doc_original[0]
    
    # ❌ YA NO insertamos texto del folio aquí - ELIMINADO COMPLETAMENTE
    
    # ✅ SOLO insertamos el resto de datos (fechas, vehículo, etc.)
    f1 = fecha_exp.strftime("%d/%m/%Y")
    f_ven = fecha_ven.strftime("%d/%m/%Y")
    
    pg1.insert_text(coords_oaxaca["fecha1"][:2], f1, 
                    fontsize=coords_oaxaca["fecha1"][2], 
                    color=coords_oaxaca["fecha1"][3])
    pg1.insert_text(coords_oaxaca["fecha2"][:2], f1, 
                    fontsize=coords_oaxaca["fecha2"][2], 
                    color=coords_oaxaca["fecha2"][3])

    # Insertar datos del vehículo
    for key in ["marca", "serie", "linea", "motor", "anio", "color"]:
        if key in datos:
            x, y, s, col = coords_oaxaca[key]
            pg1.insert_text((x, y), datos[key], fontsize=s, color=col)

    pg1.insert_text(coords_oaxaca["vigencia"][:2], f_ven, 
                    fontsize=coords_oaxaca["vigencia"][2], 
                    color=coords_oaxaca["vigencia"][3])
    pg1.insert_text(coords_oaxaca["nombre"][:2], datos.get("nombre", ""), 
                    fontsize=coords_oaxaca["nombre"][2], 
                    color=coords_oaxaca["nombre"][3])

    # 🔥 AQUÍ ESTÁ LA CLAVE: INSERTAR QR DINÁMICO EN LUGAR DEL TEXTO
    img_qr, url_qr = generar_qr_dinamico_oaxaca(folio)
    
    if img_qr:
        buf = BytesIO()
        img_qr.save(buf, format="PNG")
        buf.seek(0)
        qr_pix = fitz.Pixmap(buf.read())

        # USAR LAS COORDENADAS EXACTAS DONDE ESTABA EL TEXTO DEL FOLIO
        x_qr = coords_qr_dinamico["x"]
        y_qr = coords_qr_dinamico["y"] 
        ancho_qr = coords_qr_dinamico["ancho"]
        alto_qr = coords_qr_dinamico["alto"]

        pg1.insert_image(
            fitz.Rect(x_qr, y_qr, x_qr + ancho_qr, y_qr + alto_qr),
            pixmap=qr_pix,
            overlay=True
        )
        
        print(f"[QR INSERTADO] Folio {folio} en posición ({x_qr}, {y_qr})")
        print(f"[URL QR] {url_qr}")
        print(f"[DIMENSIONES QR] {ancho_qr}x{alto_qr}")
    else:
        # Si falla el QR, insertar al menos el folio como texto de respaldo
        print(f"[FALLBACK] Error generando QR, insertando texto del folio")
        pg1.insert_text((coords_qr_dinamico["x"], coords_qr_dinamico["y"] + 15), 
                       folio, fontsize=12, color=(1,0,0))
    
    # Procesar segunda plantilla (sin cambios)
    doc_segunda = fitz.open(PLANTILLA_OAXACA_SEGUNDA)
    pg2 = doc_segunda[0]
    
    pg2.insert_text(coords_oaxaca_segunda["fecha_exp"][:2], 
                    fecha_exp.strftime("%d/%m/%Y"), 
                    fontsize=coords_oaxaca_segunda["fecha_exp"][2])
    
    pg2.insert_text(coords_oaxaca_segunda["numero_serie"][:2], 
                    datos.get("serie", ""), 
                    fontsize=coords_oaxaca_segunda["numero_serie"][2])
    
    pg2.insert_text(coords_oaxaca_segunda["hora"][:2], 
                    fecha_exp.strftime("%H:%M:%S"), 
                    fontsize=coords_oaxaca_segunda["hora"][2])
    
    # Combinar ambas plantillas
    doc_final = fitz.open()
    doc_final.insert_pdf(doc_original)
    doc_final.insert_pdf(doc_segunda)
    
    salida = os.path.join(OUTPUT_DIR, f"{folio}_oaxaca_completo.pdf")
    doc_final.save(salida)
    
    doc_original.close()
    doc_segunda.close()
    doc_final.close()
    
    return salida

# ------------ HANDLERS OAXACA ------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🌮 ¡Órale! Sistema Digital de Permisos OAXACA.\n"
        "Aquí se trabaja en serio y sin mamadas, compadre.\n\n"
        "🚗 Usa /permiso para tramitar tu documento oficial de Oaxaca.\n\n"
        "✨ NOVEDAD: Ahora con QR inteligente que va directo al estado de tu permiso."
    )

@dp.message(Command("permiso"))
async def permiso_cmd(message: types.Message, state: FSMContext):
    await message.answer(
        "🚗 Vamos a generar tu permiso de OAXACA.\n"
        "Primero escribe la MARCA del vehículo:"
    )
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    marca = message.text.strip().upper()
    await state.update_data(marca=marca)
    await message.answer(
        f"✅ MARCA: {marca} - Registrado.\n\n"
        "Ahora la LÍNEA del vehículo:"
    )
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    linea = message.text.strip().upper()
    await state.update_data(linea=linea)
    await message.answer(
        f"✅ LÍNEA: {linea} - Anotado.\n\n"
        "El AÑO del vehículo (4 dígitos):"
    )
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer(
            "⚠️ El año debe ser de 4 dígitos (ej: 2020).\n"
            "Inténtelo de nuevo:"
        )
        return
    
    await state.update_data(anio=anio)
    await message.answer(
        f"✅ AÑO: {anio} - Confirmado.\n\n"
        "NÚMERO DE SERIE del vehículo:"
    )
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    serie = message.text.strip().upper()
    if len(serie) < 5:
        await message.answer(
            "⚠️ El número de serie parece muy corto.\n"
            "Revise bien y escriba el número completo:"
        )
        return
        
    await state.update_data(serie=serie)
    await message.answer(
        f"✅ SERIE: {serie} - En el sistema.\n\n"
        "NÚMERO DE MOTOR:"
    )
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    motor = message.text.strip().upper()
    await state.update_data(motor=motor)
    await message.answer(
        f"✅ MOTOR: {motor} - Capturado.\n\n"
        "COLOR del vehículo:"
    )
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    color = message.text.strip().upper()
    await state.update_data(color=color)
    await message.answer(
        f"✅ COLOR: {color} - Registrado.\n\n"
        "Por último, el NOMBRE COMPLETO del solicitante:"
    )
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    nombre = message.text.strip().upper()
    datos["nombre"] = nombre
    datos["folio"] = nuevo_folio()

    hoy = datetime.now()
    fecha_ven = hoy + timedelta(days=30)

    await message.answer(
        f"🔄 PROCESANDO PERMISO DE OAXACA...\n"
        f"Folio: {datos['folio']}\n"
        f"Titular: {nombre}\n\n"
        "🆕 Generando con QR dinámico inteligente..."
    )

    try:
        pdf_path = generar_pdf_oaxaca_completo(datos['folio'], datos, hoy, fecha_ven)

        await message.answer_document(
            FSInputFile(pdf_path),
            caption=f"📋 PERMISO OFICIAL OAXACA CON QR INTELIGENTE\n"
                   f"Folio: {datos['folio']}\n"
                   f"Vigencia: 30 días\n"
                   f"🔗 QR incluido - escanear para ver estado automáticamente\n"
                   f"🌮 Documento completo con ambas plantillas"
        )

        supabase.table("folios_registrados").insert({
            "folio": datos["folio"],
            "marca": datos["marca"],
            "linea": datos["linea"],
            "anio": datos["anio"],
            "numero_serie": datos["serie"],
            "numero_motor": datos["motor"],
            "nombre": datos["nombre"],
            "color": datos["color"],
            "fecha_expedicion": hoy.date().isoformat(),
            "fecha_vencimiento": fecha_ven.date().isoformat(),
            "entidad": "oaxaca",
        }).execute()

        await message.answer(
            f"🎯 PERMISO DE OAXACA GENERADO CON ÉXITO\n\n"
            f"📄 Folio: {datos['folio']}\n"
            f"🚗 Vehículo: {datos['marca']} {datos['linea']} {datos['anio']}\n"
            f"📅 Vigencia: 30 días\n"
            f"✅ Estado: ACTIVO\n\n"
            "🆕 NUEVA FUNCIONALIDAD:\n"
            f"🔗 Su permiso incluye QR inteligente\n"
            f"📱 Al escanearlo va directo al estado: {URL_CONSULTA_BASE}/consulta/{datos['folio']}\n"
            f"❌ YA NO necesita escribir el folio manualmente\n\n"
            "Para otro trámite, use /permiso nuevamente."
        )
        
    except Exception as e:
        await message.answer(
            f"💥 ERROR EN EL SISTEMA DE OAXACA\n\n"
            f"Fallo: {str(e)}\n\n"
            "Intente nuevamente con /permiso"
        )
    finally:
        await state.clear()

@dp.message()
async def fallback(message: types.Message):
    respuestas_random = [
        "🌮 No entiendo, compadre. Use /permiso para tramitar en Oaxaca.",
        "🚗 Para permisos de Oaxaca use: /permiso",
        "🎯 Directo al grano: /permiso para iniciar su trámite oaxaqueño.",
        "🔥 Sistema de Oaxaca con QR inteligente: /permiso",
    ]
    await message.answer(random.choice(respuestas_random))

# ------------ FASTAPI + LIFESPAN ------------
_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook", allowed_updates=["message"])
        _keep_task = asyncio.create_task(keep_alive())
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# ------------ ENDPOINT PARA QR DINÁMICO (PRINCIPAL) ------------
@app.get("/consulta/{folio}")
async def consulta_folio_directo(folio: str):
    try:
        response = supabase.table("folios_registrados") \
            .select("*") \
            .eq("folio", folio) \
            .eq("entidad", "oaxaca") \
            .execute()
        
        if response.data:
            registro = response.data[0]
            fecha_vencimiento = datetime.fromisoformat(registro["fecha_vencimiento"]).date()
            hoy = datetime.now().date()
            
            if fecha_vencimiento >= hoy:
                estado_visual = "VIGENTE"
                color_estado = "#28a745"
                icono = "✅"
                mensaje = "Su permiso está ACTIVO para circular"
            else:
                estado_visual = "VENCIDO"
                color_estado = "#dc3545"
                icono = "❌"
                mensaje = "Su permiso ha VENCIDO. Debe renovar"
            
            html_content = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Permiso Oaxaca - Folio {folio}</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:linear-gradient(135deg,#ff9a9e 0%,#fecfef 100%);margin:0;padding:20px;min-height:100vh}}
.container{{max-width:450px;margin:0 auto;background:white;border-radius:20px;box-shadow:0 15px 35px rgba(0,0,0,0.1);overflow:hidden;animation:slideIn 0.5s ease-out}}
@keyframes slideIn{{from{{transform:translateY(30px);opacity:0}}to{{transform:translateY(0);opacity:1}}}}
.header{{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;padding:30px 20px;text-align:center}}
.header h1{{margin:0;font-size:1.4em}}.header h2{{margin:10px 0 0 0;font-size:1em;opacity:0.9}}
.content{{padding:40px 30px;text-align:center}}
.estado{{font-size:2.2em;font-weight:700;color:{color_estado};margin:20px 0;text-shadow:0 2px 4px rgba(0,0,0,0.1)}}
.folio{{font-size:1.6em;font-weight:bold;color:#333;margin:15px 0;letter-spacing:1px;background:#f8f9fa;padding:10px;border-radius:8px}}
.mensaje{{font-size:1.1em;color:#555;margin:25px 0;line-height:1.6}}
.info-box{{background:linear-gradient(135deg,#f8f9fa 0%,#e9ecef 100%);border-radius:12px;padding:20px;margin:20px 0;text-align:left}}
.info-row{{display:flex;justify-content:space-between;margin:8px 0;padding:5px 0;border-bottom:1px solid #dee2e6}}
.info-row:last-child{{border-bottom:none}}.label{{font-weight:600;color:#495057}}.value{{color:#6c757d}}
.footer{{background:#f8f9fa;padding:20px;text-align:center;font-size:0.85em;color:#6c757d;border-top:1px solid #dee2e6}}
.refresh-btn{{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;border:none;padding:12px 25px;border-radius:25px;font-size:0.9em;font-weight:600;margin:20px 0;cursor:pointer;transition:transform 0.2s}}
.refresh-btn:hover{{transform:translateY(-2px);box-shadow:0 5px 15px rgba(0,0,0,0.2)}}
</style></head><body><div class="container"><div class="header"><h1>🏛️ ESTADO DE OAXACA</h1><h2>Consulta de Permiso de Circulación</h2></div>
<div class="content"><div class="estado">{icono} {estado_visual}</div><div class="folio">Folio: {folio}</div><div class="mensaje">{mensaje}</div>
<div class="info-box">
<div class="info-row"><span class="label">Vehículo:</span><span class="value">{registro["marca"]} {registro["linea"]}</span></div>
<div class="info-row"><span class="label">Año:</span><span class="value">{registro["anio"]}</span></div>
<div class="info-row"><span class="label">Serie:</span><span class="value">{registro["numero_serie"]}</span></div>
<div class="info-row"><span class="label">Motor:</span><span class="value">{registro["numero_motor"]}</span></div>
<div class="info-row"><span class="label">Color:</span><span class="value">{registro["color"]}</span></div>
<div class="info-row"><span class="label">Titular:</span><span class="value">{registro["nombre"]}</span></div>
<div class="info-row"><span class="label">Fecha de Vencimiento:</span><span class="value">{fecha_vencimiento.strftime("%d/%m/%Y")}</span></div>
</div><button class="refresh-btn" onclick="window.location.reload()">🔄 Actualizar Estado</button></div>
<div class="footer"><p>📅 Consulta: {datetime.now().strftime("%d/%m/%Y a las %H:%M")}</p><p>🌮 Sistema Digital de Oaxaca</p></div>
</div><script>setTimeout(() => window.location.reload(), 30000);</script></body></html>"""
            
            return HTMLResponse(content=html_content)
        else:
            return HTMLResponse(content=f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><title>No Encontrado</title>
<style>body{{font-family:Arial;background:#ff6b6b;margin:0;padding:20px;min-height:100vh;display:flex;align-items:center;justify-content:center}}
.container{{max-width:400px;background:white;border-radius:20px;padding:40px;text-align:center;box-shadow:0 15px 35px rgba(0,0,0,0.2)}}
.icono{{font-size:4em;margin-bottom:20px}}.titulo{{font-size:1.5em;font-weight:bold;color:#333;margin-bottom:15px}}
.mensaje{{color:#666;line-height:1.6}}</style></head><body><div class="container"><div class="icono">❌</div><div class="titulo">Folio No Encontrado</div>
<div class="mensaje">El folio <strong>{folio}</strong> no está registrado en Oaxaca.<br><br>🔍 Verifique que el QR sea correcto.</div></div></body></html>""")
    except Exception as e:
        print(f"[ERROR] Consulta folio {folio}: {e}")
        return HTMLResponse(content=f"<h1>Error del sistema: {str(e)}</h1>")

@app.get("/consulta_folio")
async def consulta_folio_legacy():
    html_redirect = """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Sistema de Consulta Oaxaca</title><style>
body{font-family:Arial,sans-serif;background:linear-gradient(135deg,#ff9a9e 0%,#fecfef 100%);margin:0;padding:20px;min-height:100vh;display:flex;align-items:center;justify-content:center}
.container{max-width:450px;background:white;border-radius:20px;box-shadow:0 15px 35px rgba(0,0,0,0.1);padding:40px 30px;text-align:center}
.header{color:#333;margin-bottom:30px}.info{color:#666;line-height:1.6;margin:20px 0}.input-group{margin:25px 0}
.input-group input{width:100%;padding:15px;border:2px solid #ddd;border-radius:10px;font-size:1.1em;text-align:center;letter-spacing:1px}
.btn{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:white;border:none;padding:15px 30px;border-radius:25px;font-size:1.1em;font-weight:600;cursor:pointer;transition:transform 0.2s}
.btn:hover{transform:translateY(-2px);box-shadow:0 5px 15px rgba(0,0,0,0.3)}
.note{background:#e3f2fd;padding:15px;border-radius:10px;color:#1976d2;margin:20px 0;font-size:0.9em}
.qr-highlight{background:#e8f5e8;border-left:4px solid #28a745;padding:15px;margin:20px 0;font-size:0.95em;color:#155724}
</style></head><body><div class="container"><div class="header"><h1>🏛️ ESTADO DE OAXACA</h1><h2>Consulta de Permiso de Circulación</h2></div>
<div class="info">Ingrese su número de folio para consultar el estado de su permiso:</div>
<div class="input-group"><input type="text" id="folioInput" placeholder="Ejemplo: 1770" maxlength="10"></div>
<button class="btn" onclick="consultarFolio()">🔍 Consultar Estado</button>
<div class="qr-highlight">🆕 <strong>NOVEDAD:</strong> Los nuevos permisos tienen QR que reemplaza el texto del folio. Solo escanéelo para acceso directo.</div>
<div class="note">💡 Si tiene un permiso anterior, puede consultar escribiendo el folio aquí.</div>
</div><script>function consultarFolio(){const folio=document.getElementById('folioInput').value.trim();if(!folio){alert('Por favor ingrese un número de folio válido');return;}window.location.href=`/consulta/${folio}`;}
document.getElementById('folioInput').addEventListener('keypress',function(e){if(e.key==='Enter'){consultarFolio();}});</script></body></html>"""
    return HTMLResponse(content=html_redirect)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}

@app.get("/")
async def health():
    return {
        "ok": True, 
        "bot": "Oaxaca Permisos Sistema", 
        "status": "running",
        "qr_dinamico": "REEMPLAZA_TEXTO_COMPLETO",
        "url_consulta": URL_CONSULTA_BASE,
        "coordenadas_qr": coords_qr_dinamico
    }

if __name__ == '__main__':
    try:
        import uvicorn
        port = int(os.getenv("PORT", 8000))
        print(f"[ARRANQUE OAXACA] Servidor iniciando en puerto {port}")
        print(f"[QR DINÁMICO] URL base: {URL_CONSULTA_BASE}")
        print(f"[QR POSICIÓN] X:{coords_qr_dinamico['x']}, Y:{coords_qr_dinamico['y']}")
        print(f"[REEMPLAZO TOTAL] QR sustituye 100% el texto del folio")
        print(f"[ENDPOINTS] /consulta/{{folio}} - QR directo")
        print(f"[ENDPOINTS] /consulta_folio - Entrada manual legacy")
        print(f"[SISTEMA] Oaxaca QR dinámico ACTIVO - texto eliminado")
        uvicorn.run(app, host="0.0.0.0", port=port)
    except Exception as e:
        print(f"[ERROR FATAL] No se pudo iniciar: {e}")
